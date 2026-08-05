#!/usr/bin/env python3
"""
One-time before/after query-cost benchmark for ``build_policy_engine`` (C1).

Not part of the CI-committed test suite. The durable regression gate is
``tests/stores/test_policy_engine_query_counts.py``; this script exists only
to produce the before/after evidence for the PR description. Run once
against pre-C1 HEAD and once post-C1 (same scenario, same dialect) and
compare the printed counts.

Records, for a fixed scenario (a sub-agent session with a subtree
cost-budget policy present, so both usage views compute):

- Application cursor-execute statement counts (unfiltered — unlike the CI
  gate, PRAGMA statements are NOT excluded here; this script wants full
  visibility, not a stable cross-dialect assertion).
- Pool-level ``connect`` / ``checkout`` / ``checkin`` event counts.
- Managed-session ``commit`` / ``rollback`` counts.
- A ``do_ping`` counter (``pool_pre_ping`` dialect-level invocations).

Usage:
    uv run python scripts/bench_policy_engine_queries.py [--db-uri URI]

With no ``--db-uri``, uses an ephemeral SQLite file. Pass a Postgres/MySQL
URI to benchmark that dialect instead.
"""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import event

from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.types import AgentSpec, FunctionPolicySpec, FunctionRef
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

_SUBAGENT_COST_POLICY = FunctionPolicySpec(
    name="subagent_budget",
    on=None,
    function=FunctionRef(
        path="omnigent.policies.builtins.cost.subagent_cost_budget",
        arguments={"max_cost_usd": 1000.0},
    ),
)


@dataclass
class _Counters:
    cursor_executes: int = 0
    pool_connect: int = 0
    pool_checkout: int = 0
    pool_checkin: int = 0
    session_commit: int = 0
    session_rollback: int = 0
    do_ping: int = 0


def _instrument(engine, counters: _Counters) -> None:
    """Attach best-effort SQLAlchemy event listeners feeding *counters*."""

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        _conn, _cursor, _statement, _parameters, _context, _executemany
    ) -> None:
        counters.cursor_executes += 1

    @event.listens_for(engine, "connect")
    def _connect(_dbapi_conn, _connection_record) -> None:
        counters.pool_connect += 1

    @event.listens_for(engine, "checkout")
    def _checkout(_dbapi_conn, _connection_record, _connection_proxy) -> None:
        counters.pool_checkout += 1

    @event.listens_for(engine, "checkin")
    def _checkin(_dbapi_conn, _connection_record) -> None:
        counters.pool_checkin += 1

    @event.listens_for(engine, "commit")
    def _commit(_conn) -> None:
        counters.session_commit += 1

    @event.listens_for(engine, "rollback")
    def _rollback(_conn) -> None:
        counters.session_rollback += 1

    original_do_ping = engine.dialect.do_ping

    def _counting_do_ping(dbapi_connection):
        counters.do_ping += 1
        return original_do_ping(dbapi_connection)

    engine.dialect.do_ping = _counting_do_ping


def _make_spec(name: str = "bench-agent") -> AgentSpec:
    return AgentSpec(spec_version=1, name=name)


def _seed_scenario(
    conversation_store: SqlAlchemyConversationStore,
) -> str:
    """
    Build a sub-agent-with-active-root scenario and return the conversation
    id to build the engine for.

    Matches the CI query-count test's "active child + active root, both
    usage views needed" scenario — the case that most directly exercises
    C1's consolidation (batched policy load, single tree walk feeding both
    the session-wide and subtree usage seeds, tree-sourced root state).
    """
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.set_session_usage(root.id, {"total_cost_usd": 0.10})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.05})
    return child.id


def run(db_uri: str) -> _Counters:
    """Run the fixed scenario against *db_uri* and return the counters."""
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    conversation_id = _seed_scenario(conversation_store)

    engine = get_or_create_engine(db_uri)
    counters = _Counters()
    _instrument(engine, counters)

    build_policy_engine(
        spec=_make_spec(),
        conversation_id=conversation_id,
        conversation_store=conversation_store,
        policy_store=policy_store,
        default_policies=[_SUBAGENT_COST_POLICY],
    )

    return counters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-uri",
        default=None,
        help="SQLAlchemy URI to benchmark against. Defaults to an ephemeral SQLite file.",
    )
    args = parser.parse_args()

    if args.db_uri:
        db_uri = args.db_uri
    else:
        tmp_dir = tempfile.mkdtemp(prefix="policy_engine_bench_")
        db_uri = f"sqlite:///{Path(tmp_dir) / 'bench.db'}"

    counters = run(db_uri)
    print(f"db_uri: {db_uri}")
    print(f"cursor_executes (unfiltered, incl. PRAGMA): {counters.cursor_executes}")
    print(f"pool_connect: {counters.pool_connect}")
    print(f"pool_checkout: {counters.pool_checkout}")
    print(f"pool_checkin: {counters.pool_checkin}")
    print(f"session_commit: {counters.session_commit}")
    print(f"session_rollback: {counters.session_rollback}")
    print(f"do_ping: {counters.do_ping}")


if __name__ == "__main__":
    main()
