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
- the Claude head loses its web tools (``web_fetch`` / ``web_search``) — it can
  no longer pull contemporary docs mid-debate,
- the Claude head regains a sandbox (it must stay ``type: none``, matching
  Debby's heads — the read-mostly debate head is intentionally unsandboxed),
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


def test_patricia_heads_pin_frontier_models(patricia_spec: AgentSpec) -> None:
    """
    Each head pins its intended frontier model via top-level ``executor.model``
    so debates run on the chosen models by default, not whatever provider default
    ``omnigent setup`` left configured (a per-session ``/model`` override still
    outranks the spec pin):

    - claude head -> ``claude-fable-5``
    - gpt head    -> ``gpt-5.6-sol``

    The model id is a passthrough string (the spec parser stores it verbatim;
    there is no static-catalog membership check), so a non-catalog id like
    ``gpt-5.6-sol`` validates clean even though it is not in the repo catalog;
    where and how it actually resolves is up to the operator's configured
    provider for that harness. Reasoning effort is deliberately NOT pinned here:
    for a
    ``type: omnigent`` harness head there is no static effort slot (the harness
    adapter reads effort per-turn only), so ``profile`` stays ``None`` and effort
    is a session-level concern. Fail here if a pin drifts or a profile appears.
    """
    by_name = _by_name(patricia_spec)
    assert by_name["claude"].executor.model == "claude-fable-5"
    assert by_name["gpt"].executor.model == "gpt-5.6-sol"
    for name in ("claude", "gpt"):
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


def test_patricia_claude_head_is_unsandboxed(patricia_spec: AgentSpec) -> None:
    """
    The Claude head is unsandboxed, matching Debby's heads exactly. A
    read-mostly design-debate head needs no containment, so its ``os_env``
    block is the same unsandboxed shape as Debby's:

    - ``type: caller_process`` with ``sandbox.type == "none"`` — no
      ``linux_bwrap`` backend, so the bundle also loads on macOS.
    - none of the bwrap-only knobs remain: no ``write_paths`` / ``write_files``
      grants, and no ``cwd_allow_hidden`` / ``cwd_prune_dirs`` (those only mean
      anything under a bwrap-style read-only cwd). If any of them reappears, the
      head has drifted back toward a sandbox Debby's heads never carried.

    The grounded repo being read-only is now a BEHAVIORAL instruction in the
    head's prompt ("read but do not modify"), not a physically enforced mount.
    """
    claude = _by_name(patricia_spec)["claude"]
    assert claude.os_env is not None
    assert claude.os_env.type == "caller_process"
    sandbox = claude.os_env.sandbox
    assert sandbox is not None
    assert sandbox.type == "none", (
        f"Claude head must be unsandboxed (sandbox.type == 'none') to match "
        f"Debby; got {sandbox.type!r}."
    )

    # No bwrap-only keys should linger after dropping the sandbox.
    assert sandbox.write_paths is None, sandbox.write_paths
    assert not sandbox.write_files, sandbox.write_files
    assert sandbox.cwd_allow_hidden is None, (
        f"cwd_allow_hidden is a bwrap-only knob; must be gone under sandbox "
        f"type none, got {sandbox.cwd_allow_hidden!r}."
    )
    assert sandbox.cwd_prune_dirs is None, (
        f"cwd_prune_dirs is a bwrap-only knob; must be gone under sandbox type "
        f"none, got {sandbox.cwd_prune_dirs!r}."
    )


def test_patricia_gpt_head_has_no_extra_web_builtins(patricia_spec: AgentSpec) -> None:
    """
    The GPT head gets web search natively from codex, so it declares no web
    builtins of its own — declaring one would be redundant (or wrong-keyed).
    """
    gpt = _by_name(patricia_spec)["gpt"]
    names = {b.name for b in gpt.tools.builtins}
    assert "web_search" not in names
    assert "web_fetch" not in names
