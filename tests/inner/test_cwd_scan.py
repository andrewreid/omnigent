"""
Shared cwd-walker decision tests for the sandbox backends.

The walker in :mod:`omnigent.inner._cwd_scan` is consumed by every
spawn-time sandbox backend (``linux_bwrap``, ``darwin_seatbelt``)
to decide which cwd entries must be masked from the helper. Backend
emit code (``--bind /dev/null`` / ``--tmpfs`` for bwrap, ``(deny
file-* (literal/subpath ...))`` for Seatbelt) lives in each backend
module and is asserted there. This module verifies the
**decision** layer once so a regression that would expose ``.env`` to
the agent fails the same test for both backends on whichever host
runs the suite.

Tests assert on :class:`MaskedEntry` tuples directly, not on backend-
specific tokens. They run on every platform — the walker is pure
Python and doesn't shell out to ``bwrap`` / ``sandbox-exec``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.inner._cwd_scan import (
    _ESCAPING_SYMLINK_COALESCE_THRESHOLD,
    MaskedEntry,
    scan_cwd_mask_entries,
)

# The walker contract says ``safe_roots`` should include cwd plus the
# backend-specific exposed mounts. For these decision-level tests we
# pass a minimal set: cwd and ``/usr`` (the only "system" root the
# escaping-symlink defense looks at on both Linux and macOS). Both
# backends expose ``/usr`` to the helper.
_SYSTEM_SAFE_ROOTS = (Path("/usr"),)
_DEFAULT_MAX = 50000


def _scan(
    cwd: Path,
    *,
    allow_hidden: list[str] | None = None,
    safe_roots: list[Path] | None = None,
    max_entries: int = _DEFAULT_MAX,
    overflow: str = "error",
    coalesce_names: list[str] | None = None,
) -> list[MaskedEntry]:
    """
    Thin wrapper that mirrors what each backend passes through.

    Passing ``cwd`` resolved (not strict) matches what
    ``bwrap_sandbox`` and ``seatbelt_sandbox`` do at spawn time. The
    tests in this module are written against absolute resolved paths
    on the returned :class:`MaskedEntry` instances.

    :param cwd: The throwaway tempdir each test mutates.
    :param allow_hidden: Dotfile/dotdir basenames to exempt.
        Defaults to ``[]`` (mask every dotfile) so each test states
        its allowlist intent explicitly.
    :param safe_roots: Override the safe-root set. Defaults to
        ``[cwd, /usr]`` — cwd because the walker always trusts
        traversal into its own tree, ``/usr`` to mimic the bwrap +
        seatbelt default mounts.
    :param max_entries: Visit cap. Default is the production
        baseline (50000).
    :param overflow: Behavior at the cap. Default ``"error"`` so the
        cap/overflow tests can assert on the raised :class:`OSError`;
        note this differs from the production default (``"warn"``),
        which is pinned in the spec-parser tests instead.
    :param coalesce_names: Directory basenames masked wholesale (as a
        single ``kind="dir"`` entry) and pruned when un-allowed.
        ``None`` (the default) lets the walker apply its own default
        (:data:`_DEFAULT_COALESCE_DIRS`); pass ``[]`` to disable the
        named-dir coalesce for the plain-DFS contrast cases.
    :returns: List of :class:`MaskedEntry`.
    """
    roots = [cwd.resolve(strict=False), *_SYSTEM_SAFE_ROOTS]
    if safe_roots is not None:
        roots = safe_roots
    # Only override the walker's default when the test asked to, so the
    # common case still exercises the production default (node_modules).
    if coalesce_names is None:
        return scan_cwd_mask_entries(
            cwd.resolve(strict=False),
            allow_hidden=allow_hidden or [],
            safe_roots=roots,
            max_entries=max_entries,
            overflow=overflow,
        )
    return scan_cwd_mask_entries(
        cwd.resolve(strict=False),
        allow_hidden=allow_hidden or [],
        safe_roots=roots,
        max_entries=max_entries,
        overflow=overflow,
        coalesce_names=coalesce_names,
    )


def _entry_for(entries: list[MaskedEntry], path: Path) -> MaskedEntry | None:
    """
    Look up a :class:`MaskedEntry` by absolute path.

    Comparing on ``Path`` directly works because the walker stores
    absolute paths sourced from :func:`os.scandir`. Callers pass the
    same absolute path they'd expect the backend to mount over.

    :param entries: Output of :func:`scan_cwd_mask_entries`.
    :param path: Absolute path to search for.
    :returns: The matching :class:`MaskedEntry`, or ``None`` if the
        walker chose not to mask it. Tests use ``None`` to assert
        "this path was allowed through".
    """
    needle = Path(path)
    for entry in entries:
        if entry.path == needle:
            return entry
    return None


# ---------------------------------------------------------------------------
# Top-level dotfile masking + symlink defense
# ---------------------------------------------------------------------------


def test_top_level_dotfile_is_marked_as_file(tmp_path: Path) -> None:
    """
    A top-level dotfile in cwd that isn't on the allowlist returns a
    :class:`MaskedEntry` with ``kind="file"``.

    This is the central security goal: project secrets in dotfiles
    must be marked for masking by the walker regardless of which
    backend consumes the result.
    """
    secret = tmp_path / ".env"
    secret.write_text("SECRET=42")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, secret)
    assert entry is not None, (
        f".env was not masked. Walker returned: {[(e.path, e.kind) for e in entries]}"
    )
    assert entry.kind == "file"


def test_top_level_dotdir_is_marked_as_dir(tmp_path: Path) -> None:
    """
    A top-level dot-directory (e.g. ``.aws``) returns a
    :class:`MaskedEntry` with ``kind="dir"`` so backends can pick
    the right "hide a directory" primitive.
    """
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\naws_access_key_id=x")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, aws)
    assert entry is not None
    assert entry.kind == "dir"


def test_allowlisted_dotdir_is_not_masked(tmp_path: Path) -> None:
    """
    A dot-directory on ``allow_hidden`` passes through unmasked at
    the top level. ``.venv`` is the documented default exemption so
    Python projects don't have their virtualenv hidden from the
    helper.
    """
    venv = tmp_path / ".venv"
    venv.mkdir()
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, venv) is None


def test_regular_file_is_not_masked(tmp_path: Path) -> None:
    """
    Non-dotfile content is never returned by the walker — the
    sandbox lets the helper read it through the cwd bind / SBPL
    allow rule. Regression here would over-mask everything in cwd.
    """
    plain = tmp_path / "regular.txt"
    plain.write_text("not secret")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, plain) is None


def test_symlink_pointing_outside_safe_roots_is_marked_as_file(tmp_path: Path) -> None:
    """
    A non-dotfile symlink whose target resolves outside every
    ``safe_roots`` entry returns a :class:`MaskedEntry` with
    ``kind="file"`` (the link itself, not the target).

    Backends translate this into a ``--bind /dev/null <link>`` or
    ``(deny file-* (literal <link>))`` — both reject reads through
    the link path. The escape they defend against is
    ``./link -> /etc/shadow`` showing up in cwd.
    """
    target = Path("/etc/shadow")  # exists on Linux + macOS, outside _SYSTEM_SAFE_ROOTS
    if not target.exists():
        pytest.skip("/etc/shadow not present on this host")
    link = tmp_path / "outward_link"
    link.symlink_to(target)
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, link)
    assert entry is not None, (
        f"Escaping symlink was not masked. Walker returned: {[(e.path, e.kind) for e in entries]}"
    )
    assert entry.kind == "file"


def test_symlink_pointing_into_safe_root_is_not_marked(tmp_path: Path) -> None:
    """
    A symlink whose target resolves inside ``safe_roots`` is NOT
    marked — the agent has legit reasons to symlink to system tools
    and over-masking would break realistic project layouts (e.g.
    ``./bin/python -> /usr/bin/python3``).
    """
    inside = Path("/usr/bin")
    if not inside.exists():
        pytest.skip("/usr/bin not present (unexpected on Linux/macOS)")
    link = tmp_path / "tool_link"
    link.symlink_to(inside)
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, link) is None


# ---------------------------------------------------------------------------
# Recursive dotfile masking
# ---------------------------------------------------------------------------


def test_nested_dotfile_is_marked(tmp_path: Path) -> None:
    """
    A dotfile under a regular subdirectory is masked. The previous
    pre-refactor walker only inspected cwd's immediate children,
    leaving ``cwd/services/api/.env`` exposed in monorepo layouts.
    The recursive walker now hides at any depth.
    """
    nested_dir = tmp_path / "services" / "api"
    nested_dir.mkdir(parents=True)
    secret = nested_dir / ".env"
    secret.write_text("DB_PASSWORD=secret")
    (nested_dir / "main.py").write_text("# normal file")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, secret)
    assert entry is not None
    assert entry.kind == "file"
    # Sibling non-dotfile stays visible.
    assert _entry_for(entries, nested_dir / "main.py") is None


def test_walker_prunes_at_masked_dotdir(tmp_path: Path) -> None:
    """
    Once a dot-directory is marked for masking, the walker must NOT
    descend into it. Two reasons: it would waste cap budget on
    entries the agent can't see anyway, and it would emit redundant
    masks that bloat the backend's argv / SBPL profile.
    """
    git_dir = tmp_path / ".git"
    (git_dir / "objects" / "ab").mkdir(parents=True)
    (git_dir / "objects" / "ab" / "cdef").write_text("blob")
    (git_dir / "config").write_text("[core]")
    entries = _scan(tmp_path)
    # The .git dir itself is masked.
    assert _entry_for(entries, git_dir) is not None
    # Nothing under .git appears as a separate entry.
    nested = [e for e in entries if str(e.path).startswith(str(git_dir) + os.sep)]
    assert nested == [], (
        "Walker descended into a masked .git directory. Expected "
        f"pruning at the dotdir boundary; got nested entries: "
        f"{[e.path for e in nested]}"
    )


def test_allowlist_matches_basename_at_any_depth(tmp_path: Path) -> None:
    """
    ``allow_hidden=[".venv"]`` exempts the basename at every depth,
    not just at cwd root. ``cwd/services/api/.venv`` passes through
    unmasked too.
    """
    (tmp_path / ".venv").mkdir()
    nested_venv = tmp_path / "services" / "api" / ".venv"
    nested_venv.mkdir(parents=True)
    nested_secret = tmp_path / "services" / "api" / ".env"
    nested_secret.write_text("SECRET")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, tmp_path / ".venv") is None
    assert _entry_for(entries, nested_venv) is None
    nested_secret_entry = _entry_for(entries, nested_secret)
    assert nested_secret_entry is not None, "Nested .env (not on allowlist) must still be masked."
    assert nested_secret_entry.kind == "file"


def test_nested_escaping_symlink_is_marked(tmp_path: Path) -> None:
    """
    The symlink-escape defense applies at any depth, not only at the
    cwd root. ``cwd/sub/leak -> /etc/shadow`` is masked the same way
    a top-level ``cwd/leak -> /etc/shadow`` would be.
    """
    target = Path("/etc/shadow")
    if not target.exists():
        pytest.skip("/etc/shadow not present on this host")
    sub = tmp_path / "sub"
    sub.mkdir()
    link = sub / "leak"
    link.symlink_to(target)
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, link)
    assert entry is not None
    assert entry.kind == "file"


def test_walker_does_not_follow_symlink_loops(tmp_path: Path) -> None:
    """
    A self-referential symlink (``cwd/loop -> cwd``) must not cause
    the walker to recurse forever. ``follow_symlinks=False`` on the
    recursion check is what guarantees this; if it regresses this
    test will hang rather than fail.

    The loop symlink resolves to cwd, which is inside ``safe_roots``,
    so the symlink itself is NOT masked — the walker just must not
    follow it for recursion.
    """
    (tmp_path / "loop").symlink_to(tmp_path)
    (tmp_path / "real_file").write_text("content")
    entries = _scan(tmp_path)
    assert _entry_for(entries, tmp_path / "real_file") is None


# ---------------------------------------------------------------------------
# Cap / overflow behavior
# ---------------------------------------------------------------------------


def test_overflow_error_raises_with_actionable_message(tmp_path: Path) -> None:
    """
    With ``overflow="error"`` (the production default), exceeding the
    cap raises :class:`OSError` whose message names both spec keys
    the user can tune — Fail-Loud per project conventions.
    """
    for i in range(50):
        (tmp_path / f"file_{i}.txt").write_text("x")
    with pytest.raises(OSError) as exc_info:
        _scan(tmp_path, max_entries=10, overflow="error")
    msg = str(exc_info.value)
    assert "cwd_hidden_scan_max_entries" in msg, (
        f"OSError must name the cap field so users can find the tuning knob. Got: {msg!r}"
    )
    assert "cwd_hidden_scan_overflow" in msg, (
        f"OSError must name the overflow field so users know about the "
        f"warn / unlimited escape hatches. Got: {msg!r}"
    )


def test_overflow_warn_returns_partial_mask_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    With ``overflow="warn"``, hitting the cap stops scanning, emits a
    logging warning, and returns the partial mask built so far. The
    warning must be visible because dotfiles past the cap remain
    exposed.
    """
    (tmp_path / ".env").write_text("SECRET")
    for i in range(50):
        (tmp_path / f"file_{i}.txt").write_text("x")
    caplog.set_level("WARNING", logger="omnigent.inner._cwd_scan")
    entries = _scan(tmp_path, max_entries=5, overflow="warn")
    env_entry = _entry_for(entries, tmp_path / ".env")
    assert env_entry is not None, (
        "Partial mask should still include the .env we created before the cap was hit."
    )
    assert any("Mask is incomplete" in record.message for record in caplog.records), (
        "Warn-mode overflow must emit a logging warning so the partial mask isn't silent. "
        f"Captured: {[r.message for r in caplog.records]}"
    )


