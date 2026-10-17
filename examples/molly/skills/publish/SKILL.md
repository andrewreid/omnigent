---
name: publish
description: "Molly ONLY - Publish an accepted candidate and report CI/bot status without unbounded remediation."
user-invocable: false
---

# Publication of an accepted target

Orchestrator only. Use only for the user's requested commit/PR endpoint. No automatic
publication for local-only tasks. Load verify; acceptance may rely on checks and
Molly's assessment or a selected independent review. A checkpoint alone is not
release acceptance: assess the final integrated target and required interactions.
Do not repeat a full independent review solely because its phase label changed;
record a fresh release assessment referencing evidence for the unchanged tree.

Before authorizing a commit, freeze writers and require the recorded branch, HEAD,
index tree, no unstaged changes and no nonignored untracked files. Acceptance and
required checks must match this candidate. On first publication HEAD equals the
recorded base. For a fix-push it equals the recorded published PR head. Unexpected
changes stop publication; preserve work rather than reset/clean it.

Only the implementer commits and opens a PR for delegated work; Molly does so for
its own prose. Authorize COMMIT ONLY first, with the accepted tree and parent.
Copy the exact trailer into the commit dispatch: `Co-authored-by: Molly
<molly@reidwerk.au>` for claude_code or Molly-authored prose; `Co-authored-by:
Molly <noreply@reidwerk.au>` for codex. Render each as one final trailer line,
preserving the existing worker identities. After commit, verify `HEAD^{tree}`
equals the accepted candidate, `HEAD^` equals the recorded parent, exactly one
new commit exists since that parent, and the worktree is clean. Commit hooks that
change the tree invalidate acceptance: return to verify before any push.
Before authorizing push, identify the remote destination and confirm it is the
recorded task branch, not the default or a protected branch. Include that exact
remote/branch in the worker packet. If protection status or destination is unknown,
resolve it before publication; plain-push permission is not permission to push main.
Only then authorize a plain push and requested PR creation. Never amend published
commits, force-push, delete remote refs, or merge a PR. Fixes use additional commits.
Check the remote branch equals the accepted local HEAD after every push.

GitHub reads prefer the read-only `pull_request_read` MCP tool. Mutations use shell
Git/gh, preserving the existing policy route; do not bypass it with MCP writes,
aliases, eval or nested shells. The text-matching blast_radius policy is not a
sandbox or an atomic publication gate. Respect repository PR templates. Read check
runs plus required checks (`gh pr checks --required`) and any configured legacy
statuses; neither zero legacy statuses nor an empty check-run list proves green.
For reactions use `gh api repos/O/R/issues/N/reactions` when MCP lacks the method.

If a review bot is configured/required for this repository, load
[references/review-bot.md](references/review-bot.md). Do not invent a bot gate in a
repository without one. The user/repository's required checks remain mandatory.
Report the PR URL, exact head, checks, bot status, unresolved thread count and
follow-ups. Separate implementation acceptance from external release readiness;
pending checks or exhausted waiting are not approval. The human merges.
