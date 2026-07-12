# codegraph for delegated exploration — leverage it without multiplying resources

`codegraph` is an MCP code-graph server: it indexes a repo and answers "what is
this symbol / who calls it / what does this change impact" from a graph instead
of from raw grep. A sub-agent that has codegraph wired can explore a large
codebase far more cheaply than one paging files by hand. The risk it introduces
is **resource multiplication** — one background daemon (and one on-disk index)
per place it runs. This doc is how Polly gets the exploration leverage while
bounding the daemon/index footprint.

> **Grounded against `codegraph` v1.0.1** (`codegraph --version`). The lifecycle
> facts and kill commands below were read from that CLI and confirmed against a
> live daemon on this machine. If you are on a different major version, re-confirm
> the marked commands with `codegraph --help` / `codegraph daemon --help`.

## How the daemon actually works (the fact that makes sharing free)
- **ONE detached daemon per project ROOT.** A client (the MCP server, or a bare
  `codegraph explore` / `codegraph node`) lazily spawns a single background
  daemon for the repo root it runs in. Process signature: `codegraph serve
  --mcp`. It listens on a Unix socket at `<root>/.codegraph/daemon.sock` and
  records its pid in `<root>/.codegraph/daemon.log`
  (`Listening on <…>/daemon.sock (pid <N>, v1.0.1). Idle timeout 300000ms.`).
- **N concurrent clients share that ONE daemon over the socket.** The daemon
  tracks a live client count (its shutdown log reads
  `Shutting down (… clients=<N>)`). So an implementer AND its same-branch
  reviewer working in the SAME worktree connect to the SAME daemon and the SAME
  index automatically — no second daemon, no second index build. This is the win:
  co-locating the implement + review pair in one worktree makes them share for
  free.
- **The index is on disk at `<root>/.codegraph/codegraph.db`** (hundreds of MB
  for a large repo). The daemon is the RAM cost; the `.db` is the disk cost. They
  have independent lifetimes — killing the daemon frees the RAM and leaves the
  `.db` on disk for the next spawn to reuse.
- **Idle self-shutdown backstop: 300000 ms (5 min).** An idle daemon reaps itself
  after 5 minutes with no client activity (`Shutting down (inactivity
  backstop…)`). This is a safety net, NOT a substitute for explicit teardown — a
  daemon that stays warm under a slow-drip of activity never hits it.

## The floor: one daemon + one index per ACTIVE worktree (and why not fewer)
The irreducible unit is **one daemon + one index per active worktree**, because
the index is **per-branch**. Each worktree is a different branch/working tree, so
its graph is different. Do NOT try to point multiple worktrees at one shared
cross-worktree index to "save resources": a shared index returns results for the
WRONG branch (symbols that moved, callers that changed, code that only exists on
another branch), which is worse than no index. Per-worktree isolation is the
whole point of the worktree model — respect it for the index too.

So the floor is 1 per active worktree; the leverage is that everyone IN that
worktree (implementer + same-branch reviewer) shares it.

## Bounding the count (Polly's job)
The floor is per-worktree; Polly keeps the TOTAL sane:
- **Index only worktrees whose task benefits.** Heavy implement / explore work on
  a large codebase justifies an index. A one-file doc edit, a trivial config
  tweak, or a task the worker can do from a known path does NOT — skip codegraph
  there and let the worker use plain tools. Don't index reflexively.
- **Reuse worktrees across parcels to amortize the build.** The first index build
  is the expensive step; a warm `.db` makes subsequent explores cheap. When a
  series of parcels touches the same subsystem, run them through the SAME reused
  worktree so the index build is paid once, not once per parcel.

## Kill / cleanup — the memory-risk mitigation
An un-reaped daemon is a resident process holding its index in RAM. Three
mechanisms, in order of how routinely they run:

### (i) Teardown-on-complete (the routine path)
When a worktree's parcel finishes, kill THAT worktree's daemon. Killing the
daemon is mandatory (it is the RAM cost); keeping vs dropping the on-disk `.db`
is a CHOICE — keep it if the same worktree will be reused for the next parcel
(warm index), drop it if the worktree is being torn down for good.

Target the ONE daemon for a given worktree root by cwd (each daemon's cwd is its
project root — grounded via `/proc/<pid>/cwd`):

```bash
# kill only the daemon serving <root>; leave every other worktree's daemon alone
ROOT=/abs/path/to/worktree
for p in $(pgrep -f 'codegraph serve --mcp'); do
  [ "$(readlink /proc/$p/cwd 2>/dev/null)" = "$ROOT" ] && kill "$p"
done
```

Drop the on-disk index too (only when retiring the worktree):

```bash
codegraph uninit "$ROOT"   # deletes <root>/.codegraph/  — CONFIRM subcommand name against codegraph vX
```

### (ii) Orphan-reaper (the sweep)
Periodically enumerate every running daemon, cross-check against Polly's
active-worktree registry, and kill any whose worktree is gone, idle, or stale:

```bash
for p in $(pgrep -f 'codegraph serve --mcp'); do
  cwd=$(readlink /proc/$p/cwd 2>/dev/null)
  # kill if: cwd no longer exists, OR cwd is not in Polly's active-worktree set
  if [ -z "$cwd" ] || [ ! -d "$cwd" ] || ! polly_worktree_is_active "$cwd"; then
    kill "$p"
  fi
done
```

`readlink /proc/<pid>/cwd` is the authoritative daemon→worktree map for a LIVE
daemon — prefer it over the pid in `daemon.log`, which can be stale (a
self-reaped daemon leaves its last pid in the log). The 5-minute idle backstop
means a truly-forgotten daemon eventually self-reaps, but the sweep frees RAM
immediately instead of waiting.

### (iii) Concurrent-daemon budget
Cap the number of simultaneous daemons. Before indexing another worktree, if the
cap is already hit, reap the LEAST-RECENTLY-USED idle daemon first (by (i)'s
cwd-targeted kill), then index. This keeps N large indexes from co-residing in
RAM when only a few worktrees are actually hot.

## The kill mechanism — what is grounded vs what to confirm
- **Scriptable, non-interactive (used above): `pkill`/`kill` by the
  `codegraph serve --mcp` process signature, filtered to a worktree by
  `/proc/<pid>/cwd`.** This signature and the cwd map were confirmed against a
  live v1.0.1 daemon on this machine — prefer this for automation.
- **Interactive: `codegraph daemon` (alias `codegraph daemons`)** — a picker that
  lists running daemons and stops the selected one on Enter. Grounded, but it is
  a TUI prompt, so it is for a human at a terminal, not for Polly's automated
  sweep.
- **On-disk index removal: `codegraph uninit <path>`** deletes the worktree's
  `.codegraph/`. Confirm the exact subcommand against your installed version
  before scripting a destructive delete — marked `CONFIRM against codegraph vX`
  above.

If a future codegraph version ships a first-class `codegraph daemon stop
<root>` / `--pidfile` interface, prefer that over `pkill`-by-signature; until
then the cwd-filtered signal is the reliable per-worktree kill.
