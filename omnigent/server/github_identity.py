"""Resolve a user's GitHub access token for the credential broker.

Bridges the connection store and the GitHub App client: reads the stored
connection and transparently refreshes an expired access token, so the broker
always vends a currently-valid token. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import logging

from omnigent.connections.github import GithubConnectionStore
from omnigent.connections.refresh import refresh_coordinated
from omnigent.db.utils import now_epoch
from omnigent.entities import GithubConnection
from omnigent.server.github_app import GitHubAppError, GitHubTokenSet
from omnigent.server.github_app_client import GitHubAppClient

_logger = logging.getLogger(__name__)

# Refresh a token that expires within this margin so it does not lapse
# mid-launch (or shortly after) inside the sandbox.
_REFRESH_MARGIN_S = 300

# The username git uses with a token over HTTPS (``https://<user>:<token>@…``);
# GitHub ignores the value but requires a non-empty one.
_GIT_TOKEN_USERNAME = "x-access-token"


async def resolve_access_token(
    user_id: str,
    *,
    store: GithubConnectionStore,
    client: GitHubAppClient,
) -> str | None:
    """Resolve a valid user access token for *user_id*, or ``None``.

    Reads the stored connection and, when the token is at/near expiry, refreshes
    it (persisting the new token). Concurrent callers across processes and
    replicas coordinate through the store's refresh lease, so exactly one
    refreshes (see :mod:`omnigent.connections.refresh`). Best-effort and
    **non-raising**: a transient refresh failure never discards a token that is
    still valid and never propagates — the broker degrades to
    ``{"connected": false}`` rather than a 500. A rejected refresh token marks
    the connection as needing a reconnect.

    :param user_id: The user whose token to resolve.
    :param store: The connection store (also used to persist a refresh).
    :param client: The GitHub App client.
    :returns: A usable access token, or ``None``.
    """

    def is_fresh(conn: GithubConnection) -> bool:
        expires_at = conn.token_expires_at
        # Non-expiring, or comfortably ahead of the margin: use as-is.
        return expires_at is None or expires_at > now_epoch() + _REFRESH_MARGIN_S

    async def refresh(conn: GithubConnection) -> GitHubTokenSet:
        if not conn.refresh_token:
            raise GitHubAppError("no refresh token stored")
        return await client.refresh_token(conn.refresh_token)

    def persist(tokens: GitHubTokenSet, holder: str) -> bool:
        return store.update_tokens(user_id, tokens, lease_holder=holder)

    resolved = await refresh_coordinated(
        store,
        user_id,
        is_fresh=is_fresh,
        refresh=refresh,
        persist=persist,
        provider="GitHub",
    )
    if resolved is None:
        return None
    if resolved.minted is not None:
        return resolved.minted.access_token
    connection = resolved.connection
    if not connection.access_token:
        return None
    # Refresh could not produce a new token; the current one is still usable
    # until it actually lapses, so prefer it and only give up once expired.
    expires_at = connection.token_expires_at
    if expires_at is None or expires_at > now_epoch():
        return connection.access_token
    return None


async def resolve_github_credential(
    user_id: str,
    *,
    store: GithubConnectionStore,
    client: GitHubAppClient,
) -> dict[str, object] | None:
    """Resolve the GitHub broker payload for *user_id*, or ``None``.

    The provider adapter the generic credential broker
    (:mod:`omnigent.server.routes.host_credentials`) calls: returns the vended
    token plus the attribution metadata git needs (``username``/``login``), or
    ``None`` when the owner has not linked GitHub. The ``owner``/``login`` let
    the host attribute commits to the human, decoupled from the push credential.
    """
    token = await resolve_access_token(user_id, store=store, client=client)
    if token is None:
        return None
    connection = await _run_sync(store.get, user_id)
    return {
        "username": _GIT_TOKEN_USERNAME,
        "token": token,
        "login": connection.github_login if connection is not None else None,
    }


async def _run_sync(func, /, *args, **kwargs):
    """Run a synchronous store call off the event loop."""
    import asyncio

    return await asyncio.to_thread(lambda: func(*args, **kwargs))
