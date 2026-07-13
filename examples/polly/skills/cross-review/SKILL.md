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
- **[WIDE]** — wide-angle sweep: "what ELSE does this change touch that the diff
  does not show?" This is the pass that catches the failures a diff-local
  reviewer is structurally blind to. It carries THREE mandatory axes, not one —
  see below.

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

**[WIDE] — wide-angle sweep.** The wide pass carries THREE axes, and the
reviewer must answer ALL THREE as mandatory questions — the diff-local view
covers only the first:
- **Downstream blast-radius** — who CONSUMES the contract this diff changed?
  (callers, event consumers, generated-client mirrors, anything that decodes the
  changed payload). This is the classic ripple axis.
- **Sibling-class sweep** — where else does this SAME defect shape exist that the
  diff did NOT touch? (parallel modules/handlers/routes running the same logic,
  other callers of the same helper/pattern). See "Is the finding a one-off or an
  instance of a class?" below.
- **Coupled-artifact / traceability sweep** — for this change KIND, which
  NON-CODE artifacts must move in lockstep, and did they? (docs, spec, schema,
  traceability index, config reference). See "Coupled-artifact sweep" below.

The reviewer must WALK this checklist and report a line per item (a hit, or
"clear"):
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

When the wide pass finds genuinely nothing across the whole checklist, it says so
in one line and moves on. It does not get skipped to save a dispatch.

## Is the finding a one-off or an instance of a class?
Before treating any fix as scoped to its flagged site, ask whether it addresses a
REPEATABLE pattern or a single one-off. A typo, a single wrong constant, a
one-line copy-paste slip is a one-off — fix the site and move on. But a fix whose
shape generalizes — "X not re-checked on retry", "schema omits an outcome code",
"error mis-classified by rule Y", "value not normalized before compare" — is an
instance of a CLASS, and must be treated as CLASS-CLOSURE: the fix task AND the
review must enumerate and cover EVERY instance of that class across the codebase,
found by grepping the pattern, not just the one flagged site.

A point-fix plus a delta-scoped review provably CANNOT pre-empt siblings: the
review only looked at the changed hunk, so a sibling the diff never touched was
never in view. An external whole-PR reviewer re-scans the whole surface and finds
the class at the next untouched site — one wasted round per site, indefinitely.
Close the class in one pass instead.

Ready-to-paste reviewer-mandate line (hand this to the reviewer whenever a fix
looks like a class instance):

> Name the defect class this fix addresses. Enumerate every OTHER site matching
> that class's shape — sibling routes, parallel handlers/modules, other callers
> of the same helper/pattern — and report each site the fix did NOT cover. A fix
> that closes the flagged site but leaves siblings is INCOMPLETE.

## Coupled-artifact sweep
A functional change usually OBLIGATES paired non-code updates; skipping them is a
blocking finding the external bot WILL raise. The reviewer must:
1. Read the repo's OWN contributor / spec-governance docs — an `AGENTS.md`, a
   `CONTRIBUTING`, a spec or docs tree — to LEARN that repo's code↔artifact
   coupling rules. Discover them; do not assume a fixed list.
2. For each coupling APPLICABLE to this diff's change kind, verify the paired
   artifact was updated AND that its human-facing PROSE actually reflects the new
   behavior — not merely that the file was touched.

Generic, repo-agnostic examples of coupling KINDS (a repo's own docs say which
apply and name the actual files):
- a new / changed configuration or env knob ⇒ its operations / config reference.
- requirement-bearing code or tests ⇒ the repo's traceability tags + requirement
  index.
- a new API response status / code ⇒ the API schema / OpenAPI description.
- an architectural or interface decision ⇒ an ADR + its index.
- test edits that shift a pinned line ⇒ the pinned-line baseline.

