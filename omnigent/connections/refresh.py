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
import contextlib
import dataclasses
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar

import httpx

_logger = logging.getLogger(__name__)

EntityT = TypeVar("EntityT")
TokensT = TypeVar("TokensT")

#: Longer than any provider's token-endpoint timeout, so a live holder keeps it.
LEASE_TTL_S = 60
_POLL_S = 0.25
#: Backoff between attempts to store freshly minted tokens (the old refresh
#: token is already spent, so losing them means a reconnect).
_PERSIST_RETRY_S = (0.2, 0.5, 1.0)


class RefreshRejected(Exception):
    """The provider refused the refresh token (e.g. ``invalid_grant``): the chain
    is dead and only a reconnect can fix it. Raised by provider clients."""


@dataclasses.dataclass(frozen=True)
class Refreshed(Generic[EntityT, TokensT]):
    """What :func:`refresh_coordinated` resolved.

    :param connection: The stored connection (with tokens) as last read.
    :param minted: Tokens this call just obtained from the provider, or ``None``.
        Prefer them over *connection*: they are valid even when persisting them
        failed (the connection is then flagged for reconnect, because the
        refresh token they replaced is spent).
    """

    connection: EntityT
    minted: TokensT | None = None


class _LeasedStore(Protocol[EntityT]):
    def get(self, user_id: str, *, with_tokens: bool = False) -> EntityT | None: ...
    def needs_reconnect(self, user_id: str) -> bool: ...
    def acquire_refresh_lease(self, user_id: str, *, holder: str, ttl_s: int) -> bool: ...
    def get_under_lease(self, user_id: str, *, holder: str) -> EntityT | None: ...
    def mark_refresh_inflight(self, user_id: str, *, holder: str) -> bool: ...
    def release_refresh_lease(self, user_id: str, *, holder: str) -> bool: ...
    def mark_needs_reconnect(self, user_id: str, *, holder: str | None = None) -> bool: ...


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

    A failure that leaves it unknown whether the provider rotated the chain
    (a lost answer, a failed save, a crash or cancellation mid-refresh) fails
    closed: the connection is flagged for reconnect, or the in-flight marker
    left on the lease makes the next acquirer flag it, so a possibly spent
    refresh token is never sent again.
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

    # Release only when the outcome is known. Otherwise the in-flight marker
    # outlives the lease, and the next acquirer fails closed instead of
    # replaying a refresh token that may already be spent.
    outcome_known = True
    try:
        # A fenced snapshot: None if a reconnect or flag landed since acquiring.
        leased = await asyncio.to_thread(store.get_under_lease, user_id, holder=holder)
        if leased is None:
            return await _current_or_none(store, user_id, conn)
        conn = leased
        if is_fresh(conn):
            return Refreshed(conn)
        if not await asyncio.to_thread(store.mark_refresh_inflight, user_id, holder=holder):
            return await _current_or_none(store, user_id, conn)
        try:
            tokens = await refresh(conn)
        except RefreshRejected as exc:
            _logger.warning(
                "%s refresh rejected for %s; needs reconnect: %s", provider, user_id, exc
            )
            await asyncio.to_thread(store.mark_needs_reconnect, user_id, holder=holder)
            return None
        except Exception as exc:  # noqa: BLE001 - classified below
            if _rotation_ruled_out(exc):
                _logger.warning("%s token refresh failed for %s: %s", provider, user_id, exc)
                return Refreshed(conn)
            # The request may have reached the provider and rotated the chain.
            outcome_known = False
            _logger.error(
                "%s token refresh for %s ended without an answer; needs reconnect: %s",
                provider,
                user_id,
                exc,
            )
            await _fail_closed(store, user_id, holder)
            return Refreshed(conn)
        if not await _persist_minted(persist, tokens, holder, provider, user_id):
            # The stored refresh token is spent; never let a later refresh replay it.
            outcome_known = False
            _logger.error(
                "%s refreshed token for %s could not be stored; needs reconnect", provider, user_id
            )
            await _fail_closed(store, user_id, holder)
        return Refreshed(conn, minted=tokens)
    except BaseException:
        outcome_known = False
        raise
    finally:
        if outcome_known:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(store.release_refresh_lease, user_id, holder=holder)


def _rotation_ruled_out(exc: BaseException) -> bool:
    """Whether a failed refresh certainly left the chain unrotated.

    A connect failure never reached the provider, and a provider error response
    (raised by the clients as their own errors) issued no tokens. Any other
    transport failure may have lost the answer to a refresh that succeeded.
    """
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return True
    return not isinstance(exc, httpx.TransportError)


async def _fail_closed(store: _LeasedStore[EntityT], user_id: str, holder: str) -> None:
    """Flag a reconnect; if even that fails, the in-flight marker stays behind."""
    with contextlib.suppress(Exception):
        await asyncio.to_thread(store.mark_needs_reconnect, user_id, holder=holder)


async def _current_or_none(
    store: _LeasedStore[EntityT], user_id: str, current: EntityT
) -> Refreshed[EntityT, TokensT] | None:
    """After losing the lease: ``None`` if the connection now needs a reconnect."""
    if await asyncio.to_thread(store.needs_reconnect, user_id):
        return None
    return Refreshed(current)


async def _persist_minted(
    persist: Callable[[TokensT, str], bool],
    tokens: TokensT,
    holder: str,
    provider: str,
    user_id: str,
) -> bool:
    """Store minted tokens, retrying a failing write while the lease is still ours.

    :returns: ``False`` when every attempt raised or the lease was lost.
    """
    for delay in (0.0, *_PERSIST_RETRY_S):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await asyncio.to_thread(persist, tokens, holder)
        except Exception as exc:  # noqa: BLE001 - retried, then fails closed
            _logger.warning("%s refreshed token for %s not stored: %s", provider, user_id, exc)
    return False


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
