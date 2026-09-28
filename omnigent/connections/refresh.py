"""Cross-process coordination for refreshing a stored OAuth token.

Two server replicas (or two requests) that both see a near-expiry token must not
both refresh it: providers rotate the refresh token on use, so the loser presents
a spent token, and a provider with reuse detection may revoke the whole chain.
:func:`refresh_coordinated` makes exactly one caller refresh while the others
wait for its result, using the store's refresh lease — one short write to claim,
the upstream call outside any transaction, one write to commit. See
``designs/SUBSCRIPTION_BROKER.md`` ("Store: lease + CAS refresh").
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

_logger = logging.getLogger(__name__)

EntityT = TypeVar("EntityT")
TokensT = TypeVar("TokensT")

#: Longer than any provider's token-endpoint timeout, so a live holder keeps it.
LEASE_TTL_S = 60
_POLL_S = 0.25


class RefreshRejected(Exception):
    """The provider refused the refresh token (e.g. ``invalid_grant``): the chain
    is dead and only a reconnect can fix it. Raised by provider clients."""


@dataclasses.dataclass(frozen=True)
class Refreshed(Generic[EntityT, TokensT]):
    """What :func:`refresh_coordinated` resolved.

    :param connection: The stored connection (with tokens) as last read.
    :param minted: Tokens this call just obtained from the provider, or ``None``.
        Prefer them over *connection*: they are valid even when persisting them
        failed, and the refresh token they replaced is already spent.
    """

    connection: EntityT
    minted: TokensT | None = None


class _LeasedStore(Protocol[EntityT]):
    def get(self, user_id: str, *, with_tokens: bool = False) -> EntityT | None: ...
    def needs_reconnect(self, user_id: str) -> bool: ...
    def acquire_refresh_lease(self, user_id: str, *, holder: str, ttl_s: int) -> bool: ...
    def release_refresh_lease(self, user_id: str, *, holder: str) -> bool: ...
    def mark_needs_reconnect(self, user_id: str) -> bool: ...


async def refresh_coordinated(
    store: _LeasedStore[EntityT],
    user_id: str,
    *,
    is_fresh: Callable[[EntityT], bool],
    refresh: Callable[[EntityT], Awaitable[TokensT]],
    persist: Callable[[TokensT, str], bool],
    provider: str,
    lease_ttl_s: int = LEASE_TTL_S,
    wait_s: float = LEASE_TTL_S,
) -> Refreshed[EntityT, TokensT] | None:
    """Return the user's connection (with tokens), refreshing it at most once.

    :param is_fresh: Whether a connection's access token is usable as-is.
    :param refresh: Call the provider's token endpoint. Raise
        :class:`RefreshRejected` for a dead chain; anything else is transient.
    :param persist: ``(tokens, lease_holder) -> committed``; must write through
        the store's ``update_secret(..., lease_holder=…)``.
    :returns: The resolved connection, plus any tokens this call minted. When
        refresh failed transiently or another holder didn't finish in time it is
        the current connection (the caller decides from its expiry whether it is
        still usable). ``None`` when there is no connection or it needs a
        reconnect.
    """
    conn = await asyncio.to_thread(store.get, user_id, with_tokens=True)
    if conn is None or await asyncio.to_thread(store.needs_reconnect, user_id):
        return None
    if is_fresh(conn):
        return Refreshed(conn)

    holder = uuid.uuid4().hex
    acquired = await asyncio.to_thread(
        store.acquire_refresh_lease, user_id, holder=holder, ttl_s=lease_ttl_s
    )
    if not acquired:
        return await _await_other_holder(store, user_id, conn, is_fresh, wait_s)

    try:
        # Re-read under the lease: another holder may have just committed.
        conn = await asyncio.to_thread(store.get, user_id, with_tokens=True)
        if conn is None:
            return None
        if is_fresh(conn):
            return Refreshed(conn)
        try:
            tokens = await refresh(conn)
        except RefreshRejected as exc:
            _logger.warning(
                "%s refresh rejected for %s; needs reconnect: %s", provider, user_id, exc
            )
            await asyncio.to_thread(store.mark_needs_reconnect, user_id)
            return None
        except Exception as exc:  # noqa: BLE001 - transient: keep the current token
            _logger.warning("%s token refresh failed for %s: %s", provider, user_id, exc)
            return Refreshed(conn)
        try:
            persisted = await asyncio.to_thread(persist, tokens, holder)
        except Exception as exc:  # noqa: BLE001 - a persist error must not drop minted tokens
            _logger.warning("%s refreshed token for %s not persisted: %s", provider, user_id, exc)
            persisted = False
        if not persisted:
            # Lease lost (the refresh outlived it) or the row went away.
            _logger.warning("%s refreshed token for %s was not stored", provider, user_id)
        return Refreshed(conn, minted=tokens)
    finally:
        await asyncio.to_thread(store.release_refresh_lease, user_id, holder=holder)


async def _await_other_holder(
    store: _LeasedStore[EntityT],
    user_id: str,
    current: EntityT,
    is_fresh: Callable[[EntityT], bool],
    wait_s: float,
) -> Refreshed[EntityT, TokensT] | None:
    """Poll until another holder's refresh lands, or give up with *current*."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_S)
        conn = await asyncio.to_thread(store.get, user_id, with_tokens=True)
        if conn is None or await asyncio.to_thread(store.needs_reconnect, user_id):
            return None
        if is_fresh(conn):
            return Refreshed(conn)
    return Refreshed(current)
