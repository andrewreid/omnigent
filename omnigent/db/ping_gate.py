"""Recency-gated pre-ping: skip ``pool_pre_ping``'s round-trip on connections
returned to the pool within a configured window.

Postgres-only, opt-in via :data:`PING_SKIP_WINDOW_ENV`. This module never
disables ``pool_pre_ping`` and never classifies a *raised* ping exception
itself (a delegated ping returning ``False`` bypasses ``handle_error``
entirely, so the wrapper does record that one — see
``wrapped_do_ping``) — it wraps
the per-engine dialect *instance*'s ``do_ping`` so that a "fresh enough"
checkout skips the network round-trip. A real ping that *raises* still flows
through SQLAlchemy's native ``_do_ping_w_event`` classification and
``handle_error`` machinery unchanged; a real ping that instead *returns*
``False`` (native pre-ping's other disconnect signal) bypasses
``handle_error`` entirely, so the wrapper observes and records that case
itself. See ``docs/db-pre-ping-gate.md`` for the design rationale and the
bounded stale-window trade-off this implies.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from sqlalchemy import event
from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine.interfaces import DBAPIConnection, ExceptionContext
    from sqlalchemy.pool import ConnectionPoolEntry

_logger = logging.getLogger(__name__)

PING_SKIP_WINDOW_ENV = "OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS"

# Attribute name used both to remember the installed gate on the engine
# (idempotency) and, indirectly, to detect the wrapped ``do_ping`` since the
# wrapper is always installed together with this attribute.
_GATE_ATTR = "_omnigent_db_pool_ping_gate"

# The sole ``record_info`` key this feature owns; survives connection
# replacement (unlike ``.info``) for the one-shot recovery marker. Repo-wide
# grep confirmed no other usage of this key.
_RECOVERY_MARKER_KEY = "omnigent.db.ping_gate.recovery_trigger"


def _report_swallowed(message: str) -> None:
    """
    Log a swallowed internal failure without ever raising.

    The sole reporting path for this module's suppression handlers. A log
    handler or formatter that raises would otherwise turn a swallowed
    bookkeeping failure back into a live exception on the connection path,
    so the report is inside the net rather than beside it.

    :param message: Static description of the swallowed failure.
    """
    with contextlib.suppress(Exception):
        _logger.debug(message, exc_info=True)


def _fail_safe_listener(method: Callable[..., Any]) -> Callable[..., Any]:
    """
    Make a pool-event listener incapable of breaking the DB path.

    Every listener wrapped by this is bookkeeping and metrics only: nothing
    it computes is load-bearing for the correctness of the checkout,
    checkin, invalidate or error path it rides on. A structural net rather
    than per-statement suppression so a state access added to one of these
    later is covered by construction. Not applied to ``wrapped_do_ping``,
    which carries its own protected regions instead: it has one statement --
    the real ping -- whose exception must propagate into SQLAlchemy's
    disconnect classification untouched, so it cannot be wrapped wholesale.
    """

    @functools.wraps(method)
    def _wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except Exception:  # noqa: BLE001 - metrics must never escape into the DB path
            _report_swallowed(f"database ping gate listener {method.__name__} failed")
            return None

    return _wrapper


Decision = str  # "fresh" | "skip" | "ping"
DisconnectPhase = str  # "post_skip" | "pre_ping" | "other"
RecoveryTrigger = str  # "pre_ping" | "post_skip" | "other"


def _parse_db_ping_skip_window_seconds() -> float:
    """
    Parse and validate :data:`PING_SKIP_WINDOW_ENV`.

    Unset, empty, or ``"0"`` (or any value that parses to zero) disables the
    gate: ``0.0`` is returned and callers must treat that as "byte-identical
    to today's engine config". Any other malformed value (negative, NaN,
    infinite, or non-numeric) raises ``ValueError`` so a misconfigured
    deployment fails at startup instead of running the feature in an
    undefined state.

    :returns: The configured window in seconds, or ``0.0`` if disabled.
    :raises ValueError: If the env var is set to a malformed value.
    """
    raw = os.environ.get(PING_SKIP_WINDOW_ENV, "")
    if raw.strip() == "":
        return 0.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{PING_SKIP_WINDOW_ENV} must be a finite number greater than or "
            f"equal to 0; got {raw!r}"
        ) from exc
    if math.isnan(value) or math.isinf(value) or value < 0:
        raise ValueError(
            f"{PING_SKIP_WINDOW_ENV} must be a finite number greater than or "
            f"equal to 0; got {raw!r}"
        )
    return 0.0 if value == 0 else value


def is_postgres_url(db_uri: str) -> bool:
    """
    Return whether ``db_uri``'s backend is PostgreSQL.

    :param db_uri: A SQLAlchemy database connection string.
    :returns: ``True`` for ``postgresql://`` / ``postgresql+<driver>://``
        URLs, ``False`` otherwise (including SQLite and Cloudflare D1).
    """
    return make_url(db_uri).get_backend_name() == "postgresql"


def warn_if_window_ge_recycle(window: float, pool_recycle: int) -> None:
    """
    Log (never raise) when the configured window is at or past ``pool_recycle``.

    Not a hard error: ``_ConnectionRecord.get_connection()`` runs its recycle
    check before pre-ping ever fires, so ``pool_recycle`` is already an
    independent hard upper bound on connection staleness regardless of how
    large this window is configured.

    :param window: The configured skip window, in seconds.
    :param pool_recycle: The engine's resolved ``pool_recycle`` value.
    """
    if window >= pool_recycle:
        # Suppressed for the same reason the listeners are: this runs on the
        # engine-creation path, so a raising log handler would turn advice
        # into a startup failure.
        with contextlib.suppress(Exception):
            _logger.warning(
                "%s=%s is greater than or equal to pool_recycle=%s; recycling "
                "remains the hard connection-age limit",
                PING_SKIP_WINDOW_ENV,
                window,
                pool_recycle,
            )


class _NullSinks:
    """No-op metric sinks; replaced in place by ``instrument_db_pool_ping_gate``."""

    def record_checkout_age(self, dialect: str, age_seconds: float) -> None:
        pass

    def inc_decision(self, dialect: str, decision: Decision) -> None:
        pass

    def inc_disconnect(self, dialect: str, phase: DisconnectPhase) -> None:
        pass

    def inc_recovery(self, dialect: str, trigger: RecoveryTrigger) -> None:
        pass


_NULL_SINKS = _NullSinks()


class _ConnState:
    """Per-physical-connection recency state, keyed by DBAPI connection."""

    __slots__ = ("last_decision", "last_success")

    def __init__(self, last_success: float, last_decision: Decision) -> None:
        self.last_success = last_success
        self.last_decision = last_decision


class PingGate:
    """
    Owns the wrapped ``do_ping`` and the bookkeeping pool/dialect listeners
    for one engine.

    Never installed directly by callers outside this module; use
    :func:`_install_db_pool_ping_gate`.
    """

    def __init__(
        self,
        *,
        dialect_name: str,
        window_seconds: float,
        clock: Callable[[], float],
        original_do_ping: Callable[[DBAPIConnection], bool],
    ) -> None:
        self.dialect_name = dialect_name
        self.window_seconds = window_seconds
        self.sinks: Any = _NULL_SINKS
        self._clock = clock
        self._original_do_ping = original_do_ping
        self._lock = threading.Lock()
        self._state: WeakKeyDictionary[Any, _ConnState] = WeakKeyDictionary()
        # Staged by handle_error (raised exceptions) or directly by
        # wrapped_do_ping (a delegated False return), consumed by the
        # invalidate listener that follows in the same call stack.
        self._pending_trigger: threading.local = threading.local()

    # ── state access (fail-safe: unhashable/non-weakref-able connections
    #    never raise back into a caller) ──────────────────────────────

    def _lookup_state(self, dbapi_connection: Any) -> _ConnState | None:
        with self._lock:
            return self._state.get(dbapi_connection)

    def _store_state(self, dbapi_connection: Any, state: _ConnState) -> None:
        with self._lock:
            self._state[dbapi_connection] = state

    def _drop_state(self, dbapi_connection: Any) -> None:
        with self._lock:
            self._state.pop(dbapi_connection, None)

    def _safe_lookup_state(self, dbapi_connection: Any) -> _ConnState | None:
        """Fail-safe wrapper around :meth:`_lookup_state` for listener call sites."""
        with contextlib.suppress(Exception):
            return self._lookup_state(dbapi_connection)
        return None

    def _emit(self, sink_name: str, *args: Any) -> None:
        """
        Fail-safe wrapper around a single metric-sink call.

        A raising sink (e.g. a misbehaving OTel instrument) must never
        escape into checkout/checkin/invalidate/close/handle_error --
        metrics are strictly best-effort and must never block a checkout or
        obscure the real DB error. Takes the sink's *name* rather than a
        bound method so the attribute lookup on the caller-supplied
        ``sinks`` object happens inside the suppression too.
        """
        with contextlib.suppress(Exception):
            getattr(self.sinks, sink_name)(*args)

    def _set_pending_trigger(self, phase: DisconnectPhase) -> None:
        """Stage a disconnect phase for the invalidate listener; never raises."""
        with contextlib.suppress(Exception):
            self._pending_trigger.phase = phase

    # ── the wrapped do_ping ────────────────────────────────────────────

    def wrapped_do_ping(self, dbapi_connection: DBAPIConnection) -> bool:
        """
        Skip the real ping for a connection returned to the pool recently
        enough; delegate to the real ``do_ping`` otherwise, for any reason
        (past window, missing/inconsistent state, or an internal error).

        Structure is the guarantee here, not per-statement care: all
        decision-making and bookkeeping lives in :meth:`_plan_ping` and
        :meth:`_record_real_ping`, each of which swallows everything, and
        the delegate is recovered inside that protected region rather than
        read at the call site. Exactly one statement below is unprotected --
        the delegate invocation -- because a genuine ping failure must reach
        SQLAlchemy's native ``_do_ping_w_event`` classification and
        ``handle_error(is_pre_ping=True)`` path unchanged. A state access
        added to either helper later is therefore contained by construction.
        """
        delegate, skip = self._plan_ping(dbapi_connection)
        if skip:
            return True
        if delegate is None:
            # The gate could not recover the real ping at all. Report the
            # connection as unverified rather than asserting it is healthy:
            # SQLAlchemy invalidates and replaces it, costing a reconnect,
            # where a ``True`` here would hand out an unchecked connection.
            return False

        result = delegate(dbapi_connection)

        self._record_real_ping(dbapi_connection, result)
        return result

    def _plan_ping(
        self, dbapi_connection: DBAPIConnection
    ) -> tuple[Callable[[DBAPIConnection], bool] | None, bool]:
        """
        Decide skip-vs-ping, emit the decision metrics, recover the delegate.

        Never raises. Any internal failure degrades to "ping": the gate's
        failure mode is to do the round-trip it was trying to avoid, never
        to claim a connection is fresh.

        :param dbapi_connection: The connection being checked out.
        :returns: ``(delegate, skip)``. When *skip* is ``True`` the caller
            returns success without pinging; otherwise *delegate* is the
            real ``do_ping`` to invoke, or ``None`` if even that could not
            be recovered.
        """
        delegate: Callable[[DBAPIConnection], bool] | None = None
        decision: Decision = "ping"
        state: _ConnState | None = None
        age: float | None = None
        try:
            # Recovered first and from inside the protection: a bookkeeping
            # failure below must not cost us the ability to ping at all.
            delegate = self._original_do_ping
            dialect_name = self.dialect_name
            state = self._lookup_state(dbapi_connection)
            if state is not None:
                candidate_age = self._clock() - state.last_success
                if candidate_age >= 0:
                    age = candidate_age
                    if age < self.window_seconds:
                        decision = "skip"
            if age is not None:
                self._emit("record_checkout_age", dialect_name, age)
            self._emit("inc_decision", dialect_name, decision)
            if decision == "skip" and state is not None:
                # Only last_decision changes here; on_checkin stamps
                # last_success on every return, so the window measures idle
                # time since the pool last released this connection.
                state.last_decision = "skip"
        except Exception:  # noqa: BLE001 - any bookkeeping failure falls through to a real ping
            _report_swallowed("database ping gate skip decision failed")
            return delegate, False
        return delegate, decision == "skip"

    def _record_real_ping(self, dbapi_connection: DBAPIConnection, result: bool) -> None:
        """
        Record the outcome of a real ping. Never raises.

        :param dbapi_connection: The connection that was pinged.
        :param result: What the real ``do_ping`` returned.
        """
        try:
            if result:
                self._store_state(
                    dbapi_connection, _ConnState(last_success=self._clock(), last_decision="ping")
                )
                return
            # A delegated do_ping returning False bypasses handle_error
            # entirely (only a *raised* exception reaches it), so the
            # wrapper is the only place that observes this disconnect.
            self._emit("inc_disconnect", self.dialect_name, "pre_ping")
            self._set_pending_trigger("pre_ping")
        except Exception:  # noqa: BLE001 - bookkeeping must never escape into the DB path
            _report_swallowed("database ping gate post-ping bookkeeping failed")

    # ── record_info access (fail-safe: a raising accessor or mapping never
    #    escapes into connect/invalidate) ──────────────────────────────

    def _pop_recovery_marker(self, connection_record: ConnectionPoolEntry) -> Any:
        """Take and clear the one-shot recovery marker, or ``None``."""
        with contextlib.suppress(Exception):
            record_info = connection_record.record_info
            if record_info is not None:
                return record_info.pop(_RECOVERY_MARKER_KEY, None)
        return None

    def _set_recovery_marker(
        self, connection_record: ConnectionPoolEntry, trigger: RecoveryTrigger
    ) -> None:
        """Leave a one-shot marker for the replacement connection to report."""
        with contextlib.suppress(Exception):
            record_info = connection_record.record_info
            if record_info is not None:
                record_info[_RECOVERY_MARKER_KEY] = trigger

    # ── bookkeeping listeners ───────────────────────────────────────────

    @_fail_safe_listener
    def on_connect(
        self, dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
    ) -> None:
        with contextlib.suppress(Exception):
            self._store_state(
                dbapi_connection, _ConnState(last_success=self._clock(), last_decision="fresh")
            )
        trigger = self._pop_recovery_marker(connection_record)
        if trigger is not None:
            self._emit("inc_recovery", self.dialect_name, trigger)
        self._emit("inc_decision", self.dialect_name, "fresh")

    @_fail_safe_listener
    def on_checkin(
        self, dbapi_connection: DBAPIConnection | None, _connection_record: ConnectionPoolEntry
    ) -> None:
        if dbapi_connection is None:
            return
        state = self._safe_lookup_state(dbapi_connection)
        last_decision = state.last_decision if state is not None else "ping"
        with contextlib.suppress(Exception):
            self._store_state(
                dbapi_connection,
                _ConnState(last_success=self._clock(), last_decision=last_decision),
            )

    @_fail_safe_listener
    def on_invalidate(
        self,
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
        _exception: BaseException | None,
    ) -> None:
        trigger = getattr(self._pending_trigger, "phase", None)
        if trigger is not None:
            del self._pending_trigger.phase
        else:
            trigger = "other"
        self._set_recovery_marker(connection_record, trigger)
        with contextlib.suppress(Exception):
            self._drop_state(dbapi_connection)

    @_fail_safe_listener
    def on_close(
        self, dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry
    ) -> None:
        with contextlib.suppress(Exception):
            self._drop_state(dbapi_connection)

    @_fail_safe_listener
    def on_handle_error(self, exception_context: ExceptionContext) -> None:
        """
        Metrics only. Never reclassifies, catches, or returns a replacement
        exception -- returning ``None`` leaves SQLAlchemy's own handling
        completely unmodified.
        """
        if not exception_context.is_disconnect:
            return
        if exception_context.is_pre_ping:
            phase: DisconnectPhase = "pre_ping"
        else:
            phase = "other"
            connection = exception_context.connection
            if connection is not None:
                dbapi_connection = None
                with contextlib.suppress(Exception):
                    dbapi_connection = connection.connection.dbapi_connection
                if dbapi_connection is not None:
                    state = self._safe_lookup_state(dbapi_connection)
                    if state is not None and state.last_decision == "skip":
                        phase = "post_skip"
        self._emit("inc_disconnect", self.dialect_name, phase)
        self._set_pending_trigger(phase)
        return


def _install_db_pool_ping_gate(
    engine: Engine,
    *,
    window_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> PingGate:
    """
    Install (or return the already-installed) ping gate on ``engine``.

    Idempotent: a second call with the same engine returns the existing
    gate and never stacks wrappers/listeners -- safe to call again after
    ``engine.dispose()``/pool ``recreate()``, which retain the same dialect
    instance.

    :param engine: The engine to install the gate on. Caller is responsible
        for confirming eligibility (Postgres, ``window_seconds > 0``) first.
    :param window_seconds: Recency window; a checkout whose last successful
        return to the pool is younger than this skips the real ping.
    :param clock: Injectable clock for tests; defaults to
        :func:`time.monotonic`.
    :returns: The installed (or pre-existing) :class:`PingGate`.
    """
    existing: PingGate | None = getattr(engine, _GATE_ATTR, None)
    if existing is not None:
        return existing

    dialect = engine.dialect
    original_do_ping = dialect.do_ping
    gate = PingGate(
        dialect_name=dialect.name,
        window_seconds=window_seconds,
        clock=clock,
        original_do_ping=original_do_ping,
    )
    # Instance attribute on the dialect *instance*, never the class -- a
    # fresh dialect instance is minted per create_engine() call, so this
    # cannot leak across engines or tests.
    dialect.do_ping = gate.wrapped_do_ping  # type: ignore[method-assign]

    event.listen(engine, "connect", gate.on_connect)
    event.listen(engine, "checkin", gate.on_checkin)
    event.listen(engine, "invalidate", gate.on_invalidate)
    event.listen(engine, "close", gate.on_close)
    event.listen(engine, "handle_error", gate.on_handle_error)

    setattr(engine, _GATE_ATTR, gate)
    return gate
