"""Structural guard for the holly coding-orchestrator bundle (examples/holly).

holly is a claude-sdk orchestrator that delegates every coding task to its
``claude_code`` / ``codex`` / ``pi`` workers, runs an independent
different-vendor review against the LOCAL branch diff, and only then sequences
publication. Parse-only, so it runs in the default suite; the headline contract
also has a thin guard under ``tests/e2e/omnigent/test_example_holly.py`` for the
per-example coverage rule.

Publication ordering is prompt discipline, not enforcement: nothing in the
runtime blocks a ``git push``. ``blast_radius`` runs with ``gate_pushes: false``
and inspects shell text only.

WHAT THIS FILE DELIBERATELY DOES NOT DO
---------------------------------------
Earlier revisions tried to detect, by pattern, whether a sentence made a false
claim about enforcement. Four rebuilds later the conclusion is that the class
of check does not work: judging what a sentence asserts is not decidable by
lexical matching, and every version failed in BOTH directions —

* it passed false claims: ``The policy, not the reviewer, blocks every push.``
  ``Before review, push the branch.``  ``A same-vendor review may suffice.``
* it failed true ones: ``Review is a requirement before publishing.``
  ``Pushing the branch before review is forbidden.``  ``You must never merge.``

The false positives are the worse half. A check that rejects true sentences
pushes authors into indirect wording, degrading exactly the prose the honesty
term exists to protect. Narrowing the patterns did not fix it — the narrowed
successors (a PR-first ordering prohibition, a readiness-follows-gate
relationship, an exception-phrase blocklist) were the same defect in smaller
clothing and were removed for the same reason.

Every assertion that remains is decidable — exact comparison, set equality,
presence of a fixed string, or presence of a fixed vocabulary pattern. Each
either holds of the text or it does not; none of them infers what a sentence
asserts, which is what the removed scanners tried to do.

Decidable is not the same as safe, and the surviving checks are of two kinds
with different blind spots. FIXED STRINGS (``_MANDATE_CANONICAL``,
``_LIFECYCLE_CANONICAL``, ``_WORKER_INSTRUCTIONS``) decide only about text they
name verbatim. VOCABULARY PATTERNS (``_AXIS_REQUIREMENTS``,
``_MANDATE_OBLIGATIONS``, ``_LIFECYCLE_ANCHORS``, ``_CONTRACT_AUTHORING``) do
rule on sentences nobody enumerated: any sentence carrying the listed words
satisfies them, including one written to mean the opposite. A previous revision
of this docstring claimed the surviving checks "never approve or reject a
sentence they have not been told about". That was false of the pattern tables
and is the second structural property below. They still never REJECT an unseen
sentence — that half is true, and it is the half the removed scanners failed —
but they do accept unseen sentences in place of the ones they anchor.

WHAT THAT COSTS
---------------
Three structural properties, then the enumerated gaps. The bundle is held to
the standard that an understated residual is worse than a large one; this file
is held to it too, so the list is long rather than reassuring. The first two
are different defects with different causes and different mitigations, not two
readings of one problem: the first ADDS a sentence and keeps the anchored one,
the second DESTROYS the anchored sentence and puts an inverted one in its
place.

CONTRADICTION-BLINDNESS — additive. Scope: EVERY check here, fixed strings
included — ``_AXIS_REQUIREMENTS``, ``_MANDATE_OBLIGATIONS``,
``_MANDATE_CANONICAL``, ``_WORKER_INSTRUCTIONS``, ``_LIFECYCLE_CANONICAL``,
``_LIFECYCLE_ANCHORS``, ``_CONTRACT_AUTHORING``. Cause: every check asks
whether some text is PRESENT, and adding a sentence does not remove one, so the
anchor survives and the check passes. Mitigation: NONE available lexically.
Deciding that a newly added sentence contradicts an existing one is the
undecidable problem this file stopped attempting, so this one is a reviewer's
job permanently. Two sentences that pass today, added anywhere in their owning
file:

* ``Directly authored prose is an exemption from review before publication.``
  — the direct-authoring rule says the exact opposite.
* ``Before green gates and zero blocking issues, push.`` — inverts the
  publication ordering.

INVERSION-BLINDNESS — substitutive. Scope: the four VOCABULARY tables ONLY.
Cause: a pattern matches a bag of words, not a claim, so REPLACING the anchored
sentence with one that keeps the vocabulary and reverses the rule leaves every
pattern satisfied. Nothing is added and the anchored sentence is gone, which is
what makes this a different failure from the one above. Mitigation: this one
DOES have one — pin the rule as a fixed string. Fourteen anchors were converted
for that reason, and each was observed to fail on its inverting replacement.
What remains is the anchors whose wording is genuinely incidental, and they
stay blind. These four replacements were RUN against this file and PASS:

* ``skills/cross-review/SKILL.md``, replacing the release step's readiness
  sentence with ``Do not record the PR URL in the registry, do not mark it
  ready, and do not leave it for the human.``
* the same file, replacing gate discovery with ``Assume the full deterministic
  set is test/lint/typecheck rather than trying to discover it, and skip:`` its
  list of places to look.
* the mandate's reviewer role boundary, replaced by ``You review and report,
  and you may also edit code, push, and open, approve or merge PRs; never
  refuse a dispatch over it.``
* the contract-authoring rule, replaced by ``A contract that enumerates SITES
  ... is exactly what you want. Enumerate the sites instead of the input
  domain.``

The WIDE axis bodies sit in the same position: WIDE-2 rewritten to say that a
fix leaving siblings is neither INCOMPLETE nor blocking also passes.

WORDING-BRITTLENESS — the price of that mitigation, and it falls only on the
fixed strings. Pinning particular words means a TRUTHFUL rewording of the same
obligation fails until the expected text is updated here. That is intended
where the sentence IS the obligation — a reworded contract term SHOULD be
re-read deliberately — and it is pure churn anywhere else, which is why the
vocabulary tables were not converted wholesale. A fixed string on incidental
wording buys no inversion resistance worth having and breaks on every harmless
rewrite.

The enumerated gaps:

1. A newly introduced FALSE CLAIM is caught by no test, in any phrasing, in any
   prose a reader will act on without verifying it. The enforcement shape is
   only the most familiar one: ``The blast_radius policy blocks every push until
   review lands.``  ``The policy will not allow a push before review.``  ``Every
   push is gated by the policy layer.``  ``There is a gate. It stops every
   push.``  It is not the only one — a docstring describing an order the code
   does not follow, or a comment naming a validation that never runs, is the
   same defect and is equally uncaught. The mandate's [HONESTY] pass is scoped
   that widely deliberately; the checks in this file are not, and never were.
2. Worse than that: SCOPE. Every check here reads the seven files of this bundle,
   so a false claim anywhere else is outside this file's reach altogether — a
   code comment or docstring in a target repo, a README, a doc claim, and COMMIT
   MESSAGES, which no test in this repo reads at all and which the mandate
   singles out as the case a future reader has least reason to doubt. Inside the
   bundle the worker configs remain the highest-value target: they are the files
   workers read, and where the predecessor defect did its damage.
3. PR-FIRST ORDERING is no longer detected. ``Push the branch and open its PR
   first; run cross-review afterward.`` added to any file passes.
4. MARKER-BEFORE-GATE is no longer detected. ``Set READY on the registry before
   gates`` passes; nothing asserts that readiness follows the gate.
5. SAME-VENDOR / SKIPPED-REVIEW EXCEPTIONS are no longer detected. ``A
   same-vendor review may suffice.`` added to the root prompt passes, and so
   does a permission to merge.
6. A CONTRADICTING instruction placed beside a pinned one passes, everywhere —
   this is contradiction-blindness in its concrete forms. A worker prompt
   keeping ``Do NOT push and do NOT open a PR.`` while adding ``Push the branch
   before reporting.`` passes. A mandate keeping the whole ``[HONESTY]`` pass
   while adding an exemption from it passes. A lifecycle block keeping its
   canonical sentence while adding either probe above passes.
7. The banned-token check is an unconditional ban on three spellings, so it
   also rejects a truthful sentence that names one of them. That is a
   deliberate reserved-word rule, not a claim about meaning.
8. Anything NOT enumerated. ``_LIFECYCLE_CANONICAL`` and ``_LIFECYCLE_ANCHORS``
   are a list of named branches, not a completeness claim; a branch absent from
   both is protected by nothing, and adding a branch to the prose adds no test.

All eight now depend on review rather than on CI.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOLLY_BUNDLE = _REPO_ROOT / "examples" / "holly"

_WORKERS = ("claude_code", "codex", "pi")
_SKILLS = ("cross-review", "fanout", "investigate")

_MANDATE_BEGIN = "BEGIN REVIEWER-MANDATE-V1"
_MANDATE_END = "END REVIEWER-MANDATE-V1"

_CROSS_REVIEW = "skills/cross-review/SKILL.md"
_ROOT_CONFIG = "config.yaml"


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


def _orchestration_files() -> dict[str, str]:
    """
    :returns: Bundle-relative path -> text for the root config and skills.
    """
    paths = [
        _HOLLY_BUNDLE / _ROOT_CONFIG,
        *[_HOLLY_BUNDLE / "skills" / name / "SKILL.md" for name in _SKILLS],
    ]
    return {str(p.relative_to(_HOLLY_BUNDLE)): p.read_text(encoding="utf-8") for p in paths}


def _bundle_files() -> dict[str, str]:
    """
    :returns: Bundle-relative path -> text for every prose-bearing file.
    """
    files = _orchestration_files()
    for name in _WORKERS:
        path = _HOLLY_BUNDLE / "agents" / name / "config.yaml"
        files[str(path.relative_to(_HOLLY_BUNDLE))] = path.read_text(encoding="utf-8")
    return files


def _flatten(text: str) -> str:
    """
    Collapse every whitespace run to one space.

    Lets an exact-string check survive re-wrapping, which is a layout change
    rather than a wording change. It does not otherwise transform the text.

    :param text: Raw text.
    :returns: The same text on one line with single spaces.
    """
    return " ".join(text.split())


_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s")


def _segments(text: str) -> list[str]:
    """
    Unwrap prose into one segment per paragraph or list item.

    Used by the lifecycle anchors so that each procedure step is examined on its
    own and deleting a step is detectable.

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


