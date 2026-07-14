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

Masking policy
--------------

The ONLY reason to mask a cwd entry is that it is not already safely
exposed: a non-allowed dotfile (a secret the operator didn't opt in to)
or an *escaping symlink* (a link whose target resolves OUTSIDE the
exposed roots — the host-relative dereference defense). Real in-cwd
files and directories are already exposed by the cwd bind and are left
readable and untouched. In particular there is NO name-based special
casing: a directory called ``node_modules`` is treated like any other.

- A **real npm/yarn ``node_modules``** (real package directories, few
  or no escaping symlinks) is walked normally and stays readable, so
  ``node app.js`` / ``npm test`` / local CLIs work.
- A **cross-store pnpm ``node_modules``** (where the content-addressable
  store lives on another filesystem, so each package is an escaping
  symlink into ``~/.local/share/pnpm/store``) is a farm of thousands of
  escaping symlinks. Same-store (hardlink) pnpm has NO escaping symlinks
  and is simply readable.

Subtree collapse (bounded emission)
-----------------------------------

Each :class:`MaskedEntry` becomes one mask token per backend, and bwrap
has a hard ceiling of 9000 total argv entries, so a farm of tens of
thousands of escaping symlinks would blow the command line if each were
masked individually. The escaping symlinks of a cross-store pnpm are
*spread* — roughly one per ``.pnpm/<pkg>@<ver>/node_modules/`` directory
— so a per-directory dominance test never fires; the density only shows
up when aggregated over a subtree.

So collapse is decided over the **subtree**: after the walk, a directory
``D`` is collapsed to a single ``kind="dir"`` mask (replacing every mask
beneath it) when

- the number of masks within ``D``'s subtree reaches
  :data:`_SUBTREE_COLLAPSE_THRESHOLD`, AND
- those masks *dominate* the directory's browsable content —
  ``masks >= _COALESCE_DOMINANCE_FRACTION * (masks + readable_files)`` —
  so a directory that mixes a farm with real, readable source is NEVER
  hidden wholesale.

Collapse fires at the **shallowest** dominated directory on each branch
(never the scan root itself), which for a cross-store pnpm is the
``node_modules`` dir — collapsing the whole farm to one mask — while a
directory of real source that merely *contains* a nested farm keeps its
own files readable (only the nested farm dir collapses). The dominance
guard is what makes this safe: a real ``node_modules`` full of readable
package files has ~zero masks, never reaches the threshold, and stays
readable.

``allow_hidden`` directories
----------------------------

A directory whose basename is on ``allow_hidden`` (e.g. ``.git`` when
the operator sets ``cwd_allow_hidden: [".git"]``) is left READABLE at
the top level — the directory itself is not masked — but is still WALKED
so nested non-allowed secrets (``.git`` interior ``.env`` / ``.aws`` /
``.ssh`` dotfiles, escaping symlinks) are masked as normal. The subtree
collapse above still applies inside it, so a dense interior farm (e.g. a
large ``.git/worktrees`` admin tree of escaping symlinks) collapses to a
few masks rather than thousands. Because a readable directory like
``.git`` carries lots of browsable content (loose objects, config,
refs), the dominance guard keeps the directory itself from collapsing.

Bounded traversal
-----------------

- ``max_entries`` caps how many filesystem entries the recursive walk
  is allowed to visit. The walker prunes at masked dot-directories
  (their contents are never counted once the directory itself is
  masked). A non-allowed dep dir that is a real, readable tree is
  walked in full, so a very large workspace can approach the cap.
- ``overflow`` chooses behaviour when the cap is hit:

  - ``"warn"``: emit a ``CRITICAL`` log line, stop scanning, and
    return the partial mask built up so far. Dotfiles past the cap
    remain visible.
  - ``"error"``: raise :class:`OSError` with an actionable message
    naming both spec keys the user can tune. Fail-Loud — the right
    pick for untrusted source trees.
  - ``"unlimited"``: ignore the cap and walk the full tree. O(N) on
    total entries; safe but can be slow on huge monorepos. The subtree
    collapse keeps even this mode from emitting an unbounded number of
    masks for symlink-dense farms.

When the cap is hit, the overflow message names the directories the
walk did not finish — distinguishing the one directory it was mid-scan
of ("partially scanned") from those it never reached ("not scanned").
The list is bounded (see :data:`_MAX_UNFINISHED_DIRS_REPORTED`) so a
pathological tree can't produce a multi-KB log line.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_LOGGER = logging.getLogger(__name__)


MaskKind = Literal["file", "dir"]

# Subtree collapse: a directory whose subtree accumulates at least this
# many masks (escaping symlinks + non-allowed dotfiles) is collapsed to
# a single ``kind="dir"`` mask, provided the masks also dominate the
# directory's browsable content (see _COALESCE_DOMINANCE_FRACTION).
# Chosen well below bwrap's 9000-arg ceiling (each mask costs 2-3 argv
# tokens) yet far above any plausible count of intentional escaping
# symlinks / secrets in real project content, so collapse only fires on
# genuine farms (a cross-store pnpm node_modules, a huge escaping-link
# admin tree) — never on a hand-authored source directory.
_SUBTREE_COLLAPSE_THRESHOLD = 100

# The collapse additionally requires masks to DOMINATE the directory:
# at least this fraction of ``masks + readable_files`` in the subtree
# must be masks. A directory that mixes a farm with real readable source
# is therefore never hidden wholesale — its files stay browsable and its
# masks are emitted individually (still bounded well under the ceiling).
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
        (including a whole subtree collapsed to one mask).
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
) -> list[MaskedEntry]:
    """
    Walk *cwd* and identify entries that must be masked from the helper.

    Iterative DFS over *cwd*. For each entry it visits, the entry is
    masked when EITHER:

    - the basename starts with ``.`` and is not in *allow_hidden*, OR
    - the entry is a symlink whose resolved target lies outside every
      path in *safe_roots*.

    A masked dot-directory is pruned (not descended into). Every other
    real directory — including an ``allow_hidden`` directory, which stays
    readable at the top level — IS walked so nested secrets are masked.

    After the walk, dense farms are collapsed: a directory whose subtree
    accumulates at least :data:`_SUBTREE_COLLAPSE_THRESHOLD` masks that
    also dominate the directory's readable content (see
    :data:`_COALESCE_DOMINANCE_FRACTION`) is replaced by a single
    ``kind="dir"`` mask, at the shallowest such directory on each branch
    (never the scan root). This bounds the emitted mask count for a
    cross-store pnpm ``node_modules`` (a spread farm of escaping
    symlinks) while leaving a real, readable ``node_modules`` — which has
    ~zero masks — untouched.

    Walker termination is deterministic: ``follow_symlinks=False`` on the
    recursion check ensures symlink loops can't cause infinite descent.

    :param cwd: Absolute, resolved-without-strict path of the helper's
        working directory. The walk starts here. Must be a real
        directory; if it isn't, the function returns an empty list
        without raising (the backend wraps the missing dir at spawn time
        and gets the kernel error message).
    :param allow_hidden: Dotfile/dotdir basenames that pass through
        unmasked at any depth, e.g. ``[".git"]``. Matched by basename.
        An allowed *directory* is left readable at the top level but is
        still walked so its non-allowed nested secrets are masked. Pass
        an empty sequence to mask every dotfile.
    :param safe_roots: Paths the sandbox already exposes (typically
        ``cwd``, the backend's default mounts, the policy's read / write
        roots). A symlink whose resolved target lies inside any of these
        is considered safe and not masked. The backend assembles this
        list — bwrap and Seatbelt expose different system paths.
    :param max_entries: Cap on the number of filesystem entries the
        walker may visit. Set to a large value together with
        ``overflow="unlimited"`` to disable the cap.
    :param overflow: One of ``"error"``, ``"warn"``, ``"unlimited"``.
        See module docstring for per-mode semantics.
    :param logger_name: Logger name used for the warn-mode warning
        message. ``None`` falls back to this module's logger.
    :param scope_label: Short label used in overflow log / error
        messages to identify what the walker was scanning. Defaults to
        ``"cwd"``; backends re-using the walker for ``read_paths`` roots
        pass e.g. ``"read_paths"``.
    :returns: A list of :class:`MaskedEntry`. Empty when *cwd* has
        nothing worth masking or when *cwd* is not a directory.
    :raises OSError: When the cap is reached and *overflow* is
        ``"error"``.
    """
    if not cwd.is_dir():
        return []

    allow = set(allow_hidden)
    safe_root_list = list(safe_roots)
    cap_enabled = overflow != "unlimited"
    logger = logging.getLogger(logger_name) if logger_name else _LOGGER

    root_key = str(cwd)
    mask_records: list[tuple[Path, MaskKind]] = []
    # Per-directory subtree aggregates used only by the collapse pass:
    # how many masks live under each ancestor directory, and how many
    # readable (browsable) leaf entries — so a farm can be told apart
    # from real source.
    mask_count: dict[str, int] = defaultdict(int)
    readable_count: dict[str, int] = defaultdict(int)

    stack: list[Path] = [cwd]
    entries_visited = 0
    truncated = False
    partial_dir: Path | None = None

    while stack and not truncated:
        current = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            # Unreadable directory — skip without masking. The parent is
            # in the safe set; its inaccessibility doesn't leak content.
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
                # symlinks it returns False; the backend's "file" emitter
                # handles both.
                kind: MaskKind = "dir" if child.is_dir() else "file"
                mask_records.append((child_path, kind))
                _increment_ancestors(mask_count, child_path, cwd)
                # Prune: don't descend into a masked dir.
                continue

            # Not masked. Recurse into real directories (including
            # allow_hidden dirs, so nested secrets are still masked); a
            # rogue symlink-to-dir is not followed. Everything else is a
            # readable leaf that contributes to the dominance guard.
            if child.is_dir(follow_symlinks=False):
                stack.append(child_path)
            else:
                _increment_ancestors(readable_count, child_path, cwd)

    entries = _collapse_farms(mask_records, mask_count, readable_count, root_key)

    if truncated:
        _handle_scan_overflow(
            scope_label=scope_label,
            cwd=cwd,
            max_entries=max_entries,
            overflow=overflow,
            partial_dir=partial_dir,
            not_scanned=list(stack),
            entries_visited=entries_visited,
            masks_emitted=len(entries),
            logger=logger,
        )

    return entries


