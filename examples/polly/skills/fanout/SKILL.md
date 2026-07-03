---
name: fanout
description: Run independent subtasks in parallel — one git worktree and one implementation sub-agent per task. Each task is cross-reviewed on its branch diff FIRST (see review-before-pr); the implementer opens its PR only after review is clean. polly never merges; the human does.
---

# fanout — safe parallel execution

Use ONLY for subtasks that are parallel-safe (no shared files, no ordering
dependency).

## Procedure
1. Per task, create an isolated worktree:
   `sys_os_shell("git worktree add .worktrees/<task_id> -b polly/<task_id>")`.
   Record the worktree path + branch in the registry
   (`.polly/registry.json`).
2. Dispatch one implementation sub-agent per task, scoped to its worktree:
   `sys_session_send(agent="claude_code"|"codex"|"opencode", title="<task_slug>",
   args={purpose: "implement", input: "<task + acceptance contract +
   worktree path>"})`. Use a short task-based title such as `auth-refactor` or
   `fix-sse-error`, never the raw vendor name. State the scope and that it must
   work only inside `.worktrees/<task_id>`. The worker drives the task to green
   and pushes its branch, but MUST NOT open a PR yet (see `review-before-pr`) —
   it reports its branch name + summary and waits. The PR is opened only after
   the diff passes cross-review. Every commit the worker authors must
   end with a blank line followed by the exact co-sign trailer as its final
   line — `Co-authored-by: omnigent <noreply@omnigent.ai>`.
   Record each handle's `conversation_id`
   in the registry. Emit the worktree + `sys_session_send` tool calls in THIS
   turn — never end a turn having only said you will dispatch; the dispatch
   calls and their announcement go in the same turn. Dispatch the whole
   parallel-safe set, THEN (and only then) END YOUR TURN. Do not poll.
3. Each sub-agent runs autonomously and notifies you through the inbox when it
   finishes. Collect its structured result with `sys_read_inbox` and record the
   PR URL in the registry. If the inbox result is empty/unclear, inspect that
   worker conversation with `sys_session_get_history` before deciding what to do
   next.
4. Send each finished task's BRANCH DIFF through `cross-review` — before any PR
   exists (`git -C .worktrees/<task_id> diff main...HEAD`, not `gh pr diff`).
5. polly does NOT merge — the PR is the deliverable. When cross-review passes
   (gates green + zero blocking issues), tell the SAME implementer to open its
   PR on the now-reviewed branch, then mark it ready in the registry with its
   PR URL and leave it for the human to review and merge. Never run
   `git merge` / `gh pr merge`.
6. Remove a finished worktree (`git worktree remove`) only once its PR is open
   and review is clean — the branch lives on the remote, so the worktree is
   disposable. Don't remove a worktree that still has open fix-tasks.

## Notes
- Respect the per-turn dispatch cap (enforced by policy). More tasks than the
  cap → dispatch in waves (let the running batch finish before dispatching more).
- The human can open any sub-agent in the UI's Subagents panel and read its
  conversation while it runs.
- If a running worker is wrong, runaway, superseded, or no longer useful, call
  `sys_cancel_task` with `task_id` set to the recorded `conversation_id` before
  dispatching a replacement. `claude_code` is hard-stopped; `codex` cancellation
  is best-effort until its runner-side hard-stop exists.
- A sub-agent that returns a dark or failing result: don't re-prompt it in a
  loop — re-dispatch a fresh implementation sub-agent in a clean worktree, or
  escalate to the user.
- Because polly never merges, cross-PR conflicts surface when the human merges,
  not here. Keeping each parallel task's file scope disjoint is what keeps that
  rare — honor it.
