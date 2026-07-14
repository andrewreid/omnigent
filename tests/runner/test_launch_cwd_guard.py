"""Point-of-use containment guard for the harness-launch cwd.

``_guard_launch_cwd`` is the uniform defense-in-depth recheck applied at
every harness-launch site (the direct spawn sites and the streaming /
HTTP fallback via ``_resolve_harness_config``). It refuses a resolved
sandbox cwd that escapes the spec's absolute ``os_env.cwd`` boundary and
is a no-op for relative / unset boundaries (Patricia's ``cwd: .``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner.app import _guard_launch_cwd, _resolve_harness_config


def _spec(cwd: str | None) -> SimpleNamespace:
    """A minimal spec stub exposing ``os_env.cwd`` and an executor."""
    return SimpleNamespace(
        os_env=SimpleNamespace(cwd=cwd),
        executor=SimpleNamespace(type="claude-sdk", config={}),
    )


class TestGuardLaunchCwd:
    def test_none_cwd_is_passthrough(self) -> None:
        assert _guard_launch_cwd(None, _spec("/work/repo")) is None

    def test_relative_boundary_is_noop(self) -> None:
        """Patricia-style ``cwd: .`` imposes no boundary — any cwd passes."""
        cwd = Path("/anywhere/at/all")
        assert _guard_launch_cwd(cwd, _spec(".")) == cwd

    def test_none_boundary_is_noop(self) -> None:
        cwd = Path("/anywhere")
        assert _guard_launch_cwd(cwd, _spec(None)) == cwd

    def test_dot_slash_subdir_boundary_is_noop(self) -> None:
        cwd = Path("/anywhere")
        assert _guard_launch_cwd(cwd, _spec("./src")) == cwd

    def test_within_absolute_boundary_passes(self, tmp_path: Path) -> None:
        boundary = tmp_path / "repo"
        sub = boundary / "pkg"
        sub.mkdir(parents=True)
        assert _guard_launch_cwd(sub.resolve(), _spec(str(boundary))) == sub.resolve()

    def test_escaping_absolute_boundary_raises(self) -> None:
        with pytest.raises(RuntimeError, match="escapes the agent"):
            _guard_launch_cwd(Path("/etc"), _spec("/work/repo"))

    def test_no_os_env_is_noop(self) -> None:
        cwd = Path("/anywhere")
        assert _guard_launch_cwd(cwd, SimpleNamespace()) == cwd


@pytest.mark.asyncio
async def test_resolve_harness_config_raises_on_escaping_cwd() -> None:
    """
    FIX 3: the streaming / HTTP launch path resolves the spec inside
    ``_resolve_harness_config`` and must apply the same guard — an
    escaping cwd raises before it can reach the spawn env.
    """

    async def _resolver(_agent_id: str, _session_id: str | None) -> SimpleNamespace:
        return _spec("/work/repo")

    with pytest.raises(RuntimeError, match="escapes the agent"):
        await _resolve_harness_config(
            agent_id="ag_x",
            spec_resolver=_resolver,
            session_id="conv_x",
            cwd=Path("/etc"),
        )


@pytest.mark.asyncio
async def test_resolve_harness_config_relative_boundary_passes_cwd_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Patricia-style ``cwd: .`` — the guard is a no-op, so the original
    cwd flows through to the spawn-env builder unchanged and the launch
    proceeds (no raise). Stub the builder to capture the cwd it receives.
    """

    async def _resolver(_agent_id: str, _session_id: str | None) -> SimpleNamespace:
        return _spec(".")

    captured: dict[str, object] = {}

    def _fake_build(spec: object, harness: str, **kw: object) -> dict[str, str]:
        captured["cwd"] = kw.get("cwd")
        return {"OK": "1"}

    monkeypatch.setattr(runner_app, "_build_spawn_env_from_spec", _fake_build)

    cwd = Path("/home/me/grounding-repo")
    harness, _spawn_env = await _resolve_harness_config(
        agent_id="ag_x",
        spec_resolver=_resolver,
        session_id="conv_x",
        cwd=cwd,
    )
    assert harness == "claude-sdk"
    assert captured["cwd"] == cwd
