"""Per-user Claude subscription connection store.

A Claude-typed :class:`~omnigent.connections.ConnectionStore` façade
(``provider="claude"``) holding the long-lived token ``claude setup-token``
mints. The token is the whole secret; there is no refresh chain to own. Reuses
the shared table and cipher — no new schema. See
``designs/SUBSCRIPTION_BROKER.md`` ("Claude subscriptions: Option S").
"""

from __future__ import annotations

import re
from typing import ClassVar

from omnigent.connections import ConnectionStore
from omnigent.entities import ClaudeConnection, ProviderConnection

# The token's scheme prefix, e.g. ``sk-ant-oat01-``. Matched strictly so a
# dash inside the random body can never pull secret characters into the hint.
_SCHEME_PREFIX = re.compile(r"^sk-ant-[a-z]+[0-9]{2}-")


def token_hint(token: str) -> str:
    """Non-secret display hint for *token*: its scheme prefix and last 4 chars."""
    match = _SCHEME_PREFIX.match(token)
    return f"{match.group(0) if match else ''}…{token[-4:]}"


class ClaudeConnectionStore(ConnectionStore[ClaudeConnection]):
    """Claude-typed façade over the shared credential store."""

    _PROVIDER: ClassVar[str] = "claude"

    @staticmethod
    def _to_entity(conn: ProviderConnection) -> ClaudeConnection:
        secret = conn.secret or {}
        return ClaudeConnection(
            user_id=conn.user_id,
            oauth_token=secret.get("oauth_token") if conn.secret is not None else None,
            token_hint=str(conn.metadata.get("token_hint") or ""),
            created_at=conn.created_at,
            updated_at=conn.updated_at,
        )

    def upsert(self, user_id: str, *, oauth_token: str) -> ClaudeConnection:
        """Create or replace a user's Claude connection (idempotent on ``user_id``)."""
        conn = self._store.upsert(
            user_id,
            self._PROVIDER,
            secret={"oauth_token": oauth_token},
            metadata={"token_hint": token_hint(oauth_token)},
        )
        return self._to_entity(conn)
