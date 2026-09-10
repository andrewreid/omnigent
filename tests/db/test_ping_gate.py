"""
Behavioral tests for the recency-gated pre-ping skip gate
(``omnigent.db.ping_gate``).

All tests except the two marked LIVE run against a fake clock and/or fake
DBAPI -- zero ``sleep()``s, zero wall-clock dependence, and no real socket.
The two LIVE tests at the end of this file exercise a real PostgreSQL
backend (via ``OMNIGENT_TEST_DB_URI`` / the ``db_uri`` fixture, same
convention as the rest of ``tests/db`` and ``tests/stores``) and are the
strongest evidence in this suite that the gate's core safety claim --
delegate to SQLAlchemy's native disconnect classification, never hand-roll
it -- actually holds against the real, installed psycopg driver.
"""

from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa

from omnigent.db import ping_gate as ping_gate_module
from omnigent.db.ping_gate import _RECOVERY_MARKER_KEY, PingGate, _install_db_pool_ping_gate
from omnigent.db.utils import (
    _LAKEBASE_POOL_RECYCLE_SECONDS,
    _SERVER_POOL_RECYCLE_SECONDS,
    _create_engine,
    clear_engine_cache,
    get_or_create_engine,
    make_managed_session_maker,
    set_lakebase_token_provider,
)


@pytest.fixture(autouse=True)
def _no_module_logger_residue() -> Any:
    """Fail any test in this file that leaves state on the module logger.

    ``logging.Logger`` resolves ``debug`` / ``warning`` / ``exception`` from
    the class, so ``setattr`` on the instance — including ``monkeypatch``'s
    undo, which restores the value it read rather than deleting it — plants
    a shadowing instance attribute that survives for the life of the
    process and silently defeats every later class-level patch. Handlers
    and level are checked for the same reason: they are process-wide.
    """
    logger = ping_gate_module._logger
    before = set(logger.__dict__), list(logger.handlers), logger.level
    yield
    assert set(logger.__dict__) == before[0], (
        "test left an attribute on omnigent.db.ping_gate._logger — it will "
        "shadow the class for every later test in this process"
    )
    assert list(logger.handlers) == before[1]
    assert logger.level == before[2]


# ── shared fakes ────────────────────────────────────────────────────────


class _FakeDbapiConn:
    """A hashable, weak-referenceable stand-in for a DBAPI connection."""


class _StubRecord:
    """Minimal ``ConnectionPoolEntry`` stand-in: only ``record_info`` is used."""

    def __init__(self) -> None:
        self.record_info: dict[str, Any] = {}


class _RaisingRecordInfoRecord:
    """Pool entry whose ``record_info`` accessor itself raises.

    Distinct from an unhashable connection: that breaks the weak-dict state,
    this breaks the ``record_info`` mapping the recovery marker rides on.
    A per-statement fail-safe around only the former leaves this one live.
    """

    @property
    def record_info(self) -> dict[str, Any]:
        raise RuntimeError("record_info accessor broke")


class _RecordingDoPing:
    """Fake ``do_ping`` -- records every call, optionally raises or returns
    a fixed value (native pre-ping's own disconnect-detection convention:
    ``False`` means "dead", not an exception)."""

    def __init__(self, fail: bool = False, result: bool = True) -> None:
        self.calls: list[Any] = []
        self.fail = fail
        self.result = result

    def __call__(self, dbapi_connection: Any) -> bool:
        self.calls.append(dbapi_connection)
        if self.fail:
            raise RuntimeError("simulated ping failure")
        return self.result


class _FakeInstrument:
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, str]]] = []

    def add(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))

    def record(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))


class _FakeMeter:
    def __init__(self) -> None:
        self.instruments: dict[str, _FakeInstrument] = {}

    def _make(self, name: str, **_kw: Any) -> _FakeInstrument:
        inst = _FakeInstrument()
        self.instruments[name] = inst
        return inst

    def create_counter(self, name: str, **kw: Any) -> _FakeInstrument:
        return self._make(name, **kw)

    def create_histogram(self, name: str, **kw: Any) -> _FakeInstrument:
        return self._make(name, **kw)

    def create_gauge(self, name: str, **kw: Any) -> _FakeInstrument:
        return self._make(name, **kw)


class _FakeConnection:
    """Minimal stand-in for a psycopg connection -- no real socket."""

    def __init__(self) -> None:
        self.autocommit = False
        self.closed = False
        self.broken = False
        self.read_only = False
        self.isolation_level = None
        self.ping_statements: list[str] = []
        self.fail_pings = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def add_notice_handler(self, handler: object) -> None:
        pass


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def execute(self, statement: str, params: object = None) -> None:
        self._conn.ping_statements.append(statement)
        if self._conn.fail_pings:
            import psycopg

            self._conn.closed = True
            raise psycopg.OperationalError("simulated disconnect")

    def close(self) -> None:
        pass


# ── direct PingGate unit tests (5-10) ──────────────────────────────────


def test_skip_within_window_performs_no_io() -> None:
    """
    A checkout inside the window skips the real ping entirely -- if this
    test can't fail, the feature does nothing.
    """
    recorder = _RecordingDoPing()
    clock = {"t": 0.0}
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=10.0,
        clock=lambda: clock["t"],
        original_do_ping=recorder,
    )
    conn = _FakeDbapiConn()
    gate.on_connect(conn, _StubRecord())

    clock["t"] = 9.999
    result = gate.wrapped_do_ping(conn)

    assert result is True
    assert recorder.calls == []


def test_age_at_and_past_window_pings() -> None:
    """
    Age exactly at, or past, the window must both real-ping.

    Catches an off-by-one that would either extend staleness beyond the
    configured window (silent correctness regression) or ping every time
    (silent performance regression).
    """
    for extra in (0.0, 1.0):
        recorder = _RecordingDoPing()
        clock = {"t": 0.0}
        gate = PingGate(
            dialect_name="postgresql",
            window_seconds=5.0,
            clock=lambda c=clock: c["t"],
            original_do_ping=recorder,
        )
        conn = _FakeDbapiConn()
        gate.on_connect(conn, _StubRecord())

        clock["t"] = 5.0 + extra
        result = gate.wrapped_do_ping(conn)

        assert result is True
        assert recorder.calls == [conn], f"extra={extra}"


