---
name: cross-review
description: Verify a candidate diff (an implementer's, or a doc/skill polly authored directly) with an INDEPENDENT, different-vendor sub-agent (given the diff + contract, plus a clean repo checkout for the sibling-class and coupled-artifact sweeps — but never the implementer's transcript or worktree); turn blocking issues into fix-tasks and loop until clean.
---

# cross-review — independent verification

The author never signs off on its own work — an implementer sub-agent, or polly
for a directly-authored doc/skill. A DIFFERENT-vendor sub-agent reviews and
returns a structured report (not a transcript anyone reads through).
**Independence = a different vendor + withholding the implementer's
transcript/worktree — NOT denying repo read.** The sibling-class and
coupled-artifact sweeps REQUIRE a clean checkout to grep, so always permit repo
read, and give the reviewer the changed surface plus its adjacency (callers,
sibling surfaces, the target type) — not just the raw hunk.

## The battery — every review, every round
One reviewer dispatch runs BOTH passes, ALWAYS, each labelled in its report. A
re-review / closure round runs the **identical** battery — never a narrowed
"just confirm these N blockers are closed" dispatch. That narrowing is exactly
how a fix's own siblings leak out one round at a time: the WIDE pass keeps
finding the next instance because the FIX was point-scoped even though the
review nominally was not.

- **[FOCUSED]** — diff vs contract: does the change satisfy its acceptance
  contract? The narrow pass; enough alone only for isolated, low-state changes.
