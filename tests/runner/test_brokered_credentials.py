"""Brokered ChatGPT credentials for codex in a managed sandbox.

Covers the runner-side route (binding-token gate), the auth client that signs
codex in and answers ``account/chatgptAuthTokens/refresh``, and the host's
readiness flag.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host import claude_credential
from omnigent.host.claude_credential import CHATGPT_BROKERED_ENV_VAR, configure_host_chatgpt
from omnigent.host.identity import HOST_TOKEN_ENV_VAR
from omnigent.runner import brokered_credentials as bc
from omnigent.runner.identity import (
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router

_TOKEN = "managed-runner-binding-token"
_PAYLOAD = {
    "connected": True,
    "owner": "owner@example.com",
    "token": "access-1",
    "account_id": "acct-1",
    "plan_type": "pro",
    "expires_at": 1_900_000_000,
}


# --- server: runner route ---------------------------------------------------


def _route_app() -> FastAPI:
    runner_id = token_bound_runner_id(_TOKEN)
    app = FastAPI()
    app.state.tunnel_registry = TunnelRegistry()
    app.state.chatgpt_store = object()
    app.state.chatgpt_client = None
    app.include_router(
        create_runner_tunnel_router(
            app.state.tunnel_registry,
            resolve_managed_runner_owner=lambda rid: (
                "owner@example.com" if rid == runner_id else None
            ),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    return app


@pytest.fixture
def patched_resolvers(monkeypatch: pytest.MonkeyPatch):
    import omnigent.server.routes.host_credentials as hc

    holder: dict = {}

    async def resolver(user_id: str, *, store, client):
        holder["user"] = user_id
        return holder.get("payload")

    monkeypatch.setattr(hc, "_broker_resolvers", lambda: {"chatgpt": resolver})
    return holder


async def _get(app: FastAPI, runner_id: str, token: str | None) -> httpx.Response:
    headers = {} if token is None else {RUNNER_TUNNEL_TOKEN_HEADER: token}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/v1/runners/{runner_id}/credentials/chatgpt", headers=headers)


async def test_runner_route_vends_to_the_managed_runner(patched_resolvers) -> None:
    patched_resolvers["payload"] = {"token": "access-1", "account_id": "acct-1"}
    resp = await _get(_route_app(), token_bound_runner_id(_TOKEN), _TOKEN)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "connected": True,
        "owner": "owner@example.com",
        "token": "access-1",
        "account_id": "acct-1",
    }
    assert patched_resolvers["user"] == "owner@example.com"


async def test_runner_route_reports_not_connected(patched_resolvers) -> None:
    resp = await _get(_route_app(), token_bound_runner_id(_TOKEN), _TOKEN)
    assert resp.json() == {"connected": False}


@pytest.mark.parametrize(
    ("runner_id", "token"),
    [
        (token_bound_runner_id(_TOKEN), None),  # no binding token
        ("runner_someone_else", _TOKEN),  # token bound to another runner
        (token_bound_runner_id("unmanaged-token"), "unmanaged-token"),  # no managed owner
    ],
)
async def test_runner_route_rejects_unauthenticated(patched_resolvers, runner_id, token) -> None:
    patched_resolvers["payload"] = {"token": "access-1"}
    resp = await _get(_route_app(), runner_id, token)
    assert resp.status_code == 401
    assert "access-1" not in resp.text


# --- runner: fetch + auth client ---------------------------------------------


@pytest.fixture
def managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://server.test")
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, _TOKEN)


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real(*args, **kwargs)

    monkeypatch.setattr(bc.httpx, "AsyncClient", factory)
    return seen


async def test_fetch_uses_the_binding_token_and_runner_route(managed_env, monkeypatch) -> None:
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=_PAYLOAD))
    assert await bc.fetch_runner_credential("chatgpt") == _PAYLOAD
    (request,) = seen
    assert request.url.path == f"/v1/runners/{token_bound_runner_id(_TOKEN)}/credentials/chatgpt"
    assert request.headers[RUNNER_TUNNEL_TOKEN_HEADER] == _TOKEN


async def test_fetch_is_a_noop_outside_a_managed_sandbox(monkeypatch) -> None:
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, json=_PAYLOAD))
    assert await bc.fetch_runner_credential("chatgpt") is None
    assert seen == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"connected": False}),
        httpx.Response(404),
        httpx.Response(401),
        httpx.Response(200, text="not json"),
    ],
)
async def test_fetch_returns_none_when_not_available(managed_env, monkeypatch, response) -> None:
    _mock_http(monkeypatch, lambda r: response)
    assert await bc.fetch_runner_credential("chatgpt") is None


class _FakeClient:
    """App-server client double: records requests/responses, replays events."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.responses: list[tuple[object, dict]] = []
        self.errors: list[tuple[object, str]] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return {"result": {"type": "chatgptAuthTokens"}}

    async def respond(self, request_id, result: dict) -> None:
        self.responses.append((request_id, result))

    async def respond_error(self, request_id, message: str, code: int = -32000) -> None:
        self.errors.append((request_id, message))

    async def iter_events(self):
        while True:
            yield await self.events.get()

    async def close(self) -> None:
        self.closed = True


