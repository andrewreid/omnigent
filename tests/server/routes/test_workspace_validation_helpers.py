"""Tests for workspace validation pure helpers.

The async ``validate_workspace`` function requires a live host
connection, so we test only the synchronous helpers here — including
``validate_workspace_no_host``, the single canonical-containment
chokepoint for session-create paths that set a workspace with no host.
It stats the server-local filesystem (existence + ``realpath``), so its
tests build real dirs / symlinks under ``tmp_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

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
    """Canonical (realpath) containment check for the no-host paths.

    Stats the server-local filesystem, so the happy paths build real
    directories under ``tmp_path``; the argument-shape rejections
    (non-absolute / tilde) fail before any filesystem access.
    """

    def test_relative_cwd_allows_any_existing_absolute_workspace(self, tmp_path: Path) -> None:
        """
        A relative agent cwd (``.`` — Patricia's case) imposes no
        boundary: any existing absolute workspace is accepted and
        returned canonicalized. This is the precedence path that pins a
        spawned agent to a chosen grounding repo.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        assert validate_workspace_no_host(workspace=str(repo), spec_cwd=".") == os.path.realpath(
            str(repo)
        )

    def test_none_cwd_allows_existing_absolute_workspace(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        assert validate_workspace_no_host(
            workspace=str(repo), spec_cwd=None
        ) == os.path.realpath(str(repo))

    def test_absolute_boundary_allows_subdir(self, tmp_path: Path) -> None:
        boundary = tmp_path / "work"
        pkg = boundary / "pkg"
        pkg.mkdir(parents=True)
        assert validate_workspace_no_host(
            workspace=str(pkg), spec_cwd=str(boundary)
        ) == os.path.realpath(str(pkg))

    def test_rejects_symlink_escape_of_boundary(self, tmp_path: Path) -> None:
        """
        P1-a: a workspace INSIDE the boundary that is a symlink to a
        directory OUTSIDE it passes a lexical check but must be rejected
        on the canonical (realpath) check — the runner resolves the
        symlink before sandboxing, so lexical-only would re-open the
        $HOME-exposure hole.
        """
        boundary = tmp_path / "work"
        boundary.mkdir()
        outside = tmp_path / "home"
        outside.mkdir()
        link = boundary / "link"
        link.symlink_to(outside)
        with pytest.raises(WorkspaceValidationError, match="resolves outside"):
            validate_workspace_no_host(workspace=str(link), spec_cwd=str(boundary))

    def test_rejects_workspace_outside_absolute_boundary(self, tmp_path: Path) -> None:
        boundary = tmp_path / "work"
        boundary.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(WorkspaceValidationError, match="resolves outside"):
            validate_workspace_no_host(workspace=str(other), spec_cwd=str(boundary))

    def test_rejects_nonexistent_workspace(self, tmp_path: Path) -> None:
        """Existence is required so ``realpath`` fully resolves symlinks."""
        with pytest.raises(WorkspaceValidationError, match="does not exist"):
            validate_workspace_no_host(workspace=str(tmp_path / "nope"), spec_cwd=".")

    def test_rejects_file_workspace(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(WorkspaceValidationError, match="not a directory"):
            validate_workspace_no_host(workspace=str(f), spec_cwd=".")

    def test_rejects_non_absolute_workspace(self) -> None:
        with pytest.raises(WorkspaceValidationError, match="absolute path"):
            validate_workspace_no_host(workspace="relative/dir", spec_cwd=".")

    def test_rejects_tilde_workspace(self) -> None:
        """The server never expands ``~`` — a tilde workspace is rejected."""
        with pytest.raises(WorkspaceValidationError, match="absolute path"):
            validate_workspace_no_host(workspace="~/repo", spec_cwd=".")

    def test_rejects_non_absolute_agent_boundary(self, tmp_path: Path) -> None:
        """A tilde agent boundary can't be canonicalized without a host."""
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(WorkspaceValidationError, match="not an absolute path"):
            validate_workspace_no_host(workspace=str(repo), spec_cwd="~/work")

    def test_dot_slash_subdir_present_is_accepted(self, tmp_path: Path) -> None:
        """B4: os_env.cwd './src' requires <workspace>/src to exist."""
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        assert validate_workspace_no_host(
            workspace=str(repo), spec_cwd="./src"
        ) == os.path.realpath(str(repo))

    def test_dot_slash_subdir_missing_is_rejected(self, tmp_path: Path) -> None:
        """B4: a workspace missing the required './src' subdir is rejected."""
        repo = tmp_path / "repo"
        repo.mkdir()  # no src/ inside
        with pytest.raises(WorkspaceValidationError, match="subdirectory 'src'"):
            validate_workspace_no_host(workspace=str(repo), spec_cwd="./src")

    def test_dot_slash_subdir_that_is_a_file_is_rejected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").write_text("x")  # file, not dir
        with pytest.raises(WorkspaceValidationError, match="subdirectory 'src'"):
            validate_workspace_no_host(workspace=str(repo), spec_cwd="./src")

    def test_persisted_path_is_canonical_and_stable_under_runtime_resolve(
        self, tmp_path: Path
    ) -> None:
        """
        persist -> harness-cwd wiring: the validator stores a canonical
        (symlink-resolved) path, and the runner selects the harness cwd
        via ``Path(stored).resolve()`` (app._session_runtime_cwd). Prove
        the stored value already equals its own resolve() — i.e. the
        exact validated directory is what the sandbox roots at, with no
        drift back through the symlink.
        """
        real = tmp_path / "repo"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        stored = validate_workspace_no_host(workspace=str(link), spec_cwd=".")
        assert stored == os.path.realpath(str(real))
        # Runner's cwd selection is Path(stored).resolve(); idempotent
        # here means the harness roots at exactly the validated dir.
        assert Path(stored).resolve() == Path(stored)
