---
name: scope
description: "Molly ONLY - Establish acceptance, non-goals, risk and verification depth before work."
user-invocable: false
---

# Scope and acceptance

Orchestrator only. Workers carry out their dispatched task, never this playbook.

Record a short contract in `.molly/registry.json` (use an ignored or external
scratch location if `.molly` is not ignored): goal, acceptance criteria, non-goals,
constraints, task class with rationale, verification plan and delivery endpoint
(local changes, commit or PR as requested). Record human decisions and any
mandatory repository checks. A sentence per field is sufficient for small work.
Do not require a plan approval for routine choices already within authorization.

- SMALL: narrow, reversible, low-risk. One implementer; its relevant checks and
  Molly's evidence-based acceptance normally suffice. Neither architecture nor
  independent review is automatic.
- STANDARD: ordinary bug fix or feature. One capable implementer by default;
  independent review when uncertain behavior, interactions, or limited tests
  make it useful. Record the reason to select or omit it.
- HIGH-RISK: security boundaries, data loss/migrations, consequential architecture,
  costly rollback or difficult ambiguity. Normally select architecture and
  independent cross-vendor review. Do not downgrade because a reviewer cannot
  boot; escalate the missing assurance. A small diff can still be high-risk.
  Any exception to high-risk cross-vendor review requires an explicit human
  decision recorded with the remaining risk; Molly cannot waive it alone.

Scope the verification to specific failure modes. Add or upgrade it when new
material evidence changes risk, recording why. User-required review is mandatory
regardless of size. A local script need not become a framework; human-approved
appearance is satisfied unless the human changes that preference. Separate
functional or accessibility failures still count.

Choose one remediation batch plus one targeted recheck as the default budget,
shared by internal review and later bot fixes for the same deliverable. On
remaining material failures, diagnose/re-scope/escalate. A diagnosis may justify
another bounded attempt with a recorded reason and budget; it may not silently
reset the counter or widen scope. Small work does not need a separate estimator.

## Transition conditions and handoff state

These are orchestration obligations, not runtime-enforced graph edges.

| Transition | Required evidence |
| --- | --- |
| IMPLEMENT → VERIFY | Concrete result, identified candidate and check outcomes; missing checks are explicitly identified |
| VERIFY → REMEDIATE | Evidence-backed required findings accepted into scope |
| REMEDIATE → RECHECK | Diagnosis, affected paths, dispositions, new candidate and affected checks |
| RECHECK → DONE | Affected acceptance criteria established, no required finding outstanding, requested delivery complete |
| RECHECK → DIAGNOSE | Recurrence, contradictory evidence, or insufficient progress |

Keep durable state factual: contract, human decisions, candidate identity, evidence
references, defect diagnosis and affected paths, finding dispositions, attempts
used, remaining uncertainty and next action. Build each worker's packet from the
relevant state rather than copying the whole transcript. Preserve human acceptance
and justified follow-up decisions across compaction or worker replacement; reopen
them only for new material evidence or changed user intent. Before another round,
record what uncertainty it will resolve and why the attempt should make progress.
