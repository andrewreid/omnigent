"""Claude subscription routes: status / secret / disconnect.

Mounted under ``/v1`` so paths are ``/v1/connections/claude/...``. Only mounted
when :class:`~omnigent.server.claude_subscription.ClaudeSubscriptionConfig` is
enabled and the credential store is configured. The user pastes the token
``claude setup-token`` printed; the shared paste flow lives in
:mod:`omnigent.server.routes.connections_base`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.connections.claude import ClaudeConnectionStore
from omnigent.server.auth import AuthProvider
from omnigent.server.claude_subscription import ClaudeSubscriptionConfig, normalize_setup_token
from omnigent.server.routes.connections_base import (
    ConnectionError,
    create_paste_connection_router,
)

_logger = logging.getLogger(__name__)


class ClaudeConnectionHooks:
    """Claude half of the paste flow: validate the setup-token, then store it."""

    provider = "claude"

    def __init__(self, store: ClaudeConnectionStore) -> None:
        self.store = store

    def status_fields(self, connection: Any | None) -> dict[str, Any]:
        return {"token_hint": connection.token_hint if connection is not None else None}

    async def accept(self, user_id: str, secret: str) -> None:
        token = normalize_setup_token(secret)
        if token is None:
            raise ConnectionError(
                "That doesn't look like a Claude setup token. Run `claude setup-token` "
                "and paste the token it prints (it starts with sk-ant-oat)."
            )
        await asyncio.to_thread(self.store.upsert, user_id, oauth_token=token)
        _logger.info("Claude subscription connected for %s", user_id)


def create_connections_claude_router(
    config: ClaudeSubscriptionConfig,  # noqa: ARG001 - registry signature
    store: ClaudeConnectionStore,
    *,
    auth_provider: AuthProvider | None = None,
    client: Any = None,  # noqa: ARG001 - registry signature; no OAuth client
):
    """Build the Claude subscription router over the shared paste flow."""
    return create_paste_connection_router(
        ClaudeConnectionHooks(store),
        auth_provider=auth_provider,
    )
