"""Structural test for the holly coding-orchestrator bundle (examples/holly).

holly is the three-worker sibling of polly: a claude-sdk orchestrator brain
that delegates every coding task to ``claude_code`` / ``codex`` / ``pi``, runs
an independent different-vendor review on the LOCAL branch diff, and only then
sequences publication. Pure spec-load — no LLM, no network, no credentials
beyond a dummy ``GITHUB_TOKEN`` the MCP header interpolation requires.

The bundle's defining property is that publication ordering is **prompt
discipline, not enforcement**. Nothing in the runtime blocks a ``git push``:
``blast_radius`` runs with ``gate_pushes: false`` and inspects shell text only.
The predecessor design asserted that a policy blocked ``git push`` while that
policy was never evaluated — a false claim of enforcement that reads as a
safety property to every human and worker that consumes it. Several tests here
exist specifically to keep that claim from coming back.

What breaks if this fails:
- the roster or the skill spine drifts (a worker or workflow silently drops),
- the reviewer mandate loses a delimiter or an axis (workers are told to refuse
  a mandate missing its terminator, and a dropped axis is a permanent blind
  spot),
- a file starts claiming a gate blocks publication when none does,
- a worker is told to push or open its own PR (publication escapes holly's
  sequencing),
- the github MCP gains a mutating tool (``blast_radius`` sees shell text only,
  so an MCP write is outside the one mechanical protection the bundle has).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_holly.py -> repo root is 3 parents up.
_HOLLY_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "holly"

_WORKERS = ("claude_code", "codex", "pi")
_SKILLS = ("cross-review", "fanout", "investigate")

_MANDATE_BEGIN = "BEGIN REVIEWER-MANDATE-V1"
_MANDATE_END = "END REVIEWER-MANDATE-V1"


@pytest.fixture(scope="module")
def holly_spec() -> Iterator[AgentSpec]:
    """
    Load and validate the holly bundle once for the module.

    The spec interpolates ``${GITHUB_TOKEN}`` into the github MCP
    ``Authorization`` header, and an unresolved variable is a hard parse error
    (``Unresolved environment variable '${GITHUB_TOKEN}'``) — deliberately, so a
    missing token fails loudly instead of starting degraded. The test supplies a
    dummy value rather than relaxing the spec; nothing here contacts github.

    :returns: The loaded :class:`AgentSpec` for ``examples/holly``.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GITHUB_TOKEN", "ghp_dummy_token_for_spec_parse")
        yield load(_HOLLY_BUNDLE)


def _bundle_files() -> dict[str, str]:
    """
    Read every prose-bearing file in the bundle as raw text.

    :returns: Mapping of bundle-relative path to file contents — the root
        config, the three worker configs, and the three skills.
    """
    paths = [
        _HOLLY_BUNDLE / "config.yaml",
        *[_HOLLY_BUNDLE / "agents" / name / "config.yaml" for name in _WORKERS],
        *[_HOLLY_BUNDLE / "skills" / name / "SKILL.md" for name in _SKILLS],
    ]
    return {str(p.relative_to(_HOLLY_BUNDLE)): p.read_text(encoding="utf-8") for p in paths}


# A sentence carrying any of these is denying or disclaiming, not asserting.
# The honesty scan skips such sentences so the bundle's own truthful
# disclaimers ("Nothing mechanically blocks a push", "not an enforced gate")
# don't read as the very claim they refute.
_NEGATION = re.compile(r"\b(no|not|never|nothing|none|cannot|without)\b|n't", re.IGNORECASE)

# Affirmative claims that a mechanism prevents publication. Each pattern needs
# a policy-ish subject AND a blocking verb AND a publication object in the same
# sentence, so ordinary talk of policies or of pushing does not trip it.
_ENFORCEMENT_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a named mechanism blocks publication",
        re.compile(
            r"\b(polic(?:y|ies)|guardrail|guard|gate|blast_radius|hook|runtime|"
            r"framework|mechanism)\b[^.\n]{0,80}"
            r"\b(block|blocks|prevent|prevents|deny|denies|refuse|refuses|stop|stops|"
            r"reject|rejects|gate|gates)\b[^.\n]{0,60}"
            r"\b(push|pushes|pushing|publication|publish|PR|pull request|merge)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "publication is described as mechanically blocked",
        re.compile(
            r"\b(push|pushes|pushing|publication|the PR|pull request)\b[^.\n]{0,60}"
            r"\b(is|are|gets|get|will be|would be)\b\s+"
            r"\b(blocked|gated|denied|prevented|refused|rejected)\b",
            re.IGNORECASE,
        ),
    ),
)

# Literal tokens that can only appear as a fabricated enforcement claim. Unlike
# the patterns above these take no negation escape — the string itself names a
# mechanism that does not exist, so any occurrence is a defect.
#
# ``require_pr_review`` — the predecessor design named exactly this policy as
#   the thing blocking ``git push``. No such policy exists in
#   ``omnigent.inner.nessie.policies``; it was never evaluated. Its return, in
#   any file, is the original defect coming back verbatim.
# ``review-passed`` / ``review_passed`` — a machine-checked "review passed"
#   flag or gate state that publication is conditioned on. No such state is
#   recorded or read anywhere; review completion lives only in holly's own
#   sequencing, so naming a flag implies an interlock that isn't there.
_BANNED_TOKENS: tuple[tuple[str, str], ...] = (
    ("require_pr_review", "names a policy that does not exist and is never evaluated"),
    ("review-passed", "implies a machine-checked gate state that is never recorded"),
    ("review_passed", "implies a machine-checked gate state that is never recorded"),
)


