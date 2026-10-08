"""ChatGPT subscription routes: status / device/start / device/poll / disconnect.

Mounted under ``/v1`` so paths are ``/v1/connections/chatgpt/...``. Only mounted
when :class:`~omnigent.server.chatgpt_oauth.ChatgptConfig` is enabled and the
credential store is configured. The shared device-code flow lives in
:mod:`omnigent.server.routes.connections_base`; this is the ChatGPT adapter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from omnigent.connections.chatgpt import ChatgptConnectionStore
from omnigent.server.auth import AuthProvider
from omnigent.server.chatgpt_oauth import ChatgptConfig, ChatgptOAuthClient, ChatgptOAuthError
from omnigent.server.routes.connections_base import (
    ConnectionError,
    DeviceStart,
    create_device_connection_router,
)

_logger = logging.getLogger(__name__)


class ChatgptConnectionHooks:
    """ChatGPT half of the device-code flow: start, poll/exchange, revoke."""

    provider = "chatgpt"

    def __init__(
        self,
        config: ChatgptConfig,
        store: ChatgptConnectionStore,
        client: ChatgptOAuthClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.api = client if client is not None else ChatgptOAuthClient(config)

    def signing_key(self) -> str:
        return self.config.state_key

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        return {
            "plan_type": connection.plan_type if connection is not None else None,
            "email": connection.email if connection is not None else None,
        }

    async def device_start(self) -> DeviceStart:
        try:
            code = await self.api.request_device_code()
        except ChatgptOAuthError as exc:
            raise ConnectionError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ConnectionError("Couldn't reach ChatGPT. Try again.") from exc
        return DeviceStart(
            user_code=code.user_code,
            verification_url=code.verification_url,
            interval_s=code.interval_s,
            poll_state={"device_auth_id": code.device_auth_id, "user_code": code.user_code},
        )

    async def device_poll(self, user_id: str, poll_state: dict[str, str]) -> bool:
        device_auth_id = poll_state.get("device_auth_id", "")
        user_code = poll_state.get("user_code", "")
        if not device_auth_id or not user_code:
            raise ConnectionError("This sign-in has no device code; start again.")
        try:
            tokens = await self.api.poll_device_code(device_auth_id, user_code)
        except ChatgptOAuthError as exc:
            raise ConnectionError(str(exc)) from exc
        except httpx.HTTPError:
            return False  # transient; the client polls again
        if tokens is None:
            return False
        await asyncio.to_thread(self.store.upsert, user_id, tokens)
        _logger.info("ChatGPT subscription connected for %s (plan %s)", user_id, tokens.plan_type)
        return True

    async def revoke(self, user_id: str) -> None:
        """Revoke the refresh chain upstream; every vended access token dies with it."""
        conn = await asyncio.to_thread(self.store.get, user_id, with_tokens=True)
        if conn is not None and conn.refresh_token:
            await self.api.revoke(conn.refresh_token)


def create_connections_chatgpt_router(
    config: ChatgptConfig,
    store: ChatgptConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: ChatgptOAuthClient | None = None,
):
    """Build the ChatGPT subscription router over the shared device-code flow."""
    return create_device_connection_router(
        ChatgptConnectionHooks(config, store, client),
        auth_provider=auth_provider,
    )
