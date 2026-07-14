"""
Tests for the Bubblewrap sandbox backend.

Layers tested:

- **Resolver**: :meth:`BwrapSandboxBackend.resolve` produces the
  right :class:`SandboxPolicy` shape (RO-by-default cwd, scratch
  tmpdir handling deferred to the spawn site, allow-hidden
  defaulting).
- **Argv shape**: :meth:`BwrapSandboxBackend.wrap_launcher_argv`
  emits a ``bwrap`` invocation with the expected mount and
  namespace flags, and dotfile / symlink masks for cwd entries.
- **Linux + bwrap availability gates**: explicit ``OSError`` when
  the host isn't Linux or ``bwrap`` is missing.
- **Hardened seccomp profile**: a real ``bwrap`` spawn applies the
  profile inside the helper and blocks the documented dangerous
  syscalls / socket families while leaving ``AF_INET``,
  ``AF_INET6``, and plain ``socket(AF_UNIX, ...)`` working.

The seccomp probe tests run an actual ``bwrap`` subprocess
(:func:`subprocess.run`) and skip cleanly when ``bwrap`` is not
installed, so the suite stays green on hosts without bubblewrap.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.inner.bwrap_sandbox import (
    _ALLOWED_SOCKET_FAMILIES,
    _BWRAP_MAX_ARGS,
    _CLONE_NEW_FLAG_BITS,
    _DEFAULT_CWD_ALLOW_HIDDEN,
    BwrapSandboxBackend,
    _bwrap_extra_seccomp_rules,
    _check_bwrap_arg_ceiling,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import SandboxPolicy, with_denied_unix_sockets

BWRAP_AVAILABLE = shutil.which("bwrap") is not None


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """
    Result of running a Python probe script inside a bwrap helper.

    :param stdout: Captured stdout (already stripped of trailing
        newline).
    :param stderr: Captured stderr (last 400 chars at most).
    :param exit_code: Process exit code; ``0`` for normal termination.
    """

    stdout: str
    stderr: str
    exit_code: int


def _make_backend() -> BwrapSandboxBackend:
    """
    Construct a fresh backend instance for tests that need a bare
    backend object (without going through the registry singleton).

    :returns: A new :class:`BwrapSandboxBackend` instance.
    """
    return BwrapSandboxBackend()


def _make_policy(
    cwd: Path,
    *,
    allow_hidden: list[str] | None = None,
    write_roots: list[Path] | None = None,
    allow_network: bool = True,
    read_roots: list[Path] | None = None,
    cwd_hidden_scan_max_entries: int | None = None,
    cwd_hidden_scan_overflow: str | None = None,
) -> SandboxPolicy:
    """
    Build a :class:`SandboxPolicy` directly without going through the
    resolver.

    Used in tests that want full control over policy fields without
    spec parsing or platform gates.

    :param cwd: Effective working directory for the helper.
    :param allow_hidden: Override for ``cwd_allow_hidden``; ``None``
        keeps the field as ``None`` (the bwrap argv builder treats
        it as an empty allowlist when not supplied — the resolver is
        what fills in the documented default).
    :param write_roots: Explicit write roots; defaults to ``[]``
        (cwd RO).
    :param allow_network: Whether to share host network.
    :param read_roots: Explicit read roots; defaults to ``None``
        (only the bwrap default mounts are visible).
    :param cwd_hidden_scan_max_entries: Override for the recursive
        cwd scan cap; ``None`` keeps the dataclass default (50000).
    :param cwd_hidden_scan_overflow: Override for the overflow mode;
        ``None`` keeps the dataclass default (``"error"``).
    :returns: A populated :class:`SandboxPolicy`.
    """
    kwargs: dict[str, object] = {
        "backend_type": "linux_bwrap",
        "active": True,
        "read_roots": read_roots,
        "write_roots": write_roots if write_roots is not None else [],
        "write_files": [],
        "allow_network": allow_network,
        "cwd_allow_hidden": allow_hidden,
    }
    if cwd_hidden_scan_max_entries is not None:
        kwargs["cwd_hidden_scan_max_entries"] = cwd_hidden_scan_max_entries
    if cwd_hidden_scan_overflow is not None:
        kwargs["cwd_hidden_scan_overflow"] = cwd_hidden_scan_overflow
    return SandboxPolicy(**kwargs)  # type: ignore[arg-type]


def _run_helper_probe(
    cwd: Path,
    probe_script: str,
    *,
    policy: SandboxPolicy | None = None,
    extra_env: dict[str, str] | None = None,
) -> ProbeResult:
    """
    Spawn a Python probe through ``bwrap`` plus
    :meth:`BwrapSandboxBackend.activate` and return what it printed.

    The probe runs as: ``[python, "-c", probe_script]`` wrapped by
    :meth:`wrap_launcher_argv`. Inside the script, the test must
    call :func:`omnigent.inner.sandbox.activate_sandbox` with the
    deserialised policy itself if it wants the seccomp profile to
    engage — :meth:`activate` is what installs the seccomp BPF.

    The repository root is added to the policy's ``read_roots`` so
    the probe can ``import omnigent.*`` inside the sandbox even
    when ``cwd`` is a throwaway tempdir.

    :param cwd: Effective working directory; bind-mounted into the
        sandbox per the bwrap defaults.
    :param probe_script: Python source to run as the probe.
        Receives the base64-encoded policy as ``sys.argv[1]``.
    :param policy: Sandbox policy to wrap and activate. Defaults to
        a fresh one resolved for ``cwd`` if omitted.
    :param extra_env: Extra environment variables to merge into the
        helper's environment (e.g. ``PYTHONPATH``).
    :returns: A :class:`ProbeResult`.
    """
    backend = _make_backend()
    if policy is None:
        spec = OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(
                type="linux_bwrap",
                # Make the repo root visible so the probe can import
                # the omnigent package during tests run from a
                # tempdir cwd.
                read_paths=[str(_repo_root())],
            ),
        )
        policy = backend.resolve(spec, cwd)
    encoded = _encode_policy(policy)
    argv = backend.wrap_launcher_argv(
        [sys.executable, "-c", probe_script, encoded],
        policy,
        cwd,
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_repo_root())
    if extra_env is not None:
        env.update(extra_env)
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=30,
        check=False,
    )
    return ProbeResult(
        stdout=completed.stdout.strip(),
        stderr=completed.stderr[-400:],
        exit_code=completed.returncode,
    )


def _encode_policy(policy: SandboxPolicy) -> str:
    """
    Base64-url-encode a policy for transport into a probe script.

    :param policy: Policy to serialise.
    :returns: A URL-safe base64 string the probe can decode with
        :func:`base64.urlsafe_b64decode` plus :func:`json.loads`
        plus :meth:`SandboxPolicy.from_jsonable`.
    """
    import base64

    raw = json.dumps(policy.to_jsonable()).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _repo_root() -> Path:
    """
    Return the repository root so probes can import
    :mod:`omnigent` from a clean ``$PYTHONPATH``.

    :returns: The directory containing the ``omnigent`` package.
    """
    # tests/inner/test_bwrap_sandbox.py → tests/inner/ → tests/ → repo root
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_resolve_default_keeps_cwd_read_only() -> None:
    """
    ``write_paths`` omitted (the common case) leaves ``write_roots``
    empty so the bwrap argv builder bind-mounts cwd RO. This is the
    bwrap-specific "no surprise writes" default documented at
    :meth:`BwrapSandboxBackend.resolve`.

    Failure here means a future edit silently flipped the cwd to
    writable, which would surprise users who explicitly chose the
    bwrap backend for tighter isolation.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.backend_type == "linux_bwrap"
    assert policy.active is True
    assert policy.write_roots == [], (
        "bwrap resolve() must default write_roots to [] (cwd RO). "
        "If non-empty here, the resolver is silently elevating cwd "
        "to writable — opposite of the documented default."
    )
    assert policy.read_roots is None  # No spec-supplied read_paths.


def test_resolve_write_paths_dot_makes_cwd_writable() -> None:
    """
    Setting ``write_paths: ["."]`` flips cwd to writable. This is the
    documented opt-in: an opt-in spec produces a write_root that
    matches cwd, which the argv builder turns into ``--bind``
    (read-write) instead of ``--ro-bind``.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap", write_paths=["."]),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.write_roots == [Path.cwd().resolve(strict=False)]


def test_resolve_default_cwd_allow_hidden_is_dot_venv() -> None:
    """
    ``cwd_allow_hidden=None`` in the spec resolves to the documented
    default :data:`_DEFAULT_CWD_ALLOW_HIDDEN` (``[".venv"]``) on the
    policy. The argv builder consumes ``policy.cwd_allow_hidden``
    rather than reaching back into the spec, so this default has
    to land on the policy at resolve time.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.cwd_allow_hidden == list(_DEFAULT_CWD_ALLOW_HIDDEN), (
        "Default allowlist drift — _DEFAULT_CWD_ALLOW_HIDDEN is "
        "the documented baseline; if this fails, either the constant "
        "moved or the resolver stopped substituting the default."
    )


