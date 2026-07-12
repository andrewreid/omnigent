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
  `Shutting down (… clients=<N>)`). So multiple agents legitimately working in
  the SAME implementer worktree (e.g. an implementer plus a helper explore
  sub-agent it spawned in-tree) connect to the SAME daemon and the SAME index
  automatically — no second daemon, no second index build. This is the win: any
  agents that share one worktree share its daemon for free. NOTE: the isolated
  cross-review reviewer is NOT one of them — it never enters the implementer
  worktree and runs no codegraph (see below).
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

So the floor is 1 per active worktree; the leverage is that every agent
legitimately IN that worktree shares it.

**Cross-review isolation overrides sharing.** The `cross-review` reviewer is a
DIFFERENT-vendor sub-agent that gets the diff, the acceptance contract, and any
needed adjacency as TEXT — it NEVER accesses the implementer's worktree, so it
never touches that worktree's daemon and runs NO codegraph of its own. Sharing
is only for agents that legitimately live in the SAME implementer worktree; the
isolated reviewer is not one of them. Do not stand up a daemon "for the
reviewer" — there is no reviewer worktree to index.

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
project root — grounded via `/proc/<pid>/cwd`).

> **DANGER — a bare `pgrep -f 'codegraph serve --mcp'` can kill its OWN shell.**
> `pgrep -f` matches the FULL command line, so the shell that is running this
> teardown (its argv literally contains the string `codegraph serve --mcp`),
> the `pgrep` itself, and any editor/pager showing this doc ALL match — and the
> invoking shell's cwd is usually `$ROOT` too, so the cwd filter does not save
> you. NEVER kill on the `-f` pattern alone. A candidate is a real daemon ONLY
> if `/proc/<pid>/exe` resolves to the codegraph binary, and you must EXCLUDE
> the current shell pid and its ancestors.

**Prefer codegraph's own stop path when a human is at the terminal.** v1.0.1
ships an interactive picker — `codegraph daemon` (alias `codegraph daemons`) —
that lists running daemons and stops the selected one on Enter; it cannot
self-match. Use it for hand-driven teardown. `CONFIRM against codegraph 1.0.1`:
the picker exists, but a non-interactive `codegraph daemon stop <root>` form is
NOT verified on this version — do not script it until confirmed.

For automated (non-interactive) teardown, use an exe-verified, self-excluding
kill — it cannot terminate the invoking shell because a shell's `/proc/<pid>/exe`
is the shell binary, never codegraph, and self+ancestors are excluded outright:

```bash
# kill only the codegraph DAEMON serving <root>; never the caller or a look-alike
ROOT=/abs/path/to/worktree

# build the exclusion set: this shell + every ancestor pid (walk the ppid chain)
exclude=" $$ "
p=$$
while [ "$p" -gt 1 ]; do
  ppid=$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null) || break
  [ -n "$ppid" ] || break
  exclude="$exclude$ppid "
  p=$ppid
done

for pid in $(pgrep -x codegraph); do          # -x: exact argv0 == "codegraph", not a substring match
  case "$exclude" in *" $pid "*) continue ;; esac          # never signal self or an ancestor
  exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null)
  case "$exe" in */codegraph) ;; *) continue ;; esac       # must resolve to the codegraph binary
  grep -qz -- 'serve' "/proc/$pid/cmdline" 2>/dev/null || continue   # a serving daemon, not a one-shot client
  [ "$(readlink /proc/$pid/cwd 2>/dev/null)" = "$ROOT" ] && kill "$pid"
done
```

If `pgrep -x codegraph` misses the daemon on your install (some builds exec it
as `node`/another argv0), fall back to `pgrep -f 'codegraph serve --mcp'` but
KEEP every guard above — the `/proc/<pid>/exe` check and the self+ancestor
exclusion are what make it safe, not the pattern.

Drop the on-disk index too (only when retiring the worktree):

```bash
codegraph uninit "$ROOT"   # deletes <root>/.codegraph/  — CONFIRM subcommand name against codegraph 1.0.1
```

### (ii) Orphan-reaper (the sweep)