# Reserved spellings. This is an UNCONDITIONAL ban on three strings, not a
# judgement about what any sentence means: the tokens name mechanisms that do
# not exist in this codebase, and reserving the spellings keeps them from
# reappearing as if they did. The cost is stated in the module docstring —
# a truthful sentence naming one of them must be reworded, which is the normal
# cost of a reserved word and is why the rule is worth stating as one.
#
# ``require_pr_review`` — a predecessor design named exactly this policy as the
#   thing blocking ``git push``. It exists in no builtin and was never
#   evaluated.
# ``review-passed`` / ``review_passed`` — a machine-checked "review passed"
#   flag that publication is conditioned on. No such state is recorded or read.
_BANNED_TOKENS: tuple[tuple[str, str], ...] = (
    ("require_pr_review", "names a policy that does not exist and is never evaluated"),
    ("review-passed", "implies a machine-checked gate state that is never recorded"),
    ("review_passed", "implies a machine-checked gate state that is never recorded"),
)


def test_skills_are_not_user_invocable(holly_spec: AgentSpec) -> None:
    """
    All three skills are orchestrator-only (``user-invocable: false``).

    These are playbooks written in holly's voice — they dispatch sessions,
    sequence publication, and route fixes. Exposing one as a user-invocable
    slash command would offer a human, or a worker whose skill list includes it,
    a workflow whose verbs only the orchestrator can perform.
    """
    for skill in holly_spec.skills:
        assert skill.user_invocable is False, skill.name


