"""Tests for the Claude subscription connection: paste flow, store, and broker vend."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.connections.claude import ClaudeConnectionStore, token_hint
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.claude_subscription import (
    CLAUDE_SUBSCRIPTION_ENV_VAR,
    ClaudeSubscriptionConfig,
    build_claude_connection,
    normalize_setup_token,
)
from omnigent.server.connections_registry import connection_providers
from omnigent.server.routes.connections_claude import create_connections_claude_router
from omnigent.server.routes.host_credentials import create_host_credentials_router

_TOKEN = "sk-ant-oat01-" + "Ab-_" * 20 + "WXYZ"


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64
        import json

        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64
        import json

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
        if host_id == "host1" and token == "launch-tok":
            return _Managed(RESERVED_USER_LOCAL)
        return None


@pytest.fixture
def store(db_uri: str) -> ClaudeConnectionStore:
    return ClaudeConnectionStore(db_uri, SecretBox("enc-secret"))


@pytest.fixture
def client(store: ClaudeConnectionStore) -> TestClient:
    app = FastAPI()
    app.state.claude_store = store
    app.state.claude_client = None
    app.include_router(
        create_connections_claude_router(ClaudeSubscriptionConfig(), store), prefix="/v1"
    )
    app.include_router(create_host_credentials_router(_FakeHostStore()), prefix="/v1")  # type: ignore[arg-type]
    return TestClient(app)


def test_status_before_connect(client: TestClient) -> None:
    body = client.get("/v1/connections/claude/status").json()
    assert body == {
        "enabled": True,
        "connected": False,
        "connected_at": None,
        "needs_reconnect": False,
        "token_hint": None,
    }


def test_paste_connects_and_status_never_returns_the_token(client: TestClient) -> None:
    resp = client.post("/v1/connections/claude/secret", json={"secret": _TOKEN})
    assert resp.status_code == 200
    assert resp.json() == {"connected": True}

    status = client.get("/v1/connections/claude/status")
    body = status.json()
    assert body["connected"] is True
    assert body["token_hint"] == "sk-ant-oat01-…WXYZ"
    assert _TOKEN not in status.text


def test_paste_tolerates_wrapped_whitespace(client: TestClient, store) -> None:
    wrapped = f"  {_TOKEN[:30]}\n{_TOKEN[30:]}\n"
    assert (
        client.post("/v1/connections/claude/secret", json={"secret": wrapped}).status_code == 200
    )
    assert store.get(RESERVED_USER_LOCAL, with_tokens=True).oauth_token == _TOKEN


@pytest.mark.parametrize(
    "bad",
    ["", "sk-ant-api03-" + "x" * 40, "not a token", "sk-ant-oat01-short"],
)
def test_paste_rejects_non_setup_tokens(client: TestClient, store, bad: str) -> None:
    resp = client.post("/v1/connections/claude/secret", json={"secret": bad})
    assert resp.status_code == 400
    assert "claude setup-token" in resp.json()["detail"]
    assert store.get(RESERVED_USER_LOCAL) is None


def test_disconnect_removes_the_connection(client: TestClient) -> None:
    client.post("/v1/connections/claude/secret", json={"secret": _TOKEN})
    assert client.post("/v1/connections/claude/disconnect").json() == {"disconnected": True}
    assert client.get("/v1/connections/claude/status").json()["connected"] is False


def test_host_broker_vends_the_token_to_the_owners_sandbox(client: TestClient) -> None:
    client.post("/v1/connections/claude/secret", json={"secret": _TOKEN})
    resp = client.get(
        "/v1/hosts/host1/credentials/claude", headers={MANAGED_HOST_TOKEN_HEADER: "launch-tok"}
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {"connected": True, "owner": RESERVED_USER_LOCAL, "token": _TOKEN}


def test_host_broker_reports_not_connected(client: TestClient) -> None:
    resp = client.get(
        "/v1/hosts/host1/credentials/claude", headers={MANAGED_HOST_TOKEN_HEADER: "launch-tok"}
    )
    assert resp.json() == {"connected": False}


def test_host_broker_rejects_a_bad_launch_token(client: TestClient) -> None:
    client.post("/v1/connections/claude/secret", json={"secret": _TOKEN})
    resp = client.get(
        "/v1/hosts/host1/credentials/claude", headers={MANAGED_HOST_TOKEN_HEADER: "wrong"}
    )
    assert resp.status_code == 401


def test_token_hint_never_includes_body_characters() -> None:
    # The body contains dashes; only the fixed scheme prefix and last 4 show.
    assert token_hint(_TOKEN) == "sk-ant-oat01-…WXYZ"
    assert token_hint("unexpected-shape-token-1234") == "…1234"


def test_normalize_setup_token() -> None:
    assert normalize_setup_token(f"\n{_TOKEN} ") == _TOKEN
    assert normalize_setup_token("sk-ant-api03-" + "x" * 40) is None


def test_registry_wires_claude_with_a_broker_resolver() -> None:
    claude = next(p for p in connection_providers() if p.name == "claude")
    assert claude.credential_resolver is not None
    assert claude.client_factory(ClaudeSubscriptionConfig()) is None


def test_build_claude_connection(monkeypatch: pytest.MonkeyPatch, db_uri: str) -> None:
    import omnigent.stores.credential_store as credential_store

    monkeypatch.delenv(CLAUDE_SUBSCRIPTION_ENV_VAR, raising=False)
    assert build_claude_connection(db_uri) == (None, None)

    monkeypatch.setenv(CLAUDE_SUBSCRIPTION_ENV_VAR, "1")
    monkeypatch.setattr(credential_store, "build_secret_cipher", lambda: None)
    config, store = build_claude_connection(db_uri)
    assert config is not None and store is None

    monkeypatch.setattr(credential_store, "build_secret_cipher", lambda: SecretBox("k"))
    config, store = build_claude_connection(db_uri)
    assert isinstance(store, ClaudeConnectionStore)