def test_skip_does_not_refresh_recency() -> None:
    """
    Two consecutive skip-eligible checkouts without an intervening real
    ping or checkin: age keeps growing from the original stamp.

    Catches a bug that would let repeated fast checkouts perpetually
    postpone ever re-verifying the connection.
    """
    recorder = _RecordingDoPing()
    clock = {"t": 0.0}
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=10.0,
        clock=lambda: clock["t"],
        original_do_ping=recorder,
    )
    conn = _FakeDbapiConn()
    gate.on_connect(conn, _StubRecord())

    clock["t"] = 5.0
    assert gate.wrapped_do_ping(conn) is True
    assert recorder.calls == []

    clock["t"] = 9.0
    assert gate.wrapped_do_ping(conn) is True
    assert recorder.calls == []  # still skip -- age is 9, not reset to 0 by the prior skip

    clock["t"] = 10.0
    assert gate.wrapped_do_ping(conn) is True
    assert recorder.calls == [conn]  # age (from the original t=0 stamp) now hits the window


def test_checkin_and_successful_ping_refresh_recency() -> None:
    """
    Both refresh paths -- checkin, and a successful real ping -- must
    independently update the recency stamp. A *delegated* ``do_ping``
    returning ``False`` (native pre-ping's disconnect signal, contract
    §3/§10) must pass straight through unmodified -- never converted to
    ``True`` and never stamped as a success -- so SQLAlchemy's own
    ``InvalidatePoolError``/generation-invalidation path still fires exactly
    as it would without this gate installed.

    Catches a wrapper that pings but forgets to stamp: every subsequent
    checkout would incorrectly ping too, silently degrading the feature to
    a no-op. Catches a wrapper that reclassifies or swallows a delegated
    ``False`` result, which would hide a real dead connection from native
    pre-ping's own disconnect handling.
    """
    recorder = _RecordingDoPing()
    clock = {"t": 0.0}
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=5.0,
        clock=lambda: clock["t"],
        original_do_ping=recorder,
    )
    conn = _FakeDbapiConn()
    record = _StubRecord()
    gate.on_connect(conn, record)

    # checkin refresh: without it, age at t=7 relative to t=0 would be 7 (>=
    # window) and force a real ping; with it (stamped at t=3), age is only 4.
    clock["t"] = 3.0
    gate.on_checkin(conn, record)
    clock["t"] = 7.0
    assert gate.wrapped_do_ping(conn) is True
    assert recorder.calls == []

    # successful-ping refresh: force a real ping, then confirm the very next
    # checkout treats *that* ping's timestamp as the new baseline.
    clock["t"] = 20.0
    assert gate.wrapped_do_ping(conn) is True
    assert recorder.calls == [conn]

    clock["t"] = 24.0  # age 4 from the t=20 ping stamp, not 24 from t=0
    assert gate.wrapped_do_ping(conn) is True
    assert recorder.calls == [conn]  # unchanged -- second call skipped

    # A delegated do_ping returning False (past the window, forcing a real
    # ping) must pass through unmodified, and must not be stamped as if it
    # had succeeded.
    failing_recorder = _RecordingDoPing(result=False)
    gate2 = PingGate(
        dialect_name="postgresql",
        window_seconds=5.0,
        clock=lambda: clock["t"],
        original_do_ping=failing_recorder,
    )
    conn2 = _FakeDbapiConn()
    gate2.on_connect(conn2, _StubRecord())
    stamp_before = gate2._lookup_state(conn2).last_success
    clock["t"] += 10.0  # past the window -- forces delegation
    assert gate2.wrapped_do_ping(conn2) is False  # passed through, not converted to True
    assert failing_recorder.calls == [conn2]
    # Not stamped as a success: recency is unchanged from the on_connect stamp.
    assert gate2._lookup_state(conn2).last_success == stamp_before


def test_invalidated_or_none_checkin_does_not_stamp() -> None:
    """
    A ``None``/invalidated checkin must never stamp recency, and a
    replacement connection after invalidation must start from genuinely
    fresh state -- no staleness or recovery-marker leakage from the old
    connection.

    Catches treating an invalidated/``None`` checkin as healthy (would let a
    known-bad connection become skip-eligible), and catches replacement
    state incorrectly inheriting the old connection's timestamps.
    """
    recorder = _RecordingDoPing()
    clock = {"t": 0.0}
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=5.0,
        clock=lambda: clock["t"],
        original_do_ping=recorder,
    )
    record = _StubRecord()
    conn_a = _FakeDbapiConn()
    gate.on_connect(conn_a, record)

    clock["t"] = 1.0
    gate.on_checkin(None, record)  # invalidated checkin -- must not raise or stamp
    assert gate._lookup_state(conn_a) is not None
    assert gate._lookup_state(conn_a).last_success == 0.0

    gate.on_invalidate(conn_a, record, RuntimeError("dead"))
    assert gate._lookup_state(conn_a) is None
    assert record.record_info.get(_RECOVERY_MARKER_KEY) == "other"

    conn_b = _FakeDbapiConn()
    clock["t"] = 2.0
    gate.on_connect(conn_b, record)  # replacement; consumes the recovery marker
    assert record.record_info.get(_RECOVERY_MARKER_KEY) is None

    clock["t"] = 2.0 + 5.0  # past the window relative to conn_b's own connect stamp
    assert gate.wrapped_do_ping(conn_b) is True
    assert recorder.calls == [conn_b]


