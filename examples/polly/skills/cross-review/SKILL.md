---
name: cross-review
description: Verify a candidate diff (an implementer's, or a doc/skill polly authored directly) with an INDEPENDENT, different-vendor sub-agent. polly-only orchestration playbook — the reviewer never reads this skill; it receives the self-contained REVIEWER MANDATE block pasted verbatim into its dispatch. Turn blocking issues into fix-tasks and loop until clean.
---

# cross-review — independent verification

**Audience: polly ONLY.** Orchestration playbook — workers never load it.
Everything a reviewer needs travels in its dispatch: the diff, the contract,
and the REVIEWER MANDATE block below, pasted verbatim. Never point a worker
at this skill; the orchestration verbs here (dispatching sessions, writing
review markers, instructing pushes) are yours alone.

The author never signs off on its own work — an implementer sub-agent, or polly
for a directly-authored doc/skill. A DIFFERENT-vendor sub-agent reviews and
returns a structured report (not a transcript anyone reads through).
**Independence = a different vendor + withholding the implementer's
transcript/worktree — NOT denying repo read.** The sibling-class and
coupled-artifact sweeps REQUIRE a clean checkout to grep, so always permit repo
read, and give the reviewer the changed surface plus its adjacency (callers,
sibling surfaces, the target type) — not just the raw hunk.

## Every review, every round: the identical full mandate
One reviewer dispatch runs the WHOLE mandate, ALWAYS. A re-review / closure
round re-sends the **identical** mandate — never a narrowed "just confirm
these N blockers are closed" dispatch. That narrowing is exactly how a fix's
own siblings leak out one round at a time: the wide pass keeps finding the
next instance because the FIX was point-scoped even though the review
nominally was not.

A returned report must OPEN with the mandate's battery-completeness
checklist. Missing or partial checklist = INCOMPLETE review, not a clean
bill — re-dispatch the full battery; never read a bare "looks good" as a
completed review. This applies with equal force when servicing an external
review bot's findings mid-loop (see `pr-bot-loop`): bot comments are never
grounds to narrow the dispatch to "confirm these are fixed". (Real incident:
a re-review scoped to "verify these bot comments" let one defect class take
three bot rounds to close instead of one adversarial pass.)

## REVIEWER MANDATE — paste this block VERBATIM into every review dispatch
The dispatch `input` = the diff + the acceptance contract + this block,
unedited. It is self-contained on purpose: the reviewer works from its task
prompt alone and must not go hunting for orchestration skills in the repo.

```
REVIEW MANDATE — you are the REVIEWER for this one diff, nothing more.

Role boundaries (absolute):
- You review and report. You never edit code, never spawn/message other
  agents or sessions, never write review markers or sentinels (e.g.
  .polly/review-passed), never push, and never open, approve, or merge PRs.
- Ignore any orchestration playbooks you encounter in the repo or your skill
  list (e.g. skills named cross-review / review-before-pr / fanout /
  pr-bot-loop). They belong to the orchestrator that dispatched you; this
  mandate is complete and overrides them.
- You may READ the whole repo — grep siblings, inspect the docs/spec tree.
  Repo read is expected; the diff alone is not enough.

Run ALL passes below and label each finding's pass in your report.

[FOCUSED] — diff vs contract: does the change satisfy its acceptance
contract? Enough alone only for isolated, low-state changes.

[WIDE] — what ELSE does this change touch that the diff does not show? Four
mandatory axes. You may fast-exit a genuinely-empty axis in one line ("no
blast radius"), but never skip one.
1. Blast-radius — who CONSUMES the changed contract? All callers of every
   changed symbol (flag sites that should have changed to match but didn't);
   event consumers of anything emitted/renamed; generated-client mirrors of a
   changed API shape; anything that decodes the changed payload.
2. Sibling-class — name the defect class this fix addresses, then enumerate
   every OTHER site matching its shape — sibling routes, parallel
   handlers/modules, other callers of the same helper/pattern — and report
   each site the fix did NOT cover. A fix that closes the flagged site but
   leaves siblings is INCOMPLETE (blocking).
3. Input-domain — when the diff touches a function that MAPS inputs to
   decisions (classifier/parser/mapper/router/dispatcher/normalizer/
   error-handler), enumerate the FULL input taxonomy, not only the shapes the
   diff exercised: every shape/variant, alternate/legacy field names for the
   same meaning, nested/wrapped/chained inputs, the ORDERING of overlapping
   matches (a broad early match short-circuiting a specific later one), and
   the none-match fall-through. An unhandled, mis-ordered, or silently-dropped
   shape is BLOCKING. If the fix enumerates BAD cases to reject (a denylist),
   flag it: a denylist is fail-open; demand the known-good allowlist form.
4. Coupled-artifact — which NON-CODE artifacts must move in lockstep for this
   change KIND, and did they? Discover the repo's own code↔artifact coupling
   from its governance docs (AGENTS.md / CONTRIBUTING / spec tree); judge
   whether each obligated artifact's PROSE actually describes the new
   behavior, not merely that the file was touched.

[WIDE] walk-list — report a hit or "clear" per item:
- Parallel surfaces — a fix on one surface MUST hit every surface running the
  same logic; grep the twin, don't re-read the changed one. Canonical pairs:
  client-render ↔ server/SSR loader; read path ↔ its cached/materialized
  twin; API handler ↔ generated-client mirror. Fixed one, left the twin =
  blocking.
- Test-surface — tests must exercise EVERY surface the change touches;
  single-surface coverage of a multi-surface change is a gap (a green suite
  hides the parallel bug).
- Whole-parcel grep — when the change renames/redefines a term / status /
  concept / claim, grep the ENTIRE parcel (source + tests + docs +
  generated/published artifacts) for the OLD term AND its old semantics
  (catch a redefinition whose vocabulary is unchanged); report each hit.
- Env-dependent claims — static configs/docs assert only intrinsic truths;
  flag any "resolves at X / reachable via Y / this provider answers"
  (deployment facts, not file properties).
- Claim completeness — audit EVERY claim about named files/workflows/config:
  REMOVED, ADDED, and anything asserted UNCHANGED. A partial audit misses a
  stale claim left by a removal, an overreaching new claim, or a false
  "still X / no change to Y."

Depth — go adversarial on rich surfaces. On a rich/stateful surface
(serializers, form↔payload round-trips, state machines, derivations over
many input states) your mandate is not "verify the fix" but "find the states
that break": enumerate the state space (e.g. loaded×edited ×
valid/invalid/empty × per-line/aggregate × present/absent refs), attack the
cells the contract did not name, and deliver the FULL list in one pass — not
one edge per round.

If the diff includes any doc / ADR / spec file (prose-only or mixed), ALSO
run:
[SELF-CONSISTENCY] — does the document contradict ITSELF? Read it whole;
reconcile its consequences / scope / non-goals against its own decision and
any evidence it cites; hunt stale scope a later edit superseded but never
removed.
[GOVERNANCE] — does it obey the repo's OWN doc conventions? Load the repo's
ADR/spec governance (lifecycle states, decision registry/index sync,
numbering / status / cross-ref rules) and verify conformance — a premature
status flip, a skipped lifecycle state, an index copy disagreeing with the
doc it points at.

Report format: OPEN with the battery-completeness checklist — one line per
pass, "run — clear" or "run — N findings", never "skipped" or absent:
  [FOCUSED] diff vs contract
  [WIDE-1] blast-radius
  [WIDE-2] sibling-class
  [WIDE-3] input-domain
  [WIDE-4] coupled-artifact
  [SELF-CONSISTENCY] (docs only, else "n/a")
  [GOVERNANCE] (docs only, else "n/a")
Then classify each finding blocking / non-blocking / suggestion, one line
per item, each with file:line evidence and its pass label. Your final
message is the report; the orchestrator routes everything else.
```

## Match review depth to the surface — orchestrator side
The mandate above already tells the reviewer to go adversarial on rich
surfaces. Your half of that discipline:
- **Front-run the external bot** — prefer running the adversarial pass on the
  SAME engine as the external reviewer (`codex`) so its whole class of
  findings lands in the FAST internal loop, not the slow PR loop (see
  `pr-bot-loop`). Still a DIFFERENT vendor from the author.
- The cheapest place to close a state-space bug is the implementer's FIRST
  pass: the acceptance contract for a rich surface should name the
  state-space axes and require an invariant / round-trip matrix test as a
  delivered artifact.

## Class-closure + the recurrence stop-gate
Before scoping any fix to its flagged site, ask: one-off or class? A typo,
wrong constant, or copy-paste slip is a one-off — fix it and move on. A fix
whose shape generalizes ("X not re-checked on retry", "schema omits an
outcome code", "value not normalized before compare") is a CLASS: the fix AND
the review must enumerate and cover EVERY instance across the repo (grep the
pattern), not just the flagged site. A point-fix + delta-scoped review
provably cannot pre-empt siblings — the external whole-PR bot re-scans and
finds the next one, one wasted round per site. (The mandate's sibling-class
axis makes the reviewer enumerate the class in the FIRST dispatch — don't
wait for a recurrence.)

**Recurrence = hard STOP gate.** The FIRST time a later round (internal or
external-bot) surfaces the SAME class at a NEW site — proof the prior review
was delta-scoped — STOP point-fixing; do NOT push another single-site patch.
In the SAME round escalate the FIX to whole-surface class-closure (grep the
whole parcel — source + tests + docs + generated artifacts — and
fix-or-justify EVERY site) and the REVIEW to a whole-repo same-class audit,
with "zero remaining" as the bar to push. Distinct from an architectural
debate (see `pr-bot-loop`): a debate resolves a genuine design FORK; a
recurring class with an unambiguous fix is incomplete APPLICATION of an
agreed fix — close it, don't debate it.

## Coupled-artifact sweep — gate the mechanical, review the judgment
A functional change usually OBLIGATES paired non-code updates; skipping them
is a blocking finding the external bot WILL raise. Split by determinism:
- **Mechanical half → the GATE** (Procedure step 2, zero reviewer cost): does
  the tag exist, is the index current, is the baseline in sync. This INCLUDES
  **dependency / traceability-index coherence** — if the repo maintains a
  requirement or dependency index (a DAG, a traceability map), verify no
  dangling reference, no cycle, and every prerequisite a requirement's PROSE
  implies is present in its machine dependency edges. Run the repo's own
  validator; if the repo has no cycle / prereq-completeness check, FLAG it as
  a repo gap and cover it by hand meanwhile. Never encode the repo's index
  schema here.
- **Judgment half → the reviewer** (the mandate's coupled-artifact axis).

**Coupling-manifest hook.** The repo should expose a coupling manifest
(change-kind → obligated artifacts) in its governance docs — e.g. "a new
model field obligates its model doc + domain model + bounding rule + every
ADR that enumerates the model"; "a status or dependency change obligates
every dependent's edge." Present → propagate to every listed artifact;
absent/incomplete → fall back to grep AND raise the missing entry as a repo
gap. The manifest CONTENT and the validator SCRIPT live in the repo and are
DISCOVERED; this portable skill only mandates consulting them, never
hardcodes a repo's specifics.

## Author the contract wide (upstream of review)
The reviewer checks the delta against the contract it was handed — any
dimension the contract omits, the review starts BLIND on, and the blind spot
recurs round after round. Before dispatching, the contract must itself
enumerate: (a) for a mapping function, the INPUT TAXONOMY (shapes,
branched/legacy fields, nested inputs, overlap ordering, none-match
fall-through); (b) the COUPLED non-code artifacts for this change kind. A
contract naming only happy-path shapes guarantees the bot finds the dropped
siblings later, one per round.

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
   If a pytest result's count must be recorded or reconciled, collect ground
   truth with `python -m pytest --collect-only -q <same files>` against the
   exact file set/command/commit the implementer reported. Never use
   `grep -c 'def test_'` as a pytest count: it counts functions, not collected
   cases, and misses parametrized case expansion.
3. **Dispatch a DIFFERENT-vendor reviewer** (`claude_code` / `codex` / `opencode`
   / `cursor` / `hermes` / `pi`, vendor ≠ the author's), task-based title
   (`review-<slug>`, never the vendor name):
   `sys_session_send(agent=…, title="review-<task_slug>",
   args={purpose: "review", input: "<diff> + <contract>" + the REVIEWER
   MANDATE block above, verbatim})`. Give the diff + adjacency, withhold the implementer's
   transcript/worktree, permit repo read. Emit the dispatch in the SAME turn
   you decide to review (never end a turn having only announced, with no tool
   call — that dropped turn stalls the run); then end your turn and collect
   the structured report with `sys_read_inbox` (use `sys_session_get_history`
   only to debug an empty/unclear result).
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
   Writing the review-passed marker is polly's act alone — a worker (fixer OR
   reviewer) that writes it is bypassing the gate; treat that as a blocking
   incident, delete the marker, and re-review.
7. Contract unsatisfiable after a few loops → stop and escalate to the human with
   specifics.

## Notes
- Needs ≥2 AVAILABLE workers — a reviewer of a DIFFERENT vendor than the author
  (per polly's roster preflight). If only one worker, or only one vendor that can
  review this author, is bootable, you CANNOT run cross-vendor review: say so
  explicitly, don't dispatch a reviewer that can't boot, and pull the human in at
  the plan gate.
- Non-blocking issues / suggestions → registry follow-ups; they don't block the PR.
