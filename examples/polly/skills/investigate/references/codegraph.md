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
  daemon for the repo root it runs in. On 1.0.1 the daemon runs as the bundled
  `node` binary (`comm` = `MainThread`, `/proc/<pid>/exe` → `…/codegraph-linux-x64/node`),
  with the codegraph entrypoint in its argv:
  `…/node … codegraph.js serve --mcp --path <root>`. It does NOT exec as a
  process literally named `codegraph`.
- **It records its own pid in a state file** at `<root>/.codegraph/daemon.pid`
  (grounded against a live 1.0.1 daemon) — JSON:
  `{"pid":<N>,"version":"1.0.1","socketPath":"/tmp/codegraph-<hash>.sock","startedAt":<epochMs>}`.
  The same pid is echoed to `<root>/.codegraph/daemon.log`
  (`Listening on /tmp/codegraph-<hash>.sock (pid <N>, v1.0.1). Idle timeout 300000ms.`).
  Note the socket is a HASHED path under `/tmp`, not `<root>/.codegraph/daemon.sock`.
  On a clean shutdown (SIGTERM / picker-stop) the daemon REMOVES `daemon.pid`
  (verified) — so a present `daemon.pid` whose pid is alive means a live daemon,
  and its absence means none.
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

Target the ONE daemon for a given worktree root by its OWN RECORDED PID, read
from `<root>/.codegraph/daemon.pid`. Do NOT match process signatures — see below.

> **Why not `pgrep`/`pkill` on a signature (the trap this replaced).** Two
> ways it fails on real 1.0.1:
> 1. **It can't find the daemon.** The daemon runs as the bundled `node`
>    (`/proc/<pid>/exe` → `…/codegraph-linux-x64/node`, `comm` = `MainThread`),
>    NOT a process named `codegraph`. So `pgrep -x codegraph` matches NOTHING,
>    and an exe-guard of `*/codegraph` rejects every `pgrep -f` hit — the kill is
>    a no-op against the actual daemon (verified on this machine).
> 2. **It can kill the wrong thing.** `pgrep -f 'codegraph serve --mcp'` matches
>    the FULL command line, so the shell running this teardown (its argv contains
>    that literal), the `pgrep` itself, and any editor/pager showing this doc all
>    match — and the shell's cwd is usually `$ROOT` too, so a cwd filter does not
>    save it.
>
> The recorded pid sidesteps both: it is the daemon's OWN pid (never the shell's,
> so self-kill is impossible) and it is the ACTUAL running process (so the kill
> works). Guard against a STALE pidfile (pid dead, or reused by an unrelated
> process) by verifying the pid is alive, its cwd equals `$ROOT`, and its argv
> still names the codegraph entrypoint before signalling.

**Prefer codegraph's own stop path when a human is at the terminal.** 1.0.1
ships an interactive picker — `codegraph daemon` (alias `codegraph daemons`) —
that lists running daemons and stops the selected one on Enter; it cannot
self-match. Use it for hand-driven teardown. `CONFIRM against codegraph 1.0.1`:
the picker exists (verified), but a non-interactive `codegraph daemon stop
<root>` / `--pidfile` form is NOT verified on this version — do not script it
until confirmed.

For automated (non-interactive) teardown, kill by the recorded pid:

```bash
# kill only the codegraph DAEMON serving <root>, by its OWN recorded pid.
ROOT=/abs/path/to/worktree
PIDFILE="$ROOT/.codegraph/daemon.pid"

[ -f "$PIDFILE" ] || { echo "no daemon.pid — no live daemon for $ROOT"; exit 0; }
# daemon.pid is JSON: {"pid":N,"version":"1.0.1","socketPath":"/tmp/…","startedAt":…}
PID=$(sed -n 's/.*"pid" *: *\([0-9]\{1,\}\).*/\1/p' "$PIDFILE")   # or: jq -r .pid "$PIDFILE"

[ -n "$PID" ] && kill -0 "$PID" 2>/dev/null || { echo "recorded pid dead/absent — stale pidfile"; exit 0; }
# stale-pidfile / pid-reuse guards: right worktree AND still the codegraph daemon
[ "$(readlink /proc/$PID/cwd 2>/dev/null)" = "$ROOT" ] || { echo "pid $PID cwd != $ROOT — not this daemon"; exit 0; }
grep -qa 'codegraph.js' "/proc/$PID/cmdline" 2>/dev/null || { echo "pid $PID not a codegraph process — refusing"; exit 0; }

kill "$PID"   # SIGTERM; the daemon removes its own daemon.pid on clean shutdown
```

