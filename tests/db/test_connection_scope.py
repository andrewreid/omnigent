"""Tests for request-scoped DB connection reuse (db_connection_scope)."""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, event, text

from omnigent.db.utils import (
    _connection_scope_var,
    _ConnectionScope,
    db_connection_scope,
    make_managed_session_maker,
)


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A file-backed SQLite engine with a scratch table."""
    eng = create_engine(
        f"sqlite:///{tmp_path / 'scope.db'}",
        connect_args={"check_same_thread": False},
    )
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)"))
    yield eng
    eng.dispose()


def _count_checkouts(engine: Engine) -> list[object]:
    """Attach a pool-checkout counter; returns the (mutable) event list."""
    checkouts: list[object] = []

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn: object, conn_record: object, conn_proxy: object) -> None:
        checkouts.append(dbapi_conn)

    return checkouts


def test_scope_reuses_one_connection_across_sessions(engine: Engine) -> None:
    """Five managed sessions inside one scope = one pool checkout."""
    session_maker = make_managed_session_maker(engine)
    checkouts = _count_checkouts(engine)

    with db_connection_scope():
        for i in range(5):
            with session_maker() as session:
                session.execute(
                    text("INSERT INTO kv (k, v) VALUES (:k, :v)"),
                    {"k": f"k{i}", "v": "x"},
                )

    assert len(checkouts) == 1
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM kv")).scalar_one()
    assert count == 5


def test_no_scope_checks_out_per_session(engine: Engine) -> None:
    """Without a scope, each managed session pays its own checkout."""
    session_maker = make_managed_session_maker(engine)
    checkouts = _count_checkouts(engine)

    for i in range(3):
        with session_maker() as session:
            session.execute(
                text("INSERT INTO kv (k, v) VALUES (:k, :v)"),
                {"k": f"k{i}", "v": "x"},
            )

    assert len(checkouts) == 3


def test_per_call_commit_is_visible_while_scope_open(engine: Engine) -> None:
    """
    Each managed session commits a real transaction, not a savepoint:
    a separate connection sees the row while the scope is still open.
    """
    session_maker = make_managed_session_maker(engine)

    with db_connection_scope():
        with session_maker() as session:
            session.execute(text("INSERT INTO kv (k, v) VALUES ('a', '1')"))
        with engine.connect() as other:
            value = other.execute(text("SELECT v FROM kv WHERE k = 'a'")).scalar_one()
        assert value == "1"


def test_rollback_on_exception_and_scope_still_usable(engine: Engine) -> None:
    """An exception rolls back only its own session; the scope survives."""
    session_maker = make_managed_session_maker(engine)

    with db_connection_scope():
        with pytest.raises(RuntimeError, match="boom"):
            with session_maker() as session:
                session.execute(text("INSERT INTO kv (k, v) VALUES ('bad', '1')"))
                raise RuntimeError("boom")
        with session_maker() as session:
            session.execute(text("INSERT INTO kv (k, v) VALUES ('good', '1')"))

    with engine.connect() as conn:
        keys = {row[0] for row in conn.execute(text("SELECT k FROM kv"))}
    assert keys == {"good"}


def test_overlapping_sessions_fall_back_to_pool(engine: Engine) -> None:
    """
    While the scope's connection is lent out, a nested managed session
    checks out its own connection instead of sharing.
    """
    session_maker = make_managed_session_maker(engine)
    checkouts = _count_checkouts(engine)

    with db_connection_scope():
        with session_maker() as outer:
            outer.execute(text("INSERT INTO kv (k, v) VALUES ('outer', '1')"))
            # Inner call reads only: SQLite allows one writer at a time, and
            # the point here is connection lending, not lock contention.
            with session_maker() as inner:
                inner.execute(text("SELECT count(*) FROM kv")).scalar_one()

    assert len(checkouts) == 2


def test_split_engines_use_separate_connections(tmp_path: Path) -> None:
    """Two engines in one scope each hold their own connection (split-DB)."""
    eng_a = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    eng_b = create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    for eng in (eng_a, eng_b):
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)"))
    maker_a = make_managed_session_maker(eng_a)
    maker_b = make_managed_session_maker(eng_b)
    checkouts_a = _count_checkouts(eng_a)
    checkouts_b = _count_checkouts(eng_b)

    with db_connection_scope():
        for i in range(3):
            with maker_a() as session:
                session.execute(text("INSERT INTO kv (k, v) VALUES (:k, 'a')"), {"k": f"k{i}"})
            with maker_b() as session:
                session.execute(text("INSERT INTO kv (k, v) VALUES (:k, 'b')"), {"k": f"k{i}"})

    assert len(checkouts_a) == 1
    assert len(checkouts_b) == 1
    with eng_a.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM kv")).scalar_one() == 3
    with eng_b.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM kv")).scalar_one() == 3
    eng_a.dispose()
    eng_b.dispose()


def test_closed_scope_falls_back_to_pool(engine: Engine) -> None:
    """A caller holding a closed scope gets a plain pooled session."""
    session_maker = make_managed_session_maker(engine)
    scope = _ConnectionScope()
    scope.close()
    token = _connection_scope_var.set(scope)
    try:
        with session_maker() as session:
            session.execute(text("INSERT INTO kv (k, v) VALUES ('late', '1')"))
    finally:
        _connection_scope_var.reset(token)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT v FROM kv WHERE k = 'late'")).scalar_one() == "1"


def test_nested_scope_is_noop(engine: Engine) -> None:
    """An inner db_connection_scope reuses the outer scope's connection."""
    session_maker = make_managed_session_maker(engine)
    checkouts = _count_checkouts(engine)

    with db_connection_scope():
        with session_maker() as session:
            session.execute(text("INSERT INTO kv (k, v) VALUES ('one', '1')"))
        with db_connection_scope():
            with session_maker() as session:
                session.execute(text("INSERT INTO kv (k, v) VALUES ('two', '1')"))

    assert len(checkouts) == 1