- **[WIDE]** — what ELSE does this touch that the diff does not show? FOUR
  mandatory axes (below). MAY fast-exit a genuinely-empty axis in one line ("no
  blast radius"), but is NEVER skipped — skipping it turns a one-line fix into
  multi-round whack-a-mole.

When the diff INCLUDES any doc / ADR / spec file (prose-only OR mixed code+docs),
ALSO run two more passes on the doc files, labelled:
- **[SELF-CONSISTENCY]** — does the document contradict ITSELF? [FOCUSED] audits
  each claim in isolation, so two mutually-contradictory claims can each "pass."
  Read the doc whole; reconcile its consequences / scope / non-goals against its
  own decision and any evidence it cites; hunt STALE scope a later edit
  superseded but never removed.
- **[GOVERNANCE]** — does it obey the repo's OWN doc conventions? Load the repo's
  ADR/spec governance (lifecycle e.g. Proposed→Accepted→Superseded; a decision
  registry/index that must stay in sync; numbering / status / cross-ref rules)
  and verify conformance — a premature status flip, a skipped lifecycle state, an
  index copy disagreeing with the doc it points at, a status drifting between
  copies.

### The four WIDE axes
1. **Blast-radius** — who CONSUMES the changed contract? callers, event
   consumers, generated-client mirrors, anything that decodes the changed payload.
2. **Sibling-class** — where else does this SAME defect shape exist that the diff
   did NOT touch? parallel modules/handlers/routes, other callers of the same
   helper/pattern. (See class-closure below.)
3. **Input-domain coverage** — when the diff touches a function that MAPS inputs to
   decisions (classifier/parser/mapper/router/dispatcher/normalizer/error-handler),
   enumerate the FULL input taxonomy, not only the shapes the diff exercised.
   (See "Match review depth to the surface" below.)
4. **Coupled-artifact** — which NON-CODE artifacts must move in lockstep for this
   change KIND, and did they? (See "Coupled-artifact sweep" below.)

### WIDE walk-list — report a hit or "clear" per item
- **All callers** of every changed symbol — flag sites that should have changed
  to match but didn't.
- **Consumer / event ripple** — event consumers of anything emitted/renamed;
  generated-client mirrors of a changed API shape.
- **Parallel surfaces** — a fix on one surface MUST hit every surface running the
  same logic; grep the twin, don't re-read the changed one. Canonical pairs:
  client-render ↔ server/SSR loader; read path ↔ its cached/materialized twin;
  API handler ↔ generated-client mirror. Fixed one, left the twin = blocking.
- **Test-surface** — tests must exercise EVERY surface the change touches;
  single-surface coverage of a multi-surface change is a gap (a green suite hides
  the parallel bug).
- **Whole-parcel grep (round 1)** — when the change renames/redefines a
  term/status/concept/claim, grep the ENTIRE parcel (source + tests + docs +
  generated/published artifacts) for the OLD term AND its old semantics (catch a redefinition whose
  vocabulary is unchanged) and fix-or-justify each hit THIS round.
- **Env-dependent claims** — static configs/docs assert only intrinsic truths;
  flag any "resolves at X / reachable via Y / this provider answers" (deployment
  facts, not file properties).
- **Claim completeness** — audit EVERY claim about named files/workflows/config:
  REMOVED, ADDED, and anything asserted UNCHANGED. A partial audit misses a stale
  claim left by a removal, an overreaching new claim, or a false "still X / no
  change to Y."

## Match review depth to the surface — adversarial depth, enumerate the space
Match DEPTH to the surface. On a rich surface the WIDE pass goes ADVERSARIAL — a
confirmatory pass confirms only the cells the contract named and is blind to the
rest (the "bot found another edge → patch → re-review → next edge" grind). Same
discipline, two kinds of code:
- **Rich/stateful surface** (serializers, form↔payload round-trips, state
  machines, derivations over many input states — loaded×edited × valid/invalid/
  empty × per-line/aggregate × present/absent refs): change the mandate from
  "verify the fix" to "find the states that break" — enumerate the state space,
  attack the cells the contract did not name, demand the FULL list in one pass.
- **Mapping function** (the pure-function twin): enumerate the FULL input
  taxonomy — every shape/variant, every branched field (incl. alternate/legacy
  names for the same meaning), nested/wrapped/chained inputs, the ORDERING of
  overlapping matches (a broad early match short-circuiting a specific later one),
  and the none-match fall-through. An unhandled, mis-ordered, or silently-dropped
  shape is BLOCKING.
- **Allowlist, not denylist** — when the fix is itself an enumeration, enumerate
  the KNOWN-GOOD and route anything unrecognized to the safe/strict branch. A
  denylist is fail-OPEN (every future sibling is a fresh hole; the loop never
  converges); an allowlist is fail-CLOSED (converges by construction). If a fix
  enumerates the BAD cases to reject, flag it.
- **Front-run the external bot** — prefer running the adversarial pass on the
  SAME engine as the external reviewer (`codex`) so its whole class of findings
  lands in the FAST internal loop, not the slow PR loop (see `pr-bot-loop`).
  Still a DIFFERENT vendor from the author.

The cheapest place to close a state-space bug is the implementer's FIRST pass:
the acceptance contract for a rich surface should name the state-space axes and
require an invariant / round-trip matrix test as a delivered artifact.

## Class-closure + the recurrence stop-gate
Before scoping any fix to its flagged site, ask: one-off or class? A typo, wrong
constant, or copy-paste slip is a one-off — fix it and move on. A fix whose shape
generalizes ("X not re-checked on retry", "schema omits an outcome code", "value
not normalized before compare") is a CLASS: the fix AND the review must enumerate
and cover EVERY instance across the repo (grep the pattern), not just the flagged
site. A point-fix + delta-scoped review provably cannot pre-empt siblings — the
external whole-PR bot re-scans and finds the next one, one wasted round per site.

**Recurrence = hard STOP gate.** The FIRST time a later round (internal or
external-bot) surfaces the SAME class at a NEW site — proof the prior review was
delta-scoped — STOP point-fixing; do NOT push another single-site patch. In the
SAME round escalate the FIX to whole-surface class-closure (grep the whole parcel
— source + tests + docs + generated artifacts — and fix-or-justify EVERY site)
and the REVIEW to a whole-repo same-class audit, with "zero remaining" as the bar
to push. Distinct from an architectural debate (see `pr-bot-loop`): a debate
resolves a genuine design FORK; a recurring class with an unambiguous fix is
incomplete APPLICATION of an agreed fix — close it, don't debate it.

Enumerate the class in the FIRST dispatch for any term/status/field/invariant
change — don't wait for a recurrence. Ready-to-paste reviewer mandate:

> Name the defect class this fix addresses. Enumerate every OTHER site matching
> its shape — sibling routes, parallel handlers/modules, other callers of the
> same helper/pattern — and report each site the fix did NOT cover. A fix that
> closes the flagged site but leaves siblings is INCOMPLETE.

## Coupled-artifact sweep — gate the mechanical, review the judgment
A functional change usually OBLIGATES paired non-code updates; skipping them is a
blocking finding the external bot WILL raise. Discover the repo's OWN
code↔artifact coupling from its governance docs (`AGENTS.md` / `CONTRIBUTING` /
spec tree) — do not assume a fixed list. Repo-agnostic coupling KINDS (the repo
names the actual files): config/env knob ⇒ ops/config reference; requirement-
bearing code/tests ⇒ traceability tags + requirement index; new API status/code
⇒ API/OpenAPI schema; architectural decision ⇒ ADR + its index; pinned-line-
shifting test edits ⇒ the baseline.

Split by determinism:
- **Mechanical half → the GATE** (Procedure step 2, zero reviewer cost): does the
  tag exist, is the index current, is the baseline in sync. This INCLUDES
  **dependency / traceability-index coherence** — if the repo maintains a
  requirement or dependency index (a DAG, a traceability map), verify no dangling
  reference, no cycle, and every prerequisite a requirement's PROSE implies is
  present in its machine dependency edges. Run the repo's own validator; if the
  repo has no cycle / prereq-completeness check, FLAG it as a repo gap and cover
  it by hand meanwhile. Never encode the repo's index schema here.
- **Judgment half → the reviewer**: does the artifact's PROSE actually describe
  the new behavior (not merely that the file was touched).

**Coupling-manifest hook.** The repo should expose a coupling manifest
(change-kind → obligated artifacts) in its governance docs — e.g. "a new model
field obligates its model doc + domain model + bounding rule + every ADR that
enumerates the model"; "a new requirement obligates its heading + index row +
every ADR / architecture 'depicted-requirements' list"; "a status or dependency
change obligates every dependent's edge." Present → propagate to every listed
artifact; absent/incomplete → fall back to grep AND raise the missing entry as a
repo gap. The manifest CONTENT and the validator SCRIPT live in the repo and are
DISCOVERED; this portable skill only mandates consulting them, never hardcodes a
repo's specifics.

## Author the contract wide (upstream of review)
The reviewer checks the delta against the contract it was handed — any dimension
the contract omits, the review starts BLIND on, and the blind spot recurs round
after round. Before dispatching, the contract must itself enumerate: (a) for a
mapping function, the INPUT TAXONOMY (shapes, branched/legacy fields, nested
inputs, overlap ordering, none-match fall-through); (b) the COUPLED non-code
artifacts for this change kind. A contract naming only happy-path shapes
guarantees the bot finds the dropped siblings later, one per round.

## Procedure
1. **Diff.** Review runs BEFORE a PR (per `review-before-pr`): take the branch
   diff `git -C .worktrees/<task_id> diff main...HEAD` (`gh pr diff <pr>` only for
   a pre-existing PR you did not just create).
2. **Gates first — ALL of them.** Run EVERY deterministic check the repo defines,
   not just test/lint/typecheck: discover the full set from `package.json`
   scripts, any `scripts/`/`tools/` dir, and governance docs — often a
   requirement-index checker, traceability-tag linter, pinned-line baseline,
   schema/contract and index-coherence validator. Any RED gate goes back to the
   fixer (implementer, or polly for a directly-authored artifact) to drive green
   BEFORE the reviewer is involved — zero reviewer tokens on a defect a validator
   already names.
3. **Dispatch a DIFFERENT-vendor reviewer** (`claude_code` / `codex` / `opencode`
   / `cursor` / `hermes` / `pi`, vendor ≠ the author's), task-based title
   (`review-<slug>`, never the vendor name):
   `sys_session_send(agent=…, title="review-<task_slug>", args={purpose: "review",
   input:"<diff> + <contract>. Run [FOCUSED] then [WIDE] across all four axes —
   blast-radius (callers, consumer/event ripple, parallel surfaces, test-surface,
   whole-parcel grep for a renamed term/semantics, env-dependent claims, claim completeness
   incl. unchanged-asserting claims); sibling-class (name the class + every
   untouched site); input-domain (full taxonomy incl. legacy/nested/overlap/
   none-match); coupled-artifact (each obligated non-code artifact + its prose).
   If the diff includes docs, ALSO run [SELF-CONSISTENCY] + [GOVERNANCE]. Classify each
   finding blocking / non-blocking / suggestion, one line per item; do not edit."})`. Give the diff + adjacency, withhold the
   implementer's transcript/worktree, permit repo read. Emit the dispatch in the
   SAME turn you decide to review (never end a turn having only announced, with no
   tool call — that dropped turn stalls the run); then end your turn and collect
   the structured report with `sys_read_inbox` (use `sys_session_get_history` only to debug an
   empty/unclear result).
4. The reviewer SURFACES issues; it never edits and never opens a PR.
5. **Each blocking issue loops back to the fixer on the SAME branch.** Delegated:
   re-send to the SAME implementer conversation (reuse its `agent`+`title`, or
   `session_id`, `purpose: "implement"`) so it keeps its worktree/branch. Direct-
   authoring: polly revises the prose itself, re-runs the gates + all applicable
   passes, and re-dispatches the review. Same loop, only the fixer differs. Log each blocking issue as a
   fix-task in the registry scoped to the same worktree. Then loop to step 1.
6. **Green gates + zero blocking = passes review; only now does the commit reach
   the remote.** Delegated: `git push`/`gh pr create` are blocked by
   `require_pr_review` until the marker records the CURRENT commit — write it
   (`mkdir -p .worktrees/<task_id>/.polly && git -C .worktrees/<task_id> rev-parse HEAD > .worktrees/<task_id>/.polly/review-passed`) THEN tell the SAME implementer to push
   its branch and open the PR. Direct-authoring: polly (not gated by
   `require_pr_review`) pushes and opens its OWN reviewed PR. Mark it ready in the
   registry with the PR URL and leave it for the human. **polly does NOT merge.**
7. Contract unsatisfiable after a few loops → stop and escalate to the human with
   specifics.

## Notes
- Needs ≥2 AVAILABLE workers — a reviewer of a DIFFERENT vendor than the author
  (per polly's roster preflight). If only one worker, or only one vendor that can
  review this author, is bootable, you CANNOT run cross-vendor review: say so
  explicitly, don't dispatch a reviewer that can't boot, and pull the human in at
  the plan gate.
- Non-blocking issues / suggestions → registry follow-ups; they don't block the PR.
