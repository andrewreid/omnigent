"""Regression tests: the event hot path loads the conversation once.

POST /v1/sessions/{id}/events fires per streamed chunk during active
turns (~98% of server query volume), so redundant conversation/ACL
reloads multiply hard. These pin the load-once behavior: the handler's
top-of-request row is threaded into runner routing and the usage
subtree recompute instead of each helper re-reading it.
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
    """Count SELECTs on the conversations table (not items/labels)."""
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
async def test_status_event_routes_via_binding_read_and_still_forwards(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pinned session's status event resolves its runner via a fresh
    single-column binding read — one conversations SELECT per event, not
    one per routing lookup — and the forward itself demonstrably runs
    (the count assertion alone would stay green if forwarding vanished).
    """
    from omnigent.server.routes import sessions as sessions_pkg

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="event-hot-path")
    store.set_runner_id(conv.id, "runner_bench")

    payload = {"type": "external_session_status", "data": {"status": "idle"}}
    await client.post(f"/v1/sessions/{conv.id}/events", json=payload)  # warm

    forwarded: list[dict] = []
    real_forward = sessions_pkg._forward_session_change_to_runner

    async def _recording_forward(session_id, runner_router, event, **kwargs):
        forwarded.append(event)
        return await real_forward(session_id, runner_router, event, **kwargs)

    monkeypatch.setattr(sessions_pkg, "_forward_session_change_to_runner", _recording_forward)

    engine = _engine_cache[db_uri]
    with _count_conversation_selects(engine) as selects:
        resp = await client.post(f"/v1/sessions/{conv.id}/events", json=payload)

    assert resp.status_code == 202, resp.text
    assert len(selects) <= 1, f"expected <=1 conversations SELECT, got {len(selects)}"
    assert forwarded and forwarded[0]["type"] == "external_session_status"


@pytest.mark.asyncio
async def test_usage_event_subtree_recompute_skips_root_resolution(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    The usage event's subtree recompute derives the tree root from the
    handler's row instead of re-reading the conversation. The one
    remaining non-tree SELECT is the monotonic anti-replay merge read in
    ``_persist_native_cumulative_usage`` — deliberately fresh.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="usage-hot-path")

    payload = {
        "type": "external_session_usage",
        "data": {"cumulative_cost_usd": 0.5},
    }
    await client.post(f"/v1/sessions/{conv.id}/events", json=payload)  # warm

    engine = _engine_cache[db_uri]
    with _count_conversation_selects(engine) as selects:
        resp = await client.post(f"/v1/sessions/{conv.id}/events", json=payload)

    assert resp.status_code == 202, resp.text
    # top-of-handler row + security merge read + the tree page scan.
    assert len(selects) <= 3, f"expected <=3 conversations SELECTs, got {len(selects)}"


@pytest.mark.asyncio
async def test_delta_event_semantics_unchanged(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Transient delta events still publish and never persist items."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="delta-path")

    resp = await client.post(
        f"/v1/sessions/{conv.id}/events",
        json={"type": "external_output_text_delta", "data": {"delta": "chunk"}},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}
    items = store.list_items(conv.id)
    assert items.data == []