Split the labor by determinism: the MECHANICAL half — does the tag exist, is the
index current, is the baseline in sync — belongs in the GATE (a validator proves
it, at zero reviewer cost, per Procedure step 2). Only the JUDGMENT half — does
the artifact's PROSE correctly describe the new behavior — is the reviewer's to
make.

## Recurrence rule
If a LATER review round, or an external-bot round, surfaces the SAME class at a
NEW site, that is proof the prior review was delta-scoped, not class-scoped.
Respond by WIDENING, immediately: escalate the FIX from a point-fix to a
whole-surface class-closure, and the REVIEW mandate from delta-scoped to a
whole-repo same-class audit.

Distinguish this from an architectural-debate escalation (see `pr-bot-loop`). A
debate resolves a genuine design FORK — two defensible directions, no
unambiguous answer. A recurring class with an unambiguous fix is NOT a fork: it
is incomplete APPLICATION of an already-agreed fix. Close the class; do not
debate it.

## Procedure
1. Get the task's diff. Per `review-before-pr`, review runs BEFORE a PR exists,
   so take the branch diff: `git -C .worktrees/<task_id> diff main...HEAD`. Use
   `gh pr diff <pr>` only for a pre-existing PR you did not just create.
2. Run the deterministic gates first — but run EVERY deterministic check the
   repo defines, not only tests / lint / typecheck. Discover the full validator
   set from the repo itself: its `package.json` scripts, any `scripts/` or
   `tools/` directory, and its contributor / spec-governance docs. That set
   routinely includes spec / traceability / governance validators beyond the
   usual three — a requirement-index checker, a traceability-tag linter, a
   pinned-line baseline verifier, a schema/contract validator. Run them all via
   `sys_os_shell`. A functional change that fails ANY repo validator — a missing
   traceability tag, a stale requirement/spec index, a drifted pinned-line
   baseline — is a RED gate: send it back to the implementer to drive green
   first; don't involve the reviewer yet. These deterministic couplings are
   caught here for free, with ZERO reviewer tokens — never spend a reviewer on a
   defect a validator already names.
3. Dispatch a DIFFERENT-vendor sub-agent as reviewer: pick any AVAILABLE worker
   whose vendor differs from the implementer's — `claude_code`, `codex`,
   `opencode`, `cursor`, `hermes`, or `pi` (e.g. Claude built it → any of
   `codex` / `opencode` / `cursor` / `hermes` / `pi`, and so on). Use a
   task-based title such as `review-auth-refactor`, never the raw vendor name:
   `sys_session_send(agent="claude_code"|"codex"|"opencode"|"cursor"|"hermes"|"pi", title="review-<task_slug>",
   args={purpose: "review", input: "<the diff> + <the acceptance contract>. Run
   BOTH passes and report under both headings: [FOCUSED] diff-vs-contract, then
   [WIDE] wide-angle sweep across all THREE axes — (1) downstream blast-radius
   (callers, consumer/event ripple, parallel surfaces, test-surface coverage,
   whole-parcel grep for any renamed term, env-dependent claims); (2)
   sibling-class sweep (name the defect class, enumerate every un-touched site
   matching its shape); (3) coupled-artifact sweep (read the repo's contributor /
   spec-governance docs, verify each non-code artifact this change kind obligates
   was updated and its prose reflects the new behavior) — report a line per item.
   Report blocking / non-blocking / suggestions. Do not edit code."})`. Give it the diff
   as text and bar the implementer's WORKTREE and REASONING (its transcript) —
   but the sibling-class and coupled-artifact sweeps REQUIRE the reviewer to grep
   for siblings and inspect the docs/spec tree, so explicitly PERMIT it to read a
   clean checkout of the repo. Independence = a different vendor + withholding the
   implementer's transcript/worktree, NOT denying repo read. For the WIDE pass the
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
  and withholding the implementer's transcript/worktree, NOT from starving the
  reviewer of context — a clean repo checkout for the sibling-class and
  coupled-artifact sweeps is permitted and expected.
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

