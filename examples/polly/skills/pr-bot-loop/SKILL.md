---
name: pr-bot-loop
description: Drive the GitHub Codex-review-bot feedback loop on a PR you pushed — arm a timer for the delayed bot review, reply in-thread to each finding (fix or pushback with rationale), re-request review, and after 3 straight rounds of small bugs escalate to a claude-vs-codex architectural debate. Loop until a clean bill, then disarm timers.
---

# pr-bot-loop — close the external review-bot loop on a PR

After an implementer opens/updates a PR, a review bot (`chatgpt-codex-connector`, the "Codex" GitHub app) posts automated review comments ~10 min later. This is the EXTERNAL layer, separate from the internal `cross-review` skill (a different-vendor sub-agent judging the diff pre-push): cross-review gates the push, this loop services the bot on the live PR so the human can resolve threads. polly runs the loop; polly never merges.

## Procedure
1. **Arm a timer after every push.** The bot lags ~10 min. After a push or `@codex review`, `sys_timer_set(seconds≈600-660, note="sweep codex-bot on PR #<n> ...")`. Don't busy-poll — end the turn; the timer (or inbox) revives you. One timer per PR.
2. **Sweep on fire.** FIRST confirm the PR is still yours: a human may have merged/closed/rebased it silently — `gh pr view <n> --json state,mergedAt,headRefOid`. If MERGED/closed, STOP and jump to "When a PR merges under you". Else read the bot's **reaction emoji first** (section below) — the quickest state signal, and it gates whether a nudge is allowed. Then fetch findings:
   - threads (inline): `gh api repos/<owner>/<repo>/pulls/<n>/comments` (`path`, `line`, `id`, `body`, `user.login`).
   - summaries + head sha: `gh pr view <n> --json reviews,headRefOid`.
   The bot names the **commit it reviewed** (`Reviewed commit: <sha>`) — often PRE-fix, so reconcile findings against current head before acting.
3. **Disposition EACH finding** (P1 > P2 > P3), exactly one of:
   - **fix** — real defect; bundle into a cohesive fix task (step 5).
   - **pushback** — wrong/out-of-context; reply with concrete rationale (e.g. "greenfield app, never shipped → migration code is dead"). Evidence-backed pushback is expected, not rude.
   - **already-fixed** — closed by a later commit; reply naming the sha and what closed it.
4. **Reply IN-THREAD, never top-level.** `add_reply_to_pull_request_comment(owner, repo, pullNumber, commentId=<id>, body=...)` — `commentId` is the numeric review-comment id from step 2; the reply threads under the bot's comment so the human resolves in place. A top-level comment orphans it. Reply to every finding, pushbacks and already-fixed included.
5. **Push fixes as ONE cohesive pass, not per-bug.** Cluster fix-worthy findings by theme; send to the SAME implementer conversation (reuse `agent`+`title`, `purpose:"implement"`) so it keeps its worktree/branch/PR. Re-run gates yourself (tsc/lint/build) after it pushes, then re-request review — a NEW top-level comment of exactly `@codex review` (`gh pr comment <n> --body "@codex review"`) — and re-arm the timer. Loop.
   - Nudge discipline: re-request ONLY in the true not-picked-up state — no body reaction AND no inline findings AND the ~10 min lag elapsed (see "The bot's reaction emoji" for disambiguating a CLEARED reaction, which means issues were posted, from a never-engaged one). NEVER re-request while a 👀 (`eyes`) is present, when inline findings already exist for the current head, or before the current head's ~10 min elapses — a second `@codex review` on an in-progress review duplicates/confuses it. Unsure → re-arm and wait.
