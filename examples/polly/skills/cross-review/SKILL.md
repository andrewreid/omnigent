---
name: cross-review
description: Verify an implementer's diff with an INDEPENDENT, different-vendor sub-agent (diff plus contract only); turn blocking issues into fix-tasks and loop until clean.
---

# cross-review — independent verification

The implementer never signs off on its own work — a different model does, and
review is a sub-agent that returns a structured report, not a transcript
anyone needs to read through.

Every review runs TWO passes, ALWAYS, in one reviewer dispatch:
- **[FOCUSED]** — diff-vs-contract: "does this change do what it claims, against
  the acceptance contract?" This is the narrow, confirmatory pass.
- **[WIDE]** — blast-radius: "what ELSE does this change touch that the diff does
  not show?" This is the pass that catches the failures a diff-local reviewer is
  structurally blind to.

These are not "confirmatory review, with adversarial as an occasional
escalation". Both passes run on every review. The WIDE pass MAY fast-exit with
an explicit "no blast radius — nothing else touches this" verdict when that is
genuinely true, but it is NEVER skipped. Skipping the wide pass is exactly how a
one-line fix becomes a multi-round whack-a-mole: the stragglers the diff didn't
show surface one at a time, each its own review round. Match the review DEPTH to
the surface (rich surfaces get an ADVERSARIAL wide pass — see below), but the two
passes themselves are not optional.

## The two passes — label them in the reviewer's report
Instruct the reviewer to structure its report under both headings so you can see
each pass ran:

**[FOCUSED] — diff vs contract.** The narrow pass. Reviewer gets the diff + the
acceptance contract and answers whether the change satisfies the contract. Enough
on its own only for isolated, low-state changes.

**[WIDE] — blast radius.** The reviewer must WALK this checklist and report a
line per item (a hit, or "clear"):
- **All callers of every changed function/symbol.** Enumerate them. Flag sites
  that SHOULD have changed to match but didn't.
- **Consumer / event ripple.** Event-type consumers of anything the change emits
  or renames; generated-client mirrors of a changed API shape; anything
  downstream that decodes the changed payload.
- **PARALLEL SURFACES.** A fix on one surface MUST be applied to every surface
  running the same logic. Grep the sibling surface — do not just re-read the
  changed one. Canonical pairs: client-render ↔ server/SSR loader/prefetch; a
  read path ↔ its cached / materialized twin; an API handler ↔ its
  generated-client mirror. If the change fixed one and left the twin untouched,
  that is a blocking finding.
- **TEST-SURFACE coverage.** Tests must exercise EVERY surface the change touches
  (e.g. the component AND the route-loader), not just the convenient one. A green
  suite that hits only one surface HIDES the bug on the parallel one — treat
  single-surface coverage of a multi-surface change as a gap.
- **ROUND-1 WHOLE-PARCEL GREP.** When the change renames or redefines a term,
  status, concept, or claim, grep the ENTIRE parcel — source + tests + docs +
  generated/published artifacts — for the OLD term/semantics. Classify every hit
  and fix-or-justify each, in THIS round. A vocab/claim change reviewed only near
  the diff guarantees multi-round whack-a-mole as stragglers surface one at a
  time.
- **ENV-DEPENDENT CLAIMS.** Static example configs and docs must assert only
  intrinsic truths, never environment-dependent facts. Flag any "resolves at X
  login / reachable via Y / routes through Z / this provider answers" claim — the
  auth path, model reachability, and which provider resolves are deployment
  facts, not properties of the file.
- **CLAIM COMPLETENESS.** When the diff makes claims about named files,
  workflows, or config, audit EVERY such claim — both what is REMOVED and what is
  ADDED, not a convenient spot-check. A partial audit catches one direction and
  misses its symmetric twin: a stale claim left behind by a removal, or a fresh
  claim that overreaches what it added. Enumerate all of them; verify each.

When the wide pass finds genuinely nothing across the whole checklist, it says so
in one line and moves on. It does not get skipped to save a dispatch.

## When the artifact under review is a doc / ADR / spec — two more mandatory passes
The two passes above are tuned for a CODE diff: claim-vs-code and blast radius.
When the reviewed artifact is a documentation / ADR / spec file rather than code,
those are necessary but NOT sufficient — a prose artifact fails in ways a
code-centric lens is structurally blind to. Run BOTH of the following in ADDITION
to [FOCUSED] and [WIDE], and label them in the reviewer's report:

