"""Structural test for the holly coding-orchestrator bundle (examples/holly).

holly is a claude-sdk orchestrator brain that delegates every coding task to
its ``claude_code`` / ``codex`` / ``pi`` workers, runs an independent
different-vendor review against the LOCAL branch diff, and only then sequences
publication. Pure spec-load — no LLM, no network, no credentials beyond a dummy
``GITHUB_TOKEN`` the MCP header interpolation requires.

The bundle's defining property is that publication ordering is **prompt
discipline, not enforcement**. Nothing in the runtime blocks a ``git push``:
``blast_radius`` runs with ``gate_pushes: false`` and inspects shell text only.
A predecessor design asserted that a policy blocked ``git push`` while that
policy was never evaluated — a false claim of enforcement that reads as a
safety property to every human and worker that consumes it.

The tests below are organised by ratified contract term, and each was written
by asking "what is the cheapest edit that violates this term?" rather than by
restating what the bundle already says:

- D1  — no enforcement policy, and no policy ARGUMENT that creates enforcement
- D1a — no false claim of enforcement anywhere in the bundle
- D2  — the reviewer mandate is delimited and complete
- D3  — the review lifecycle branches survive
- D4  — workers commit and stop, carry mandate integrity, and hold no policy layer
- D5  — external-review-bot servicing keeps its honesty semantics
- D6  — directly-authored work is not exempt from review
- the three-worker roster, whose independence rests on executor shape
- the read-only github MCP surface
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


def _worker_configs() -> dict[str, Path]:
    """
    :returns: Worker name -> its ``config.yaml`` path.
    """
    return {name: _HOLLY_BUNDLE / "agents" / name / "config.yaml" for name in _WORKERS}


def _orchestration_files() -> dict[str, str]:
    """
    :returns: Bundle-relative path -> text, for the files that carry
        orchestration prose (root config plus the three skills).
    """
    paths = [
        _HOLLY_BUNDLE / "config.yaml",
        *[_HOLLY_BUNDLE / "skills" / name / "SKILL.md" for name in _SKILLS],
    ]
    return {str(p.relative_to(_HOLLY_BUNDLE)): p.read_text(encoding="utf-8") for p in paths}


def _bundle_files() -> dict[str, str]:
    """
    :returns: Bundle-relative path -> text, for every prose-bearing file: the
        root config, the three worker configs, and the three skills.
    """
    files = _orchestration_files()
    files.update(
        {
            str(p.relative_to(_HOLLY_BUNDLE)): p.read_text(encoding="utf-8")
            for p in _worker_configs().values()
        }
    )
    return files


# ─────────────────────────── claim scanning ───────────────────────────
#
# Both the honesty scan (D1a) and the worker publication scan (D4) ask the same
# question of prose: does this text ASSERT something it must not? A whole-
# sentence negation test cannot answer it — "The policy blocks publication, but
# it does not block local commits" carries a true clause and a false one, and
# skipping the sentence because it contains "not" is the fail-open denylist the
# reviewer mandate itself warns about.
#
# So negation is scoped the way English scopes it: FORWARD from the negation
# word, and only until the next strong clause boundary (";", ":", an em dash, or
# the end of the sentence). A claim is exempt only when it falls INSIDE some
# negation's scope. That keeps "Never edit, push, open/merge a PR, or dispatch"
# passing — one "Never" covers the whole coordinated list — while
# "Push and open a PR; do not merge it" fails on its first two clauses.

_NEGATION = re.compile(r"\b(no|not|never|nothing|none|cannot|without|nor)\b|n't", re.IGNORECASE)

# Strong clause boundaries that end a negation's reach.
_SCOPE_BREAK = re.compile(r"[;:—]")

_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s")


def _segments(text: str) -> list[str]:
    """
    Unwrap hard-wrapped prose into one segment per paragraph or list item.

    Both YAML block scalars and Markdown wrap sentences across lines, so a
    naive per-line scan would sever a negation from the clause it governs.
    Blank lines and list markers start a new segment; everything else is joined.

    :param text: Raw file contents.
    :returns: Single-line segments preserving paragraph and bullet boundaries.
    """
    segments: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if current:
                segments.append(" ".join(current))
                current = []
            continue
        if _BULLET.match(line) and current:
            segments.append(" ".join(current))
            current = []
        current.append(line.strip())
    if current:
        segments.append(" ".join(current))
    return segments


def _sentences(text: str) -> list[str]:
    """
    :param text: Raw file contents.
    :returns: Unwrapped, sentence-split fragments.
    """
    out: list[str] = []
    for segment in _segments(text):
        out.extend(s for s in re.split(r"(?<=[.!?])\s+", segment) if s.strip())
    return out


def _negation_spans(sentence: str) -> list[tuple[int, int]]:
    """
    Locate each negation and the span it governs.

    :param sentence: One unwrapped sentence.
    :returns: ``(start, end)`` spans, each running from a negation word to the
        next strong clause boundary or the end of the sentence.
    """
    spans: list[tuple[int, int]] = []
    for match in _NEGATION.finditer(sentence):
        stop = _SCOPE_BREAK.search(sentence, match.end())
        spans.append((match.start(), stop.start() if stop else len(sentence)))
    return spans


def _unnegated_claims(
    text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> list[tuple[str, str]]:
    """
    Find pattern matches that no negation governs.

    :param text: Raw file contents.
    :param patterns: ``(label, compiled pattern)`` pairs to search for.
    :returns: ``(label, sentence)`` for every match outside every negation span.
    """
    hits: list[tuple[str, str]] = []
    for sentence in _sentences(text):
        spans = _negation_spans(sentence)
        for label, pattern in patterns:
            for match in pattern.finditer(sentence):
                if not any(start <= match.start() < end for start, end in spans):
                    hits.append((label, sentence))
                    break
    return hits


# Affirmative claims that a mechanism prevents publication. Each pattern needs a
# mechanism-ish subject AND a blocking verb AND a publication object within one
# clause, so ordinary talk of policies, or of pushing, does not trip it.
_ENFORCEMENT_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a named mechanism blocks publication",
        re.compile(
            r"\b(polic(?:y|ies)|guardrail|guard|gate|blast_radius|hook|runtime|"
            r"framework|mechanism|enforcement|server|platform)\b[^.;:—\n]{0,80}"
            r"\b(block|blocks|blocking|prevent|prevents|deny|denies|refuse|refuses|"
            r"stop|stops|reject|rejects|gate|gates|forbid|forbids|require|requires)\b"
            r"[^.;:—\n]{0,60}"
            r"\b(push|pushes|pushing|publication|publish|publishing|PR|pull request|merge)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "publication is described as mechanically blocked",
        re.compile(
            r"\b(push|pushes|pushing|publication|the PR|pull request)\b[^.;:—\n]{0,60}"
            r"\b(is|are|gets|get|will be|would be|cannot be|can't be)\b\s+"
            r"\b(blocked|gated|denied|prevented|refused|rejected|stopped)\b",
            re.IGNORECASE,
        ),
    ),
)

# Literal tokens that can only appear as a fabricated enforcement claim. Unlike
# the patterns above these take no negation exemption — the string itself names
# a mechanism that does not exist, so any occurrence is a defect.
#
# ``require_pr_review`` — a predecessor design named exactly this policy as the
#   thing blocking ``git push``. No such policy exists in
#   ``omnigent.inner.nessie.policies``; it was never evaluated. Its return, in
#   any file, is the original defect coming back verbatim.
# ``review-passed`` / ``review_passed`` — a machine-checked "review passed" flag
#   or gate state that publication is conditioned on. No such state is recorded
#   or read anywhere; review completion lives only in holly's own sequencing, so
#   naming a flag implies an interlock that is not there.
_BANNED_TOKENS: tuple[tuple[str, str], ...] = (
    ("require_pr_review", "names a policy that does not exist and is never evaluated"),
    ("review-passed", "implies a machine-checked gate state that is never recorded"),
    ("review_passed", "implies a machine-checked gate state that is never recorded"),
)

# Instructions to publish. Deliberately narrow: "publish" alone is excluded
# because workers are legitimately told the orchestrator will tell them when to
# publish, and that sentence is not itself an instruction to push.
_PUBLICATION_ACTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instructs the worker to publish",
        re.compile(
            r"\b(push|pushes|pushing|open(?:s|ing)? (?:a|the|its) (?:PR|pull request)|"
            r"gh pr create|git push)\b",
            re.IGNORECASE,
        ),
    ),
)


def test_spec_identity_roster_and_skills(holly_spec: AgentSpec) -> None:
    """
    The bundle parses and ships exactly three workers and three skills.

    Three vendors is the minimum that keeps different-vendor review always
    satisfiable and still leaves a tiebreak vendor. Dropping one collapses
    cross-vendor review; adding an undeclared one escapes the routing rules the
    prompt and skills are written against. Set equality on both, not membership,
    so an addition fails as loudly as a removal.
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
    The orchestrator and each worker keep their harness, and the three worker
    harnesses stay distinct.

    Vendor independence is a property of the executor, not of the worker's name.
    Repointing ``codex`` at the ``pi`` harness leaves every name, prompt and
    routing rule in the bundle intact while silently collapsing two of the three
    vendors — cross-vendor review then dispatches to the same engine that wrote
    the diff and reports independence it does not have.

    Models stay unpinned where the contract requires it. The orchestrator brain
    must resolve whatever Claude provider the deployment configured, so a pin
    would re-couple the bundle to one provider. ``pi`` must stay unpinned
    because it is the multi-model worker: its independence comes from
    ``args.model`` chosen per dispatch, and a pinned model would silently fix
    its effective vendor for every review it is ever given.
    """
    assert holly_spec.executor.config.get("harness") == "claude-sdk"
    assert holly_spec.executor.model is None
    assert holly_spec.executor.profile is None

    harnesses = {a.name: a.executor.config.get("harness") for a in holly_spec.sub_agents}
    assert harnesses == {
        "claude_code": "claude-native",
        "codex": "codex-native",
        "pi": "pi",
    }
    # Three distinct engines -> any author's diff has a different-vendor reviewer.
    assert len(set(harnesses.values())) == 3

    pi = next(a for a in holly_spec.sub_agents if a.name == "pi")
    assert pi.executor.model is None
    assert pi.executor.profile is None


def test_skills_are_not_user_invocable(holly_spec: AgentSpec) -> None:
    """
    All three skills are orchestrator-only (``user-invocable: false``).

    These are playbooks written in holly's voice — they dispatch sessions,
    sequence publication, and route fixes. Exposing one as a user-invocable
    slash command would offer a human (or a worker whose skill list includes it)
    a workflow whose verbs only the orchestrator can perform.
    """
    for skill in holly_spec.skills:
        assert skill.user_invocable is False, skill.name


def test_reviewer_mandate_delimiters_are_intact(holly_spec: AgentSpec) -> None:
    """
    D2: the mandate carries both delimiters, END after BEGIN, once each.

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
    D2: the block BETWEEN the delimiters carries all four WIDE axes and the
    checklist, each axis bound to the same number in both places.

    Only the delimited block travels in a dispatch, so an axis that drifts out
    of it — into the surrounding orchestration prose — is an axis no reviewer
    ever receives. Each axis is a distinct blind spot when missing: blast-radius
    (unchanged consumers of a changed contract), sibling-class (the same defect
    at parallel sites), input-domain (unhandled or mis-ordered input shapes),
    coupled-artifact (docs/spec that must move in lockstep).

    Checking one place is not enough. An axis whose definition is gutted while
    its checklist line survives yields a reviewer that dutifully reports
    "[WIDE-3] run — clear" on a pass it was never told how to run. The
    battery-completeness checklist is what makes any omission detectable at all:
    holly rejects a report that does not open with it, so without it a bare
    "looks good" reads as a completed review.
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
    assert "battery-completeness" in mandate.lower()
    assert "[FOCUSED]" in mandate