def _mandate(holly_spec: AgentSpec) -> str:
    """
    :param holly_spec: Loaded bundle spec.
    :returns: The text between the mandate delimiters — the only part that
        travels in a dispatch.
    """
    body = next(s for s in holly_spec.skills if s.name == "cross-review").content
    return body[body.index(_MANDATE_BEGIN) : body.index(_MANDATE_END)]


def test_reviewer_mandate_delimiters_are_intact(holly_spec: AgentSpec) -> None:
    """
    The mandate carries both delimiters, END after BEGIN, once each.

    Every worker prompt instructs the reviewer to refuse a dispatch whose
    mandate lacks its END marker, because a missing terminator is how truncation
    is detected. If the END marker is lost from the source block, every dispatch
    holly pastes is permanently un-terminated and every reviewer is obliged to
    refuse it. A duplicated marker is equally bad — the extractable block
    becomes ambiguous.
    """
    body = next(s for s in holly_spec.skills if s.name == "cross-review").content
    assert body.count(_MANDATE_BEGIN) == 1
    assert body.count(_MANDATE_END) == 1
    assert body.index(_MANDATE_END) > body.index(_MANDATE_BEGIN)


# What each WIDE axis must REQUIRE of a reviewer. Naming the axis is not the
# contract; these obligations are. An axis body reduced to "mention the input
# domain and report clear" still parses, still carries its heading, still
# matches its checklist label — and asks the reviewer for nothing.
#
# These are PRESENCE checks over a fixed vocabulary, so they are decidable: each
# either appears in the axis body or does not. They do not judge whether the
# body means the right thing, and rewording an axis will require updating the
# vocabulary here.
_AXIS_REQUIREMENTS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "blast-radius", (r"caller|consum", r"chang|renam|emit", r"did not|mirror|decod|match")),
    (
        2,
        "sibling-class",
        (r"\bclass\b", r"enumerat|every other|other site", r"not cover|incomplete|blocking"),
    ),
    (
        3,
        "input-domain",
        (
            r"taxonom|every shape",
            r"order|legacy|alternate",
            r"fall-?through|none-match",
            r"blocking",
            r"denylist|allowlist",
        ),
    ),
    (
        4,
        "coupled-artifact",
        (r"non-?code|artifact", r"lockstep|change kind|governance", r"prose|not merely"),
    ),
)

# Passes and walk-list items that are not numbered axes but are equally part of
# what a dispatch obliges. Each is a distinct defect class the reviewer would
# otherwise never look for.
_MANDATE_OBLIGATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("parallel-surfaces", (r"parallel surface", r"twin|same logic", r"blocking|grep")),
    ("test-surface-reverts", (r"test", r"revert", r"coverage|defect")),
    ("whole-parcel-grep", (r"parcel|entire", r"grep", r"old term|renames|redefin")),
    ("env-dependent-claims", (r"env|deployment", r"intrinsic|static", r"flag|claim")),
    ("claim-completeness", (r"claim", r"removed|added|unchanged", r"audit|stale")),
    ("rich-state-depth", (r"state space|stateful|rich", r"enumerat", r"one pass|full list")),
    ("self-consistency-pass", (r"self-?consistency", r"contradict", r"whole|scope")),
    ("governance-pass", (r"governance", r"convention|lifecycle|registry", r"conform|verify")),
    ("report-format-checklist", (r"battery-completeness", r"skipped|absent", r"one line")),
    ("finding-classification", (r"blocking", r"suggestion|non-blocking", r"file:line")),
    ("reviewer-role-boundary", (r"review and report", r"never", r"edit|push|merge")),
    ("repo-read-permitted", (r"read", r"repo", r"grep|sibling|not enough")),
)

