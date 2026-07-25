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

Every assertion that remains is decidable — exact comparison, set equality, or
presence of a fixed string (sometimes one of a fixed set of alternatives).
Nothing here classifies an unseen sentence.

The presence checks have one honest caveat, which is churn rather than
misjudgement: they require particular text to BE there, so rewording a mandate
axis or a procedure step fails this file until the expected text is updated.
They never approve or reject a sentence they have not been told about, which is
the property the removed scanners lacked.

WHAT THAT COSTS — the full list, not a summary
----------------------------------------------
1. A newly introduced FALSE ENFORCEMENT CLAIM is caught by no test, in any
   phrasing: ``The blast_radius policy blocks every push until review lands.``
   ``The policy will not allow a push before review.``  ``Every push is gated by
   the policy layer.``  ``The review step is mandatory and automatic.``
   ``There is a gate. It stops every push.``
2. The same, INSIDE THE WORKER CONFIGS specifically — the files workers read,
   and where the predecessor defect actually did its damage.
3. PR-FIRST ORDERING is no longer detected. ``Push the branch and open its PR
   first; run cross-review afterward.`` added to any file passes.
4. MARKER-BEFORE-GATE is no longer detected. ``Set READY on the registry before
   gates`` passes; nothing asserts that readiness follows the gate.
5. SAME-VENDOR / SKIPPED-REVIEW EXCEPTIONS are no longer detected. ``A
   same-vendor review may suffice.`` added to the root prompt passes, and so
   does a permission to merge.
6. The worker-obligation check proves only that the canonical instruction is
   PRESENT. It does not prove that a CONTRADICTING instruction is absent: a
   prompt keeping ``Do NOT push and do NOT open a PR.`` while adding ``Push the
   branch before reporting.`` passes.
7. The banned-token check is an unconditional ban on three spellings, so it
   also rejects a truthful sentence that names one of them. That is a
   deliberate reserved-word rule, not a claim about meaning.

All seven now depend on review rather than on CI.
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


