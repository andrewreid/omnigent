"""Structural guard for the holly coding-orchestrator bundle (examples/holly).

holly is a claude-sdk orchestrator that delegates every coding task to its
``claude_code`` / ``codex`` / ``pi`` workers, runs an independent
different-vendor review against the LOCAL branch diff, and only then sequences
publication. Parse-only, so it runs in the default suite; the headline contract
also has a thin guard under ``tests/e2e/omnigent/test_example_holly.py`` for the
per-example coverage rule.

Publication ordering is prompt discipline, not enforcement. No policy SHIPPED BY
HOLLY gates ordinary publication: a plain ``git push`` is ungated.
``blast_radius`` does deny a set of destructive push forms — the bundle
enumerates them and ``test_review_lifecycle_branches_survive`` below pins that
enumeration, which is why the list is not repeated here — but the denial is
TEXT-MATCHED on the shell command and
models neither nested shells nor ``eval`` nor git aliases, so
``sh -c 'git push --force'``, ``eval`` and ``git -c alias.x='push --force' x``
all pass even though the nested text contains an enumerated form. It is a guard
against accident, not against intent, and nothing here should be read as saying
otherwise. A deployment may also attach session-level or server-wide policies
that DO gate ordinary pushes; those are not Holly's and this file asserts
nothing either way about them.

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
9. DIRTY RUNNER-ROOT CONTAMINATION is not tested, and the fanout pins do not
   test it. They prove the INSTRUCTION survives — that the three baselines, the
   porcelain clause and the moved-HEAD check are still in the file a reader
   receives. They prove NOTHING about whether contamination is actually caught,
   because catching it is holly's runtime judgement rather than a mechanism:
   nothing compares a baseline for it, and nothing fails if it never looks. The
   behavioural test was considered and deliberately not built — it needs a git
   fixture with a runner root plus worktree and a scripted brain, and against a
   scripted brain the assertion is about the script, not about holly. This is
   the same wall as review sequencing (see the e2e module's own disclaimer), and
   it is a reviewer's job for the same reason.

All nine now depend on review rather than on CI.
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
_FANOUT = "skills/fanout/SKILL.md"
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
    place, turns ``blast_radius`` into an ASK gate on ORDINARY pushes, and
    falsifies the bundle's disclaimer that a plain ``git push`` is ungated
    everywhere it appears. It does not affect the catastrophic variants, which
    the DENY branch refuses before ``gate_pushes`` is consulted.

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
        "blast_radius must not gate ordinary pushes: the bundle tells every "
        "reader that a plain `git push` is ungated and that only catastrophic "
        "variants are denied, and enabling the gate makes the first half of that "
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
        # The D1a honesty claim itself, and the one sentence in the bundle with a
        # record of being wrong in BOTH directions: it once claimed a gate that
        # was never evaluated, and then claimed nothing was denied at all while
        # blast_radius refuses the catastrophic set before gate_pushes is read.
        # Nothing else pins it, so either reversion is invisible. Pinned whole,
        # including the bidirectional instruction, because a reworded claim about
        # what the runtime does is exactly the sentence that should be re-read
        # deliberately rather than drift.
        "push-enforcement-stated-in-both-directions",
        "a worker is told either that a gate exists, or that nothing is denied and a "
        "force-push is available to it; or the denial set is named short of what "
        "`_push_severity` actually refuses",
        _CROSS_REVIEW,
        "`blast_radius` denies destructive push variants — `--force*`, `--delete`, "
        "`--mirror`, `--prune`, bundled short forms containing `-f` or `-d`, and "
        "`+refspec` / `:refspec` — while a plain `git push` is ungated.",
    ),
    (
        # Split out of the span above, which a new sentence now sits inside. This
        # one is a claim about how STRONG the denial is, and its deletion leaves the
        # list of denied variants reading as a guarantee against a determined
        # worker. `sh -c` and a git alias defeat a text match; nothing in the list
        # says so on its own.
        "push-denial-is-text-matched-not-semantic",
        "the variant list reads as a real barrier, so a worker infers that a "
        "force-push is impossible rather than merely spelled differently",
        _CROSS_REVIEW,
        "That denial is TEXT-MATCHED and models neither nested shells nor `eval` nor "
        "git aliases, so `sh -c '...'`, `eval '...'` and `git -c alias.x='push "
        "--force' x` all pass it: a guard against accident, not against intent.",
    ),
    (
        # Also split out: it was inside the old span and is now separated from it.
        # Without it the file reads as if Holly's policy set were the whole story.
        "push-gating-may-come-from-the-deployment",
        "a deployment's own session or server-wide gate is assumed absent, and an ASK "
        "on an ordinary push is read as a malfunction",
        _CROSS_REVIEW,
        "A deployment may attach session-level or server-wide policies that gate "
        "ordinary pushes too; those are not Holly's and you cannot assume either way.",
    ),
    (
        # Split out of the span above rather than pinned with it. The sentence
        # between them — "The ordering below holds only because you sequence it" —
        # is a consequence of the fact, and keeping it inside one long span meant a
        # rewrite of the consequence failed the test while every fact stood. This
        # instruction is separately deletable and is the D1a rule itself, so it
        # gets its own entry instead.
        "push-claim-must-not-mislead-in-either-direction",
        "the honesty rule for worker-facing prose is gone, leaving the facts above "
        "with no instruction attached to them",
        _CROSS_REVIEW,
        "That is the honest description, and you must not tell a worker otherwise in "
        "EITHER direction — neither that a gate exists, nor that nothing is denied "
        "at all.",
    ),
    (
        # The same D1a claim in the ROOT PROMPT — the copy holly itself carries
        # every turn, where the skill above is loaded only when the review
        # lifecycle runs. Same both-directions history, same invisible reversion,
        # different file, so neither pin can catch the other's edit.
        #
        # Span ends at "is ungated". The tail that follows — "and this ordering
        # holds only because you sequence it" — explains why the ordering holds
        # rather than stating what the runtime does, and pinning it would fail
        # this test on a rewrite of the explanation while both facts stood
        # untouched. The facts are what a reader acts on and cannot verify.
        "push-enforcement-in-the-root-prompt",
        "the prompt holly carries every turn says either that a gate exists, or that "
        "nothing is denied and a force-push is available; or it names the denial set "
        "short of what `_push_severity` refuses",
        _ROOT_CONFIG,
        "No policy SHIPPED BY HOLLY gates ordinary publication: `blast_radius` "
        "denies destructive push variants — `--force*`, `--delete`, `--mirror`, "
        "`--prune`, bundled short forms containing `-f` or `-d`, and `+refspec` / "
        "`:refspec` — while a plain `git push` is ungated.",
    ),
    (
        # The root-prompt twin of the text-matching caveat. Its wording differs from
        # the skill's — this copy also carries the explicit instruction not to
        # DESCRIBE the guard as preventing anything, which is the D1a rule applied
        # to Holly's own future prose, so the two are not interchangeable.
        "push-denial-is-text-matched-in-the-root-prompt",
        "holly repeats the variant list to a worker as if it prevented a force-push",
        _ROOT_CONFIG,
        "That denial is TEXT-MATCHED on the shell command and models neither nested "
        "shells nor `eval` nor git aliases: `sh -c '...'`, `eval '...'` and `git -c "
        "alias.x='push --force' x` all pass. It guards against accident, not against "
        "intent, and you must not describe it as preventing anything.",
    ),
    (
        "push-gating-may-come-from-the-deployment-root-prompt",
        "the prompt holly carries every turn assumes no deployment gate exists",
        _ROOT_CONFIG,
        "A deployment may attach session-level or server-wide policies that DO gate "
        "ordinary pushes; those are not Holly's and you cannot assume either way.",
    ),
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
        # Re-synced: independence is now a model you CONFIRM rather than one you
        # pass, which is the whole point of the routing caveat — passing a model is
        # a request the server may discard. The span stops before the
        # continuation-rejects-a-resent-model mechanic, which has its own entry.
        "pi-independence-requires-a-confirmed-cross-vendor-model",
        "a pi reviewer on the author's own model reports independence it lacks, or a "
        "requested model is treated as a confirmed one",
        _CROSS_REVIEW,
        "A reviewer is independent only if the effective model you CONFIRM differs in "
        "vendor from the author's, which for `pi` means passing `args.model`, on the "
        "dispatch that CREATES the review session",
    ),
    (
        # The mechanic that makes the rule above ACTIONABLE, pinned separately
        # because it is a different claim: that one says WHICH model a pi
        # reviewer needs, this one says the create-time send is the only place
        # to set it. Deleting it leaves the independence rule word-for-word
        # intact while removing the only statement of how a continuation
        # behaves. The rejection is NOT silent — the runtime returns an explicit,
        # actionable error naming the existing session and saying to re-send
        # without `model` or close the session first — but holly only benefits
        # from that error if the prose tells it the resend is refused rather
        # than applied, which is what this pins.
        "continuation-keeps-the-creation-model",
        "a re-sent model is assumed to have taken effect, so a review continues on "
        "the model the session was created with while holly believes it changed",
        _CROSS_REVIEW,
        "a continuation keeps the model set at creation and rejects a resent one, "
        "so verify it rather than resending it.",
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
        # The return-path rule, pinned as its two OPERATIVE clauses rather than as
        # one span across the paragraph. They are separated by the paragraph's
        # rationale (the DISPATCH/RETURN asymmetry), which is the most rewordable
        # sentence in it and which pinning bought nothing: a rewrite of the reason
        # failed the test while the rule was untouched. Deleting either clause now
        # fails, and the reason can be re-explained freely.
        #
        # Both are separate from procedure step 4, which pins the count mismatch as
        # one of four invalidating conditions. Step 4 says a mismatch invalidates;
        # only these say what to reconcile against and what a mismatch does and does
        # NOT establish. Deleting this paragraph leaves step 4 word-for-word intact.
        "report-count-reconciliation",
        "nothing tells holly to compare the declared count against what arrived, so a "
        "short-delivered report reads as a complete one",
        _CROSS_REVIEW,
        "The checklist declares a finding count per pass: reconcile it against the "
        "findings actually delivered.",
    ),
    (
        # The cause clause is load-bearing, not commentary: a mismatch has two
        # explanations (truncated transport, or a wrong count) and the symptom is
        # identical, so a rule naming only one of them asserts a cause it cannot
        # establish. The verdict does not depend on which — the clause and its
        # consequence stand or fall together, which is why they are one string.
        "count-mismatch-invalidates-the-report",
        "a short-delivered report is treated as a completed review, or its cause is "
        "asserted from a symptom that does not establish one",
        _CROSS_REVIEW,
        "A report declaring more findings than arrived is INVALID: it may have been "
        "truncated in transport, or the count may simply be wrong, and the mismatch "
        "does not tell you which. Either way it is not a completed review — "
        "re-dispatch.",
    ),
    (
        "same-fixer-same-branch",
        "a new title spawns a fresh worker with no memory of the task",
        _CROSS_REVIEW,
        "Route blocking issues back to the SAME fixer, on the SAME branch.",
    ),
    (
        # Task-branch diff base. The old anchor for this was a vocabulary tuple
        # whose middle pattern was ``worktree|main\.\.\.HEAD`` — it ACCEPTED the
        # hard-coded base it was supposed to forbid, and passed anyway on the
        # incidental word "worktree" that the corrected prose also contains. A
        # fixed string is the only decidable form for a rule of the shape "use X,
        # not Y": restoring Y removes X's text and fails.
        "task-diff-base-is-the-recorded-branch-point",
        "a task branched from a feature branch shows unrelated pre-existing work as "
        "part of its diff, and a repo with no `main` cannot be reviewed at all",
        _CROSS_REVIEW,
        "diff against the exact ref the task branched FROM, recorded when you created "
        "the worktree — `git -C .worktrees/<task_id> diff <base_ref>...HEAD`. Do not "
        "hard-code `main`",
    ),
    (
        # The same rule at its second site. Direct authoring has no task worktree,
        # so it carries its own command, and a revert there is invisible to the
        # pin above. This branch's recurring defect is a sweep that stops at the
        # first site, so the twin is pinned rather than assumed.
        "direct-authoring-diff-base-is-not-hard-coded-main",
        "prose holly authored itself is reviewed against the wrong range, or not "
        "reviewable in a repo without `main`",
        _CROSS_REVIEW,
        "then diff it against the ref it branched FROM — `git diff <base_ref>...HEAD`, "
        "not a hard-coded `main`",
    ),
    (
        # Same weakness as the diff base, found by the sweep the finding prompted:
        # the vocabulary anchor for this branch matched ``worktree``, a word the
        # rule contains whether it says a reused session KEEPS a worktree binding
        # or says it does NOT. The claim that just changed was therefore pinned by
        # a word that survives its own reversal.
        # Re-synced from the INSUFFICIENT form. "re-verify the branch on return" was
        # the whole check; a branch name matches whether the fixer committed or not,
        # and says nothing about the runner root. The rule now requires the same
        # three baselines fanout uses, taken FRESH each round, and the pin covers
        # all three plus the freshness — a stale baseline reused across rounds makes
        # a no-commit round look like a commit.
        #
        # Left OUT of the span: the two "without X you cannot Y" clauses that follow.
        # They explain why each baseline is needed, and the requirement above already
        # names all three explicitly, so excluding them costs nothing a reader acts
        # on. Deleting a reason alone therefore passes here — unlike fanout's
        # porcelain entry, where the reason IS the discriminator and stays pinned.
        "fix-round-carries-the-absolute-worktree-path",
        "a fix-round dispatch assumes a worktree binding that does not exist; a fixer "
        "that committed nothing is indistinguishable from one that did; and a fixer "
        "that edited the runner root instead is invisible",
        _CROSS_REVIEW,
        "It does NOT keep a worktree binding, because none exists: repeat the ABSOLUTE "
        "worktree path in every fix-round dispatch, and re-verify on return with the "
        "SAME baselines fanout uses, taken fresh before each round — the task "
        "worktree's HEAD and `git status --porcelain`, and the runner root's branch, "
        "HEAD and `git status --porcelain`, with the same already-dirty handling "
        "fanout describes when the root cannot be clean.",
    ),
    (
        # The already-open-PR revision range. Its vocabulary anchor names the
        # rationale words (``unreviewed``, ``published diff``) but never the
        # command, so restoring a review of the PUBLISHED diff — the defect the
        # rule exists to prevent — leaves every pattern satisfied.
        "open-pr-round-diffs-the-local-delta",
        "a fix round approves what is already on the remote and never sees the commit "
        "about to be added",
        _CROSS_REVIEW,
        "review the LOCAL delta that is about to be pushed — `git -C "
        ".worktrees/<task_id> diff origin/<branch>...HEAD`",
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
        # Found by the premises-vs-conclusion sweep: "Review authorises exactly one
        # commit" was pinned while the sentence saying NOTHING ENFORCES IT was not.
        # Deleting that sentence leaves the rule reading as a mechanism, which is the
        # D1a defect this bundle exists to avoid, and no test would have failed.
        "post-review-edit-rule-has-no-mechanism",
        "the one-commit rule reads as enforced, so holly stops checking HEAD and "
        "relies on a gate that does not exist",
        _CROSS_REVIEW,
        "There is no mechanism enforcing this; it holds only because you check HEAD "
        "before releasing.",
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
    # ── cross-file copies the widened premises sweep found unpinned ──
    #
    # Each of these is a rule stated in more than one bundle file where only some
    # copies were pinned. The round-8 sweep could not see them because it looked
    # only inside a pinned span's OWN paragraph in its OWN file, so a rule whose
    # premise or qualifier lived in a different file read as fully covered.
    (
        # The SKILL headline. Its body is pinned, but "shipped by Holly" lives only
        # in this sentence, and that qualifier IS the round-7 correction: without it
        # the claim overreaches into deployment-attached policies the bundle cannot
        # see. Deleting the headline leaves the pinned body intact.
        "no-holly-policy-gates-publication-headline",
        "the skill claims no policy at all gates publication, overreaching past "
        "Holly's own set into policies a deployment may have attached",
        _CROSS_REVIEW,
        "No policy shipped by Holly gates ordinary publication.",
    ),
    (
        # The root prompt's copy of the isolation claim plus the obligation it
        # implies. fanout's copy is pinned; this one is what holly carries every
        # turn, and it is the only place the two halves sit together.
        "root-prompt-isolation-is-not-a-guarantee",
        "the root prompt implies the runtime binds a worker to its worktree, so holly "
        "skips both the absolute path and the branch verification",
        _ROOT_CONFIG,
        "so worktree isolation is the worker obeying its instructions, not a runtime "
        "guarantee. Give an ABSOLUTE worktree path and VERIFY the resulting branch "
        "before accepting the work",
    ),
    (
        # The root prompt's copy of commit-and-stop. fanout's is pinned; this is the
        # one holly reads before it ever loads a skill.
        "root-prompt-implementer-commits-and-stops",
        "the root prompt lets an implementer publish its own branch, so review "
        "sequencing never happens and no skill is consulted to prevent it",
        _ROOT_CONFIG,
        "Each implementer commits to its own branch and STOPS — it does not push and "
        "does not open a PR.",
    ),
    # ── Smart Routing: the override, and what the readback does NOT establish ──
    #
    # The round that added this prose shipped with it pinned by nothing: the whole
    # override paragraph and the readback instruction could be deleted with every
    # test still green, because the only effective-vendor anchor pinned the generic
    # same-or-undeterminable STOP, which survives their deletion. The stop is the
    # conclusion; these are the premises that make it necessary, and a conclusion
    # whose premises are gone is prose nobody can act on.
    #
    # Pinned in BOTH carrying files. The root prompt is what holly holds every
    # turn; the cross-review skill is what it consults when dispatching a reviewer,
    # and the reviewer dispatch is the only place independence is decided.
    (
        # Replaces territory a pin never covered: last round's prose carried a Smart
        # Routing EXCEPTION to the roster rule ("a worker whose CLI is missing may
        # still run"), which turned out to be unreachable — the dispatch tool probes
        # PATH in the runner and fails there, before the server create route where
        # forced-auto lives. Nothing pinned the exception, so nothing had to be
        # deleted; what is pinned now is the corrected rule TOGETHER WITH the probe
        # ordering that makes it true, so the exception cannot come back unnoticed.
        # Re-synced. The claim was an assertion about probe ORDERING ("fails loud in
        # the runner, before the server create route"); it is now an OBLIGATION plus
        # an honest statement of what the probe does not cover. Pinning the ordering
        # was itself the defect this round is about — the prose no longer needs the
        # mechanism to carry the instruction, and neither does the pin.
        "missing-cli-is-always-disqualifying",
        "holly reasons its way to dispatching a worker whose CLI cannot launch, on the "
        "theory that some later stage will rescue it",
        _ROOT_CONFIG,
        "Treat a missing CLI as disqualifying and do not dispatch to that worker.",
    ),
    (
        # The obligation above without this note reads as backed by a probe that
        # covers everything. It does not: a fresh named create without a harness
        # override only. Pinned separately because deleting the scope leaves the
        # obligation looking mechanically guaranteed, which is the D1a shape.
        "cli-probe-scope-is-narrow",
        "the preflight probe is treated as complete coverage, so a launch failure "
        "arriving later as a failed turn reads as impossible",
        _ROOT_CONFIG,
        "Do NOT reason about whether some later stage might rescue it: the probe's "
        "scope is narrow — it covers a fresh named create without a harness override, "
        "and does not cover a continuation, an allowlisted per-dispatch override, or "
        "a harness that needs no CLI at all — so a launch failure can still surface "
        "as a failed turn rather than a dispatch error. The obligation stands either "
        "way.",
    ),
    (
        # The root prompt's acceptance criteria for a returned report, which had no
        # entry. It is the one place the ORCHESTRATOR decides whether a report counts,
        # so it has to agree with the mandate the reviewer was handed: [HONESTY] is
        # always applicable and never "n/a" there, and if this list omits it or lets
        # it be n/a, holly accepts a report the skill's own checklist rejects. Pinned
        # with the incomplete-review consequence, because an attestation list with no
        # consequence attached is a list holly can note and move past.
        "accepted-report-must-attest-every-pass-including-honesty",
        "holly accepts a report missing the HONESTY attestation — or accepts `n/a` for "
        "it — while the dispatched mandate says that pass always applies, so the two "
        "halves of the bundle disagree about what a complete review is",
        _ROOT_CONFIG,
        "Every accepted review report must attest FOCUSED, WIDE-1 blast-radius, "
        "WIDE-2 siblings, WIDE-3 input-domain, WIDE-4 coupled-artifacts, HONESTY — "
        'which is always applicable, never "n/a" — and the applicable document '
        "lenses. A missing attestation is an INCOMPLETE review, not a pass — "
        "re-dispatch the identical full mandate rather than accepting it.",
    ),
    (
        # `sys_list_models` is the tool holly would naturally trust to prove a model
        # will run. It does not: subscription and CLI-config rows are static. Pinned
        # because the false version of this claim ("the list proves the model runs")
        # is the same shape as the readback overclaim — a lookup treated as a
        # guarantee.
        "list-models-rows-need-their-verified-flag",
        "a static row is read as proof the model will run, so a dispatch is planned on "
        "a model that fails at launch",
        _ROOT_CONFIG,
        "`sys_list_models` lists candidates per worker, but subscription and "
        "CLI-config rows are static: check each row's `verified` flag rather than "
        "treating the list as proof a model will run.",
    ),
    (
        "smart-routing-overrides-model-and-harness",
        "vendor fixity is assumed to survive routing, so a `codex` reviewer routed "
        "onto the author's own vendor is reported as an independent review",
        _ROOT_CONFIG,
        "SMART ROUTING OVERRIDES BOTH, AND YOU MUST CHECK FOR IT. If the session you "
        "are running in has Smart Routing / Auto enabled, the server routes every "
        "worker session you create: it discards your `args.model` at creation and "
        "replaces the worker's declared harness with one it picks from `claude-sdk`, "
        "`codex`, `pi`. Native harnesses are excluded from that set, so a "
        "`claude_code` worker does NOT run claude-native and a `codex` worker does "
        "NOT run codex-native under routing, and `pi` may be moved to either. Your "
        "requested model is not recovered even if routing later fails. The "
        "consequence is that the roster's vendor fixity is NOT a property you can "
        "assume: a `codex` reviewer can be routed onto the same vendor as a "
        "`claude_code` author, which silently defeats cross-vendor independence.",
    ),
    (
        # The ASYMMETRY is the pinned content, not decoration. `sys_session_get_info`
        # returns STORED metadata, observes neither execution nor harness, and can be
        # stale — so a mismatch is proof of substitution while a match is only
        # CONSISTENT with independence. A span that kept the readback instruction and
        # dropped the asymmetry would pin an overclaim: holly would read a match as
        # confirmation and report an independence it never established. Both halves
        # therefore stay in one string.
        # Re-synced and SPLIT. The previous single entry pinned a conflated check
        # and the claim that a match was "consistent with independence" — which the
        # prose now rejects outright: a match is not evidence. Two entries, because
        # the two halves fail differently. This one is the pair of checks and what
        # each does and does not prove; the next is the limit that makes routed
        # independence unconfirmable.
        "readback-is-two-separate-checks",
        "the two comparisons are merged again into one that establishes neither — "
        "requested-vs-recorded cannot compare vendors, author-vs-reviewer cannot "
        "detect substitution",
        _ROOT_CONFIG,
        "After any dispatch you intend to rely on for independence, run TWO SEPARATE "
        "checks and do not conflate them. FIRST, substitution: compare the model you "
        "REQUESTED for a session against the RECORDED model `sys_session_get_info` "
        "returns for that same session. A mismatch is DIAGNOSTIC, not proof: the "
        "runner normalizes a model id before the server persists it, so a mismatch "
        "may be normalization rather than substitution. Investigate one; do not "
        "report it as substitution on its own. SECOND, vendor: compare the AUTHOR's "
        "recorded model against the REVIEWER's. "
        "Identical recorded models necessarily share a vendor, so that is a definite "
        "failure — but DIFFERENT recorded models are NOT proof of different vendors, "
        "since either value may be a default or a stale record rather than what "
        "executed.",
    ),
    (
        # The decisive half, and the reason the STOP exists at all: under routing
        # NOTHING available identifies the executing model, so independence is not
        # weakly supported — it is unconfirmable, and the instruction is to stop
        # rather than to compare. Pinned separately because deleting it leaves the
        # two checks above reading as sufficient.
        "routed-independence-is-unconfirmable",
        "holly performs a comparison that cannot settle the question and reports the "
        "result as an independent review",
        _ROOT_CONFIG,
        "Know the limit of both: `sys_session_get_info` returns only the STORED "
        "`model_override` / `llm_model`. It observes neither what executed nor the "
        "harness, and routing can reach the runner after persistence has failed. So "
        "when Smart Routing is ON, nothing available to you identifies the model that "
        "actually ran, and routed independence is UNCONFIRMABLE: STOP and say so. "
        "Never report a cross-vendor review you cannot establish.",
    ),
    (
        # Same override, stated at the dispatch site. Shorter than the root-prompt
        # copy and carrying the operative half of vendor fixity — that neither the
        # NAME nor the requested MODEL establishes what ran — so no separate fixity
        # entry is needed for this file.
        "smart-routing-defeats-name-and-requested-model",
        "the reviewer dispatch treats the worker name or the model it asked for as "
        "evidence of vendor, at the one point where independence is decided",
        _CROSS_REVIEW,
        "Under Smart Routing the server discards your `args.model` and re-picks the "
        "harness from `claude-sdk`/`codex`/`pi`, so neither the worker name nor the "
        "model you asked for establishes the vendor that actually ran.",
    ),
    (
        # The skill's readback, with the same asymmetry kept whole for the same
        # reason. This copy also names the harness gap explicitly ("exposes no
        # harness"), which the root-prompt copy words differently.
        # The dispatch-site copy of the same split, re-synced for the same reason.
        "readback-is-two-separate-checks-at-the-dispatch",
        "the two comparisons are merged at the one place independence is decided, so "
        "neither substitution nor vendor is actually established",
        _CROSS_REVIEW,
        "Two separate checks, not one: requested-versus-recorded for a SINGLE session "
        "detects substitution, and author-recorded-versus-reviewer-recorded compares "
        "vendors. Identical recorded models necessarily share a vendor and are a "
        "definite failure; different recorded models prove nothing, since either may "
        "be a default or a stale record rather than what executed.",
    ),
    (
        "routed-independence-is-unconfirmable-at-the-dispatch",
        "a routed review is reported as independent on the strength of a comparison "
        "that cannot identify what executed",
        _CROSS_REVIEW,
        "`sys_session_get_info` reports stored metadata only, exposes no harness, and "
        "routing can reach the runner after persistence fails — so under routing the "
        "executing model is unidentifiable and independence is UNCONFIRMABLE. Stop "
        "and say so rather than reporting a review you cannot establish.",
    ),
    (
        # The declared-vs-runtime distinction, pinned in the root prompt because the
        # e2e harness test docstring now cites exactly this sentence for what its
        # assertion does and does not establish.
        "harness-fixity-holds-only-absent-routing",
        "declared configuration is read as runtime fact, which is the error the "
        "routing investigation found in three places",
        _ROOT_CONFIG,
        "ABSENT Smart Routing, `claude_code` and `codex` run their declared native "
        "harness and `pi` runs ANY gateway model; UNDER routing none of the three is "
        "fixed, per the caveat above.",
    ),
    # ── fanout: dispatch isolation and the verification that stands in for it ──
    #
    # This file had NO entry in either table until now — the only bundle file with
    # no coverage at all — and it carries the rules that exist BECAUSE
    # ``sys_session_send`` provides no worktree binding. Every one of them is a
    # claim about what the runtime does or does not do, which is the class this
    # bundle keeps getting wrong, so all of them are fixed strings rather than
    # vocabulary.
    (
        # The honesty claim for this file, and pinned first because everything
        # below exists only if it is true. Its reversal — "the worktree is a
        # binding" — is the false-mechanism claim in its purest form.
        "worktree-isolation-is-instruction-following",
        "a binding is assumed where none exists, so nothing downstream is verified",
        _FANOUT,
        "Isolation here is instruction-following, not a binding.",
    ),
    (
        # Operative instruction only. The three runtime facts that follow it
        # (no workspace parameter, ``workspace=None``, cwd is the runner root)
        # explain WHY and are left out: they can be re-explained without changing
        # the obligation, and the obligation is what a dispatch either carries or
        # does not.
        "dispatch-carries-the-absolute-worktree-path",
        "the child starts in the runner root and edits, tests and commits against "
        "the wrong checkout",
        _FANOUT,
        "give the ABSOLUTE worktree path rather than a relative one",
    ),
    (
        # Re-synced: the scheme gained task-side porcelain, so it is no longer three
        # values. Renamed off the count, which was never the point.
        "baselines-recorded-before-dispatch",
        "there is nothing to compare a returning worker against, so contamination is "
        "undetectable after the fact; and gates that passed on uncommitted task-side "
        "changes passed on work absent from the diff",
        _FANOUT,
        "baselines taken BEFORE dispatch: the task worktree's HEAD and `git status "
        "--porcelain`, and the runner root's branch, HEAD and `git status "
        "--porcelain`.",
    ),
    (
        # The PRECONDITION, pinned separately because it is what makes the baselines
        # mean anything: against an already-dirty path every recorded value stays
        # byte-identical while the path is altered underneath. Deleting this leaves a
        # performable scheme that discriminates nothing — the exact defect the round
        # that added it was fixing.
        # Re-synced: a hard requirement became a preference with two workable
        # alternatives, because a legitimately dirty runner root is common and
        # refusing the task over it is worse than handling it. The span covers the
        # preference AND both alternatives — dropping the alternatives leaves a
        # preference holly cannot satisfy and will therefore ignore.
        "baselines-prefer-clean-and-handle-a-dirty-root",
        "the baselines are taken against an already-dirty path with no compensation, "
        "so a worker can alter it while every recorded value stays byte-identical — or "
        "holly refuses a task over a dirty root",
        _FANOUT,
        "Prefer both worktrees clean. When the runner root is legitimately dirty — "
        "which is common and is not grounds to refuse the task — additionally record a "
        "content hash of each already-dirty path and compare those too, or run the "
        "orchestration from a dedicated clean checkout rather than disturbing the "
        "human's.",
    ),
    (
        # Reason kept INSIDE the span here, unlike the absolute-path entry. The
        # reason is the discriminator: without "leaves its branch and HEAD
        # untouched" there is nothing to say why the other two baselines are
        # insufficient, and porcelain reads as belt-and-braces that a later edit
        # can drop.
        "porcelain-catches-uncommitted-runner-root-edits",
        "a worker that edited the runner root without committing passes verification, "
        "because branch and HEAD both still match",
        _FANOUT,
        "Porcelain is not optional here — a worker that edited the runner root "
        "without committing leaves its branch and HEAD untouched.",
    ),
    (
        "task-head-must-have-moved",
        "a worker that committed nothing is accepted on its own report",
        _FANOUT,
        "its `rev-parse HEAD` has MOVED from the recorded task baseline, since an "
        "unchanged HEAD means no commit was made whatever the worker reported",
    ),
    (
        # A negative capability claim: this tool CANNOT do the job. Pinned because
        # its reversal invites holly to swap the real verification for a call that
        # returns ``null`` on this path and reads as a pass.
        "session-get-info-cannot-verify-the-worktree",
        "verification is swapped for a call that always returns null here, which "
        "looks like confirmation",
        _FANOUT,
        "`sys_session_get_info` cannot substitute for this: it reports the child's "
        "persisted `workspace` and `git_branch`, and on this dispatch path both are "
        "always `null`.",
    ),
    (
        # The worker configs carry "Do NOT push and do NOT open a PR." as a
        # canonical instruction, but that is the worker's copy in a different file.
        # This is holly's own copy in the dispatch procedure, and deleting it here
        # leaves the orchestrator with no rule about what it dispatches for.
        "fanout-worker-commits-and-stops",
        "an implementer publishes its own branch and holly's review sequencing never happens",
        _FANOUT,
        "The worker drives the task to green, commits, and STOPS — it does not push "
        "and does not open a PR.",
    ),
)

# Anchoring here is per block and per owning file. Per block, because scattering
# the vocabulary across a document would satisfy a whole-file search. Per owning
# file, because the prose deliberately restates several rules in more than one
# place — good redundancy, but the restatement is not the procedure, so deleting
# the procedural step must fail even while the restatement lives.
_LIFECYCLE_ANCHORS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "unopened-branch-diff-is-committed-and-unpushed",
        _CROSS_REVIEW,
        "the reviewer is handed an uncommitted or already-published state",
        (r"implementer has committed and stopped", r"nothing is pushed"),
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