def test_resolve_explicit_cwd_allow_hidden_overrides_default() -> None:
    """
    An explicit ``cwd_allow_hidden`` in the spec replaces the default
    entirely (no merge). This matches the Fail-Loud contract — the
    spec-self-containment rule says the spec is the source of truth,
    not a delta against an invisible default.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            cwd_allow_hidden=[".cache", ".npmrc"],
        ),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.cwd_allow_hidden == [".cache", ".npmrc"]


def test_resolve_raises_on_non_linux() -> None:
    """
    The resolver hard-errors on non-Linux hosts. The bwrap backend
    requires Linux user namespaces; there is no fallback path.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
    )
    with patch("omnigent.inner.bwrap_sandbox.sys.platform", "darwin"):
        with pytest.raises(OSError, match="only available on Linux"):
            backend.resolve(spec, Path.cwd())


def test_resolve_raises_when_bwrap_missing() -> None:
    """
    If ``bwrap`` is not on PATH, the resolver fails loud with an
    actionable message. The user explicitly chose ``linux_bwrap``;
    silent fallback to a different backend would be a Fail-Loud
    violation.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
    )
    with patch("omnigent.inner.bwrap_sandbox.shutil.which", return_value=None):
        with pytest.raises(OSError, match="bwrap"):
            backend.resolve(spec, Path.cwd())


# ---------------------------------------------------------------------------
# wrap_launcher_argv shape
# ---------------------------------------------------------------------------


def test_wrap_launcher_argv_starts_with_bwrap_and_ends_with_command(
    tmp_path: Path,
) -> None:
    """
    The wrapped argv must begin with ``bwrap`` (so
    :func:`subprocess.Popen` exec's the launcher) and end with the
    original command after ``--`` so bwrap forwards the rest as the
    inner argv.

    Failure here means the wrap is structurally broken — Popen
    would either run the wrong binary or pass bwrap flags to the
    helper.
    """
    backend = _make_backend()
    policy = _make_policy(tmp_path)
    argv = backend.wrap_launcher_argv(
        [sys.executable, "-m", "omnigent.inner.os_env", "helper", "X"],
        policy,
        tmp_path,
    )
    assert argv[0] == "bwrap"
    assert "--" in argv
    dash_idx = argv.index("--")
    # Everything after `--` is the inner command unchanged.
    assert argv[dash_idx + 1 :] == [
        sys.executable,
        "-m",
        "omnigent.inner.os_env",
        "helper",
        "X",
    ]


def test_wrap_launcher_argv_includes_required_mounts_and_chdir(
    tmp_path: Path,
) -> None:
    """
    The wrapped argv emits the default RO mounts (``/usr``,
    ``/lib*``, ``/bin``, ``/sbin``), the cwd bind, ``--proc /proc``,
    ``--dev /dev``, ``--tmpfs /tmp``, and the ``--chdir`` target.

    A regression here means the hermetic root degraded — typical
    cause is dropping a default mount during a refactor and not
    noticing because the helper still launches (it just can't find
    libc, or sees a different /proc, etc.).
    """
    backend = _make_backend()
    policy = _make_policy(tmp_path)
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)
    # The plan defines these as the minimum hermetic root.
    assert "--ro-bind-try" in argv
    assert "/usr" in argv
    assert "--proc" in argv
    assert "/proc" in argv
    assert "--dev" in argv
    assert "--tmpfs" in argv
    assert "--chdir" in argv
    # The chdir target is the resolved cwd.
    chdir_idx = argv.index("--chdir")
    assert argv[chdir_idx + 1] == str(tmp_path.resolve(strict=False))


@pytest.mark.parametrize(
    "allow_network,should_unshare",
    [(True, False), (False, True)],
    ids=["network_shared", "network_isolated"],
)
def test_wrap_launcher_argv_unshare_net_follows_allow_network(
    tmp_path: Path, allow_network: bool, should_unshare: bool
) -> None:
    """
    ``--unshare-net`` is emitted iff ``policy.allow_network`` is
    False. With ``allow_network=True`` the helper sees the host
    network namespace; with ``False`` bwrap creates a fresh empty
    namespace (no host interfaces).

    Direction of the flag matters — flipping it would silently
    invert network policy for every spec.
    """
    backend = _make_backend()
    policy = _make_policy(tmp_path, allow_network=allow_network)
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)
    assert ("--unshare-net" in argv) is should_unshare


def test_wrap_launcher_argv_cwd_writable_when_write_root_matches(
    tmp_path: Path,
) -> None:
    """
    When a ``write_root`` resolves to cwd, the cwd bind-mount must
    use ``--bind`` (read-write) instead of ``--ro-bind``.

    Direct shape check on the argv slot following the cwd path —
    the ordering is fixed so the index of the cwd token tells us
    which bind variant landed.
    """
    backend = _make_backend()
    cwd_resolved = tmp_path.resolve(strict=False)
    policy = _make_policy(tmp_path, write_roots=[cwd_resolved])
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)
    # Find the cwd bind by locating the path in the argv and checking
    # the bind verb that precedes it.
    cwd_indices = [i for i, token in enumerate(argv) if token == str(cwd_resolved)]
    # The cwd appears multiple times (--bind <cwd> <cwd> + --chdir <cwd>),
    # so look for the bind variant that precedes the *first* mount slot.
    bind_verbs = [argv[i - 1] for i in cwd_indices if argv[i - 1] in {"--bind", "--ro-bind"}]
    assert "--bind" in bind_verbs, (
        f"Expected --bind for writable cwd; found {bind_verbs} "
        f"(--ro-bind would silently make cwd read-only)."
    )
    assert "--ro-bind" not in bind_verbs, (
        "Both --bind and --ro-bind landed on the cwd — duplicate "
        "mounts would shadow each other in bwrap."
    )


def test_wrap_launcher_argv_cwd_read_only_by_default(tmp_path: Path) -> None:
    """
    With an empty ``write_roots`` (the default), cwd is bound
    ``--ro-bind`` — the bwrap-specific default the user
    explicitly asked for.
    """
    backend = _make_backend()
    cwd_resolved = tmp_path.resolve(strict=False)
    policy = _make_policy(tmp_path, write_roots=[])
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)
    cwd_indices = [i for i, token in enumerate(argv) if token == str(cwd_resolved)]
    bind_verbs = [argv[i - 1] for i in cwd_indices if argv[i - 1] in {"--bind", "--ro-bind"}]
    assert "--ro-bind" in bind_verbs
    assert "--bind" not in bind_verbs


def test_wrap_launcher_argv_masks_denied_unix_socket_after_write_root(
    tmp_path: Path,
) -> None:
    """
    A denied AF_UNIX socket inside a writable root is masked with a
    ``--bind-try /dev/null <socket>`` overlay emitted AFTER that
    root's bind.

    The tmux control socket lives in the instance ``private_dir``,
    which is a write root so the forked workspace stays writable.
    bwrap mounts are last-wins, so the /dev/null mask must come after
    the ``--bind`` of the enclosing root or the writable bind would
    shadow it and the pane could ``connect(2)`` to the unsandboxed
    tmux server. We assert both that the exact mask triple is present
    and that it follows the root's bind index.
    """
    backend = _make_backend()
    private_dir = (tmp_path / "instance").resolve(strict=False)
    private_dir.mkdir()
    socket_path = private_dir / "tmux.sock"
    policy = _make_policy(tmp_path, write_roots=[private_dir])
    policy = with_denied_unix_sockets(policy, [socket_path])

    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)

    mask_idx = _index_of_triple(argv, "--bind-try", "/dev/null", str(socket_path))
    assert mask_idx is not None, (
        f"Expected a `--bind-try /dev/null {socket_path}` mask for the "
        f"denied socket; got argv tail {argv[-20:]}"
    )
    # The write-root bind of private_dir must precede the mask, else
    # the writable bind would layer on top and re-expose the socket.
    root_bind_indices = [
        i
        for i in range(len(argv) - 2)
        if argv[i] in {"--bind", "--bind-try"} and argv[i + 1] == str(private_dir)
    ]
    assert root_bind_indices, "private_dir was never bound as a write root"
    assert min(root_bind_indices) < mask_idx, (
        "The /dev/null socket mask was emitted BEFORE the private_dir "
        "write-root bind; bwrap last-wins would let the writable bind "
        "re-expose the tmux socket to the pane."
    )


def test_wrap_launcher_argv_no_socket_mask_when_deny_list_empty(
    tmp_path: Path,
) -> None:
    """
    With no ``deny_unix_socket_paths`` the builder emits no
    ``--bind-try /dev/null`` socket mask — the feature is opt-in and
    must not perturb the argv for terminals that don't manage a tmux
    control socket.
    """
    backend = _make_backend()
    policy = _make_policy(tmp_path, write_roots=[tmp_path.resolve(strict=False)])
    assert policy.deny_unix_socket_paths is None

    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)

    # The only /dev/null binds present (if any) come from the dotfile
    # mask; none should target a path we didn't ask to deny. Since this
    # tmp_path has no dotfiles, there should be no /dev/null bind at all.
    devnull_targets = [
        argv[i + 2]
        for i in range(len(argv) - 2)
        if argv[i] == "--bind-try" and argv[i + 1] == "/dev/null"
    ]
    assert devnull_targets == [], (
        f"Unexpected /dev/null masks with an empty deny list: {devnull_targets}"
    )


def _index_of_triple(argv: list[str], a: str, b: str, c: str) -> int | None:
    """
    Return the index of the first ``[a, b, c]`` contiguous triple in
    ``argv``, or ``None`` if absent.

    :param argv: The argument vector to scan.
    :param a: First token of the triple.
    :param b: Second token.
    :param c: Third token.
    :returns: Index of ``a`` in the matching triple, or ``None``.
    """
    for i in range(len(argv) - 2):
        if argv[i] == a and argv[i + 1] == b and argv[i + 2] == c:
            return i
    return None


def test_wrap_launcher_argv_chdir_overrides_only_entry_directory(
    tmp_path: Path,
) -> None:
    """
    Passing an explicit ``chdir`` separates the workspace mount
    target from the launcher's entry directory.

    The workspace (``cwd``) still gets bound at its real absolute
    path so the agent can reach project files via absolute paths,
    but ``--chdir`` points at the alternate directory — the
    expected behaviour for ``OSEnvSpec.start_in_scratch``, where
    the helper boots inside the scratch tmpdir while reads of the
    project tree keep working.

    Regression here would either drop the workspace mount (breaking
    reads) or chdir into the wrong directory (silently relocating
    every relative-path tool call).
    """
    backend = _make_backend()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_resolved = workspace.resolve(strict=False)
    scratch_resolved = scratch.resolve(strict=False)

    policy = _make_policy(workspace, write_roots=[scratch_resolved])
    argv = backend.wrap_launcher_argv(
        [sys.executable, "-c", "pass"],
        policy,
        workspace,
        chdir=scratch,
    )

    assert "--chdir" in argv
    chdir_idx = argv.index("--chdir")
    assert argv[chdir_idx + 1] == str(scratch_resolved), (
        "chdir target should be the override path, not the workspace"
    )

    # Workspace must still be bind-mounted at its real absolute path.
    workspace_indices = [i for i, token in enumerate(argv) if token == str(workspace_resolved)]
    bind_verbs = [argv[i - 1] for i in workspace_indices if argv[i - 1] in {"--bind", "--ro-bind"}]
    assert "--ro-bind" in bind_verbs, (
        "Workspace bind missing — start_in_scratch must keep the project reachable for reads."
    )

    # Scratch is added to write_roots so it appears as --bind-try later
    # in the argv (the resolver-augmented helper-spawn path is what
    # adds the per-helper scratch tmpdir to write_roots; we mimic
    # that here).
    assert "--bind-try" in argv
    bind_try_idx = argv.index("--bind-try")
    # The first --bind-try following the cwd bind references the
    # scratch path we plumbed in via write_roots.
    assert str(scratch_resolved) in argv[bind_try_idx : bind_try_idx + 3]


def test_wrap_launcher_argv_chdir_none_falls_back_to_cwd(
    tmp_path: Path,
) -> None:
    """
    Omitting ``chdir`` (or passing ``None``) preserves the long-
    standing behaviour of using ``cwd`` as the ``--chdir`` target,
    so existing callers don't need to thread the new parameter
    through to keep their semantics.
    """
    backend = _make_backend()
    cwd_resolved = tmp_path.resolve(strict=False)
    policy = _make_policy(tmp_path)
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path, chdir=None)
    chdir_idx = argv.index("--chdir")
    # chdir=None must resolve to cwd; if it resolved to something else,
    # existing specs that don't pass chdir would silently relocate.
    assert argv[chdir_idx + 1] == str(cwd_resolved)


def test_wrap_launcher_argv_binds_intermediate_symlink_at_literal_path(
    tmp_path: Path,
) -> None:
    """
    When ``argv[0]`` resolves through an intermediate directory-symlink
    (the uv-managed-Python layout: ``cpython-3.12 -> cpython-3.12.13``),
    the argv builder must emit a bind that mounts the real directory at
    the *literal* symlink path. Otherwise bwrap's ``execvp`` will fail
    with ENOENT because the kernel traverses the literal name and the
    intermediate symlink doesn't exist inside the sandbox.

    Regression for the CI failure: switching to
    ``python-preference = "only-managed"`` made uv create a venv whose
    ``.venv/bin/python`` resolves through ``cpython-3.12 -> cpython-3.12.13``,
    and the previous ``_ensure_executable_visible`` implementation only
    bound the fully-resolved ``cpython-3.12.13`` path. The kernel had no
    way to reach it through the literal ``cpython-3.12`` name.
    """
    # Build the uv-style layout:
    #   <tmp>/installs/cpython-3.12.13/bin/python   (real file)
    #   <tmp>/installs/cpython-3.12 -> cpython-3.12.13   (directory symlink)
    #   <tmp>/venv/bin/python -> <tmp>/installs/cpython-3.12/bin/python
    installs = tmp_path / "installs"
    real_install = installs / "cpython-3.12.13"
    (real_install / "bin").mkdir(parents=True)
    real_python = real_install / "bin" / "python"
    real_python.write_text("#!/bin/sh\necho real-python\n")
    real_python.chmod(0o755)
    floating = installs / "cpython-3.12"
    floating.symlink_to(real_install)
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(floating / "bin" / "python")

    backend = _make_backend()
    # cwd is a separate scratch dir, so the venv-and-installs tree is
    # outside cwd and outside the default mounts — _ensure_executable_visible
    # is the only path that can expose them.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    policy = _make_policy(scratch)
    argv = backend.wrap_launcher_argv([str(venv_python), "-c", "pass"], policy, scratch)

    # Pair up the ``--ro-bind-try src dst`` triples for inspection.
    def _bind_pairs() -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for i, token in enumerate(argv):
            if token == "--ro-bind-try" and i + 2 < len(argv):
                pairs.append((argv[i + 1], argv[i + 2]))
        return pairs

    pairs = _bind_pairs()
    # The fix: a bind whose DESTINATION is the literal version-floating
    # path (``cpython-3.12``) and whose SOURCE is the resolved real
    # directory (``cpython-3.12.13``). Without this, the kernel can't
    # traverse the floating name inside the sandbox.
    floating_bin_dst = str(floating / "bin")
    real_bin_src = str(real_install / "bin")
    assert (real_bin_src, floating_bin_dst) in pairs, (
        f"Expected a bind mapping the real Python install bin "
        f"({real_bin_src!r}) at the floating symlink's literal bin path "
        f"({floating_bin_dst!r}). Without this, bwrap's execvp on the "
        f"literal venv path fails with ENOENT because the floating "
        f"symlink dir is missing inside the sandbox. Pairs: {pairs}"
    )


def test_wrap_launcher_argv_target_binds_non_default_path(
    tmp_path: Path,
) -> None:
    """
    When ``target`` names a binary outside the default mounts
    (e.g. an npm-managed CLI under ``node_modules/.bin/``),
    ``wrap_launcher_argv`` must emit ``--ro-bind-try`` args that
    bind the target's directory chain into the sandbox.

    This is the bwrap-PATH bug that caused the 5 claude-sdk sandbox
    e2e failures: the launcher re-execs into the bwrap namespace and
    then runs ``subprocess.run([target_path, ...])``.  Without binding
    the target's directory, that exec fails with FileNotFoundError
    because the directory (e.g. ``node_modules/.bin/``) is never
    visible inside the namespace.

    **What breaks if wrong:** every harness-CLI sandbox test that
    installs its CLI outside ``/usr``, ``/bin``, or ``/sbin`` would
    fail with ``FileNotFoundError`` when the launcher re-execs under
    bwrap.
    """
    # Simulate node_modules/.bin/claude outside the default mounts.
    cli_install = tmp_path / "node_modules" / ".bin"
    cli_install.mkdir(parents=True)
    fake_claude = cli_install / "claude"
    fake_claude.write_text("#!/bin/sh\necho ok\n")
    fake_claude.chmod(0o755)
    cli_path = str(fake_claude)

    # Use a separate cwd so the target is not covered by the cwd bind.
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    backend = _make_backend()
    policy = _make_policy(workspace)
    argv = backend.wrap_launcher_argv(
        [sys.executable, "-c", "pass"],
        policy,
        workspace,
        target=cli_path,
    )

    # The target's parent directory must be bind-mounted (as --ro-bind-try
    # <real-src> <literal-dst>).  The src and dst are the same for a real
    # (non-symlink) file.
    parent_dir = str(cli_install.resolve(strict=False))
    bind_pairs = [
        (argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2) if argv[i] == "--ro-bind-try"
    ]
    assert any(dst == parent_dir for _, dst in bind_pairs), (
        f"Expected a --ro-bind-try entry with destination {parent_dir!r} "
        f"so the target binary at {cli_path!r} is reachable inside the "
        f"bwrap namespace.  Got bind pairs: {bind_pairs}"
    )


def test_wrap_launcher_argv_target_none_no_extra_binds(
    tmp_path: Path,
) -> None:
    """
    When ``target=None`` (the default), the argv must be identical to
    the ``target``-free call — no spurious extra ``--ro-bind-try``
    args are emitted.

    Regression guard: the target-visibility feature must be strictly
    opt-in so callers that never pass a target (e.g. the
    parent-side ``_HelperProcessClient`` spawn path) keep their argv
    unchanged.
    """
    backend = _make_backend()
    policy = _make_policy(tmp_path)
    without_target = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)
    with_none = backend.wrap_launcher_argv(
        [sys.executable, "-c", "pass"], policy, tmp_path, target=None
    )
    assert without_target == with_none, (
        "Passing target=None must produce identical argv to omitting the parameter."
    )


def test_wrap_launcher_argv_target_already_in_default_mounts(
    tmp_path: Path,
) -> None:
    """
    When the target binary lives under a default RO mount (``/usr``,
    ``/bin``, ``/sbin``, etc.) no extra bind args are emitted for it
    — ``_ensure_executable_visible`` already skips paths covered by
    :data:`_DEFAULT_RO_DIRS`.

    Ensures the target-visibility feature doesn't add redundant mounts
    for system binaries that are already visible in the sandbox.
    """
    backend = _make_backend()
    policy = _make_policy(tmp_path)
    # /usr/bin/env is a regular binary directly under /usr, which is in
    # _DEFAULT_RO_DIRS — no extra binds should land for it.
    env_path = "/usr/bin/env"
    argv_no_target = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)
    argv_with_target = backend.wrap_launcher_argv(
        [sys.executable, "-c", "pass"], policy, tmp_path, target=env_path
    )
    assert argv_no_target == argv_with_target, (
        "Passing a target already covered by the default mounts must not "
        "add any extra --ro-bind-try entries to the argv."
    )


# ---------------------------------------------------------------------------
# Dotfile masking + symlink defense
# ---------------------------------------------------------------------------


def test_dotfile_masking_hides_disallowed_dotfiles(tmp_path: Path) -> None:
    """
    Top-level dotfiles in cwd that aren't on ``cwd_allow_hidden``
    appear as ``--bind-try /dev/null <path>`` (files / symlinks) or
    ``--tmpfs <path>`` (directories) in the argv. Allowed
    dotfiles produce no mask.

    This is the user-facing security goal: no host secret in the
    project tree leaks into the agent's view.
    """
    (tmp_path / ".env").write_text("SECRET=42")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("[default]\naws_access_key_id=x")
    (tmp_path / ".venv").mkdir()
    (tmp_path / "regular.txt").write_text("not secret")

    backend = _make_backend()
    policy = _make_policy(tmp_path, allow_hidden=[".venv"])
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)

    cwd = tmp_path.resolve(strict=False)
    env_path = str(cwd / ".env")
    aws_path = str(cwd / ".aws")
    venv_path = str(cwd / ".venv")
    regular_path = str(cwd / "regular.txt")

    # .env is a file → masked with --bind-try /dev/null
    assert _has_pair(argv, "--bind-try", "/dev/null", env_path), (
        f".env not masked with --bind-try /dev/null. argv slice: "
        f"{[t for t in argv if '.env' in t or 'null' in t]}"
    )
    # .aws is a dir → masked with --tmpfs
    assert _has_pair_single_dest(argv, "--tmpfs", aws_path), (
        f".aws not masked with --tmpfs. argv slice: {[t for t in argv if '.aws' in t]}"
    )
    # .venv is on the allowlist → no mask of any kind
    assert not _argv_mentions(argv, venv_path, after_token="--tmpfs"), (
        ".venv is on the allowlist but a --tmpfs mask landed for it."
    )
    assert not _argv_mentions(argv, venv_path, after_token="--bind-try"), (
        ".venv is on the allowlist but a --bind-try mask landed for it."
    )
    # Non-dotfile is untouched.
    assert not _argv_mentions(argv, regular_path, after_token="--tmpfs")
    assert not _argv_mentions(argv, regular_path, after_token="--bind-try")


def test_dotfile_masking_skips_target_that_vanished_after_scan(
    tmp_path: Path,
) -> None:
    """
    A dotfile the scan saw but that vanished before the argv is built
    produces NO mask triple — otherwise bwrap would try to create the
    mountpoint inside the ro-bound cwd and abort (the flaky CI failure,
    where coverage.py's transient ``.coverage.*`` files raced the scan).
    A persistent dotfile alongside it is still masked.
    """
    from omnigent.inner import bwrap_sandbox
    from omnigent.inner._cwd_scan import MaskedEntry

    cwd = tmp_path.resolve(strict=False)
    (tmp_path / ".env").write_text("SECRET=42")
    present_path = cwd / ".env"
    # Never created on disk: simulates a file renamed away after the scan.
    vanished_path = cwd / ".coverage.inner-rest.host.pid2942.XbRGYxCx"

    real_scan = bwrap_sandbox.scan_cwd_mask_entries

    def _scan_with_phantom(*args: object, **kwargs: object) -> list[MaskedEntry]:
        entries = list(real_scan(*args, **kwargs))  # type: ignore[arg-type]
        entries.append(MaskedEntry(path=vanished_path, kind="file"))
        return entries

    backend = _make_backend()
    policy = _make_policy(tmp_path, allow_hidden=[".venv"])
    with patch.object(bwrap_sandbox, "scan_cwd_mask_entries", _scan_with_phantom):
        argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, tmp_path)

    assert _has_pair(argv, "--bind-try", "/dev/null", str(present_path)), (
        ".env (present) should still be masked with --bind-try /dev/null."
    )
    assert _index_of_triple(argv, "--bind-try", "/dev/null", str(vanished_path)) is None, (
        "A vanished dotfile must NOT be masked — bwrap would have to "
        "create the mountpoint inside the read-only cwd bind and abort. "
        f"argv slice: {[t for t in argv if 'coverage' in t]}"
    )


# NOTE: walker-decision tests (escape symlink defense, recursion,
# allow_hidden basename matching, the three overflow modes) moved to
# ``tests/inner/test_cwd_scan.py``. That suite asserts the
# :class:`MaskedEntry` tuples directly so the same logic is
# verified once for both `linux_bwrap` and `darwin_seatbelt`. The
# top-level dotfile / dotdir test above stays here to assert the
# bwrap-specific emit translation
# (``"file"`` → ``--bind-try /dev/null``, ``"dir"`` → ``--tmpfs``).


def test_s5_read_paths_dotfile_masking_blocks_dot_aws_under_home_grant(
    tmp_path: Path,
) -> None:
    """
    S5: a ``read_paths`` grant that covers a directory carrying
    dotfile-shaped secrets (``~/`` style) must NOT silently expose
    them. The bwrap argv must contain a ``--tmpfs <root>/.aws`` (and
    similar) mask for every dotfile/dotdir under the granted root,
    just like cwd has had.

    Pre-fix behaviour: the dotfile masker was cwd-only, so granting
    ``~/`` exposed ``~/.aws/credentials`` to the helper despite
    nobody asking for it. Post-fix: the masker walks every
    ``read_paths`` root with the same rules.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".aws").mkdir()
    (fake_home / ".aws" / "credentials").write_text("[default]\nkey=secret")
    (fake_home / ".ssh").mkdir()
    (fake_home / ".ssh" / "id_ed25519").write_text("-----BEGIN")
    (fake_home / ".env").write_text("SECRET=1")
    (fake_home / "code").mkdir()  # non-dotfile, must NOT be masked
    cwd = tmp_path / "work"
    cwd.mkdir()

    backend = _make_backend()
    policy = _make_policy(
        cwd,
        read_roots=[fake_home.resolve(strict=False)],
        allow_hidden=[".venv"],
    )
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, cwd)

    aws_path = str(fake_home.resolve(strict=False) / ".aws")
    ssh_path = str(fake_home.resolve(strict=False) / ".ssh")
    env_path = str(fake_home.resolve(strict=False) / ".env")
    code_path = str(fake_home.resolve(strict=False) / "code")

    assert _has_pair_single_dest(argv, "--tmpfs", aws_path), (
        ".aws under a read_paths grant was not masked. The dotfile "
        "masker must walk read_paths roots in addition to cwd, "
        "otherwise a broad ``read_paths: ['~/']`` silently exposes "
        "~/.aws/credentials."
    )
    assert _has_pair_single_dest(argv, "--tmpfs", ssh_path), (
        ".ssh under a read_paths grant was not masked."
    )
    assert _has_pair(argv, "--bind-try", "/dev/null", env_path), (
        ".env (regular file) under a read_paths grant must be masked with --bind-try /dev/null."
    )
    assert not _argv_mentions(argv, code_path, after_token="--tmpfs"), (
        "Non-dotfile entries under a read_paths root must NOT be "
        "masked — the whole point of granting the root is to "
        "expose those files."
    )


