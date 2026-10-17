# External bot and CI evidence

Record the PR head, push time and bot account. Only that bot's signals count;
use the later of created/updated timestamps for editable comments. Reactions
are idempotent: a new +1 can establish a first-round clean verdict, but an old
reaction never establishes a verdict on a fix push. Later rounds need explicit
clean comment/review/inline evidence for the current head. Eyes means engagement,
not acceptance. Prior findings do not disappear because another push occurred.

When CI or review-bot evidence is pending, submit the current evidence through
`factory_submit_result` and return. The factory owns observation and wakes Rosie;
do not schedule timers or poll.

On each wake, refresh the current head, reactions, root comments, reviews, inline
threads, check runs and required check/status results. Apply in order:

1. Head differs: stop; preserve local state, inspect the new head and return to
   verify for fresh acceptance evidence. Never use a verdict on another head.
2. Failed/cancelled/timed-out/stale/unknown check conclusion: not green.
   `action_required` goes to the human, not a code-fix loop. A clean completed
   check conclusion is success, skipped or neutral subject to repo requirements.
3. New findings: triage through verify; only accepted required defects become
   remediation. Preserve prior outstanding findings and dispositions.
4. Pending/missing checks: submit the current evidence and return. An empty read is unknown
   unless a recorded repo configuration or human decision establishes no checks
   are expected. Newly appearing checks retire that exemption. Read legacy
   statuses too where configured; don't confuse their absence with check success.
5. Checks pass and every bot finding on the current head has an outcome (fixed,
   deferred, or advisory) with no unresolved required gate: external
   readiness established. A clean bot verdict is not required.
6. Otherwise submit the current evidence and return.

Report exactly what is pending. Never call silence approval.
Give every finding one outcome. Fix in this PR: a real defect in the plan's area
with a small, local fix (the default for in-scope nits). FOLLOW_UP: a real defect
with real-world impact outside the plan, or one that would materially grow the
PR. ADVISORY: a speculative edge case with no realistic trigger here, a style
preference, or hardening against threats not in play. For both, reply on the
thread with the outcome and resolve it, then list it in the result's `deferred`;
the factory files one follow-up issue per PR. If the issue is itself a follow-up,
defer nothing as a follow-up. Resolve only threads you replied to. From review
round strict_from_round (factory_get_status) only P1 findings with real impact
must be fixed; once the rounds are used up, fix remaining in-scope P1s, defer the
rest and do not request another review. Work the owner directs be filed
separately is not a finding: list it in `split_out`. Never open issues yourself.
Ask the human only when a required defect cannot be fixed within scope.

Use remediate's shared budget and targeted recheck for fixes. After accepted
fixes, publish an additional commit with a plain push. Reply in-thread with each
disposition (the factory authorizes this and thread resolution through the bot
wrapper), then resolve that thread, and request review on the PR root while
review rounds remain. Refresh the bot verdict for the new head. Report unresolved thread counts.