6. **Clean bill → stop.** Merge-clean requires BOTH: (a) the latest review/reaction on the CURRENT head shows no new P1/P2 (a 👍 `+1` on the current head is the bot's explicit "no issues" verdict — see below), AND (b) ZERO UNRESOLVED bot threads (verify via GraphQL, not REST — a reply does NOT resolve, and `isOutdated` ≠ resolved). RESOLVE every handled thread (fixed / accepted-pushback / superseded) so the human sees a clean PR; leave open only a real finding you're still working. Mark ready in the registry, **disarm timers** (`sys_timer_cancel`), leave the merge to the human.

## The bot's reaction emoji = its state signal (read BEFORE nudging)
The bot's PRIMARY signal is the reaction on the MAIN PR body — faster and more reliable than its comments. Read it first every sweep; the reaction MOVES between states (it is NOT monotonic):
- **👀 `eyes` on the PR body** — picked up, review IN PROGRESS: the initial state on a fresh PR, and again after every `@codex review`. Do NOT nudge; re-arm a normal timer and wait.
- **👍 `+1` on the PR body** — reviewed the CURRENT head, NO issues = clean bill. On the FIRST clean pass this comes with NO comment (don't wait for one); on a re-review clean pass it ALSO posts an inline "no major issues" comment. Confirm zero unresolved prior threads, then disarm.
- **NO reaction on the PR body — AMBIGUOUS; disambiguate by inline findings.** When the bot reviews and FINDS issues it posts inline review comments and CLEARS the body reaction — so a bare "no reaction" means one of two OPPOSITE things:
  - inline review comments exist for the current head → **reviewed, issues found** → service the findings (this is NOT a nudge situation).
  - no inline findings and the bot has never engaged (brand-new PR) → **not picked up yet** → the ONLY nudge-eligible state, and only after the ~10 min lag.

A `@codex review` ping puts 👀 on THAT comment AND flips the PR-body reaction back to 👀 — so after a nudge, watch the BODY reaction cycle 👀 → (👍 clean | cleared-with-inline-findings). Read both surfaces:
```
gh api repos/<owner>/<repo>/issues/<n>/reactions --jq '.[] | {content, user:.user.login}'   # PR body
gh api repos/<owner>/<repo>/issues/comments/<comment-id>/reactions                           # a specific @codex review comment
```
Every reaction binds to the head SHA the bot reviewed: after you push a fix the prior 👍 is STALE — expect a fresh 👀 → (👍 | cleared) on the new commit; never read an old thumbs-up as covering a newer head. Bottom line: NEVER nudge on a bare "no reaction" without first checking for inline findings — a cleared reaction almost always means issues are already posted, not that the bot is idle.

## Finding & resolving threads (GraphQL, not REST)
REST (`gh api repos/<o>/<r>/pulls/<n>/comments`) reads a finding's body and posts a reply, but can't report resolution state — a PR looks "handled" while 30 threads sit UNRESOLVED. Use GraphQL to scan and resolve.

Scan bot threads (state + whether YOU replied):
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
- `replied==0 && isOutdated==false` — the DANGER set: never answered, code still live. Verify against current code; may be a genuine open bug, not just an un-clicked button. Fix or reply+resolve.
- `replied>=1` (fixed / accepted-pushback) or `isOutdated==true` (superseded) — handled; resolve it.

Resolve a handled thread (`id` = thread node id `PRRT_...`):
```
pull_request_review_write(method:"resolve_thread", threadId:"<PRRT_...>")
```
(or GraphQL `resolveReviewThread(input:{threadId})`). Resolve only what's truly handled — never mass-resolve to silence the bot. `unresolve_thread` reopens one.

## Deciding structural vs small (classify first, then escalate)
Before fixing a batch, CLASSIFY and hunt a shared root — the decision drives the approach; don't jump to patching.

1. **Classify each finding** against CURRENT code (delegate a read-only explore when there are several; across stacked branches, check each):
   - REAL-AND-OPEN — present on this branch, not fixed elsewhere → fix.
   - SUPERSEDED — already fixed by a later commit, or by a DOWNSTREAM branch in a stack → don't re-fix; resolve the thread.
   - INVALID / already-handled → reply (pushback) + resolve.
2. **Root-cluster analysis.** Group REAL-AND-OPEN by root/subsystem. STRUCTURAL signals (ONE design problem, not N bugs): several findings share one root / state machine / subsystem — even in a SINGLE round (cluster trigger); OR findings recur across commits / each patch spawns an adjacent one (whack-a-mole); OR blocking-per-round is BRANCHING not CONVERGING to zero; OR the honest fix touches the same code at N points. SMALL signals: isolated, single-file, no shared root, config/one-liner. A real batch is usually MIXED (one cluster + a few smalls + some superseded/pushback) — don't force one bucket.
3. **3-strikes is ONE structural signal, not the only one.** 3 rounds each surfacing fresh small bugs in the same area → treat as structural — but the cluster/branching/recurrence signals can declare "structural" from a SINGLE batch, before round 3.

### Escalation recipe (when structural)
1. **Design first, strong model.** Dispatch a DESIGN-ONLY (no code) agent at the strongest tier for the hardest cluster (e.g. `claude_code`, top model/high effort), and CROSS-CHECK with a DIFFERENT vendor (e.g. `codex`, top model) — a debate (both argue tradeoffs) or one designs + one stress-tests. Tier/vendor are per-dispatch; pick strongest for security/lifecycle-critical design. The human may name models.
2. **Plan-gate.** A design spanning several files is a plan-gate item — get the human's nod before coding. Fold the isolated smalls into the same plan.
3. **Implement once, consolidated.** Land the root fix as ONE change on the AUTHORITATIVE branch (in a stack, the one owning the subsystem — usually the top), cross-review it, then loop the bot to clean.
4. **Reset the round counter** after a genuine structural pass — its follow-ups are refinements, not fresh strikes. Watch they CONVERGE; if they branch again the design is wrong → back to step 1, don't keep patching.

## When a PR merges under you (don't trust the human to say so)
A tracked PR can be merged/closed mid-loop, silently. On EVERY sweep check `state==MERGED`/closed and whether `headRefOid` still equals your last-pushed SHA. When one merged:
- **Orphan check (critical).** Compare the MERGED SHA vs your latest pushed/reviewed SHA. The head-pointer LAGS a push, so a human merging right after a "clean bill" can merge an OLDER commit and drop your later work. If the merge (esp. a SQUASH) missed your latest, those commits are orphaned — rescue into a NEW PR based where the merge landed (cherry-pick onto the merged base).
- **Stop the moot loop.** A merged PR's loop/timers/threads are dead — cancel its timer, don't re-ping `@codex review`.
- **Before recommending a merge:** verify head SHA == the SHA that got the clean bill; a lagging head is how work orphans.
- **Stacked PRs:** merge bottom-up, delete each head branch so GitHub auto-retargets the child — but merges can happen out of order, so the orphan check above is the real protection, not order.
- **SQUASH-merged parent → REBASE the child, don't just retarget.** A squash deletes the parent branch and lands its commits as ONE squash commit — the child's COPIES of those commits are NOT in the base, so auto-retarget (or a manual base change) makes the child's diff DOUBLE-show the parent's changes, and the old base ref may be gone (PR-open/retarget fails "Base ref must be a branch"). Fix: rebase the child `--onto <new-base> <old-parent-tip>` (replay only the child's commits), or fresh-branch off the new base + cherry-pick just the child's commits (force-push is often blocked, so a fresh branch is cleanest). VERIFY `git diff <new-base>...HEAD --stat` shows ONLY the child's files before repointing the base.

## Notes
- This loop composes with `cross-review` (internal, pre-push, different-vendor) and `fanout` (each parallel PR runs its own loop). Internal cross-review and the bot often overlap the same commit — merge their findings into ONE fix pass to avoid double churn.
- Waiting is the framework's job: supervise sub-agents via the inbox, the bot via `gh` + a timer. Never `sys_timer_set` a self-ping to poll a sub-agent that auto-wakes you — timers are only for the bot's wall-clock lag.
- Recurring races/edge-cases in the SAME state machine across rounds are the classic 3-strikes trigger — prefer a structural fix (e.g. serialize mutations behind one lock) over patching each window.