def test_scope_shared_across_threads_sequentially(engine: Engine) -> None:
    """
    Context propagates into worker threads (the asyncio.to_thread model):
    sequential store calls from different threads reuse one connection.
    """
    session_maker = make_managed_session_maker(engine)
    checkouts = _count_checkouts(engine)

    def _store_call(key: str) -> None:
        with session_maker() as session:
            session.execute(text("INSERT INTO kv (k, v) VALUES (:k, 'x')"), {"k": key})

    with db_connection_scope():
        for i in range(3):
            ctx = contextvars.copy_context()
            thread = threading.Thread(target=ctx.run, args=(_store_call, f"k{i}"))
            thread.start()
            thread.join()

    assert len(checkouts) == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM kv")).scalar_one() == 3


def test_immediate_sessions_work_inside_scope(engine: Engine) -> None:
    """BEGIN IMMEDIATE sessions run correctly on the scope's connection."""
    immediate_maker = make_managed_session_maker(engine, immediate=True)
    checkouts = _count_checkouts(engine)

    with db_connection_scope():
        for i in range(2):
            with immediate_maker() as session:
                session.execute(text("INSERT INTO kv (k, v) VALUES (:k, 'x')"), {"k": f"k{i}"})

    assert len(checkouts) == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM kv")).scalar_one() == 2


def test_scope_close_returns_connection_to_pool(engine: Engine) -> None:
    """After the scope exits, its connection is checked back in."""
    session_maker = make_managed_session_maker(engine)
    checkins: list[object] = []

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn: object, conn_record: object) -> None:
        checkins.append(dbapi_conn)

    with db_connection_scope():
        with session_maker() as session:
            session.execute(text("SELECT 1"))
        assert len(checkins) == 0

    assert len(checkins) == 1
