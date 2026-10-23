"""Resolve a user's Databricks access token for the credential broker.

Bridges the connection store and the Databricks OAuth client: reads the stored
connection and transparently refreshes an expired access token, so the broker
always vends a currently-valid token together with its workspace host (needed to
build the Unity Gateway MCP URL). Mirrors :mod:`omnigent.server.github_identity`.
See ``designs/DATABRICKS_CONNECT.md``.
"""

from __future__ import annotations

import logging

from omnigent.connections.databricks import DatabricksConnectionStore
from omnigent.connections.refresh import LEASE_TTL_S, refresh_coordinated
from omnigent.db.utils import now_epoch
from omnigent.entities import DatabricksConnection
from omnigent.server.databricks_app import (
    DatabricksTokenRequestFailed,
    DatabricksTokenSet,
)
from omnigent.server.databricks_app_client import DatabricksAppClient

_logger = logging.getLogger(__name__)

# Refresh a token that expires within this margin so it does not lapse
# mid-launch (or shortly after) inside the sandbox.
_REFRESH_MARGIN_S = 300

# Margin for Databricks Sandbox owner tokens. They double as the in-sandbox
# proxy bearer, refreshed by the keepalive, so keep them valid past one interval.
OWNER_IDENTITY_REFRESH_MARGIN_S = 900

# How long an owner-identity resolve waits for another holder's refresh. Its
# callers (launcher, single-worker keepalive) are blocking, so keep it short.
OWNER_IDENTITY_LEASE_WAIT_S = 5.0


async def resolve_databricks_token(
    user_id: str,
    *,
    store: DatabricksConnectionStore,
    client: DatabricksAppClient,
    refresh_margin_s: int = _REFRESH_MARGIN_S,
    lease_wait_s: float = LEASE_TTL_S,
) -> tuple[str, str] | None:
    """Resolve a valid ``(access_token, workspace_host)`` for *user_id*, or ``None``.

    Reads the stored connection and transparently refreshes a token at/near
    expiry (persisting the refresh, workspace-scoped). Concurrent callers
    coordinate through the store's refresh lease so exactly one refreshes.
    Best-effort: any failure (no connection, no refresh token, refresh
    rejected) returns ``None``; a rejected refresh token also marks the
    connection as needing a reconnect.

    :param refresh_margin_s: Refresh when the token expires within this many
        seconds, so the returned token stays valid at least that long.
    :param lease_wait_s: How long to wait for another holder's refresh before
        settling for the current token.

    When a refresh can't complete (a transient failure, or another holder still
    refreshing), a token that hasn't expired yet is returned rather than
    ``None``, even inside the margin; the next call tries again.
    """

    def is_fresh(conn: DatabricksConnection) -> bool:
        expires_at = conn.token_expires_at
        return expires_at is None or expires_at > now_epoch() + refresh_margin_s

    async def refresh(conn: DatabricksConnection) -> DatabricksTokenSet:
        if not conn.refresh_token:
            raise DatabricksTokenRequestFailed("no refresh token stored")
        return await client.refresh_token(conn.workspace_host, conn.refresh_token)

    def persist(tokens: DatabricksTokenSet, holder: str) -> bool:
        return store.update_tokens(user_id, tokens, lease_holder=holder)

    resolved = await refresh_coordinated(
        store,
        user_id,
        is_fresh=is_fresh,
        refresh=refresh,
        persist=persist,
        provider="Databricks",
        wait_s=lease_wait_s,
    )
    if resolved is None:
        return None
    connection = resolved.connection
    if not connection.access_token or not connection.workspace_host:
        return None
    if resolved.minted is not None:
        return resolved.minted.access_token, connection.workspace_host
    expires_at = connection.token_expires_at
    if expires_at is None or expires_at > now_epoch():
        return connection.access_token, connection.workspace_host
    return None


async def resolve_databricks_credential(
    user_id: str,
    *,
    store: DatabricksConnectionStore,
    client: DatabricksAppClient,
    refresh_margin_s: int = _REFRESH_MARGIN_S,
    lease_wait_s: float = LEASE_TTL_S,
) -> dict[str, object] | None:
    """Resolve the Databricks broker payload for *user_id*, or ``None``.

    The provider adapter the generic credential broker
    (:mod:`omnigent.server.routes.host_credentials`) calls: returns the vended
    (server-refreshed) access token plus the ``workspace_host`` the sandbox
    needs to target the Unity Gateway, or ``None`` when the owner has not linked
    Databricks. Mirrors
    :func:`omnigent.server.github_identity.resolve_github_credential`.
    """
    resolved = await resolve_databricks_token(
        user_id,
        store=store,
        client=client,
        refresh_margin_s=refresh_margin_s,
        lease_wait_s=lease_wait_s,
    )
    if resolved is None:
        return None
    access_token, workspace_host = resolved
    return {"token": access_token, "workspace_host": workspace_host}
