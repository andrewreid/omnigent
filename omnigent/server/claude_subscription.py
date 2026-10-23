"""Claude subscription connection: config, token validation, broker resolver.

A user pastes the long-lived token ``claude setup-token`` mints; the server
stores it encrypted and vends it to the owner's managed sandboxes, where the
host exports it as ``CLAUDE_CODE_OAUTH_TOKEN``. Unlike ChatGPT there is no
refresh chain to own: Claude Code offers no refresh hook to third-party hosts.
See ``designs/SUBSCRIPTION_BROKER.md`` ("Claude subscriptions: Option S").
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
from typing import Any

from omnigent.connections.claude import ClaudeConnectionStore

_logger = logging.getLogger(__name__)

#: Opt-in switch for the Settings → Claude connect flow. The credential store's
#: cipher must also be configured, or the store (and so the flow) stays off.
CLAUDE_SUBSCRIPTION_ENV_VAR = "OMNIGENT_CLAUDE_SUBSCRIPTION_CONNECT"

# ``claude setup-token`` output, e.g. ``sk-ant-oat01-<base64url>``.
_SETUP_TOKEN = re.compile(r"^sk-ant-oat[0-9]{2}-[A-Za-z0-9_-]{16,}$")


@dataclasses.dataclass(frozen=True)
class ClaudeSubscriptionConfig:
    """Enables the Claude subscription connection. Carries no settings yet:
    the token is minted by the user's own CLI, so there is no OAuth client."""

    @classmethod
    def from_env(cls) -> ClaudeSubscriptionConfig | None:
        """The config when :data:`CLAUDE_SUBSCRIPTION_ENV_VAR` is truthy, else ``None``."""
        raw = (os.environ.get(CLAUDE_SUBSCRIPTION_ENV_VAR) or "").strip().lower()
        return cls() if raw in ("1", "true", "yes", "on") else None


def build_claude_connection(
    database_url: str,
) -> tuple[ClaudeSubscriptionConfig | None, ClaudeConnectionStore | None]:
    """Build the ``(config, store)`` pair ``create_app`` takes, from env.

    Shared by ``omnigent server`` and the Docker entrypoint so the two can't
    drift. ``(None, None)`` when the flow is off; ``(config, None)`` — logged —
    when it is on but no credential-store cipher is configured.
    """
    config = ClaudeSubscriptionConfig.from_env()
    if config is None:
        return None, None
    from omnigent.stores.credential_store import build_secret_cipher

    cipher = build_secret_cipher()
    if cipher is None:
        _logger.error(
            "%s is set but the Claude subscription connection is disabled: configure "
            "the credential store's cipher (OMNIGENT_CREDENTIAL_KMS_KEY_ID or "
            "OMNIGENT_CREDENTIAL_VAULT_KEY).",
            CLAUDE_SUBSCRIPTION_ENV_VAR,
        )
        return config, None
    return config, ClaudeConnectionStore(database_url, cipher)


def normalize_setup_token(raw: str) -> str | None:
    """Return the trimmed token when it looks like ``claude setup-token`` output.

    Rejects anything else (an API key, a truncated paste, stray text) so a bad
    paste fails at connect time instead of as a 401 inside a sandbox.
    """
    token = "".join(raw.split())
    return token if _SETUP_TOKEN.match(token) else None


async def resolve_claude_credential(
    user_id: str,
    *,
    store: ClaudeConnectionStore,
    client: Any = None,  # noqa: ARG001 - registry signature; no OAuth client
) -> dict[str, object] | None:
    """Resolve the Claude broker payload for *user_id*, or ``None`` if not linked.

    :returns: ``{"token": <oauth token>}``; the host exports it as
        ``CLAUDE_CODE_OAUTH_TOKEN``.
    """
    connection = await asyncio.to_thread(store.get, user_id, with_tokens=True)
    if connection is None or not connection.oauth_token:
        return None
    return {"token": connection.oauth_token}