def test_root_guardrail_policies_are_exactly_the_three(holly_spec: AgentSpec) -> None:
    """
    D1: the orchestrator carries exactly ``blast_radius``, ``spawn_bounds`` and
    ``headless_subagent_purpose_guard`` — no more, no fewer.

    Set equality, not membership, is the whole point. The bundle's honesty
    contract is that publication is unenforced; a fourth policy would change
    what the runtime does while every prose disclaimer about "nothing
    mechanically blocks a push" stays behind, silently false. Equality also
    still catches the ordinary regression of a policy being dropped (unbounded
    fan-out, or an unlabelled headless dispatch).
    """
    assert holly_spec.guardrails is not None
    assert {p.name for p in holly_spec.guardrails.policies} == {
        "blast_radius",
        "spawn_bounds",
        "headless_subagent_purpose_guard",
    }


def test_root_policy_arguments_create_no_enforcement(holly_spec: AgentSpec) -> None:
    """
    D1: the policies' ARGUMENTS are pinned, not just their names.

    A policy set can satisfy name equality and still enforce. Flipping
    ``gate_pushes`` to ``true`` is a one-word edit that leaves every name in
    place, turns ``blast_radius`` into an ASK gate on push, and falsifies every
    "nothing blocks a push" disclaimer in the bundle at once — the exact
    divergence between claimed and actual behaviour this suite exists to
    prevent, arriving from the other direction.

    ``spawn_bounds`` and the purpose guard are pinned for the opposite reason:
    they are the two policies that legitimately DO enforce, and their arguments
    are what makes them do so. An emptied ``dispatch_tools`` leaves a named
    policy that counts nothing, and a widened ``allowed_purposes`` lets an
    unlabelled dispatch through the guard that exists to label it.
    """
    policies = {p.name: p for p in holly_spec.guardrails.policies}

    blast_radius_args = policies["blast_radius"].function.arguments
    assert blast_radius_args.get("gate_pushes") is False, (
        "blast_radius must not gate pushes: the bundle tells every reader that "
        "nothing mechanically blocks a push, and enabling the gate makes that "
        "claim false everywhere it appears."
    )

    spawn_args = policies["spawn_bounds"].function.arguments
    assert isinstance(spawn_args.get("max_dispatches_per_turn"), int)
    assert spawn_args["max_dispatches_per_turn"] > 0
    # Both dispatch surfaces are counted: with spawn: true, an uncounted
    # sys_session_create would bypass the per-turn fan-out cap entirely.
    assert set(spawn_args.get("dispatch_tools") or []) == {
        "sys_session_send",
        "sys_session_create",
    }

    purpose_args = policies["headless_subagent_purpose_guard"].function.arguments
    assert set(purpose_args.get("allowed_purposes") or []) == {
        "implement",
        "review",
        "explore",
        "search",
    }

    # Every function policy must pass at least one argument: the resolver uses
    # the factory itself as the evaluator when arguments are empty, and the
    # first gated tool call then fails closed.
    for policy in holly_spec.guardrails.policies:
        assert policy.function.arguments, policy.name


