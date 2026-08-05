---
name: cross-review
description: Orchestrator playbook for checkpoint, release, and fix-push reviews - gates, an INDEPENDENT different-vendor reviewer, fix loops, bot servicing, and publication. A worker reading this must ignore it and carry out its own task. Every verdict binds to one staged tree before that tree is committed or pushed.
user-invocable: false
---

# cross-review — independent verification before the remote

**Audience: Orchestrator ONLY.** Never point a worker at this file — the verbs here (dispatching
sessions, sequencing publication, routing fixes) are yours alone.

The author never signs off on its own work. That author is an implementer
sub-agent, or Molly itself for a directly-authored doc, spec or skill. A
DIFFERENT-vendor sub-agent reviews and returns a structured report.

## Reviewer's mandate

Direct the reviewer to the implementer's worktree, provide the implementer's exact
tasking, and ask it to assess the work against that task.

Give the following mandate near-verbatim, adapting only task- and repo-specific
details:

Review ADVERSARIALLY: try to break the implementation and expose bugs, holes, and
failure modes. Review both the narrow diff and its wider blast radius—its effects
on surrounding code and the codebase's architecture. Evaluate:

### ARCHITECTURE

- Is this the right high-level approach for the intended use cases, or is it
  over-engineered for the current requirements?
- Does it follow the codebase's existing principles, conventions, and rules?
- Is the design efficient and maintainable without unnecessary complexity or
  coupling?
- Are the problem scope and abstractions at the right level, or should the
  approach step back?
- Does it support likely future waves of work rather than create a dead end?

### SIMPLICITY

- Does it do only what the task requires, in a way that is easy to understand and
  reason about?
- Can any unnecessary abstractions, indirection, or convoluted logic be removed?
- Does it reuse existing code, patterns, libraries, and frameworks rather than
  hand-roll new solutions?

### IMPLEMENTATION

- Does it fully and correctly meet the task's requirements?
- Are edge cases and failure modes handled correctly?
- Do the tests provide appropriate coverage, meaningful verification, and
  regression protection?
- Are there security, performance, or scalability concerns?
- Does it follow language, framework, and repo best practices, style,
  conventions, and favored tools without introducing known anti-patterns?
- If existing tools are insufficient, is the chosen library or framework
  well-maintained, widely used, reputable, justified, and documented rather than
  hand-rolled?
- Does the code comply with the repo's existing specs and does it document them according to the existing convention?

### BLOAT

- Does the change add unnecessary code, dependencies, features, or tests beyond
  the task, and are any additions justified by concrete benefits?
- Can redundant or repetitive code be removed without affecting behavior?
- Are increases in line count, file size, or complexity justified by their
  benefits?
- Do the tests verify meaningful behavior, or merely prove the obvious?

For every issue, determine whether it is an INDIVIDUAL error or represents a
CLASS of bug across the codebase.

## Reviewer's report

The reviewer will ALWAYS provide a report structured the same way, as specified below between delimeters.

```
### REVIEW REPORT: <task_slug> — <implementer_name> / <reviewer_vendor>

## REVIEW TARGET: phase=<checkpoint|release|fix-push> base=<base_oid> seed=<seed_tree> tree=<candidate_tree>

## VERDICT: <ONE of CHANGES REQUIRED / SUGGESTED CHANGES / NO CHANGES REQUIRED> <1 sentence summary>

## BLOCKING:

1. [INDIVIDUAL / CLASS] <file>:<line> — <1 paragraph summary of the issue>
2. [INDIVIDUAL / CLASS] <file>:<line> — <1 paragraph summary of the issue>

## NON-BLOCKING:

1. [INDIVIDUAL / CLASS] <file>:<line> — <1 paragraph summary of the issue>
2. [INDIVIDUAL / CLASS] <file>:<line> — <1 paragraph summary of the issue>

## ARCHITECTURE:

<comment/verdict/suggestion on the architecture of the implementation, including high-level design, alignment with codebase principles, efficiency, maintainability, and support for future work.>

## SIMPLICITY:

<comment/verdict/suggestion on the simplicity of the implementation, including whether it does only what is needed, ease of understanding, and avoidance of unnecessary abstractions or complexity.>

## BLOAT:

<comment/verdict/suggestion on the bloat of the implementation, including unnecessary code, dependencies, features, or tests that do not add value to the codebase, noting the linecount of the implementation.>

## VERIFICATION:

<summary of tests and verification steps taken by the reviewer>
```