def _increment_ancestors(counter: dict[str, int], path: Path, root: Path) -> None:
    """
    Add one to *counter* for every ancestor directory of *path* from its
    immediate parent up to and including *root*.

    Used to build the per-directory subtree tallies the collapse pass
    consumes. *path* is always under *root* (the walk guarantees it), so
    the ascent stops at *root* and never leaks counts above the scan.

    :param counter: Accumulator keyed by ``str(directory)``.
    :param path: The masked entry / readable leaf being counted.
    :param root: The scan root; the ascent stops here (inclusive).
    """
    for ancestor in path.parents:
        counter[str(ancestor)] += 1
        if ancestor == root:
            break


def _collapse_farms(
    mask_records: list[tuple[Path, MaskKind]],
    mask_count: dict[str, int],
    readable_count: dict[str, int],
    root_key: str,
) -> list[MaskedEntry]:
    """
    Replace dense mask farms with a single directory mask each.

    A directory ``D`` (never the scan *root_key*) is a collapse candidate
    when its subtree holds at least :data:`_SUBTREE_COLLAPSE_THRESHOLD`
    masks AND those masks dominate its browsable content
    (``masks >= _COALESCE_DOMINANCE_FRACTION * (masks + readable)``).
    Collapse fires at the SHALLOWEST candidate on each branch (a
    candidate with no candidate ancestor), so a spread farm collapses at
    the one directory that contains it rather than at each sub-cluster,
    and a directory of real source that merely contains a nested farm
    keeps its own files readable while the nested farm dir collapses.

    Every individual mask under a collapse directory is dropped in favour
    of the single directory mask; masks outside all collapse directories
    are kept as-is. Output is de-duplicated by path.

    :param mask_records: ``(path, kind)`` for every individually masked
        entry from the walk, in visit order.
    :param mask_count: Per-directory subtree mask tally.
    :param readable_count: Per-directory subtree readable-leaf tally.
    :param root_key: ``str`` of the scan root; excluded from collapse so
        the whole working directory can never be hidden.
    :returns: The final de-duplicated :class:`MaskedEntry` list.
    """
    candidates = {
        directory
        for directory, masks in mask_count.items()
        if directory != root_key
        and masks >= _SUBTREE_COLLAPSE_THRESHOLD
        and masks >= _COALESCE_DOMINANCE_FRACTION * (masks + readable_count.get(directory, 0))
    }
    # Keep only the shallowest candidate on each branch: a candidate that
    # has a candidate ancestor would be redundant (its parent collapses
    # the whole cluster already).
    collapse = {
        directory
        for directory in candidates
        if not _has_ancestor_in(directory, candidates, root_key)
    }

    seen: set[str] = set()
    entries: list[MaskedEntry] = []
    for path, kind in mask_records:
        if _is_under_any(str(path), collapse):
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        entries.append(MaskedEntry(path=path, kind=kind))
    for directory in sorted(collapse):
        if directory in seen:
            continue
        seen.add(directory)
        entries.append(MaskedEntry(path=Path(directory), kind="dir"))
    return entries


def _has_ancestor_in(directory: str, candidates: set[str], root_key: str) -> bool:
    """
    Return whether any proper ancestor of *directory* (up to but not
    including *root_key*) is in *candidates*.

    :param directory: The candidate directory path string.
    :param candidates: All collapse-candidate directory path strings.
    :param root_key: The scan root; the ascent stops before it.
    :returns: ``True`` when a shallower candidate already covers this one.
    """
    for ancestor in Path(directory).parents:
        ancestor_key = str(ancestor)
        if ancestor_key == root_key:
            break
        if ancestor_key in candidates:
            return True
    return False


def _is_under_any(path: str, directories: set[str]) -> bool:
    """
    Return whether *path* equals or lives under any dir in *directories*.

    :param path: Candidate path string.
    :param directories: Collapse directory path strings.
    :returns: ``True`` when *path* is covered by a collapse directory.
    """
    return any(path == d or path.startswith(d + os.sep) for d in directories)


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
