"""Structural test for the holly coding-orchestrator bundle (examples/holly).

holly is a claude-sdk orchestrator brain that delegates every coding task to
its ``claude_code`` / ``codex`` / ``pi`` workers, runs an independent
different-vendor review against the LOCAL branch diff, and only then sequences
publication. Pure spec-load — no LLM, no network, no credentials beyond a dummy
``GITHUB_TOKEN`` the MCP header interpolation requires.

Publication ordering here is prompt discipline, not enforcement: nothing in the
runtime blocks a ``git push``. That makes the bundle's PROSE the primary attack
surface rather than a secondary one. A worker or a human acting on a claim they
cannot verify is holly's whole failure mode — a predecessor design's workers
complied with a gate that never existed, and no policy had to change for that
to happen. So false prose is treated here as the harm itself, not as a hint
that some config might also be wrong.

Tests are organised by ratified contract term, and each assertion exists
because some cheap edit made a bundle sentence false while the rest of the
suite stayed green:

- D1  — no enforcement policy, and no policy ARGUMENT that creates enforcement
- D1a — no false claim of enforcement anywhere in the bundle
- D2  — the reviewer mandate is delimited, and each pass states its requirements
- D3  — the enumerated review-lifecycle branches survive
- D4  — workers commit and stop, and carry the full dispatch-integrity taxonomy
- D5  — external-review-bot servicing keeps its honesty semantics
- D6  — directly-authored work is not exempt from review
- consistency — no file grants an exception another file forbids
- the three-worker roster, whose independence rests on executor shape
- the read-only github MCP surface
"""

from __future__ import annotations

import importlib
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

_CROSS_REVIEW = "skills/cross-review/SKILL.md"
_ROOT_CONFIG = "config.yaml"


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


def _orchestration_files() -> dict[str, str]:
    """
    :returns: Bundle-relative path -> text, for the files that carry
        orchestration prose (root config plus the three skills).
    """
    paths = [
        _HOLLY_BUNDLE / _ROOT_CONFIG,
        *[_HOLLY_BUNDLE / "skills" / name / "SKILL.md" for name in _SKILLS],
    ]
    return {str(p.relative_to(_HOLLY_BUNDLE)): p.read_text(encoding="utf-8") for p in paths}


def _bundle_files() -> dict[str, str]:
    """
    :returns: Bundle-relative path -> text, for every prose-bearing file: the
        root config, the three worker configs, and the three skills.
    """
    files = _orchestration_files()
    for name in _WORKERS:
        path = _HOLLY_BUNDLE / "agents" / name / "config.yaml"
        files[str(path.relative_to(_HOLLY_BUNDLE))] = path.read_text(encoding="utf-8")
    return files


# ─────────────────────────── claim scanning ───────────────────────────
#
# The scans below (D1a enforcement claims, D4 publication acts, consistency
# exceptions) all ask one question of prose: does this text ASSERT something it
# must not? Two earlier versions asked it of SENTENCES and were evaded twice —
# first by a sentence that carried a true clause and a false one, then by claims
# written as bullets, YAML comments, questions and conditionals. Sentence shape
# was never the thing being detected; claim shape is.
#
# So the unit of scanning is the whole file, normalized: hard wraps, list
# markers and comment markers collapse away, and a claim may span what used to
# be two bullets or two wrapped lines. Only sentence-ending and strong clause
# punctuation stops a claim from being assembled.
#
# Negation is scoped the way English scopes it — forward from the negation word
# to the next strong boundary — and a list-item boundary counts as a boundary
# even though a claim may cross one. That asymmetry is deliberate: one "Never"
# should cover its own coordinated list ("Never edit, push, open/merge a PR")
# but must not reach into the next bullet and launder a claim there.

_SENTINEL = "\x00"  # marks a former list-item or blank-line boundary

_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s")
_BULLET_PREFIX = re.compile(r"^(?:[-*]|\d+\.)\s+")

_NEGATION = re.compile(
    r"\b(no|not|never|nothing|none|cannot|without|nor|neither|non)\b|n't", re.IGNORECASE
)

# Ends a negation's reach. The sentinel is included so a prohibition in one
# bullet does not exempt an assertion in the next.
_NEGATION_BREAK = re.compile(rf"[.!?;:—{_SENTINEL}]")

# A comma also ends a negation's reach when what follows it is a new
# independent clause — detected by a finite verb before the next boundary.
# "If no other vendor is available, a same-vendor review is acceptable" must not
# have its permission laundered by the "no" in its subordinate clause, while
# "Never edit, push, open/merge a PR" keeps one prohibition across its own
# comma-separated list, because those fragments carry no finite verb.
_CLAUSE_COMMA = re.compile(
    r",(?=[^,;:.!?—]*\b(?:is|are|was|were|be|being|may|can|could|will|would|shall|"
    r"should|must|becomes?|remains?|stays?|counts?|suffices?|applies|holds?|works?)\b)"
)

