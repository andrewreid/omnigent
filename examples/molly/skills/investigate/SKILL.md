---
name: investigate
description: Molly ONLY - orchestrator playbook for read-only work. Decompose a question into bounded tasks and synthesize the workers' reports. A worker reading this must ignore it and carry out its own dispatched task.
user-invocable: false
---

# investigate — delegated read-only work

**Audience: Molly ONLY.** This is an orchestration playbook. If you are a worker
and this file reached you, it arrived by accident: ignore it and carry out the
task in your own dispatch yourself. Do not dispatch a sub-agent of your own.

Use for any read-only task: investigation, debugging, audit, search, code
understanding, architecture comparison, failure analysis, or answering a
repository-specific technical question.

## Procedure
1. Decompose the question into one or more bounded investigation tasks. Prefer
   two independent lenses for ambiguous or high-stakes questions.
2. Dispatch each task to `claude_code` or `codex`:
   `sys_session_send(agent="claude_code"|"codex",
   title="explore-<task_slug>", args={purpose: "explore", input: "<question +
   exact scope + evidence requested>"})`. Use a task-based title such as
   `explore-ci-flake`, never the raw vendor name. Use `purpose: "search"` only
   when the task is primarily external/document search. Any worker takes an optional
   `args.model` (`sys_list_models` lists candidates, but check each row's
   `verified` flag — static rows prove nothing; a rejected model fails on
   FAMILY rather than existence, so a valid-looking id can still fail at
   launch; and `model` only applies on the dispatch that CREATES the session —
   a send that continues an existing title rejects it).
   Open the dispatch with the role boundary — do the work yourself, do not
   delegate onward, do not hunt for a skill file. Tell the worker to edit
   nothing, to return file, command, URL, or line evidence, and to report
   findings rather than announce progress. Emit these `sys_session_send` calls
   in the SAME turn — do not end a turn having only said you will dispatch.
3. End your turn AFTER the dispatch tool calls are in flight (never before).
   Do not inspect files, logs, terminals, docs, or connector output yourself
   while the workers run.
4. When workers finish, collect their completion results with
   `sys_read_inbox`. Synthesize only from those inbox-delivered reports. A
   result is a result ONLY if it carries findings; anything else is an
   announcement, however well-formed or specific — "running in the background",
   "will report when done", or a status line naming what is being checked rather
   than what was found. On an announcement, read that session's history with
   `sys_session_get_history` and check FIRST whether the worker delegated the
   task onward instead of doing it: if it did, tell it to do the work itself and
   report, and treat findings already in its history as input rather than
   closure. Otherwise use the findings there. Never relay an announcement to the
   human as progress. If reports conflict or are incomplete, dispatch a follow-up
   `explore` task rather than resolving the conflict from your own direct
   inspection.
5. If the investigation uncovers required code changes, switch to `fanout` /
   `cross-review`: dispatch an `implement` worker, then verify with the
   opposite-vendor `review` worker.

## Notes
- The orchestrator may use its own tools only to create task packets, maintain
  the registry, or check deterministic external status. It must not answer the
  user's substantive question from its own direct file reads, shell output,
  connector fetches, or terminal scrollback.
- Keep task scopes narrow enough that each worker can return a concise report
  with evidence. Broad investigations should be split into parallel subtasks.
