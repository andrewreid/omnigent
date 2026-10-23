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


class RefreshNotRotated(Exception):
    """The refresh certainly issued no tokens: the provider answered with an
    error, or nothing was sent. The stored refresh token is still current.
    Raised by provider clients; any other failure counts as an unknown outcome."""


_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


def oauth_error_code(payload: object) -> str | None:
    """The ``error`` code of an OAuth error body that carries no tokens, else ``None``.

    A body with both tokens and an error is ambiguous and yields ``None``.
    """
    if not isinstance(payload, dict) or any(key in payload for key in _TOKEN_FIELDS):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) and error else None


def provider_refusal_code(resp: httpx.Response) -> str | None:
    """The ``error`` code when *resp* is the provider's explicit refusal, else ``None``.

    Only a 4xx JSON error body counts: a 5xx or an unreadable body may come from
    a proxy after the provider already issued tokens.
    """
    if not 400 <= resp.status_code < 500:
        return None
    try:
        return oauth_error_code(resp.json())
    except ValueError:
        return None


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
    def get_usable(self, user_id: str) -> EntityT | None: ...
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
    conn = await asyncio.to_thread(store.get_usable, user_id)
    if conn is None:
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
            return await _current_or_none(store, user_id)
        conn = leased
        if is_fresh(conn):
            return Refreshed(conn)
        if not await asyncio.to_thread(store.mark_refresh_inflight, user_id, holder=holder):
            return await _current_or_none(store, user_id)
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
        stored = await _persist_minted(persist, tokens, holder, provider, user_id)
        if stored is None:
            # The stored refresh token is spent; never let a later refresh replay it.
            outcome_known = False
            _logger.error(
                "%s refreshed token for %s could not be stored; needs reconnect", provider, user_id
            )
            await _fail_closed(store, user_id, holder)
        elif not stored:
            # The lease was lost (expired, fenced by a disconnect, or replaced by a
            # reconnect): the minted chain is discarded unvended, and the marker
            # left behind makes the next acquirer fail closed.
            outcome_known = False
            _logger.error(
                "%s refreshed token for %s was refused by the store; discarded", provider, user_id
            )
            return await _current_or_none(store, user_id)
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

    Only a connect failure (never reached the provider) or an explicit provider
    error (:class:`RefreshNotRotated`) qualifies. Anything else, such as a lost
    answer or a success response that couldn't be parsed, may have rotated it.
    """
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | RefreshNotRotated)


async def _fail_closed(store: _LeasedStore[EntityT], user_id: str, holder: str) -> None:
    """Flag a reconnect; if even that fails, the in-flight marker stays behind."""
    with contextlib.suppress(Exception):
        await asyncio.to_thread(store.mark_needs_reconnect, user_id, holder=holder)


async def _current_or_none(
    store: _LeasedStore[EntityT], user_id: str
) -> Refreshed[EntityT, TokensT] | None:
    """After losing the lease: the stored connection, or ``None`` if it's gone or
    needs a reconnect."""
    conn = await asyncio.to_thread(store.get_usable, user_id)
    return Refreshed(conn) if conn is not None else None


async def _persist_minted(
    persist: Callable[[TokensT, str], bool],
    tokens: TokensT,
    holder: str,
    provider: str,
    user_id: str,
) -> bool | None:
    """Store minted tokens, retrying a failing write while the lease is still ours.

    :returns: ``True`` once stored, ``False`` when the store refused the write
        (the lease was lost), ``None`` when every attempt raised.
    """
    for delay in (0.0, *_PERSIST_RETRY_S):
        if delay:
            await asyncio.sleep(delay)
        try:
            return bool(await asyncio.to_thread(persist, tokens, holder))
        except Exception as exc:  # noqa: BLE001 - retried, then fails closed
            _logger.warning("%s refreshed token for %s not stored: %s", provider, user_id, exc)
    return None


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
        conn = await asyncio.to_thread(store.get_usable, user_id)
        if conn is None:
            return None
        if is_fresh(conn):
            return Refreshed(conn)
    return Refreshed(current)