def test_no_sub_agent_declares_a_policy_layer(holly_spec: AgentSpec) -> None:
    """
    D4: no worker declares ``guardrails.policies``.

    The absence is ratified, not incidental. A sub-agent's own policy block is
    not evaluated for its tool calls on the server deploy path — the server
    loads the ROOT bundle spec and ignores the sub-agent name — so a block here
    reads as protection while enforcing nothing. That is a false claim of
    enforcement expressed in YAML instead of prose, and it is exactly what was
    removed from these three files. Re-adding a syntactically valid block is
    cheap, silent, and looks like a hardening commit.

    ``guardrails`` itself may remain (``pi`` sets ``ask_timeout`` there, which
    is a real runner knob); only the policy list must stay empty.
    """
    for sub in holly_spec.sub_agents:
        policies = list(sub.guardrails.policies or []) if sub.guardrails else []
        assert policies == [], (
            f"{sub.name} declares {[p.name for p in policies]}; a sub-agent policy "
            f"block is not evaluated on the server deploy path, so it claims "
            f"protection it does not provide."
        )


def test_no_file_claims_a_gate_blocks_publication() -> None:
    """
    D1a: no bundle file claims a policy or gate blocks publication.

    A false claim of enforcement is worse than no claim — a human reads it as a
    safety property and stops sequencing carefully, and a worker reads it as
    permission to attempt a push it believes something will catch. Nothing
    catches it: ``blast_radius`` runs with ``gate_pushes: false`` and inspects
    shell command text only.

    RESIDUAL ESCAPES, stated plainly, because natural-language claim detection
    is not decidable and this scan is a denylist — the mandate's own words, a
    denylist is fail-open:

    1. Negation scope is a heuristic. A negation governs forward to the next
       ";", ":", em dash or sentence end, so a false claim placed INSIDE that
       span is exempt: "we do not gate anything, the policy blocks the push"
       passes. Moving the claim past a boundary ("...; the policy blocks the
       push") is caught.
    2. Cross-sentence claims are missed. "There is a gate. It stops every push."
       has no mechanism word in the asserting sentence; pronoun reference is not
       resolved.
    3. Novel vocabulary is missed. A claim built from words outside the subject,
       verb and object lists — "the runner will refuse to let the branch leave"
       — matches nothing here.

    What makes those escapes tolerable is that the claim scan is not the only
    guard. Real enforcement cannot be added by prose alone: it needs a policy or
    a policy argument, and those are pinned by exact comparison in
    ``test_root_guardrail_policies_are_exactly_the_three`` and
    ``test_root_policy_arguments_create_no_enforcement``. This test narrows the
    prose gap; those two make the underlying property decidable.
    """
    for rel_path, text in _bundle_files().items():
        lowered = text.lower()
        for token, why in _BANNED_TOKENS:
            assert token not in lowered, f"{rel_path}: banned token {token!r} — {why}"

        violations = _unnegated_claims(text, _ENFORCEMENT_CLAIMS)
        assert not violations, (
            f"{rel_path}: {violations[0][0]} — {violations[0][1].strip()!r}. Nothing "
            f"in this bundle blocks a push (blast_radius runs gate_pushes: false "
            f"and inspects shell text only); publication ordering is holly's "
            f"sequencing, and claiming otherwise is the defect this test exists for."
        )


