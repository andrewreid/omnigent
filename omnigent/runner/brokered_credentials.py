"""Brokered subscription credentials for harnesses running in a managed sandbox.

The server owns the user's ChatGPT refresh chain; a managed runner fetches
short-lived access tokens from ``GET /v1/runners/{id}/credentials/chatgpt``
(authenticated by its tunnel binding token) and hands them to codex through the
app-server's ``chatgptAuthTokens`` login — nothing credential-shaped is written
to disk. Codex asks for a fresh token with ``account/chatgptAuthTokens/refresh``
after a 401; the app-server broadcasts that request to every connected client, so
a dedicated client that lives as long as the app-server answers it. See
``designs/SUBSCRIPTION_BROKER.md`` ("Delivery in the sandbox").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

import httpx

from omnigent.runner.identity import (
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)

_logger = logging.getLogger(__name__)

_SERVER_URL_ENV_VAR = "RUNNER_SERVER_URL"
_TIMEOUT_S = 15.0
_REFRESH_METHOD = "account/chatgptAuthTokens/refresh"
# Codex retries a failed refresh ~14 times in quick succession; answer those
# from cache rather than re-asking the server each time.
_NEGATIVE_CACHE_S = 60.0


def _in_managed_sandbox() -> bool:
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


async def fetch_runner_credential(provider: str) -> dict[str, Any] | None:
    """Fetch the owner's *provider* credential for this managed runner.

    :returns: The broker payload when connected, else ``None`` (not in a managed
        sandbox, no binding token, server unreachable, or not connected).
    """
    if not _in_managed_sandbox():
        return None
    server = (os.environ.get(_SERVER_URL_ENV_VAR) or "").rstrip("/")
    token = (os.environ.get(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR) or "").strip()
    if not server or not token:
        return None
    url = f"{server}/v1/runners/{token_bound_runner_id(token)}/credentials/{provider}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as http:
            resp = await http.get(url, headers={RUNNER_TUNNEL_TOKEN_HEADER: token})
    except httpx.HTTPError as exc:
        _logger.info("brokered %s credential unavailable: %s", provider, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("connected") or not data.get("token"):
        return None
    return data


def _login_params(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "accessToken": payload["token"],
        "chatgptAccountId": payload.get("account_id") or "",
        "chatgptPlanType": payload.get("plan_type"),
    }


class BrokeredChatgptAuth:
    """Logs a codex app-server in with the owner's ChatGPT subscription and keeps
    answering its token-refresh requests for as long as the app-server lives.

    :param client: A connected app-server client used only for auth.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._task: asyncio.Task[None] | None = None
        self._declined_until = 0.0
        # The token codex currently holds; a refresh request means it was rejected.
        self._current_token: str | None = None

    async def login(self, payload: dict[str, Any]) -> None:
        """Log the app-server in and start serving refresh requests."""
        await self._client.request(
            "account/login/start", {"type": "chatgptAuthTokens", **_login_params(payload)}
        )
        self._current_token = payload["token"]
        self._task = asyncio.create_task(self._serve(), name="codex-chatgpt-auth")

    async def _serve(self) -> None:
        async for message in self._client.iter_events():
            if message.get("method") != _REFRESH_METHOD or "id" not in message:
                continue  # notifications and other clients' requests
            try:
                await self._answer(message["id"])
            except Exception:  # noqa: BLE001 - one bad answer must not stop serving
                _logger.warning("could not answer codex token refresh", exc_info=True)

    async def _answer(self, request_id: int | str) -> None:
        payload = None
        if time.monotonic() >= self._declined_until:
            payload = await fetch_runner_credential("chatgpt")
            # The broker handing back the token codex just rejected means it is
            # dead upstream (e.g. revoked); answering with it again only feeds
            # codex's retry loop.
            if payload is not None and payload["token"] == self._current_token:
                payload = None
            if payload is None:
                self._declined_until = time.monotonic() + _NEGATIVE_CACHE_S
        if payload is None:
            await self._client.respond_error(
                request_id, "ChatGPT subscription is not connected; reconnect it in Settings"
            )
            return
        self._current_token = payload["token"]
        await self._client.respond(request_id, _login_params(payload))

    async def close(self) -> None:
        """Stop serving refreshes and disconnect."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        with contextlib.suppress(Exception):
            await self._client.close()


async def start_brokered_chatgpt_auth(ws_url: str) -> BrokeredChatgptAuth | None:
    """Log the codex app-server at *ws_url* in with the owner's ChatGPT subscription.

    A no-op (``None``) outside a managed sandbox or when the owner hasn't
    connected ChatGPT; the caller keeps codex's own login path. Never raises.
    """
    payload = await fetch_runner_credential("chatgpt")
    if payload is None:
        return None
    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient

    client = CodexAppServerClient(ws_url=ws_url, client_name="omnigent-chatgpt-auth")
    auth = BrokeredChatgptAuth(client)
    try:
        await client.connect()
        await auth.login(payload)
    except Exception:  # noqa: BLE001 - fall back to codex's own login
        _logger.warning("codex: brokered ChatGPT login failed", exc_info=True)
        await auth.close()
        return None
    _logger.info("codex: logged in with the owner's brokered ChatGPT subscription")
    return auth
