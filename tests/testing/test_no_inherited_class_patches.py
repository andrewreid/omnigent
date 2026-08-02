"""Suite-wide guard against tests planting inherited-attribute shadows.

Assigning ``SomeClass.attr = ...`` when ``attr`` is *inherited* creates a
real entry in ``SomeClass.__dict__``. "Restoring" by assigning the original
back does not remove it, so every later patch of the defining owner is
silently bypassed for the rest of the pytest worker. The damage is invisible
to the file that causes it and surfaces as an unrelated failure elsewhere.

``monkeypatch.setattr`` and ``mock.patch.object`` are exempt: both restore
through their own bookkeeping instead of a bare re-assignment, and for a
target that only *inherits* the attribute pytest records it as absent
(``target.__dict__.get``) and deletes the shadow on teardown. Only unmanaged
assignment is unsafe, so that is what this checks.

The resolution logic is exercised by ``test_guard_flags_every_import_form``
below. A guard that cannot be shown to fire is worthless, so that self-test
is part of the guard, not an extra.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _dotted(node: ast.expr) -> str | None:
    """Render a dotted attribute chain, or ``None`` if it is not one."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _import_map(tree: ast.Module) -> dict[str, str]:
    """Map each name this file's imports BIND to the object it refers to.

    The binding, not the text, is what matters. ``import a.b.c`` binds ``a``
    to the *root* module ``a`` -- mapping ``a`` to ``a.b.c`` would make a
    later ``a.b.c.X.y`` resolve as ``a.b.c`` + ``.b.c.X``, which never
    resolves and silently passes the offender.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    out[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return out


def _resolve(path: str) -> object | None:
    """Import the longest importable prefix of *path*, then getattr the rest."""
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:i]))
        except Exception:  # unimportable prefix, try a shorter one
            continue
        for attr in parts[i:]:
            obj = getattr(obj, attr, None)
            if obj is None:
                return None
        return obj
    return None


def _class_shadow_owner(imports: dict[str, str], target: str, attr: str) -> str | None:
    """Return the defining class if *target* only INHERITS *attr*, else None."""
    head, _, rest = target.partition(".")
    full = imports[head] + (f".{rest}" if rest else "") if head in imports else target
    obj = _resolve(full)
    if not isinstance(obj, type) or attr in vars(obj):
        return None
    return next((k.__name__ for k in obj.__mro__ if attr in vars(k)), None)


def _shadow_reports(source: str, label: str) -> list[str]:
    """Report each plain ``X.y = ...`` in *source* where *X* only inherits ``y``.

    Pure over text so the self-test can feed it synthetic sources.

    :param source: Python source to scan.
    :param label: Location prefix used in the returned messages.
    :returns: One human-readable report per offending assignment.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imports = _import_map(tree)
    local_classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        elif isinstance(node, ast.Call):
            # Builtin setattr(X, "y", v) -- same hazard, no restore.
            if getattr(node.func, "id", "") != "setattr" or len(node.args) < 2:
                continue
            name = node.args[1]
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                continue
            owner = _dotted(node.args[0])
            if owner:
                found.append((node.lineno, owner, name.value))
            continue
        else:
            continue

        for target_node in targets:
            if not isinstance(target_node, ast.Attribute):
                continue
            owner = _dotted(target_node.value)
            if owner and owner != "self" and not owner.startswith("self."):
                if owner.split(".")[0] not in local_classes:
                    found.append((node.lineno, owner, target_node.attr))

    reports = []
    for line, target, attr in found:
        defining = _class_shadow_owner(imports, target, attr)
        if defining is not None:
            reports.append(f"{label}:{line}  {target}.{attr} is defined by {defining}")
    return reports


def _inherited_class_shadows(path: pathlib.Path) -> list[str]:
    """Report each inherited-class-attribute assignment in the file at *path*."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _shadow_reports(source, str(path.relative_to(TESTS_ROOT.parent)))


# ── the guard's own correctness ─────────────────────────────────────────────

# `do_ping` is defined by _PGDialect_common_psycopg and only INHERITED by
# PGDialect_psycopg, so every spelling below plants a permanent shadow.
_BAD_SOURCES = {
    "import a.b.c": (
        "import sqlalchemy.dialects.postgresql.psycopg\n"
        "sqlalchemy.dialects.postgresql.psycopg.PGDialect_psycopg.do_ping = None\n"
    ),
    "import a.b.c as x": (
        "import sqlalchemy.dialects.postgresql.psycopg as pg\n"
        "pg.PGDialect_psycopg.do_ping = None\n"
    ),
    "from a.b import c": (
        "from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg\n"
        "PGDialect_psycopg.do_ping = None\n"
    ),
    "from a.b import c as x": (
        "from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg as PD\n"
        "PD.do_ping = None\n"
    ),
    "builtin setattr": (
        "from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg\n"
        'setattr(PGDialect_psycopg, "do_ping", None)\n'
    ),
}

_GOOD_SOURCES = {
    # Patching the class that actually defines the attribute owns it outright.
    "assign on the defining owner": (
        "from sqlalchemy.dialects.postgresql.psycopg import _PGDialect_common_psycopg\n"
        "_PGDialect_common_psycopg.do_ping = None\n"
    ),
    # monkeypatch/mock restore through their own bookkeeping, not assignment.
    "monkeypatch.setattr on inherited": (
        "from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg\n"
        'def test_x(monkeypatch):\n    monkeypatch.setattr(PGDialect_psycopg, "do_ping", None)\n'
    ),
    "class defined in this file": ("class Local:\n    pass\n\n\nLocal.anything = 1\n"),
    "instance attribute": ("def test_x(obj):\n    obj.do_ping = None\n"),
    "self attribute": ("class C:\n    def __init__(self):\n        self.do_ping = None\n"),
}


@pytest.mark.parametrize("form", sorted(_BAD_SOURCES))
def test_guard_flags_every_import_form(form: str) -> None:
    """The guard must fire for an inherited assignment via ANY import spelling.

    Regression: the unaliased ``import a.b.c`` form silently resolved to
    nothing, so a real offender passed the guard. A guard that cannot be
    shown to fire licenses a false belief that the defect class is closed.

    :param form: Key into :data:`_BAD_SOURCES`.
    """
    reports = _shadow_reports(_BAD_SOURCES[form], "synthetic")
    assert reports, f"guard MISSED an inherited-attribute assignment via {form!r}"
    assert "_PGDialect_common_psycopg" in reports[0]


@pytest.mark.parametrize("form", sorted(_GOOD_SOURCES))
def test_guard_does_not_flag_safe_forms(form: str) -> None:
    """The guard must stay silent on assignments that plant no shadow.

    :param form: Key into :data:`_GOOD_SOURCES`.
    """
    assert _shadow_reports(_GOOD_SOURCES[form], "synthetic") == [], (
        f"guard false-positived on {form!r}"
    )


# ── the guard itself ────────────────────────────────────────────────────────


@pytest.mark.timeout(900)
def test_no_test_assigns_an_inherited_class_attribute() -> None:
    """No test may assign directly to an attribute its target only inherits."""
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        offenders.extend(_inherited_class_shadows(path))

    assert not offenders, (
        "Direct assignment to an inherited class attribute plants a permanent "
        "shadow in the subclass __dict__ that survives 'restoration' and "
        "silently bypasses every later patch of the defining owner:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse monkeypatch.setattr / mock.patch.object, or patch the "
        "defining class directly."
    )