def test_overflow_unlimited_walks_full_tree(tmp_path: Path) -> None:
    """
    With ``overflow="unlimited"``, the cap is ignored and every
    nested dotfile is masked regardless of how many regular entries
    are in cwd. Trade-off: O(N) on the cwd tree, but the user
    explicitly opted in.
    """
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    deep_env = deep / ".env"
    deep_env.write_text("DEEP_SECRET")
    for i in range(100):
        (tmp_path / f"sibling_{i}.txt").write_text("x")
    entries = _scan(tmp_path, max_entries=5, overflow="unlimited")
    entry = _entry_for(entries, deep_env)
    assert entry is not None, (
        "Unlimited overflow mode must still mask the deeply-nested .env. "
        "Either the walker bailed early or the recursion is broken."
    )


# ---------------------------------------------------------------------------
# Coalesced regenerable dep dirs (node_modules et al. masked wholesale)
# ---------------------------------------------------------------------------


def _make_escaping_symlink_farm(directory: Path, count: int) -> None:
    """
    Fill *directory* with *count* symlinks that escape every safe root.

    Each link points at ``/etc/hostname`` (a real file outside the
    test's ``[cwd, /usr]`` safe roots) so the walker classifies it as an
    escaping symlink — the same shape a pnpm ``node_modules/.pnpm`` store
    presents. The directory is created if it does not exist.

    :param directory: Directory to populate (created if missing).
    :param count: Number of escaping symlinks to create.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = Path("/etc/hostname")
    for i in range(count):
        (directory / f"link_{i:05d}").symlink_to(target)


def test_coalesced_dep_dir_masked_as_single_dir_and_pruned(tmp_path: Path) -> None:
    """
    A ``node_modules`` that is NOT on ``allow_hidden`` is masked as a
    single ``kind="dir"`` entry and its subtree is pruned — the walker
    never emits a separate entry for anything inside it, even a dotfile.

    This is the core of the arg-explosion fix: whatever lives under a
    regenerable dep dir collapses to one mask instead of one-per-child.
    """
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / ".npmrc").write_text("token=zzz")
    (node_modules / "m1.txt").write_text("x")
    (tmp_path / "regular.txt").write_text("x")

    entries = _scan(tmp_path)
    nm_entry = _entry_for(entries, node_modules)
    assert nm_entry is not None and nm_entry.kind == "dir", (
        "node_modules must be masked as a single directory entry."
    )
    nested = [e for e in entries if str(e.path).startswith(str(node_modules) + os.sep)]
    assert nested == [], (
        "The coalesced node_modules subtree must be pruned — no per-child "
        f"masks. Got nested entries: {[e.path for e in nested]}"
    )
    # Sibling project content stays visible.
    assert _entry_for(entries, tmp_path / "regular.txt") is None


def test_coalesced_dep_dir_symlink_farm_emits_exactly_one_entry(tmp_path: Path) -> None:
    """
    A pnpm-shaped ``node_modules`` — thousands of escaping store links —
    produces EXACTLY ONE :class:`MaskedEntry`, not one per link. This is
    the concrete regression the fix targets: without coalescing this
    farm would emit tens of thousands of masks and blow bwrap's
    9000-arg ceiling. The subtree must not be visited past the dir.
    """
    farm = tmp_path / "node_modules" / ".pnpm"
    _make_escaping_symlink_farm(farm, 500)

    entries = _scan(tmp_path, overflow="unlimited")
    node_modules = tmp_path / "node_modules"
    nm_entry = _entry_for(entries, node_modules)
    assert nm_entry is not None and nm_entry.kind == "dir", (
        "node_modules must coalesce to a single dir mask before its symlink farm is ever walked."
    )
    under_nm = [e for e in entries if str(e.path).startswith(str(node_modules) + os.sep)]
    assert under_nm == [], (
        "No entry under the coalesced node_modules may be emitted; the "
        f"symlink farm must be pruned unvisited. Got: {len(under_nm)} entries."
    )
    assert len(entries) == 1, (
        f"The whole tree should collapse to one mask (node_modules). "
        f"Got {len(entries)}: {[(e.path, e.kind) for e in entries]}"
    )


def test_allowed_dep_dir_is_readable_not_masked_and_not_walked(tmp_path: Path) -> None:
    """
    Operator opt-in wins: when ``node_modules`` is on ``allow_hidden``
    it is left readable — NOT masked — and (per the allow_hidden rule)
    NOT deep-walked, so its interior escaping symlinks are not masked
    either. This preserves "allowed = readable" and keeps an allowed
    dep dir from becoming a source of per-interior masks.
    """
    farm = tmp_path / "node_modules" / ".pnpm"
    _make_escaping_symlink_farm(farm, 200)

    entries = _scan(tmp_path, allow_hidden=["node_modules"], overflow="unlimited")
    assert _entry_for(entries, tmp_path / "node_modules") is None, (
        "An allow_hidden node_modules must be readable, not masked."
    )
    under_nm = [
        e for e in entries if str(e.path).startswith(str(tmp_path / "node_modules") + os.sep)
    ]
    assert under_nm == [], (
        "An allow_hidden dir must not be deep-walked; its interior "
        f"escaping symlinks must not be masked. Got: {len(under_nm)} entries."
    )


# ---------------------------------------------------------------------------
# Generic symlink-farm collapse (Part 1 backstop, non-dep dirs)
# ---------------------------------------------------------------------------


def test_generic_symlink_farm_collapses_to_single_dir(tmp_path: Path) -> None:
    """
    A directory NOT in the coalesce set but dominated by escaping
    symlinks (>= the threshold, >= the dominance fraction) is masked
    once as a ``kind="dir"`` entry and pruned. Backstop for symlink
    farms that aren't a known regenerable dep dir.
    """
    farm = tmp_path / "store"
    _make_escaping_symlink_farm(farm, _ESCAPING_SYMLINK_COALESCE_THRESHOLD + 10)

    entries = _scan(tmp_path, overflow="unlimited")
    farm_entry = _entry_for(entries, farm)
    assert farm_entry is not None and farm_entry.kind == "dir", (
        "A near-pure escaping-symlink farm must collapse to one dir mask."
    )
    under = [e for e in entries if str(e.path).startswith(str(farm) + os.sep)]
    assert under == [], f"The collapsed farm must be pruned. Got nested: {[e.path for e in under]}"


def test_generic_collapse_does_not_fire_below_threshold(tmp_path: Path) -> None:
    """
    A handful of escaping symlinks (below the threshold) are masked
    INDIVIDUALLY, not collapsed — the whole-dir collapse must not fire
    for the ordinary "a few escaping links" case.
    """
    sub = tmp_path / "sub"
    _make_escaping_symlink_farm(sub, 3)

    entries = _scan(tmp_path, overflow="unlimited")
    assert _entry_for(entries, sub) is None, (
        "A dir with only a few escaping links must not be collapsed."
    )
    for i in range(3):
        link = sub / f"link_{i:05d}"
        entry = _entry_for(entries, link)
        assert entry is not None and entry.kind == "file", (
            f"{link} must be masked individually below the collapse threshold."
        )


def test_generic_collapse_respects_dominance_and_keeps_content_visible(tmp_path: Path) -> None:
    """
    A directory with many escaping symlinks but ALSO abundant readable
    content (so links are below the dominance fraction) is NOT
    collapsed: readable files stay visible and each link is masked
    individually. Guards against over-masking browsable project trees.
    """
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    # Threshold escaping links, but an equal amount of readable files so
    # links are only ~50% of children — under the dominance fraction.
    _make_escaping_symlink_farm(mixed, _ESCAPING_SYMLINK_COALESCE_THRESHOLD)
    for i in range(_ESCAPING_SYMLINK_COALESCE_THRESHOLD):
        (mixed / f"src_{i:05d}.txt").write_text("readable")

    entries = _scan(tmp_path, overflow="unlimited")
    assert _entry_for(entries, mixed) is None, (
        "A dir mixing readable content with links must not be hidden "
        "wholesale — the dominance guard must keep it browsable."
    )
    # A readable file stays visible; a link is still masked individually.
    assert _entry_for(entries, mixed / "src_00000.txt") is None
    link_entry = _entry_for(entries, mixed / "link_00000")
    assert link_entry is not None and link_entry.kind == "file"


# ---------------------------------------------------------------------------
# allow_hidden directories are readable, not deep-walked (Part 3)
# ---------------------------------------------------------------------------


def test_allow_hidden_dir_is_not_deep_walked_for_masking(tmp_path: Path) -> None:
    """
    An ``allow_hidden`` directory (e.g. ``.git`` when the operator sets
    ``cwd_allow_hidden: [".git"]``) passes through readable and is NOT
    descended into: an interior escaping symlink (mimicking a
    ``.git/worktrees`` admin link) is left unmasked, so the allowed dir
    never becomes a source of per-interior mask emits. A sibling
    non-allowed dotfile must still be masked — the dotfile guarantee is
    unchanged for entries that were not opted in.
    """
    git_dir = tmp_path / ".git"
    wt = git_dir / "worktrees" / "wt1"
    wt.mkdir(parents=True)
    (wt / "gitdir").symlink_to("/etc/hostname")  # escapes cwd + /usr
    (tmp_path / ".env").write_text("SECRET=1")

    entries = _scan(tmp_path, allow_hidden=[".git"])
    assert _entry_for(entries, git_dir) is None, ".git (allowed) must be readable."
    under_git = [e for e in entries if str(e.path).startswith(str(git_dir) + os.sep)]
    assert under_git == [], (
        "allow_hidden .git must not be deep-walked; its interior escaping "
        f"symlinks must not be masked. Got: {[e.path for e in under_git]}"
    )
    # The non-allowed sibling dotfile is still masked.
    env_entry = _entry_for(entries, tmp_path / ".env")
    assert env_entry is not None and env_entry.kind == "file"


# The dot-prefixed coalesce basenames. When NOT on ``allow_hidden`` the
# dotfile rule masks + prunes them before the coalesce branch is
# reached, so membership in the coalesce set is a no-op for them.
_COALESCE_DOT_DIRS = [".venv", ".mypy_cache", ".codex-tmp"]


@pytest.mark.parametrize("dotdir", _COALESCE_DOT_DIRS)
def test_unallowed_coalesce_dot_dir_is_masked_not_walked(dotdir: str, tmp_path: Path) -> None:
    """
    When a coalesce dot-dir is NOT on ``allow_hidden``, the dotfile rule
    masks it as a directory and prunes it (its contents never walked) —
    membership in the coalesce set changes nothing here because the
    dotfile decision runs first.

    This guards the branch ordering: masking is decided before the
    coalesce branch, so a name in the coalesce set can never be promoted
    to "walked" while un-allowed.
    """
    cache = tmp_path / dotdir
    cache.mkdir()
    (cache / ".inner_secret").write_text("SECRET=1")
    entries = _scan(tmp_path)  # allow_hidden=[] → mask every dotfile
    cache_entry = _entry_for(entries, cache)
    assert cache_entry is not None and cache_entry.kind == "dir", (
        f"Un-allowed {dotdir} must be masked as a directory, not walked."
    )
    nested = [e for e in entries if str(e.path).startswith(str(cache) + os.sep)]
    assert nested == [], (
        f"Walker descended into a masked {dotdir}; masking + pruning must "
        f"win for an un-allowed dotdir. Got: {[e.path for e in nested]}"
    )


# ---------------------------------------------------------------------------
# Overflow message names the unfinished directories
# ---------------------------------------------------------------------------


def test_overflow_error_message_names_unfinished_dir(tmp_path: Path) -> None:
    """
    The ``error`` overflow message must name the directory the walk did
    not finish so an operator can see at a glance which subtree was left
    unmasked, and keep naming both tuning knobs.

    A failure here means the enriched message regressed back to the
    counts-only form, which left operators guessing which folder was
    only partially walked.
    """
    src = tmp_path / "src"
    src.mkdir()
    for i in range(5):
        (src / f"f{i}.txt").write_text("x")
    # cwd's only child `src` is entry 1 (pushed). Popped, its files are
    # entries 2,3,4; the 4th (> cap=3) trips inside src, making it the
    # partially-scanned dir the message must name.
    with pytest.raises(OSError) as exc_info:
        _scan(tmp_path, max_entries=3, overflow="error")
    msg = str(exc_info.value)
    src_path = str(src.resolve(strict=False))
    assert "Unfinished directories" in msg, (
        f"Message must introduce the unfinished-dirs clause. Got: {msg!r}"
    )
    assert f"{src_path} (partially scanned)" in msg, (
        f"Message must name the partially-scanned dir. Got: {msg!r}"
    )
    # The tuning knobs stay in the message so users can find the escape hatches.
    assert "cwd_hidden_scan_max_entries" in msg and "cwd_hidden_scan_overflow" in msg, (
        f"Message must keep naming both tunable spec keys. Got: {msg!r}"
    )


def test_overflow_warn_message_distinguishes_partial_and_bounds_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    The ``warn`` overflow log must (a) distinguish the directory it was
    mid-scan of ("partially scanned") and (b) bound the list with a
    ``(+N more)`` suffix when many directories remain, so a huge tree
    can't produce a multi-KB log line.

    Setup: 15 top-level dirs each holding one file, cap=15. The walker
    pushes all 15 dirs while scanning cwd (entries 1..15), then trips
    on the first grandchild file (entry 16) while inside the
    last-popped dir — making that dir "partially scanned" and leaving
    14 dirs queued. 1 partial + 14 not-scanned = 15 lines; the first
    10 are shown and the remaining 5 collapse to ``(+5 more)``.
    """
    for i in range(15):
        d = tmp_path / f"d{i:02d}"
        d.mkdir()
        (d / "f.txt").write_text("x")
    caplog.set_level("CRITICAL", logger="omnigent.inner._cwd_scan")
    _scan(tmp_path, max_entries=15, overflow="warn")
    records = [r for r in caplog.records if "Mask is incomplete" in r.getMessage()]
    assert len(records) == 1, (
        f"Exactly one overflow CRITICAL expected, got {len(records)}: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    rendered = records[0].getMessage()
    assert "partially scanned" in rendered, (
        f"Message must mark the mid-scan directory as partially scanned. Got: {rendered!r}"
    )
    assert "not scanned" in rendered, (
        f"Message must mark the never-reached directories as not scanned. Got: {rendered!r}"
    )
    # 15 unfinished dirs - 10 shown = 5 collapsed into the suffix.
    assert "(+5 more)" in rendered, (
        f"Unfinished-dir list must be bounded to {10} entries with a (+5 more) "
        f"suffix so the log line stays small. Got: {rendered!r}"
    )


# ---------------------------------------------------------------------------
# Defensive edge cases
# ---------------------------------------------------------------------------


def test_missing_cwd_returns_empty_list(tmp_path: Path) -> None:
    """
    The walker swallows a missing/non-directory cwd and returns an
    empty list. Backends raise the user-facing error at spawn time
    (when bwrap / sandbox-exec try to enter the directory and fail
    loudly with the kernel's own message).
    """
    missing = tmp_path / "does_not_exist"
    entries = scan_cwd_mask_entries(
        missing,
        allow_hidden=[],
        safe_roots=[tmp_path],
        max_entries=_DEFAULT_MAX,
        overflow="error",
    )
    assert entries == []


def test_unreadable_subdirectory_is_skipped_silently(tmp_path: Path) -> None:
    """
    A subdirectory that can't be opened (e.g. permission denied)
    is skipped without raising. The parent stays in the safe set,
    and the inaccessibility itself doesn't leak the content the
    sandbox is trying to hide.
    """
    sub = tmp_path / "locked"
    sub.mkdir()
    (sub / ".env").write_text("masked-if-readable")
    # Drop read+execute permissions so os.scandir raises PermissionError
    # inside the walker; the walker is contractually required to
    # swallow this rather than propagate.
    sub.chmod(0o000)
    try:
        entries = _scan(tmp_path)
    finally:
        sub.chmod(0o700)
    # The locked subdirectory itself is a non-dot dir with a non-
    # escaping target (cwd), so it's not in the result; the .env
    # inside is unreachable and also absent. The contract is
    # "no crash, no leak", which is what we assert.
    for entry in entries:
        assert "locked" not in str(entry.path) or entry.path == sub, (
            f"Unreadable subdir leaked a child mask entry: {entry.path}"
        )
