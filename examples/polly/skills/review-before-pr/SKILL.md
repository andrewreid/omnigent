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
2. **Gates.** Run the deterministic gates (tests / lint / typecheck) yourself
   against the branch via `sys_os_shell`. If red, loop back to the SAME
   implementer to drive green before review.
3. **Cross-review the branch diff** (see `cross-review`): collect the diff with
   `git -C .worktrees/<task_id> diff main...HEAD` (there is NO PR to
   `gh pr diff` yet), and dispatch a DIFFERENT-vendor reviewer with the diff +
   acceptance contract. Reviewer reports issues; it never edits.
4. **Loop on blocking issues.** Each blocking issue → fix-task back to the SAME
   implementer conversation (same worktree/branch). Re-run gates, re-review.
   Repeat until gates are green AND zero blocking issues remain.
5. **Only now open the PR.** Instruct the SAME implementer (it is the only
   party that opens PRs — a reviewer's stray edits must never reach the
   deliverable) to open the PR for its already-reviewed branch. The PR body
   should note it passed cross-vendor review (reviewer vendor + verdict).
6. Record the PR URL in the registry and mark the task ready for the human to
   merge. **polly does NOT merge** — the human does.

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
