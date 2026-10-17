---
name: remediate
description: "Molly ONLY - Diagnose isolated versus shared defects, fix affected paths, then recheck coverage."
user-invocable: false
---

# Bounded remediation

Orchestrator only. Start from verify's triage. Prefer the original fixer; a
justified replacement receives the same complete packet and used-attempt count
only after the previous writer is confirmed stopped. Give the fixer the full
immutable report(s), digests, review target(s), and the accepted BLOCKING and
SHOULD_FIX action list. Preserve challenges/dispositions, including FOLLOW_UP and
ADVISORY. For findings from gates rather than a reviewer, record command, candidate,
output and the concrete failure instead of inventing a review artifact.

Before the first fix, ask the implementer to distinguish an isolated mistake
from a shared cause. For a shared cause, identify the affected execution paths
within the acceptance boundary and fix the cause and related instances together.
The boundary is affected behavior, not just the reported line or changed files:
an unchanged helper or caller may need edits to satisfy the task. Inspect beyond
that boundary when needed to establish impact, but do not automatically make
unrelated discoveries required work. A broad enumeration is warranted only when
needed to establish coverage; a short explanation suffices for an isolated typo.
Require verification that distinguishes a systemic fix from a one-site patch.

Load dispatch/worktree-routing: reacquire the sole writer lease, repeat absolute
path, branch, expected HEAD and EXPECTED SEED TREE equal to the current candidate.
For unpublished work the worker's BASE OID is the original base; after publication
it is the recorded published HEAD (the parent of the next fix commit). Keep the
original review base separately in the review target so cumulative scope is not lost. Require
report digest/target verification before edits. The fixer chooses the implementation,
runs affected checks, stages, reports every disposition and the new tree, then stops.

Recheck whether the defect is addressed across the identified affected paths,
not merely whether the reported line changed. Check the fix delta and regressions to affected
acceptance criteria. When independent review was selected, use cross-review with
`review_kind=targeted` and supply the original findings plus dispositions and old/new
trees. This produces a fresh report bound to the new target; an old verdict is
never reused. Unrelated improvements become FOLLOW_UP. New material defects still
block acceptance and count against the existing remediation budget.

Do not automatically repeat a complete architecture/quality audit. Classify a
recurring bug as evidence of inadequate diagnosis or coverage: enumerate related sites only as needed to
establish acceptance impact. Repository-wide closure or a future-addition lint
rule is justified only by the contract/risk, not the word CLASS.

After the default one remediation batch and targeted recheck, unresolved material
problems go to diagnosis (architecture if useful), re-scope or the human. Explain
what remains and why another bounded attempt would help. No blind loop, automatic
budget reset, or false DONE. The same rule applies after publication and bot fixes.
