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
    _COALESCE_DOMINANCE_FRACTION,
    _SUBTREE_COLLAPSE_THRESHOLD,
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
    :returns: List of :class:`MaskedEntry`.
    """
    roots = [cwd.resolve(strict=False), *_SYSTEM_SAFE_ROOTS]
    if safe_roots is not None:
        roots = safe_roots
    return scan_cwd_mask_entries(
        cwd.resolve(strict=False),
        allow_hidden=allow_hidden or [],
        safe_roots=roots,
        max_entries=max_entries,
        overflow=overflow,
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
# Subtree collapse of escaping-symlink farms (target-agnostic, no names)
# ---------------------------------------------------------------------------


def _make_spread_escaping_farm(node_modules: Path, packages: int) -> int:
    """
    Build a cross-store pnpm-shaped ``node_modules``: escaping symlinks
    SPREAD one-per-directory across a ``.pnpm`` store mirror, plus one
    top-level package symlink each.

    Mirrors the real layout measured on a cross-filesystem pnpm store —
    each ``.pnpm/<pkg>@1.0.0/node_modules/<pkg>`` is a single symlink to
    ``/etc/hostname`` (a real file outside the test's ``[cwd, /usr]``
    safe roots, so it counts as escaping), and ``node_modules/<pkg>`` is
    another. No single directory holds enough escaping links to trip a
    per-directory dominance test, so ONLY the subtree aggregate can.

    :param node_modules: The ``node_modules`` dir to build under.
    :param packages: Number of package versions to synthesise.
    :returns: The total number of escaping symlinks created.
    """
    escaping_target = Path("/etc/hostname")
    pnpm = node_modules / ".pnpm"
    total = 0
    for i in range(packages):
        pkg = f"pkg{i:04d}"
        inner = pnpm / f"{pkg}@1.0.0" / "node_modules"
        inner.mkdir(parents=True)
        (inner / pkg).symlink_to(escaping_target)  # store-escaping link
        total += 1
        (node_modules / pkg).symlink_to(escaping_target)  # top-level link
        total += 1
    return total


def test_cross_store_pnpm_farm_collapses_to_one_mask(tmp_path: Path) -> None:
    """
    Contract 1: a cross-store pnpm ``node_modules`` — thousands of
    escaping symlinks SPREAD one-per-dir — collapses to a SINGLE
    ``kind="dir"`` mask via the subtree aggregate, so bwrap's 9000-arg
    ceiling is never approached. No name-based rule is involved; the
    collapse fires purely because escaping masks dominate the subtree.
    """
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    total = _make_spread_escaping_farm(node_modules, packages=120)
    assert total >= _SUBTREE_COLLAPSE_THRESHOLD

    entries = _scan(tmp_path, overflow="unlimited")
    nm_entry = _entry_for(entries, node_modules)
    assert nm_entry is not None and nm_entry.kind == "dir", (
        "The spread escaping farm must collapse to a single node_modules "
        f"dir mask. Got {[(e.path, e.kind) for e in entries]}"
    )
    under = [e for e in entries if str(e.path).startswith(str(node_modules) + os.sep)]
    assert under == [], f"Collapsed farm must emit no per-link masks; got {len(under)} nested."
    assert len(entries) == 1, f"Whole farm should be one mask; got {len(entries)}."


def test_real_npm_node_modules_stays_readable(tmp_path: Path) -> None:
    """
    Contract 2: a real npm/yarn ``node_modules`` (real package dirs, no
    escaping symlinks) is NOT masked and its package content stays
    readable, so project commands (``node app.js`` / local CLIs) work.
    There is no name-based masking: the dir simply has ~zero masks and
    never collapses. (A real ``.bin`` shim dir stays readable too — see
    :func:`test_shim_only_dotdir_stays_readable`; omitted here to keep
    the "readable package content" assertion unambiguous.)
    """
    node_modules = tmp_path / "node_modules"
    for i in range(150):
        pkg = node_modules / f"pkg{i:04d}"
        pkg.mkdir(parents=True)
        (pkg / "index.js").write_text("module.exports = {}\n")
        (pkg / "package.json").write_text("{}\n")
    # Relative (non-escaping) inter-package link, like real npm nesting.
    (node_modules / "pkg0001" / "dep").symlink_to("../pkg0000")

    entries = _scan(tmp_path, overflow="unlimited")
    assert _entry_for(entries, node_modules) is None, "A real npm node_modules must NOT be masked."
    assert _entry_for(entries, node_modules / "pkg0000") is None
    assert _entry_for(entries, node_modules / "pkg0000" / "index.js") is None
    # No mask anywhere under it (no dotfiles, no escaping links).
    under = [e for e in entries if str(e.path).startswith(str(node_modules) + os.sep)]
    assert under == [], f"Real node_modules must stay fully readable; got {under}."


def test_shim_only_dotdir_stays_readable(tmp_path: Path) -> None:
    """
    P2-a: a real ``node_modules/.bin`` — a hidden dir whose entries are
    ALL non-escaping symlinks into sibling packages — is kept READABLE
    (not masked by dotfile policy) so ``npm test`` / ``npx`` / local CLIs
    can resolve their shims. The shims themselves (non-escaping symlinks)
    stay reachable, not masked.
    """
    node_modules = tmp_path / "node_modules"
    pkg = node_modules / "eslint" / "bin"
    pkg.mkdir(parents=True)
    (pkg / "eslint.js").write_text("#!/usr/bin/env node\n")
    bin_dir = node_modules / ".bin"
    bin_dir.mkdir()
    # Relative shim into a sibling package — resolves inside cwd (safe).
    (bin_dir / "eslint").symlink_to("../eslint/bin/eslint.js")
    (bin_dir / "tsc").symlink_to("../eslint/bin/eslint.js")

    entries = _scan(tmp_path)  # allow_hidden=[] → dotfile policy active
    assert _entry_for(entries, bin_dir) is None, (
        "A real .bin shim dir (only non-escaping symlinks) must stay readable so local CLIs run."
    )
    assert _entry_for(entries, bin_dir / "eslint") is None, "Shim must stay reachable."
    assert _entry_for(entries, bin_dir / "tsc") is None, "Shim must stay reachable."
    under = [e for e in entries if str(e.path).startswith(str(bin_dir) + os.sep)]
    assert under == [], f"No shim under a readable .bin may be masked; got {under}."


def test_dotdir_with_regular_file_is_not_shim_only_and_masked(tmp_path: Path) -> None:
    """
    The shim-only carve-out is symlink-gated: a hidden dir holding a
    REGULAR FILE (real content, not a pointer) is masked as before. This
    is the guard that the carve-out can never re-expose ``.aws/credentials``
    or a stray secret file just because the dir is otherwise link-shaped.
    """
    bin_dir = tmp_path / ".bin"
    bin_dir.mkdir()
    (bin_dir / "greet").symlink_to("/usr/bin/env")  # non-escaping symlink
    (bin_dir / "credentials").write_text("[default]\ntoken=hunter2\n")  # real content

    entries = _scan(tmp_path)
    entry = _entry_for(entries, bin_dir)
    assert entry is not None and entry.kind == "dir", (
        "A hidden dir with a regular file is NOT shim-only and must be masked."
    )
    under = [e for e in entries if str(e.path).startswith(str(bin_dir) + os.sep)]
    assert under == [], "Masked dir is pruned; the regular file is hidden with it."


def test_shim_dir_with_escaping_link_is_masked(tmp_path: Path) -> None:
    """
    A hidden dir containing even one ESCAPING symlink is NOT shim-only and
    is masked — the carve-out never un-masks an escaping link (a
    cross-store pnpm ``.bin`` whose shims point into an off-device store).
    """
    bin_dir = tmp_path / ".bin"
    bin_dir.mkdir()
    (bin_dir / "safe").symlink_to("/usr/bin/env")  # non-escaping
    (bin_dir / "escape").symlink_to("/etc/hostname")  # escapes safe roots

    entries = _scan(tmp_path)
    entry = _entry_for(entries, bin_dir)
    assert entry is not None and entry.kind == "dir", (
        "A hidden dir with an escaping link must be masked, not carved out."
    )


def test_collapse_does_not_fire_below_threshold(tmp_path: Path) -> None:
    """
    A handful of escaping symlinks (below the subtree threshold) are
    masked INDIVIDUALLY, not collapsed — the collapse must not fire for
    the ordinary "a few escaping links" case.
    """
    sub = tmp_path / "sub"
    sub.mkdir()
    for i in range(3):
        (sub / f"link_{i:05d}").symlink_to("/etc/hostname")

    entries = _scan(tmp_path, overflow="unlimited")
    assert _entry_for(entries, sub) is None, "Few escaping links must not collapse the dir."
    for i in range(3):
        entry = _entry_for(entries, sub / f"link_{i:05d}")
        assert entry is not None and entry.kind == "file", (
            "Each escaping link must be masked individually below the threshold."
        )


def test_collapse_respects_dominance_and_keeps_source_readable(tmp_path: Path) -> None:
    """
    A directory with many escaping symlinks but ALSO abundant readable
    source (so masks are below the dominance fraction) is NOT collapsed:
    readable files stay visible and each link is masked individually.
    Guards against hiding browsable project content wholesale.
    """
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    n = _SUBTREE_COLLAPSE_THRESHOLD + 10
    for i in range(n):
        (mixed / f"link_{i:05d}").symlink_to("/etc/hostname")
    # Nine readable files per link → masks are ~10% of content, well under
    # the 0.9 dominance fraction.
    for i in range(n * 9):
        (mixed / f"src_{i:05d}.txt").write_text("readable")

    entries = _scan(tmp_path, overflow="unlimited")
    assert _entry_for(entries, mixed) is None, (
        "A dir where readable source dominates must not be collapsed."
    )
    assert _entry_for(entries, mixed / "src_00000.txt") is None
    link_entry = _entry_for(entries, mixed / "link_00000")
    assert link_entry is not None and link_entry.kind == "file", (
        "Below dominance, escaping links are masked individually."
    )


def test_collapse_fires_at_shallowest_dir_keeping_sibling_source(tmp_path: Path) -> None:
    """
    Shallowest-collapse: a real source tree that merely CONTAINS a nested
    farm keeps its own files readable — only the nested farm directory
    collapses, not the ancestor source dir.

    ``src`` holds readable source AND ``src/vendor/node_modules`` (a farm).
    The farm is dominated only within ``vendor``/``node_modules``, not
    within ``src`` (whose readable files dilute it), so collapse fires at
    the farm, and ``src``'s own source stays visible.
    """
    src = tmp_path / "src"
    src.mkdir()
    for i in range(200):
        (src / f"module_{i:04d}.js").write_text("export default 1\n")
    node_modules = src / "vendor" / "node_modules"
    node_modules.mkdir(parents=True)
    _make_spread_escaping_farm(node_modules, packages=120)

    entries = _scan(tmp_path, overflow="unlimited")
    # src's own readable source is untouched.
    assert _entry_for(entries, src / "module_0000.js") is None
    assert _entry_for(entries, src) is None
    # The nested farm collapses (at node_modules or its vendor parent).
    collapse_dirs = [e for e in entries if e.kind == "dir"]
    assert any(
        str(node_modules) == str(e.path) or str(node_modules).startswith(str(e.path) + os.sep)
        for e in collapse_dirs
    ), f"Nested farm must collapse to a dir mask; got {[(e.path, e.kind) for e in entries]}"
    # And nothing above the collapse point (no src collapse) hides source.
    assert not any(str(e.path) == str(src) for e in entries)


# ---------------------------------------------------------------------------
# allow_hidden directories: readable at top, but walked for nested masks
# ---------------------------------------------------------------------------


def test_allow_hidden_dir_readable_but_nested_secrets_still_masked(tmp_path: Path) -> None:
    """
    Contract 3: an ``allow_hidden`` directory (e.g. ``.git``) is readable
    at the top level (the dir itself is NOT masked), but is still WALKED
    so non-allowed nested secrets are masked — no blanket-expose of
    nested dotfiles.
    """
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")  # readable git config
    # Nested secrets the operator did NOT allow — must be masked.
    (git_dir / ".env").write_text("SECRET=1")
    aws = git_dir / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\n")
    (git_dir / "leak").symlink_to("/etc/hostname")  # nested escaping link

    entries = _scan(tmp_path, allow_hidden=[".git"])
    assert _entry_for(entries, git_dir) is None, ".git (allowed) must be readable at top."
    assert _entry_for(entries, git_dir / "config") is None, "git config stays readable."
    env_entry = _entry_for(entries, git_dir / ".env")
    assert env_entry is not None and env_entry.kind == "file", "Nested .env must be masked."
    aws_entry = _entry_for(entries, aws)
    assert aws_entry is not None and aws_entry.kind == "dir", "Nested .aws must be masked."
    leak_entry = _entry_for(entries, git_dir / "leak")
    assert leak_entry is not None and leak_entry.kind == "file", (
        "Nested escaping symlink must be masked."
    )


def test_allow_hidden_dir_with_nested_farm_collapses_inside(tmp_path: Path) -> None:
    """
    Contract 4: coalescing applies WITHIN an allow_hidden dir. A dense
    interior farm (mimicking a large ``.git/worktrees`` escaping-symlink
    admin tree) collapses to a few masks instead of thousands, while the
    allowed dir's own readable content keeps it from collapsing wholesale.
    """
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    # Readable git content so .git itself is not dominated.
    for i in range(50):
        (git_dir / f"obj_{i:04d}").write_text("blob\n")
    worktrees = git_dir / "worktrees"
    worktrees.mkdir()
    n = _SUBTREE_COLLAPSE_THRESHOLD + 20
    for i in range(n):
        (worktrees / f"wt_{i:05d}").symlink_to("/etc/hostname")

    entries = _scan(tmp_path, allow_hidden=[".git"], overflow="unlimited")
    assert _entry_for(entries, git_dir) is None, ".git must stay readable at top."
    # The interior farm collapsed rather than emitting one mask per link.
    dir_masks_under_git = [
        e for e in entries if e.kind == "dir" and str(e.path).startswith(str(git_dir) + os.sep)
    ]
    assert any(str(e.path) == str(worktrees) for e in dir_masks_under_git), (
        f"The .git/worktrees farm must collapse to one dir mask; "
        f"got {[(e.path, e.kind) for e in entries]}"
    )
    per_link = [e for e in entries if str(e.path).startswith(str(worktrees) + os.sep)]
    assert per_link == [], "Collapsed worktrees farm must not emit per-link masks."


def test_allow_hidden_sparse_dir_never_collapses_but_interior_folds(tmp_path: Path) -> None:
    """
    P2-b: an ``allow_hidden`` directory itself must NEVER be the collapse
    fold point, even when a dense interior farm DOMINATES its subtree.

    Here ``.git`` has only a few readable files (a real bare-ish repo
    invariant, not the 50-file cushion the older test manufactured) and a
    dense ``.git/worktrees`` farm of >=100 escaping links. The farm makes
    ``.git``'s subtree mask-dominated (>0.9), so the dominance guard ALONE
    would let ``.git`` collapse — the allowlist exclusion is what keeps it
    readable. The ordinary-named interior ``worktrees`` still folds.
    """
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    # Only a few readable files — sparse, so masks dominate .git's subtree.
    (git_dir / "config").write_text("[core]\n")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "description").write_text("repo\n")
    worktrees = git_dir / "worktrees"
    worktrees.mkdir()
    n = _SUBTREE_COLLAPSE_THRESHOLD + 20
    for i in range(n):
        (worktrees / f"wt_{i:05d}").symlink_to("/etc/hostname")

    # Sanity: .git's subtree really is mask-dominated (guard would not
    # have saved it) — so the assertion below tests the exclusion, not
    # the dominance fraction.
    assert n >= _COALESCE_DOMINANCE_FRACTION * (n + 3)

    entries = _scan(tmp_path, allow_hidden=[".git"], overflow="unlimited")
    assert _entry_for(entries, git_dir) is None, (
        ".git (allow_hidden) must stay readable at the top even when a "
        "dense interior farm dominates its subtree."
    )
    assert _entry_for(entries, git_dir / "config") is None, "git config stays readable."
    # The interior farm still folds to a single dir mask.
    wt_entry = _entry_for(entries, worktrees)
    assert wt_entry is not None and wt_entry.kind == "dir", (
        f".git/worktrees farm must still collapse; got {[(e.path, e.kind) for e in entries]}"
    )
    per_link = [e for e in entries if str(e.path).startswith(str(worktrees) + os.sep)]
    assert per_link == [], "Collapsed worktrees farm must not emit per-link masks."


def test_unallowed_dotdir_is_masked_and_pruned(tmp_path: Path) -> None:
    """
    A non-allowed dot-directory is masked as a directory and pruned (its
    contents never walked) — unchanged by the redesign. Guards that the
    dotfile decision still runs before recursion.
    """
    for dotdir in (".venv", ".aws", ".ssh"):
        cache = tmp_path / dotdir
        cache.mkdir()
        (cache / ".inner_secret").write_text("SECRET=1")
    entries = _scan(tmp_path)  # allow_hidden=[] → mask every dotfile
    for dotdir in (".venv", ".aws", ".ssh"):
        cache = tmp_path / dotdir
        cache_entry = _entry_for(entries, cache)
        assert cache_entry is not None and cache_entry.kind == "dir", (
            f"Un-allowed {dotdir} must be masked as a directory, not walked."
        )
        nested = [e for e in entries if str(e.path).startswith(str(cache) + os.sep)]
        assert nested == [], f"Masked {dotdir} must be pruned; got {[e.path for e in nested]}"


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
