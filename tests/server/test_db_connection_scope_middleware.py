"""Server wiring tests for the per-request DB connection scope."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from omnigent.db.utils import _connection_scope_var, _engine_cache


@pytest.mark.asyncio
async def test_scope_active_in_handler_and_worker_threads(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    The middleware binds a connection scope for the request, and the
    scope propagates into ``asyncio.to_thread`` workers — the execution
    model every store call uses.
    """
    seen: dict[str, bool] = {}

    async def _probe() -> dict[str, bool]:
        seen["handler"] = _connection_scope_var.get() is not None
        seen["thread"] = await asyncio.to_thread(lambda: _connection_scope_var.get() is not None)
        return seen

    # Insert ahead of the SPA catch-all route, which would otherwise
    # shadow the probe path.
    from fastapi.routing import APIRoute

    app.router.routes.insert(0, APIRoute("/test/scope-probe", _probe, methods=["GET"]))

    resp = await client.get("/test/scope-probe")

    assert resp.status_code == 200
    assert seen == {"handler": True, "thread": True}


@pytest.mark.asyncio
async def test_sessions_list_reuses_one_connection(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``GET /v1/sessions`` makes several store calls; with the request
    scope they share a single pool checkout instead of one each.
    """
    engine = _engine_cache[db_uri]
    checkouts: list[object] = []

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn: object, conn_record: object, conn_proxy: object) -> None:
        checkouts.append(dbapi_conn)

    try:
        resp = await client.get("/v1/sessions")
        assert resp.status_code == 200
        # One checkout for the scope; allow one more for an overlapping
        # (gathered) store call that falls back to the pool.
        assert len(checkouts) <= 2
    finally:
        event.remove(engine, "checkout", _on_checkout)
