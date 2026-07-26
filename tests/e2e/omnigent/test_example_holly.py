"""Headline contract for the holly coding-orchestrator bundle (examples/holly).

Thin by design. ``CONTRIBUTING.md`` reserves ``tests/e2e/`` for full-stack flows
a unit test cannot capture, and a spec-load check is not that — so this file
keeps only the headline shape of the bundle (identity, roster, executor
harnesses, the policy names and the read-only MCP surface) and satisfies the
per-example coverage rule enforced by ``test_examples_coverage_sync.py``.

The detailed structural assertions — the reviewer mandate, the review-lifecycle
branches, the worker obligations, the policy argument values — live in
``tests/spec/test_holly_bundle.py``, which runs in the default suite.

holly delegates every coding task to its ``claude_code`` / ``codex`` / ``pi``
workers, reviews the LOCAL branch diff with a different-vendor reviewer, and
only then sequences publication. No policy SHIPPED BY HOLLY gates ordinary
publication: ``blast_radius`` denies destructive push variants — ``--force*``,
``--delete``, ``--mirror``, ``--prune``, bundled short forms containing ``-f`` or
``-d``, and ``+refspec`` / ``:refspec`` — while a plain ``git push`` is ungated,
so that ordering is prompt discipline and the tests are written accordingly. A
deployment may attach session-level or server-wide policies that DO gate ordinary
pushes; those are not Holly's and nothing here asserts either way.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_holly.py -> repo root is 3 parents up.
_HOLLY_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "holly"


@pytest.fixture(scope="module")
def holly_spec() -> Iterator[AgentSpec]:
    """
    Load and validate the holly bundle once for the module.

    The spec interpolates ``${GITHUB_TOKEN}`` into the github MCP
    ``Authorization`` header, and an unresolved variable is a hard parse error —
    deliberately, so a missing token fails loudly instead of starting degraded.
    A dummy value is supplied rather than relaxing the spec; nothing here
    contacts github.

    :returns: The loaded :class:`AgentSpec` for ``examples/holly``.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GITHUB_TOKEN", "ghp_dummy_token_for_spec_parse")
        yield load(_HOLLY_BUNDLE)


def test_identity_roster_and_skills(holly_spec: AgentSpec) -> None:
    """
    The bundle parses and ships exactly three workers and three skills.

    What this pins is the ROSTER, not a vendor count. Cross-vendor review needs
    TWO distinct effective vendors, and three workers do not supply three
    vendors: ``pi``'s effective vendor is whatever model it is dispatched with,
    so a third-vendor tiebreak is conditional rather than standing. Dropping a
    worker does not by itself make different-vendor review unsatisfiable, and
    nothing here asserts that it would.

    The names are what the bundle is written against. The root prompt and all
    three skills dispatch by these exact strings — ``claude_code`` and ``codex``
    as the implementers, ``pi`` as the read-mostly multi-model worker — and the
    roster preflight probes exactly these three binaries. A removed worker
    leaves prose dispatching to something absent; an added one is reachable
    while no routing rule, preflight or skill mentions it. Set equality on both
    lists, not membership, so an addition fails as loudly as a removal.
    """
    assert holly_spec.name == "holly"
    assert sorted(a.name for a in holly_spec.sub_agents) == ["claude_code", "codex", "pi"]
    # tools.agents is the dispatch surface; it must match what is on disk.
    assert sorted(holly_spec.tools.agents) == ["claude_code", "codex", "pi"]
    assert sorted(s.name for s in holly_spec.skills) == [
        "cross-review",
        "fanout",
        "investigate",
    ]


def test_executor_shape_keeps_vendors_distinct(holly_spec: AgentSpec) -> None:
    """
    The orchestrator and each worker keep their harness.

    Vendor independence is a property of the executor, not of the worker's name.
    Two of the three harnesses have a FIXED vendor — ``claude_code`` and
    ``codex``; ``pi`` runs whatever model its dispatch names, so it is not a
    standing third vendor. Repointing ``codex`` at the ``pi`` harness leaves
    every name, prompt and routing rule intact while removing one of the two
    fixed vendors, and what remains is one fixed vendor plus a worker whose
    independence has to be established per dispatch. A review routed on the old
    assumption then reaches the same engine that wrote the diff and reports an
    independence nobody checked.

    Models stay unpinned where the contract requires it: the orchestrator brain
    must resolve whatever Claude provider the deployment configured, and ``pi``
    is the multi-model worker whose independence comes from ``args.model``
    chosen per dispatch.
    """
    assert holly_spec.executor.config.get("harness") == "claude-sdk"
    assert holly_spec.executor.model is None
    assert holly_spec.executor.profile is None

    assert {a.name: a.executor.config.get("harness") for a in holly_spec.sub_agents} == {
        "claude_code": "claude-native",
        "codex": "codex-native",
        "pi": "pi",
    }

    pi = next(a for a in holly_spec.sub_agents if a.name == "pi")
    assert pi.executor.model is None
    assert pi.executor.profile is None


def test_root_guardrail_policies_are_exactly_the_three(holly_spec: AgentSpec) -> None:
    """
    The orchestrator carries exactly ``blast_radius``, ``spawn_bounds`` and
    ``headless_subagent_purpose_guard`` — no more, no fewer.

    Set equality, not membership. The bundle's honesty contract is that ORDINARY
    publication is unenforced — only catastrophic push variants are denied; a
    fourth policy would change what the runtime does while every prose disclaimer
    stays behind, silently false. Equality also catches the ordinary regression
    of a policy being dropped.

    Argument values are pinned in ``tests/spec/test_holly_bundle.py``.
    """
    assert holly_spec.guardrails is not None
    assert {p.name for p in holly_spec.guardrails.policies} == {
        "blast_radius",
        "spawn_bounds",
        "headless_subagent_purpose_guard",
    }


def test_github_mcp_is_read_only(holly_spec: AgentSpec) -> None:
    """
    Exactly one MCP server, ``github``, allowlisted to ``pull_request_read``.

    Equality on the allowlist, because a mutating MCP tool would be invisible to
    the only mechanical protection the bundle has: ``blast_radius`` matches on
    shell command text and ALLOWs every non-shell tool. That asymmetry is why
    the prompt routes every github MUTATION through the shell and only READS
    through MCP. Adding ``merge_pull_request`` here would hand holly a merge
    button that no policy inspects — and holly is forbidden from merging.

    Sub-agents are checked too: a mutating server attached to a worker bypasses
    the same gate, and workers have even less reason to hold one.
    """
    servers = holly_spec.mcp_servers
    assert [s.name for s in servers] == ["github"]
    assert servers[0].tools == ["pull_request_read"]

    for sub in holly_spec.sub_agents:
        assert sub.mcp_servers == [], f"{sub.name} declares an MCP server"
