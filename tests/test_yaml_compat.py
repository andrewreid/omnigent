"""Resolver behaviour must survive the libyaml-backed loader base.

``omnigent._yaml_compat.SafeLoaderBase`` is ``yaml.CSafeLoader`` wherever
PyYAML was built against libyaml, and ``yaml.SafeLoader`` otherwise. The two
parsers are different C/Python implementations, so every guarantee the spec
loaders rely on is asserted here against whichever base is actually active.
"""

from __future__ import annotations

import yaml

from omnigent._yaml_compat import USING_LIBYAML, SafeLoaderBase, safe_load
from omnigent.inner.loader import _OmnigentYamlLoader
from omnigent.spec.parser import _ConfigYamlLoader

# Both spec loaders narrow the bool resolver the same way, so they carry the
# same expectations.
_NARROWED_LOADERS = (_ConfigYamlLoader, _OmnigentYamlLoader)

# ``on``/``off``/``yes``/``no`` are YAML 1.1 bool aliases. The policy system
# keys on ``on:``, so they must stay strings; ``true``/``false`` must not.
_YAML_1_2_BOOLS = """\
a: on
b: off
c: yes
d: no
e: true
f: false
"""


def test_base_is_libyaml_when_available() -> None:
    """The base tracks libyaml availability rather than being hard-coded."""
    assert SafeLoaderBase is getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert USING_LIBYAML is (SafeLoaderBase is not yaml.SafeLoader)


def test_narrowed_loaders_build_on_the_active_base() -> None:
    """Both spec loaders subclass the shared base, not a hard-coded loader."""
    for loader in _NARROWED_LOADERS:
        assert issubclass(loader, SafeLoaderBase), loader


def test_narrowed_loaders_keep_yaml_1_1_bool_aliases_as_strings() -> None:
    """``on``/``off``/``yes``/``no`` stay strings; ``true``/``false`` stay bools.

    This is the one behaviour the C parser could plausibly break — libyaml
    scans tokens itself, and only calls back into the Python resolver to tag
    them. If that callback ever stopped honouring the subclass's resolver
    table, ``on:`` would silently become the key ``True`` and every policy
    selector in the repo would break.
    """
    for loader in _NARROWED_LOADERS:
        parsed = yaml.load(_YAML_1_2_BOOLS, Loader=loader)
        assert parsed == {
            "a": "on",
            "b": "off",
            "c": "yes",
            "d": "no",
            "e": True,
            "f": False,
        }, loader
        # Bools must be real bools, not the strings "true"/"false".
        assert parsed["e"] is True, loader
        assert parsed["f"] is False, loader


def test_narrowed_loaders_own_their_resolver_table() -> None:
    """Narrowing must not mutate the resolver dict shared by every loader.

    ``yaml_implicit_resolvers`` lives on PyYAML's ``BaseResolver`` and is the
    same object for ``SafeLoader`` and ``CSafeLoader`` alike, so an in-place
    edit would strip bool parsing from every ``yaml.safe_load`` caller in the
    process.
    """
    for loader in _NARROWED_LOADERS:
        assert loader.yaml_implicit_resolvers is not SafeLoaderBase.yaml_implicit_resolvers
        assert loader.yaml_implicit_resolvers is not yaml.SafeLoader.yaml_implicit_resolvers
    assert yaml.safe_load("on") is True
    assert yaml.safe_load("false") is False


def test_safe_load_matches_yaml_safe_load() -> None:
    """The drop-in keeps stock ``safe_load`` semantics, YAML 1.1 bools included."""
    source = _YAML_1_2_BOOLS + "g: 2026-07-24\nh: [1, 2]\ni: null\n"
    assert safe_load(source) == yaml.safe_load(source)
    # Unlike the narrowed loaders, this one still resolves the 1.1 aliases.
    assert safe_load("on") is True


def test_safe_load_handles_empty_and_non_mapping_documents() -> None:
    """Callers isinstance-check the result, so the empty cases must match."""
    for source in ("", "# comment only\n", "- a\n- b\n", "just a string\n"):
        assert safe_load(source) == yaml.safe_load(source), source


def test_parse_errors_stay_marked_yaml_errors() -> None:
    """Call sites catch ``yaml.YAMLError`` and report the mark's line/column.

    libyaml words its messages differently and drops the echoed source line,
    but the exception type and the mark must survive — ``diagnose_yaml_rejection``
    promises the user a location.
    """
    broken = "name: agent\ndescription: has: unquoted colon\n"
    for load in (safe_load, lambda s: yaml.load(s, Loader=_ConfigYamlLoader)):
        try:
            load(broken)
        except yaml.YAMLError as exc:
            assert isinstance(exc, yaml.MarkedYAMLError)
            assert exc.problem_mark is not None
            assert exc.problem_mark.line == 1
            assert exc.problem_mark.column == 16
        else:  # pragma: no cover - the document is malformed by construction
            raise AssertionError("expected a YAMLError")
