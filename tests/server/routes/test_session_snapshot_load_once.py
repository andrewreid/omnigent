"""Regression test: the session snapshot reuses its conversation row.

GET /v1/sessions/{id} was measured at ~19 queries in production — more
than the list endpoint spends on a whole page — because the subtree-cost
recompute (load_session_usage) re-read the conversation the handler
already held before walking the spawn tree.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import event

from omnigent.db.utils import _engine_cache
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@contextmanager
def _count_conversation_selects(engine):
    counts: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().startswith("SELECT")
            and "FROM conversations" in statement
            and "conversation_items" not in statement
            and "conversation_labels" not in statement
        ):
            counts.append(statement)

    event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        yield counts
    finally:
        event.remove(engine, "before_cursor_execute", _on_exec)


@pytest.mark.asyncio
async def test_snapshot_subtree_usage_reuses_conversation_row(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    The snapshot's subtree-usage recompute derives the tree root from the
    row in hand instead of re-reading the conversation. Remaining SELECTs:
    the handler's own row, the tree page scan, and the two deliberate
    child-indicator / liveness id scans.
    """
    from tests.server.helpers import create_test_session

    snap = await create_test_session(client, title="snapshot-load-once")
    sid = snap["id"]
    engine = _engine_cache[db_uri]
    await client.get(f"/v1/sessions/{sid}")  # warm

    with _count_conversation_selects(engine) as selects:
        resp = await client.get(f"/v1/sessions/{sid}")

    assert resp.status_code == 200
    assert len(selects) <= 4, f"expected <=4 conversations SELECTs, got {len(selects)}"


@pytest.mark.asyncio
async def test_snapshot_subtree_usage_aggregates_child_spend(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The reused-root recompute still sums the whole subtree: a
    sub-agent's spend shows up in the parent snapshot's total."""
    from tests.server.helpers import create_test_session

    snap = await create_test_session(client, title="subtree-agg")
    sid = snap["id"]
    store = SqlAlchemyConversationStore(db_uri)
    child = store.create_conversation(
        kind="sub_agent", parent_conversation_id=sid, title="agg:child"
    )
    store.set_session_usage(sid, {"total_cost_usd": 0.10})
    store.set_session_usage(child.id, {"total_cost_usd": 0.05})

    resp = await client.get(f"/v1/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["total_cost_usd"] == pytest.approx(0.15)