def test_s5_read_paths_dotfile_masking_honors_cwd_allow_hidden(
    tmp_path: Path,
) -> None:
    """
    S5: ``cwd_allow_hidden`` is the explicit opt-in for granting a
    dotfile-shaped path through, and it MUST apply to read_paths
    roots too. Otherwise the only way to legitimately grant
    ``~/.aws`` would be to disable the walker entirely, which would
    reintroduce the bigger hole.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".aws").mkdir()
    (fake_home / ".aws" / "credentials").write_text("[default]")
    (fake_home / ".ssh").mkdir()  # NOT in the allowlist
    cwd = tmp_path / "work"
    cwd.mkdir()

    backend = _make_backend()
    policy = _make_policy(
        cwd,
        read_roots=[fake_home.resolve(strict=False)],
        allow_hidden=[".aws"],
    )
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, cwd)

    aws_path = str(fake_home.resolve(strict=False) / ".aws")
    ssh_path = str(fake_home.resolve(strict=False) / ".ssh")
    assert not _argv_mentions(argv, aws_path, after_token="--tmpfs"), (
        ".aws is in cwd_allow_hidden but a --tmpfs mask still "
        "landed for it under the read_paths root — the allowlist "
        "filter is not being applied to read_paths."
    )
    assert _has_pair_single_dest(argv, "--tmpfs", ssh_path), (
        ".ssh is NOT in the allowlist; it must still be masked under read_paths roots."
    )


def test_s5_read_paths_dedup_skips_paths_under_cwd(tmp_path: Path) -> None:
    """
    A ``read_paths`` entry at-or-under ``cwd`` is fully covered by
    the cwd dotfile scan; the read_paths walker must skip it to
    avoid emitting the same mount triple twice.
    """
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / ".env").write_text("SECRET=1")
    sub = cwd / "sub"
    sub.mkdir()

    backend = _make_backend()
    policy = _make_policy(
        cwd,
        read_roots=[
            cwd.resolve(strict=False),
            sub.resolve(strict=False),
        ],
        allow_hidden=[".venv"],
    )
    argv = backend.wrap_launcher_argv([sys.executable, "-c", "pass"], policy, cwd)

    env_path = str(cwd.resolve(strict=False) / ".env")
    # Count occurrences of the .env path as a destination for
    # --bind-try /dev/null. Should appear exactly once.
    occurrences = sum(
        1
        for i in range(len(argv) - 2)
        if argv[i] == "--bind-try" and argv[i + 1] == "/dev/null" and argv[i + 2] == env_path
    )
    assert occurrences == 1, (
        f".env masked {occurrences}x — the read_paths walker should "
        f"skip roots at-or-under cwd to avoid double-emission. "
        f"argv slice: {[t for t in argv if '.env' in t or t == '--bind-try' or t == '/dev/null']}"
    )


# ---------------------------------------------------------------------------
# Seccomp profile (real bwrap subprocess required)
# ---------------------------------------------------------------------------


pytestmark_bwrap = pytest.mark.skipif(
    not BWRAP_AVAILABLE, reason="bwrap not installed on this host"
)


@pytestmark_bwrap
def test_seccomp_blocks_dangerous_socket_families_inside_helper(tmp_path: Path) -> None:
    """
    The full hardened seccomp profile installed by
    :meth:`BwrapSandboxBackend.activate` blocks the documented
    high-risk socket families while leaving ``AF_UNIX`` /
    ``AF_INET`` / ``AF_INET6`` open.

    Spawns a real bwrap helper that activates the sandbox itself
    and reports per-family results — the only honest way to
    verify a kernel BPF filter actually engaged.
    """
    probe = """