EVERY review round reports in the SAME, structured format, and YOU, the orchestrator, 
check the report for compliance with that format before accepting it. If the reviewer
does not provide the required format, tell it to re-write the report in the required format.

## Every review, every round: the identical full mandate

One dispatch runs the WHOLE mandate, ALWAYS. A re-review re-sends the
**identical** block — NEVER a narrowed "just confirm these N are closed".


## Procedure

1. **Freeze and identify the staged candidate.** Every round reviews an index
   tree, never a preliminary commit or informal working-tree diff. The fixer
   confirms its newly introduced changes belong to the task while preserving
   the accepted staged seed, runs `git add -A`, verifies `git diff --quiet`
   succeeds and `git ls-files --others --exclude-standard` is empty, then stops.
   Do not stage on the fixer's behalf. Record worktree mode, `review_phase`
   (`checkpoint`, `release`, or `fix-push`), exact base commit as `base_oid`,
   accepted input tree as `seed_tree`, current `HEAD` as `head_oid`, and `git
   write-tree` as `candidate_tree`. The target is (`review_phase`, `base_oid`,
   `seed_tree`, `candidate_tree`). Do not hard-code `main`.

   *Unopened branch* (the normal case): require `head_oid == base_oid`; any
   commit violates the review-before-commit boundary. Review both the task delta
   with `git diff --find-renames <seed_tree> <candidate_tree>` and the complete
   cumulative candidate with `git diff --cached --find-renames <base_oid>`.

   *Already-open PR* (a pre-existing PR or review-bot fix): require `head_oid`
   to equal the recorded published PR head. Review the complete next candidate,
   including the existing PR plus staged fixes, with the same narrow and
   cumulative comparisons. `pull_request_read` shows only published state, so
   use it for threads and their resolution state, not as the candidate diff. A
   clean fix round creates an additional commit and plain push; force pushes and
   amendments remain forbidden.

   *Direct authoring* (a doc or skill Molly wrote itself): require a clean
   recorded baseline before authoring; if the checkout was already dirty, use a
   dedicated clean worktree. Apply the same phase/base/seed/tree protocol. Molly
   is the fixer, but direct prose receives no weaker review boundary.

2. **Dispatch a reviewer whose vendor is different to the implementers.** Where
   both vendors authored the feature, or perfect independence is otherwise
   impossible, choose the most independent available reviewer, breaking a tie
   at random. For prose Molly authored directly, the author vendor is Molly's
   model family (Claude). Give the reviewer the worktree mode and absolute path,
   exact task and acceptance contract, `review_phase`, `base_oid`, `seed_tree`,
   `candidate_tree`, and the complete mandate and report format. The reviewer
   first requires `git write-tree == candidate_tree`; a mismatch is an
   `INCOMPLETE DISPATCH` with no verdict. Emit the dispatch in the same turn you
   decide to review, then end the turn and collect it with `sys_read_inbox`.

3. **Validate the report before acting.** Require the exact structured format
   and an exact phase/base/seed/candidate match. Otherwise tell the reviewer to
   rewrite it; a verdict on another target is no verdict.

4. **Route blocking issues to the SAME fixer, on the SAME branch.** Re-send to
   the same implementer conversation so it retains task context. Because no
   runtime worktree binding exists, repeat the mode, phase, ABSOLUTE worktree,
   base, and current accepted seed on every fix dispatch. Take fresh task and
   runner-root baselines using the same evidence as `fanout`, including hashes
   for already-dirty root paths. On return, require task HEAD and every root
   baseline to remain unchanged. The fixer reruns gates, stages the complete
   amended candidate, reports its new tree, and stops without committing. Open
   every fix dispatch with the no-delegation role boundary. For direct prose,
   Molly is the fixer and reruns the same gates and review loop. Log each
   blocking issue as a registry fix task, then return to step 1.

5. **Class closure, and the recurrence STOP gate.** Before scoping any fix to
   its flagged site, ask whether it is a one-off or a class. A typo, wrong
   constant or copy-paste slip is a one-off. A fix whose shape generalizes —
   "X not re-checked on retry", "value not normalized before compare",
   "resolution failure treated as absence" — is a CLASS, and both the fix and
   the review must cover EVERY instance in the repo. When that class can recur by
   ADDITION — a new call site, a new cache, a new consumer reintroducing it —
   closure ALSO owes a guard that fails on the next instance: a repo-level test
   or lint rule over the invariant, not only fixes to today's sites. That is
   owed the FIRST time you call something a class, not after it recurs. The FIRST time any later
   round surfaces the SAME class at a NEW site, STOP point-fixing: in that same
   round escalate the fix to whole-surface closure and the review to a
   whole-repo same-class audit, with "zero remaining" as the bar. Demand the
   enumeration table BEFORE the fixes — it is the deliverable that proves the
   class was closed rather than sampled.