# Gap inside a single claim. Excludes sentence and strong-clause punctuation but
# NOT the sentinel, so a claim split across bullets is still assembled.
_GAP = r"[^.!?;:—]"


def _normalized(text: str) -> str:
    """
    Flatten prose so a claim can be seen regardless of how it was laid out.

    Hard wraps are joined, list markers and leading ``#`` comment markers are
    dropped, and each list item or blank line leaves a sentinel behind.

    :param text: Raw file contents.
    :returns: One normalized string with sentinels at former item boundaries.
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

    Used by the lifecycle anchors, which must see each procedure step as its own
    block so that deleting a step is detectable.

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


def _negation_spans(text: str) -> list[tuple[int, int]]:
    """
    Locate each negation and the span it governs.

    :param text: Normalized text.
    :returns: ``(start, end)`` spans from each negation word to the next strong
        boundary or the end of the text.
    """
    spans: list[tuple[int, int]] = []
    for match in _NEGATION.finditer(text):
        stops = [s.start() for s in (_NEGATION_BREAK.search(text, match.end()),) if s]
        stops += [s.start() for s in (_CLAUSE_COMMA.search(text, match.end()),) if s]
        spans.append((match.start(), min(stops) if stops else len(text)))
    return spans


def _unnegated_claims(
    text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> list[tuple[str, str]]:
    """
    Find claim-pattern matches that no negation governs.

    :param text: Raw file contents (normalized internally).
    :param patterns: ``(label, compiled pattern)`` pairs to search for.
    :returns: ``(label, quoted excerpt)`` for every match outside every
        negation span.
    """
    normalized = _normalized(text)
    spans = _negation_spans(normalized)
    hits: list[tuple[str, str]] = []
    for label, pattern in patterns:
        for match in pattern.finditer(normalized):
            # (see _states for the mirror-image rule used by obligations)
            # Exempt when a negation governs the claim's start, or sits INSIDE
            # the claim itself ("a same-vendor review is not independent"). A
            # negation that merely follows a completed claim exempts nothing —
            # that was how "blocks publication, but it does not block local
            # commits" laundered its false half.
            if any(start <= match.start() < end for start, end in spans):
                continue
            if any(match.start() <= start < match.end() for start, _ in spans):
                continue
            excerpt = normalized[max(0, match.start() - 40) : match.end() + 40]
            hits.append((label, excerpt.replace(_SENTINEL, " | ").strip()))
    return hits


def _states(text: str, pattern: re.Pattern[str]) -> bool:
    """
    Whether *text* states the obligation *pattern* describes, uninverted.

    The mirror image of :func:`_unnegated_claims`. A negation that starts
    strictly BEFORE the match inverts it — "do NOT commit to your branch" does
    not state the commit obligation. A negation inside the match is part of the
    obligation's own wording ("issue NO verdict") and does not.

    :param text: Raw file contents (normalized internally).
    :param pattern: Compiled obligation pattern.
    :returns: ``True`` when at least one uninverted match exists.
    """
    normalized = _normalized(text)
    spans = _negation_spans(normalized)
    return any(
        not any(start < match.start() < end for start, end in spans)
        for match in pattern.finditer(normalized)
    )


# Affirmative claims that a mechanism prevents publication. Each needs a
# mechanism-ish subject AND a blocking verb AND a publication object, so
# ordinary talk of policies, or of pushing, does not trip it.
_ENFORCEMENT_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a named mechanism blocks publication",
        re.compile(
            r"\b(polic(?:y|ies)|guardrail|guard|gate|blast_radius|spawn_bounds|hook|"
            r"runner|runtime|framework|mechanism|enforcement|server|platform|"
            r"harness|engine)\b" + _GAP + r"{0,80}?"
            r"\b(block|blocks|blocked|blocking|prevent|prevents|deny|denies|denied|"
            r"refuse|refuses|refused|stop|stops|stopped|reject|rejects|forbid|forbids|"
            r"gate|gates|gated|require|requires|hold|holds|withhold|withholds|"
            r"veto|vetoes|bar|bars|disallow|disallows)\b" + _GAP + r"{0,60}?"
            r"\b(push|pushes|pushing|publication|publish|publishing|PR|pull request|"
            r"merge|merging|release|releasing|remote|upstream|commit|commits|branch|"
            r"branches|code|change|changes|diff)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "publication is described as mechanically blocked",
        re.compile(
            r"\b(push|pushes|pushing|publication|the PR|pull request|release)\b"
            + _GAP
            + r"{0,60}?"
            r"\b(is|are|was|were|gets|get|will be|would be|can be|cannot be|can't be)\b\s+"
            r"\b(blocked|gated|denied|prevented|refused|rejected|stopped|barred|"
            r"disallowed|vetoed|held)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "an approval or gate is described as a precondition enforced by something",
        re.compile(
            r"\b(polic(?:y|ies)|guardrail|gate|blast_radius|runner|runtime|server|"
            r"platform|mechanism|enforcement)\b" + _GAP + r"{0,60}?"
            r"\b(until|before|unless|without)\b" + _GAP + r"{0,60}?"
            r"\b(review|approval|approved|sign-?off|reviewed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a gate or interlock is asserted to exist",
        re.compile(
            r"\bthere (?:is|are|exists?)\b" + _GAP + r"{0,25}?"
            r"\b(gate|gating|interlock|enforcement|blocker|guardrail|"
            r"safety net|hard stop)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "enforcement is claimed over a publication act",
        re.compile(
            r"\benforc(?:e|es|ed|ing|ement)\b" + _GAP + r"{0,80}?"
            r"\b(push|pushes|pushing|publication|publish|publishing|PR|pull request|"
            r"merge|release|remote|upstream)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tooling is claimed to guarantee review",
        re.compile(
            r"\b(system|tooling|toolchain|platform|runner|server|framework|harness|"
            r"engine|CI|hook|pipeline|automation|infrastructure)\b" + _GAP + r"{0,40}?"
            r"\b(guarantee\w*|ensure\w*|assure\w*|make sure|see to it)\b" + _GAP + r"{0,60}?"
            r"\b(review\w*|approv\w*|unreviewed|publish\w*|push\w*|merge\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review is called mandatory AND mechanised",
        # Calling review required is honest — holly does require it of itself.
        # Calling it automatic, technical or enforced is the false part, so the
        # mechanism qualifier is what this pattern turns on.
        re.compile(
            r"\b(review|approval|sign-?off)\b" + _GAP + r"{0,60}?"
            r"\b(mandatory|required|requirement|obligatory|compulsory)\b" + _GAP + r"{0,60}?"
            r"\b(automatic\w*|technical\w*|mechanic\w*|enforced|guaranteed|"
            r"by the (?:system|platform|runner|server|tooling|hook|CI))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review is called mechanised AND mandatory (reverse order)",
        re.compile(
            r"\b(automatic\w*|technical\w*|mechanic\w*|enforced|guaranteed)\b" + _GAP + r"{0,40}?"
            r"\b(review|approval|sign-?off)\b" + _GAP + r"{0,40}?"
            r"\b(mandatory|required|requirement|obligatory|compulsory)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a filter is claimed between review and the remote",
        re.compile(
            r"\bonly\b" + _GAP + r"{0,40}?\b(reviewed|approved)\b" + _GAP + r"{0,50}?"
            r"\b(reach\w*|make it|makes it|get\w*|land\w*|goes?|arrive\w*)\b" + _GAP + r"{0,25}?"
            r"\b(remote|origin|upstream|production|public|main)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review is called mechanised THEN mandatory",
        re.compile(
            r"\b(review|approval|sign-?off)\b" + _GAP + r"{0,30}?"
            r"\b(automatic\w*|technical\w*|mechanic\w*|enforced|guaranteed|"
            r"structural\w*|systemic\w*)\b" + _GAP + r"{0,30}?"
            r"\b(mandatory|required|requirement|obligatory|compulsory)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "publication is claimed to require a review artefact",
        re.compile(
            r"\b(publication|publishing|push|pushing|merge|merging|release|releasing|"
            r"shipping)\b" + _GAP + r"{0,30}?"
            r"\b(requires?|demands?|needs?)\b" + _GAP + r"{0,40}?"
            r"\b(review|approval|sign-?off|record|token|flag|marker|receipt)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "approval is called a requirement for publication",
        re.compile(
            r"\b(approval|review|sign-?off)\b" + _GAP + r"{0,30}?"
            r"\b(requirement|precondition|prerequisite|gate)\b" + _GAP + r"{0,30}?"
            r"\b(for|to|before)\b" + _GAP + r"{0,30}?"
            r"\b(ship\w*|publish\w*|push\w*|merg\w*|releas\w*)\b",
            re.IGNORECASE,
        ),
    ),
)

# Enforcement claims whose own wording contains a negation. These are scanned
# WITHOUT negation exemption, because here the negation IS the claim: "the
# policy will not allow a push before review" asserts a gate exactly as loudly
# as "the policy blocks the push", and the scan that exempts negated text was
# hiding the entire family. Every pattern therefore pins a mechanism subject to
# a publication object, so ordinary prohibitions addressed to holly or a worker
# ("never push", "you cannot run independent review") do not match.
_ENFORCEMENT_CLAIMS_NEGATIVE: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a mechanism is claimed to refuse or disallow publication",
        re.compile(
            r"\b(polic(?:y|ies)|guardrail|gate|blast_radius|spawn_bounds|hook|runner|"
            r"runtime|framework|mechanism|enforcement|server|platform|harness|engine)\b"
            + _GAP
            + r"{0,60}?"
            r"\b(will not|won'?t|does not|doesn'?t|do not|don'?t|cannot|can'?t|never|"
            r"refuses? to|declines? to|fails? to)\b" + _GAP + r"{0,25}?"
            r"\b(allow|allows|permit|permits|let|lets|accept|accepts|pass|passes|"
            r"release|releases)\b" + _GAP + r"{0,50}?"
            r"\b(push|pushes|pushing|publish|publishing|publication|PR|pull request|"
            r"merge|release|remote|upstream|branch|commit|commits)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "publication is claimed to be impossible without review",
        re.compile(
            r"\bno\b" + _GAP + r"{0,20}?"
            r"\b(push|merge|PR|pull request|release|publication|commit)\b" + _GAP + r"{0,40}?"
            r"\b(is|are|will be|would be|can be|gets?)\b" + _GAP + r"{0,20}?"
            r"\b(possible|allowed|permitted|permissible|accepted|made)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "publication is described as impossible, forbidden or locked",
        re.compile(
            r"\b(push|pushes|pushing|publication|publishing|merge|merging|release|"
            r"the PR|pull request)\b" + _GAP + r"{0,40}?"
            r"\b(is|are|remains?|stays?|becomes?)\b" + _GAP + r"{0,20}?"
            r"\b(impossible|forbidden|prohibited|locked|barred|disallowed|"
            r"unavailable|off limits)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "nothing is claimed to reach the remote",
        re.compile(
            r"\b(nothing|no code|no commit|no commits|no change|no changes|no work)\b"
            + _GAP
            + r"{0,40}?"
            r"\b(reach\w*|get\w*|go\w*|land\w*|leave\w*|escape\w*|ship\w*|move\w*)\b"
            + _GAP
            + r"{0,30}?"
            r"\b(remote|upstream|origin|out|published|public)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "nothing unreviewed is claimed to be publishable",
        re.compile(
            r"\b(nothing|no|none)\b" + _GAP + r"{0,25}?\bunreviewed\b" + _GAP + r"{0,40}?"
            r"\b(is|are|gets?|can be|will be|ever)\b" + _GAP + r"{0,25}?"
            r"\b(publish\w*|push\w*|merged|released|shipped|landed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "an unreviewed publication is claimed to fail",
        # Scanned raw: "without" is itself a negation word, so under the
        # negation-aware scan this claim exempted itself.
        re.compile(
            r"\b(push|pushes|pushing|merge|publish\w*|release)\b" + _GAP + r"{0,30}?"
            r"\b(without|lacking|missing|absent)\b" + _GAP + r"{0,30}?"
            r"\b(review\w*|approval|sign-?off)\b" + _GAP + r"{0,40}?"
            r"\b(fail\w*|error\w*|rejected|denied|blocked|refused|bounce\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "the work is claimed to be immobilised pending review",
        re.compile(
            r"\b(branch|code|commit|commits|work|diff|change|changes)\b" + _GAP + r"{0,30}?"
            r"\b(cannot|can'?t|may not|must not|will not|won'?t|stays?|remains?|sits?)\b"
            + _GAP
            + r"{0,25}?"
            r"\b(leave|move|go|reach|ship|land|escape|published|put|local|in place)\b"
            + _GAP
            + r"{0,40}?"
            r"\b(until|unless|without|before)\b" + _GAP + r"{0,40}?"
            r"\b(review\w*|approv\w*|sign\w*|verdict|clear\w*|gate\w*)\b",
            re.IGNORECASE,
        ),
    ),
)

# Literal tokens that can only appear as a fabricated enforcement claim. These
# take no negation exemption — the string itself names a mechanism that does not
# exist, so any occurrence is a defect.
#
# ``require_pr_review`` — a predecessor design named exactly this policy as the
#   thing blocking ``git push``. No such policy exists in the builtins; it was
#   never evaluated. Its return, in any file, is that defect coming back.
# ``review-passed`` / ``review_passed`` — a machine-checked "review passed" flag
#   or gate state that publication is conditioned on. No such state is recorded
#   or read anywhere; review completion lives only in holly's sequencing.
_BANNED_TOKENS: tuple[tuple[str, str], ...] = (
    ("require_pr_review", "names a policy that does not exist and is never evaluated"),
    ("review-passed", "implies a machine-checked gate state that is never recorded"),
    ("review_passed", "implies a machine-checked gate state that is never recorded"),
)

# Instructions to publish, by EFFECT rather than by verb. "ship the branch
# upstream" and "land the commits" are publication; "tells you when to publish"
# is not, because it has no object being moved.
_PUBLICATION_ACTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "moves the work to the remote",
        re.compile(
            r"\b(push|pushes|pushed|pushing|publish|publishes|published|publishing|"
            r"ship|ships|shipped|shipping|land|lands|landed|landing|upload|uploads|"
            r"uploading|send|sends|sending)\b" + _GAP + r"{0,40}?"
            r"\b(branch|commit|commits|code|diff|work|change|changes|it|them|"
            r"upstream|remote|origin|PR|pull request)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "opens or merges a pull request",
        re.compile(
            r"\b(open|opens|opened|opening|create|creates|created|creating|file|files|"
            r"raise|raises|merge|merges|merged|merging)\b" + _GAP + r"{0,25}?"
            r"\b(PR|pull request)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "names a publishing command",
        re.compile(r"\bgit push\b|\bgh pr (?:create|merge)\b", re.IGNORECASE),
    ),
)

# Exceptions that would contradict a rule stated elsewhere in the bundle. The
# review rules are stated in more than one live consumer (the root prompt and
# cross-review), so a permission added to one silently overrides the other for
# whichever file holly happens to be reading.
_EXCEPTION_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "permits a same-vendor or skipped review",
        re.compile(
            r"\b(same[- ]vendor|same model|own vendor|skip(?:ping|ped)? the review|"
            r"without (?:a )?review|self[- ]review)\b" + _GAP + r"{0,60}?"
            r"\b(ok|okay|fine|acceptable|allowed|permitted|permissible|may|can|"
            r"is enough|suffices|sufficient|proceed|accept)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "permits a same-vendor or skipped review (verb first)",
        re.compile(
            r"\b(accept|accepts|allow|allows|permit|permits|may use|can use|fall back|"
            r"falls back|degrade|degrades|settle)\b" + _GAP + r"{0,50}?"
            r"\b(same[- ]vendor|same model|skipped review|self[- ]review|"
            r"unreviewed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "permits holly to merge",
        re.compile(
            r"\b(holly|you)\b" + _GAP + r"{0,30}?\b(may|can|should|must)\b" + _GAP + r"{0,20}?"
            r"\bmerge\b",
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
    The orchestrator and each worker keep their harness.

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

    assert {a.name: a.executor.config.get("harness") for a in holly_spec.sub_agents} == {
        "claude_code": "claude-native",
        "codex": "codex-native",
        "pi": "pi",
    }

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


# What each WIDE axis must actually REQUIRE of a reviewer. Naming the axis is
# not the contract; these obligations are. An axis body reduced to "mention the
# input domain and report clear" still parses, still carries its heading, still
# matches its checklist label — and asks the reviewer for nothing.
_AXIS_REQUIREMENTS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "blast-radius",
        (r"caller|consum", r"chang|renam|emit", r"did not|mirror|decod|match"),
    ),
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
# what a dispatch obliges. Each is a distinct class of defect the reviewer would
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
    D2: each WIDE axis is bound to its number in both places AND still states
    what it requires.

    Round one caught an axis heading being renamed. The same defect lives one
    level down: keep the heading and the checklist label, replace the body with
    "mention the input domain and report clear", and the reviewer receives a
    pass name with no obligation attached. Every finding it would have produced
    disappears, and the battery checklist still reports "[WIDE-3] run — clear".

    So the axis BODY is pinned to its obligations — for input-domain: the full
    taxonomy, legacy and alternate shapes, match ORDERING, the none-match
    fall-through, that an unhandled shape is blocking, and the demand for an
    allowlist over a fail-open denylist.
    """
    mandate = _mandate(holly_spec)

    # Split the numbered axis list into per-axis bodies.
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


