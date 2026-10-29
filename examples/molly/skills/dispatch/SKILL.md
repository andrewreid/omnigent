---
name: dispatch
description: "Molly ONLY - Load before dispatching workers; readiness, API packets, results and recovery."
user-invocable: false
---

# Worker dispatch

Orchestrator only. Workers must do their assigned work themselves, not dispatch
or load orchestrator skills. Keep every required worker instruction in `args.input`;
native workers may not receive their configured prompt in every runtime path.

Before the first dispatch, call `sys_session_get_info({})` once. Read
`configured_harnesses`: `claude_code` maps to `claude-native`, `codex` to
`codex-native`. Only literal `true` is ready. False or strings such as
`binary-missing`, `needs-auth`, or `version-too-low` are unavailable. An absent/null
map is unknown: one `command -v claude codex` probe can establish binary presence,
not authentication. Drop boot failures from the roster for this run. Do not retry
a missing CLI. Report missing capabilities only when they affect the task.

Use `sys_session_send` with `agent`, a task-based `title`, and
`args={purpose: implement|review|explore|search, input: ...}`. Alternatively use
`session_id` for an existing direct child. Reusing agent/title continues that
conversation. Record its returned `conversation_id`. Choose capability, cost and
useful context, not forced vendor rotation. `args.model` is create-time only;
inspect `sys_list_models` and its readiness/verification evidence when selecting
an override. A model listing does not guarantee launch. Read dispatch errors
rather than repeatedly retrying. For fanout, use `sys_advise_models` when available
with the planned tasks; apply each suitable recommendation as that task's
initial `args.model`, retaining user constraints and recording any deviation.
A Cost advisor note describes the current brain choice; it does not constrain
worker count, vendor or model selection.

Every input states: do this work yourself; never delegate onward; report a concrete
result. Include the contract and absolute worktree. IMPLEMENT additionally gets
mode, branch, BASE OID, EXPECTED SEED TREE, exclusive writer ownership, preservation
of accepted seed changes, and stage/report/stop without commit or publication.
Before editing require matching branch, HEAD/base, index/seed, no unstaged changes
or nonignored untracked files; stop on unexpected ownership/state. Before staging
check that every new change belongs to the task, then stage the complete candidate,
verify no unstaged/nonignored untracked files, and report HEAD, branch, tree and
checks. Native worker defaults are not a substitute for these packet fields.
REVIEW gets the complete packet from cross-review. EXPLORE/SEARCH is read-only
and returns file:line or source evidence. Repeat these bounds on continuations.
Do not assume dispatch binds the worker's cwd; require it to enter and verify the
assigned worktree before touching files. Delegate integration conflicts too.

For long IMPLEMENT work, optional goal mode is one standalone `/goal <condition>`
input, with the full packet inside the condition. Completion is a verified staged
handoff, not permission to publish. Do not use it for review/explore/search or
invent a goal parameter. Honor the six-dispatch cap; the configured counter is
not a durable scheduling guarantee.

Act in the same turn as announcing dispatch. Once calls are in flight, wait for
the inbox wake; no timers or busy polling. Use `sys_read_inbox` for completion.
Empty/unclear results and announcements require `sys_session_get_history`.
If a worker delegated onward, send it back to do the work itself. Coverage claims
need the enumerated evidence that supports them; a bare assurance is not closure.

Cancel runaway/superseded work with `sys_cancel_task(task_id=conversation_id)`.
Wait for confirmed termination before transferring a writer lease; a cancellation
request alone is insufficient, especially for best-effort cancellation. A task
failure may justify one fresh worker in a clean worktree; repeated failure needs
diagnosis or escalation. Never mask a boot failure as a code failure.

Use own tools for registry, worktrees and deterministic gates, not coding or deep
investigation. For servers/watchers use `sys_terminal_launch` with the shell
terminal and assigned `cwd`; never launch coding workers there.
Author skills only under `examples/molly/skills/`, never host user-scope folders.
Do not evade destructive-operation policies through shell wrappers or other APIs.

## Route recovery by failure type

- Transient network/rate-limit failure: bounded retry with backoff, after checking
  whether a side effect already completed; do not duplicate a dispatch or push.
- Invalid tool arguments: use the tool error/schema to correct the invocation.
- Repeated code defect: remediate/diagnose the cause and affected-path coverage.
- Ambiguous acceptance: resolve the criterion; ask the human only if material.
- Missing capability/authentication/authorization: escalate that dependency,
  not a code-fix task. Discover available platform capabilities when relevant,
  but never silently replace required independent assurance.
- Unexpected or repeated runtime failure: preserve evidence and diagnose/escalate.

Record the failure class separately from engineering remediation attempts. A
transport retry must not consume a code-fix round or reset its budget.

If a dispatch times out without a conversation_id, inspect available session
listing/history evidence for the named task before retrying. If its execution
state cannot be established, preserve that uncertainty and diagnose/escalate;
do not create another title to bypass the ambiguity.
