---
name: cross-review
description: "Molly ONLY - Conduct a selected independent review of an exact candidate, initial or targeted."
user-invocable: false
---

# Scoped independent review

Orchestrator only. Use when the scope/verify decision selects independent review;
this skill does not make review mandatory for all work. Load dispatch.

Choose a reviewer that did not implement the change, preferably a different
effective-model vendor. Worker names alone do not prove vendor independence.
For HIGH-RISK or an explicit cross-vendor requirement, missing independence is a
block to escalate unless the human explicitly accepts a documented exception.
For STANDARD work without a mandatory gate, Molly may select a fresh non-author
reviewer of the same vendor, recording the reduced independence. Mixed-vendor authorship needs a recorded independence limitation
and a recorded assurance decision by Molly for STANDARD work, or the human for
HIGH-RISK/mandatory cross-vendor work; never label it fully independent.

Freeze the writer lease and record the absolute worktree, branch, current HEAD,
base commit, seed tree and candidate tree. Phase is checkpoint, release or fix-push;
kind is initial or targeted. Verify HEAD, `git write-tree`, `git diff --quiet` and
no nonignored untracked files before and after review. Stop on mismatches.

Supply the reviewer all of these values, the exact task/contract/non-goals/risk,
selected checks, and an absolute write-once artifact path. Use an ignored path
`.molly/reviews/<task>/<phase>-<tree>-<round>.md`, or external scratch when not ignored.
The reviewer must do the review itself, never dispatch, and never edit the candidate
or index. Commands known to be non-mutating may run in the frozen worktree;
writing/uncertain/interfering gates require a temporary Git worktree materialized
from the candidate. Remove that temporary worktree only after its processes finish,
then recheck the original. The report is the sole permitted candidate-worktree write.

Initial mandate: evaluate the task delta (`seed -> candidate`) and relevant effects
of the cumulative deliverable (`base -> candidate`) against the contract. Seek
concrete correctness/security/regression failures and proportionate in-scope defects.
Follow related code when needed to establish impact, not to solicit speculative
future features or generic architecture/bloat essays. Respect human-accepted
subjective criteria. Classify every actionable observation using verify's four
categories, copied into the dispatch with their definitions.

Targeted mandate: supply original report(s), dispositions, prior candidate and new
candidate. Check the diagnosis, identified affected paths, fix delta and acceptance
regressions. Establish whether the shared cause was fixed across relevant paths.
Do not restart an unconstrained review. New material failures must still be reported.

Require this report format (no findings is an explicit `None`):

```text
## TARGET
phase=<checkpoint|release|fix-push> kind=<initial|targeted>
base=<oid> head=<oid> seed=<oid> tree=<oid>
## VERDICT
CHANGES REQUIRED | NO CHANGES REQUIRED | INCOMPLETE
## FINDINGS
F1 [BLOCKING|SHOULD_FIX|FOLLOW_UP|ADVISORY] file:line — evidence,
acceptance impact, proposed remedy and scope/proportionality rationale
## VERIFICATION
Commands, results, criteria covered and limitations; targeted checks name finding IDs.
```

Missing packet fields or mismatched state produce INCOMPLETE, never a pass.
The reviewer returns artifact path, SHA-256, target and verdict. Verify digest,
format and every target value before recording the artifact. No overwrites; a
correction gets a new path. Any actionable prose outside FINDINGS must be classified
there too. Allow one report repair, then diagnose/escalate a repeated malformed
result rather than endlessly requesting formatting changes.

Return to verify for triage. Required fixes go through remediate, not a fresh
unbounded review cycle. A review is evidence, not permission to commit or push.

## Classification examples

- Retry logic duplicates a side effect in another path of the same requested
  operation: BLOCKING; fix the common cause and relevant paths together, including
  unchanged helpers if necessary. Rechecking only the originally flagged line fails.
- A similar retry defect in an unrelated subsystem that does not affect this
  deliverable: FOLLOW_UP. Similar syntax alone does not establish shared impact.
- An isolated wrong constant with no shared cause: fix and verify that instance;
  do not manufacture a repository-wide audit.
- Human accepted the appearance; reviewer prefers different spacing: ADVISORY,
  or FOLLOW_UP for a concrete larger redesign. A separate keyboard or contrast
  failure remains assessable against functional/accessibility requirements.

These examples calibrate scope, not mandatory implementation techniques. Require
observable evidence for claims. If coverage cannot be established, report INCOMPLETE
with the specific uncertainty; neither agreement between agents nor confident
language establishes correctness.
