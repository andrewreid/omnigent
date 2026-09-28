"""Tests for GitHub access-token resolution + refresh (the credential broker core).

The refresh path is the most sensitive logic in the broker: it must always
vend a currently-valid token, and it must fail *soft* (return ``None`` / keep a
still-valid token) rather than raise — a transient GitHub blip in the last few
minutes of a token's life must not turn into a 500 from the host endpoint.
"""

from __future__ import annotations

import asyncio

import httpx

from omnigent.db.utils import now_epoch
from omnigent.server import github_identity as gi
from omnigent.server.github_app import GitHubAppError, GitHubRefreshRejected, GitHubTokenSet


class _Conn:
    def __init__(self, access_token, refresh_token, token_expires_at) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = token_expires_at
        self.github_login = "octocat"


class _Store:
    """In-memory stand-in for GithubConnectionStore, including the refresh lease."""

    def __init__(self, conn: _Conn | None) -> None:
        self._conn = conn
        self.updated: list[GitHubTokenSet] = []
        self.raise_on_update = False
        self.reconnect = False
        self.lease: str | None = None

    def get(self, user_id: str, with_tokens: bool = False) -> _Conn | None:
        return self._conn

    def needs_reconnect(self, user_id: str) -> bool:
        return self.reconnect

    def acquire_refresh_lease(self, user_id: str, *, holder: str, ttl_s: int) -> bool:
        if self.lease not in (None, holder):
            return False
        self.lease = holder
        return True

    def release_refresh_lease(self, user_id: str, *, holder: str) -> bool:
        if self.lease != holder:
            return False
        self.lease = None
        return True

    def mark_needs_reconnect(self, user_id: str) -> bool:
        self.reconnect, self.lease = True, None
        return True

    def update_tokens(
        self, user_id: str, tokens: GitHubTokenSet, *, lease_holder: str | None = None
    ) -> bool:
        if self.raise_on_update:
            raise RuntimeError("db down")
        self.updated.append(tokens)
        return True


class _Client:
    def __init__(self, result: GitHubTokenSet | None = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls = 0

    async def refresh_token(self, refresh_token: str) -> GitHubTokenSet:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _resolve(store: _Store, client: _Client) -> str | None:
    return asyncio.run(gi.resolve_access_token("u@example.com", store=store, client=client))  # type: ignore[arg-type]


def _fresh(access_token: str) -> GitHubTokenSet:
    return GitHubTokenSet(access_token, "ghr_new", now_epoch() + 28800, None, "repo")


def test_no_connection_returns_none() -> None:
    assert _resolve(_Store(None), _Client()) is None


def test_non_expiring_token_used_as_is() -> None:
    client = _Client()
    assert _resolve(_Store(_Conn("ghu_live", "ghr", None)), client) == "ghu_live"
    assert client.calls == 0  # no refresh attempted


def test_token_with_distant_expiry_used_as_is() -> None:
    client = _Client()
    conn = _Conn("ghu_live", "ghr", now_epoch() + 10_000)
    assert _resolve(_Store(conn), client) == "ghu_live"
    assert client.calls == 0


def test_near_expiry_refreshes_and_persists() -> None:
    store = _Store(_Conn("ghu_old", "ghr", now_epoch() + 60))  # inside the 300s margin
    client = _Client(result=_fresh("ghu_new"))
    assert _resolve(store, client) == "ghu_new"
    assert client.calls == 1
    assert store.updated and store.updated[0].access_token == "ghu_new"


def test_refresh_rejected_keeps_still_valid_token() -> None:
    # GitHub rejects the refresh, but the current token has life left: use it.
    store = _Store(_Conn("ghu_old", "ghr", now_epoch() + 120))
    assert _resolve(store, _Client(exc=GitHubAppError("bad refresh"))) == "ghu_old"


def test_transient_network_error_does_not_raise_and_keeps_token() -> None:
    # The P1 core: an httpx timeout during refresh must not escape as a 500.
    store = _Store(_Conn("ghu_old", "ghr", now_epoch() + 120))
    assert _resolve(store, _Client(exc=httpx.TimeoutException("slow"))) == "ghu_old"
    assert _resolve(store, _Client(exc=httpx.ConnectError("down"))) == "ghu_old"


def test_expired_and_refresh_fails_returns_none() -> None:
    store = _Store(_Conn("ghu_dead", "ghr", now_epoch() - 10))  # already lapsed
    assert _resolve(store, _Client(exc=httpx.ConnectError("down"))) is None


def test_no_refresh_token_near_expiry_uses_still_valid() -> None:
    client = _Client()
    store = _Store(_Conn("ghu_old", None, now_epoch() + 120))
    assert _resolve(store, client) == "ghu_old"
    assert client.calls == 0  # nothing to refresh with


def test_no_refresh_token_expired_returns_none() -> None:
    store = _Store(_Conn("ghu_dead", None, now_epoch() - 10))
    assert _resolve(store, _Client()) is None


def test_persist_failure_still_returns_refreshed_token() -> None:
    # A DB write error after a successful refresh must not drop the new token.
    store = _Store(_Conn("ghu_old", "ghr", now_epoch() + 60))
    store.raise_on_update = True
    assert _resolve(store, _Client(result=_fresh("ghu_new"))) == "ghu_new"


def test_rejected_refresh_token_marks_reconnect_and_stops_refreshing() -> None:
    # invalid_grant/bad_refresh_token: the chain is dead. Vend nothing, flag the
    # connection, and don't hit GitHub again on the next resolve.
    store = _Store(_Conn("ghu_old", "ghr", now_epoch() + 60))
    client = _Client(exc=GitHubRefreshRejected("bad_refresh_token"))
    assert _resolve(store, client) is None
    assert store.reconnect is True
    assert store.lease is None  # released with the flag
    assert _resolve(store, client) is None
    assert client.calls == 1


def test_lease_held_elsewhere_does_not_refresh() -> None:
    # Another process holds the lease: wait for its result rather than refresh.
    store = _Store(_Conn("ghu_old", "ghr", now_epoch() + 120))
    store.lease = "other-process"
    client = _Client(result=_fresh("ghu_new"))
    token = asyncio.run(
        gi.refresh_coordinated(  # type: ignore[attr-defined]
            store,
            "u@example.com",
            is_fresh=lambda c: False,
            refresh=client.refresh_token,
            persist=lambda tokens, holder: True,
            provider="GitHub",
            wait_s=0.3,
        )
    )
    assert client.calls == 0
    assert token is not None and token.minted is None


def test_lease_released_after_successful_refresh() -> None:
    store = _Store(_Conn("ghu_old", "ghr", now_epoch() + 60))
    assert _resolve(store, _Client(result=_fresh("ghu_new"))) == "ghu_new"
    assert store.lease is None
