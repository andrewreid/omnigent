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
- **It records its pid in TWO state files — when it registers at all.** Grounded
  against a live 1.0.1 daemon started with `--path`:
  - a GLOBAL registry entry `~/.codegraph/daemons/<hash>.json`, where `<hash>` is
    the first 16 hex chars of `sha256(<root>)` (verified: `printf %s "<root>" |
    sha256sum | cut -c1-16` reproduces the filename). It carries the ROOT:
    `{"root":"<root>","pid":<N>,"version":"1.0.1","socketPath":"/tmp/codegraph-<hash>.sock","startedAt":<epochMs>}`.
    This is the authoritative root→pid map ACROSS all worktrees.
  - a LOCAL mirror `<root>/.codegraph/daemon.pid` (same JSON minus `root`), plus
    the pid echoed to `<root>/.codegraph/daemon.log`
    (`Listening on /tmp/codegraph-<hash>.sock (pid <N>, v1.0.1). Idle timeout 300000ms.`).
  The socket is a HASHED path under `/tmp`, NOT `<root>/.codegraph/daemon.sock`.
  On a clean shutdown (SIGTERM / picker-stop) the daemon REMOVES both the
  registry entry AND the local `daemon.pid` (verified).
- **CRITICAL — absence of a pidfile does NOT mean no daemon.** A daemon started
  WITHOUT `--path` (verified live: a `…/node … codegraph.js serve --mcp` with no
  `--path`, cwd inside a real worktree, running) registers in NEITHER file and is
  therefore undiscoverable by root, AND the `codegraph daemon` picker — which
  enumerates the same registry — does not list it either (verified: picker
  reported "No CodeGraph daemons running" while such a daemon was alive). So you
  can NEVER conclude "no daemon" from a missing pidfile or an empty picker; you
  can only conclude "none that I can target." The 5-min idle self-reap is the
  only mechanism that reliably retires these.
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
An un-reaped daemon is a resident process holding its index in RAM. Cleanup is
NOT fully solvable by a script, because a live daemon may register nowhere (see
"CRITICAL" above). So the posture is:
- **PRIMARY (automatic): the 5-min idle self-reap.** Verified: an idle daemon
  retires itself after 300000 ms of no client activity. This is the ONLY
  mechanism that reliably retires EVERY daemon, including ones no pidfile or
  picker can see. Design for it: do not rely on catching every daemon by hand.
- **PRIMARY (manual): the interactive `codegraph daemon` picker.** For a human at
  a terminal, this is the first-line stop — it lists the daemons it knows about
  and stops the selected one on Enter, and it cannot self-match. Caveat (verified):
  it enumerates the SAME registry, so a daemon started without `--path`
  registers nowhere and does NOT appear — an empty picker is not proof of "no
  daemons".
- **BACKSTOP (scripted, best-effort): the hardened pidfile-kill below.** It frees
  RAM sooner than the 5-min backstop for the daemons it CAN target, but it is
  best-effort — a live daemon may have no pidfile, in which case fall back to the
  picker / self-reap and never conclude "no daemon".

### (i) Teardown-on-complete (best-effort scripted backstop)
When a worktree's parcel finishes, try to retire THAT worktree's daemon early
(freeing RAM); if it can't be targeted, let the 5-min self-reap handle it.
Keeping vs dropping the on-disk `.db` is a CHOICE — keep it if the worktree will
be reused (warm index), drop it if retiring the worktree for good.

Discover the pid from codegraph's OWN state — never a process signature. On 1.0.1
the daemon runs as the bundled `node` (`/proc/<pid>/exe` → `…/codegraph-linux-x64/node`,
`comm` = `MainThread`), NOT a process named `codegraph`, so `pgrep -x codegraph`
finds nothing and `pgrep -f 'codegraph serve --mcp'` also matches the invoking
shell and can self-terminate — do not use either.

```bash
# BEST-EFFORT: retire the codegraph daemon serving <root> early, by its recorded pid.
# A missing pidfile does NOT mean "no daemon" — it may be unregistered; fall back
# to the picker / 5-min self-reap and do not conclude absence.
ROOT=/abs/path/to/worktree

# 1) discover pid: prefer the GLOBAL registry (keyed by sha256(root)), then the local mirror
HASH=$(printf '%s' "$ROOT" | sha256sum | cut -c1-16)
PID=""
for f in "$HOME/.codegraph/daemons/$HASH.json" "$ROOT/.codegraph/daemon.pid"; do
  [ -f "$f" ] || continue
  PID=$(sed -n 's/.*"pid" *: *\([0-9]\{1,\}\).*/\1/p' "$f")   # or: jq -r .pid "$f"
  [ -n "$PID" ] && break
done
[ -n "$PID" ] || { echo "no pidfile for $ROOT — may still be a live UNREGISTERED daemon; use the picker or let the 5-min self-reap retire it"; exit 0; }
kill -0 "$PID" 2>/dev/null || { echo "recorded pid dead — stale pidfile"; exit 0; }

# 2) HARDENED validation before signalling — must be the node-hosted daemon for THIS root.
#    Defeats self-kill on a stale/reused pid even if that pid is the shell running
#    this snippet (whose cmdline contains the literal 'codegraph.js … --path $ROOT').
ancestors=" $$ "; q=$$
while [ "$q" -gt 1 ]; do
  q=$(awk '{print $4}' "/proc/$q/stat" 2>/dev/null); [ -n "$q" ] || break
  ancestors="$ancestors$q "
done
[ "$PID" = "$$" ] && { echo "refusing: pid is this shell"; exit 0; }
case "$ancestors" in *" $PID "*) echo "refusing: pid is an ancestor of this shell"; exit 0 ;; esac
case "$(readlink -f /proc/$PID/exe 2>/dev/null)" in */node) ;; *) echo "refusing: exe is not node"; exit 0 ;; esac
# exact NUL-delimited argv fields (NOT substring): needs codegraph.js AND serve AND --mcp AND --path==ROOT.
# A shell's argv is `sh -c <blob>` — the blob is ONE field, so it can never satisfy these exact-field tests.
awk 'BEGIN{RS="\0"} {a[NR]=$0}
     END{cj=s=m=pp=0;
         for(i=1;i<=NR;i++){ if(a[i]~/\/codegraph\.js$/)cj=1; if(a[i]=="serve")s=1;
                             if(a[i]=="--mcp")m=1; if(a[i]=="--path"&&a[i+1]==root)pp=1 }
         exit (cj&&s&&m&&pp)?0:1}' root="$ROOT" "/proc/$PID/cmdline" 2>/dev/null \
  || { echo "refusing: pid $PID argv is not the codegraph daemon for $ROOT"; exit 0; }

kill "$PID"   # SIGTERM; on clean shutdown the daemon removes BOTH its registry entry and daemon.pid
```

This backstop cannot self-terminate — it rejects `$$`, every ancestor pid, any
non-`node` exe, and any process whose EXACT argv fields are not
`… codegraph.js … serve … --mcp … --path <ROOT>` (verified: the snippet passes a
real daemon and rejects both this shell and a decoy `sh -c` whose cmdline
contains the literal script text). It is NOT a guaranteed reaper: a live daemon
with no pidfile is invisible to it — that case is the picker's / self-reap's job.

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
> self-safe). Enumerate via the state files — the global registry
> `~/.codegraph/daemons/*.json` and/or the per-root `daemon.pid` of Polly's KNOWN
> worktrees — and reuse (i)'s FULL hardened validation (self/ancestor exclusion +
> exe==node + exact `--path==ROOT` argv) for each. This sweep is best-effort like
> (i): a daemon that registered nowhere is invisible to it and is left to the
> 5-min self-reap. A sweep is more dangerous than a single teardown, not less.

Because Polly already tracks its worktree roots, iterate THOSE (or the registry
entries), and best-effort reap any whose worktree is gone or is idle-past-threshold:

```bash
# PSEUDOCODE — see the requirements box above; not safe to run verbatim.
# Best-effort: unregistered daemons won't appear here; the 5-min self-reap covers them.
for root in $(polly_known_worktree_roots); do       # Polly's registry, NOT a process scan
  hash=$(printf '%s' "$root" | sha256sum | cut -c1-16)
  pid=""
  for f in "$HOME/.codegraph/daemons/$hash.json" "$root/.codegraph/daemon.pid"; do
    [ -f "$f" ] || continue
    pid=$(sed -n 's/.*"pid" *: *\([0-9]\{1,\}\).*/\1/p' "$f"); [ -n "$pid" ] && break
  done
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || continue   # absent/dead → nothing targetable
  # kill if: root gone, OR root not in Polly's active set, OR idle past threshold
  if [ ! -d "$root" ] \
     || ! polly_worktree_is_active "$root" \
     || polly_daemon_idle_past_threshold "$pid"; then
    reap_codegraph_daemon "$pid" "$root"            # = (i)'s FULL hardened validation, then kill
  fi
done
```

