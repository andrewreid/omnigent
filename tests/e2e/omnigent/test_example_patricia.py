"""Structural test for the Patricia engineering design-debate bundle
(examples/patricia).

Patricia is the rigorous fork of Debby: a two-headed claude-vs-gpt adversarial
engineering debate, grounded on real merged code, that runs >=4 rounds and
produces a *ratified implementation contract* rather than a freeform synthesis.
Both heads must sign; unresolved design forks are halted for a human decision.

Pure spec-load — no LLM, no credentials — modeled on ``test_example_debby.py``.

What breaks if this fails:
- the two heads collapse onto one vendor (no cross-model contrast) or a head is
  dropped,
- a head silently switches harness (e.g. the GPT head ends up on claude-sdk),
- the ``debate`` skill is dropped or renamed (the debate protocol regresses),
- the Claude head loses its web tools (``web_fetch`` / ``web_search``) or its
  network-enabled ``linux_bwrap`` sandbox — it can no longer pull contemporary
  docs mid-debate,
- the ``os_env`` blocks disappear (the heads lose the file/shell tools the
  grounding-on-merged-code protocol relies on).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_patricia.py -> repo root is 3 parents up.
_PATRICIA_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "patricia"


@pytest.fixture(scope="module")
def patricia_spec() -> AgentSpec:
    """Load and validate the patricia bundle once for the module."""
    return load(_PATRICIA_BUNDLE)


def _by_name(spec: AgentSpec) -> dict[str, AgentSpec]:
    return {a.name: a for a in spec.sub_agents}


def test_patricia_is_two_headed_cross_vendor(patricia_spec: AgentSpec) -> None:
    """
    Patricia has exactly two heads — ``claude`` on claude-sdk and ``gpt`` on
    codex — so every debate contrasts two distinct vendors.

    A missing/renamed head, or both heads on the same harness, removes the
    cross-model adversarial contrast that is Patricia's entire reason to exist.
    """
    assert patricia_spec.name == "patricia"
    fam = {a.name: a.executor.config.get("harness") for a in patricia_spec.sub_agents}
    assert sorted(patricia_spec.tools.agents) == ["claude", "gpt"]
    assert fam["claude"] == "claude-sdk"
    assert fam["gpt"] == "codex"
    # Two distinct vendors → the heads always argue across providers.
    assert len(set(fam.values())) == 2


def test_patricia_heads_are_unpinned(patricia_spec: AgentSpec) -> None:
    """
    Neither head pins a model: each inherits whatever Claude / OpenAI provider
    the user configured. A Databricks-specific model id would 404 on a plain
    Anthropic / OpenAI key, so re-introducing a pin re-couples a head to one
    provider — fail here if a model reappears.
    """
    by_name = _by_name(patricia_spec)
    for name in ("claude", "gpt"):
        assert by_name[name].executor.model is None, name
        assert by_name[name].executor.profile is None, name


def test_patricia_debate_skill_present(patricia_spec: AgentSpec) -> None:
    """The ``debate`` skill is discovered from skills/debate/SKILL.md."""
    assert sorted(s.name for s in patricia_spec.skills) == ["debate"]


def test_patricia_orchestrator_has_unsandboxed_os_env(patricia_spec: AgentSpec) -> None:
    """
    The orchestrator carries an ``os_env`` block (so the ``sys_os_*`` tools
    register to read/write contracts). It needs no web access — the heads carry
    the web tools — so its sandbox stays ``type: none``.
    """
    assert patricia_spec.os_env is not None
    assert patricia_spec.os_env.type == "caller_process"
    assert patricia_spec.os_env.sandbox is not None
    assert patricia_spec.os_env.sandbox.type == "none"


def test_patricia_claude_head_has_web_tools(patricia_spec: AgentSpec) -> None:
    """
    The Claude head declares ``web_fetch`` and ``web_search`` (keyless
    duckduckgo) so it can pull contemporary docs during a debate. Tool
    inheritance is not supported, so these must be declared on the head itself.
    """
    claude = _by_name(patricia_spec)["claude"]
    builtins = {b.name: b for b in claude.tools.builtins}
    assert "web_fetch" in builtins
    assert "web_search" in builtins
    assert builtins["web_search"].config.get("search_provider") == "duckduckgo"


def test_patricia_claude_head_is_network_sandboxed(patricia_spec: AgentSpec) -> None:
    """
    The Claude head runs under ``linux_bwrap`` (network-enabled by default) so
    its web tools have outbound access, with cwd writable. ``type: none`` would
    still work for web, but bwrap is what the bundle ships — regressing it to
    ``none`` here would drop the intended hardening.
    """
    claude = _by_name(patricia_spec)["claude"]
    assert claude.os_env is not None
    assert claude.os_env.sandbox is not None
    assert claude.os_env.sandbox.type == "linux_bwrap"


def test_patricia_gpt_head_has_no_extra_web_builtins(patricia_spec: AgentSpec) -> None:
    """
    The GPT head gets web search natively from codex, so it declares no web
    builtins of its own — declaring one would be redundant (or wrong-keyed).
    """
    gpt = _by_name(patricia_spec)["gpt"]
    names = {b.name for b in gpt.tools.builtins}
    assert "web_search" not in names
    assert "web_fetch" not in names
