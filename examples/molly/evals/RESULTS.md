# First-increment validation — 2026-09-12

## Executed

- Production bundle loader: both native workers and all ten skills load; worker
  definitions contain no bundled orchestrator skills. The bot reference survives
  copying the bundle to a new directory.
- Focused pytest run: 118 passed (two new Molly tests plus 116 existing
  orchestration-policy tests). These check packaging and existing deterministic
  guardrails, not actual worker compliance or end-to-end publication.
- An independent Codex subagent read the v2 bundle and all skills, then simulated
  five scenarios without edits or worker launches. It selected minimal process
  for accepted UI and dev utilities; diagnosis for recurring retry failures;
  interaction verification with disclosed independence limits for a multi-file
  feature; and escalation when high-risk cross-vendor assurance was unavailable.
- That dry-run found ambiguity in high-risk exceptions, mixed-vendor decisions,
  solitary isolation and co-author packet requirements. The instructions now
  explicitly assign those decisions and preserve the existing trailer identities.
- The generic Codex skill validator rejects OmniGent's supported
  `user-invocable` frontmatter extension. The production OmniGent parser accepts
  it; the extension is retained and covered by the bundle test.

- Applicable pre-commit hooks passed except `no-hardcoded-models`, which flags
  the pre-existing `claude-opus-5` pin in Molly's executor configuration. That
  value is unchanged from `57365be0f`; model selection was deliberately held
  constant for this policy experiment. The complete pre-commit run is therefore
  not green. No suppression or lint exemption was added.

## Not established

No live paired Claude Code/Codex task trials were run. No measured cost, latency,
convergence rate or defect-escape comparison exists yet. The simulated responses
are not proof that optional cross-review preserves correctness. Use README.md's
paired pilot before making that claim or treating this candidate as validated
for broad unattended use. No new runtime publication/worktree enforcement was
introduced; these obligations remain prompt discipline atop existing policies.

## Updated validation — 2026-09-13

- 123 focused tests passed: bundle loading, existing orchestration policies,
  real Molly executor Smart Routing opt-in, routing-off and explicit-override cases.
- All applicable pre-commit hooks passed. Upstream-style auto harness routing and
  provider model selection replace the fixed model pin, resolving the earlier lint
  failure recorded above.
- Independent simulated forward-check found the intended boundaries held for shared
  defects including unchanged helpers, isolated typos, unrelated defects, worker
  replacement after human acceptance, and transport failures versus code recurrence.
  Clarified replacement-fixer continuity and uncertain dispatch recovery in response.
- This remains a source/test and simulated validation result. No live paired vendor
  comparison or deployed-session adoption is claimed.