# The [HONESTY] pass, pinned as EXACT strings — same shape as the canonical
# worker instructions below, for the same reason: these sentences ARE the
# obligation, so presence of the sentence is the assertion.
#
# The pass is asserted against the text BETWEEN the mandate delimiters, which
# is the whole point: the mandate is all a reviewer worker ever receives (it
# never loads the skill file), so an obligation that drifts outside the
# delimiters reaches nobody while the file still reads as if it were enforced.
#
# The four shapes are pinned individually because each names a distinct shape of
# false claim — the first is the one the bundle's own history produced — and
# deleting any one leaves the pass heading and its checklist line intact.
#
# The pass is scoped to any prose a reader acts on without verifying it — code
# comments, docstrings, commit messages, README and doc claims — not only to
# agent-facing prose. holly runs against arbitrary target repos, most carrying no
# agent prose at all, and under the narrow scope the pass returned "n/a" on
# nearly every diff. That scope clause sits INSIDE the honesty-pass string below
# rather than beside it, so re-narrowing the pass fails this test instead of
# silently reverting the widening. Same for the checklist row: it no longer
# carries an "n/a" opt-out, and the pinned row is the one without it.
_MANDATE_CANONICAL: tuple[tuple[str, str, str], ...] = (
    (
        "honesty-pass",
        "false claims a reader acts on without verifying are audited by nobody — "
        "no reviewer is asked to",
        "[HONESTY] — audit every sentence in this diff that a reader will act on "
        "without verifying it: code comments, docstrings, commit messages, README and "
        "doc claims, and any prose an agent will read as instruction. Report each one "
        "that asserts something the code or runtime does not actually do.",
    ),
    (
        "honesty-shape-nonexistent-guarantee",
        "a mechanism never reached on the live path is described as blocking, gating or "
        "ensuring — the predecessor defect here, and the same defect in any repo",
        "a guarantee that does not exist — a policy, guard, validation, constraint, "
        "lock, limit, permission check or runtime step described as blocking, gating, "
        'requiring, preventing or ensuring something. Include negated phrasings ("will '
        'not accept an unvalidated payload") and named actors ("X checks every request '
        'before ..."), which assert a guarantee just as strongly. Verify the mechanism '
        "EXISTS and does what the sentence says; a declared mechanism never reached on "
        "the live path does not count.",
    ),
    (
        "honesty-shape-ordering",
        "prose documenting an order of operations the code does not follow reads as true",
        "a documented sequence that contradicts the real one — prose describing an "
        "order of operations the code does not follow, in any phrasing.",
    ),
    (
        "honesty-shape-marker-before-attestation",
        "a status set before the thing it attests is read as evidence that thing happened",
        "a status, flag or marker set before the thing it is supposed to attest.",
    ),
    (
        "honesty-shape-exception-to-an-absolute",
        "an exception is written into a rule the contract or spec states has none",
        "an exception to a rule the contract or spec states has none.",
    ),
    (
        "honesty-checklist-entry",
        "the pass is silently skippable: a report omitting it still looks complete",
        "[HONESTY] prose claims vs actual behaviour",
    ),
)


def test_reviewer_mandate_axes_state_their_requirements(holly_spec: AgentSpec) -> None:
    """
    Each WIDE axis is bound to its number in both places AND still states what
    it requires; the non-axis passes state theirs too.

    Renaming an axis heading is one defect; gutting its body is the same defect
    one level down. Keep the heading and the checklist label, replace the body
    with "mention the input domain and report clear", and the reviewer receives
    a pass name with no obligation attached — every finding it would have
    produced disappears while the battery checklist still reports
    "[WIDE-3] run — clear".

    ``_MANDATE_CANONICAL`` pins the [HONESTY] pass verbatim rather than by
    vocabulary. Presence only, as everywhere in this file: it proves the pass is
    inside the delimiters, not that nothing beside it contradicts the pass.
    """
    mandate = _mandate(holly_spec)

    bounds = [
        (int(m.group(1)), m.start())
        for m in re.finditer(r"^\s*(\d)\.\s", mandate, re.MULTILINE)
        if int(m.group(1)) in (1, 2, 3, 4)
    ]
    assert [n for n, _ in bounds] == [1, 2, 3, 4], "the four WIDE axes are no longer a 1-4 list"
    bodies = {}
    for index, (number, start) in enumerate(bounds):
        end = bounds[index + 1][1] if index + 1 < len(bounds) else len(mandate)
        bodies[number] = mandate[start:end]

    for number, axis, requirements in _AXIS_REQUIREMENTS:
        body = bodies[number]
        assert re.search(rf"^\s*{number}\.\s*{axis}\b", body, re.IGNORECASE | re.MULTILINE), (
            f"WIDE axis {number} is no longer defined as {axis}"
        )
        assert re.search(rf"\[WIDE-{number}\][^\n]*\b{axis}\b", mandate, re.IGNORECASE), (
            f"the report checklist does not list [WIDE-{number}] as {axis}"
        )
        for requirement in requirements:
            assert re.search(requirement, body, re.IGNORECASE), (
                f"WIDE-{number} ({axis}) no longer requires {requirement!r} of the "
                f"reviewer; the pass survives in name only and its findings vanish."
            )

    for obligation, patterns in _MANDATE_OBLIGATIONS:
        assert all(re.search(p, mandate, re.IGNORECASE) for p in patterns), (
            f"the mandate no longer obliges {obligation!r}; a dispatched reviewer "
            f"would not look for that defect class at all."
        )

    flat_mandate = _flatten(mandate)
    for obligation, consequence, canonical in _MANDATE_CANONICAL:
        assert canonical in flat_mandate, (
            f"the mandate no longer carries {obligation!r} between its delimiters, "
            f"or it has been reworded.\n"
            f"  expected verbatim: {canonical!r}\n"
            f"  Without it: {consequence}."
        )

    assert "[FOCUSED]" in mandate


def test_root_policy_arguments_are_pinned(holly_spec: AgentSpec) -> None:
    """
    The policies' ARGUMENTS are pinned, not just their names.

    A policy set can satisfy name equality and still enforce. Flipping
    ``gate_pushes`` to ``true`` is a one-word edit that leaves every name in
    place, turns ``blast_radius`` into an ASK gate on push, and falsifies every
    "nothing blocks a push" disclaimer in the bundle at once.

    ``blast_radius`` and the purpose guard are stateless: they decide from the
    event in front of them, so their arguments determine what happens on every
    gated call. ``spawn_bounds`` is different — it counts dispatches in closure
    state and relies on the policy engine surviving the turn, which it does not
    on the deploy path where the engine is rebuilt per request. Its arguments
    are pinned as the DECLARED bound; this test makes no claim that the per-turn
    cap is enforced.
    """
    policies = {p.name: p for p in holly_spec.guardrails.policies}

    assert policies["blast_radius"].function.arguments.get("gate_pushes") is False, (
        "blast_radius must not gate pushes: the bundle tells every reader that "
        "nothing mechanically blocks a push, and enabling the gate makes that "
        "claim false everywhere it appears."
    )

    spawn_args = policies["spawn_bounds"].function.arguments
    assert isinstance(spawn_args.get("max_dispatches_per_turn"), int)
    assert spawn_args["max_dispatches_per_turn"] > 0
    # Both dispatch surfaces are declared: with spawn: true, an omitted
    # sys_session_create would leave self-defined children outside the bound.
    assert set(spawn_args.get("dispatch_tools") or []) == {
        "sys_session_send",
        "sys_session_create",
    }

    purpose_args = policies["headless_subagent_purpose_guard"].function.arguments
    assert set(purpose_args["allowed_purposes"]) == {
        "implement",
        "review",
        "explore",
        "search",
    }