6. **Any target change invalidates the verdict.** A verdict covers exactly the
   dispatched (`review_phase`, `base_oid`, `seed_tree`, `candidate_tree`). Before
   acceptance and again before its transition, require the same phase, base and
   current HEAD, the same `git write-tree`, no unstaged changes, and no
   nonignored untracked files. A fix, rebase, index change, seed change, phase
   change, or "tiny" follow-up creates a new target: rerun gates and the
   identical full review. Never transfer a verdict between phases or tree OIDs.

7. **Apply the phase transition.** Green gates and zero blocking issues permit
   only the transition named by `review_phase`:

   - `checkpoint`: record the accepted candidate and return it to
     `worktree-routing` or `fanout`. Do not commit, push, or open a PR.
   - `release`: tell the SAME implementer to commit the reviewed staged tree,
     push, and open the PR. Before the first push, verify `HEAD^{tree} ==
     candidate_tree`, `HEAD^ == base_oid`, `git rev-list --count
     <base_oid>..HEAD` is exactly one, and the worktree is clean.
   - `fix-push`: verify the new commit tree equals `candidate_tree`, its parent
     is the recorded PR head, and the worktree is clean, then use a plain push.
     This is an additional reviewed commit, never an amendment or force push.

   For directly-authored release work, Molly commits the reviewed staged tree
   and performs the same verification before publishing. After every push,
   verify the remote branch equals local HEAD. Record the PR URL, then service
   the review bot below. Mark it ready only once that loop establishes a clean
   bot verdict for the current HEAD (row 5 below). Absence of findings is not a
   verdict: silence, a burnt cap, a bot that never engaged, and the catch-all
   row do not clear the PR. **Molly does NOT merge.**

## Servicing an external review bot

Some repos have a review bot that runs on the PR after it is opened. It may
report findings, or it may report a clean bill. It is not a human, and it
does not merge. It is a third-party service, and it may be wrong. It is
independent of the implementer and the reviewer, and it is not a sub-agent.

After opening a PR — and after every push of fixes to it — service the bot
before handing back. Record your HEAD sha and push time, and identify the bot's
account: a signal counts only if the BOT produced it and it is newer than that
push. A finding from an EARLIER round that you declined to fix does not expire
with its round: it stays outstanding until the bot withdraws it or the human
rules on it. For a signal that can be
edited, use the LATER of its created and updated times — the bot may revise a
comment rather than post a new one. Reactions are idempotent per account and
content — an existing one is not recreated on a later trigger and keeps its
original timestamp — so a reaction can settle the first round and never a
re-review.

The bot posts on its own wall-clock lag, so sweep on a re-armed single-shot
timer (~2 min); that is a genuine scheduled delay, not sub-agent polling. Never
leave a `repeat=true` timer running. Cap the sweeps, and the cap is over the
whole servicing of this PR: every re-arm, every restart after a head change,
and every round of fixes draw on one budget. Nothing resets it.

Each sweep, read the PR's current head sha, the bot's reactions, root comments,
reviews and inline review comments, and the check runs, then take the FIRST row
below that matches. Every row classifies only on what that read collects. The
ORDER is part of the rule: these are overlapping predicates, not a narrowing
hierarchy, so a broad row placed early would swallow the case beneath it.

1. the PR head is no longer your recorded sha -> unreviewed code is on the PR.
   Stop the bot loop, fetch that exact commit, record its tree as the new
   candidate, and run the gates and full independent review against `base_oid`
   before resuming. This is recovery from an external or human head change, not
   evidence that the pre-commit boundary was followed. Otherwise a verdict below
   judges other code, or accepts a bot verdict on code no different vendor ever
   reviewed.
2. a check run on your recorded HEAD concluded anything other than `success`,
   `skipped` or `neutral` -> a finding, even while other checks are still
   pending. Those three are an ALLOWLIST of clean conclusions, so `failure`,
   `timed_out`, `cancelled`, `action_required`, `stale` and any conclusion you
   do not recognise are all findings rather than passes. A check that concluded
   on an earlier sha is not a verdict on this one.
