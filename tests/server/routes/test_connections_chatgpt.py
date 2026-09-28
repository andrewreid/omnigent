"""Tests for the ChatGPT subscription connection: device-code flow, broker, refresh."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.connections.chatgpt import ChatgptConnectionStore
from omnigent.db.account_authority import account_generation
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.chatgpt_identity import resolve_chatgpt_credential
from omnigent.server.chatgpt_oauth import (
    ChatgptConfig,
    ChatgptOAuthClient,
    ChatgptRefreshRejected,
    token_set_from_payload,
)
from omnigent.server.routes.connections_chatgpt import create_connections_chatgpt_router
from omnigent.server.routes.host_credentials import create_host_credentials_router

_ISSUER = "https://auth.example"
_KEY = "state-key-that-is-at-least-32-bytes-long"
_USER = RESERVED_USER_LOCAL


def _jwt(claims: dict) -> str:
    def seg(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def _tokens(n: int, *, ttl: int = 864000) -> dict:
    auth = {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "pro"}
    return {
        "access_token": _jwt({"exp": int(time.time()) + ttl, "https://api.openai.com/auth": auth}),
        "refresh_token": f"rt-{n}",
        "id_token": _jwt({"email": "a@example.com", "https://api.openai.com/auth": auth}),
    }


class FakeAuthService:
    """Just enough of auth.openai.com for the device-code, refresh and revoke flows."""

    def __init__(self) -> None:
        self.pending_polls = 1
        self.usercode_status = 200
        self.refresh_error: tuple[int, dict] | None = None
        self.refreshes: list[str] = []
        self.revoked: list[str] = []
        self.exchanges: list[dict] = []
        self.user_agents: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.user_agents.add(request.headers.get("user-agent", ""))
        path = request.url.path
        if path == "/api/accounts/deviceauth/usercode":
            if self.usercode_status != 200:
                return httpx.Response(self.usercode_status)
            assert json.loads(request.content) == {"client_id": "cid"}
            return httpx.Response(
                200, json={"device_auth_id": "dai-1", "user_code": "ABCD-1234", "interval": "5"}
            )
        if path == "/api/accounts/deviceauth/token":
            assert json.loads(request.content) == {
                "device_auth_id": "dai-1",
                "user_code": "ABCD-1234",
            }
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return httpx.Response(403)
            return httpx.Response(
                200,
                json={"authorization_code": "ac", "code_verifier": "cv", "code_challenge": "cc"},
            )
        if path == "/oauth/token" and request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"
        ):
            form = dict(httpx.QueryParams(request.content.decode()))
            self.exchanges.append(form)
            return httpx.Response(200, json=_tokens(1))
        if path == "/oauth/token":
            body = json.loads(request.content)
            self.refreshes.append(body["refresh_token"])
            if self.refresh_error is not None:
                status, payload = self.refresh_error
                return httpx.Response(status, json=payload)
            n = int(body["refresh_token"].split("-")[1]) + 1
            return httpx.Response(200, json=_tokens(n))
        if path == "/oauth/revoke":
            self.revoked.append(json.loads(request.content)["token"])
            return httpx.Response(200)
        return httpx.Response(404)


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


@dataclass
class _Managed:
    user_id: str


class _FakeHostStore:
    def resolve_launch_token(self, host_id: str, token: str) -> _Managed | None:
        return _Managed(_USER) if (host_id, token) == ("host1", "launch-tok") else None


@pytest.fixture
def auth() -> FakeAuthService:
    return FakeAuthService()


@pytest.fixture
def api(auth: FakeAuthService) -> ChatgptOAuthClient:
    config = ChatgptConfig(issuer=_ISSUER, client_id="cid", state_key=_KEY)
    return ChatgptOAuthClient(config, transport=httpx.MockTransport(auth))


@pytest.fixture
def store(db_uri: str) -> ChatgptConnectionStore:
    return ChatgptConnectionStore(db_uri, SecretBox("enc"))


@pytest.fixture
def client(store: ChatgptConnectionStore, api: ChatgptOAuthClient) -> TestClient:
    app = FastAPI()
    app.state.chatgpt_store = store
    app.state.chatgpt_client = api
    config = ChatgptConfig(issuer=_ISSUER, client_id="cid", state_key=_KEY)
    app.include_router(create_connections_chatgpt_router(config, store, client=api), prefix="/v1")
    app.include_router(create_host_credentials_router(_FakeHostStore()), prefix="/v1")  # type: ignore[arg-type]
    return TestClient(app)


def _connect(client: TestClient) -> dict:
    start = client.post("/v1/connections/chatgpt/device/start").json()
    poll = lambda: client.post(  # noqa: E731
        "/v1/connections/chatgpt/device/poll", json={"handle": start["handle"]}
    ).json()
    assert poll() == {"status": "pending"}
    assert poll() == {"status": "complete"}
    return start


def test_device_start_returns_code_link_and_interval(client: TestClient) -> None:
    body = client.post("/v1/connections/chatgpt/device/start").json()
    assert body["user_code"] == "ABCD-1234"
    assert body["verification_url"] == f"{_ISSUER}/codex/device"
    assert body["interval"] == 5
    assert body["expires_in"] == 900
    assert "dai-1" not in json.dumps({k: v for k, v in body.items() if k != "handle"})


def test_full_connect_stores_tokens_and_status_shows_identity_only(
    client: TestClient, auth: FakeAuthService, store: ChatgptConnectionStore
) -> None:
    _connect(client)
    assert auth.exchanges == [
        {
            "grant_type": "authorization_code",
            "code": "ac",
            "redirect_uri": f"{_ISSUER}/deviceauth/callback",
            "client_id": "cid",
            "code_verifier": "cv",
        }
    ]
    status = client.get("/v1/connections/chatgpt/status")
    body = status.json()
    assert body["connected"] is True and body["needs_reconnect"] is False
    assert body["plan_type"] == "pro" and body["email"] == "a@example.com"
    assert "rt-1" not in status.text and "access_token" not in status.text
    assert store.get(_USER, with_tokens=True).refresh_token == "rt-1"
    assert auth.user_agents == {"omnigent"}


def test_broker_vends_access_token_but_never_the_refresh_chain(client: TestClient) -> None:
    _connect(client)
    resp = client.get(
        "/v1/hosts/host1/credentials/chatgpt", headers={MANAGED_HOST_TOKEN_HEADER: "launch-tok"}
    )
    body = resp.json()
    assert body["connected"] is True
    assert set(body) == {"connected", "owner", "token", "account_id", "plan_type", "expires_at"}
    assert body["account_id"] == "acct-1" and body["plan_type"] == "pro"
    assert "rt-1" not in resp.text


def test_device_code_disabled_upstream_is_a_clear_400(client: TestClient, auth) -> None:
    auth.usercode_status = 404
    resp = client.post("/v1/connections/chatgpt/device/start")
    assert resp.status_code == 400
    assert "Settings → Security" in resp.json()["detail"]


def test_handle_is_bound_to_its_user(client: TestClient) -> None:
    forged = jwt.encode(
        {
            "sub": "mallory@example.com",
            "account_generation": account_generation("mallory@example.com"),
            "poll": {"device_auth_id": "dai-1", "user_code": "ABCD-1234"},
            "exp": int(time.time()) + 60,
        },
        _KEY,
        algorithm="HS256",
    )
    resp = client.post("/v1/connections/chatgpt/device/poll", json={"handle": forged})
    assert resp.status_code == 403


def test_expired_and_tampered_handles(client: TestClient) -> None:
    expired = jwt.encode(
        {"sub": _USER, "account_generation": account_generation(_USER), "poll": {}, "exp": 1},
        _KEY,
        algorithm="HS256",
    )
    poll = "/v1/connections/chatgpt/device/poll"
    assert client.post(poll, json={"handle": expired}).json() == {"status": "expired"}
    assert client.post(poll, json={"handle": "not-a-jwt"}).status_code == 400


def test_disconnect_revokes_upstream_then_deletes(client: TestClient, auth) -> None:
    _connect(client)
    assert client.post("/v1/connections/chatgpt/disconnect").json() == {"disconnected": True}
    assert auth.revoked == ["rt-1"]
    assert client.get("/v1/connections/chatgpt/status").json()["connected"] is False


# --- resolver refresh -------------------------------------------------------


def _seed(store: ChatgptConnectionStore, *, ttl: int) -> None:
    store.upsert(_USER, token_set_from_payload(_tokens(1, ttl=ttl)))


def _resolve(store, api) -> dict | None:
    return asyncio.run(resolve_chatgpt_credential(_USER, store=store, client=api))


def test_fresh_token_is_vended_without_refreshing(store, api, auth) -> None:
    _seed(store, ttl=864000)
    assert _resolve(store, api)["account_id"] == "acct-1"
    assert auth.refreshes == []


def test_token_inside_the_margin_is_refreshed_and_rotated(store, api, auth) -> None:
    _seed(store, ttl=3600)  # less than the 2-day margin
    payload = _resolve(store, api)
    assert auth.refreshes == ["rt-1"]
    assert payload["token"] == store.get(_USER, with_tokens=True).access_token
    assert store.get(_USER, with_tokens=True).refresh_token == "rt-2"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "invalid_grant"}),
        (400, {"error": {"code": "refresh_token_reused"}}),
        (401, {"error": "unauthorized"}),
    ],
)
def test_rejected_refresh_needs_reconnect_and_stops(store, api, auth, status, body) -> None:
    _seed(store, ttl=3600)
    auth.refresh_error = (status, body)
    assert _resolve(store, api) is None
    assert store.needs_reconnect(_USER)
    assert _resolve(store, api) is None
    assert auth.refreshes == ["rt-1"]


def test_transient_refresh_failure_keeps_a_still_valid_token(store, api, auth) -> None:
    _seed(store, ttl=3600)
    auth.refresh_error = (503, {"error": "unavailable"})
    assert _resolve(store, api) is not None
    assert not store.needs_reconnect(_USER)


def test_expired_token_with_failed_refresh_is_not_vended(store, api, auth) -> None:
    _seed(store, ttl=-10)
    auth.refresh_error = (503, {"error": "unavailable"})
    assert _resolve(store, api) is None


def test_refresh_rejection_is_a_refresh_rejected() -> None:
    assert issubclass(ChatgptRefreshRejected, Exception)
    from omnigent.connections.refresh import RefreshRejected

    assert issubclass(ChatgptRefreshRejected, RefreshRejected)


def test_token_set_parses_claims_and_carries_over_on_refresh() -> None:
    first = token_set_from_payload(_tokens(1))
    assert (first.account_id, first.plan_type, first.email) == ("acct-1", "pro", "a@example.com")
    assert first.access_expires_at and first.access_expires_at > time.time()
    # A refresh response that omits id_token/refresh_token keeps the previous ones.
    second = token_set_from_payload({"access_token": _tokens(2)["access_token"]}, previous=first)
    assert second.refresh_token == "rt-1" and second.id_token == first.id_token