def test_policy_factory_paths_resolve(holly_spec: AgentSpec) -> None:
    """
    Every policy's dotted path actually imports to a callable.

    A prefix check passes ``blost_radius``. The failure mode is quiet: the spec
    still loads, the policy still appears in the set by name, and the breakage
    surfaces only when the runner first tries to evaluate it.
    """
    for policy in holly_spec.guardrails.policies:
        module_name, _, attribute = policy.function.path.rpartition(".")
        target = getattr(importlib.import_module(module_name), attribute, None)
        assert callable(target), f"{policy.name}: {policy.function.path} is not callable"


def test_no_sub_agent_declares_a_policy_layer(holly_spec: AgentSpec) -> None:
    """
    No worker declares ``guardrails.policies``.

    The absence is ratified, not incidental. A sub-agent's own policy block is
    not evaluated for its tool calls on the server deploy path — the server
    loads the ROOT bundle spec and ignores the sub-agent name — so a block here
    reads as protection while enforcing nothing. That is a false claim of
    enforcement expressed in YAML instead of prose, and it is what was removed
    from these three files. Re-adding a syntactically valid block is cheap,
    silent, and looks like a hardening commit.

    ``guardrails`` itself may remain (``pi`` sets ``ask_timeout``, a real runner
    knob); only the policy list must stay empty.
    """
    for sub in holly_spec.sub_agents:
        policies = list(sub.guardrails.policies or []) if sub.guardrails else []
        assert policies == [], (
            f"{sub.name} declares {[p.name for p in policies]}; a sub-agent policy "
            f"block is not evaluated on the server deploy path, so it claims "
            f"protection it does not provide."
        )


def test_no_file_uses_a_reserved_spelling() -> None:
    """
    No bundle file contains a reserved token.

    Unconditional: the three spellings name mechanisms absent from this
    codebase, and the ban keeps them from reappearing as though they existed.
    It is a reserved-word rule, so it also rejects a truthful sentence that
    happens to name one — that cost is stated in the module docstring and is
    the price of the rule being decidable rather than interpretive.
    """
    for rel_path, text in _bundle_files().items():
        lowered = text.lower()
        for token, why in _BANNED_TOKENS:
            assert token not in lowered, f"{rel_path}: reserved spelling {token!r} — {why}"


# The canonical worker instructions, asserted as EXACT strings.
#
# The earlier form pattern-matched around these ("commit" near "branch") and was
# weakened further during the module split into a plain search, at which point
# a prompt saying "do NOT commit to your task branch" satisfied every one of
# them. Deciding whether an instruction has been inverted is the undecidable
# problem again; deciding whether a known sentence is present is not. So the
# sentence itself is the assertion.
#
# This proves PRESENCE only. It does not prove that a contradicting instruction
# is absent — see cost item 6 in the module docstring. Rewording a worker prompt
# requires updating the string here, which is the intended trade: the check is
# exact, and it fails loudly rather than silently drifting.
_WORKER_INSTRUCTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "scope-discipline",
        "a worker refactors beyond the task and the diff stops matching the contract",
        "Stay strictly within the files/scope named in your task and acceptance contract.",
    ),
    (
        "drive-to-green",
        "the orchestrator gets a diff that was never run",
        "Make the change, then drive it to green: run the relevant tests, lint, and "
        "typecheck for the code you touched.",
    ),
    (
        "exact-test-command-and-file-set",
        "reported counts cannot be reconciled against the same gate",
        "When you report test results, include the exact command and file set.",
    ),
    (
        "collected-cases-vs-functions",
        "a parametrized suite is mislabelled as an over-report",
        "If you mention counts, distinguish collected test cases from test functions.",
    ),
    (
        "co-sign-commits",
        "commits lose the trailer that marks them as agent-authored",
        "Co-sign every commit you author:",
    ),
    (
        "commit-to-its-branch",
        "an uncommitted worktree leaves nothing for the orchestrator to review",
        "When green, commit to your task branch.",
    ),
    (
        "no-publication-by-the-worker",
        "publication escapes holly's sequencing and the skipped review cannot be re-inserted",
        "Do NOT push and do NOT open a PR.",
    ),
    (
        "report-and-stop",
        "a worker that keeps going after reporting drifts outside its scope",
        "Report the branch name and a file:line summary of what changed, then stop.",
    ),
    (
        "publication-is-orchestrator-sequenced",
        "the worker does not know who releases its branch, so it waits forever or publishes",
        "The orchestrator reviews your local diff with an independent different-vendor "
        "reviewer and tells you when to publish.",
    ),
    (
        "review-is-report-only",
        "a reviewer edits the diff it is judging and independence is gone",
        "Review and report ONLY. Never edit, push, open/merge a PR, or dispatch.",
    ),
    (
        "dispatch-completeness-definition",
        "a reviewer that cannot say what a complete dispatch contains cannot detect a partial one",
        "A valid dispatch carries BOTH mandate delimiters, the diff, the contract, and "
        "the complete battery checklist.",
    ),
    (
        "incomplete-dispatch-handling",
        "a truncated mandate yields a verdict instead of a refusal, and reads as a pass",
        "If any is absent, truncated, malformed, or missing an axis: open with "
        "`INCOMPLETE DISPATCH`, name exactly what is missing, give best-effort "
        "evidence, and issue NO verdict.",
    ),
    (
        "follow-the-dispatched-mandate",
        "a worker substitutes a repo skill for the mandate it was handed",
        "Otherwise follow the dispatched mandate exactly; never hunt for skills.",
    ),
    (
        "explore-is-read-only",
        "a read-only task mutates the repo",
        "Read only what you need; edit nothing. Answer with file:line evidence.",
    ),
)