**[SELF-CONSISTENCY] — does the document contradict ITSELF?** The [FOCUSED] pass
audits each claim against its source IN ISOLATION, so two mutually-contradictory
claims can each individually "pass" while directly contradicting each other. This
pass reads the document as a whole and reconciles it against itself: do its stated
consequences / scope / non-goals match its own decision and any evidence it cites
(a spike, a finding, a benchmark it references)? Reconcile statements introduced
across MULTIPLE edit passes — hunt STALE scope left behind when a later edit
superseded an earlier claim but the earlier wording was never removed. A document
that is internally inconsistent is wrong even when every individual claim traces to
a real source.

**[GOVERNANCE] — does the change obey the repo's OWN doc conventions?** Load the
repository's own documentation / ADR / spec governance — an ADR lifecycle (e.g.
Proposed → Accepted → Superseded), any decision registry / index that must stay in
sync with the documents it lists, numbering / status / cross-reference rules — and
verify the change CONFORMS. Flag a status transition applied prematurely (e.g.
marked Accepted before the gate that accepts it), a new document that skips a
required lifecycle state, a registry / index copy that disagrees with the document
it points at, or a duplicated status that drifts between copies. The repo's own
rules are the contract here; the diff must not violate them.

These two passes are as mandatory for a doc artifact as [WIDE] is for code: run
both, label both, fast-exit a pass in one line only when it genuinely finds
nothing. A doc/ADR/spec diff that skips them is not reviewed.

## Procedure
1. Get the task's diff. Per `review-before-pr`, review runs BEFORE a PR exists,
   so take the branch diff: `git -C .worktrees/<task_id> diff main...HEAD`. Use
   `gh pr diff <pr>` only for a pre-existing PR you did not just create.
2. Run the deterministic gates first — tests / lint / typecheck via
   `sys_os_shell`. If red, re-dispatch the implementer to drive it green first;
   don't involve the reviewer yet.
3. Dispatch a DIFFERENT-vendor sub-agent as reviewer: pick any AVAILABLE worker
   whose vendor differs from the implementer's — `claude_code`, `codex`,
   `opencode`, `cursor`, `hermes`, or `pi` (e.g. Claude built it → any of
   `codex` / `opencode` / `cursor` / `hermes` / `pi`, and so on). Use a
   task-based title such as `review-auth-refactor`, never the raw vendor name:
   `sys_session_send(agent="claude_code"|"codex"|"opencode"|"cursor"|"hermes"|"pi", title="review-<task_slug>",
   args={purpose: "review", input: "<the diff> + <the acceptance contract>. Run
   BOTH passes and report under both headings: [FOCUSED] diff-vs-contract, then
   [WIDE] blast-radius — walk the blast-radius checklist (callers, consumer/event
   ripple, PARALLEL SURFACES, test-surface coverage, whole-parcel grep for any
   renamed term, env-dependent claims, claim completeness) and report a line per
   item; if the reviewed artifact is a doc/ADR/spec rather than code, ALSO run
   [SELF-CONSISTENCY] (does it contradict itself / carry stale scope from an
   earlier edit pass?) and [GOVERNANCE] (does it obey the repo's own ADR/spec
   lifecycle, numbering, and registry-sync rules?). Report
   blocking / non-blocking / suggestions. Do not edit code."})`. Give it the diff
   as text — do NOT point it at the implementer's worktree. For the WIDE pass the
   reviewer needs enough context to trace ripple: give it the changed surface
   plus its adjacency (callers, sibling surfaces, the type it targets), not just
   the raw hunk. Fetch the diff and emit the
   `sys_session_send` call in the SAME turn you decide to review — never end a
   turn having only announced "I'll load cross-review and fetch the diff" with
   no tool call (that dropped turn stalls the run; nothing dispatches and no
   inbox wake arrives). Once the reviewer dispatch is in flight, end your turn;
   collect the inbox-delivered structured report with `sys_read_inbox` when it
   returns. Use `sys_session_get_history` only to debug an empty or unclear
   review result.