async def _drain() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_login_then_answers_refresh_with_a_fresh_token(monkeypatch) -> None:
    fetched = iter([{**_PAYLOAD, "token": "access-2"}])

    async def fake_fetch(provider: str):
        return next(fetched)

    monkeypatch.setattr(bc, "fetch_runner_credential", fake_fetch)
    client = _FakeClient()
    auth = bc.BrokeredChatgptAuth(client)
    await auth.login(_PAYLOAD)
    assert client.requests == [
        (
            "account/login/start",
            {
                "type": "chatgptAuthTokens",
                "accessToken": "access-1",
                "chatgptAccountId": "acct-1",
                "chatgptPlanType": "pro",
            },
        )
    ]
    # Notifications and other clients' requests are ignored.
    await client.events.put({"method": "turn/started", "params": {}})
    await client.events.put({"id": 3, "method": "item/commandExecution/requestApproval"})
    await client.events.put(
        {
            "id": 7,
            "method": "account/chatgptAuthTokens/refresh",
            "params": {"reason": "unauthorized"},
        }
    )
    await _drain()
    assert client.responses == [
        (7, {"accessToken": "access-2", "chatgptAccountId": "acct-1", "chatgptPlanType": "pro"})
    ]
    await auth.close()
    assert client.closed


async def test_not_connected_refresh_fails_fast_and_is_cached(monkeypatch) -> None:
    calls = 0

    async def fake_fetch(provider: str):
        nonlocal calls
        calls += 1
        return

    monkeypatch.setattr(bc, "fetch_runner_credential", fake_fetch)
    client = _FakeClient()
    auth = bc.BrokeredChatgptAuth(client)
    await auth.login(_PAYLOAD)
    for i in range(14):  # codex retries a failed refresh ~14 times
        await client.events.put({"id": i, "method": "account/chatgptAuthTokens/refresh"})
    await _drain()
    assert len(client.errors) == 14 and client.responses == []
    assert calls == 1
    await auth.close()


async def test_rejected_token_handed_back_again_stops_the_retry_storm(monkeypatch) -> None:
    calls = 0

    async def fake_fetch(provider: str):
        nonlocal calls
        calls += 1
        return _PAYLOAD  # the broker still has the token codex just rejected

    monkeypatch.setattr(bc, "fetch_runner_credential", fake_fetch)
    client = _FakeClient()
    auth = bc.BrokeredChatgptAuth(client)
    await auth.login(_PAYLOAD)
    for i in range(14):
        await client.events.put({"id": i, "method": "account/chatgptAuthTokens/refresh"})
    await _drain()
    assert client.responses == [] and len(client.errors) == 14
    assert calls == 1
    await auth.close()


async def test_start_is_a_noop_when_not_connected(monkeypatch) -> None:
    async def fake_fetch(provider: str):
        return None

    monkeypatch.setattr(bc, "fetch_runner_credential", fake_fetch)
    assert await bc.start_brokered_chatgpt_auth("ws://127.0.0.1:1") is None


# --- host: readiness flag ------------------------------------------------------


def test_host_flags_chatgpt_availability_without_taking_the_token(monkeypatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.delenv(CHATGPT_BROKERED_ENV_VAR, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: list[str] = []

    def fake_fetch(server_url, host_id, host_token, provider="claude"):
        seen.append(provider)
        return _PAYLOAD

    monkeypatch.setattr(claude_credential, "_fetch", fake_fetch)
    assert configure_host_chatgpt("https://srv", "host1") is True
    import os

    assert os.environ[CHATGPT_BROKERED_ENV_VAR] == "1"
    assert seen == ["chatgpt"]
    assert "access-1" not in json.dumps(dict(os.environ))


def test_host_flag_not_set_outside_sandbox_or_when_disconnected(monkeypatch) -> None:
    monkeypatch.delenv(CHATGPT_BROKERED_ENV_VAR, raising=False)
    monkeypatch.setattr(claude_credential, "_fetch", lambda *a, **k: {"connected": False})
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    assert configure_host_chatgpt("https://srv", "host1") is False
    monkeypatch.delenv("IS_SANDBOX")
    assert configure_host_chatgpt("https://srv", "host1") is False
    import os

    assert CHATGPT_BROKERED_ENV_VAR not in os.environ


def test_codex_readiness_honours_the_broker_flag(monkeypatch) -> None:
    from omnigent.harnesses.codex_native import main as codex_main
    from omnigent.onboarding import harness_install

    monkeypatch.setattr(codex_main, "_find_codex_cli", lambda: "/usr/local/bin/codex")
    monkeypatch.setattr(harness_install, "harness_cli_installed", lambda *a, **k: True)
    monkeypatch.setenv(CHATGPT_BROKERED_ENV_VAR, "1")
    assert codex_main._codex_auth_unavailable_reason() is None