@pytest.mark.parametrize(
    "case",
    [
        "missing_state",
        "clock_raises",
        "unhashable_connection",
        "clock_regression",
        "raising_metric_sink",
        "raising_record_info",
    ],
)
def test_missing_state_or_internal_error_fails_safe_to_ping(case: str) -> None:
    """
    Anything unanticipated -- no recorded state, a misbehaving clock, a
    connection object that can't be used as a weak-dict key, a clock that
    regresses (age would compute negative), or a raising metric sink --
    must fail safe to a real ping, never crash and never silently skip.

    ``raising_record_info`` covers the other state channel: a pool entry
    whose ``record_info`` accessor raises. The recovery marker is read in
    ``on_connect`` and written in ``on_invalidate``, so a fail-safe built
    only around the weak-dict state leaves those two unguarded.

    ``unhashable_connection`` additionally drives every bookkeeping listener
    (``on_connect``/``on_checkin``/``on_invalidate``/``on_close``/
    ``on_handle_error``, not just ``wrapped_do_ping``) with the same
    unhashable connection first -- a fail-safe that only covers the
    ping-decision path while leaving the other five listeners unguarded
    would let a ``TypeError`` escape into pool internals and obscure the
    real DB error.

    This is the feature's entire safety net for "anything we didn't
    anticipate."
    """
    recorder = _RecordingDoPing()

    if case == "missing_state":
        gate = PingGate(
            dialect_name="postgresql",
            window_seconds=5.0,
            clock=lambda: 0.0,
            original_do_ping=recorder,
        )
        conn: Any = _FakeDbapiConn()
    elif case == "clock_raises":
        calls = {"n": 0}

        def _flaky_clock() -> float:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("clock broke")
            return 0.0

        gate = PingGate(
            dialect_name="postgresql",
            window_seconds=5.0,
            clock=_flaky_clock,
            original_do_ping=recorder,
        )
        conn = _FakeDbapiConn()
        gate.on_connect(conn, _StubRecord())
    elif case == "clock_regression":
        clock = {"t": 10.0}
        gate = PingGate(
            dialect_name="postgresql",
            window_seconds=5.0,
            clock=lambda: clock["t"],
            original_do_ping=recorder,
        )
        conn = _FakeDbapiConn()
        gate.on_connect(conn, _StubRecord())  # stamps last_success at t=10
        clock["t"] = 3.0  # clock went backwards -- age would be -7
    elif case == "raising_metric_sink":

        class _RaisingSinks:
            def record_checkout_age(self, *args: Any) -> None:
                raise RuntimeError("sink boom")

            def inc_decision(self, *args: Any) -> None:
                raise RuntimeError("sink boom")

            def inc_disconnect(self, *args: Any) -> None:
                raise RuntimeError("sink boom")

            def inc_recovery(self, *args: Any) -> None:
                raise RuntimeError("sink boom")

        clock = {"t": 0.0}
        gate = PingGate(
            dialect_name="postgresql",
            window_seconds=5.0,
            clock=lambda: clock["t"],
            original_do_ping=recorder,
        )
        gate.sinks = _RaisingSinks()
        conn = _FakeDbapiConn()
        gate.on_connect(conn, _StubRecord())  # inc_decision("fresh") raises -- must not escape
        clock["t"] = 1.0
        # Skip path with raising sinks, exercised separately: the common
        # tail below drives the real-ping path for every case uniformly.
        assert gate.wrapped_do_ping(conn) is True
        assert recorder.calls == []
        clock["t"] = 10.0  # past the window for the common real-ping tail below
    elif case == "raising_record_info":
        clock = {"t": 0.0}
        gate = PingGate(
            dialect_name="postgresql",
            window_seconds=5.0,
            clock=lambda: clock["t"],
            original_do_ping=recorder,
        )
        conn = _FakeDbapiConn()
        bad_record: Any = _RaisingRecordInfoRecord()
        gate.on_connect(conn, bad_record)  # marker read raises -- must not escape
        gate.on_checkin(conn, bad_record)
        gate.on_invalidate(conn, bad_record, RuntimeError("dead"))  # marker write raises
        gate.on_close(conn, bad_record)
        # on_invalidate dropped the state; re-stamp so the common tail below
        # exercises the real-ping path for the same reason every case does.
        gate.on_connect(conn, bad_record)
        clock["t"] = 10.0  # past the window
    else:
        conn = {}  # unhashable -- WeakKeyDictionary lookup raises TypeError
        gate = PingGate(
            dialect_name="postgresql",
            window_seconds=5.0,
            clock=lambda: 0.0,
            original_do_ping=recorder,
        )
        record = _StubRecord()
        gate.on_connect(conn, record)  # must not raise (first weak-dict insertion)
        gate.on_checkin(conn, record)  # must not raise
        gate.on_invalidate(conn, record, RuntimeError("dead"))  # must not raise
        assert record.record_info.get(_RECOVERY_MARKER_KEY) == "other"  # bookkeeping still runs
        gate.on_close(conn, record)  # must not raise
        gate.on_handle_error(
            SimpleNamespace(
                is_disconnect=True,
                is_pre_ping=False,
                connection=SimpleNamespace(connection=SimpleNamespace(dbapi_connection=conn)),
            )
        )  # must not raise despite the unhashable lookup inside

    result = gate.wrapped_do_ping(conn)

    assert result is True
    assert recorder.calls == [conn]


# ── config/eligibility tests (11-13) ───────────────────────────────────


