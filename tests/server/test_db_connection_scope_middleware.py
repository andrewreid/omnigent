"""Server wiring tests for the per-request DB connection scope."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``GET /v1/sessions`` makes several store calls; with the request
    scope they share pool checkouts instead of paying one each.

    Measured comparatively — the same request is re-run with the scope
    refusing to lend — so the assertion cannot pass vacuously on an
    endpoint that happens to make few store calls.
    """
    import omnigent.db.utils as db_utils
    from tests.server.helpers import create_test_session

    # Seed a real agent-bound session: the list route filters on
    # has_agent_id, so bare conversations would leave the page empty and
    # the request nearly store-call-free.
    await create_test_session(client, title="scope-bench")

    engine = _engine_cache[db_uri]

    async def _count_checkouts() -> int:
        checkouts: list[object] = []

        @event.listens_for(engine, "checkout")
        def _on_checkout(dbapi_conn: object, conn_record: object, conn_proxy: object) -> None:
            checkouts.append(dbapi_conn)

        try:
            resp = await client.get("/v1/sessions")
            assert resp.status_code == 200
        finally:
            event.remove(engine, "checkout", _on_checkout)
        return len(checkouts)

    await client.get("/v1/sessions")  # warm caches for a fair comparison
    scoped = await _count_checkouts()

    # Disable lending: every managed session falls back to its own checkout.
    monkeypatch.setattr(db_utils._ConnectionScope, "lend", lambda self, eng: None)
    unscoped = await _count_checkouts()

    assert scoped < unscoped, f"scope saved nothing (scoped={scoped}, unscoped={unscoped})"
    assert scoped <= 2


@pytest.mark.asyncio
async def test_env_kill_switch_disables_scope(
    monkeypatch: pytest.MonkeyPatch,
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """``OMNIGENT_DB_CONNECTION_SCOPE=0`` builds the app without the scope
    middleware, restoring checkout-per-store-call behaviour."""
    monkeypatch.setenv("OMNIGENT_DB_CONNECTION_SCOPE", "0")

    from fastapi.routing import APIRoute

    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
    )

    seen: dict[str, bool] = {}

    async def _probe() -> dict[str, bool]:
        seen["handler"] = _connection_scope_var.get() is not None
        return seen

    app.router.routes.insert(0, APIRoute("/test/scope-probe", _probe, methods=["GET"]))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/test/scope-probe")

    assert resp.status_code == 200
    assert seen == {"handler": False}