def test_root_guardrail_policies_are_exactly_the_three(holly_spec: AgentSpec) -> None:
    """
    D1: the orchestrator carries exactly ``blast_radius``, ``spawn_bounds`` and
    ``headless_subagent_purpose_guard`` — no more, no fewer.

    Set equality, not membership, is the whole point. The bundle's honesty
    contract is that publication is unenforced; a fourth policy would change
    what the runtime does while every prose disclaimer stays behind, silently
    false. Equality also still catches the ordinary regression of a policy being
    dropped.
    """
    assert holly_spec.guardrails is not None
    assert {p.name for p in holly_spec.guardrails.policies} == {
        "blast_radius",
        "spawn_bounds",
        "headless_subagent_purpose_guard",
    }


def test_root_policy_arguments_are_pinned(holly_spec: AgentSpec) -> None:
    """
    D1: the policies' ARGUMENTS are pinned, not just their names.

    A policy set can satisfy name equality and still enforce. Flipping
    ``gate_pushes`` to ``true`` is a one-word edit that leaves every name in
    place, turns ``blast_radius`` into an ASK gate on push, and falsifies every
    "nothing blocks a push" disclaimer in the bundle at once.

    ``blast_radius`` and the purpose guard are stateless: they decide from the
    event in front of them, so their arguments determine what actually happens
    on every gated call. ``spawn_bounds`` is different — it counts dispatches in
    closure state and relies on the policy engine surviving the turn, which it
    does not on the deploy path where the engine is rebuilt per request. Its
    arguments are pinned here as the DECLARED bound, and this test makes no
    claim that the per-turn cap is enforced.
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
    assert set(purpose_args.get("allowed_purposes") or []) == {
        "implement",
        "review",
        "explore",
        "search",
    }


def test_policy_factory_paths_resolve(holly_spec: AgentSpec) -> None:
    """
    D1: every policy's dotted path actually imports to a callable.

    A prefix check passes ``blost_radius``. The failure mode is quiet: the spec
    still loads, the policy still appears in the set by name, and the breakage
    surfaces only when the runner first tries to evaluate it. Resolving the path
    here is the same work the resolver does, done at test time.
    """
    for policy in holly_spec.guardrails.policies:
        path = policy.function.path
        module_name, _, attribute = path.rpartition(".")
        module = importlib.import_module(module_name)
        target = getattr(module, attribute, None)
        assert callable(target), f"{policy.name}: {path} does not resolve to a callable"


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

    This is the primary term, not a secondary one. holly's failure mode is a
    worker or human acting on a claim they cannot check: the predecessor
    design's workers complied with a gate that never existed, and no policy had
    to change for that to happen. A false claim of enforcement is the harm
    itself.

    Scanned over normalized text, so the shape a claim is written in does not
    matter — bullets, YAML comments, questions, conditionals and passive voice
    all reduce to the same subject/verb/object test. Rhetorical questions about
    gates are caught too, which is deliberate over-approximation: there is no
    honest reason to phrase an enforcement claim as a question here, and the
    scan errs toward failing.

    THE RESIDUAL, stated precisely, because natural-language detection is not
    decidable and the previous two statements of it were both widened on first
    contact.

    Twenty-four evasion attempts were written against this scanner — negation-
    phrased enforcement ("the policy will not allow a push"), modal and
    adjective forms ("pushing is impossible until review"), cross-sentence
    subjects ("There is a gate. It stops every push."), nominalised enforcement,
    requirement-plus-mechanism phrasing, and immobilisation claims. Twenty-three
    are caught and pinned in
    ``test_claim_scanners_catch_known_evasions``. ONE survives:

        an enforcing actor named by a word outside the mechanism vocabulary —
        "Ada checks every change before it leaves the worktree"

    That one is not closed on purpose, not on grounds of effort. Catching a
    bare subject plus "checks ... before ..." would also catch this bundle's own
    true sentences: holly DOES check HEAD before releasing, and cross-review
    says so. Separating a real actor from an invented one needs a roster of
    which actors exist, which is a different test from a claim scanner. Naming
    the boundary is worth more than widening the pattern until honest prose
    starts failing — the point of the scan is that authors can still write the
    truth plainly.

    What backs the residual up is not a longer denylist. It is that the
    surviving shape requires inventing an actor, which reads as obviously wrong
    to any human reviewing the diff, and that every escape found so far has been
    closed in the same commit that found it.
    """
    for rel_path, text in _bundle_files().items():
        lowered = text.lower()
        for token, why in _BANNED_TOKENS:
            assert token not in lowered, f"{rel_path}: banned token {token!r} — {why}"

        violations = _unnegated_claims(text, _ENFORCEMENT_CLAIMS)
        # Negatively-phrased claims are scanned raw: their negation is the claim.
        normalized = _normalized(text)
        violations += [
            (label, normalized[max(0, m.start() - 40) : m.end() + 40].replace(_SENTINEL, " | "))
            for label, pattern in _ENFORCEMENT_CLAIMS_NEGATIVE
            for m in pattern.finditer(normalized)
        ]
        assert not violations, (
            f"{rel_path}: {violations[0][0]} — ...{violations[0][1]}... Nothing in "
            f"this bundle blocks a push (blast_radius runs gate_pushes: false and "
            f"inspects shell text only); publication ordering is holly's "
            f"sequencing, and claiming otherwise is the defect this test guards."
        )


