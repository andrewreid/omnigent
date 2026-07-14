"""Tests for workspace validation pure helpers.

The async ``validate_workspace`` function requires a live host
connection, so we test only the synchronous helpers here — including
``validate_workspace_no_host``, the host-independent path-safety check
for sub-agent spawns that carry an explicit ``workspace`` but no host.
"""

from __future__ import annotations

import pytest

from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
    _is_relative_cwd,
    _is_subpath_of,
    validate_workspace_no_host,
)


class TestIsRelativeCwd:
    """Tests for the spec cwd classification helper."""

    def test_none_is_relative(self) -> None:
        assert _is_relative_cwd(None) is True

    def test_dot_is_relative(self) -> None:
        assert _is_relative_cwd(".") is True

    def test_dot_slash_is_relative(self) -> None:
        assert _is_relative_cwd("./") is True

    def test_empty_is_relative(self) -> None:
        assert _is_relative_cwd("") is True

    def test_dot_slash_subdir_is_relative(self) -> None:
        assert _is_relative_cwd("./src") is True

    def test_absolute_is_not_relative(self) -> None:
        assert _is_relative_cwd("/Users/alice/project") is False

    def test_tilde_is_not_relative(self) -> None:
        assert _is_relative_cwd("~/project") is False


class TestIsSubpathOf:
    """Tests for the canonicalized path containment check."""

    def test_same_path(self) -> None:
        assert _is_subpath_of("/a/b", "/a/b") is True

    def test_child_path(self) -> None:
        assert _is_subpath_of("/a/b/c", "/a/b") is True

    def test_not_a_subpath(self) -> None:
        assert _is_subpath_of("/a/b", "/a/b/c") is False

    def test_prefix_collision(self) -> None:
        """``/a/foo`` must NOT be treated as a subpath of ``/a/fo``."""
        assert _is_subpath_of("/a/foo", "/a/fo") is False

    def test_root_boundary(self) -> None:
        assert _is_subpath_of("/Users/corey/x", "/") is True

    def test_trailing_slash_boundary(self) -> None:
        assert _is_subpath_of("/a/b/c", "/a/b/") is True


class TestValidateWorkspaceNoHost:
    """Tests for the host-independent workspace path-safety check."""

    def test_relative_cwd_allows_any_absolute_workspace(self) -> None:
        """
        A relative agent cwd (``.`` — Patricia's case) imposes no
        boundary: any absolute workspace is accepted and returned
        normalized. This is the precedence path that pins a spawned
        agent to a chosen grounding repo.
        """
        assert (
            validate_workspace_no_host(
                workspace="/home/andrew/projects/timesheets", spec_cwd="."
            )
            == "/home/andrew/projects/timesheets"
        )

    def test_none_cwd_allows_any_absolute_workspace(self) -> None:
        assert (
            validate_workspace_no_host(workspace="/srv/repo", spec_cwd=None) == "/srv/repo"
        )

    def test_absolute_boundary_allows_subdir(self) -> None:
        assert (
            validate_workspace_no_host(workspace="/work/repo/pkg", spec_cwd="/work/repo")
            == "/work/repo/pkg"
        )

    def test_normalizes_dotdot_within_boundary(self) -> None:
        """``..`` segments are collapsed before the containment check."""
        assert (
            validate_workspace_no_host(workspace="/work/repo/a/../b", spec_cwd="/work/repo")
            == "/work/repo/b"
        )

    def test_rejects_workspace_outside_absolute_boundary(self) -> None:
        with pytest.raises(WorkspaceValidationError, match="outside the agent's required path"):
            validate_workspace_no_host(workspace="/etc/secrets", spec_cwd="/work/repo")

    def test_rejects_dotdot_escape_of_boundary(self) -> None:
        """A ``..`` escape normalizes out of the boundary and is rejected."""
        with pytest.raises(WorkspaceValidationError, match="outside the agent's required path"):
            validate_workspace_no_host(workspace="/work/repo/../other", spec_cwd="/work/repo")

    def test_rejects_non_absolute_workspace(self) -> None:
        with pytest.raises(WorkspaceValidationError, match="absolute path"):
            validate_workspace_no_host(workspace="relative/dir", spec_cwd=".")

    def test_rejects_tilde_workspace(self) -> None:
        """The server never expands ``~`` — a tilde workspace is rejected."""
        with pytest.raises(WorkspaceValidationError, match="absolute path"):
            validate_workspace_no_host(workspace="~/repo", spec_cwd=".")

    def test_rejects_non_absolute_agent_boundary(self) -> None:
        """A tilde agent boundary can't be canonicalized without a host."""
        with pytest.raises(WorkspaceValidationError, match="not an absolute path"):
            validate_workspace_no_host(workspace="/work/repo", spec_cwd="~/work")
