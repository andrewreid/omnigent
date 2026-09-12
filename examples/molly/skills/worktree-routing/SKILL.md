---
name: worktree-routing
description: Molly-only worktree topology and integration lifecycle. Choose shared or isolated execution, enforce one writer per worktree, seed task worktrees from staged trees without commits, and route accepted results to separate PRs or one integrated session candidate.
user-invocable: false
---

# worktree-routing — visible by default, isolated when necessary

**Audience: MOLLY ONLY.** Workers receive the selected mode and its concrete
contract in their dispatch; never point them at this file.

Load `scope` and `dispatch` before using this lifecycle.

The default is Molly's own session worktree because changes made there appear in
the supervising human's Files panel. Isolation is a safety tool, not the default.
No mode ever permits two write-capable workers in one worktree at the same time.

## Select and announce the mode

Choose and record a mode before every implementation dispatch. Tell the human which mode applies and when changes will become visible.

- `SHARED`: default for one cumulative session deliverable when implementers can
  run sequentially. Changes are visible in Molly's Files panel as they happen.
- `ISOLATED-SEPARATE`: required for concurrent implementers whose tasks publish
  as separate PRs. Task changes remain in their own worktrees and are not shown
  as parent-worktree changes.
- `ISOLATED-INTEGRATE`: required when implementers must run concurrently but the
  session publishes one combined commit and PR. Task changes become visible in
  Molly's worktree only after promotion.

A single worker may also use ISOLATED-INTEGRATE, including local-only delivery,
when isolation is needed for safety. Integration does not itself authorize a PR.
For direct prose authoring Molly holds the writer lease and obeys the same
baseline, staging and verification rules; no worker dispatch is required.

Use an isolated mode when ANY of these is true:

- two implementers must write concurrently;
- tasks publish separately;
- scopes overlap or are uncertain;
- a task may touch lockfiles, generated files, migrations, shared configuration,
  repository-wide formatting, or dependency state;
- build, install, generation, formatting, or test processes may interfere; or
- a task needs branch-level operations such as checkout, reset, or rebase.

Read-only `explore` and `search` workers may share any worktree. A reviewer may
inspect the candidate and run known non-mutating gates there only while its
writer lease is frozen; its allocated ignored review artifact is its only
permitted write there. Isolate commands that write, may write, or may interfere
with shared dependency or build state.

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
    acceptance: <criteria, checks, review decision, dispositions, target>
    review_artifact: <absolute path and SHA-256, or null>
```

Treat a missing or mismatched worktree, base, seed tree, candidate tree, or lease
as a hard stop. Never repair a mismatch with `git reset`, `git clean`, checkout,
or an inferred baseline.

## SHARED procedure

1. Require no unstaged or nonignored untracked files. Existing staged work must
   have known ownership and be explicitly accepted into this deliverable; otherwise
   isolate or clarify ownership without changing it. Record the session worktree's
   exact `base_oid`. Require HEAD to remain at that
   commit until final release. Record `git write-tree` as both `initial_tree`
   and the first `candidate_tree`.
2. Record branch, HEAD, index and status baselines. If the runner root is a
   different worktree, record its state too (hash existing dirty paths, or use a
   clean orchestration checkout). Acquire the session writer lease for exactly one implementer conversation.
   Do not dispatch another writer or direct-author prose while it is held.
3. Load `dispatch`. Dispatch `WORKTREE MODE: SHARED`, the absolute session worktree, `base_oid`,
   and `EXPECTED SEED TREE: <candidate_tree>`. State that existing staged changes
   are accepted session work and must be preserved.
4. The worker verifies `git write-tree` equals the seed before editing, changes
   only its assigned scope, runs the gates, stages the complete cumulative
   candidate, reports the new tree, and stops without committing.
5. Verify HEAD and any separate runner-root baselines are unchanged, no unstaged or
   nonignored untracked files remain, and the reported tree equals
   `git write-tree`.
6. Freeze the lease for `verify`, recording the previous seed and new candidate.
   Run independent review only when selected by the contract/risk. No writer may
   run while verification reads its candidate.
7. Route required fixes through `remediate` under the exclusive lease; recheck
   the new tree and affected criteria.
8. Record accepted evidence, release the lease, and dispatch the next sequential
   task from that tree.
9. Assess the aggregate deliverable and interactions with `verify`. Use `publish`
   only for a requested commit/PR endpoint; no automatic second full review.

## ISOLATED-SEPARATE procedure

Use `fanout` with `publication_mode=separate`. Each task gets a clean worktree,
its recorded base commit's tree as `seed_tree`, release acceptance, one initial
commit, and its own PR. Do not copy its files into Molly's worktree merely for
visibility; that would create a second source of truth.

## ISOLATED-INTEGRATE procedure

1. Freeze Molly's current accepted tree as the fanout `seed_tree`.
2. Use `fanout` with `publication_mode=integrate`. Create a clean worktree for
   every concurrent task at the recorded base. Materialize the seed exactly with
   `git read-tree --reset -u <seed_tree>` before dispatch; this is deterministic
   Git plumbing, not permission to resolve or author code.
3. Each task receives contract-selected verification. Never commit or push a scratch branch.
4. Before any materialization, require the recorded HEAD/index and no unstaged
   changes or nonignored untracked files, with the exclusive lease held. A matching
   index alone cannot establish that overwriting working files is safe.
   Promote accepted tasks into Molly's worktree one at a time under its writer
   lease. If Molly's current tree still equals the task seed, materialize the
   accepted task tree exactly with `git read-tree --reset -u <candidate_tree>`.
   Otherwise dispatch an integration implementer in `SHARED` mode, seeded from
   Molly's current candidate, to apply the narrow task `seed_tree ->
   candidate_tree` delta and resolve any conflicts. Molly never resolves source
   conflicts.
5. Check each promotion for integration failures. Task-level acceptance does
   not prove interactions; select additional review based on integration risk.
6. After all promotions, verify aggregate acceptance with `initial_tree` as seed.
   Use publish for the requested endpoint; never publish scratch branches.
7. Keep scratch worktrees until promotion and aggregate acceptance are complete;
   then remove them without force, preserving any unexpected work.

## Hard stops

- Never run two implementers in one worktree, even for disjoint source files.
- Never let an implementer write while a reviewer is judging that worktree.
- Never transfer a review verdict between tree OIDs.
- Never publish an `ISOLATED-INTEGRATE` scratch branch.
- Never describe isolated changes as visible in Molly's Files panel before
  promotion.
- If unexpected edits appear, freeze all writers, preserve the evidence, and ask
  the human rather than guessing ownership.