def test_reviewer_mandate_axes_state_their_requirements(holly_spec: AgentSpec) -> None:
    """
    Each WIDE axis is bound to its number in both places AND still states what
    it requires.

    Renaming an axis heading is one defect; gutting its body is the same defect
    one level down. Keep the heading and the checklist label, replace the body
    with "mention the input domain and report clear", and the reviewer receives
    a pass name with no obligation attached — every finding it would have
    produced disappears while the battery checklist still reports
    "[WIDE-3] run — clear".
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
# This list is an ENUMERATION, not a claim of completeness: it pins the branches
# named below, each checked by deleting the block that carries it. A branch not
# listed here is not protected.
#
# Anchoring is per block and per owning file. Per block, because scattering the
# vocabulary across a document would satisfy a whole-file search. Per owning
# file, because the prose deliberately restates several rules in more than one
# place — good redundancy, but the restatement is not the procedure, so deleting
# the procedural step must fail even while the restatement lives.
#
# Like the axis requirements, these are PRESENCE checks over a fixed vocabulary:
# decidable, and they say nothing about whether a block means the right thing.
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
        "a fix round approves what is already published and misses the new commit",
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
        "red-gate-returns-to-the-fixer",
        _CROSS_REVIEW,
        "reviewer tokens get spent on defects a validator already names",
        (r"\bred\b", r"fixer|back", r"before|first|zero reviewer"),
    ),
    (
        "pytest-ground-truth",
        _CROSS_REVIEW,
        "a parametrized suite is mislabelled as an over-report",
        (r"collect-only", r"grep -c|def test_", r"parametriz|collected"),
    ),
    (
        "effective-vendor-independence",
        _CROSS_REVIEW,
        "a pi reviewer on the author's own model reports independence it lacks",
        (r"vendor", r"\bmodels?\b", r"differ", r"stop|escalate|cannot|do not"),
    ),
    (
        "mandate-pasted-verbatim",
        _CROSS_REVIEW,
        "the dispatched mandate is paraphrased and its obligations quietly shrink",
        (r"verbatim", r"delimiter", r"input|unedited|included"),
    ),
    (
        "dispatch-in-the-same-turn",
        _CROSS_REVIEW,
        "a turn that only announces the review stalls the whole run",
        (r"same turn", r"announce|intent|stalls", r"inbox|dispatch"),
    ),
    (
        "report-validation",
        _CROSS_REVIEW,
        "an incomplete report, or a verdict despite a refusal, reads as a pass",
        (r"INCOMPLETE DISPATCH", r"re-?dispatch", r"identical|full"),
    ),
    (
        "same-fixer-same-branch",
        _CROSS_REVIEW,
        "a new title spawns a fresh worker with no memory of the task",
        (r"\bsame\b", r"fixer|implementer", r"branch|worktree", r"title|session_id"),
    ),
    (
        "blocking-issues-logged-as-registry-fix-tasks",
        _CROSS_REVIEW,
        "findings are tracked only in the turn that produced them",
        (r"registry", r"fix-task|log", r"worktree|scoped"),
    ),
    (
        "class-closure-recurrence-stop",
        _CROSS_REVIEW,
        "point-fixing continues while the same defect class recurs at new sites",
        (r"\bclass\b", r"\bstop\b", r"one-off|enumerat", r"zero remaining|every instance"),
    ),
    (
        "post-review-edit-invalidates-verdict",
        _CROSS_REVIEW,
        "a 'tiny' follow-up ships on a verdict that never saw it",
        (r"invalidat|authoris|authoriz|verdict", r"\bafter\b", r"HEAD|again|re-?run"),
    ),
    (
        "release-requires-green-and-zero-blocking",
        _CROSS_REVIEW,
        "the PR opens on red gates or on unresolved blocking findings",
        (r"green", r"zero blocking|no blocking", r"push|PR"),
    ),
    (
        "release-records-registry-readiness",
        _CROSS_REVIEW,
        "the human is never handed a PR URL or told it is ready",
        (r"registry", r"PR URL|record", r"ready|human"),
    ),
    (
        "direct-authoring-pr-ownership",
        _CROSS_REVIEW,
        "prose holly wrote has no one responsible for publishing it",
        (r"director|directly", r"holly", r"push|open"),
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
            r"no different-vendor|cannot run independent|reviewer is available",
            r"\bstop\b",
            r"human|escalate",
            r"unreviewed|do not open|not open the PR",
        ),
    ),
    (
        "unsatisfiable-contract-escalation",
        _CROSS_REVIEW,
        "the fix loop spins forever instead of returning to the human",
        (r"contract", r"cannot be satisfied|a few loops", r"escalate|stop"),
    ),
    (
        "never-degrade-to-same-vendor",
        _CROSS_REVIEW,
        "review quietly downgrades to same-vendor or skipped instead of stopping",
        (r"never", r"same-vendor|same vendor", r"skip|degrade"),
    ),
    (
        "bot-sweep-uses-a-timer",
        _CROSS_REVIEW,
        "holly busy-polls a bot that posts on its own lag",
        (r"timer", r"sweep|lag", r"polling|genuine|delay"),
    ),
    (
        "bot-findings-clustered-by-class",
        _CROSS_REVIEW,
        "the same class is point-fixed once per comment instead of closed",
        (r"cluster", r"\bclass\b", r"before fixing|before"),
    ),
    (
        "bot-findings-never-narrow-the-mandate",
        _CROSS_REVIEW,
        "a bot's comment list becomes the review scope and the battery shrinks",
        (r"bot|findings", r"narrow|confirm these", r"mandate"),
    ),
    (
        "every-fix-diff-is-reviewed",
        _CROSS_REVIEW,
        "fix rounds ship unreviewed because the first round was reviewed",
        (r"every fix|fix diff", r"same", r"review"),
    ),
    (
        "reply-in-thread",
        _CROSS_REVIEW,
        "replies detach from the finding and resolution state cannot be read",
        (r"repl", r"in-?thread", r"top-level|new"),
    ),
    (
        "repeated-class-hard-stop",
        _CROSS_REVIEW,
        "a recurring class keeps being point-fixed during bot servicing",
        (r"repeated class", r"stop", r"whole-surface|escalate"),
    ),
    (
        "unresolved-thread-count-on-handoff",
        _CROSS_REVIEW,
        "the human merges believing replies established resolution",
        (r"unresolved", r"thread", r"count|report"),
    ),
    (
        "handoff-wording-implies-no-completeness",
        _CROSS_REVIEW,
        "'findings serviced' is read as 'threads resolved' and the human merges",
        (r"serviced", r"resolved", r"complet|not|≠"),
    ),
    (
        "never-declares-a-clean-bill",
        _CROSS_REVIEW,
        "the orchestrator issues the sign-off that is the human's to give",
        (r"clean bill", r"never"),
    ),
    (
        "direct-authoring-not-exempt",
        _ROOT_CONFIG,
        "prose holly wrote itself reaches the remote without an independent review",
        (
            r"authored directly|directly[- ]author|you author",
            r"review",
            r"exemption|exactly like|still owes|before",
        ),
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
    """
    texts = _orchestration_files()
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
