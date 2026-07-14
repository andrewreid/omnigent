"""`omnigent run --workspace` fails loud on unsupported dispatch shapes.

--workspace only pins the sandbox cwd of a runner served by a co-located
auto-spawned local server (a fresh local-AGENT launch). Every other
dispatch shape used to silently drop it or would validate against the
wrong filesystem, so the CLI now rejects those combinations up front
(B5 + explicit-remote reject).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

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