def test_valid_window_ignored_on_non_postgres_engine(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A positive window on a non-Postgres engine (sqlite, D1) is a per-engine
    no-op, never a startup failure -- and it must be observable (a debug log
    line), not merely silent, so an operator debugging "why isn't the gate
    active" has something to grep for.

    Also covers the inverse defense-in-depth case: if the URL says
    PostgreSQL but the *constructed* engine's dialect disagrees (should be
    unreachable in practice), the mismatched engine must be disposed and a
    ``RuntimeError`` raised -- never silently installed against the wrong
    dialect.

    Catches the gate accidentally activating for a backend it was never
    validated against -- Postgres-only is a hard scope boundary -- and
    catches the reverse: a mismatch slipping through undetected.
    """
    pytest.importorskip("sqlalchemy_cloudflare_d1")
    monkeypatch.setenv("OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS", "3.0")
    caplog.set_level(logging.DEBUG, logger="omnigent.db.utils")

    sqlite_engine = _create_engine(f"sqlite:///{tmp_path / 't.db'}")
    try:
        assert getattr(sqlite_engine, "_omnigent_db_pool_ping_gate", None) is None
        assert any(
            r.levelno == logging.DEBUG and "sqlite" in r.getMessage() for r in caplog.records
        )
    finally:
        sqlite_engine.dispose()
    caplog.clear()

    d1_engine = _create_engine("cloudflare_d1://acct:token@db")
    try:
        assert getattr(d1_engine, "_omnigent_db_pool_ping_gate", None) is None
        assert any(
            r.levelno == logging.DEBUG and "cloudflare_d1" in r.getMessage()
            for r in caplog.records
        )
    finally:
        d1_engine.dispose()

    # Defense-in-depth: URL says postgres, but the constructed engine's
    # dialect disagrees.
    mock_engine = MagicMock()
    mock_engine.dialect.name = "sqlite"
    monkeypatch.setattr("omnigent.db.utils.create_engine", lambda uri, **kwargs: mock_engine)
    with pytest.raises(RuntimeError, match="eligibility mismatch"):
        _create_engine("postgresql+psycopg://user@localhost/db")
    mock_engine.dispose.assert_called_once()


@pytest.mark.parametrize("raw_value", ["-1", "nan", "inf", "abc", "--3"])
@pytest.mark.parametrize(
    "db_uri", ["sqlite:///:memory:", "postgresql+psycopg://user@localhost/db"]
)
def test_malformed_window_fails_engine_creation(
    monkeypatch: pytest.MonkeyPatch, raw_value: str, db_uri: str
) -> None:
    """
    A malformed window value must fail engine creation for any backend, not
    just Postgres -- a misconfigured deployment must fail loud at startup
    instead of running the feature in an undefined state. The offending raw
    value must also appear in the message, so an operator can see exactly
    what was rejected.
    """
    monkeypatch.setenv("OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS", raw_value)
    with pytest.raises(ValueError, match=re.escape(raw_value)) as exc_info:
        _create_engine(db_uri)
    assert "OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS" in str(exc_info.value)


@pytest.mark.databricks
def test_window_ge_recycle_warns_not_rejects(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A window at or past ``pool_recycle`` (static 1800s, Lakebase 600s) must
    warn, not raise -- recycle is already an independent hard upper bound on
    staleness.

    Catches either direction of a wrong severity: rejecting a technically
    safe configuration is operator-hostile; silently accepting with no
    warning hides a likely-misconfigured value from whoever set it. The
    severity must specifically be WARNING, not merely present at some level.

    Marked ``databricks``: constructs a real ``postgresql+psycopg`` engine
    (never connects), so it needs the real driver importable -- same
    precedent as ``test_static_postgres_uri_path_unchanged`` etc. in
    ``tests/db/test_utils.py``.
    """
    caplog.set_level(logging.DEBUG, logger="omnigent.db.ping_gate")

    monkeypatch.setenv("OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS", str(_SERVER_POOL_RECYCLE_SECONDS))
    engine = _create_engine("postgresql+psycopg://user@localhost/db")
    try:
        assert any(
            r.levelno == logging.WARNING and "pool_recycle" in r.getMessage()
            for r in caplog.records
        )
    finally:
        engine.dispose()
    caplog.clear()

    set_lakebase_token_provider(lambda: "fake-token")
    try:
        monkeypatch.setenv(
            "OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS", str(_LAKEBASE_POOL_RECYCLE_SECONDS)
        )
        engine2 = _create_engine("postgresql+psycopg://user@localhost/db2")
        try:
            assert any(
                r.levelno == logging.WARNING and "pool_recycle" in r.getMessage()
                for r in caplog.records
            )
        finally:
            engine2.dispose()
    finally:
        set_lakebase_token_provider(None)


@pytest.mark.databricks
def test_pool_use_lifo_iff_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    LIFO pool reuse must be wired exactly when the gate is enabled, and its
    effect on actual checkout order must be real (not just the kwarg).

    Catches the kwarg being set but not actually changing checkout order (a
    wiring bug), or LIFO leaking on when the gate is off (an unrelated
    behavior change no deployment opted into).

    Marked ``databricks``: part 2 below constructs a real
    ``postgresql+psycopg`` engine (never connects) to observe actual pool
    reuse order, so it needs the real driver importable.
    """

    # 1. Kwarg wiring: enabled adds *exactly* pool_use_lifo=True and nothing
    #    else changes -- assert the full delta, not just presence/absence of
    #    that one key, so any other kwarg drift between the two paths fails
    #    this test too.
    captured_by_window: dict[str, dict[str, Any]] = {}
    for window in ("0", "3.0"):
        captured: dict[str, Any] = {}
        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"

        def _capturing_create_engine(
            uri: str,
            _engine: MagicMock = mock_engine,
            _captured: dict[str, Any] = captured,
            **kwargs: Any,
        ) -> MagicMock:
            _captured.update(kwargs)
            return _engine

        monkeypatch.setattr("omnigent.db.utils.create_engine", _capturing_create_engine)
        monkeypatch.setenv("OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS", window)
        _create_engine("postgresql+psycopg://user@localhost/db")
        captured_by_window[window] = captured

    assert "pool_use_lifo" not in captured_by_window["0"]
    assert captured_by_window["3.0"] == {
        **captured_by_window["0"],
        "pool_use_lifo": True,
    }

    # 2. Actual reuse-order behavior for the kwarg itself.
    def _make_engine(use_lifo: bool) -> Any:
        conns = []

        def _creator() -> _FakeConnection:
            conn = _FakeConnection()
            conns.append(conn)
            return conn

        engine = sa.create_engine(
            "postgresql+psycopg://fake/db",
            creator=_creator,
            poolclass=sa.pool.QueuePool,
            pool_use_lifo=use_lifo,
            pool_size=3,
            max_overflow=0,
        )
        engine.dialect.initialize = lambda dbapi_conn: None  # type: ignore[method-assign]
        return engine, conns

    for use_lifo in (True, False):
        engine, _conns = _make_engine(use_lifo)
        first_round = [engine.raw_connection() for _ in range(3)]
        checked_out_order = [c.dbapi_connection for c in first_round]
        for c in first_round:
            c.close()  # returned to the pool in acquisition order

        second_round = [engine.raw_connection() for _ in range(3)]
        reuse_order = [c.dbapi_connection for c in second_round]
        for c in second_round:
            c.close()

        if use_lifo:
            assert reuse_order == list(reversed(checked_out_order))
        else:
            assert reuse_order == checked_out_order
        engine.dispose()


# ── metrics tests (15-18) ───────────────────────────────────────────────


def _wired_gate(
    monkeypatch: pytest.MonkeyPatch, *, window_seconds: float, clock: Any, fail: bool = False
) -> tuple[PingGate, _FakeMeter]:
    from omnigent.runtime import telemetry

    recorder = _RecordingDoPing(fail=fail)
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=window_seconds,
        clock=clock,
        original_do_ping=recorder,
    )
    fake_meter = _FakeMeter()
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr("opentelemetry.metrics.get_meter", lambda name: fake_meter)
    telemetry.instrument_db_pool_ping_gate(SimpleNamespace(_omnigent_db_pool_ping_gate=gate))
    return gate, fake_meter


def _stub_disconnect_ctx(*, is_pre_ping: bool, dbapi_connection: Any = None) -> SimpleNamespace:
    connection = None
    if dbapi_connection is not None:
        connection = SimpleNamespace(connection=SimpleNamespace(dbapi_connection=dbapi_connection))
    return SimpleNamespace(is_disconnect=True, is_pre_ping=is_pre_ping, connection=connection)


def test_decisions_total_fresh_skip_ping_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Fresh/skip/ping decisions must land on exact labeled series.

    Catches mislabeling that would corrupt the primary operability signal
    for this feature, the ping-skip rate.
    """
    clock = {"t": 0.0}
    gate, meter = _wired_gate(monkeypatch, window_seconds=5.0, clock=lambda: clock["t"])
    record = _StubRecord()
    conn = _FakeDbapiConn()

    gate.on_connect(conn, record)  # fresh
    clock["t"] = 1.0
    gate.wrapped_do_ping(conn)  # skip
    clock["t"] = 10.0
    gate.wrapped_do_ping(conn)  # ping

    decisions = meter.instruments["omnigent.db.pool.pre_ping_decisions_total"].calls
    by_decision: dict[str, int] = {}
    for amount, attrs in decisions:
        by_decision[attrs["decision"]] = by_decision.get(attrs["decision"], 0) + amount

    assert by_decision == {"fresh": 1, "skip": 1, "ping": 1}


