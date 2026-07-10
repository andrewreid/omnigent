---
name: cross-review
description: Verify an implementer's diff with an INDEPENDENT, different-vendor sub-agent (diff plus contract only); turn blocking issues into fix-tasks and loop until clean.
---

# cross-review — independent verification

The implementer never signs off on its own work — a different model does, and
review is a sub-agent that returns a structured report, not a transcript
anyone needs to read through.

Default review is *confirmatory* — the reviewer gets the diff + contract and
answers "does this fix do what it claims?". That is enough for isolated, low-state
changes and NOT enough for stateful, combinatorial surfaces, where it is the single
biggest time-sink polly hits: the states the contract never listed get discovered
reactively, one external-bot round at a time, in the slow post-PR loop. Match the
review DEPTH to the surface (see below).

## Procedure
1. Get the task's diff. Per `review-before-pr`, review runs BEFORE a PR exists,
   so take the branch diff: `git -C .worktrees/<task_id> diff main...HEAD`. Use
   `gh pr diff <pr>` only for a pre-existing PR you did not just create.
2. Run the deterministic gates first — tests / lint / typecheck via
   `sys_os_shell`. If red, re-dispatch the implementer to drive it green first;
   don't involve the reviewer yet.
3. Dispatch a DIFFERENT-vendor sub-agent as reviewer: pick any AVAILABLE worker
   whose vendor differs from the implementer's — `claude_code`, `codex`,
   `opencode`, `cursor`, `hermes`, or `pi` (e.g. Claude built it → any of
   `codex` / `opencode` / `cursor` / `hermes` / `pi`, and so on). Use a
   task-based title such as `review-auth-refactor`, never the raw vendor name:
   `sys_session_send(agent="claude_code"|"codex"|"opencode"|"cursor"|"hermes"|"pi", title="review-<task_slug>",
   args={purpose: "review", input: "<the diff> + <the acceptance contract>.
   Review ONLY against the contract. Report blocking / non-blocking /
   suggestions. Do not edit code."})`. Give it the diff as text — do NOT point
   it at the implementer's worktree. Fetch the diff and emit the
   `sys_session_send` call in the SAME turn you decide to review — never end a
   turn having only announced "I'll load cross-review and fetch the diff" with
   no tool call (that dropped turn stalls the run; nothing dispatches and no
   inbox wake arrives). Once the reviewer dispatch is in flight, end your turn;
   collect the inbox-delivered structured report with `sys_read_inbox` when it
   returns. Use `sys_session_get_history` only to debug an empty or unclear
   review result.
4. The reviewer SURFACES issues; it does not fix them.
5. For each **blocking** issue: add a fix-task to the registry scoped to the
   same worktree, and send the concrete fixes back to the SAME implementer
   conversation via `sys_session_send` — reuse the original implementer's
   `agent` + `title` (or address it by `session_id`) with
   `purpose: "implement"`, so the worker keeps its worktree/branch context and
   updates its existing PR. A new title would spawn a fresh worker with no
   memory of the task. Then loop to step 1.
6. When gates are green AND there are zero blocking issues, the diff passes
   review. Now (and only now) tell the SAME implementer to open the PR on its
   reviewed branch (per `review-before-pr` — the PR is opened on the reviewed
   product, and only the implementer opens PRs). Then mark it ready in the
   registry (with its PR URL) and leave it for the human to merge. polly does
   NOT merge it.
7. If the contract can't be satisfied after a few loops, stop and escalate to
   the user with specifics.

## Match review depth to the surface — confirmatory is not enough
A confirmatory review confirms exactly the cells the contract lists and is BLIND to
every state nobody enumerated. On a **combinatorially-rich surface** — serializers,
form↔payload round-trips, state machines, derivations/classifiers whose output
depends on many input states (loaded vs edited × valid/invalid/empty inputs ×
per-line vs aggregate flags × present/absent references) — the contract can only name
a handful of states, so a confirmatory pass keeps signing off diffs that the external
Codex bot's whole-state-space reasoning then breaks LATER, one edge per round. That
"bot found another edge → patch → re-review → bot found the next" grind is a
confirmatory review that should have been ADVERSARIAL.

Classify the surface BEFORE dispatching the reviewer. If it is rich, escalate review
to adversarial:
- **Widen the reviewer's context.** Give it the changed surface PLUS its adjacency
  (the read / serialize / validate functions around the diff, the type it targets,
  the consumers) — not just the raw hunk. Independence comes from a DIFFERENT vendor
  and withholding the implementer's transcript, NOT from starving the reviewer of
  context.
- **Change the mandate.** Not "verify the fix" but "find the states that break":
  instruct it to ENUMERATE the state space of the surface and ATTACK the cells the
  contract did not name — "list every combination of {axes}; for each, does
  load→edit→serialize round-trip correctly? which combinations silently drop,
  mis-write, or wrongly block?". Mirror the external bot.
- **Demand the FULL list in one pass.** The reviewer surfaces every issue it can find
  now, exhaustively — not a trickle that becomes N rounds.
- **Front-run the external bot.** Prefer running this adversarial pass on the SAME
  engine as the external reviewer (`codex`) so its whole class of findings lands in
  the FAST internal loop instead of the slow PR loop (see `pr-bot-loop` → front-run
  the bot). Keep it a DIFFERENT vendor from the implementer.

The cheapest place to close a state-space bug is the implementer's FIRST pass: pair
this with `review-before-pr` — the acceptance contract for a rich surface must name
the state-space axes and require an invariant / round-trip matrix test as a delivered
artifact, so the surface is built right once instead of hardened reactively.

## Notes
- Cross-review requires a reviewer from a DIFFERENT vendor than the implementer,
  so it needs at least two AVAILABLE workers (per polly's roster preflight). If
  only one worker — or only one vendor that can review this implementer's PR —
  is available on the machine, you CANNOT run independent cross-vendor review:
  don't dispatch a reviewer that can't boot, say so explicitly, and pull in the
  human at the plan gate.
- Give the reviewer ONLY the diff + contract — never the implementer's
  transcript or worktree. The cross-vendor independence is the whole point.
- Review is a coding sub-agent (`claude_code`/`codex`/`opencode`/`cursor`/`hermes`/`pi`) dispatched with
  `purpose: "review"` — a DIFFERENT vendor from the one that built the diff. It
  reports issues and never edits; only the implementer opens a PR, so a stray
  reviewer edit never reaches the deliverable.
- Non-blocking issues / suggestions go in the registry as follow-ups; they
  don't block the PR.