import base64, json, socket, sys
from omnigent.inner.sandbox import SandboxPolicy, activate_sandbox

policy = SandboxPolicy.from_jsonable(
    json.loads(base64.urlsafe_b64decode(sys.argv[1]).decode("utf-8"))
)
activate_sandbox(policy)
results = {}
for name, fam in [
    ("AF_UNIX", socket.AF_UNIX),
    ("AF_INET", socket.AF_INET),
    ("AF_INET6", socket.AF_INET6),
    ("AF_NETLINK", socket.AF_NETLINK),
    ("AF_PACKET", 17),
    ("AF_VSOCK", 40),
    ("AF_KEY", 15),
    ("AF_ALG", 38),
]:
    try:
        s = socket.socket(fam, socket.SOCK_DGRAM, 0)
        s.close()
        results[name] = "opened"
    except PermissionError:
        results[name] = "EPERM"
    except OSError as e:
        results[name] = f"err:{e.errno}"
print(json.dumps(results))
"""
    result = _run_helper_probe(tmp_path, probe)
    assert result.exit_code == 0, f"Probe failed (rc={result.exit_code}). stderr={result.stderr!r}"
    parsed = json.loads(result.stdout)
    # Allowed families remain functional under the seccomp filter.
    assert parsed["AF_UNIX"] == "opened"
    assert parsed["AF_INET"] == "opened"
    assert parsed["AF_INET6"] == "opened"
    # Non-allowlisted families return EPERM (blanket deny for socket).
    for blocked in ("AF_NETLINK", "AF_PACKET", "AF_VSOCK", "AF_KEY", "AF_ALG"):
        assert parsed[blocked] == "EPERM", (
            f"{blocked} should return EPERM under the bwrap seccomp "
            f"profile; got {parsed[blocked]!r}. The seccomp filter "
            "either failed to engage or the socket allowlist is wrong."
        )


@pytestmark_bwrap
def test_seccomp_blocks_unshare_and_setns_inside_helper(tmp_path: Path) -> None:
    """
    ``unshare(CLONE_NEWUSER)`` and ``setns()`` return ``EPERM``
    inside the helper. These are the canonical user-namespace
    privesc primitives — blocking them is the central security
    invariant the bwrap backend exists to enforce.
    """
    probe = """