def test_disconnects_total_post_skip_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A disconnect discovered right after a skip decision must classify as
    ``post_skip``; one during pre-ping itself as ``pre_ping``; anything else
    as ``other``. A non-disconnect exception (``is_disconnect=False``) must
    fire no disconnect/recovery metric at all and stage no pending recovery
    trigger -- a handler that fires on every exception regardless of
    classification would pollute ``disconnects_total`` with non-disconnect
    noise and misattribute the next, unrelated invalidation.

    ``post_skip`` is explicitly named as the future default-on promotion's
    gating signal -- if it's wrong, that decision is made on bad data.
    """
    clock = {"t": 0.0}
    gate, meter = _wired_gate(monkeypatch, window_seconds=5.0, clock=lambda: clock["t"])
    record = _StubRecord()

    conn_skip = _FakeDbapiConn()
    gate.on_connect(conn_skip, record)
    clock["t"] = 1.0
    gate.wrapped_do_ping(conn_skip)  # skip -> last_decision="skip"
    gate.on_handle_error(_stub_disconnect_ctx(is_pre_ping=False, dbapi_connection=conn_skip))

    gate.on_handle_error(_stub_disconnect_ctx(is_pre_ping=True))

    conn_ping = _FakeDbapiConn()
    gate.on_connect(conn_ping, record)
    clock["t"] = 20.0
    gate.wrapped_do_ping(conn_ping)  # real ping -> last_decision="ping"
    gate.on_handle_error(_stub_disconnect_ctx(is_pre_ping=False, dbapi_connection=conn_ping))

    disconnects = meter.instruments["omnigent.db.pool.disconnects_total"].calls
    phases = [attrs["phase"] for _amount, attrs in disconnects]
    assert phases == ["post_skip", "pre_ping", "other"]

    # is_disconnect=False: no additional disconnect metric, and the pending
    # trigger staged by the last real disconnect above is left untouched
    # (neither cleared nor overwritten) -- proving this call didn't touch
    # trigger-staging state at all.
    trigger_before = getattr(gate._pending_trigger, "phase", None)
    gate.on_handle_error(SimpleNamespace(is_disconnect=False, is_pre_ping=False, connection=None))
    assert len(meter.instruments["omnigent.db.pool.disconnects_total"].calls) == 3
    assert getattr(gate._pending_trigger, "phase", None) == trigger_before

    # Consume that staged trigger, then verify a *fresh* on_handle_error with
    # is_disconnect=False, with nothing staged, leaves a subsequent
    # unrelated invalidate to fall back to "other" -- not pick up anything.
    conn_stale_trigger = _FakeDbapiConn()
    gate.on_connect(conn_stale_trigger, record)
    gate.on_invalidate(conn_stale_trigger, record, RuntimeError("dead"))

    gate.on_handle_error(SimpleNamespace(is_disconnect=False, is_pre_ping=False, connection=None))
    assert getattr(gate._pending_trigger, "phase", None) is None

    conn_c = _FakeDbapiConn()
    gate.on_connect(conn_c, record)
    gate.on_invalidate(conn_c, record, RuntimeError("dead"))
    assert record.record_info.get(_RECOVERY_MARKER_KEY) == "other"


def test_recoveries_total_single_increment_per_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The recovery marker must be consumed exactly once per recovery, never
    double-counted and never missed -- and correctly attributed across the
    full ``pre_ping``/``post_skip``/``other`` taxonomy (contract: recovery
    ``trigger`` must reflect what actually staged it via ``handle_error``,
    not always fall back to the same value regardless of what happened).
    Includes the one taxonomy path ``handle_error`` never sees at all: a
    delegated ``do_ping`` returning ``False`` (not raising) still means
    ``pre_ping``, but only the wrapper itself observes it.
    """
    clock = {"t": 0.0}
    gate, meter = _wired_gate(monkeypatch, window_seconds=5.0, clock=lambda: clock["t"])

    # "other": no handle_error staged a trigger before invalidate -- the
    # unattributed fallback.
    record_other = _StubRecord()
    conn_a = _FakeDbapiConn()
    gate.on_connect(conn_a, record_other)
    gate.on_invalidate(conn_a, record_other, RuntimeError("dead"))
    conn_b = _FakeDbapiConn()
    gate.on_connect(conn_b, record_other)  # consumes the marker -> one recovery
    conn_c = _FakeDbapiConn()
    gate.on_connect(conn_c, record_other)  # no marker present -> no further recovery

    # "pre_ping": handle_error staged during the pre-ping step itself.
    record_pre_ping = _StubRecord()
    conn_d = _FakeDbapiConn()
    gate.on_connect(conn_d, record_pre_ping)
    gate.on_handle_error(SimpleNamespace(is_disconnect=True, is_pre_ping=True, connection=None))
    gate.on_invalidate(conn_d, record_pre_ping, RuntimeError("dead"))
    conn_e = _FakeDbapiConn()
    gate.on_connect(conn_e, record_pre_ping)

    # "post_skip": handle_error staged during ordinary statement execution,
    # on a connection whose most recent decision was a skip.
    record_post_skip = _StubRecord()
    conn_f = _FakeDbapiConn()
    gate.on_connect(conn_f, record_post_skip)
    clock["t"] = 1.0
    gate.wrapped_do_ping(conn_f)  # skip -> last_decision="skip"
    gate.on_handle_error(
        SimpleNamespace(
            is_disconnect=True,
            is_pre_ping=False,
            connection=SimpleNamespace(connection=SimpleNamespace(dbapi_connection=conn_f)),
        )
    )
    gate.on_invalidate(conn_f, record_post_skip, RuntimeError("dead"))
    conn_g = _FakeDbapiConn()
    gate.on_connect(conn_g, record_post_skip)

    # "pre_ping" via a delegated do_ping returning False (not raising) --
    # bypasses handle_error entirely, so the wrapper stages the trigger and
    # records the disconnect itself.
    record_pre_ping_false = _StubRecord()
    conn_h = _FakeDbapiConn()
    gate.on_connect(conn_h, record_pre_ping_false)
    clock["t"] = 200.0  # past the window -- forces delegation
    gate._original_do_ping.result = False
    assert gate.wrapped_do_ping(conn_h) is False  # passed through unmodified, not converted
    gate._original_do_ping.result = True
    gate.on_invalidate(conn_h, record_pre_ping_false, RuntimeError("dead"))
    conn_i = _FakeDbapiConn()
    gate.on_connect(conn_i, record_pre_ping_false)

    disconnects = meter.instruments["omnigent.db.pool.disconnects_total"].calls
    assert [attrs["phase"] for _amount, attrs in disconnects] == [
        "pre_ping",  # from the earlier handle_error(is_pre_ping=True) block
        "post_skip",  # from the earlier post_skip handle_error block
        "pre_ping",  # from this block's delegated do_ping() -> False
    ]

    recoveries = meter.instruments["omnigent.db.pool.recoveries_total"].calls
    assert len(recoveries) == 4
    assert [attrs["trigger"] for _amount, attrs in recoveries] == [
        "other",
        "pre_ping",
        "post_skip",
        "pre_ping",
    ]


