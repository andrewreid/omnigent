"""Per-user ChatGPT subscription connection store.

A ChatGPT-typed :class:`~omnigent.connections.ConnectionStore` façade
(``provider="chatgpt"``). The secret is the whole token set, refresh token
included — it never leaves the server. Reuses the shared table and cipher, no
new schema. See ``designs/SUBSCRIPTION_BROKER.md``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from omnigent.connections import ConnectionStore
from omnigent.entities import ChatgptConnection, ProviderConnection
from omnigent.server.chatgpt_oauth import ChatgptTokenSet


class ChatgptConnectionStore(ConnectionStore[ChatgptConnection]):
    """ChatGPT-typed façade over the shared credential store."""

    _PROVIDER: ClassVar[str] = "chatgpt"

    @staticmethod
    def _to_entity(conn: ProviderConnection) -> ChatgptConnection:
        secret = conn.secret or {}
        meta = conn.metadata
        with_secret = conn.secret is not None
        return ChatgptConnection(
            user_id=conn.user_id,
            access_token=secret.get("access_token") if with_secret else None,
            refresh_token=secret.get("refresh_token") if with_secret else None,
            id_token=secret.get("id_token") if with_secret else None,
            account_id=meta.get("account_id"),
            access_expires_at=meta.get("access_expires_at"),
            plan_type=meta.get("plan_type"),
            email=meta.get("email"),
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    @staticmethod
    def _secret(tokens: ChatgptTokenSet) -> dict[str, Any]:
        return {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "id_token": tokens.id_token,
        }

    @staticmethod
    def _metadata(tokens: ChatgptTokenSet) -> dict[str, Any]:
        # Plaintext: identity and expiry only, never token material.
        return {
            "account_id": tokens.account_id,
            "access_expires_at": tokens.access_expires_at,
            "plan_type": tokens.plan_type,
            "email": tokens.email,
        }

    @staticmethod
    def token_set(conn: ChatgptConnection) -> ChatgptTokenSet:
        """The stored tokens as a :class:`ChatgptTokenSet` (for a refresh)."""
        return ChatgptTokenSet(
            access_token=conn.access_token or "",
            refresh_token=conn.refresh_token,
            id_token=conn.id_token,
            access_expires_at=conn.access_expires_at,
            account_id=conn.account_id,
            plan_type=conn.plan_type,
            email=conn.email,
        )

    def upsert(self, user_id: str, tokens: ChatgptTokenSet) -> ChatgptConnection:
        """Create or replace a user's ChatGPT connection (idempotent on ``user_id``)."""
        conn = self._store.upsert(
            user_id, self._PROVIDER, secret=self._secret(tokens), metadata=self._metadata(tokens)
        )
        return self._to_entity(conn)

    def update_tokens(
        self, user_id: str, tokens: ChatgptTokenSet, *, lease_holder: str | None = None
    ) -> bool:
        """Persist refreshed tokens. ``False`` if the row went or the lease was lost."""
        return self._store.update_secret(
            user_id,
            self._PROVIDER,
            secret=self._secret(tokens),
            metadata=self._metadata(tokens),
            lease_holder=lease_holder,
        )
