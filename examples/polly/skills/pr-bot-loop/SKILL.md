---
name: pr-bot-loop
description: Drive the GitHub Codex-review-bot feedback loop on a PR you pushed — arm a timer for the delayed bot review, reply in-thread to each finding (fix or pushback with rationale), re-request review, and after 3 straight rounds of small bugs escalate to a claude-vs-codex architectural debate. Loop until a clean bill, then disarm timers.
---

# pr-bot-loop — close the external review-bot loop on a PR

After an implementer opens/updates a PR, a review bot (`chatgpt-codex-connector`,
the "Codex" GitHub app) posts automated review comments ~10 minutes later. This
is the EXTERNAL review layer, separate from the internal `cross-review` skill (a
different-vendor sub-agent judging the diff before push). Both run: internal
cross-review gates the push; this loop services the bot on the live PR so the
human can resolve the threads. polly runs this loop; polly never merges.

## Procedure
1. **Arm a timer after every push.** The bot lags ~10 min. Immediately after a
   push or a `@codex review` request, `sys_timer_set(seconds≈600-660,
   note="sweep codex-bot on PR #<n> ...")`. Do NOT busy-poll; end the turn and
   let the timer (or an inbox wake) revive you. One timer per PR under review.
2. **Sweep on fire.** FIRST check the PR is still yours to loop: a human may
   have merged / closed / rebased it AT ANY TIME and will NOT tell you. Query
   its state + head SHA (`gh pr view <n> --json state,mergedAt,headRefOid`). If
   MERGED/closed, STOP this loop and jump to "When a PR merges under you" below.
   Otherwise fetch both review surfaces:
   - review threads (inline): `gh api repos/<owner>/<repo>/pulls/<n>/comments`
     (fields: `path`, `line`, `id`, `body`, `user.login`).
   - review summaries + head sha: `gh pr view <n> --json reviews,headRefOid`.
   Note the **commit the bot reviewed** (`Reviewed commit: <sha>` in the body):
   it is often a PRE-fix commit, so some findings may already be resolved by a
   later commit — reconcile against the current head before acting.
   Also read the bot's **reaction emoji** FIRST (see "The bot's reaction emoji"
   below): it is the quickest state signal and gates whether a nudge is even
   allowed — do not skip it and jump to re-requesting review.
3. **Disposition EACH finding** (badges: P1 > P2 > P3). Exactly one of:
   - **fix** — a real defect. Bundle it into a cohesive fix task (see step 5).
   - **pushback** — wrong or out-of-context. Reply with concrete rationale
     (e.g. "greenfield app, never shipped → migration code is dead"). Pushing
     back with evidence is expected, not rude.
   - **already-fixed** — resolved by a later commit than the one reviewed. Reply
     naming the sha and what closed it.
4. **Reply IN-THREAD, never a new top-level comment.** Use
   `add_reply_to_pull_request_comment(owner, repo, pullNumber, commentId=<id>,
   body=...)` — `commentId` is the numeric review-comment id from step 2, and
   the reply threads under the bot's comment so the human can resolve it in
   place. A fresh top-level comment orphans the discussion. Reply to every
   finding, including pushbacks and already-fixed.
5. **Push fixes as ONE cohesive pass, not per-bug.** Cluster the fix-worthy
   findings by theme and send them to the SAME implementer conversation
   (reuse its `agent`+`title`, `purpose:"implement"`) so it keeps its
   worktree/branch/PR. Re-run the gates yourself (tsc/lint/build) after it
   pushes. Then re-request review with a NEW top-level comment containing
   exactly `@codex review` (`gh pr comment <n> --body "@codex review"`), and
   re-arm the timer (step 1). Loop.
   - Before ANY nudge, read the bot's reaction (see "The bot's reaction emoji"
     below). Re-request `@codex review` ONLY when there is NO reaction AND no
     review of the current head after the lag window. NEVER re-request while a
     👀 (`eyes`) reaction is present, or before a review of the current
     head SHA has had its ~10 min — a second `@codex review` on an in-progress
     review spawns a duplicate and confuses the run. When unsure, re-arm the
     timer and wait; do not nudge.
6. **Clean bill → stop.** A 👍 (`+1`) reaction on the CURRENT head SHA is the
   bot's explicit "no issues" verdict (a positive outcome, not silence) — the
   fast clean-bill signal; the "Codex Review: Didn't find any major issues"
   comment says the same thing more slowly. A PR is merge-clean only when BOTH
   hold: (a) the latest review/reaction on the CURRENT head shows no new P1/P2,
   AND (b) there are ZERO UNRESOLVED bot
   review threads (verify via GraphQL, not REST — replying to a thread does
   NOT resolve it, and `isOutdated` does NOT mean resolved). RESOLVE every
   handled thread (fixed / accepted-pushback / superseded-outdated) so the
   human sees a clean PR; only leave a thread open if it is a real unaddressed
   finding you are still working. Then mark ready in the registry and **disarm
   any timers** (`sys_timer_cancel`). Leave the merge to the human.

