"""Exercise the shipped Rosie bundle through the production spec loader."""

from pathlib import Path

import pytest

from omnigent.policies.builtins.cel import cel_policy
from omnigent.spec import load

BUNDLE = Path(__file__).resolve().parents[2] / "examples" / "rosie"
PACKAGED_BUNDLE = (
    Path(__file__).resolve().parents[2] / "omnigent" / "resources" / "examples" / "rosie"
)
FACTORY_TOOLS = [
    "factory_get_issue",
    "factory_get_plan",
    "factory_get_feedback",
    "factory_get_status",
    "factory_ask_owner",
    "factory_submit_result",
]
FACTORY_TOOL_PATTERN = "^(factory__|mcp__omnigent__factory__|mcp__factory__)?factory_.*$"


def test_rosie_loads_workers_skills_and_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTORY_MCP_URL", "http://factory.test/mcp")
    monkeypatch.setenv("FACTORY_MCP_TOKEN", "factory-test-token")

    spec = load(BUNDLE)

    assert PACKAGED_BUNDLE.resolve() == BUNDLE.resolve()
    assert spec.name == "rosie"
    assert set(spec.tools.agents) == {"claude_code", "codex"}
    assert {worker.name for worker in spec.sub_agents} == set(spec.tools.agents)
    assert {skill.name for skill in spec.skills} == {
        "scope",
        "dispatch",
        "architecture",
        "investigate",
        "worktree-routing",
        "fanout",
        "verify",
        "cross-review",
        "remediate",
        "publish",
    }
    assert all(skill.content and skill.description for skill in spec.skills)
    assert all(not skill.user_invocable for skill in spec.skills)
    assert all(not worker.skills for worker in spec.sub_agents)

    assert [server.name for server in spec.mcp_servers] == ["factory"]
    factory = spec.mcp_servers[0]
    assert factory.url == "http://factory.test/mcp"
    assert factory.headers == {"Authorization": "Bearer factory-test-token"}
    assert factory.tools == FACTORY_TOOLS


def test_rosie_workers_deny_factory_tools_without_factory_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTORY_MCP_URL", "http://factory.test/mcp")
    monkeypatch.setenv("FACTORY_MCP_TOKEN", "factory-test-token")

    spec = load(BUNDLE)

    for worker in spec.sub_agents:
        assert all(server.name != "factory" for server in worker.mcp_servers)
        assert worker.guardrails is not None
        assert worker.guardrails.policies is not None
        guard = next(
            policy for policy in worker.guardrails.policies if policy.name == "deny_factory_tools"
        )
        assert guard.function is not None
        assert guard.function.path == "omnigent.policies.builtins.cel.cel_policy"
        assert guard.function.arguments is not None
        expression = guard.function.arguments["expression"]
        assert FACTORY_TOOL_PATTERN in expression
        assert '"result": "DENY"' in expression
        evaluate = cel_policy(**guard.function.arguments)
        for tool_name in (
            "factory_get_status",
            "factory__factory_get_status",
            "mcp__omnigent__factory__factory_get_status",
            "mcp__factory__factory_get_status",
        ):
            result = evaluate({"type": "tool_call", "data": {"name": tool_name, "arguments": {}}})
            assert result["result"] == "DENY"
        allowed = evaluate(
            {"type": "tool_call", "data": {"name": "sys_os_shell", "arguments": {}}}
        )
        assert allowed["result"] == "ALLOW"


def test_rosie_publication_reference_survives_bundle_copy(tmp_path: Path) -> None:
    import shutil

    copied = tmp_path / "rosie"
    shutil.copytree(BUNDLE, copied)
    spec = load(copied, expand_env=False)
    publish = next(skill for skill in spec.skills if skill.name == "publish")
    assert publish.skill_dir is not None
    reference = publish.skill_dir / "references" / "review-bot.md"
    assert reference.is_file()
    assert (
        reference.read_text()
        == (BUNDLE / "skills" / "publish" / "references" / "review-bot.md").read_text()
    )
