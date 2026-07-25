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

WHAT THIS FILE DELIBERATELY NO LONGER DOES
------------------------------------------
Earlier revisions carried a general natural-language scanner that tried to
detect any claim that some mechanism blocked publication. It was rebuilt three
times and failed in both directions each time:

* false negatives — ``The policy, not the reviewer, blocks every push.`` passed,
  because a negation anywhere inside a matched claim exempted the whole claim;
  ``If no reviewer is available, accept a same-vendor review.`` passed because
  the verb was missing from an allowlist.
* false positives — it rejected ``Review is a requirement before publishing.``
  and ``blast_radius blocks force-pushes while allowing ordinary
  publication.``, both true, and the first the kind of sentence this file's own
  rules called legal.

The false positives are the worse half: a check that fails true sentences
pressures authors into indirect wording, degrading exactly the prose the
honesty term exists to protect. After four rounds the residual kept moving
rather than shrinking, so the general scanner is REMOVED rather than weakened.

What remains here is only what is decidable: literal tokens naming mechanisms
that do not exist, a prohibition on PR-first ordering, and the worker-file
checks. Judging whether a sentence overstates enforcement is now a REVIEWER'S
question, asked per diff, not a regex asked per commit.

The cost is real and is not hedged. No test in this repo now catches a newly
introduced false enforcement claim, in any phrasing — for example::

    The blast_radius policy blocks every push until review lands.
    The policy will not allow a push before review.
    Every push is gated by the policy layer.
    The review step is mandatory and automatic.
    There is a gate. It stops every push.

Each of those is false about this bundle, each would have been caught by the
removed scanner, and each now depends entirely on review. That is the trade:
the scanner also rejected true sentences, and a check that fails true sentences
pushes authors away from writing plainly, which costs more than it saves.
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


# ───────────────────────────── text helpers ─────────────────────────────
#
# These flatten layout so a rule cannot be evaded by reformatting. They do NOT
# attempt to understand claims — see the module docstring.

_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s")
_BULLET_PREFIX = re.compile(r"^(?:[-*]|\d+\.)\s+")
_SENTINEL = "\x00"

# Used only by the narrow checks below, as a short adjacency test: is this
# instruction immediately prohibited? It is not a negation-scope model.
_PROHIBITION = re.compile(r"\b(not|never|no|nor)\b|n't", re.IGNORECASE)


def _normalized(text: str) -> str:
    """
    Flatten hard wraps, list markers and comment markers into one string.

    :param text: Raw file contents.
    :returns: Normalized text with sentinels at former item boundaries.
    """
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            parts.append(_SENTINEL)
            continue
        if _BULLET.match(line):
            parts.append(_SENTINEL)
            stripped = _BULLET_PREFIX.sub("", stripped)
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        parts.append(stripped)
    return " ".join(parts)