def test_no_file_grants_an_exception_to_the_review_rules() -> None:
    """
    Consistency: no file permits what another file forbids.

    The review rules have parallel live consumers. cross-review is the playbook,
    but the root prompt states the same rules, and holly acts on whichever it is
    reading. An exception added to one file — "if no other vendor is available,
    a same-vendor review is acceptable" — never contradicts anything locally; it
    just quietly wins wherever it is read, while cross-review's hard stop stays
    on the page looking authoritative.

    Two rules have no exception anywhere in the bundle: review is never
    same-vendor or skipped, and holly never merges. This scans for a grant of
    either, in any orchestration file, using the same negation scoping as the
    other claim scans so that stating the prohibition stays legal.
    """
    for rel_path, text in _orchestration_files().items():
        violations = _unnegated_claims(text, _EXCEPTION_CLAIMS)
        assert not violations, (
            f"{rel_path}: {violations[0][0]} — ...{violations[0][1]}... The rule is "
            f"stated in more than one live consumer; an exception here overrides "
            f"the other file wherever holly happens to read this one."
        )


# What every worker must be told, beyond not publishing. The IMPLEMENT half is
# the positive contract — "commit and stop" is a two-part instruction, and
# dropping either half is invisible to a scan that only looks for publication.
# The REVIEW half is the dispatch-integrity taxonomy: a reviewer that does not
# know what a complete dispatch contains cannot detect an incomplete one, and
# the whole truncation defence rests on it refusing rather than guessing.
#
# Obligations are matched with PROXIMITY, not by keyword presence anywhere in
# the prompt. "commit" and "branch" both survive a prompt that tells the worker
# to leave its worktree uncommitted — one appears in the commit-trailer rule,
# the other in "report the branch name" — so an obligation that only checked for
# the two words passed that edit while the thing holly reviews was destroyed.
# Each pattern below must match as one clause, and must not be negated.
_NEAR = r"[^.!?;:—]"  # stays inside one clause
_NEAR_COLON = r"[^.!?;—]"  # tolerates a colon, for "green: run the tests"