def _sentences(text: str) -> list[str]:
    """
    Split prose into rough sentences for claim-level scanning.

    Newlines are boundaries too, so a Markdown bullet or a YAML comment line is
    scanned on its own rather than bleeding into its neighbour.

    :param text: Raw file contents.
    :returns: Non-empty sentence-ish fragments.
    """
    return [s for s in re.split(r"(?<=[.!?:])\s+|\n", text) if s.strip()]


def test_spec_identity_roster_and_skills(holly_spec: AgentSpec) -> None:
    """
    The bundle parses and ships exactly three workers and three skills.

    Three vendors is the minimum that keeps different-vendor review always
    satisfiable and still leaves a tiebreak vendor. Dropping one collapses
    cross-vendor review; adding an undeclared one escapes the routing rules the
    prompt and skills are written against. Set equality on both, not
    membership, so an addition fails as loudly as a removal.
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


def test_skills_are_not_user_invocable(holly_spec: AgentSpec) -> None:
    """
    All three skills are orchestrator-only (``user-invocable: false``).

    These are playbooks written in holly's voice — they dispatch sessions,
    sequence publication, and route fixes. Exposing one as a user-invocable
    slash command would offer a human (or a worker whose skill list includes
    it) a workflow whose verbs only the orchestrator can perform.
    """
    for skill in holly_spec.skills:
        assert skill.user_invocable is False, skill.name


def test_reviewer_mandate_delimiters_are_intact(holly_spec: AgentSpec) -> None:
    """
    The cross-review mandate carries both delimiters, END after BEGIN, once each.

    The delimiters are load-bearing rather than decorative: every worker prompt
    instructs the reviewer to refuse a dispatch whose mandate lacks its END
    marker, because a missing terminator is how truncation is detected. If the
    END marker is lost from the source block, every dispatch holly pastes is
    permanently un-terminated and every reviewer is obliged to refuse it. A
    duplicated marker is equally bad — the extractable block becomes ambiguous.
    """
    body = next(s for s in holly_spec.skills if s.name == "cross-review").content
    assert body.count(_MANDATE_BEGIN) == 1
    assert body.count(_MANDATE_END) == 1
    assert body.index(_MANDATE_END) > body.index(_MANDATE_BEGIN)


def test_reviewer_mandate_names_all_wide_axes_and_checklist(holly_spec: AgentSpec) -> None:
    """
    The block BETWEEN the delimiters carries all four WIDE axes and the checklist.

    Only the delimited block travels in a dispatch, so an axis that drifts out
    of it — into the surrounding orchestration prose — is an axis no reviewer
    ever receives. Each axis is a distinct blind spot when missing: blast-radius
    (unchanged consumers of a changed contract), sibling-class (the same defect
    at parallel sites), input-domain (unhandled or mis-ordered input shapes),
    coupled-artifact (docs/spec that must move in lockstep). The
    battery-completeness checklist is what makes an omission detectable at all:
    holly rejects a report that does not open with it, so without it a bare
    "looks good" reads as a completed review.

    Each axis is pinned in BOTH places under the SAME number: the numbered
    definition that tells the reviewer what to do, and the checklist line it
    must report against. Checking only one is not enough — an axis whose
    definition is gutted while its checklist line survives yields a reviewer
    that dutifully reports "[WIDE-3] run — clear" on a pass it was never told
    how to run.
    """
    body = next(s for s in holly_spec.skills if s.name == "cross-review").content
    mandate = body[body.index(_MANDATE_BEGIN) : body.index(_MANDATE_END)]

    axes = ("blast-radius", "sibling-class", "input-domain", "coupled-artifact")
    for number, axis in enumerate(axes, start=1):
        assert re.search(rf"^\s*{number}\.\s*{axis}\b", mandate, re.IGNORECASE | re.MULTILINE), (
            f"WIDE axis {number} ({axis}) is not defined in the mandate"
        )
        assert re.search(rf"\[WIDE-{number}\][^\n]*\b{axis}\b", mandate, re.IGNORECASE), (
            f"the report checklist does not list [WIDE-{number}] as {axis}"
        )
    # The report-format checklist the reviewer must open with, plus the
    # contract-vs-diff pass that every review runs regardless of axes.
    assert "battery-completeness" in mandate.lower()
    assert "[FOCUSED]" in mandate


def test_root_guardrail_policies_are_exactly_the_three(holly_spec: AgentSpec) -> None:
    """
    The orchestrator carries exactly ``blast_radius``, ``spawn_bounds`` and
    ``headless_subagent_purpose_guard`` — no more, no fewer.

    Set equality, not membership, is the whole point. The bundle's honesty
    contract is that publication is unenforced; a fourth policy appearing here
    would change what the runtime actually does while every prose disclaimer
    about "nothing mechanically blocks a push" stays behind, silently false.
    Equality also still catches the ordinary regression of a policy being
    dropped (unbounded fan-out, or an unlabelled headless dispatch).
    """
    assert holly_spec.guardrails is not None
    assert {p.name for p in holly_spec.guardrails.policies} == {
        "blast_radius",
        "spawn_bounds",
        "headless_subagent_purpose_guard",
    }


def test_no_file_claims_a_gate_blocks_publication() -> None:
    """
    No bundle file claims a policy or gate blocks publication.

    This is the honesty term, and the defect it guards is concrete: the
    predecessor design asserted that a policy blocked ``git push`` while that
    policy was never evaluated. A false claim of enforcement is worse than no
    claim — a human reads it as a safety property and stops sequencing
    carefully, and a worker reads it as permission to try a push it believes
    something will catch. Nothing catches it: ``blast_radius`` runs with
    ``gate_pushes: false`` and inspects shell command text only.

    Two layers. Banned literal tokens name mechanisms that do not exist, so any
    occurrence is a defect regardless of surrounding words. The claim patterns
    then scan sentence-by-sentence and skip any sentence carrying a negation,
    so the bundle's truthful disclaimers ("Nothing mechanically blocks a push",
    "not an enforced gate") pass while an affirmative "the policy blocks the
    push" fails. That skip is a deliberate trade: a false claim smuggled into a
    sentence that also happens to contain an unrelated "not" would slip through,
    but the alternative — no negation guard — would make honest disclaiming
    impossible and push authors toward saying nothing at all.
    """
    for rel_path, text in _bundle_files().items():
        lowered = text.lower()
        for token, why in _BANNED_TOKENS:
            assert token not in lowered, f"{rel_path}: banned token {token!r} — {why}"

        for sentence in _sentences(text):
            if _NEGATION.search(sentence):
                continue  # a denial, not an assertion
            for label, pattern in _ENFORCEMENT_CLAIMS:
                assert not pattern.search(sentence), (
                    f"{rel_path}: {label} — {sentence.strip()!r}. Nothing in this "
                    f"bundle blocks a push (blast_radius runs gate_pushes: false "
                    f"and inspects shell text only); publication ordering is "
                    f"holly's sequencing, and claiming otherwise is the exact "
                    f"defect this test exists for."
                )


def test_workers_commit_and_stop(holly_spec: AgentSpec) -> None:
    """
    Each worker is told to commit and stop, never to publish on its own.

    Publication ordering — gates, then an independent different-vendor review of
    the local diff, then push — holds only because holly sequences it. A worker
    that pushes or opens its own PR steps outside that sequence entirely and the
    review it skipped can never be re-inserted, because the code is already on
    the remote.

    Asserted on the parsed ``instructions`` rather than the raw YAML so the
    file's explanatory comments (which discuss ``gate_pushes`` and force-push)
    are not mistaken for instructions to the model. Every sentence that mentions
    publishing must carry a prohibition, and the publishing commands themselves
    must be absent — a real instruction to publish would name one.
    """
    by_name = {a.name: a for a in holly_spec.sub_agents}
    for name in _WORKERS:
        instructions = by_name[name].instructions or ""

        # The mandate-integrity clause: a reviewer must refuse a truncated or
        # axis-missing dispatch under this exact banner instead of guessing a
        # verdict. holly keys its own report validation off the same string.
        assert "INCOMPLETE DISPATCH" in instructions, name

        for command in ("git push", "gh pr create", "gh pr merge"):
            assert command not in instructions, f"{name}: publishes directly via {command!r}"

        publish = re.compile(r"\b(push|pushes|pushing|open(?:s|ing)? a PR|pull request)\b", re.I)
        for sentence in _sentences(instructions):
            if not publish.search(sentence):
                continue
            assert _NEGATION.search(sentence), (
                f"{name}: publication sentence without a prohibition — "
                f"{sentence.strip()!r}. Workers commit and stop; holly releases "
                f"the branch after gates and an independent review."
            )


def test_github_mcp_is_read_only(holly_spec: AgentSpec) -> None:
    """
    Exactly one MCP server, ``github``, allowlisted to ``pull_request_read``.

    Equality on the allowlist, because a mutating MCP tool would be invisible to
    the only mechanical protection the bundle has: ``blast_radius`` matches on
    shell command text (``sys_os_shell`` and friends) and ALLOWs every non-shell
    tool. That asymmetry is exactly why the prompt routes every github MUTATION
    through the shell and only READS through MCP. Adding, say, ``merge_pull
    _request`` to this list would hand holly a merge button that no policy
    inspects — and holly is explicitly forbidden from merging.

    Sub-agents are checked too: a mutating server attached to a worker bypasses
    the same gate, and workers have even less reason to hold one.
    """
    servers = holly_spec.mcp_servers
    assert [s.name for s in servers] == ["github"]
    assert servers[0].tools == ["pull_request_read"]

    for sub in holly_spec.sub_agents:
        assert sub.mcp_servers == [], f"{sub.name} declares an MCP server"
