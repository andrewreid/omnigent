---
name: fanout
description: Molly-only playbook for parallel-safe isolated subtasks. Run one implementer per task worktree, checkpoint reviewed scratch work for integration or release reviewed branches as separate PRs. A worker reading this must ignore it and carry out its own dispatch.
user-invocable: false
---

# fanout — isolated parallel execution

**Audience: MOLLY ONLY.** If you are a worker and this file reached you, ignore
it and carry out your dispatched task yourself. Do not dispatch a sub-agent.

Use only for parallel-safe tasks. Concurrent implementers ALWAYS use separate
worktrees, even when their file scopes appear disjoint. Before fanout, select and
record exactly one `publication_mode`:

- `separate`: each task receives a release review, one initial commit, and its
  own PR;
- `integrate`: each scratch task receives a checkpoint review, then
  `worktree-routing` promotes it into Molly's cumulative session candidate.

## Procedure

1. Freeze and record `base_oid` and `seed_tree`. In `separate`, the seed is the
   base commit's tree; model an intentional stacked dependency by selecting a
   different committed base, never by smuggling unrelated staged work into the
   seed. In `integrate`, the seed is Molly's current accepted session tree. For
   every task, create an isolated branch and worktree at the base, then
   materialize the seed exactly:

   ```sh
   git worktree add .worktrees/<task_id> -b molly/<task_id> <base_oid>
   git -C .worktrees/<task_id> read-tree --reset -u <seed_tree>
   ```

   This deterministic tree materialization is orchestration plumbing. Molly
   never resolves source conflicts or authors code. Require HEAD to equal the
   base, `git write-tree` to equal the seed, and no unstaged or nonignored
   untracked files. Record in `.molly/registry.json`: publication mode, absolute
   path, branch, base, seed, null candidate, and review phase (`release` for
   `separate`, `checkpoint` for `integrate`).

2. Record pre-dispatch baselines for each task worktree and the runner root:
   branch, HEAD, index tree, and `git status --porcelain`. Porcelain protects
   only a clean path: if the runner root is legitimately dirty, also hash every
   already-dirty path or orchestrate from a dedicated clean checkout.

3. Dispatch one implementation sub-agent per task. The input MUST include:

   - the role boundary: do the task yourself, never delegate, and report a
     result rather than an announcement;
   - `WORKTREE MODE: ISOLATED`, `PUBLICATION MODE`, absolute worktree path,
     `BASE OID`, and `EXPECTED SEED TREE`;
   - the exact task, scope, acceptance contract, and required gates; and
   - the instruction to stage the complete candidate, report `git write-tree`,
     and stop without committing, pushing, or opening a PR.

   Use a task-based title, never a vendor name. `sys_session_send` has no
   workspace binding: the child is persisted with `workspace=None` and starts
   in the runner root. Isolation exists only because the dispatch identifies the
   absolute worktree and the worker obeys it. Record every returned
   `conversation_id`. Emit the whole parallel-safe dispatch set in the same turn
   as its announcement, then end the turn; do not poll.

4. Collect results through the inbox. An empty, unclear, or merely announced
   result requires `sys_session_get_history`; it is not progress to relay. Before
   accepting a candidate, verify:

   - the task branch is the recorded branch and HEAD still equals `base_oid`;
   - `git write-tree` equals the reported candidate and differs from the seed;
   - `git diff --quiet` succeeds and no nonignored untracked files exist; and
   - the runner-root branch, HEAD, index, status, and dirty-path hashes still
     match their baselines.

   A mismatch is a hard stop, not permission to reset or clean. Record the
   verified candidate tree.

5. Run `cross-review` on every candidate with the recorded mode, worktree,
   `base_oid`, `seed_tree`, and candidate tree:

   - `separate` uses `review_phase=release`;
   - `integrate` uses `review_phase=checkpoint`.

   Route `BLOCKING` and `CLEANUP` findings to the same fixer and re-review every
   new tree.

6. Complete the selected publication mode:

   - `separate`: after a clean release review, authorize the implementer to
     create the single reviewed initial commit, push, and open its PR. Verify
     `HEAD^{tree}` equals the reviewed candidate, `HEAD^` equals the base,
     `git rev-list --count <base_oid>..HEAD` is one, the worktree is clean, and
     the remote branch equals HEAD. Service the review bot per `cross-review`;
     fixes are additional reviewed commits and plain pushes, never amendments or
     force pushes.
   - `integrate`: do not commit, push, or open a PR from the scratch branch.
     Return the reviewed seed/candidate pair and worktree to `worktree-routing`
     for sequential promotion. Retain the scratch worktree until promotion and
     the aggregate release review are clean.

7. Remove a `separate` worktree only after its review bot establishes a clean
   verdict for the current HEAD and no fix task remains. Remove an `integrate`
   worktree only after its result is promoted and the aggregate release review
   is clean. Molly never merges.

## Notes

- Respect the per-turn dispatch cap yourself; dispatch larger sets in waves.
- The human can inspect sub-agent conversations in the Subagents panel. Only
  changes in Molly's own worktree appear in its Files panel.
- Cancel a wrong, runaway, or superseded worker before dispatching a replacement.
  Do not repeatedly re-prompt a dark or failing worker; use a fresh session and
  clean worktree or escalate to the human.
- `separate` scopes should remain disjoint because cross-PR conflicts surface
  when the human merges. `integrate` may discover conflicts during promotion;
  delegate those to an integration implementer.