## Input-domain coverage — the pure-function analogue of the state-space attack
The state-space attack above enumerates the STATES of a stateful surface. Its
pure-function twin enumerates the INPUT SHAPES of a mapping function, and it runs
as a peer WIDE-pass axis whenever the diff touches one — the same move applied to a
different kind of code.

When a change touches a function that MAPS inputs to decisions — a classifier,
parser, mapper, router, dispatcher, normalizer, or error/exception handler —
enumerate the FULL input taxonomy it must accept, not only the shapes the diff
exercised. Cover: every distinct input shape/variant; every field or property the
function may branch on (including alternate/legacy field names carrying the same
meaning); nested or wrapped inputs (a cause/inner chain, envelope, or union
member); and the ORDERING of overlapping matches (a broad early match that
short-circuits before a more-specific later branch). Verify every branch of the
decision tree, and verify what happens for an input that matches NONE. A function
that handles the common shape but drops a sibling shape, mis-orders an overlapping
match, or falls through silently on an unrecognized shape is a BLOCKING finding.
State the taxonomy explicitly and check each member against the code.

Both axes are the same discipline — "enumerate the space, then check each member" —
applied to different kinds of code: state-space = the states of a stateful surface;
input-domain = the input shapes of a mapping function. A diff looks correct
precisely because the shape it dropped is never in the diff; only enumerating the
whole taxonomy surfaces the gap.

## The review can never out-scope its contract — author the contract wide
The reviewer checks the DELTA against the acceptance contract it was handed, so any
dimension the contract omits is a dimension the review starts BLIND on. The
reviewer's ceiling is the contract: however diligent the review, a narrow contract
makes the blind spot recur round after round. This is upstream of review — a
discipline for whoever AUTHORS the acceptance contract BEFORE dispatching the
reviewer, not something the reviewer can recover on its own.

Before dispatching, the contract must itself enumerate:
1. **For a mapping function** (classifier, parser, mapper, router, dispatcher,
   normalizer, error/exception handler) — the INPUT TAXONOMY the function must
   cover: every shape/variant, every field it branches on (including
   alternate/legacy names carrying the same meaning), nested/wrapped inputs, the
   ordering of overlapping matches, and the none-match fall-through. If the contract
   omits the taxonomy, the reviewer inherits the omission — the input-domain axis
   has nothing to check the delta against.
2. **The coupled non-code artifacts** that must move in lockstep with this change
   kind (discovered from the repo's OWN contributor / spec-governance docs — see
   "Coupled-artifact sweep").

Write the contract WIDE so the review can be wide. A contract that names only the
shapes the happy path exercises guarantees the review keeps signing off diffs whose
dropped siblings the external whole-PR bot then finds LATER, one per round.

## Notes
- Cross-review requires a reviewer from a DIFFERENT vendor than the implementer,
  so it needs at least two AVAILABLE workers (per polly's roster preflight). If
  only one worker — or only one vendor that can review this implementer's PR —
  is available on the machine, you CANNOT run independent cross-vendor review:
  don't dispatch a reviewer that can't boot, say so explicitly, and pull in the
  human at the plan gate.
- Give the reviewer the diff + contract, and bar the implementer's transcript
  and worktree — that reasoning-independence is the whole point. This does NOT
  mean starving it of repo read: the sibling-class and coupled-artifact sweeps
  need a clean checkout to grep siblings and inspect the docs/spec tree, so
  PERMIT that. Independence = a different vendor + withholding the implementer's
  transcript/worktree, NOT denying repo read.
- Review is a coding sub-agent (`claude_code`/`codex`/`opencode`/`cursor`/`hermes`/`pi`) dispatched with
  `purpose: "review"` — a DIFFERENT vendor from the one that built the diff. It
  reports issues and never edits; only the implementer opens a PR, so a stray
  reviewer edit never reaches the deliverable.
- Non-blocking issues / suggestions go in the registry as follow-ups; they
  don't block the PR.