def test_workers_carry_their_canonical_instructions(holly_spec: AgentSpec) -> None:
    """
    Every worker prompt contains each canonical instruction verbatim.

    Exact-string presence, because the alternative — asking whether an
    instruction has been negated, inverted or contradicted — is the undecidable
    problem this file no longer attempts. The three worker prompts are identical
    apart from the vendor name, so the same strings apply to all three.

    Presence only: a prompt that keeps every string below and ALSO adds a
    contradicting instruction passes. That gap is cost item 6 in the module
    docstring and is now a reviewer's responsibility.
    """
    by_name = {a.name: a for a in holly_spec.sub_agents}
    for name in _WORKERS:
        instructions = _flatten(by_name[name].instructions or "")
        assert instructions, f"{name}: no instructions at all"
        for obligation, consequence, canonical in _WORKER_INSTRUCTIONS:
            assert canonical in instructions, (
                f"{name}: canonical instruction {obligation!r} is missing or reworded.\n"
                f"  expected verbatim: {canonical!r}\n"
                f"  Without it: {consequence}."
            )


# ───────────────── review-lifecycle branches (D3 / D5 / D6) ─────────────────
#
# Both tables below are an ENUMERATION, not a claim of completeness: they pin
# the branches named, each checked by deleting the text that carries it. A
# branch listed in neither table is unprotected.
#
# The split is by what the words are doing. Where the sentence IS the rule — an
# ordering, a prohibition, an authority boundary, a denied exemption — it goes
# in _LIFECYCLE_CANONICAL and is pinned verbatim, because a vocabulary pattern
# for such a rule is satisfied just as well by a sentence that INVERTS it
# ("Before green gates and zero blocking issues, push" carries green + zero
# blocking + push). Where the sentence is a procedure whose wording is
# incidental — a command to run, a discovery step, a storage mechanic — it stays
# in _LIFECYCLE_ANCHORS as vocabulary, because pinning that prose verbatim would
# fail on every harmless rewrite.
#
# The test applied when an anchor moves: NORMATIVE (must / never / only-then /
# who-may) goes to the fixed string; MECHANIC (which command, which file, which
# store) stays a pattern. When a block holds both, the rule is pinned and the
# anchor is kept, narrowed to the mechanic it still guards — so no coverage is
# dropped in the move. Where the pinned sentence already carries every word the
# anchor searched for, the anchor is deleted as dead weight.
#
# Both are PRESENCE checks, so both are contradiction-blind; see the module
# docstring. A fixed string closes the inversion hole for the sentence it pins.
# It closes nothing for a block still anchored by vocabulary, and the module
# docstring names the ones that remain.
_LIFECYCLE_CANONICAL: tuple[tuple[str, str, str, str], ...] = (
    (
        "mandate-pasted-verbatim",
        "the dispatched mandate is paraphrased and its obligations quietly shrink",
        _CROSS_REVIEW,
        "The dispatch `input` = the diff + the acceptance contract + this block, "
        "unedited, delimiters included.",
    ),
    (
        "fix-round-reviews-the-local-delta",
        "a fix round approves what is already published and misses the new commit",
        _CROSS_REVIEW,
        "review the LOCAL delta that is about to be pushed",
    ),
    (
        "mcp-serves-threads-not-the-diff",
        "the diff is fetched from MCP, so the review reads what is already published "
        "instead of the commit about to be added",
        _CROSS_REVIEW,
        "Use MCP here for review THREADS and their resolution state, not for the diff.",
    ),
    (
        "red-gate-returns-to-the-fixer",
        "reviewer tokens get spent on defects a validator already names",
        _CROSS_REVIEW,
        "Any RED gate goes back to the fixer to drive green first — zero reviewer "
        "tokens on a defect a validator already names.",
    ),
    (
        "grep-c-is-not-a-count-oracle",
        "a parametrized suite is mislabelled as an over-report",
        _CROSS_REVIEW,
        "Never use `grep -c 'def test_'`: it counts functions, not collected cases, "
        "and misses parametrized expansion.",
    ),
    (
        "pi-independence-requires-a-cross-vendor-model",
        "a pi reviewer on the author's own model reports independence it lacks",
        _CROSS_REVIEW,
        "`pi` runs any gateway model, so a `pi` reviewer is independent only if you "
        "pass it `args.model` from a different vendor than the author's.",
    ),
    (
        "undeterminable-or-same-vendor-is-a-hard-stop",
        "the review is dispatched to the author's own vendor and called independent",
        _CROSS_REVIEW,
        "If the effective vendor on either side cannot be determined, or resolves to "
        "the same vendor, STOP and escalate — do not dispatch and call it independent.",
    ),
    (
        "dispatch-in-the-same-turn",
        "a turn that only announces the review stalls the whole run",
        _CROSS_REVIEW,
        "Emit the dispatch in the SAME turn you decide to review — a turn that only "
        "announces the intent stalls the run, because nothing dispatches and no inbox "
        "wake will arrive.",
    ),
    (
        "an-invalid-report-is-not-a-review",
        "an incomplete report, a short-delivered one, or a verdict despite a refusal, "
        "reads as a pass",
        _CROSS_REVIEW,
        "No opening checklist, a missing axis, a declared finding count the report "
        "does not deliver, or a verdict issued despite an `INCOMPLETE DISPATCH` "
        "notice means the review did not happen. Re-dispatch the identical full "
        "mandate.",
    ),
    (
        # Separate from the step above, which pins the same count clause as one of
        # four invalidating conditions. This pins the RULE the clause is shorthand
        # for: what a count mismatch means and why the return path needs it at all.
        # Deleting this paragraph leaves step 4 word-for-word intact, so nothing
        # else here fails — holly is left told to notice a mismatch and never told
        # that a mismatch is a truncated transport rather than a completed review.
        "return-path-truncation-detected-by-count",
        "a report truncated in transport reads as a normal successful completion, and "
        "the missing findings are never dispatched for",
        _CROSS_REVIEW,
        "The checklist declares a finding count per pass: reconcile it against the "
        "findings actually delivered. The DISPATCH is guarded against truncation by "
        "the mandate's END marker; the RETURN is not, and a worker can return a "
        "partial result that reads as a normal successful completion. A report "
        "declaring more findings than arrived was truncated in transport, not "
        "completed.",
    ),
    (
        "same-fixer-same-branch",
        "a new title spawns a fresh worker with no memory of the task",
        _CROSS_REVIEW,
        "Route blocking issues back to the SAME fixer, on the SAME branch.",
    ),
    (
        "class-recurrence-stops-point-fixing",
        "point-fixing continues while the same defect class recurs at new sites",
        _CROSS_REVIEW,
        "The FIRST time any later round surfaces the SAME class at a NEW site, STOP "
        "point-fixing: in that same round escalate the fix to whole-surface closure "
        'and the review to a whole-repo same-class audit, with "zero remaining" as '
        "the bar.",
    ),
    (
        "post-review-edit-invalidates-verdict",
        "a 'tiny' follow-up ships on a verdict that never saw it",
        _CROSS_REVIEW,
        'If anything changes after the clean report — a fix, a rebase, a "tiny" '
        "follow-up — the gates and the full review run again on the new HEAD.",
    ),
    (
        "release-requires-green-and-zero-blocking",
        "the PR opens on red gates or on unresolved blocking findings",
        _CROSS_REVIEW,
        "Green gates AND zero blocking issues, and only then: tell the SAME "
        "implementer to push its branch and open the PR",
    ),
    (
        "direct-authoring-pr-ownership",
        "prose holly wrote has no one responsible for publishing it",
        _CROSS_REVIEW,
        "For directly-authored work Holly pushes and opens its own reviewed PR.",
    ),
    (
        "holly-never-merges",
        "the orchestrator takes the merge decision that belongs to the human",
        _CROSS_REVIEW,
        "Holly does NOT merge.",
    ),
    (
        "reviewer-unavailable-hard-stop",
        "with no different-vendor reviewer the run silently proceeds unreviewed",
        _CROSS_REVIEW,
        "If no different-vendor reviewer is available, you CANNOT run independent "
        "review: STOP, do not open the PR on unreviewed code, and pull in the human.",
    ),
    (
        "never-degrade-to-same-vendor",
        "review quietly downgrades to same-vendor or skipped instead of stopping",
        _CROSS_REVIEW,
        "Never silently degrade to a same-vendor or skipped review.",
    ),
    (
        "every-fix-diff-is-reviewed",
        "fix rounds ship unreviewed because the first round was reviewed",
        _CROSS_REVIEW,
        "Every fix diff gets the same pre-push review as any other change.",
    ),
    (
        "bot-findings-clustered-and-the-mandate-not-narrowed",
        "the same class is point-fixed once per comment, and the bot's list becomes "
        "the review scope",
        _CROSS_REVIEW,
        "Cluster its findings by BUG CLASS before fixing anything, and feed them in as "
        "additional FOCUSED inputs to a re-run of the identical complete mandate — "
        'never a "confirm these are fixed" scope.',
    ),
    (
        "reply-in-thread",
        "replies detach from the finding and resolution state cannot be read",
        _CROSS_REVIEW,
        "Reply in-thread rather than as a new top-level comment.",
    ),
    (
        "repeated-class-hard-stop",
        "a recurring class keeps being point-fixed during bot servicing",
        _CROSS_REVIEW,
        "A repeated class is a hard stop for point-fixing: escalate to whole-surface closure.",
    ),
    (
        "unresolved-thread-count-on-handoff",
        "the human merges believing replies established resolution",
        _CROSS_REVIEW,
        "When handing status to the human, report the count of UNRESOLVED review "
        "threads alongside your summary, because replies do not establish resolution — "
        '"findings serviced" is not "threads resolved", and the human is merging on '
        "that distinction.",
    ),
    (
        "handoff-wording-implies-no-completeness",
        "'findings serviced' is read as 'threads resolved' and the human merges",
        _CROSS_REVIEW,
        "Holly's hand-off wording must not imply completeness ('findings serviced' "
        "≠ 'threads resolved').",
    ),
    (
        "never-declares-a-clean-bill",
        "the orchestrator issues the sign-off that is the human's to give",
        _CROSS_REVIEW,
        "Holly never declares a clean bill and never merges.",
    ),
    (
        "direct-authoring-not-exempt",
        "prose holly wrote itself reaches the remote without an independent review",
        _ROOT_CONFIG,
        "This carve-out is about WHO WRITES the artifact, not an exemption from "
        "review-before-push.",
    ),
)