_WORKER_OBLIGATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "commit-to-its-branch",
        "an uncommitted worktree leaves nothing for the orchestrator to review",
        (rf"\bcommit\b{_NEAR}{{0,40}}?\b(?:task )?branch\b",),
    ),
    (
        "report-and-stop",
        "a worker that keeps going after reporting drifts outside its scope",
        # Colon tolerated: the reporting clause names a "file:line" summary.
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
        # The section HEADING says "read-only" too, so bind to the instruction:
        # an explicit no-edit clause plus the evidence the answer must carry.
        (
            r"\b(edit nothing|change nothing|make no edits|do not edit|edits? nothing)\b",
            r"file:line|evidence",
        ),
    ),
)


def test_workers_commit_and_stop(holly_spec: AgentSpec) -> None:
    """
    D4: each worker is told to commit and stop, carries the dispatch-integrity
    taxonomy, and is never told to publish.

    The negative half alone was evadable: changing "commit to your task branch"
    to "leave the worktree uncommitted" instructs no publication at all and
    passed cleanly, while destroying the thing holly reviews. So the positive
    contract is asserted directly, and so is the REVIEW taxonomy — both
    delimiters, the diff, the contract, the checklist, what counts as malformed,
    the ``INCOMPLETE DISPATCH`` banner, best-effort evidence, and no verdict.
    A reviewer missing any of those cannot refuse a truncated dispatch, which is
    the only defence the mandate's delimiters have.

    Asserted on parsed ``instructions`` rather than raw YAML so the file's
    explanatory comments are not mistaken for instructions to the model.
    """
    by_name = {a.name: a for a in holly_spec.sub_agents}
    for name in _WORKERS:
        instructions = by_name[name].instructions or ""
        assert instructions.strip(), f"{name}: no instructions at all"

        for obligation, consequence, patterns in _WORKER_OBLIGATIONS:
            for pattern in patterns:
                # Uninverted, so "do NOT commit to your branch" cannot satisfy
                # the rule it inverts, while a prohibition-shaped obligation
                # ("issue NO verdict") still counts as stated.
                assert _states(instructions, re.compile(pattern, re.IGNORECASE)), (
                    f"{name}: worker no longer carries {obligation!r}. Without it: {consequence}."
                )

        violations = _unnegated_claims(instructions, _PUBLICATION_ACTS)
        assert not violations, (
            f"{name}: publication act outside any prohibition — "
            f"...{violations[0][1]}... Workers commit and stop; holly releases "
            f"the branch after gates and an independent review."
        )