4. The reviewer SURFACES issues; it does not fix them.
5. For each **blocking** issue: add a fix-task to the registry scoped to the
   same worktree, and send the concrete fixes back to the SAME implementer
   conversation via `sys_session_send` — reuse the original implementer's
   `agent` + `title` (or address it by `session_id`) with
   `purpose: "implement"`, so the worker keeps its worktree/branch context and
   pushes the fixes to the SAME branch. No PR exists yet — review runs
   pre-PR (per `review-before-pr`), so blocking issues loop back on the BRANCH,
   not against an open PR. A new title would spawn a fresh worker with no
   memory of the task. Then loop to step 1.
6. When gates are green AND there are zero blocking issues, the diff passes
   review. Now (and only now) tell the SAME implementer to open the PR on its
   reviewed branch (per `review-before-pr` — the PR is opened on the reviewed
   product, and only the implementer opens PRs). Then mark it ready in the
   registry (with its PR URL) and leave it for the human to merge. polly does
   NOT merge it.
7. If the contract can't be satisfied after a few loops, stop and escalate to
   the user with specifics.

## Match review DEPTH to the surface — the wide pass goes adversarial
The two passes above always run. On a rich surface the WIDE pass is not just a
blast-radius walk — it must go ADVERSARIAL. A confirmatory-only pass confirms
exactly the cells the contract lists and is BLIND to every state nobody
enumerated. On a **combinatorially-rich surface** — serializers,
form↔payload round-trips, state machines, derivations/classifiers whose output
depends on many input states (loaded vs edited × valid/invalid/empty inputs ×
per-line vs aggregate flags × present/absent references) — the contract can only name
a handful of states, so a confirmatory pass keeps signing off diffs that the external
Codex bot's whole-state-space reasoning then breaks LATER, one edge per round. That
"bot found another edge → patch → re-review → bot found the next" grind is a
confirmatory review that should have been ADVERSARIAL.

Classify the surface BEFORE dispatching the reviewer. If it is rich, deepen the
WIDE pass into a full adversarial state-space attack:
- **Widen the reviewer's context.** Give it the changed surface PLUS its adjacency
  (the read / serialize / validate functions around the diff, the type it targets,
  the consumers) — not just the raw hunk. Independence comes from a DIFFERENT vendor
  and withholding the implementer's transcript, NOT from starving the reviewer of
  context.
- **Change the mandate.** Not "verify the fix" but "find the states that break":
  instruct it to ENUMERATE the state space of the surface and ATTACK the cells the
  contract did not name — "list every combination of {axes}; for each, does
  load→edit→serialize round-trip correctly? which combinations silently drop,
  mis-write, or wrongly block?". Mirror the external bot.
- **Demand the FULL list in one pass.** The reviewer surfaces every issue it can find
  now, exhaustively — not a trickle that becomes N rounds.
- **Front-run the external bot.** Prefer running this adversarial pass on the SAME
  engine as the external reviewer (`codex`) so its whole class of findings lands in
  the FAST internal loop instead of the slow PR loop (see `pr-bot-loop` → front-run
  the bot). Keep it a DIFFERENT vendor from the implementer.

The cheapest place to close a state-space bug is the implementer's FIRST pass: pair
this with `review-before-pr` — the acceptance contract for a rich surface must name
the state-space axes and require an invariant / round-trip matrix test as a delivered
artifact, so the surface is built right once instead of hardened reactively.

## Notes
- Cross-review requires a reviewer from a DIFFERENT vendor than the implementer,
  so it needs at least two AVAILABLE workers (per polly's roster preflight). If
  only one worker — or only one vendor that can review this implementer's PR —
  is available on the machine, you CANNOT run independent cross-vendor review:
  don't dispatch a reviewer that can't boot, say so explicitly, and pull in the
  human at the plan gate.
- Give the reviewer ONLY the diff + contract — never the implementer's
  transcript or worktree. The cross-vendor independence is the whole point.
- Review is a coding sub-agent (`claude_code`/`codex`/`opencode`/`cursor`/`hermes`/`pi`) dispatched with
  `purpose: "review"` — a DIFFERENT vendor from the one that built the diff. It
  reports issues and never edits; only the implementer opens a PR, so a stray
  reviewer edit never reaches the deliverable.
- Non-blocking issues / suggestions go in the registry as follow-ups; they
  don't block the PR.