# Anchoring here is per block and per owning file. Per block, because scattering
# the vocabulary across a document would satisfy a whole-file search. Per owning
# file, because the prose deliberately restates several rules in more than one
# place — good redundancy, but the restatement is not the procedure, so deleting
# the procedural step must fail even while the restatement lives.
_LIFECYCLE_ANCHORS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "diff-of-the-unopened-branch",
        _CROSS_REVIEW,
        "review runs against the wrong revision range",
        (r"diff", r"worktree|main\.\.\.HEAD", r"committed|nothing is pushed"),
    ),
    (
        "diff-of-an-already-open-pr",
        _CROSS_REVIEW,
        "the already-open-PR case loses the revision range that isolates the unreviewed "
        "delta, and the reason MCP cannot supply it",
        (
            r"already-open|pre-existing PR",
            r"origin/|local delta|unreviewed",
            r"pull_request_read|published diff",
        ),
    ),
    (
        "gate-discovery-is-not-assumed",
        _CROSS_REVIEW,
        "repo-specific validators never run and their defects reach the reviewer",
        (r"discover", r"package\.json|scripts|governance", r"rather than assuming|full"),
    ),
    (
        "pytest-count-ground-truth-command",
        _CROSS_REVIEW,
        "a reported count is reconciled against a different command, file set or commit",
        (r"ground truth", r"--collect-only", r"exact file set"),
    ),
    (
        "fix-round-reuses-the-implementer-session",
        _CROSS_REVIEW,
        "the fixer is re-addressed in a way that loses its worktree and branch",
        (r"same implementer conversation", r"session_id", r"worktree"),
    ),
    (
        "blocking-issues-logged-as-registry-fix-tasks",
        _CROSS_REVIEW,
        "findings are tracked only in the turn that produced them",
        (r"registry", r"fix-task|log", r"worktree|scoped"),
    ),
    (
        "class-vs-one-off-and-the-enumeration-table",
        _CROSS_REVIEW,
        "a class-shaped fix is scoped like a typo, and closure is sampled not proven",
        (r"one-off", r"every instance", r"enumeration table"),
    ),
    (
        "release-records-registry-readiness",
        _CROSS_REVIEW,
        "the human is never handed a PR URL or told it is ready",
        (r"registry", r"PR URL|record", r"ready|human"),
    ),
    (
        "bot-sweep-uses-a-timer",
        _CROSS_REVIEW,
        "holly busy-polls a bot that posts on its own lag",
        (r"timer", r"sweep|lag", r"polling|genuine|delay"),
    ),
)

