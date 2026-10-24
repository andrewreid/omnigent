---
name: verify
description: "Molly ONLY - Assess candidate acceptance and select proportionate independent review."
user-invocable: false
---

# Acceptance assessment

Orchestrator only. Assess the complete deliverable against the scope contract.
Verify the reported branch, HEAD, staged tree, clean unstaged state and absence
of nonignored untracked files under worktree-routing. Record evidence by criterion
and candidate tree. Missing evidence is NOT ESTABLISHED, not a pass or proof
of a defect. Seek the specific missing evidence without widening the task. Do not infer success from
Git status alone. Run required gates; reuse reliable results for the same candidate
and environment instead of reflexively duplicating every implementer test.

When reconciling pytest totals, use collected cases from `python -m pytest
--collect-only -q <same files>` at the same candidate and environment as the
reported command. Test functions are not case counts. Do not accuse a worker of
miscounting by comparing different file sets or revisions.

Apply the selected verification plan: SMALL normally uses checks plus acceptance;
STANDARD selects independent review for a concrete confidence gain; HIGH-RISK
normally requires it. Explicit repo/user review gates prevail. If selected, load
cross-review. Record skipped independent review and its reason honestly; an
acceptance assessment is not an independent review. Multi-task integration needs
checks of interactions, but not automatic full reviews of every intermediate tree.

Triage every finding, including bot reports:
- BLOCKING: prevents acceptance through correctness, regression, safety, security
  or a contract failure. Must resolve; scope cannot excuse a change's dangerous
  behavior. If the necessary remedy exceeds authorization, escalate.
- SHOULD_FIX: a real defect within agreed scope whose remedy is proportionate
  to this task's risk and benefit. Resolve or explicitly reclassify with evidence.
- FOLLOW_UP: valid but materially wider, pre-existing or disproportionate work
  unnecessary for acceptance. Record it; only the human expands scope.
- ADVISORY: no edit required. Preserve the observation without making a task.

Molly owns triage, not the reviewer. Preserve the original report and record each
classification/disposition with rationale; never silently waive a blocker. Human
acceptance closes subjective criteria, not separate functional/accessibility defects.
A generic preference or possible future requirement is not a SHOULD_FIX defect.

If required findings remain, load remediate. Otherwise record acceptance for this
exact target, checks, review artifacts (if any), dispositions and limitations.
New edits invalidate that acceptance: assess their delta and affected criteria,
using targeted recheck where appropriate. Do not transfer a verdict to a new tree.
DONE requires the requested endpoint; use publish only if commit/PR is requested.
