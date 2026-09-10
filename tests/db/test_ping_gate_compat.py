"""
Compatibility tests for ``omnigent.db.ping_gate`` against the installed
SQLAlchemy version.

These guard the loose ``sqlalchemy>=2.0,<3`` pin (``pyproject.toml``,
installed 2.0.50 at authoring time): every test here fails if a future
SQLAlchemy 2.x release breaks the specific internals this feature relies on
(a fresh dialect instance per engine, native pre-ping invoking the instance's
``do_ping``, and ``QueuePool.recreate()``/``dispose()`` retaining that
instance). They are the safety net every other test in this plan, and the
wrapper implementation itself, is checked against.

Technique: real ``postgresql+psycopg`` dialect with a fake DBAPI connection
supplied via ``create_engine(..., creator=...)`` -- never a real socket.
Constructing that engine still imports the real driver, so the three tests
that do are marked ``@pytest.mark.databricks`` and run only on the lane
installing that extra, matching the precedent in ``tests/db/test_utils.py``.
``test_gate_off_engine_kwargs_byte_identical`` monkeypatches
``create_engine`` away entirely, needs no driver, and stays unmarked so the
highest-consequence regression (accidental default-on) is checked on every
lane.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from omnigent.db.ping_gate import PingGate, _install_db_pool_ping_gate


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


def _fake_psycopg_engine() -> Any:
    """A postgresql+psycopg engine whose connections are fakes (no socket)."""
    engine = create_engine(
        "postgresql+psycopg://fake/db",
        creator=lambda: _FakeConnection(),
        pool_pre_ping=True,
    )
    # Skip real dialect.initialize() (server-version detection etc. via SQL
    # our fake cursor can't answer) -- irrelevant to what these tests check.
    engine.dialect.initialize = lambda dbapi_conn: None  # type: ignore[method-assign]
    return engine


@pytest.mark.databricks
def test_native_pre_ping_invokes_instance_do_ping_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A checkout on a reused connection must hit the installed wrapper via
    SQLAlchemy's *native* pre-ping dispatch (never called directly by us).

    Catches a future 2.x caching ``do_ping`` at class level, or bypassing the
    instance attribute entirely -- the gate would go silently dead while
    ``pool_use_lifo=True`` stays on, turning the "optimization" into a pure
    ordering change with zero benefit and no visible signal.

    The counting wrap is applied to ``PingGate.wrapped_do_ping`` at the
    *class* level, before ``_install_db_pool_ping_gate`` ever runs -- not by
    overwriting ``engine.dialect.do_ping`` ourselves afterward. If install
    stopped actually assigning ``dialect.do_ping = gate.wrapped_do_ping``
    (e.g. degraded to a no-op that only attaches the gate marker), native
    pre-ping would never reach this patched method and ``calls["n"]`` would
    stay ``0``, so this genuinely proves the wrapper is installed.

    Also asserts the gate registers no ``checkout`` listener (contract: all
    skip/ping decisions happen inside the wrapped ``do_ping``, never in a
    checkout hook) -- a future change accidentally adding checkout-hook
    logic would run on every checkout, including fresh ones that never
    reach ``do_ping``, changing behavior no deployment opted into.
    """
    engine = _fake_psycopg_engine()
    checkout_listeners_before = list(engine.pool.dispatch.checkout)

    calls = {"n": 0}
    original_method = PingGate.wrapped_do_ping

    def _counting_wrapped_do_ping(self: PingGate, dbapi_connection: object) -> bool:
        calls["n"] += 1
        return original_method(self, dbapi_connection)

    monkeypatch.setattr(PingGate, "wrapped_do_ping", _counting_wrapped_do_ping)
    _install_db_pool_ping_gate(engine, window_seconds=3.0)

    assert list(engine.pool.dispatch.checkout) == checkout_listeners_before

    engine.raw_connection().close()
    engine.raw_connection().close()

    assert calls["n"] >= 1


@pytest.mark.databricks
def test_sibling_engine_dialect_do_ping_is_class_descriptor() -> None:
    """
    Installing the gate on one engine must never leak the wrap onto a
    sibling engine's dialect instance.

    Catches a wrap that leaks across engines -- would silently gate
    sqlite/D1/a second Postgres engine that was never opted in.
    """
    gated_engine = _fake_psycopg_engine()
    ungated_engine = _fake_psycopg_engine()

    _install_db_pool_ping_gate(gated_engine, window_seconds=3.0)

    assert "do_ping" not in vars(ungated_engine.dialect)
    assert ungated_engine.dialect.do_ping.__func__ is type(ungated_engine.dialect).do_ping
    assert "do_ping" in vars(gated_engine.dialect)