## The bot's reaction emoji = its state signal (read it BEFORE nudging)
The Codex bot signals review state by REACTING to the PR, faster and more
reliably than any comment it posts. Read the reaction FIRST on every sweep — it
is what distinguishes "not picked up" from "reviewing" from "done, clean", and
is the single best guard against a redundant `@codex review`:
- **no reaction** — not picked up yet. Only here MAY a single nudge apply, and
  only after the ~10 min lag.
- **👀 `eyes`** — acknowledged, review IN PROGRESS. Do NOT nudge, do NOT
  re-request. Re-arm a normal timer and wait; the verdict is coming.
- **👍 `+1`** — reviewed, NO issues = clean bill (a positive verdict).
  Confirm zero unresolved prior threads, then disarm.

Read it (reactions sit on the PR issue itself, and separately on the `@codex
review` comment):
```
gh api repos/<owner>/<repo>/issues/<n>/reactions --jq '.[] | {content, user:.user.login}'
```
A reaction is bound to the HEAD SHA the bot reviewed. After you push a fix the
prior 👍 is STALE — expect a fresh 👀→👍 cycle on the new commit; never read an
old thumbs-up as covering a newer head. If the reaction and a review comment
disagree, trust whichever is tied to the current head SHA. This reaction-state
check is why most sweeps need NO nudge at all — an 👀 means "keep waiting",
not "poke again".

## Finding & resolving threads (GraphQL, not REST)
REST (`gh api repos/<o>/<r>/pulls/<n>/comments`) is fine for reading a
finding's body + posting an in-thread reply, but it cannot tell you a thread's
resolution state and will let a PR look "handled" while 30 threads sit
UNRESOLVED. Use GraphQL to scan and to resolve.

Scan one PR's bot threads (state + whether YOU replied):
```
gh api graphql -f query='
query($n:Int!){ repository(owner:"<owner>",name:"<repo>"){ pullRequest(number:$n){
  reviewThreads(first:100){ nodes{ id isResolved isOutdated
    comments(first:20){ nodes{ author{login} bodyText } } } } } } }' -F n=<PR> \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[]
        | select(.comments.nodes[0].author.login=="chatgpt-codex-connector"
                 and .isResolved==false)
        | { outdated:.isOutdated, id:.id,
            title:(.comments.nodes[0].bodyText|gsub("\n";" ")|.[0:70]),
            replied:([.comments.nodes[]|select(.author.login=="<your-login>")]|length) }'
```
Triage each UNRESOLVED thread:
- `replied==0 && isOutdated==false` — the DANGER set: a finding you never
  answered and whose code is still live. Verify it against current code; it may
  be a genuine open bug, not just an un-clicked button. Fix or reply+resolve.
- `replied>=1` (fixed / accepted-pushback) or `isOutdated==true` (superseded by
  a later commit) — handled; resolve the thread.

Resolve a handled thread (the `id` above is the thread node id `PRRT_...`):
```
pull_request_review_write(method:"resolve_thread", threadId:"<PRRT_...>")
```
(or GraphQL `resolveReviewThread(input:{threadId})`). Resolve only what is truly
handled; never mass-resolve to silence the bot. `unresolve_thread` reopens one.

## Deciding structural vs small (classify first, then escalate)
Before fixing a batch of findings, CLASSIFY and hunt for a shared root — the
decision drives the whole approach; don't jump straight to patching.

1. **Classify each finding** (verify against CURRENT code; delegate a read-only
   explore when there are several, and when the code spans stacked branches have
   it check each branch):
   - REAL-AND-OPEN — defect present on this branch, not fixed elsewhere -> fix.
   - SUPERSEDED — already fixed by a later commit, or in a stacked-PR chain by a
     DOWNSTREAM branch -> don't re-fix; resolve the thread. (A finding can be
     real on an early PR yet already corrected by a PR stacked on top.)
   - INVALID / already-handled -> reply (pushback) + resolve.
2. **Root-cluster analysis.** Group the REAL-AND-OPEN findings by root cause /
   subsystem. STRUCTURAL signals (treat as ONE design problem, not N bugs):
   - several findings share one root / one state machine / one subsystem — even
     in a SINGLE review round (cluster trigger), OR
   - findings recur across commits, or each patch spawns an adjacent one
     (whack-a-mole), OR
   - blocking-per-round is BRANCHING / escalating rather than CONVERGING toward
     zero, OR
   - the honest fix would touch the same code at N points.
   SMALL signals: isolated, single-file, no shared root, config / one-liner.
   A real batch is usually MIXED — one structural cluster + a few isolated
   smalls + some superseded/pushback. Don't force one bucket.
