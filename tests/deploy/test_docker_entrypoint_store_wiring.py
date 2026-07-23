"""Guard: the Docker entrypoint wires every store ``create_app`` accepts.

``omnigent/cli.py`` and ``deploy/docker/entrypoint.py`` are two independent
paths that build the same app, and the entrypoint hand-duplicates the CLI's
store construction. A store added to ``create_app`` but not to the entrypoint
does not fail loudly: the container simply drops the feature that store backs
(its router is mounted only when the store is not ``None``), and operators see
a bare 404 with no signal that this is deployment-mode-specific.

Both files are read statically rather than imported so this stays a pure
source-level check — no config, no database, no app construction.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _REPO_ROOT / "deploy" / "docker" / "entrypoint.py"
_APP = _REPO_ROOT / "omnigent" / "server" / "app.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _create_app_store_params() -> set[str]:
    """Every ``*_store`` parameter ``create_app`` accepts."""
    tree = _parse(_APP)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    args = fn.args
    return {
        arg.arg
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
        if arg.arg.endswith("_store")
    }


def _entrypoint_create_app_kwargs() -> set[str]:
    """Keyword names the entrypoint passes to ``create_app``."""
    tree = _parse(_ENTRYPOINT)
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_app"
    )
    return {kw.arg for kw in call.keywords if kw.arg is not None}


def test_entrypoint_passes_every_create_app_store() -> None:
    missing = _create_app_store_params() - _entrypoint_create_app_kwargs()
    assert not missing, (
        "deploy/docker/entrypoint.py must pass every store create_app accepts, "
        f"but omits: {sorted(missing)}. Construct the store next to the others "
        "in build_app() and pass it through — an omitted store silently "
        "disables its routes in Docker/Kubernetes deployments."
    )


def test_entrypoint_wires_project_store() -> None:
    """The projects router mounts only when ``project_store`` is supplied."""
    assert "project_store" in _entrypoint_create_app_kwargs()