@pytest.mark.databricks
def test_dispose_recreate_no_double_wrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``engine.dispose()`` + reconnect must not double-wrap or lose the gate,
    and each real ping must invoke the underlying real ``do_ping`` exactly
    once.

    Catches a future ``QueuePool.recreate()`` minting a new dialect instance
    (breaking the premise the wrap relies on), or a double-install
    corrupting age logic via duplicate listeners.

    Every checkout below that is *supposed* to skip is followed by an
    assertion that the real (counted) ``do_ping`` did NOT fire. Without this,
    a checkout pattern where the window has always elapsed between checkouts
    can't distinguish "the gate wrapped and skipped" from "the gate never
    wrapped anything and every checkout just always real-pings" -- both
    produce the same call count. The in-window re-checkouts here are what
    make this test fail if install degrades to a no-op.

    Also drives the REAL native pre-ping/invalidate/reconnect lifecycle
    (never a manual ``on_invalidate()`` simulation) for a delegated
    ``do_ping`` that returns ``False`` without raising: this is the one
    disconnect path ``handle_error`` never sees at all (contract Sec 9/10),
    so it's this compat suite's job to hard-fail if a future SQLAlchemy 2.x
    release changes how that specific case gets invalidated or starts
    routing it through ``handle_error`` too (which would double-count).
    """
    from sqlalchemy.dialects.postgresql.psycopg import (
        PGDialect_psycopg,
        _PGDialect_common_psycopg,
    )

    # Patch the class that DEFINES do_ping, not PGDialect_psycopg, which only
    # inherits it: assigning on the subclass plants a permanent shadow that
    # hides every later patch of the owner for the rest of the worker.
    assert "do_ping" not in vars(PGDialect_psycopg)
    real_do_ping = _PGDialect_common_psycopg.do_ping
    call_count = {"n": 0}

    def _counting_real_do_ping(self: object, dbapi_connection: object) -> bool:
        call_count["n"] += 1
        return real_do_ping(self, dbapi_connection)

    monkeypatch.setattr(_PGDialect_common_psycopg, "do_ping", _counting_real_do_ping)
    try:
        engine = _fake_psycopg_engine()
        clock = {"t": 0.0}
        gate = _install_db_pool_ping_gate(engine, window_seconds=1.0, clock=lambda: clock["t"])

        engine.raw_connection().close()  # fresh connect; no ping
        clock["t"] += 10.0  # past the window
        engine.raw_connection().close()  # real ping #1
        assert call_count["n"] == 1

        engine.raw_connection().close()  # same clock: still inside the window -- must skip
        assert call_count["n"] == 1

        engine.dispose()
        # QueuePool.recreate() retains the same dialect instance -- no
        # re-install needed, and re-installing must be idempotent.
        gate_again = _install_db_pool_ping_gate(
            engine, window_seconds=1.0, clock=lambda: clock["t"]
        )
        assert gate_again is gate

        engine.raw_connection().close()  # fresh connect after dispose; no ping
        clock["t"] += 10.0
        engine.raw_connection().close()  # real ping #2
        assert call_count["n"] == 2

        engine.raw_connection().close()  # inside the window again -- must skip
        assert call_count["n"] == 2
    finally:
        monkeypatch.setattr(_PGDialect_common_psycopg, "do_ping", real_do_ping)

    ping_returns_false = {"value": False}

    def _sometimes_false_do_ping(self: object, dbapi_connection: object) -> bool:
        if ping_returns_false["value"]:
            return False
        return real_do_ping(self, dbapi_connection)

    monkeypatch.setattr(_PGDialect_common_psycopg, "do_ping", _sometimes_false_do_ping)
    try:
        engine2 = _fake_psycopg_engine()
        clock2 = {"t": 0.0}
        _install_db_pool_ping_gate(engine2, window_seconds=1.0, clock=lambda: clock2["t"])

        fake_meter = _FakeMeter()
        monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
        monkeypatch.setattr("opentelemetry.metrics.get_meter", lambda name: fake_meter)
        from omnigent.runtime import telemetry

        telemetry.instrument_db_pool_ping_gate(engine2)

        engine2.raw_connection().close()  # fresh connect; no ping
        clock2["t"] += 10.0  # past the window -- forces delegation
        ping_returns_false["value"] = True
        # Native checkout -> pre-ping -> our wrapper delegates -> real
        # do_ping returns False -> SQLAlchemy raises InvalidatePoolError ->
        # native invalidate + reconnect, all synchronously within this call.
        engine2.raw_connection().close()
        ping_returns_false["value"] = False

        disconnects = fake_meter.instruments["omnigent.db.pool.disconnects_total"].calls
        assert [attrs["phase"] for _amount, attrs in disconnects] == ["pre_ping"]
        recoveries = fake_meter.instruments["omnigent.db.pool.recoveries_total"].calls
        assert [attrs["trigger"] for _amount, attrs in recoveries] == ["pre_ping"]
    finally:
        monkeypatch.setattr(_PGDialect_common_psycopg, "do_ping", real_do_ping)
        engine2.dispose()

    # No subclass shadow may survive this test: a planted PGDialect_psycopg
    # entry silently bypasses every later patch of the defining owner.
    assert "do_ping" not in vars(PGDialect_psycopg)
    assert PGDialect_psycopg.do_ping is real_do_ping


def test_gate_off_engine_kwargs_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    With the gate unset or explicitly "0", ``_create_engine``'s kwargs to
    ``create_engine`` must be byte-identical to today's -- no
    ``pool_use_lifo`` key present at all -- and no gate must be installed.

    Catches accidental default-on, or any kwarg drift: this is the single
    highest-consequence regression this feature could introduce, silently
    changing behavior for every existing deployment that never opts in. No
    psycopg dependency -- runs on every lane.

    Pins the *exact* expected kwargs (not just self-consistency between the
    unset and "0" paths) against the ratified baseline
    (``omnigent/db/utils.py``'s ``pool_pre_ping``/``pool_recycle``/
    ``pool_size``/``max_overflow``/``pool_timeout`` values), and asserts the
    installer function itself is never even called -- not merely that a gate
    attribute happens to be absent from a ``MagicMock`` engine (whose
    ``getattr`` would auto-vivify any attribute name and never actually
    report absence).
    """
    from omnigent.db.utils import (
        _SERVER_POOL_RECYCLE_SECONDS,
        clear_engine_cache,
        get_or_create_engine,
    )

    expected_kwargs = {
        "pool_pre_ping": True,
        "pool_recycle": _SERVER_POOL_RECYCLE_SECONDS,
        "pool_size": 200,
        "max_overflow": 20,
        "pool_timeout": 10,
    }

    def _capture(monkeypatch_ctx: pytest.MonkeyPatch) -> tuple[dict[str, Any], list[Any]]:
        captured: dict[str, Any] = {}
        install_calls: list[Any] = []
        mock_engine = MagicMock()

        def _capturing_create_engine(uri: str, **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return mock_engine

        def _spying_install(*args: Any, **kwargs: Any) -> Any:
            install_calls.append((args, kwargs))
            raise AssertionError("_install_db_pool_ping_gate must not be called when disabled")

        monkeypatch_ctx.setattr("omnigent.db.utils.create_engine", _capturing_create_engine)
        monkeypatch_ctx.setattr("omnigent.db.utils._run_migrations", lambda engine, db_uri: None)
        monkeypatch_ctx.setattr("omnigent.db.utils._install_db_pool_ping_gate", _spying_install)
        return captured, install_calls

    clear_engine_cache()
    monkeypatch.delenv("OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS", raising=False)
    unset_kwargs, unset_install_calls = _capture(monkeypatch)
    get_or_create_engine("postgresql://user:pass@localhost/testdb")
    assert unset_kwargs == expected_kwargs
    assert unset_install_calls == []

    clear_engine_cache()
    monkeypatch.setenv("OMNIGENT_DB_PING_SKIP_WINDOW_SECONDS", "0")
    zero_kwargs, zero_install_calls = _capture(monkeypatch)
    get_or_create_engine("postgresql://user:pass@localhost/testdb2")
    assert zero_kwargs == expected_kwargs
    assert zero_install_calls == []
