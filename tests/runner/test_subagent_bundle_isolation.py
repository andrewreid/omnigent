"""Sub-agent sessions must not inherit the parent bundle's skills.

A sub-agent session's ``ResolvedSpec.workdir`` is the bundle root the
runner exposes to the child harness as its skills/tools source
(``--plugin-dir`` for claude-native, ``CODEX_HOME/skills`` for
codex-native, ``HARNESS_*_BUNDLE_DIR`` for the SDK harnesses). Pointing
it at the PARENT bundle root leaks the parent's ``skills/`` into every
worker — e.g. polly's cross-review orchestration skill showing up in a
dispatched reviewer's skill list, which then starts orchestrating
instead of reviewing. The child's bundle root is its own
``agents/<name>/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import _sub_agent_bundle_dir


def _make_bundle(root: Path) -> Path:
    """Create a polly-shaped bundle: parent skills + two sub-agents."""
    (root / "skills" / "cross-review").mkdir(parents=True)
    (root / "skills" / "cross-review" / "SKILL.md").write_text(
        "---\nname: cross-review\ndescription: d\n---\nbody\n"
    )
    (root / "agents" / "claude_code").mkdir(parents=True)
    (root / "agents" / "claude_code" / "config.yaml").write_text("spec_version: 1\n")
    (root / "agents" / "codex").mkdir(parents=True)
    return root


def test_sub_agent_bundle_dir_resolves_child_dir(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    assert _sub_agent_bundle_dir(bundle, "claude_code") == bundle / "agents" / "claude_code"


def test_sub_agent_bundle_dir_resolves_nested_child(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    nested = bundle / "agents" / "claude_code" / "agents" / "grandchild"
    nested.mkdir(parents=True)
    assert _sub_agent_bundle_dir(bundle, "grandchild") == nested


def test_sub_agent_bundle_dir_missing_child_is_none(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    # A synthesized sub-agent (e.g. __web_researcher) has no on-disk dir:
    # fail closed — no bundle, no inherited skills.
    assert _sub_agent_bundle_dir(bundle, "__web_researcher") is None


def test_sub_agent_bundle_dir_none_parent_is_none() -> None:
    assert _sub_agent_bundle_dir(None, "claude_code") is None


@pytest.mark.asyncio
async def test_resolve_harness_config_uses_child_bundle_dir(tmp_path: Path) -> None:
    """The harness-HTTP swap path must thread the CHILD's bundle dir.

    Regression for the cache-miss/reconnect paths: the sub-agent spec swap
    kept the parent workdir, so SDK workers received HARNESS_*_BUNDLE_DIR
    pointing at the parent bundle and its skills leaked in.
    """
    from omnigent.runner.app import ResolvedSpec, _resolve_harness_config
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    bundle = _make_bundle(tmp_path)
    child = AgentSpec(
        spec_version=1,
        name="claude_code",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    parent = AgentSpec(
        spec_version=1,
        name="polly",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[child],
    )

    async def resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        return ResolvedSpec(spec=parent, workdir=bundle)

    _, spawn_env = await _resolve_harness_config(
        agent_id="ag_polly",
        spec_resolver=resolver,
        sub_agent_name="claude_code",
    )
    assert spawn_env is not None
    assert spawn_env["HARNESS_CLAUDE_SDK_BUNDLE_DIR"] == str(bundle / "agents" / "claude_code")