def test_claim_scanners_catch_known_evasions() -> None:
    """
    The scanners are pinned against every evasion tried against them so far.

    Two rounds of review found shapes the scanners missed: a true clause
    laundering a false one, then bullets, YAML comments, questions, conditionals
    and synonym vocabulary. Each is a fixture below. Without this test a future
    "simplification" of the scope or vocabulary rules would leave every other
    test green while reopening a closed hole.

    The negatives matter as much: they are the bundle's own truthful
    disclaimers and prohibitions, which must keep passing, or the honest way to
    describe an unenforced ordering becomes untestable and authors stop writing
    it at all.
    """
    caught_enforcement = (
        # Round 2: a true clause laundering a false one.
        "The policy blocks publication, but it does not block local commits.",
        # Round 3: layout shapes.
        "- The blast_radius policy is what\n- blocks every push to the remote.",
        "# The guardrail denies any push before review.",
        "Does the policy block a push before review? Yes, always.",
        "If the runner refuses the push, tell the human.",
        "Every push is gated by the policy layer.",
        "The platform holds the branch until review is approved.",
        "The guardrail prevents anything reaching the remote.",
        # Round 3: negation-phrased enforcement — the negation IS the claim, and
        # the negation-aware scan was hiding this entire family.
        "The policy will not allow a push before review.",
        "The guardrail does not permit publishing until the reviewer signs off.",
        "The runner never allows a merge without an approved review.",
        "The platform won't let the branch reach the remote before review.",
        "No push is possible without a passing review.",
        "Our tooling ensures nothing unreviewed is published.",
        # Round 3: modal / adjective / nominalised forms.
        "Pushing is impossible until review completes.",
        "A push is forbidden by the guardrail.",
        "Publication remains locked until the policy clears it.",
        "Enforcement of review happens in the runner before any push.",
        "The CI hook rejects unreviewed commits.",
        "A push without review will fail.",
        # Round 3: existence and cross-sentence subjects.
        "There is a gate. It stops every push.",
        "We ship a policy layer. Nothing reaches the remote without it.",
        # Round 3: requirement phrasing that implies a mechanism.
        "The review step is mandatory and automatic.",
        "Review is technically required, not just expected.",
        "Publication requires a green review record.",
        "Approval is a hard requirement for shipping.",
        # Round 3: immobilisation without naming a mechanism.
        "The branch cannot leave without a signature.",
        "Code stays put until someone signs the paperwork.",
    )
    for text in caught_enforcement:
        normalized = _normalized(text)
        assert _unnegated_claims(text, _ENFORCEMENT_CLAIMS) or any(
            pattern.search(normalized) for _, pattern in _ENFORCEMENT_CLAIMS_NEGATIVE
        ), text

    allowed_enforcement = (
        "Nothing mechanically blocks a push.",
        "There is no policy gate; the ordering holds only because you sequence it.",
        "There is no mechanism enforcing this; it holds only because you check HEAD.",
        "A block here would read as protection while enforcing nothing.",
        "Publication is orchestrator-sequenced: commit and stop.",
        "blast_radius inspects shell command text only and ALLOWs every non-shell tool.",
        "blast_radius does not gate pushes; gate_pushes is false.",
        "If no different-vendor reviewer is available, you CANNOT run independent review.",
        "These are NOT a review gate and do not enforce the publication ordering.",
    )
    for text in allowed_enforcement:
        normalized = _normalized(text)
        assert not _unnegated_claims(text, _ENFORCEMENT_CLAIMS), text
        assert not any(p.search(normalized) for _, p in _ENFORCEMENT_CLAIMS_NEGATIVE), text

    caught_publication = (
        "Push and open a PR; do not merge it.",
        "When green, push your branch.",
        # Round 3: synonym vocabulary for the same effect.
        "When green, ship the branch upstream.",
        "Land the commits on origin when the gates pass.",
        "Publish your work to the remote and report back.",
        "- Commit your work.\n- Then open a PR for it.",
    )
    for text in caught_publication:
        assert _unnegated_claims(text, _PUBLICATION_ACTS), text

    allowed_publication = (
        "Do NOT push and do NOT open a PR.",
        "Never edit, push, open/merge a PR, or dispatch.",
        "It does not push and does not open a PR.",
        "The orchestrator tells you when to publish.",
        "When green, commit to your task branch, then stop.",
    )
    for text in allowed_publication:
        assert not _unnegated_claims(text, _PUBLICATION_ACTS), text

    caught_exceptions = (
        "If no other vendor is available, a same-vendor review is acceptable.",
        "You may accept a same-vendor review when time is short.",
        "Holly may merge the PR once the bot is happy.",
    )
    for text in caught_exceptions:
        assert _unnegated_claims(text, _EXCEPTION_CLAIMS), text

    allowed_exceptions = (
        "Never silently degrade to a same-vendor or skipped review.",
        "A same-vendor review is not independent and is never acceptable.",
        "Holly does NOT merge.",
    )
    for text in allowed_exceptions:
        assert not _unnegated_claims(text, _EXCEPTION_CLAIMS), text