3. bot findings newer than your push -> service them below.
4. CI on your recorded HEAD is not finished -> engaged; re-arm and loop. This
   is a COMPLETENESS test, not a list of pending statuses: it matches when ANY
   check run on that sha is not `completed`, and it also matches when that sha
   has NO check runs at all — UNLESS you hold a record, from the human, that
   this repo runs no checks on PRs. `queued`, `in_progress`, `waiting`,
   `requested`, `pending` and any status you do not recognise all count as
   unfinished, and an empty read is NOT evidence of completion — GitHub
   routinely has not created checks yet in the moments after a push, and a repo
   reporting via the legacy commit-status API has none for you to read. So if
   an empty read looks permanent, ask the human and record the answer on the
   task; until that record exists an empty read keeps matching here, and
   without it this row would match forever and every PR would end at the cap.
   Never infer the record from an empty read, and a check run appearing
   ANYWHERE on this PR RETIRES the record: CI exists after all, so go back to
   reading it. Otherwise a repo that gains CI after the record was taken, or
   one whose checks have landed on an earlier sha but not yet on the recorded
   HEAD, would be exempted here and handed off below on an empty read — the
   state this row exists to catch. This row deliberately outranks
   the clean verdict below, which cannot speak for a PR whose CI has not
   finished.
5. every check run on your recorded HEAD is `completed` with a clean
   conclusion — `success`, `skipped` or `neutral`, the allowlist from row 2 —
   or you hold row 4's record that this repo runs no checks, which satisfies
   this premise with none to read; AND no finding from an earlier round is
   still outstanding, AND
   there is a clean bot verdict — what counts as one differs by round: on the
   FIRST, a bot `+1` newer than your push OR a bot comment or review stating it
   found nothing, because a clean first round may post either one alone and you
   must not wait for a particular shape. After a FIX PUSH, only a bot comment,
   review or inline comment saying so — its `+1` is already sitting there and
   cannot say anything about this round. All three together -> CLEAN: the loop
   ends here and step 7 may mark the PR ready.
6. a bot `eyes` newer than your push, on the FIRST round only -> engaged;
   re-arm and loop. A reaction keeps its original timestamp, so an `eyes` from
   an earlier round says nothing about this one and falls through to row 7.
7. anything else -> re-arm and loop.

Reaching the cap ends the loop from any row, and it is not a verdict:
STOP and tell the human exactly what you could and could not establish.
Silence is not approval, a `+1` left from an earlier round is not a verdict on
this one, and a bot that reacted and then went quiet has reviewed nothing.

A check whose conclusion is `action_required` is a human gate — a CLA, a
deployment approval — not a code defect. It is still a finding, so the loop does
not hand off past it, but do not spend review rounds on it: name it to the human
and let them clear it.

Servicing findings. Cluster its findings by BUG CLASS before fixing anything,
and feed them in as additional FOCUSED inputs to a re-run of the identical
complete mandate — never a "confirm these are fixed" scope. The fixer reruns
the gates, stages the complete amended candidate and stops; bind the full
pre-push review to its new tree with `review_phase=fix-push` and the published
HEAD tree as `seed_tree`. After a clean report, create an additional
commit and use a plain push — never amend or force-push. Reply in-thread rather
than as a new top-level comment. A repeated class is a hard stop for
point-fixing: escalate to whole-surface closure, which owes the same
future-addition guard step 5 requires. After pushing fixes, comment on the PR root to
re-request review, naming any finding you did NOT fix and why — then loop. A
fix pushed without a re-request leaves the bot waiting. When handing status to
the human, report the count of UNRESOLVED review threads alongside your summary, because replies do not
establish resolution — "findings serviced" is not "threads resolved", and the
human is merging on that distinction. Molly's hand-off wording must not imply
completeness ('findings serviced' ≠ 'threads resolved'). **Molly never declares
a clean bill and never merges.**

## Notes

- Non-blocking issues and suggestions from the INDEPENDENT REVIEWER become
  registry follow-ups; they do not block the PR. A REVIEW BOT's findings are
  not covered by this: whatever severity you assign one, it stays outstanding
  until the bot withdraws it or the human rules on it, and row 5 of the
  servicing loop will not clear the PR while it stands. Take that ruling to the
  human when you decline a bot finding rather than waiting for the cap.