def _segments(text: str) -> list[str]:
    """
    Unwrap prose into one segment per paragraph or list item.

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


def _unprohibited(text: str, pattern: re.Pattern[str], window: int = 30) -> list[str]:
    """
    Find matches that no prohibition word immediately precedes.

    The window is short and local on purpose: this answers "is this instruction
    prohibited right here", not "what does this sentence mean". "Do NOT push and
    do NOT open a PR" is prohibited; a publication instruction with no nearby
    prohibition is not.

    The lookback stops at the start of the current sentence or list item.
    Without that bound it reached into the PREVIOUS sentence and a "never"
    there exempted a fresh instruction in the next one — which is how four
    publication mutations passed.

    :param text: Raw file contents.
    :param pattern: Compiled pattern for the instruction being looked for.
    :param window: Characters to look back for a prohibition word.
    :returns: Quoted excerpts for each unprohibited match.
    """
    normalized = _normalized(text)
    hits: list[str] = []
    for match in pattern.finditer(normalized):
        boundary = max(
            normalized.rfind(char, 0, match.start()) for char in (".", "!", "?", _SENTINEL)
        )
        back = normalized[max(boundary + 1, match.start() - window) : match.start()]
        if _PROHIBITION.search(back):
            continue
        excerpt = normalized[max(0, match.start() - 40) : match.end() + 40]
        hits.append(excerpt.replace(_SENTINEL, " | ").strip())
    return hits


# Literal tokens that name a mechanism which does not exist. No context makes
# them honest, so no interpretation is needed to reject them.
#
# ``require_pr_review`` — a predecessor design named exactly this policy as the
#   thing blocking ``git push``. It exists in no builtin and was never
#   evaluated; its return is that defect coming back.
# ``review-passed`` / ``review_passed`` — a machine-checked "review passed" flag
#   that publication is conditioned on. No such state is recorded or read.
_BANNED_TOKENS: tuple[tuple[str, str], ...] = (
    ("require_pr_review", "names a policy that does not exist and is never evaluated"),
    ("review-passed", "implies a machine-checked gate state that is never recorded"),
    ("review_passed", "implies a machine-checked gate state that is never recorded"),
)

# Publication instructions, by effect. Used for the worker prohibition and the
# PR-first ordering prohibition.
_PUBLICATION = re.compile(
    r"\b(push|pushes|pushed|pushing|publish|publishes|published|publishing|ship|ships|"
    r"shipped|shipping|land|lands|landed|landing|upload|uploads|uploading)\b"
    r"[^.!?;:—]{0,40}?"
    r"\b(branch|commit|commits|code|diff|work|change|changes|it|them|upstream|remote|"
    r"origin|PR|pull request)\b"
    r"|\b(open|opens|opening|create|creates|creating|file|files|raise|raises)\b"
    r"[^.!?;:—]{0,25}?\b(PR|pull request)\b"
    r"|\bgit push\b|\bgh pr (?:create|merge)\b",
    re.IGNORECASE,
)

# Grouped before concatenation: _PUBLICATION is a top-level alternation, so
# appending a suffix directly would bind it to the LAST alternative only and
# every other alternative would match on its own.
_PUB = f"(?:{_PUBLICATION.pattern})"

# PR-first ordering: a publication instruction marked as coming BEFORE review.
# Three literal orderings, not a general reading of the sentence.
_PR_FIRST: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "publication marked as the first step",
        re.compile(_PUB + r"[^.!?]{0,60}?\bfirst\b", re.IGNORECASE),
    ),
    (
        "publication ordered before review",
        re.compile(
            _PUB + r"[^.!?]{0,60}?\bbefore\b[^.!?]{0,40}?\brevie\w+\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review deferred until after publication",
        re.compile(
            _PUB + r"[^.!?]{0,80}?\b(then|afterwards?|later)\b[^.!?]{0,40}?\brevie\w+\b",
            re.IGNORECASE,
        ),
    ),
)

# Exception wordings ratified as forbidden. A short phrase blocklist, NOT a
# semantic check: it rejects these exact permissions and nothing else. Honest
# prose stays legal because the prohibition forms ("is never acceptable",
# "never accept") do not contain these phrases adjacently.
_BANNED_EXCEPTION_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "permits a same-vendor review",
        re.compile(
            r"\b(same[- ]vendor|same model)\b[^.!?;:—]{0,30}?"
            r"\b(review|reviewer)\b[^.!?;:—]{0,20}?"
            r"\b(is|are)\s+(acceptable|allowed|permitted|fine|ok|okay|enough|sufficient)\b"
            r"|\b(accept|allow|permit|use|fall back to|settle for)\b[^.!?;:—]{0,25}?"
            r"\b(same[- ]vendor|same model)\b[^.!?;:—]{0,20}?\b(review|reviewer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "permits skipping review",
        re.compile(
            r"\b(skip|skipping|forgo|forego|omit|waive)\b[^.!?;:—]{0,25}?"
            r"\b(the )?revie\w+\b",
            re.IGNORECASE,
        ),
    ),
    (
        "permits holly to merge",
        re.compile(
            r"\b(holly|you)\b[^.!?;:—]{0,25}?\b(may|can|should|must)\b[^.!?;:—]{0,15}?\bmerge\b",
            re.IGNORECASE,
        ),
    ),
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

    assert set(
        policies["headless_subagent_purpose_guard"].function.arguments["allowed_purposes"]
    ) == {"implement", "review", "explore", "search"}


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


def test_no_file_names_a_mechanism_that_does_not_exist() -> None:
    """
    No bundle file uses a banned literal token.

    This is what survives of the removed claim scanner, and it survives because
    it needs no interpretation: each token names a specific mechanism that is
    absent from the codebase, so there is no context in which writing it is
    honest. Everything softer — whether a sentence overstates what a policy
    does — is now a reviewer-checklist question, for the reasons in the module
    docstring.
    """
    for rel_path, text in _bundle_files().items():
        lowered = text.lower()
        for token, why in _BANNED_TOKENS:
            assert token not in lowered, f"{rel_path}: banned token {token!r} — {why}"


def test_no_file_orders_publication_before_review() -> None:
    """
    No file instructs publishing before review — a whole-parcel PROHIBITION.

    Anchoring only that the correct ordering appears SOMEWHERE is not enough:
    adding "Push the branch and open its PR first; run cross-review afterward."
    to the root config left the correct ordering intact everywhere else and
    every other assertion green, while inverting the sequence holly actually
    follows. Review-before-push is the entire product, and a PR opened first
    can never be un-opened.

    Narrow by construction: three literal orderings of a publication
    instruction relative to review (marked "first", placed "before review", or
    with review deferred to "then"/"afterward"). It does not judge whether a
    sentence is otherwise accurate.
    """
    for rel_path, text in _bundle_files().items():
        for label, pattern in _PR_FIRST:
            violations = _unprohibited(text, pattern)
            assert not violations, (
                f"{rel_path}: {label} — ...{violations[0]}... Review runs on the "
                f"local branch diff BEFORE anything reaches the remote; a PR "
                f"opened first cannot be un-opened."
            )


def test_no_file_grants_a_banned_exception() -> None:
    """
    No file grants an exception the ratified rules forbid.

    The review rules have parallel live consumers — cross-review is the
    playbook, the root prompt restates them, and holly acts on whichever it is
    reading — so a permission added to one file quietly wins wherever it is read
    while the other file's hard stop stays on the page looking authoritative.

    This is a short phrase blocklist, not a semantic check. It rejects the
    ratified-forbidden wordings (permitting a same-vendor review, permitting a
    skipped review, permitting holly to merge) and nothing else. Stating the
    prohibitions stays legal because the prohibition forms do not contain these
    phrases adjacently.
    """
    for rel_path, text in _orchestration_files().items():
        for label, pattern in _BANNED_EXCEPTION_PHRASES:
            violations = _unprohibited(text, pattern, window=15)
            assert not violations, (
                f"{rel_path}: {label} — ...{violations[0]}... The rule is stated in "
                f"more than one live consumer; an exception here overrides the "
                f"other file wherever holly happens to read this one."
            )


# What every worker must be told. The IMPLEMENT half is the positive contract —
# "commit and stop" is a two-part instruction, and dropping either half is
# invisible to a check that only looks for publication. The REVIEW half is the
# dispatch-integrity taxonomy: a reviewer that does not know what a complete
# dispatch contains cannot detect an incomplete one, and the whole truncation
# defence rests on it refusing rather than guessing.
#
# Matched with PROXIMITY, not keyword presence: "commit" and "branch" both
# survive a prompt that tells the worker to leave its worktree uncommitted, so
# an obligation checking only for the two words passed that edit.
_NEAR = r"[^.!?;:—]"
_NEAR_COLON = r"[^.!?;—]"

_WORKER_OBLIGATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "commit-to-its-branch",
        "an uncommitted worktree leaves nothing for the orchestrator to review",
        (rf"\bcommit\b{_NEAR}{{0,40}}?\b(?:task )?branch\b",),
    ),
    (
        "report-and-stop",
        "a worker that keeps going after reporting drifts outside its scope",
        (rf"\breport\b{_NEAR_COLON}{{0,90}}?\bstop\b",),
    ),
    (
        "co-sign-commits",
        "commits lose the trailer that marks them as agent-authored",
        (r"co-?authored-by|co-?sign",),
    ),
    (
        "drive-to-green",
        "the orchestrator gets a diff that was never run",
        (rf"\bgreen\b{_NEAR_COLON}{{0,90}}?\b(lint|typecheck)\b",),
    ),
    (
        "exact-test-command-and-file-set",
        "reported counts cannot be reconciled against the same gate",
        (
            rf"exact command{_NEAR}{{0,40}}?file set",
            rf"collected{_NEAR}{{0,40}}?test function",
        ),
    ),
    (
        "dispatch-requires-both-delimiters",
        "a truncated mandate is indistinguishable from a complete one",
        (rf"\bboth\b{_NEAR}{{0,30}}?delimiter",),
    ),
    (
        "dispatch-requires-diff-and-contract",
        "the reviewer judges a diff against a contract it never received",
        (rf"\bdiff\b{_NEAR}{{0,40}}?\bcontract\b",),
    ),
    (
        "dispatch-requires-checklist",
        "the battery can be silently narrowed at dispatch time",
        (r"\b(checklist|battery)\b",),
    ),
    (
        "malformed-dispatch-handling",
        "a partial dispatch gets treated as a complete one",
        (rf"absent{_NEAR}{{0,60}}?(truncated|malformed)",),
    ),
    (
        "incomplete-dispatch-banner",
        "holly cannot detect the refusal it is told to check for",
        (r"INCOMPLETE DISPATCH",),
    ),
    (
        "best-effort-evidence-on-refusal",
        "a refusal returns nothing usable and the round is wasted",
        (rf"best-?effort{_NEAR}{{0,30}}?evidence",),
    ),
    (
        "no-verdict-on-incomplete",
        "a verdict issued on a partial mandate reads as a pass",
        (r"\bno verdict\b",),
    ),
    (
        "review-is-report-only",
        "a reviewer edits the diff it is judging and independence is gone",
        (r"report ONLY|review and report",),
    ),
    (
        "explore-is-read-only",
        "a read-only task mutates the repo",
        # The section HEADING says "read-only" too, so bind to the instruction.
        (
            r"\b(edit nothing|change nothing|make no edits|do not edit|edits? nothing)\b",
            r"file:line|evidence",
        ),
    ),
)


def test_workers_commit_and_stop(holly_spec: AgentSpec) -> None:
    """
    Each worker is told to commit and stop, carries the dispatch-integrity
    taxonomy, and is never told to publish.

    The negative half alone was evadable: changing "commit to your task branch"
    to "leave the worktree uncommitted" instructs no publication at all and
    passed cleanly, while destroying the thing holly reviews. So the positive
    contract is asserted directly, and so is the REVIEW taxonomy — both
    delimiters, the diff, the contract, the checklist, what counts as malformed,
    the ``INCOMPLETE DISPATCH`` banner, best-effort evidence, and no verdict.

    Asserted on parsed ``instructions`` rather than raw YAML so the file's
    explanatory comments are not mistaken for instructions to the model.
    """
    by_name = {a.name: a for a in holly_spec.sub_agents}
    for name in _WORKERS:
        instructions = by_name[name].instructions or ""
        assert instructions.strip(), f"{name}: no instructions at all"

        for obligation, consequence, patterns in _WORKER_OBLIGATIONS:
            for pattern in patterns:
                assert re.search(pattern, instructions, re.IGNORECASE), (
                    f"{name}: worker no longer carries {obligation!r}. Without it: {consequence}."
                )

        violations = _unprohibited(instructions, _PUBLICATION)
        assert not violations, (
            f"{name}: publication instruction with no prohibition beside it — "
            f"...{violations[0]}... Workers commit and stop; holly releases the "
            f"branch after gates and an independent review."
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


# Readiness must FOLLOW the gate. Anchoring "green release" and "registry
# readiness" as two independent facts let "Before gates run, mark the registry
# entry ready" pass — both facts were still present somewhere. The ratified
# relationship is an ordering, so the ordering is what gets asserted.
_READINESS = re.compile(
    r"\bmark\b[^.!?]{0,40}?\bready\b|\bregistry\b[^.!?]{0,60}?\bready\b", re.IGNORECASE
)
# The gate is stated two ways across the bundle — cross-review names the gates
# and the finding count, fanout names the review outcome. Both are the same
# precondition, so both count.
_GATE_CONDITION = re.compile(
    r"\bgreen\b[^.!?]{0,60}?\b(zero|no) blocking\b"
    r"|\b(zero|no) blocking\b[^.!?]{0,60}?\bgreen\b"
    r"|\brevie\w+\b[^.!?]{0,60}?\b(passes|passed|is clean|clean|green)\b",
    re.IGNORECASE,
)


def test_registry_readiness_follows_the_gate() -> None:
    """
    Every block that marks the registry ready states the gate condition first.

    Readiness is the signal the human acts on. If a block can declare a task
    ready without the green-gates-and-zero-blocking precondition preceding it,
    then "ready" stops meaning "reviewed" — and the human merges on that word.
    Asserted as a relationship rather than as two independent anchors, because
    both anchors survive an edit that simply moves readiness earlier.
    """
    for rel_path, text in _orchestration_files().items():
        for block in _segments(text):
            readiness = _READINESS.search(block)
            if not readiness:
                continue
            gate = _GATE_CONDITION.search(block)
            assert gate is not None, (
                f"{rel_path}: a block marks the registry ready without stating the "
                f"green-gates-and-zero-blocking precondition — {block[:120]!r}"
            )
            assert gate.start() < readiness.start(), (
                f"{rel_path}: readiness is marked before the gate condition — "
                f"{block[:120]!r}. Readiness must follow the gate, or 'ready' "
                f"stops meaning 'reviewed'."
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
