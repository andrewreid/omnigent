"""`omnigent run --workspace` fails loud on unsupported dispatch shapes.

--workspace only pins the sandbox cwd of a runner served by a co-located
auto-spawned local server (a fresh local-AGENT launch). Every other
dispatch shape used to silently drop it or would validate against the
wrong filesystem, so the CLI now rejects those combinations up front
(B5 + explicit-remote reject).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import omnigent.cli as cli_mod
from omnigent.cli import cli


def test_workspace_with_remote_server_fails_loud(tmp_path: Path) -> None:
    """Explicit --workspace against a remote --server is rejected (not
    validated on the remote server's filesystem)."""
    result = CliRunner().invoke(
        cli,
        ["run", "--server", "https://remote.example.com", "agent.yaml",
         "--workspace", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "--workspace is only supported" in result.output


def test_workspace_with_schemaless_remote_server_fails_loud(tmp_path: Path) -> None:
    """A schemaless remote --server value (no http(s)://) is still remote
    and must fail loud — the guard classifies any nonempty non-'' server
    as remote, not only URL-shaped ones."""
    result = CliRunner().invoke(
        cli,
        ["run", "agent.yaml", "--server", "remote.example.com",
         "--workspace", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "--workspace is only supported" in result.output


def test_workspace_with_no_session_prompt_fails_loud(tmp_path: Path) -> None:
    """A --no-session -p one-shot (a branch that dropped --workspace)
    now fails loud instead of running in the default workspace."""
    result = CliRunner().invoke(
        cli,
        ["run", "agent.yaml", "--no-session", "-p", "hi", "--workspace", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "--workspace is only supported" in result.output


def test_workspace_nonexistent_dir_fails_loud() -> None:
    """A non-existent --workspace directory is rejected before dispatch."""
    result = CliRunner().invoke(
        cli, ["run", "agent.yaml", "--workspace", "/no/such/dir/xyz"]
    )
    assert result.exit_code != 0
    assert "must be an existing directory" in result.output


def test_workspace_local_autospawn_server_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--server ""` (auto-spawned co-located local server) with a valid
    --workspace passes the guard (dispatch stubbed to isolate the check)."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli_mod, "_dispatch_run", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(
        cli, ["run", "agent.yaml", "--server", "", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "--workspace is only supported" not in result.output
    assert seen["workspace"] == str(tmp_path)


def test_workspace_plain_local_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Plain `omnigent run agent.yaml --workspace <dir>` (no --server)
    passes the guard."""
    seen: dict[str, Any] = {}
    # Empty config so a machine-local configured server can't turn this
    # into a remote invocation and mask the assertion.
    monkeypatch.setattr(cli_mod, "_load_effective_config", lambda *a, **k: {})
    monkeypatch.setattr(cli_mod, "_dispatch_run", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(
        cli, ["run", "agent.yaml", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "--workspace is only supported" not in result.output
    assert seen["workspace"] == str(tmp_path)