3. **The 3-strikes TEMPORAL trigger is ONE structural signal, not the only one.**
   If 3 bot rounds in a row each surface fresh small bugs in the same area, stop
   patching and treat it as structural — but the cluster/branching/recurrence
   signals above can declare "structural" from a SINGLE batch, before round 3.

### Escalation recipe (when structural)
1. **Design first, on a strong model.** Dispatch a DESIGN-ONLY (no code) agent at
   the strongest reasoning tier for the hardest cluster (e.g. `claude_code` on
   the top model / high effort), and CROSS-CHECK the design with a
   DIFFERENT-vendor agent (e.g. `codex` on its top model) — either a debate
   (both propose + argue tradeoffs) or one designs + the other stress-tests.
   Model tier and vendor split are per-dispatch choices; pick the strongest for a
   security/lifecycle-critical design. The human may name the models to use.
2. **Plan-gate.** A design spanning several files is a plan-gate item — get the
   human's nod before coding. Fold the isolated small fixes into the same plan.
3. **Implement once, consolidated.** Land the root fix as ONE cohesive change on
   the AUTHORITATIVE branch (in a stack, the branch that owns the subsystem —
   usually the top), cross-review it, then run the bot loop to a clean bill.
4. **Reset the round counter** after a genuine structural pass — its follow-up
   findings are refinements of the new design, not a fresh strike. Watch that
   they CONVERGE; if they branch again the design itself is wrong -> back to
   step 1, don't keep patching.

## When a PR merges under you (don't trust the human to say so)
A tracked PR can be merged/closed by the human mid-loop — silently. Never
assume you'll be told. On EVERY sweep, check each PR you're looping for
`state==MERGED`/closed and whether its `headRefOid` still equals the SHA you
last pushed. When one merged:
- **Orphan check (critical).** Compare the MERGED SHA against your latest
  pushed/reviewed SHA. A PR head-pointer LAGS a push, so a human who merges
  right after a "clean bill" can merge an OLDER commit and silently drop your
  later commits. If the merge (esp. a SQUASH) did not include your latest work,
  those commits are orphaned on the branch — rescue them into a NEW PR based on
  wherever the merge landed (cherry-pick onto the merged base).
- **Stop the moot loop.** A merged PR's bot loop / timers / thread-resolution
  are dead — cancel its timer, don't re-ping `@codex review`.
- **Before merging anything yourself-recommend:** verify the PR's head SHA ==
  the SHA that got the clean bill; a lagging head is how work gets orphaned.
- Stacked PRs: merge bottom-up and delete each head branch after merge so
  GitHub auto-retargets the child — but the merge can happen out of order, so
  the lifecycle/orphan check above is what actually protects you, not the order.
- **SQUASH-merged parent → REBASE the child, do NOT just retarget.** When a
  parent PR is SQUASH-merged, its branch is deleted and its commits enter the
  base as ONE new squash commit — the child's COPIES of those commits are NOT in
  the base. Auto-retarget (or a manual base change) to the new base then makes
  the child's diff DOUBLE-show the parent's changes, and the child's old base
  ref may be gone (a pending PR-open/retarget fails with "Base ref must be a
  branch"). Fix: rebase the child `--onto <new-base> <old-parent-tip>` (replay
  ONLY the child's own commits), or fresh-branch off the new base + cherry-pick
  just the child's commits. Force-push is often blocked, so a fresh
  non-destructive branch is usually cleanest. VERIFY `git diff <new-base>...HEAD
  --stat` shows ONLY the child's own files before repointing the PR base.

## Notes
- The bot reviews a specific commit; a comment can target a line/commit that a
  later fix already changed. Always reconcile against current head — reply
  "fixed in <sha>" rather than re-fixing.
- This loop composes with `cross-review` (internal, pre-push, different-vendor
  sub-agent) and `fanout` (each parallel PR runs its own bot loop). Internal
  cross-review and the bot frequently overlap; when both are in flight on the
  same commit, merge their findings into ONE fix pass to avoid double churn.
- Waiting is the framework's job: supervise internal sub-agents via the inbox,
  the external bot via `gh` + a timer. Never `sys_timer_set` a self-ping to poll
  a sub-agent that already auto-wakes you on completion — timers are only for
  the bot's wall-clock lag.
- Recurring races/edge-cases in the SAME state machine across rounds are the
  classic 3-strikes trigger — prefer a structural fix (e.g. serialize mutations
  behind one lock) over patching each window.
