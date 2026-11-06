# External bot and CI evidence

Record the PR head, push time and bot account. Only that bot's signals count;
use the later of created/updated timestamps for editable comments. Reactions
are idempotent: a new +1 can establish a first-round clean verdict, but an old
reaction never establishes a verdict on a fix push. Later rounds need explicit
clean comment/review/inline evidence for the current head. Eyes means engagement,
not acceptance. Prior findings do not disappear because another push occurred.

Default wait budget: ten single-shot sweeps about two minutes apart, cumulative
for this deliverable across head changes and fix pushes. Set a different bounded
budget if the contract requires it. Never leave a repeating timer. This scheduled
external wait is distinct from workers, which wake Molly through the inbox.

Each sweep reads current head, reactions, root comments, reviews, inline threads,
check runs and required check/status results. Apply in order:

1. Head differs: stop; preserve local state, inspect the new head and return to
   verify for fresh acceptance evidence. Never use a verdict on another head.
2. Failed/cancelled/timed-out/stale/unknown check conclusion: not green.
   `action_required` goes to the human, not a code-fix loop. A clean completed
   check conclusion is success, skipped or neutral subject to repo requirements.
3. New findings: triage through verify; only accepted required defects become
   remediation. Preserve prior outstanding findings and dispositions.
4. Pending/missing checks: keep waiting within budget. An empty read is unknown
   unless a recorded repo configuration or human decision establishes no checks
   are expected. Newly appearing checks retire that exemption. Read legacy
   statuses too where configured; don't confuse their absence with check success.
5. Checks pass and the current head has a clean bot verdict with no outstanding
   required finding or unresolved required gate: external readiness established.
6. Otherwise wait within the remaining budget.

At the cap, stop and report exactly what is pending. Never call silence approval.
A valid FOLLOW_UP/ADVISORY need not cause edits, but an outstanding bot objection
is not a clean bot verdict: if clean bot approval is required, ask the human to
adjudicate the disposition instead of silently waiving it or endlessly fixing.

Use remediate's shared budget and targeted recheck for fixes. After accepted
fixes, publish an additional commit with a plain push. When authorized to comment,
reply in-thread with dispositions and request review on the PR root. Otherwise
report the required communication to the human. Refresh the bot verdict for the
new head. Report unresolved thread counts; a reply is not thread resolution.