# What the contract handed to an IMPLEMENTER must enumerate. Upstream of the
# review: any dimension the contract omits, the reviewer starts blind on, and
# the blind spot recurs every round.
_CONTRACT_AUTHORING: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input-taxonomy", (r"taxonom|input domain", r"legacy|alternate|nested|wrapped")),
    ("match-ordering-and-fallthrough", (r"order", r"fall-?through|none-match")),
    ("failure-mode-as-behaviour", (r"failure mode", r"behaviour|behavior|not.*site list")),
    ("coupled-non-code-artifacts", (r"coupled", r"non-?code|artifact")),
    ("rich-state-matrix-test", (r"state-?space|stateful|rich", r"invariant|round-?trip|matrix")),
    ("sites-are-not-a-contract", (r"site", r"enumerat|guarantee|instead")),
)


def test_review_lifecycle_branches_survive() -> None:
    """
    The enumerated review-lifecycle branches are still present.

    These are the branches whose deletion is invisible — the bundle still
    parses, every other test stays green, and the failure shows up only as a run
    that quietly did the wrong thing: a review dispatched to the author's own
    vendor, a fix round reviewing the published diff instead of the delta about
    to be pushed, gate discovery narrowed so the repo's own validators never
    run, a bot's comment list replacing the full battery, or a hand-off that
    reports findings serviced while threads stay unresolved.

    The list is an enumeration, not a completeness claim: a branch not listed
    is not protected by this test.

    Rules whose exact sentence IS the obligation are pinned verbatim; procedure
    steps whose wording is incidental stay as per-block vocabulary. Presence
    either way, so neither form fails on a sentence ADDED beside it that says
    the opposite — the module docstring's contradiction-blindness. The two forms
    differ on REPLACEMENT: a pinned rule fails when its sentence is swapped for
    an inverted one, a vocabulary anchor does not. The anchors still here are
    the ones whose wording is incidental, and two of them are named in the
    module docstring as verified-blind.
    """
    texts = _orchestration_files()
    for branch, consequence, owner, canonical in _LIFECYCLE_CANONICAL:
        assert canonical in _flatten(texts[owner]), (
            f"review-lifecycle rule {branch!r} is gone from {owner}, or reworded.\n"
            f"  expected verbatim: {canonical!r}\n"
            f"  Without it: {consequence}."
        )

    for branch, owner, consequence, patterns in _LIFECYCLE_ANCHORS:
        blocks = _segments(texts[owner])
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        assert any(all(c.search(block) for c in compiled) for block in blocks), (
            f"review-lifecycle branch {branch!r} is gone from {owner}. Without it: {consequence}."
        )


def test_contract_authoring_requirements_survive() -> None:
    """
    The contract handed to an implementer still has to be authored WIDE.

    A contract that enumerates SITES rather than the input DOMAIN guarantees
    that siblings arrive one per round, and no amount of reviewer diligence
    recovers a dimension the contract never named. Gutting this section leaves
    the mandate untouched and the blind spots permanent.
    """
    text = _orchestration_files()[_CROSS_REVIEW]
    for requirement, patterns in _CONTRACT_AUTHORING:
        assert all(re.search(p, text, re.IGNORECASE) for p in patterns), (
            f"the contract-authoring section no longer requires {requirement!r}; "
            f"every review inherits that blind spot and it recurs each round."
        )