def test_workers_commit_and_stop(holly_spec: AgentSpec) -> None:
    """
    D4: each worker is told to commit and stop, never to publish on its own.

    Publication ordering — gates, then an independent different-vendor review of
    the local diff, then push — holds only because holly sequences it. A worker
    that pushes or opens its own PR steps outside that sequence entirely, and
    the review it skipped can never be re-inserted, because the code is already
    on the remote.

    Asserted on the parsed ``instructions`` rather than raw YAML so the file's
    explanatory comments are not mistaken for instructions to the model. Every
    publication act must fall inside a prohibition's scope, and the publishing
    commands themselves must be absent — a real instruction to publish would
    name one. The scope rule is clause-level: a bullet reading "Push and open a
    PR; do not merge it" fails on its first two clauses even though the sentence
    contains "not".
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

        violations = _unnegated_claims(instructions, _PUBLICATION_ACTS)
        assert not violations, (
            f"{name}: publication act outside any prohibition — "
            f"{violations[0][1].strip()!r}. Workers commit and stop; holly "
            f"releases the branch after gates and an independent review."
        )


def test_claim_scanners_catch_known_evasions() -> None:
    """
    The scanners themselves are pinned against the evasions that got through.

    Both scans previously skipped any sentence containing a negation anywhere,
    which let a true clause launder a false one in the same sentence. These
    fixtures are the regression: the positives must be caught, and the negatives
    are the bundle's own truthful disclaimers and prohibitions, which must keep
    passing. Without this test a future "simplification" of the scope rule back
    to whole-sentence skipping would leave every other test still green.
    """
    caught = ("The policy blocks publication, but it does not block local commits.",)
    for text in caught:
        assert _unnegated_claims(text, _ENFORCEMENT_CLAIMS), text

    allowed = (
        "Nothing mechanically blocks a push.",
        "There is no policy gate; the ordering holds only because you sequence it.",
        "There is no mechanism enforcing this; it holds only because you check HEAD.",
        "A block here would read as protection while enforcing nothing.",
    )
    for text in allowed:
        assert not _unnegated_claims(text, _ENFORCEMENT_CLAIMS), text

    publishes = ("Push and open a PR; do not merge it.", "When green, push your branch.")
    for text in publishes:
        assert _unnegated_claims(text, _PUBLICATION_ACTS), text

    prohibited = (
        "Do NOT push and do NOT open a PR.",
        "Never edit, push, open/merge a PR, or dispatch.",
        "It does not push and does not open a PR.",
    )
    for text in prohibited:
        assert not _unnegated_claims(text, _PUBLICATION_ACTS), text


# ───────────────────── review-lifecycle branches (D3/D5/D6) ─────────────────
#
# Each branch is anchored by a set of patterns that must all match within ONE
# paragraph or list item of the file that OWNS the branch. Two scoping choices
# do the work here:
#
# Per block, not per file — scattering the words across a document would
# otherwise satisfy a whole-file search, and deleting a step would go unnoticed
# because its vocabulary survives elsewhere.
#
# Per owning file, because the orchestration prose deliberately restates
# several of these rules in more than one place. The lifecycle playbook is
# cross-review; the direct-authoring carve-out is the root prompt. A branch
# restated in the root prompt is good redundancy, but it is not the procedure,
# and deleting the procedural step must fail even while the restatement lives.
#
# Patterns name the branch's SEMANTICS — trigger and consequence — with
# alternatives for the wording, and match case-insensitively, so rephrasing a
# step is free and removing it is not.
_CROSS_REVIEW = "skills/cross-review/SKILL.md"
_ROOT_CONFIG = "config.yaml"

_LIFECYCLE_ANCHORS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "gates-before-reviewer",
        _CROSS_REVIEW,
        "reviewer tokens get spent on defects a validator already names",
        (r"\bgates?\b", r"\bbefore\b", r"review", r"(RED|green|lint|typecheck|deterministic)"),
    ),
    (
        "effective-vendor-independence",
        _CROSS_REVIEW,
        "a pi reviewer on the author's own model reports independence it lacks",
        (r"vendor", r"\bmodels?\b", r"(differ|different)", r"(stop|escalate|cannot|do not)"),
    ),
    (
        "report-validation",
        _CROSS_REVIEW,
        "an incomplete report, or a verdict issued despite a refusal, reads as a pass",
        (r"INCOMPLETE DISPATCH", r"re-?dispatch", r"(identical|full)"),
    ),
    (
        "same-fixer-same-branch",
        _CROSS_REVIEW,
        "a new title spawns a fresh worker with no memory of the task",
        (
            r"\bsame\b",
            r"(fixer|implementer)",
            r"(branch|worktree)",
            r"(title|session_id|conversation)",
        ),
    ),
    (
        "class-closure-recurrence-stop",
        _CROSS_REVIEW,
        "point-fixing continues while the same defect class recurs at new sites",
        (
            r"\bclass\b",
            r"\bstop\b",
            r"(one-off|enumerat)",
            r"(zero remaining|every instance|whole-repo|whole-surface)",
        ),
    ),
    (
        "post-review-edit-invalidates-verdict",
        _CROSS_REVIEW,
        "a 'tiny' follow-up ships on a verdict that never saw it",
        (r"(invalidat|authoris|authoriz|verdict)", r"\bafter\b", r"(HEAD|again|re-?run|gates)"),
    ),
    (
        "release-requires-green-and-zero-blocking",
        _CROSS_REVIEW,
        "the PR opens on red gates or on unresolved blocking findings",
        (r"green", r"(zero blocking|no blocking)", r"(push|PR)"),
    ),
    (
        "holly-never-merges",
        _CROSS_REVIEW,
        "the orchestrator takes the merge decision that belongs to the human",
        (r"merge", r"\b(not|never)\b", r"human"),
    ),
    (
        "reviewer-unavailable-hard-stop",
        _CROSS_REVIEW,
        "with no different-vendor reviewer the run silently proceeds unreviewed",
        (
            r"(no different-vendor|cannot run independent|reviewer is available|"
            r"reviewer available)",
            r"\bstop\b",
            r"(human|escalate)",
            r"(unreviewed|do not open|not open the PR)",
        ),
    ),
    (
        "never-degrade-to-same-vendor",
        _CROSS_REVIEW,
        "review quietly downgrades to same-vendor or skipped instead of stopping",
        (r"never", r"(same-vendor|same vendor)", r"(skip|degrade)"),
    ),
    (
        "direct-authoring-not-exempt",
        _ROOT_CONFIG,
        "prose holly wrote itself reaches the remote without an independent review",
        (
            r"(authored directly|directly[- ]author|you author)",
            r"review",
            r"(exemption|exactly like|still owes|before)",
        ),
    ),
    (
        "bot-findings-never-narrow-the-mandate",
        _CROSS_REVIEW,
        "a bot's comment list becomes the review scope and the battery shrinks",
        (r"bot", r"(narrow|confirm these)", r"mandate"),
    ),
    (
        "unresolved-thread-count-on-handoff",
        _CROSS_REVIEW,
        "the human merges believing replies established resolution",
        (r"unresolved", r"thread", r"(count|report)"),
    ),
    (
        "never-declares-a-clean-bill",
        _CROSS_REVIEW,
        "the orchestrator issues the sign-off that is the human's to give",
        (r"clean bill", r"never"),
    ),
)


def test_review_lifecycle_branches_survive() -> None:
    """
    D3/D5/D6: every branch of the review lifecycle is still present.

    The mandate's structure is pinned elsewhere; this pins the procedure that
    dispatches it. These branches are the ones whose deletion is invisible —
    the bundle still parses, every other test stays green, and the failure only
    shows up as a run that quietly did the wrong thing months later: a review
    dispatched to the author's own vendor, a PR opened with no reviewer
    available at all, a "tiny" post-review edit riding a stale verdict, a bot's
    comment list silently replacing the full battery, or a hand-off that reports
    findings serviced while threads stay unresolved and the human merges on the
    difference.
    """
    texts = _orchestration_files()
    for branch, owner, consequence, patterns in _LIFECYCLE_ANCHORS:
        blocks = _segments(texts[owner])
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        assert any(all(c.search(block) for c in compiled) for block in blocks), (
            f"review-lifecycle branch {branch!r} is gone from {owner}. Without it: {consequence}."
        )


def test_github_mcp_is_read_only(holly_spec: AgentSpec) -> None:
    """
    Exactly one MCP server, ``github``, allowlisted to ``pull_request_read``.

    Equality on the allowlist, because a mutating MCP tool would be invisible to
    the only mechanical protection the bundle has: ``blast_radius`` matches on
    shell command text (``sys_os_shell`` and friends) and ALLOWs every non-shell
    tool. That asymmetry is exactly why the prompt routes every github MUTATION
    through the shell and only READS through MCP. Adding, say,
    ``merge_pull_request`` here would hand holly a merge button that no policy
    inspects — and holly is explicitly forbidden from merging.

    Sub-agents are checked too: a mutating server attached to a worker bypasses
    the same gate, and workers have even less reason to hold one.
    """
    servers = holly_spec.mcp_servers
    assert [s.name for s in servers] == ["github"]
    assert servers[0].tools == ["pull_request_read"]

    for sub in holly_spec.sub_agents:
        assert sub.mcp_servers == [], f"{sub.name} declares an MCP server"


def test_function_policies_are_resolvable(holly_spec: AgentSpec) -> None:
    """
    Every function policy in the bundle points at a real factory path.

    A typo in ``function.path`` is a silent downgrade rather than a loud error
    at spec-load time, so the count and the module prefix are pinned here.

    :raises AssertionError: When a policy names an unexpected factory module.
    """
    specs: list[Any] = [holly_spec, *holly_spec.sub_agents]
    paths = [
        p.function.path
        for spec in specs
        if spec.guardrails
        for p in spec.guardrails.policies or []
        if getattr(p, "function", None)
    ]
    assert len(paths) == 3, f"expected 3 function policies in the bundle, found {len(paths)}"
    for path in paths:
        assert path.startswith("omnigent.inner.nessie.policies."), path