def test_checkout_age_histogram_reused_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Only reused (non-fresh) checkouts that reach ``do_ping`` record into the
    age histogram; a bare ``connect`` never does.

    Catches a wrong denominator that would distort every derived
    staleness-distribution metric downstream.
    """
    clock = {"t": 0.0}
    gate, meter = _wired_gate(monkeypatch, window_seconds=5.0, clock=lambda: clock["t"])
    record = _StubRecord()
    conn = _FakeDbapiConn()

    gate.on_connect(conn, record)
    histogram = meter.instruments["omnigent.db.pool.checkout_age_seconds"]
    assert histogram.calls == []  # connect alone: no age sample

    clock["t"] = 1.0
    gate.wrapped_do_ping(conn)  # reused, skip decision -- still an age sample
    assert len(histogram.calls) == 1

    clock["t"] = 10.0
    gate.wrapped_do_ping(conn)  # reused, ping decision -- still an age sample
    assert len(histogram.calls) == 2


# ── live PostgreSQL tests (19-20) ──────────────────────────────────────


def _pg_backend_pid(conn: sa.Connection) -> int:
    return conn.execute(sa.text("SELECT pg_backend_pid()")).scalar()


def _kill_backend_and_wait(control_engine: sa.Engine, pid: int) -> None:
    """
    Terminate exactly one self-identified Postgres backend PID, then poll
    until it's actually gone from ``pg_stat_activity``.

    Exact-PID kill protocol (mandatory for any live test in this file that
    kills a connection): never terminate by ``datname``, application name, a
    broad ``pg_stat_activity`` scan, or any other predicate wider than a
    single PID retrieved via ``SELECT pg_backend_pid()`` on the exact
    connection under test. xdist worker databases are isolated
    (``tests/conftest.py``), but same-worker sibling connections (migration
    helpers, pool siblings) still exist on the same server, so a broad kill
    could take down an unrelated connection and produce a flaky, misleading
    failure elsewhere in the run.
    """
    with control_engine.connect() as control_conn:
        terminated = control_conn.execute(
            sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
        ).scalar()
        assert terminated is True
    # Busy-poll real Postgres server state -- no sleep() anywhere in this
    # suite, per the ratified plan (this bounds real wall-clock elapsed time,
    # it does not fake or drive the gate's own injected clock).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with control_engine.connect() as control_conn:
            still_alive = control_conn.execute(
                sa.text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            ).scalar()
        if not still_alive:
            return
    raise AssertionError(f"backend pid {pid} did not terminate within 5s")


@pytest.fixture()
def postgres_db_uri(db_uri: str) -> str:
    """
    ``db_uri`` if (and only if) it's backed by PostgreSQL; skips otherwise.

    Not a module-level skip: every deterministic test above still runs on
    every backend lane (SQLite by default, MySQL on ``stores-mysql``) --
    only the two live tests below are gated on this fixture.

    See :func:`_kill_backend_and_wait` for the mandatory exact-PID kill
    protocol any test using this fixture to simulate a dead connection must
    follow.
    """
    clear_engine_cache()
    engine = get_or_create_engine(db_uri)
    if engine.dialect.name != "postgresql":
        pytest.skip("live ping-gate tests require a PostgreSQL db_uri (OMNIGENT_TEST_DB_URI)")
    return db_uri


@pytest.mark.databricks
def test_dead_connection_inside_window_one_failure_then_recovery(postgres_db_uri: str) -> None:
    """
    LIVE. A backend killed while its connection sits inside the skip window:
    the next statement fails exactly once through the real managed-session
    rollback+re-raise path, and the checkout after that recovers.

    This is the one test that proves the feature's documented bounded-
    stale-window trade-off actually holds against a real socket -- not
    silently worse.
    """
    gate_engine = sa.create_engine(
        postgres_db_uri,
        pool_pre_ping=True,
        poolclass=sa.pool.QueuePool,
        pool_size=2,
        max_overflow=0,
    )
    control_engine = sa.create_engine(postgres_db_uri, poolclass=sa.pool.NullPool)
    try:
        _install_db_pool_ping_gate(gate_engine, window_seconds=30.0)

        with gate_engine.connect() as conn:
            pid = _pg_backend_pid(conn)
        _kill_backend_and_wait(control_engine, pid)

        session_maker = make_managed_session_maker(gate_engine)
        with pytest.raises(Exception):  # noqa: B017 -- exact DBAPI exception type is driver-specific
            with session_maker() as session:
                session.execute(sa.text("SELECT 1"))

        # The checkout after the one failure recovers transparently.
        with session_maker() as session:
            assert session.execute(sa.text("SELECT 1")).scalar() == 1
    finally:
        gate_engine.dispose()
        control_engine.dispose()


@pytest.mark.databricks
def test_dead_connection_past_window_recovered_by_native_pre_ping(
    postgres_db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    LIVE. A backend killed, then checked out again past the skip window: the
    real ping fires, the real psycopg disconnect classification reaches
    ``handle_error(is_pre_ping=True)``, and native SQLAlchemy machinery
    invalidates the generation and transparently obtains a new backend
    before the caller's statement -- zero caller-visible errors.

    A bare successful ``SELECT 1`` alone would prove nothing here: with
    ``pool_pre_ping=True`` set on ``gate_engine`` independently of this
    feature, plain native pre-ping recovers the exact same way whether or
    not the gate is installed at all. Two extra assertions make this
    genuinely about the gate: (1) ``PingGate.wrapped_do_ping`` is
    class-patched with a call counter *before* installing the gate, so a
    successful run proves native pre-ping actually dispatched through our
    wrapper, not just through native ``pool_pre_ping`` directly; (2) a real
    (non-null) metric sink is wired in, and its
    ``pre_ping_decisions_total``/``disconnects_total`` recordings are
    checked afterward to prove the wrapper delegated (decision="ping") and
    that the disconnect it observed was classified
    ``handle_error(is_pre_ping=True)`` (phase="pre_ping") -- the contract's
    central claim, made observable rather than merely inferred from the
    final return value.

    Strongest evidence in this suite that the contract's central claim --
    delegate to native disconnect classification, never hand-roll it --
    holds against the real, installed driver rather than a fake DBAPI's
    approximation of it.
    """
    from omnigent.runtime import telemetry

    clock = {"t": 0.0}
    wrapper_calls = {"n": 0}
    original_method = PingGate.wrapped_do_ping

    def _counting_wrapped_do_ping(self: PingGate, dbapi_connection: object) -> bool:
        wrapper_calls["n"] += 1
        return original_method(self, dbapi_connection)

    monkeypatch.setattr(PingGate, "wrapped_do_ping", _counting_wrapped_do_ping)

    fake_meter = _FakeMeter()
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr("opentelemetry.metrics.get_meter", lambda name: fake_meter)

    gate_engine = sa.create_engine(
        postgres_db_uri,
        pool_pre_ping=True,
        poolclass=sa.pool.QueuePool,
        pool_size=2,
        max_overflow=0,
    )
    control_engine = sa.create_engine(postgres_db_uri, poolclass=sa.pool.NullPool)
    try:
        _install_db_pool_ping_gate(gate_engine, window_seconds=5.0, clock=lambda: clock["t"])
        telemetry.instrument_db_pool_ping_gate(gate_engine)

        with gate_engine.connect() as conn:
            pid = _pg_backend_pid(conn)
        _kill_backend_and_wait(control_engine, pid)

        clock["t"] = 100.0  # past the window -- forces the real ping
        with gate_engine.connect() as conn:
            assert conn.execute(sa.text("SELECT 1")).scalar() == 1

        assert wrapper_calls["n"] >= 1  # native pre-ping actually dispatched through our wrapper

        decisions = fake_meter.instruments["omnigent.db.pool.pre_ping_decisions_total"].calls
        assert any(attrs.get("decision") == "ping" for _amount, attrs in decisions)

        disconnects = fake_meter.instruments["omnigent.db.pool.disconnects_total"].calls
        assert any(attrs.get("phase") == "pre_ping" for _amount, attrs in disconnects)
    finally:
        gate_engine.dispose()
        control_engine.dispose()


