"""Exercise the shipped Molly bundle through the production spec loader."""

from pathlib import Path

from omnigent.spec import load

BUNDLE = Path(__file__).resolve().parents[2] / "examples" / "molly"


def test_molly_loads_workers_and_situational_skills() -> None:
    spec = load(BUNDLE, expand_env=False)
    assert spec.name == "molly"
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
    # Orchestrator skills must not be bundled into native worker definitions.
    assert all(not worker.skills for worker in spec.sub_agents)


def test_molly_publication_reference_survives_bundle_copy(tmp_path: Path) -> None:
    import shutil

    copied = tmp_path / "molly"
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