# ───────────────── review-lifecycle branches (D3 / D5 / D6) ─────────────────
#
# This list is an ENUMERATION, not a claim of completeness: it pins the branches
# named below, each of which was checked by deleting the block that carries it
# and confirming this test fails. A branch not listed here is not protected.
#
# Anchoring is per block and per owning file. Per block, because scattering the
# vocabulary across a document would satisfy a whole-file search and deleting a
# step would go unnoticed. Per owning file, because the prose deliberately
# restates several rules in more than one place — good redundancy, but the
# restatement is not the procedure, so deleting the procedural step must fail
# even while the restatement lives.
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

# What the contract handed to an IMPLEMENTER must enumerate. This is upstream of
# the review: any dimension the contract omits, the reviewer starts blind on,
# and the blind spot recurs every round. Reducing this section to "write a good
# contract" leaves the mandate intact and still guarantees the blind spots.
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
    D3/D5/D6: the enumerated review-lifecycle branches are still present.

    These are the branches whose deletion is invisible — the bundle still
    parses, every other test stays green, and the failure shows up only as a run
    that quietly did the wrong thing: a review dispatched to the author's own
    vendor, a fix round reviewing the published diff instead of the delta about
    to be pushed, gate discovery narrowed to test/lint/typecheck so the repo's
    own validators never run, a bot's comment list replacing the full battery,
    or a hand-off that reports findings serviced while threads stay unresolved
    and the human merges on the difference.

    The list is an enumeration, not a completeness claim: each branch below was
    verified by deleting the block that carries it. A branch not listed is not
    protected by this test.
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
    D3: the contract handed to an implementer still has to be authored WIDE.

    This section is upstream of every review round. A contract that enumerates
    SITES rather than the input DOMAIN guarantees that siblings arrive one per
    round, and no amount of reviewer diligence recovers a dimension the contract
    never named. Gutting it leaves the mandate untouched and the blind spots
    permanent.
    """
    text = _orchestration_files()[_CROSS_REVIEW]
    for requirement, patterns in _CONTRACT_AUTHORING:
        assert all(re.search(p, text, re.IGNORECASE) for p in patterns), (
            f"the contract-authoring section no longer requires {requirement!r}; "
            f"every review inherits that blind spot and it recurs each round."
        )


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