> **PSEUDOCODE — implementation requirements, NOT a runnable command.** The
> block below is a specification of what the sweep must do; it references
> primitives Polly has to SUPPLY. Do not paste it as-is: `polly_worktree_is_active`
> is not a real command (it stands for a lookup against Polly's own
> active-worktree registry), and "idle / stale" needs an idleness source Polly
> must define (e.g. the daemon's own last-activity from `daemon.log`, or an
> mtime/registry timestamp — codegraph exposes no idle-age query as of v1.0.1,
> `CONFIRM against codegraph 1.0.1`). Any real implementation MUST also carry the
> exe-verified, self+ancestor-excluding guards from (i) — a sweep is more
> dangerous than a single teardown, not less.

Periodically enumerate every running daemon, cross-check against Polly's
active-worktree registry, and kill any whose worktree is gone or is no longer
active/idle-past-threshold:

```bash
# PSEUDOCODE — see the requirements box above; not safe to run verbatim.
# Reuse (i)'s $exclude set and its exe/argv0 guards for every candidate pid.
for p in $(list_codegraph_daemon_pids); do          # (i)'s guarded enumeration, NOT bare pgrep -f
  cwd=$(readlink /proc/$p/cwd 2>/dev/null)
  # kill if: cwd no longer exists, OR cwd is not in Polly's active-worktree set,
  #          OR the daemon has been idle past Polly's threshold (idleness source TBD)
  if [ -z "$cwd" ] || [ ! -d "$cwd" ] \
     || ! polly_worktree_is_active "$cwd" \
     || polly_daemon_idle_past_threshold "$p"; then
    kill "$p"                                         # only after (i)'s exe + self/ancestor guards pass
  fi
done
```

`readlink /proc/<pid>/cwd` is the authoritative daemon→worktree map for a LIVE
daemon — prefer it over the pid in `daemon.log`, which can be stale (a
self-reaped daemon leaves its last pid in the log). The 5-minute idle backstop
means a truly-forgotten daemon eventually self-reaps, but the sweep frees RAM
immediately instead of waiting.

### (iii) Concurrent-daemon budget

> **PSEUDOCODE — implementation requirements.** "LEAST-RECENTLY-USED" and "idle"
> presume a per-daemon last-use timestamp that Polly must MAINTAIN (codegraph
> exposes no LRU/idle-age query as of v1.0.1, `CONFIRM against codegraph 1.0.1`).
> Track last-use in Polly's registry at dispatch/reap time; do not assume the
> daemon reports it.

Cap the number of simultaneous daemons. Before indexing another worktree, if the
cap is already hit, reap the LEAST-RECENTLY-USED idle daemon first (by (i)'s
guarded cwd-targeted kill), then index. This keeps N large indexes from
co-residing in RAM when only a few worktrees are actually hot.

## The kill mechanism — what is grounded vs what to confirm
- **Scriptable, non-interactive (used above): exe-verified, self-excluding
  `kill`.** A candidate pid is a real daemon ONLY if `/proc/<pid>/exe` resolves
  to the codegraph binary; the current shell pid and its ancestors are excluded;
  and the worktree is matched by `/proc/<pid>/cwd`. NEVER a bare
  `pkill`/`pgrep -f 'codegraph serve --mcp'` — that pattern matches the invoking
  shell (whose cmdline contains the literal) and can self-terminate. The exe
  path and the cwd map were confirmed against a live v1.0.1 daemon on this
  machine; the self/ancestor exclusion is what makes automating it safe.
- **Interactive: `codegraph daemon` (alias `codegraph daemons`)** — a picker that
  lists running daemons and stops the selected one on Enter. Grounded on v1.0.1,
  and it cannot self-match — PREFER it for hand-driven teardown. It is a TUI
  prompt, so it is for a human at a terminal, not for Polly's automated sweep.
- **On-disk index removal: `codegraph uninit <path>`** deletes the worktree's
  `.codegraph/`. Confirm the exact subcommand against your installed version
  before scripting a destructive delete — marked `CONFIRM against codegraph 1.0.1`
  above.

If a future codegraph version ships a first-class non-interactive
`codegraph daemon stop <root>` / `--pidfile` interface, prefer that over the
signal-by-pid path; until then the exe-verified, self-excluding, cwd-filtered
`kill` is the reliable per-worktree automated kill, and the interactive picker
is the reliable hand-driven one.
