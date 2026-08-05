---
name: worktree-routing
description: Molly-only worktree topology and integration lifecycle. Choose shared or isolated execution, enforce one writer per worktree, seed task worktrees from staged trees without commits, and route reviewed results to separate PRs or one integrated session candidate.
user-invocable: false
---

# worktree-routing — visible by default, isolated when necessary

**Audience: MOLLY ONLY.** Workers receive the selected mode and its concrete
contract in their dispatch; never point them at this file.

The default is Molly's own session worktree because changes made there appear in
the supervising human's Files panel. Isolation is a safety tool, not the default.
No mode ever permits two write-capable workers in one worktree at the same time.

## Select and announce the mode

Choose and record a mode before every implementation dispatch. Tell the human at
the plan gate which mode applies and when changes will become visible.

- `SHARED`: default for one cumulative session deliverable when implementers can
  run sequentially. Changes are visible in Molly's Files panel as they happen.
- `ISOLATED-SEPARATE`: required for concurrent implementers whose tasks publish
  as separate PRs. Task changes remain in their own worktrees and are not shown
  as parent-worktree changes.
- `ISOLATED-INTEGRATE`: required when implementers must run concurrently but the
  session publishes one combined commit and PR. Task changes become visible in
  Molly's worktree only after promotion.

Use an isolated mode when ANY of these is true:

- two implementers must write concurrently;
- tasks publish separately;
- scopes overlap or are uncertain;
- a task may touch lockfiles, generated files, migrations, shared configuration,
  repository-wide formatting, or dependency state;
- build, install, generation, formatting, or test processes may interfere; or
- a task needs branch-level operations such as checkout, reset, or rebase.

Read-only `explore` and `search` workers may share any worktree. A reviewer may
read the candidate worktree only while its writer lease is frozen.

## Registry contract

Record at least:

```yaml
session:
  worktree: <absolute path>
  base_oid: <commit>
  initial_tree: <tree>
  candidate_tree: <tree>
  writer_lease: <conversation id or null>
tasks:
  <task_id>:
    mode: SHARED | ISOLATED-SEPARATE | ISOLATED-INTEGRATE
    worktree: <absolute path>
    branch: <branch>
    seed_tree: <tree>
    candidate_tree: <tree or null>
    review_phase: checkpoint | release | fix-push
```

Treat a missing or mismatched worktree, base, seed tree, candidate tree, or lease
as a hard stop. Never repair a mismatch with `git reset`, `git clean`, checkout,
or an inferred baseline.

## SHARED procedure

1. Record the session worktree's exact `base_oid`. Require HEAD to remain at that
   commit until final release. Record `git write-tree` as both `initial_tree`
   and the first `candidate_tree`.
2. Acquire the session writer lease for exactly one implementer conversation.
   Do not dispatch another writer or direct-author prose while it is held.
3. Dispatch `WORKTREE MODE: SHARED`, the absolute session worktree, `base_oid`,
   and `EXPECTED SEED TREE: <candidate_tree>`. State that existing staged changes
   are accepted session work and must be preserved.
4. The worker verifies `git write-tree` equals the seed before editing, changes
   only its assigned scope, runs the gates, stages the complete cumulative
   candidate, reports the new tree, and stops without committing.
5. Verify HEAD and the runner root baselines are unchanged, no unstaged or
   nonignored untracked files remain, and the reported tree equals
   `git write-tree`.
6. Freeze the lease in review state. Run `cross-review` with
   `review_phase=checkpoint`, `seed_tree=<previous tree>`, and the new candidate.
   No writer may run until the report is accepted.
7. Route fixes to the same implementer under the same exclusive lease. Every fix
   creates and reviews a new tree.
8. On a clean checkpoint, record the accepted candidate, release the lease, and
   dispatch the next sequential task from that tree.
9. After all tasks, run aggregate gates and `cross-review` with
   `review_phase=release` and `seed_tree=<initial_tree>`. Only that clean release
   review may create the one session commit and PR.

## ISOLATED-SEPARATE procedure

Use `fanout` with `publication_mode=separate`. Each task gets a clean worktree,
its recorded base commit's tree as `seed_tree`, a release review, one initial
commit, and its own PR. Do not copy its files into Molly's worktree merely for
visibility; that would create a second source of truth.

## ISOLATED-INTEGRATE procedure

1. Freeze Molly's current accepted tree as the fanout `seed_tree`.
2. Use `fanout` with `publication_mode=integrate`. Create a clean worktree for
   every concurrent task at the recorded base. Materialize the seed exactly with
   `git read-tree --reset -u <seed_tree>` before dispatch; this is deterministic
   Git plumbing, not permission to resolve or author code.
3. Each task receives a checkpoint review. Never commit or push a scratch branch.
4. Promote reviewed tasks into Molly's worktree one at a time under its writer
   lease. If Molly's current tree still equals the task seed, materialize the
   reviewed task tree exactly with `git read-tree --reset -u <candidate_tree>`.
   Otherwise dispatch an integration implementer in `SHARED` mode, seeded from
   Molly's current candidate, to apply the narrow task `seed_tree ->
   candidate_tree` delta and resolve any conflicts. Molly never resolves source
   conflicts.
5. Run gates and a checkpoint review on every promoted cumulative tree. A clean
   task-level review does not approve interactions introduced during promotion.
6. After all promotions, run aggregate gates and a release review in Molly's
   worktree with its `initial_tree` as the seed, then create the one session
   commit and PR.
7. Keep scratch worktrees until their changes are promoted and the aggregate
   release review is clean; then remove them without publishing their branches.

## Hard stops

- Never run two implementers in one worktree, even for disjoint source files.
- Never let an implementer write while a reviewer is judging that worktree.
- Never transfer a review verdict between tree OIDs.
- Never publish an `ISOLATED-INTEGRATE` scratch branch.
- Never describe isolated changes as visible in Molly's Files panel before
  promotion.
- If unexpected edits appear, freeze all writers, preserve the evidence, and ask
  the human rather than guessing ownership.
