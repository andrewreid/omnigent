"""Resolve a user's ChatGPT access token for the credential broker.

Reads the stored connection and refreshes it when little life remains, through
the shared lease (:mod:`omnigent.connections.refresh`) so replicas never race on
the rotating refresh token — OpenAI's auth service detects refresh-token reuse.
Access tokens live ~10 days; refreshing with :data:`_REFRESH_MARGIN_S` left means
every vended token has at least that long to run. See
``designs/SUBSCRIPTION_BROKER.md``.
"""

from __future__ import annotations

import logging

from omnigent.connections.chatgpt import ChatgptConnectionStore
from omnigent.connections.refresh import refresh_coordinated
from omnigent.db.utils import now_epoch
from omnigent.entities import ChatgptConnection
from omnigent.server.chatgpt_oauth import ChatgptConfig, ChatgptOAuthClient, ChatgptTokenSet

_logger = logging.getLogger(__name__)

# Two days: vended tokens always outlive a long sandbox session, and the
# ~10-day chain is refreshed about every 8 days.
_REFRESH_MARGIN_S = 2 * 24 * 3600


def build_chatgpt_connection(
    database_url: str,
) -> tuple[ChatgptConfig | None, ChatgptConnectionStore | None]:
    """Build the ``(config, store)`` pair ``create_app`` takes, from env.

    Shared by ``omnigent server`` and the Docker entrypoint so the two can't
    drift. ``(config, None)`` — logged — when enabled without a cipher.
    """
    config = ChatgptConfig.from_env()
    if config is None:
        return None, None
    from omnigent.stores.credential_store import build_secret_cipher

    cipher = build_secret_cipher()
    if cipher is None:
        _logger.error(
            "ChatGPT subscription connection is enabled but disabled: configure the "
            "credential store's cipher (OMNIGENT_CREDENTIAL_KMS_KEY_ID or "
            "OMNIGENT_CREDENTIAL_VAULT_KEY)."
        )
        return config, None
    return config, ChatgptConnectionStore(database_url, cipher)


async def resolve_chatgpt_credential(
    user_id: str,
    *,
    store: ChatgptConnectionStore,
    client: ChatgptOAuthClient,
) -> dict[str, object] | None:
    """Resolve the ChatGPT broker payload for *user_id*, or ``None``.

    :returns: ``{"token", "account_id", "plan_type", "expires_at"}``. Never the
        refresh token or id token: those stay on the server.
    """

    def is_fresh(conn: ChatgptConnection) -> bool:
        expires_at = conn.access_expires_at
        return expires_at is not None and expires_at > now_epoch() + _REFRESH_MARGIN_S

    async def refresh(conn: ChatgptConnection) -> ChatgptTokenSet:
        return await client.refresh(ChatgptConnectionStore.token_set(conn))

    def persist(tokens: ChatgptTokenSet, holder: str) -> bool:
        return store.update_tokens(user_id, tokens, lease_holder=holder)

    resolved = await refresh_coordinated(
        store, user_id, is_fresh=is_fresh, refresh=refresh, persist=persist, provider="ChatGPT"
    )
    if resolved is None:
        return None
    if resolved.minted is not None:
        tokens = resolved.minted
        access, account, plan, expires = (
            tokens.access_token,
            tokens.account_id,
            tokens.plan_type,
            tokens.access_expires_at,
        )
    else:
        conn = resolved.connection
        access, account, plan, expires = (
            conn.access_token,
            conn.account_id,
            conn.plan_type,
            conn.access_expires_at,
        )
    # A failed refresh keeps a token that is still valid; never vend an expired one.
    if not access or not account or expires is None or expires <= now_epoch():
        return None
    return {"token": access, "account_id": account, "plan_type": plan, "expires_at": expires}