This is self-kill-impossible (the pid comes from the daemon's own state file, it
is never `$$`) and functional (it is the live daemon's real pid). The `cwd` and
`codegraph.js` checks are the only defence against a stale pidfile whose number
has been recycled by an unrelated process — keep them.

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
> `CONFIRM against codegraph 1.0.1`). Do NOT enumerate by process signature
> (see (i) — `pgrep`/`pkill` neither finds the `node`-hosted daemon nor is
> self-safe). Enumerate via the per-root `daemon.pid` files of Polly's KNOWN
> worktrees, and reuse (i)'s recorded-pid kill (alive + cwd + `codegraph.js`
> guards) for each — a sweep is more dangerous than a single teardown, not less.

Because Polly already tracks its worktree roots, iterate THOSE, read each
`<root>/.codegraph/daemon.pid`, and kill any whose worktree is gone or is no
longer active/idle-past-threshold:

```bash
# PSEUDOCODE — see the requirements box above; not safe to run verbatim.
for root in $(polly_known_worktree_roots); do       # Polly's registry, NOT a process scan
  pidfile="$root/.codegraph/daemon.pid"
  [ -f "$pidfile" ] || continue                     # no daemon for this root
  pid=$(sed -n 's/.*"pid" *: *\([0-9]\{1,\}\).*/\1/p' "$pidfile")
  kill -0 "$pid" 2>/dev/null || continue            # dead pid / stale pidfile → nothing to reap
  # kill if: root gone, OR root not in Polly's active set, OR idle past threshold
  if [ ! -d "$root" ] \
     || ! polly_worktree_is_active "$root" \
     || polly_daemon_idle_past_threshold "$pid"; then
    # apply (i)'s stale-pid guards before signalling:
    [ "$(readlink /proc/$pid/cwd 2>/dev/null)" = "$root" ] \
      && grep -qa 'codegraph.js' "/proc/$pid/cmdline" 2>/dev/null \
      && kill "$pid"
  fi
done
```

The `daemon.pid` file is the authoritative daemon→worktree record; `/proc/<pid>/cwd`
confirms it for a LIVE daemon. Prefer both over the pid in `daemon.log`, which
can be stale (a self-reaped daemon may leave its last pid in the log). The
5-minute idle backstop means a truly-forgotten daemon eventually self-reaps on
its own — that self-reap is the PRIMARY cleanup; this sweep and (i)'s teardown
are the BACKSTOPS that free RAM immediately instead of waiting.

### (iii) Concurrent-daemon budget

> **PSEUDOCODE — implementation requirements.** "LEAST-RECENTLY-USED" and "idle"
> presume a per-daemon last-use timestamp that Polly must MAINTAIN (codegraph
> exposes no LRU/idle-age query as of v1.0.1, `CONFIRM against codegraph 1.0.1`).
> Track last-use in Polly's registry at dispatch/reap time; do not assume the
> daemon reports it.

Cap the number of simultaneous daemons. Before indexing another worktree, if the
cap is already hit, reap the LEAST-RECENTLY-USED idle daemon first (by (i)'s
recorded-pid kill on that worktree's `daemon.pid`), then index. This keeps N
large indexes from co-residing in RAM when only a few worktrees are actually hot.

## The kill mechanism — what is grounded vs what to confirm
- **Scriptable, non-interactive (used above): kill by the daemon's OWN RECORDED
  PID** from `<root>/.codegraph/daemon.pid` (JSON `{"pid":…}`), after verifying
  the pid is alive, its `/proc/<pid>/cwd` == `<root>`, and its `/proc/<pid>/cmdline`
  still names `codegraph.js`. NEVER a `pkill`/`pgrep` on a signature: on 1.0.1
  the daemon runs as the bundled `node` (`/proc/<pid>/exe` → `…/codegraph-linux-x64/node`,
  never `codegraph`), so `pgrep -x codegraph` finds nothing and an exe-guard of
  `*/codegraph` rejects every match — the signature kill is a no-op against the
  real daemon; and `pgrep -f 'codegraph serve --mcp'` additionally matches the
  invoking shell and can self-terminate. The `daemon.pid` path/format, the pid's
  cwd, the `node` exe, and that a SIGTERM removes `daemon.pid` were all confirmed
  against a live 1.0.1 daemon on this machine.
- **Interactive: `codegraph daemon` (alias `codegraph daemons`)** — a picker that
  lists running daemons and stops the selected one on Enter. Grounded on 1.0.1,
  and it cannot self-match — PREFER it for hand-driven teardown. It is a TUI
  prompt, so it is for a human at a terminal, not for Polly's automated sweep.
- **On-disk index removal: `codegraph uninit <path>`** deletes the worktree's
  `.codegraph/`. Confirm the exact subcommand against your installed version
  before scripting a destructive delete — marked `CONFIRM against codegraph 1.0.1`
  above.

The 5-minute idle self-reap is the PRIMARY cleanup; the recorded-pid kill and
the picker are backstops that reclaim RAM sooner. If a future codegraph version
ships a first-class non-interactive `codegraph daemon stop <root>` / `--pidfile`
interface, prefer that; until then the recorded-pid kill is the reliable
per-worktree automated path, and the interactive picker the reliable hand-driven
one.