import base64, ctypes, errno, json, sys
from omnigent.inner.sandbox import SandboxPolicy, activate_sandbox
policy = SandboxPolicy.from_jsonable(
    json.loads(base64.urlsafe_b64decode(sys.argv[1]).decode("utf-8"))
)
activate_sandbox(policy)
libc = ctypes.CDLL(None, use_errno=True)
results = {}
# unshare(CLONE_NEWUSER == 0x10000000)
rc = libc.unshare(0x10000000)
results["unshare"] = {"rc": rc, "errno": ctypes.get_errno()}
# setns on /proc/self/ns/user
fd = -1
try:
    fd = ctypes.c_int(libc.open(b"/proc/self/ns/user", 0))
    if fd.value >= 0:
        rc = libc.setns(fd.value, 0)
        results["setns"] = {"rc": rc, "errno": ctypes.get_errno()}
    else:
        results["setns"] = {"rc": -1, "errno": ctypes.get_errno(),
                             "note": "open failed first"}
finally:
    if fd != -1 and getattr(fd, "value", -1) >= 0:
        libc.close(fd.value)
print(json.dumps(results))
"""
    result = _run_helper_probe(tmp_path, probe)
    assert result.exit_code == 0, f"Probe failed (rc={result.exit_code}). stderr={result.stderr!r}"
    parsed = json.loads(result.stdout)
    assert parsed["unshare"]["rc"] == -1
    assert parsed["unshare"]["errno"] == errno.EPERM, (
        f"unshare(CLONE_NEWUSER) returned errno={parsed['unshare']['errno']}"
        f" instead of EPERM ({errno.EPERM}); seccomp profile didn't"
        " block the syscall."
    )
    assert parsed["setns"]["rc"] == -1
    assert parsed["setns"]["errno"] == errno.EPERM


@pytestmark_bwrap
def test_seccomp_blocks_clone_with_namespace_flags_inside_helper(
    tmp_path: Path,
) -> None:
    """
    A raw ``clone(CLONE_NEWNET | SIGCHLD)`` syscall returns ``EPERM``;
    plain ``clone(SIGCHLD)`` (i.e. the equivalent of ``fork()``) is
    not affected.

    The MASKED_EQ filter in the seccomp helper is the only thing
    standing between an agent and the user-namespace privesc class
    of CVEs; this is the directly-targeted regression test.
    """
    probe = """
