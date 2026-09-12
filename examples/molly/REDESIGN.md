# Molly v2: scoped delivery

## Baselines inspected

- Andrew Reid's `molly`: `57365be0f` (2026-09-12 fetch), including the core,
  both native worker definitions, and investigate/fanout/worktree-routing/cross-review.
- Upstream `omnigent-ai/omnigent` main: `7a8846b26` (2026-09-12 fetch), including
  Polly's core and investigate/fanout/cross-review skills.

Molly has stronger candidate identity and publication discipline than Polly.
Preserve those mechanics while replacing the mandatory, repeated full-review
policy. Upstream supplies useful readiness and test-count handling, but also
retains mandatory cross-vendor review. Its policy is a comparison arm, not v2's
acceptance criterion.

## Instruction disposition / implementation plan

| Current instruction family | Destination | First increment |
| --- | --- | --- |
| Delegate coding and substantive investigation; direct prose allowed | Core | Keep role boundary, remove repeated explanations |
| Task decomposition and plan gate | Core + scope skill | Acceptance contract, non-goals, risk class; ask only for material decisions |
| Mandatory different-vendor review, including prose | Core + verify | Conditional review; high-risk normally requires it; explicit repo/user gates win |
| Full adversarial mandate on every round | cross-review + remediate | Initial scoped review, then issue/delta/regression recheck |
| CLEANUP always mandatory; repo-wide CLASS closure and new lint guard | verify + remediate | SHOULD_FIX requires scope and proportionality; wider work is FOLLOW_UP unless essential to acceptance |
| Final accepted tree, frozen writer, immutable report, artifact digest | worktree-routing + cross-review + publish | Retain; acceptance can be based on checks without an independent review |
| Shared/separate/integrate topology, seed preservation, root baselines | worktree-routing + fanout | Retain; remove compulsory checkpoint-plus-release review duplication |
| Dispatch shape, readiness, model rules, result validation, cancellation | dispatch | One situational operational skill; do not infer successful boot from a model list |
| Goal-mode command syntax | dispatch | Optional IMPLEMENT only; completion is staged handoff, never implicit publication |
| Test-count reconciliation | verify | Same command, files and candidate; collected cases, not function counts |
| GitHub reads/mutations, bot timing, CI identity, unresolved threads | publish + bot reference | Retain evidence safeguards; bounded wait, scoped triage, no silent approval |
| Ordinary architecture/coding/testing tutorials in worker prompts | Delete | Keep worker role, worktree and staged handoff contracts only |
| Forced vendor rotation / fresh worker after every task | Delete | Choose on capability, cost and relevant context; preserve fixer continuity |
| Universal architecture/simplicity/bloat essays | Delete | Findings and verification evidence only |
| Skill authoring destination, terminals, same-turn dispatch | dispatch | Preserve terse obligations |
| Destructive shell command protection and purpose validation | Existing code | Preserve configured policies in `omnigent/policies/builtins/orchestration.py` (legacy handler shim retained) |
| Dispatch identity/model validation and wakeup | Existing code + dispatch | `omnigent/tools/builtins/spawn.py` already describes/enforces API; retain operational recovery |
| Durable writer leases, candidate-bound publication authorization | Future runtime enforcement | Not implemented here; retained as prompt discipline until an atomic runtime design exists |
| Shell protection bypasses; advisory spawn counter | Documentation + dispatch/publish | Do not call these a sandbox or a reliable publication gate |

Implement in this order: core and scope/dispatch; verification and remediation;
worktree/fanout and publication; workers; bundle-loading tests and paired scenarios.
Keep all changes inside the Molly bundle plus its focused tests. Preserve credentials, terminals and guardrails. Adopt upstream
Smart Routing harness opt-in and provider-driven brain model selection. Do not change the shared runtime
for a Molly-specific policy experiment.

## Acceptance and limitations

A small task can reach DONE after implementer checks plus Molly's acceptance
assessment. Standard work gets independent review when a specific uncertainty
justifies it. High-risk work normally gets architecture and cross-vendor review;
unavailability is escalated, never silently downgraded. A fresh target needs
fresh evidence, not automatically a fresh unconstrained audit. One remediation
and targeted recheck is the default; unresolved material problems trigger diagnosis
or a human decision, not a success verdict or another blind loop.

The 150–300-word core is a hypothesis about cognitive focus, not a performance
claim. Skill descriptions still consume context, and loaded skills may remain in
session history. Measure whole-session cost, not just initial prompt length.
The YAML guardrails are preserved but do not atomically enforce worktree ownership,
acceptance, or publication. Native workers remain unsandboxed. Moving prose into a
skill does not turn it into code enforcement.

`evals/README.md` defines paired optional/mandatory review trials and hidden-defect
checks. Bundle tests establish parseability and skill discovery, not model
convergence or correctness equivalence. Do not claim optional review is validated
until recorded live trials support it. Keep the mandatory policy available through
an explicit acceptance-contract override during the pilot.

## Follow-up incorporated 2026-09-13

Restore systemic diagnosis on first remediation, bounded by affected behavior;
add reviewer examples for required related fixes, isolated errors and unrelated
follow-ups. Add evidence-based transitions, durable decision handoffs and failure
classification. Incorporate the wiring audit: auto harness routing, explicit
advisor-to-worker model mapping, terminal launch discovery and protected/default
branch publication exclusion. The core remains 267 words. Transition conditions
remain skill obligations, not a newly implemented runtime state machine.

Sources informing the update:
- https://www.anthropic.com/engineering/building-effective-agents
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- https://www.anthropic.com/engineering/harness-design-long-running-apps
- https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph
