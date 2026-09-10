# Database pre-ping skip gate

An opt-in, PostgreSQL-only optimization that skips the network round-trip
`pool_pre_ping` normally pays on every pooled-connection checkout, for
checkouts recent enough that a fresh liveness check is unlikely to be worth
the cost. Implemented in `omnigent/db/ping_gate.py`, wired into engine
creation in `omnigent/db/utils.py::_create_engine`.

## Why

`pool_pre_ping=True` (`omnigent/db/utils.py`) verifies a pooled connection is
alive before handing it to a caller — a `SELECT 1`-shaped round-trip on every
non-fresh checkout. A single Omnigent request typically opens/commits several
independent managed sessions (`make_managed_session_maker`, one per store
call), each of which pays this round-trip even though the connections were
all in active use milliseconds apart. The gate cuts that redundant
round-trip without disabling liveness checking altogether.

## Mechanism

`pool_pre_ping` stays on. The gate wraps the per-engine dialect *instance*'s
`do_ping` (never the class, never a checkout listener) so that a checkout
whose connection was last returned to the pool within the configured window
skips the real ping and returns `True` immediately — zero I/O. A checkout at
or past the window, or with missing/inconsistent internal state, always
delegates to the real `do_ping`. A disconnect it reports by *raising* still
flows through SQLAlchemy's own classification and
`handle_error(is_pre_ping=True)` path completely unmodified; a disconnect it
reports by *returning `False`* bypasses `handle_error` entirely (native
SQLAlchemy behavior, not this gate's choice), so the gate observes and
records that case directly instead. The gate never classifies a raised
exception itself and never retries a statement.

## The trade-off: a bounded stale window, not eliminated risk

This is the trade-off to understand before enabling it: a connection that
dies while inside the skip window is not caught until the next statement
actually runs on it. That statement fails — once — surfaced through the
normal managed-session rollback+re-raise path (`make_managed_session_maker`
in `omnigent/db/utils.py`), unchanged by this feature. The connection is then
invalidated and replaced transparently for the next checkout, the same as it
always would be. This feature adds **no statement-level retry**, deliberately: retrying a
statement whose transaction state is unknown is a correctness decision that
belongs to the caller, not to a connection-pool optimisation. A caller that
hits this window sees one failed request.

This matters most for write paths that assume a connection survives a whole
multi-statement transaction, such as `conversation_store`'s
`BEGIN IMMEDIATE` / `SELECT ... FOR UPDATE` sections
(`omnigent/stores/conversation_store/sqlalchemy_store.py`) — a mid-transaction
disconnect there fails the same way it would without this gate, just
(rarely) via a connection that looked fresher than it was.

## Configuration

`OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS` — unset or `0` (the default) leaves
engine construction byte-identical to today: `pool_pre_ping` behaves exactly
as it always has, no `pool_use_lifo` is set, nothing else changes. A positive
value enables the gate for PostgreSQL engines only; SQLite and Cloudflare D1
engines silently ignore it (debug log only, never a startup failure — the
engine cache is keyed by URI, so a process can hold both a SQLite and a
Postgres engine at once). A malformed value (negative, NaN, infinite, or
non-numeric) fails engine construction immediately, for any backend.

**Suggested starting value: `3.0` seconds.** The premise is measured: a
statement-level trace of one policy-engine build against PostgreSQL showed
consecutive pool checkouts 0.7-7 ms apart, so store calls within one request
sit far inside a 3-second window.

How much of the *available* benefit that captures is a hypothesis, not a
measurement. The skip rate in production is set by the idle gap between
requests on a given connection, which has not been measured here — only the
within-request gap has. A benchmark at a 30-second window skipped 100% of
pre-ping round trips (20 per build on an unoptimised builder, 2 on the
current one), but no window-size sweep was run, so `3.0` is a
staleness-conservative starting point to tune against real traffic rather
than a tuned optimum.

Note also that the wall-clock win was **not** observable over loopback: the
round trips vanish from the counters, but elapsed time moved within
measurement noise. The gate pays off in proportion to network round-trip
time, so evaluate it on a deployment with real network latency between the
app and the database, not locally.

When enabled, the pool also switches to LIFO connection reuse
(`pool_use_lifo=True`) — with `pool_size=200`, the default FIFO round-robin
would spread idle gaps across the whole pool wide enough to defeat the gate
for most checkouts; LIFO keeps a hot working set the window can actually
hit. This only happens together with the gate, never independently of it.

**Restart required to change this value.** Engines are cached per URI for
the life of the process (`omnigent/db/utils.py::get_or_create_engine`); the
window is read once, at that engine's construction.

**If the configured window is at or past `pool_recycle`** (1800s for a
static deployment, 600s for Lakebase), a warning is logged, not rejected —
`pool_recycle` is already an independent hard upper bound on connection
staleness that runs *before* pre-ping ever fires, so a window past it isn't
unsafe, just probably not doing what whoever configured it intended.

## Metrics

Four instruments, emitted best-effort through the existing OpenTelemetry
seam (silently no-op if telemetry is disabled or unavailable — never blocks
engine creation):

- `omnigent.db.pool.checkout_age_seconds` — histogram of pool idle time at
  checkout: the interval since the connection's last check-in or real ping,
  not the physical age of the connection. Reused (non-fresh) checkouts only.
- `omnigent.db.pool.pre_ping_decisions_total{decision=skip|ping|fresh}` —
  ping-skip rate is `skip / (skip + ping)`.
- `omnigent.db.pool.disconnects_total{phase=post_skip|pre_ping|other}` —
  `post_skip` counts a disconnect discovered on a connection whose most
  recent decision was a skip; this is the signal any future default-on
  promotion of this gate should be evaluated against.
- `omnigent.db.pool.recoveries_total{trigger=pre_ping|post_skip|other}`

## Out of scope for this feature

Default-on promotion, deployment-config changes (Databricks/Helm/`.env`
defaults), and any form of statement-level retry are all explicitly out of
scope. This document describes the capability as shipped: opt-in, and
undocumented as a default anywhere.