The registry entry / `daemon.pid` is the authoritative daemon→worktree record
WHEN present; `/proc/<pid>/cwd` confirms it for a LIVE daemon. Prefer both over
the pid in `daemon.log`, which can be stale (a self-reaped daemon may leave its
last pid in the log). The 5-minute idle self-reap is the PRIMARY, complete
cleanup — it retires even daemons this sweep can't see; the sweep and (i)'s
teardown are best-effort backstops that free RAM sooner for the daemons they CAN
target.

### (iii) Concurrent-daemon budget

> **PSEUDOCODE — implementation requirements.** "LEAST-RECENTLY-USED" and "idle"
> presume a per-daemon last-use timestamp that Polly must MAINTAIN (codegraph
> exposes no LRU/idle-age query as of v1.0.1, `CONFIRM against codegraph 1.0.1`).
> Track last-use in Polly's registry at dispatch/reap time; do not assume the
> daemon reports it.

Cap the number of simultaneous daemons. Before indexing another worktree, if the
cap is already hit, best-effort reap the LEAST-RECENTLY-USED idle daemon first
(via (i)'s hardened recorded-pid kill), then index. This keeps N large indexes
from co-residing in RAM when only a few worktrees are actually hot. If the LRU
daemon can't be targeted (no pidfile), let its idle self-reap free the slot
rather than force a kill.

## The kill mechanism — what is grounded vs what to confirm
- **PRIMARY, automatic: the 5-min idle self-reap** (300000 ms, verified). The
  ONLY complete mechanism — it retires every daemon, including ones no pidfile or
  picker can see. Everything below reclaims RAM sooner but is best-effort.
- **PRIMARY, manual: `codegraph daemon` (alias `codegraph daemons`)** — a picker
  that lists running daemons and stops the selected one on Enter (grounded on
  1.0.1; cannot self-match). First-line for a human at a terminal. Caveat
  (verified): it enumerates the registry, so a daemon started without `--path`
  does NOT appear — an empty picker is not proof of "no daemons".
- **BACKSTOP, scripted best-effort: kill by the daemon's OWN RECORDED PID.**
  Discover it from `~/.codegraph/daemons/<sha256(root)[:16]>.json` (has `root`;
  authoritative across worktrees) or the local mirror `<root>/.codegraph/daemon.pid`.
  Before signalling, apply the FULL hardening: pid ≠ `$$`, pid ∉ ancestor chain,
  `/proc/<pid>/exe` basename == `node`, and EXACT NUL-argv fields
  `codegraph.js` + `serve` + `--mcp` + `--path <root>` (not substring). NEVER a
  `pkill`/`pgrep` signature: on 1.0.1 the daemon runs as the bundled `node`
  (`/proc/<pid>/exe` → `…/codegraph-linux-x64/node`, never `codegraph`), so
  `pgrep -x codegraph` finds nothing, `*/codegraph` exe-guards reject every match,
  and `pgrep -f 'codegraph serve --mcp'` also matches the invoking shell and can
  self-terminate. **A missing pidfile does NOT mean "no daemon"** (verified: a
  live `--path`-less daemon registers nowhere) — fall back to picker / self-reap;
  never conclude absence.
  Grounded on this machine: the registry + local pidfile paths/format, the
  `sha256(root)` hash, the `node` exe, that SIGTERM removes both files, and that
  the hardened snippet passes a real daemon while rejecting this shell and a decoy
  `sh -c`.
- **On-disk index removal: `codegraph uninit <path>`** deletes the worktree's
  `.codegraph/`. Confirm the exact subcommand against your installed version
  before scripting a destructive delete — marked `CONFIRM against codegraph 1.0.1`
  above.

If a future codegraph version ships a first-class non-interactive
`codegraph daemon stop <root>` / `--pidfile` interface, prefer that. Until then
there is NO guaranteed automated per-worktree kill — the self-reap is the only
complete reaper, the picker is the manual first line, and the hardened
recorded-pid kill is a best-effort backstop for the daemons it can target.
