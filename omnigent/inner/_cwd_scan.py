"""
Backend-agnostic cwd dotfile / escaping-symlink walker.

Both spawn-time sandbox backends (``linux_bwrap`` and
``darwin_seatbelt``) need to identify the same set of cwd entries
that must be hidden from the sandboxed helper:

1. **Hidden entries** — any file/directory whose basename starts with
   ``.`` and is NOT in the spec's ``cwd_allow_hidden`` allowlist.
2. **Escaping symlinks** — any symlink (at any depth) whose target
   resolves outside the set of paths the sandbox already exposes.

The two backends emit different tokens for the same masked path
(``--bind /dev/null`` / ``--tmpfs`` for bwrap, ``(deny file-*)``
expressions for Seatbelt), but the *decision* of which paths to mask
is identical. Centralising the walker here guarantees both backends
hide exactly the same set of entries; only the emit shape differs.

Bounded emission
----------------

Each :class:`MaskedEntry` becomes one mask token per backend (bwrap
emits ``--bind /dev/null <path>`` / ``--tmpfs <path>``; Seatbelt a
``(deny ... (literal/subpath ...))`` line). bwrap in particular has a
hard ceiling of 9000 total argv entries, so the walker must keep the
mask count small even on trees with tens of thousands of maskable
leaves. Two coalescing rules do this WITHOUT walking (and emitting)
one entry per leaf:

- **Regenerable dep dirs** (:data:`_DEFAULT_COALESCE_DIRS`:
  ``node_modules``, ``.venv``, ``.mypy_cache``, ``.codex-tmp``) that
  are NOT on ``allow_hidden`` are masked as a SINGLE ``kind="dir"``
  entry and pruned — the walker never descends into them. Their
  contents are regenerable and shouldn't be exposed to the helper
  anyway, so hiding the whole directory is both cheaper and safer than
  masking each child. A pnpm ``node_modules`` (a symlink farm with
  tens of thousands of store links) collapses from ~N masks to one.
- **Generic symlink farm** — any other directory that is dominated by
  escaping symlinks (at least
  :data:`_ESCAPING_SYMLINK_COALESCE_THRESHOLD` of them AND at least
  :data:`_COALESCE_DOMINANCE_FRACTION` of its direct children) is
  likewise masked once and pruned. The dominance guard means a
  directory that mixes readable project files with a few escaping
  links is NEVER hidden wholesale — only near-pure farms coalesce.

``allow_hidden`` directories
----------------------------

A directory whose basename is on ``allow_hidden`` (e.g. ``.git`` when
the operator sets ``cwd_allow_hidden: [".git"]``) is meant to be
READABLE. The walker passes it through and does NOT descend into it:
deep-walking an allowed directory to mask its interior escaping
symlinks (e.g. a large ``.git/worktrees`` admin tree) both contradicts
"allowed = readable" and can emit thousands of masks. The interior of
an allowed directory is therefore left readable, not masked.

Bounded traversal
-----------------

- ``max_entries`` caps how many filesystem entries the recursive walk
  is allowed to visit. Realistic projects fit well under the default
  (50000) because the walker prunes at masked dot-directories and at
  coalesced dep dirs / symlink farms (their contents are never
  counted toward the cap once the directory itself is masked).
- ``overflow`` chooses behaviour when the cap is hit:

  - ``"warn"``: emit a ``CRITICAL`` log line, stop scanning, and
    return the partial mask built up so far. Dotfiles past the cap
    remain visible.
  - ``"error"``: raise :class:`OSError` with an actionable message
    naming both spec keys the user can tune. Fail-Loud — the right
    pick for untrusted source trees.
  - ``"unlimited"``: ignore the cap and walk the full tree. O(N) on
    total entries; safe but can be slow on huge monorepos. The
    coalescing rules above keep even this mode from emitting an
    unbounded number of masks for symlink-dense dep dirs.

When the cap is hit, the overflow message names the directories the
walk did not finish — distinguishing the one directory it was mid-scan
of ("partially scanned") from those it never reached ("not scanned")
— so an operator can tell at a glance which subtree was left unmasked.
The list is bounded (see :data:`_MAX_UNFINISHED_DIRS_REPORTED`) so a
pathological tree can't produce a multi-KB log line.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_LOGGER = logging.getLogger(__name__)


MaskKind = Literal["file", "dir"]

# Directory basenames whose subtrees are masked as a SINGLE
# ``kind="dir"`` entry and pruned (the walker never descends into
# them) when they are NOT on ``allow_hidden``. Kept as a module
# constant (rather than a spec field) so both backends get the same
# behaviour for free; callers can override via the ``coalesce_names``
# parameter of :func:`scan_cwd_mask_entries`.
#
# These are large, regenerable trees (``node_modules`` is a symlink
# farm; ``.venv`` / ``.mypy_cache`` / ``.codex-tmp`` are build caches)
# that rarely carry the project's own secrets and shouldn't be exposed
# to the sandboxed helper anyway. Masking the whole directory once is
# both cheaper (no per-leaf mask, so no bwrap arg explosion) and safer
# (contents hidden wholesale) than walking them. The dot-prefixed ones
# are already masked-and-pruned by the dotfile rule when un-allowed, so
# listing them here only changes behaviour for a plain-named dir like
# ``node_modules``; when a dot-dir IS on ``allow_hidden`` it is left
# readable and this set does not apply.
_DEFAULT_COALESCE_DIRS: tuple[str, ...] = (
    "node_modules",
    ".venv",
    ".mypy_cache",
    ".codex-tmp",
)

# Generic symlink-farm collapse: a single non-coalesce directory that
# would emit at least this many escaping-symlink child masks is
# collapsed to one ``kind="dir"`` mask and pruned, rather than emitting
# one mask per link. Chosen well below bwrap's 9000-arg ceiling (each
# mask costs 2-3 argv tokens) yet far above any plausible count of
# intentional escaping symlinks in real project content, so this only
# fires on symlink farms — never on hand-authored source directories.
_ESCAPING_SYMLINK_COALESCE_THRESHOLD = 100

# The generic collapse additionally requires escaping symlinks to
# DOMINATE the directory: at least this fraction of its direct children
# must be escaping symlinks. A directory that mixes readable content
# with many links is therefore never hidden wholesale — only near-pure
# farms coalesce. Guards against over-masking browsable project trees.
_COALESCE_DOMINANCE_FRACTION = 0.9

# Cap on how many unfinished-directory paths the overflow message
# lists before collapsing the rest into a ``(+N more)`` suffix, so a
# pathological tree can't produce a multi-KB log line / SBPL comment.
_MAX_UNFINISHED_DIRS_REPORTED = 10


@dataclass(frozen=True)
class MaskedEntry:
    """
    A single cwd entry the sandbox must hide from the helper.

    :param path: Absolute, resolved-without-strict path to the entry.
        Backend emitters use this verbatim — ``bwrap`` as the mount
        destination, ``sandbox-exec`` as the ``literal`` / ``subpath``
        argument.
    :param kind: ``"file"`` for regular files, symlinks, sockets, and
        broken-symlink fall-throughs; ``"dir"`` for real directories
        (including a coalesced dep dir / symlink farm masked wholesale).
        The bwrap emitter maps ``"file"`` to ``--bind /dev/null`` and
        ``"dir"`` to ``--tmpfs``; the Seatbelt emitter maps ``"file"``
        to ``(deny ... (literal ...))`` and ``"dir"`` to
        ``(deny ... (subpath ...))``.
    """

    path: Path
    kind: MaskKind


def scan_cwd_mask_entries(
    cwd: Path,
    *,
    allow_hidden: Sequence[str],
    safe_roots: Sequence[Path],
    max_entries: int,
    overflow: str,
    logger_name: str | None = None,
    scope_label: str = "cwd",
    coalesce_names: Sequence[str] = _DEFAULT_COALESCE_DIRS,
) -> list[MaskedEntry]:
    """
    Walk *cwd* and identify entries that must be masked from the helper.

    Iterative DFS over *cwd* with early termination at masked
    dot-directories, coalesced dep dirs, symlink farms, and
    ``allow_hidden`` directories (the walker never descends into any of
    these). For each entry it visits, the entry is masked when EITHER:

    - the basename starts with ``.`` and is not in *allow_hidden*, OR
    - the entry is a symlink whose resolved target lies outside every
      path in *safe_roots*.

    Directory-level coalescing keeps the emitted mask count bounded:

    - A directory whose basename is in *coalesce_names* (default:
      :data:`_DEFAULT_COALESCE_DIRS`) and is NOT on *allow_hidden* is
      masked as a single ``kind="dir"`` entry and pruned — not walked.
    - A directory dominated by escaping symlinks (see
      :data:`_ESCAPING_SYMLINK_COALESCE_THRESHOLD` /
      :data:`_COALESCE_DOMINANCE_FRACTION`) is likewise masked once and
      pruned.
    - A directory whose basename is on *allow_hidden* passes through
      readable and is NOT descended into, so its interior is never a
      source of per-entry masks.

    Walker termination is deterministic: ``follow_symlinks=False`` on
    the recursion check ensures symlink loops can't cause infinite
    descent, and every coalescing/pruning rule removes a directory from
    the work stack rather than adding to it.

    :param cwd: Absolute, resolved-without-strict path of the
        helper's working directory. The walk starts here. Must be a
        real directory; if it isn't, the function returns an empty
        list without raising (the backend wraps the missing dir at
        spawn time and gets the kernel error message).
    :param allow_hidden: Dotfile/dotdir basenames that pass through
        unmasked at any depth, e.g. ``[".git"]``. Matched by
        basename, so ``".venv"`` exempts both ``cwd/.venv`` and
        ``cwd/services/api/.venv``. An allowed *directory* is left
        readable and not descended into. Pass an empty sequence to
        mask every dotfile.
    :param safe_roots: Paths the sandbox already exposes (typically
        ``cwd``, the backend's default mounts, the policy's read /
        write roots). A symlink whose resolved target lies inside
        any of these is considered safe and not masked. The backend
        is responsible for assembling this list — bwrap and Seatbelt
        expose different system paths.
    :param max_entries: Cap on the number of filesystem entries the
        walker may visit. The walker counts every child returned by
        :func:`os.scandir`, masked or not, EXCEPT children of a
        coalesced/pruned directory (those are never visited). Set to a
        large value together with ``overflow="unlimited"`` to disable
        the cap.
    :param overflow: One of ``"error"``, ``"warn"``, ``"unlimited"``.
        See module docstring for per-mode semantics.
    :param logger_name: Logger name used for the warn-mode warning
        message. ``None`` falls back to this module's logger; backends
        pass their own logger name so the warning surfaces under the
        backend's logging namespace.
    :param scope_label: Short label used in overflow log / error
        messages to identify what the walker was scanning. Defaults
        to ``"cwd"``; backends that re-use the walker for
        ``read_paths`` roots pass e.g. ``"read_paths"``.
    :param coalesce_names: Directory basenames masked wholesale (as a
        single ``kind="dir"`` entry) and pruned when un-allowed;
        defaults to :data:`_DEFAULT_COALESCE_DIRS`. Matched by basename
        at any depth. A name that is also on *allow_hidden* is left
        readable (the allow rule wins). Pass an empty sequence to walk
        every directory in plain DFS order (the generic symlink-farm
        collapse still applies).
    :returns: A list of :class:`MaskedEntry`. Empty when *cwd* has
        nothing worth masking or when *cwd* is not a directory.
    :raises OSError: When the cap is reached and *overflow* is
        ``"error"``. The message names both tunable spec keys plus the
        directories the walk did not finish so a user hitting the cap
        can find the escape hatches and the culprit without re-reading
        source.
    """
    entries: list[MaskedEntry] = []
    if not cwd.is_dir():
        return entries

    allow = set(allow_hidden)
    safe_root_list = list(safe_roots)
    cap_enabled = overflow != "unlimited"
    logger = logging.getLogger(logger_name) if logger_name else _LOGGER

    coalesce = set(coalesce_names)
    seen: set[str] = set()
    stack: list[Path] = [cwd]
    entries_visited = 0
    truncated = False
    # The directory the walk was mid-``scandir`` of when the cap
    # tripped — reported as "partially scanned". ``None`` until then.
    partial_dir: Path | None = None

    while stack and not truncated:
        current = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            # Unreadable directory — skip without masking. The backend
            # will surface any deeper issue at spawn time. The parent
            # is in the safe set; its inaccessibility doesn't leak
            # content.
            continue

        # Generic symlink-farm collapse (backstop to the named-dir
        # coalesce below): a non-cwd directory dominated by escaping
        # symlinks is masked once and pruned, so a symlink farm that
        # isn't a known regenerable dep dir still can't emit one arg
        # per link. cwd itself is never collapsed — that would hide the
        # whole working directory. The dominance guard keeps readable
        # project content from being hidden wholesale.
        if current != cwd and _is_escaping_symlink_farm(children, safe_root_list):
            _record_mask(entries, seen, current, "dir")
            continue

        for child in children:
            entries_visited += 1
            if cap_enabled and entries_visited > max_entries:
                truncated = True
                partial_dir = current
                break

            child_path = Path(child.path)
            should_mask = False
            if child.name.startswith(".") and child.name not in allow:
                should_mask = True
            elif child.is_symlink():
                resolved_target = child_path.resolve(strict=False)
                if not any(_is_within(resolved_target, root) for root in safe_root_list):
                    should_mask = True

            if should_mask:
                # ``is_dir`` follows symlinks by default — matches what
                # the agent would observe through the bind. For broken
                # symlinks it returns False; the backend's "file"
                # emitter handles both.
                kind: MaskKind = "dir" if child.is_dir() else "file"
                _record_mask(entries, seen, child_path, kind)
                # Prune: don't descend into a masked dir.
                continue

            # Not masked — recurse only into real directories so a
            # rogue symlink-to-dir can't cause a loop.
            if child.is_dir(follow_symlinks=False):
                if child.name in allow:
                    # allow_hidden dir → operator opted into
                    # readability; pass through WITHOUT deep-walking.
                    # Deep-walking would mask its interior escaping
                    # symlinks (e.g. a large .git/worktrees admin tree)
                    # one token apiece, contradicting "allowed =
                    # readable" and risking the backend's arg ceiling.
                    continue
                if child.name in coalesce:
                    # Regenerable dep dir (node_modules, ...) not on
                    # allow_hidden: mask the whole dir once and prune
                    # instead of walking its (often huge) symlink farm.
                    _record_mask(entries, seen, child_path, "dir")
                    continue
                stack.append(child_path)

    if truncated:
        _handle_scan_overflow(
            scope_label=scope_label,
            cwd=cwd,
            max_entries=max_entries,
            overflow=overflow,
            partial_dir=partial_dir,
            # Directories the walk never finished: the one it was
            # mid-scan of (partial) plus everything still queued.
            not_scanned=list(stack),
            entries_visited=entries_visited,
            masks_emitted=len(entries),
            logger=logger,
        )

    return entries


def _record_mask(
    entries: list[MaskedEntry],
    seen: set[str],
    path: Path,
    kind: MaskKind,
) -> None:
    """
    Append a :class:`MaskedEntry` for *path*, de-duplicating by path.

    :param entries: Accumulator the new entry is appended to.
    :param seen: Set of already-masked path strings; *path* is skipped
        if present, otherwise added.
    :param path: Absolute path to mask.
    :param kind: ``"file"`` or ``"dir"`` mask kind.
    """
    key = str(path)
    if key in seen:
        return
    seen.add(key)
    entries.append(MaskedEntry(path=path, kind=kind))


def _is_escaping_symlink_farm(
    children: Sequence[os.DirEntry[str]],
    safe_roots: Sequence[Path],
) -> bool:
    """
    Return whether *children* make a directory a coalescible symlink farm.

    True only when the directory is dominated by escaping symlinks —
    at least :data:`_ESCAPING_SYMLINK_COALESCE_THRESHOLD` of them AND at
    least :data:`_COALESCE_DOMINANCE_FRACTION` of the direct children.
    Both guards must hold so a directory mixing readable files with a
    handful of escaping links is never coalesced (its links are masked
    individually instead).

    Symlink resolution (the expensive step) is only performed once the
    cheap ``is_symlink`` counts already clear both thresholds, so
    ordinary source directories — which have few or no symlinks — never
    pay for the escape check twice.

    :param children: The directory's direct children, from
        :func:`os.scandir`.
    :param safe_roots: Paths the sandbox already exposes; a symlink
        resolving inside any of these does not count as escaping.
    :returns: ``True`` when the directory should be masked wholesale.
    """
    total = len(children)
    if total < _ESCAPING_SYMLINK_COALESCE_THRESHOLD:
        return False
    # Cheap pre-filter on the scandir-cached ``is_symlink`` flag —
    # dotfile symlinks are excluded because the dotfile rule already
    # masks them and shouldn't drag a dir into wholesale hiding.
    symlink_children = [
        child for child in children if not child.name.startswith(".") and _safe_is_symlink(child)
    ]
    if len(symlink_children) < _ESCAPING_SYMLINK_COALESCE_THRESHOLD:
        return False
    if len(symlink_children) < _COALESCE_DOMINANCE_FRACTION * total:
        return False
    escaping = 0
    for child in symlink_children:
        resolved = Path(child.path).resolve(strict=False)
        if not any(_is_within(resolved, root) for root in safe_roots):
            escaping += 1
    return (
        escaping >= _ESCAPING_SYMLINK_COALESCE_THRESHOLD
        and escaping >= _COALESCE_DOMINANCE_FRACTION * total
    )


def _safe_is_symlink(child: os.DirEntry[str]) -> bool:
    """
    Return ``child.is_symlink()``, treating an OS error as "not a symlink".

    :param child: A :func:`os.scandir` entry.
    :returns: ``True`` when *child* is a symlink; ``False`` on
        ``is_symlink`` failure (e.g. the entry vanished mid-scan).
    """
    try:
        return child.is_symlink()
    except OSError:
        return False


def _handle_scan_overflow(
    *,
    scope_label: str,
    cwd: Path,
    max_entries: int,
    overflow: str,
    partial_dir: Path | None,
    not_scanned: Sequence[Path],
    entries_visited: int,
    masks_emitted: int,
    logger: logging.Logger,
) -> None:
    """
    React to the entry cap being hit: raise (``"error"``) or log a
    ``CRITICAL`` warning (``"warn"``).

    Builds the shared overflow message — which names the unfinished
    directories via :func:`_summarize_unfinished_dirs` — and then either
    fails loud or fails soft depending on *overflow*.

    :param scope_label: Human label for the scan scope, e.g. ``"cwd"``.
    :param cwd: The root the scan started from, e.g. ``Path("/work")``.
    :param max_entries: The cap that was exceeded, e.g. ``50000``.
    :param overflow: Resolved overflow mode, one of ``"error"``,
        ``"warn"``, ``"unlimited"``.
    :param partial_dir: The directory the walk was mid-scan of when the
        cap tripped; ``None`` if it tripped at a directory boundary.
    :param not_scanned: Directories still queued that were never
        entered, e.g. ``[Path("/work/src")]``.
    :param entries_visited: Total entries visited before stopping.
    :param masks_emitted: Number of mask entries produced so far.
    :param logger: The resolved logger to emit the CRITICAL warning on.
    :raises OSError: When *overflow* is ``"error"``.
    """
    unfinished = _summarize_unfinished_dirs(
        partial_dir=partial_dir,
        not_scanned=not_scanned,
    )
    message = (
        f"{scope_label} dotfile scan visited more than {max_entries} entries under "
        f"{cwd}. Unfinished directories (dotfiles inside these are NOT masked): "
        f"{unfinished}. Raise os_env.sandbox.cwd_hidden_scan_max_entries, or set "
        "os_env.sandbox.cwd_hidden_scan_overflow ('warn' = partial mask [default], "
        "'error' = fail loud, 'unlimited' = no cap)."
    )
    if overflow == "error":
        raise OSError(message)
    # ``"warn"`` and ``"unlimited"`` — fail-soft with an obvious log
    # line. We deliberately do NOT swallow the truncation silently;
    # entries past the cap remain visible to the agent.
    #
    # L6 (security): when ``overflow == "warn"`` (the default),
    # dotfiles past the cap are NOT masked, which means a deeply-
    # nested ``.aws`` / ``.ssh`` / ``.env`` checked in by the operator
    # (or planted by a previous compromised tool call) becomes
    # readable by the sandboxed agent. The warning escalates to
    # ``CRITICAL`` (naming the unfinished dirs) so it can't be lost in
    # INFO-noise logs. Untrusted trees should switch to ``"error"``.
    logger.critical(
        "%s Mask is incomplete (overflow=%r): %d entries "
        "visited so far, %d masks emitted. Dotfiles past the "
        "cap are READABLE by the sandboxed helper — including "
        "any credentials checked in or planted under cwd. "
        "Switch overflow to 'error' to fail loud instead, or "
        "raise cwd_hidden_scan_max_entries to scan the full "
        "tree.",
        message,
        overflow,
        entries_visited,
        masks_emitted,
    )


def _summarize_unfinished_dirs(
    *,
    partial_dir: Path | None,
    not_scanned: Sequence[Path],
) -> str:
    """
    Build the human-readable "unfinished directories" clause for an
    overflow message.

    Each directory is annotated with its state: "partially scanned" for
    the one the walk was mid-scan of (listed first), "not scanned" for
    those it never reached. The list is truncated to
    :data:`_MAX_UNFINISHED_DIRS_REPORTED` with a ``(+N more)`` suffix.

    Note the walker's coalescing rules mask-and-prune regenerable dep
    dirs / symlink farms / allow_hidden dirs before they are ever queued,
    so those never appear here — an unfinished dir is always a plain
    directory the cap stopped the walk inside of or before.

    :param partial_dir: The directory the walk was mid-``scandir`` of
        when the cap tripped. ``None`` when the cap tripped at a
        directory boundary (no dir is partial).
    :param not_scanned: Directories still queued that were never
        entered, e.g. ``[Path("/work/src")]``.
    :returns: A ``"; "``-joined summary string. Empty string when there
        is nothing to report (no partial dir and an empty *not_scanned*).
    """
    lines: list[str] = []
    if partial_dir is not None:
        lines.append(f"{partial_dir} (partially scanned)")
    for path in not_scanned:
        lines.append(f"{path} (not scanned)")

    shown = lines[:_MAX_UNFINISHED_DIRS_REPORTED]
    remainder = len(lines) - len(shown)
    summary = "; ".join(shown)
    if remainder > 0:
        summary += f" (+{remainder} more)"
    return summary


def _is_within(path: Path, root: Path) -> bool:
    """
    Return whether *path* equals or descends from *root*.

    Both paths are passed through :func:`Path.resolve` with
    ``strict=False`` first so symlinks pointing into a safe root
    (e.g. ``./.venv/bin/python -> /usr/bin/python3.12``) count as
    "within" the safe root for safety checks.

    :param path: Candidate path, e.g. ``/usr/bin/python3``.
    :param root: Prefix path, e.g. ``/usr``.
    :returns: ``True`` when *path* equals or lives under *root*
        after symlink-free resolution.
    """
    try:
        compare_path = path.resolve(strict=False)
        compare_root = root.resolve(strict=False)
        compare_path.relative_to(compare_root)
        return True
    except (ValueError, OSError):
        return False
