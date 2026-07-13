---
name: review-before-pr
description: polly's standing quality gate — an implementer must NOT open a PR until its diff has PASSED cross-vendor review. Work happens on a worktree branch; gates + independent different-vendor review run against that branch diff; blocking issues loop back to the same implementer; only when gates are green AND zero blocking issues remain does the implementer open the PR. The PR is opened on an already-reviewed product. polly never merges; the human does.
---

# review-before-pr — the PR is the reviewed product, not the draft

Standing policy for ALL implementation work (applies to `fanout`,
`cross-review`, and any one-off implement task). The Pull Request is a
DELIVERABLE, not a scratchpad: it must be opened only AFTER the diff has passed
independent cross-vendor review, so whoever reads the PR sees a product that
already survived a second vendor's scrutiny. Never open a PR just to have
something to review against.

## Why
- A PR opened pre-review invites humans (and merge automation) to act on
  unreviewed code, and buries the review outcome in later force-pushes.
- Opening only the reviewed product keeps the PR's first state meaningful and
  the review history clean.
- It costs nothing: review runs against the branch diff, which exists before
  any PR does.

## Procedure (the ordering every implement task MUST follow)
1. **Implement on a branch, do NOT open a PR.** Dispatch the implementer into
   its own worktree/branch with `purpose: "implement"`, and state EXPLICITLY in
   the task packet: *drive the task to green, commit to the branch, but do NOT
   run `gh pr create` / open a PR yet — report your branch name + a summary; I
   will tell you when to open the PR.* The worker still pushes its branch to the
   remote (so the diff is fetchable) but stops short of opening the PR.
2. **Gates.** Run the repo's FULL deterministic validator set yourself against
   the branch via `sys_os_shell` — not only tests / lint / typecheck, but every
   spec / traceability / governance validator the repo defines (discover them
   from its `package.json` scripts, a `scripts/` or `tools/` dir, and its
   contributor docs — see `cross-review` Procedure step 2). The deterministic
   couplings — traceability tags, requirement / spec index, pinned-line baseline
   — must be GREEN before a PR opens, and they cost zero reviewer tokens. If any
   is red, loop back to the SAME implementer to drive green before review.
3. **Cross-review the branch diff** (see `cross-review`): collect the diff with
   `git -C .worktrees/<task_id> diff main...HEAD` (there is NO PR to
   `gh pr diff` yet), and dispatch a DIFFERENT-vendor reviewer with the diff +
   acceptance contract. EVERY review runs BOTH passes — [FOCUSED] diff-vs-contract
   AND [WIDE] blast-radius (callers, consumer/event ripple, parallel surfaces,
   test-surface coverage, whole-parcel grep for any renamed term, env-dependent
   claims). The wide pass may fast-exit "no blast radius" but is NEVER skipped —
   skipping it is what turns one fix into N reactive rounds. Reviewer reports
   issues; it never edits.
4. **Loop on blocking issues.** Each blocking issue → fix-task back to the SAME
   implementer conversation (same worktree/branch). Re-run gates, re-review.
   Repeat until gates are green AND zero blocking issues remain.
5. **Only now open the PR.** Instruct the SAME implementer (it is the only
   party that opens PRs — a reviewer's stray edits must never reach the
   deliverable) to open the PR for its already-reviewed branch. The PR body
   should note it passed cross-vendor review (reviewer vendor + verdict).
6. Record the PR URL in the registry and mark the task ready for the human to
   merge. **polly does NOT merge** — the human does.

## Rich surfaces: specify the state space in the contract, up front
The most expensive failure mode in this loop is a **combinatorially-rich surface**
(serializer, form↔payload round-trip, state machine, multi-input derivation/filter)
shipped against a contract that enumerated only a few of its states. Each unlisted
state becomes a later bot finding — code → review → PR → bot → patch, for hours,
one edge at a time. Head it off at the SPEC, not the review:
- When a task touches a rich surface, the acceptance contract you hand the implementer
  MUST name the **state-space axes** (e.g. loaded vs edited, valid/invalid/empty
  input, per-line vs aggregate flag, present/absent reference) and REQUIRE, as a
  delivered artifact:
  - a **single source of truth** for the classification (one classifier/normalizer all
    consumers derive from — no parallel re-derivation that can disagree), and
  - an **invariant / transition / round-trip matrix test** that exercises the cells of
    that state space (including a cross-consumer agreement assertion where several code
    paths must agree).
- The pre-PR review for a rich surface is ADVERSARIAL, not confirmatory — see
  `cross-review` → "Match review depth to the surface". Run it (ideally front-running
  the external bot on `codex`) BEFORE opening the PR, so the state-space class of
  finding lands in the fast internal loop.
Build the surface right in one pass; do not let the external bot enumerate the state
space for you across a dozen slow rounds.

## Functional changes: list the coupled non-code artifacts in the contract
A functional change usually obligates paired NON-CODE updates that the repo
requires to move in lockstep — and an external bot will raise a missing one as a
blocking finding. Head it off in the CONTRACT, not the review: when a task makes
a functional change, the acceptance contract you hand the implementer MUST
enumerate the coupled artifacts the diff is ALSO expected to update, so the
implementer builds them in on the first pass instead of hardening reactively.
Discover the couplings from the repo's OWN contributor / spec-governance docs
(an `AGENTS.md` / `CONTRIBUTING` / a spec or docs tree — do not assume a fixed
list), and name the actual artifacts for this change kind — e.g. docs / config
reference for a new knob, traceability tags + requirement index for
requirement-bearing code, API schema for a new response code, an ADR for an
interface decision, the pinned-line baseline for test edits that shift a line.
See `cross-review` → "Coupled-artifact sweep" for how the review splits the
mechanical half (validators in the gate) from the judgment half (does the prose
describe the new behavior).

## Mapping functions: enumerate the input taxonomy in the contract
Same discipline as the state-space one above, for a function that MAPS inputs to
decisions (classifier, parser, mapper, router, normalizer, error/exception
handler): the acceptance contract MUST enumerate the full INPUT TAXONOMY the
function is expected to accept — every shape/variant, alternate/legacy field names
for the same meaning, nested/wrapped inputs, the ordering of overlapping matches,
and the none-match fall-through — so the implementer covers the whole domain on the
first pass and the pre-PR review has the taxonomy to check against. The review can
never out-scope the contract it is handed, so a narrow contract makes this blind
spot recur round after round. See `cross-review` → "Input-domain coverage" and
"The review can never out-scope its contract".

## Notes
- This flips the older "implementer opens its own PR immediately, then we
  review" ordering. If any other skill still implies PR-first, THIS policy wins:
  review first, PR second.
- Cross-review still requires two AVAILABLE workers of DIFFERENT vendors (per
  polly's roster preflight). If independent cross-vendor review cannot run,
  do NOT open the PR on unreviewed code — say so and pull in the human at the
  plan gate.
- A pre-existing PR (opened before this policy, or by a human) is a legitimate
  exception: review its diff and loop fixes in place — you cannot un-open it.
  For all NEW work, follow the ordering above.
- "Reviewed product" means gates green + zero blocking issues. Non-blocking
  suggestions are follow-ups; they do not hold the PR closed.