def test_both_engine_factories_instrument_the_ping_gate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Every engine factory that can install the gate must also wire its metric
    sinks.

    ``_create_engine`` installs the gate for an eligible PostgreSQL URI, and
    BOTH ``get_or_create_engine`` and ``get_or_create_conversation_engine``
    (the separate Agent Platform engine) route through it. Only the former
    used to call ``instrument_db_pool_ping_gate``, so an AP PostgreSQL engine
    ran its gate on null sinks and emitted none of the four instruments --
    making ``docs/db-pre-ping-gate.md``'s unconditional metrics claim false
    for a supported surface.

    Asserted against sqlite URIs so no real driver or server is needed: the
    instrumentation call must be unconditional at both sites (it no-ops
    internally when no gate is installed), which is exactly the property
    that was missing.
    """
    from omnigent.db import utils as db_utils

    calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.runtime.telemetry.instrument_db_pool_ping_gate",
        lambda engine: calls.append("gate"),
    )
    monkeypatch.setattr(
        "omnigent.runtime.telemetry.instrument_sqlalchemy_engine",
        lambda engine: calls.append("tracing"),
    )

    clear_engine_cache()
    db_utils.get_or_create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    assert calls.count("gate") == 1, calls

    calls.clear()
    db_utils.get_or_create_conversation_engine(f"sqlite:///{tmp_path / 'ap.db'}")
    assert calls.count("gate") == 1, calls
    clear_engine_cache()


class _RaisingLastDecisionState:
    """Gate state whose ``last_decision`` read raises.

    Reached through the WeakKeyDictionary, so the state lookup itself
    succeeds and the failure lands on a plain attribute read in the listener
    body — a spot no per-statement ``suppress`` in the module covers. Only
    the ``_fail_safe_listener`` decorator stands between it and the pool.
    """

    last_success = 0.0

    @property
    def last_decision(self) -> str:
        raise RuntimeError("state accessor broke")


def _gate_with_raising_state() -> tuple[PingGate, _FakeDbapiConn]:
    """Build a gate whose stored state raises on ``last_decision``.

    :returns: The gate and the connection its poisoned state is keyed by.
    """
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=5.0,
        clock=lambda: 0.0,
        original_do_ping=_RecordingDoPing(),
    )
    conn = _FakeDbapiConn()
    gate._store_state(conn, _RaisingLastDecisionState())  # type: ignore[arg-type]
    return gate, conn


def test_listener_body_failure_is_contained_only_by_the_decorator() -> None:
    """The structural net must contain a failure nothing else guards.

    ``on_checkin`` reads ``state.last_decision`` directly. That read sits
    inside no ``contextlib.suppress`` — the decorator is the only thing
    holding it — so this is the case that distinguishes "the net exists"
    from "the helpers happen to suppress this one". Undecorating via
    ``__wrapped__`` is asserted to raise, which is what makes the decorated
    call's silence meaningful rather than vacuous.
    """
    gate, conn = _gate_with_raising_state()
    record = _StubRecord()

    # The decorated listener contains it.
    assert gate.on_checkin(conn, record) is None  # type: ignore[arg-type]

    # The same body, undecorated, does not — proving the decorator is load-bearing.
    with pytest.raises(RuntimeError, match="state accessor broke"):
        type(gate).on_checkin.__wrapped__(gate, conn, record)  # type: ignore[attr-defined]


def test_handle_error_body_failure_is_contained_only_by_the_decorator() -> None:
    """Same structural check on the ``handle_error`` listener.

    ``on_handle_error`` reads ``state.last_decision`` to classify a
    post-skip disconnect. A failure there must not reclassify or replace the
    real DB exception being handled.
    """
    gate, conn = _gate_with_raising_state()
    ctx = SimpleNamespace(
        is_disconnect=True,
        is_pre_ping=False,
        connection=SimpleNamespace(connection=SimpleNamespace(dbapi_connection=conn)),
    )

    assert gate.on_handle_error(ctx) is None  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="state accessor broke"):
        type(gate).on_handle_error.__wrapped__(gate, ctx)  # type: ignore[attr-defined]


def test_listener_net_survives_a_raising_logger() -> None:
    """The net's own failure report must not resurrect the exception.

    ``_fail_safe_listener`` catches a listener failure and then reports it.
    A logging handler (or formatter) that raises turns that swallowed
    bookkeeping failure back into a live exception on the connection path —
    the guard defeating itself, which no amount of per-site suppression
    inside the listeners can fix.

    The fault is injected as a real raising handler rather than by patching
    ``_logger.debug``: ``debug`` is inherited from ``logging.Logger``, so
    setting it on the instance (even via ``monkeypatch``) leaves the
    resolved bound method behind as an instance attribute that shadows the
    class for the rest of the process.
    """
    gate, conn = _gate_with_raising_state()

    class _RaisingHandler(logging.Handler):
        """Fails on emit, the way a broken formatter or sink would."""

        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("logging handler broke")

    logger = ping_gate_module._logger
    handler = _RaisingHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        # The report path really does reach the raising handler — otherwise
        # the assertion below would hold for want of any fault at all.
        with pytest.raises(RuntimeError, match="logging handler broke"):
            logger.debug("probe")

        # Listener body fails AND the report fails; neither may reach the pool.
        assert gate.on_checkin(conn, _StubRecord()) is None  # type: ignore[arg-type]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert handler not in logger.handlers
    assert logger.level == previous_level


def test_wrapper_bookkeeping_failure_falls_through_to_the_real_ping() -> None:
    """An internal bookkeeping failure must reach the real ping, not escape.

    Fault-injects by deleting ``dialect_name`` -- a field the wrapper reads
    only to label metrics -- so the failure originates inside the wrapper's
    own decision region rather than in a listener. Two things must hold:
    nothing propagates into checkout, and the connection is still actually
    verified. Returning ``True`` here would be worse than raising, because
    it would hand out an unchecked connection.

    The connection is inside the skip window, so a wrapper that swallowed
    the failure but kept the skip decision would return ``True`` without
    calling the delegate -- which this test also rejects.
    """
    recorder = _RecordingDoPing()
    clock = {"t": 0.0}
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=5.0,
        clock=lambda: clock["t"],
        original_do_ping=recorder,
    )
    conn = _FakeDbapiConn()
    gate.on_connect(conn, _StubRecord())  # type: ignore[arg-type]
    clock["t"] = 1.0  # well inside the window: the gate would otherwise skip

    del gate.dialect_name  # type: ignore[attr-defined]

    result = gate.wrapped_do_ping(conn)

    assert result is True
    assert len(recorder.calls) == 1, (
        "bookkeeping failure did not fall through to the real ping — the "
        "gate skipped verification or raised into checkout"
    )


def test_wrapper_reports_unverified_when_the_delegate_cannot_be_recovered() -> None:
    """With no recoverable delegate the gate must not claim health.

    The degenerate case of the same region: if even the real ``do_ping``
    cannot be reached there is nothing to fall through to. Returning
    ``False`` costs a reconnect (SQLAlchemy invalidates and replaces);
    returning ``True`` would hand out a connection nothing checked.
    """
    gate = PingGate(
        dialect_name="postgresql",
        window_seconds=5.0,
        clock=lambda: 0.0,
        original_do_ping=_RecordingDoPing(),
    )
    conn = _FakeDbapiConn()
    del gate._original_do_ping  # type: ignore[attr-defined]

    assert gate.wrapped_do_ping(conn) is False