import base64, ctypes, errno, json, signal, sys
from omnigent.inner.sandbox import SandboxPolicy, activate_sandbox
policy = SandboxPolicy.from_jsonable(
    json.loads(base64.urlsafe_b64decode(sys.argv[1]).decode("utf-8"))
)
activate_sandbox(policy)
libc = ctypes.CDLL(None, use_errno=True)
NR_CLONE = 56  # x86_64
CLONE_NEWNET = 0x40000000
results = {}
# Block test: clone with CLONE_NEWNET set must return EPERM.
rc = libc.syscall(NR_CLONE, ctypes.c_ulong(CLONE_NEWNET | signal.SIGCHLD),
                  0, 0, 0, 0)
results["clone_newnet"] = {"rc": rc, "errno": ctypes.get_errno()}
print(json.dumps(results))
"""
    result = _run_helper_probe(tmp_path, probe)
    assert result.exit_code == 0, f"Probe failed (rc={result.exit_code}). stderr={result.stderr!r}"
    parsed = json.loads(result.stdout)
    assert parsed["clone_newnet"]["rc"] == -1
    assert parsed["clone_newnet"]["errno"] == errno.EPERM, (
        f"clone(CLONE_NEWNET) returned errno={parsed['clone_newnet']['errno']}"
        f" instead of EPERM; the MASKED_EQ rule for CLONE_NEWNET in"
        " _bwrap_extra_seccomp_rules() is missing or incorrect."
    )


# ---------------------------------------------------------------------------
# Rule-list invariants (no subprocess needed)
# ---------------------------------------------------------------------------


def test_seccomp_extra_rules_include_one_clone_rule_per_namespace_bit() -> None:
    """
    :func:`_bwrap_extra_seccomp_rules` emits exactly one ``clone`` rule
    per :data:`_CLONE_NEW_FLAG_BITS` entry. Each rule is a
    ``MASKED_EQ`` filter on arg 0 with ``datum_a == datum_b == bit``.

    Failure here means the per-bit fan-out logic regressed — easy to
    miss because bwrap would still launch and the shared baseline
    syscalls (mount, ptrace, etc.) would still be blocked, just not
    the namespace bits.
    """
    rules = _bwrap_extra_seccomp_rules()
    clone_bits_blocked: set[int] = set()
    for rule in rules:
        if rule.syscall != "clone" or not rule.arg_filters:
            continue
        for filt in rule.arg_filters:
            if filt.arg == 0 and filt.datum_a == filt.datum_b:
                clone_bits_blocked.add(filt.datum_a)
    assert clone_bits_blocked == set(_CLONE_NEW_FLAG_BITS), (
        f"clone rules cover {sorted(clone_bits_blocked)} but the "
        f"expected set is {sorted(_CLONE_NEW_FLAG_BITS)}. Missing or "
        "extra bits would silently widen / narrow the namespace "
        "block."
    )


def test_seccomp_extra_rules_block_clone3_outright() -> None:
    """
    :func:`_bwrap_extra_seccomp_rules` includes a ``clone3`` rule
    with no arg filters (the seccomp filter can't dereference
    ``struct clone_args`` in user memory, so the only safe call is
    to deny the entire syscall) and the action returns ``ENOSYS``
    rather than ``EPERM``. ``ENOSYS`` is the contract glibc's
    ``clone_internal`` checks before falling back to legacy
    ``clone`` — returning ``EPERM`` here breaks ``pthread_create``
    on glibc 2.34+ (Ubuntu 22.04 ships 2.35) because glibc treats
    ``EPERM`` as a hard failure and propagates it instead of
    retrying. The in-helper egress relay thread depends on
    ``pthread_create`` succeeding, so a regression here would
    surface as ``RuntimeError: can't start new thread`` inside the
    sandbox.
    """
    import errno

    from omnigent.inner._seccomp import scmp_act_errno

    rules = _bwrap_extra_seccomp_rules()
    clone3 = [r for r in rules if r.syscall == "clone3"]
    assert len(clone3) == 1, (
        f"Exactly one clone3 rule expected (unconditional deny); got {len(clone3)}."
    )
    assert clone3[0].arg_filters == ()
    assert clone3[0].action == scmp_act_errno(errno.ENOSYS), (
        "clone3 deny must return ENOSYS, not EPERM, so glibc's "
        "clone_internal falls back to the legacy clone syscall. "
        "Returning EPERM breaks pthread_create on glibc 2.34+."
    )


def test_seccomp_extra_rules_socket_allowlist() -> None:
    """
    Socket rules deny everything outside :data:`_ALLOWED_SOCKET_FAMILIES`
    using range-based deny rules: individual denies for the gaps plus
    a ``SCMP_CMP_GE`` rule that catches all families >= 11 (future-proof).
    """
    from omnigent.inner._seccomp import SCMP_CMP_EQ, SCMP_CMP_GE

    rules = _bwrap_extra_seccomp_rules()
    socket_rules = [r for r in rules if r.syscall == "socket"]

    # All socket rules should be deny rules.
    deny_action = socket_rules[0].action
    assert all(r.action == deny_action for r in socket_rules)

    # Extract denied families: EQ rules give exact families, GE rule
    # gives the lower bound of the "deny everything above" range.
    eq_families = set()
    ge_bound = None
    for r in socket_rules:
        assert r.arg_filters, "All socket rules should have arg filters"
        filt = r.arg_filters[0]
        if filt.op == SCMP_CMP_EQ:
            eq_families.add(filt.datum_a)
        elif filt.op == SCMP_CMP_GE:
            ge_bound = filt.datum_a

    # The allowed families (1, 2, 10) must NOT appear in the deny set.
    assert not eq_families.intersection(_ALLOWED_SOCKET_FAMILIES)
    # The GE rule must start above AF_INET6 (10).
    assert ge_bound == 11


# ---------------------------------------------------------------------------
# Helpers internal to this test module
# ---------------------------------------------------------------------------


def _has_pair(argv: list[str], verb: str, src: str, dest: str) -> bool:
    """
    Return whether the ``argv`` contains the triple ``[verb, src,
    dest]`` adjacent to each other.

    Used to assert on bwrap mount triples (``--bind-try /dev/null
    /path``) without depending on absolute argv positions.

    :param argv: The bwrap argv to scan.
    :param verb: The mount verb, e.g. ``"--bind"`` or ``"--ro-bind"``.
    :param src: Mount source path, e.g. ``"/dev/null"``.
    :param dest: Mount destination path, e.g. ``"/cwd/.env"``.
    :returns: ``True`` when the triple appears anywhere in argv.
    """
    for i in range(len(argv) - 2):
        if argv[i] == verb and argv[i + 1] == src and argv[i + 2] == dest:
            return True
    return False


def _has_pair_single_dest(argv: list[str], verb: str, dest: str) -> bool:
    """
    Return whether ``argv`` contains the pair ``[verb, dest]``
    adjacent to each other.

    Used for single-arg mount commands (``--tmpfs <dest>``,
    ``--proc <dest>``).

    :param argv: The bwrap argv to scan.
    :param verb: The mount verb, e.g. ``"--tmpfs"``.
    :param dest: Mount destination path.
    :returns: ``True`` when the pair appears anywhere in argv.
    """
    for i in range(len(argv) - 1):  # noqa: SIM110
        if argv[i] == verb and argv[i + 1] == dest:
            return True
    return False


def _argv_mentions(argv: list[str], path: str, *, after_token: str) -> bool:
    """
    Return whether ``path`` appears in ``argv`` immediately after a
    token equal to ``after_token`` (or two slots after, for triple
    mount verbs like ``--bind src dest``).

    Used for negative-existence checks ("the .venv path should not
    appear after any --bind / --tmpfs verb").

    :param argv: The bwrap argv to scan.
    :param path: The path to look for.
    :param after_token: The verb token that must precede ``path``.
    :returns: ``True`` when the path follows the verb.
    """
    for i in range(len(argv) - 1):
        if argv[i] == after_token and argv[i + 1] == path:
            return True
        # Triple form: --bind <src> <dest> — path can be the dest.
        if i + 2 < len(argv) and argv[i] == after_token and argv[i + 2] == path:
            return True
    return False


# ---------------------------------------------------------------------------
# bwrap arg-ceiling backstop (Part 2)
# ---------------------------------------------------------------------------


def test_arg_ceiling_boundary_is_exactly_the_limit() -> None:
    """
    Pin the raise boundary exactly at :data:`_BWRAP_MAX_ARGS`: an argv of
    ``_BWRAP_MAX_ARGS - 1`` tokens passes, and an argv of exactly
    ``_BWRAP_MAX_ARGS`` tokens raises. Prod uses ``>=`` (bwrap aborts
    AT its limit, not one past it), so the just-under case must be
    accepted and the at-limit case rejected.
    """
    cwd = Path("/work")
    just_under = ["x"] * (_BWRAP_MAX_ARGS - 1)
    assert len(just_under) == _BWRAP_MAX_ARGS - 1
    # Must not raise at one below the limit.
    _check_bwrap_arg_ceiling(just_under, cwd)

    at_limit = ["x"] * _BWRAP_MAX_ARGS
    assert len(at_limit) == _BWRAP_MAX_ARGS
    with pytest.raises(OSError):
        _check_bwrap_arg_ceiling(at_limit, cwd)


def test_arg_ceiling_error_is_actionable_and_safe() -> None:
    """
    The over-ceiling :class:`OSError` message (a) states the arg count
    and the 9000-arg bwrap limit, (b) names the likely culprit and the
    cwd to inspect, (c) offers SAFE remediations (narrow read_paths /
    restructure / drop an allowlist entry so it coalesces), and (d) when
    it mentions the cap / overflow knobs, labels them explicitly as
    exposing directory contents rather than presenting them as cures.

    The safety wording is the P2 fix: the message must NOT tell an
    operator to ADD the culprit to cwd_allow_hidden (which would make the
    whole dir readable) or imply raising the cap fixes an argv overflow.
    """
    cwd = Path("/work/grounded-repo")
    over = ["x"] * (_BWRAP_MAX_ARGS + 1)

    with pytest.raises(OSError) as exc_info:
        _check_bwrap_arg_ceiling(over, cwd)
    msg = str(exc_info.value)

    assert str(len(over)) in msg, f"Message must state the arg count. Got: {msg!r}"
    assert str(_BWRAP_MAX_ARGS) in msg, (
        f"Message must state bwrap's {_BWRAP_MAX_ARGS}-arg limit. Got: {msg!r}"
    )
    assert str(cwd) in msg, f"Message must name the cwd to inspect. Got: {msg!r}"
    assert "symlink" in msg, f"Message must name the likely culprit. Got: {msg!r}"
    # Safe remediations offered.
    assert "read_paths" in msg, f"Message must offer narrowing read_paths. Got: {msg!r}"
    # The cap/overflow knobs, if mentioned, are labelled as an exposure —
    # never presented as a cure for an argv overflow.
    assert "cwd_hidden_scan_max_entries" in msg and "cannot cure" in msg, (
        f"Message must state raising the cap cannot cure an argv overflow. Got: {msg!r}"
    )
    assert "cwd_hidden_scan_overflow" in msg and "EXPOSE" in msg, (
        f"Message must label overflow=warn/unlimited as exposing contents. Got: {msg!r}"
    )
    # Must NOT advise adding the culprit to the allowlist (security downgrade).
    assert "Add it to os_env.sandbox.cwd_allow_hidden" not in msg, (
        f"Message must not recommend adding the culprit to cwd_allow_hidden. Got: {msg!r}"
    )


def test_arg_ceiling_allows_normal_arg_counts() -> None:
    """
    A realistic bwrap argv (well under the ceiling) passes the backstop
    without raising, so the check never trips on ordinary spawns.
    """
    normal = ["bwrap", "--ro-bind-try", "/usr", "/usr", "--", "/bin/true"]
    assert len(normal) < _BWRAP_MAX_ARGS
    # Must not raise.
    _check_bwrap_arg_ceiling(normal, Path("/work"))


@dataclass
class _ReexposeCase:
    """
    A single target-re-expose scenario for the parametrized regression.

    :param cwd: Working directory passed to :meth:`wrap_launcher_argv`.
    :param target: Absolute exec-target path (the binary the launcher
        will exec).
    :param policy: Sandbox policy (controls read_roots / allow_hidden).
    :param cover_dst: The resolved directory the walker coalesces to a
        single ``--tmpfs`` that would shadow the target, or ``None`` when
        no mask covers the target (the allow_hidden case).
    :param realpath_differs: ``True`` when the target's lexical parent
        differs from its realpath parent, so the re-overlay destination
        must be the realpath-based path (not the lexical one).
    """

    cwd: Path
    target: str
    policy: SandboxPolicy
    cover_dst: str | None
    realpath_differs: bool


def _make_escaping_symlink_farm(directory: Path, count: int) -> None:
    """
    Fill *directory* with *count* symlinks that escape every safe root.

    Each points at ``/etc/hostname`` — a real file outside the bwrap
    safe-root set (``/usr``, ``/tmp``, cwd, ...) — so the walker
    classifies the directory as an escaping-symlink farm and coalesces
    it, exactly like a pnpm ``.pnpm`` store.

    :param directory: Directory to populate (created if missing).
    :param count: Number of escaping symlinks to create.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (directory / f"link_{i:05d}").symlink_to("/etc/hostname")


def _build_reexpose_case(shape: str, tmp_path: Path) -> _ReexposeCase:
    """
    Build the filesystem + policy for one target-re-expose *shape*.

    Covers the edge matrix the implementation must handle: a regular
    target under a named-coalesced ``node_modules``; a symlink target; a
    nested coalesced dir; a generic symlink-farm-coalesced dir (not
    named ``node_modules``); a target whose lexical parent differs from
    its realpath parent; a target reached via an external ``read_paths``
    mask; and a target under an ``allow_hidden`` dir (no mask emitted).

    :param shape: One of the parametrized shape keys.
    :param tmp_path: Pytest tempdir the tree is built under.
    :returns: The assembled :class:`_ReexposeCase`.
    """
    cwd = tmp_path / "work"
    cwd.mkdir()

    def _reg_target(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    if shape == "regular_named":
        target = _reg_target(cwd / "node_modules" / ".bin" / "claude")
        return _ReexposeCase(
            cwd,
            str(target),
            _make_policy(cwd),
            str(Path(os.path.realpath(str(cwd / "node_modules")))),
            False,
        )

    if shape == "symlink":
        # Target is itself an escaping symlink into an external real file.
        (cwd / "node_modules" / ".bin").mkdir(parents=True)
        link = cwd / "node_modules" / ".bin" / "claude"
        link.symlink_to("/etc/hostname")
        return _ReexposeCase(
            cwd,
            str(link),
            _make_policy(cwd),
            str(Path(os.path.realpath(str(cwd / "node_modules")))),
            False,
        )

    if shape == "nested":
        target = _reg_target(cwd / "a" / "b" / "node_modules" / ".bin" / "tool")
        return _ReexposeCase(
            cwd,
            str(target),
            _make_policy(cwd),
            str(Path(os.path.realpath(str(cwd / "a" / "b" / "node_modules")))),
            False,
        )

    if shape == "generic_farm":
        farm = cwd / "store"
        _make_escaping_symlink_farm(farm, 150)
        target = _reg_target(farm / "tool")
        return _ReexposeCase(
            cwd,
            str(target),
            _make_policy(cwd),
            str(Path(os.path.realpath(str(farm)))),
            False,
        )

    if shape == "realpath_parent":
        # ``current`` is a symlink to the real release dir, so the
        # target's lexical parent (.../current/...) differs from its
        # realpath parent (.../v1.2/...). The walker descends the real
        # path and coalesces .../v1.2/node_modules.
        real_release = cwd / "proj" / "v1.2"
        _reg_target(real_release / "node_modules" / ".bin" / "tool")
        (cwd / "proj" / "current").symlink_to(real_release)
        lexical_target = cwd / "proj" / "current" / "node_modules" / ".bin" / "tool"
        return _ReexposeCase(
            cwd,
            str(lexical_target),
            _make_policy(cwd),
            str(Path(os.path.realpath(str(real_release / "node_modules")))),
            True,
        )

    if shape == "read_paths":
        # Target lives under an external read_paths root, not under cwd.
        ext = tmp_path / "ext"
        target = _reg_target(ext / "node_modules" / ".bin" / "tool")
        return _ReexposeCase(
            cwd,
            str(target),
            _make_policy(cwd, read_roots=[ext]),
            str(Path(os.path.realpath(str(ext / "node_modules")))),
            False,
        )

    if shape == "allow_hidden":
        # node_modules on allow_hidden → readable, NOT masked. The
        # re-overlay is still emitted (harmless) and nothing is shadowed.
        target = _reg_target(cwd / "node_modules" / ".bin" / "tool")
        return _ReexposeCase(
            cwd,
            str(target),
            _make_policy(cwd, allow_hidden=["node_modules"]),
            None,
            False,
        )

    raise AssertionError(f"unknown shape {shape!r}")


@pytest.mark.parametrize(
    "shape",
    [
        "regular_named",
        "symlink",
        "nested",
        "generic_farm",
        "realpath_parent",
        "read_paths",
        "allow_hidden",
    ],
)
def test_target_reexpose_survives_mask_across_shapes(shape: str, tmp_path: Path) -> None:
    """
    P1 regression matrix: an exec ``target`` living under a coalesced /
    masked subtree must stay reachable in the assembled argv, and the
    re-overlay must be an EXACT-FILE read-only bind — never re-exposing
    siblings or lifting the surrounding tmpfs.

    For each shape assert (a) ordering: the target re-overlay index is
    AFTER the covering ``--tmpfs`` (later bwrap mount wins), and
    (b) exact-file exposure: the overlay is a single ``--ro-bind-try``
    of the resolved regular file at the launcher-reached (realpath) path,
    and it is the ONLY mount under the coalesced dir after the mask.
    """
    case = _build_reexpose_case(shape, tmp_path)
    backend = _make_backend()
    argv = backend.wrap_launcher_argv(
        [sys.executable, "-c", "pass"],
        case.policy,
        case.cwd,
        target=case.target,
    )

    # The launcher-reached path: literal basename under the parent's
    # realpath — exactly what _reexpose_target_after_mask emits.
    expected_dst = os.path.join(
        os.path.realpath(os.path.dirname(case.target)),
        os.path.basename(case.target),
    )

    overlays = [m for m in _mount_destinations(argv) if m[3] == expected_dst]
    assert len(overlays) == 1, (
        f"[{shape}] target must be re-exposed by exactly one mount at "
        f"{expected_dst}; got {overlays}. argv tail: {argv[-30:]}"
    )
    o_idx, o_verb, o_src, _ = overlays[0]
    assert o_verb == "--ro-bind-try", (
        f"[{shape}] re-overlay must be a read-only file bind, got {o_verb!r}."
    )
    assert o_src is not None and os.path.isfile(o_src) and not os.path.islink(o_src), (
        f"[{shape}] re-overlay source must be the resolved regular file, got {o_src!r}."
    )

    if case.realpath_differs:
        lexical = os.path.abspath(case.target)
        assert expected_dst != lexical, "[realpath_parent] test setup should differ"
        # The overlay dest must be the realpath path, not the lexical one.
        assert _last_index(argv, lexical, after_token="--ro-bind-try") is None, (
            f"[{shape}] re-overlay must target the realpath path {expected_dst}, "
            f"not the lexical path {lexical}."
        )

    if case.cover_dst is not None:
        cover_idx = _last_index(argv, case.cover_dst, after_token="--tmpfs")
        assert cover_idx is not None, (
            f"[{shape}] covering dir {case.cover_dst} must coalesce to a "
            f"--tmpfs mask. argv tail: {argv[-30:]}"
        )
        assert o_idx > cover_idx, (
            f"[{shape}] target re-overlay ({o_idx}) must come AFTER the "
            f"covering tmpfs ({cover_idx}) so the later mount wins."
        )
        under = _dsts_under(argv, case.cover_dst, after_idx=cover_idx)
        assert [(verb, dst) for _, verb, _, dst in under] == [("--ro-bind-try", expected_dst)], (
            f"[{shape}] only the target file may be re-exposed under the "
            f"coalesced dir after the mask; got {under}."
        )
    else:
        # allow_hidden: dir stays readable, so no tmpfs shadows it and the
        # re-overlay (redundant but harmless) is the only mount under it.
        nm_dir = os.path.dirname(os.path.dirname(expected_dst))  # .../node_modules
        assert _last_index(argv, nm_dir, after_token="--tmpfs") is None, (
            f"[{shape}] an allow_hidden dir must NOT be tmpfs-masked."
        )
        under = _dsts_under(argv, nm_dir, after_idx=-1)
        assert [(verb, dst) for _, verb, _, dst in under] == [("--ro-bind-try", expected_dst)], (
            f"[{shape}] only the target file may be bound under the allow_hidden dir; got {under}."
        )


def _mount_destinations(argv: list[str]) -> list[tuple[int, str, str | None, str]]:
    """
    Parse *argv* into ``(index, verb, src, dst)`` mount tuples.

    Steps over each mount option's operands so a following value (e.g. a
    ``--setenv`` payload) can't be misread as a mount, and stops at the
    ``--`` that terminates bwrap options. ``src`` is ``None`` for
    destination-only verbs (``--tmpfs`` / ``--proc`` / ``--dev``).

    :param argv: The assembled bwrap argv.
    :returns: One tuple per mount, in argv order.
    """
    bind_verbs = {
        "--bind",
        "--bind-try",
        "--ro-bind",
        "--ro-bind-try",
        "--dev-bind",
        "--dev-bind-try",
    }
    dst_only_verbs = {"--tmpfs", "--proc", "--dev", "--mqueue", "--dir"}
    out: list[tuple[int, str, str | None, str]] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break
        if tok in bind_verbs and i + 2 < len(argv):
            out.append((i, tok, argv[i + 1], argv[i + 2]))
            i += 3
            continue
        if tok in dst_only_verbs and i + 1 < len(argv):
            out.append((i, tok, None, argv[i + 1]))
            i += 2
            continue
        i += 1
    return out


def _dsts_under(
    argv: list[str], prefix: str, *, after_idx: int
) -> list[tuple[int, str, str | None, str]]:
    """
    Return mounts whose destination equals or lives under *prefix* and
    whose argv index is strictly greater than *after_idx*.

    Used to assert that after a coalescing ``--tmpfs``, the ONLY mount
    re-exposing anything under the coalesced directory is the single
    target-file overlay (no siblings, no re-expansion of the subtree).

    :param argv: The assembled bwrap argv.
    :param prefix: The coalesced directory path.
    :param after_idx: Only mounts after this index are considered.
    :returns: Matching ``(index, verb, src, dst)`` tuples.
    """
    res: list[tuple[int, str, str | None, str]] = []
    for idx, verb, src, dst in _mount_destinations(argv):
        if idx <= after_idx:
            continue
        if dst == prefix or dst.startswith(prefix + os.sep):
            res.append((idx, verb, src, dst))
    return res


def _last_index(argv: list[str], path: str, *, after_token: str) -> int | None:
    """
    Return the greatest index at which *path* appears as the value of
    *after_token* (``--tmpfs <path>`` or the dest of ``--ro-bind-try
    <src> <path>``), or ``None`` if it never does.

    Used to compare mount ordering: the re-overlay must win, so its
    index must exceed the mask's.

    :param argv: The assembled bwrap argv.
    :param path: The path to locate.
    :param after_token: The verb token that introduces the mount.
    :returns: The last matching index, or ``None``.
    """
    found: int | None = None
    for i in range(len(argv) - 1):
        if argv[i] != after_token:
            continue
        # ``--tmpfs <path>`` (path at i+1) or ``--ro-bind-try <src> <path>``
        # (dest at i+2).
        if argv[i + 1] == path or (i + 2 < len(argv) and argv[i + 2] == path):
            found = i
    return found
