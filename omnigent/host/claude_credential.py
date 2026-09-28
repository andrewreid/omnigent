"""Export the session owner's brokered Claude subscription token in a sandbox.

When the owner has connected a Claude subscription (Settings → Claude, a pasted
``claude setup-token`` token), the server's credential broker vends it to the
owner's managed sandboxes. At host startup this sets ``CLAUDE_CODE_OAUTH_TOKEN``
in the host's environment; the host forwards that name to every runner it
spawns (:data:`omnigent.host.connect.HARNESS_CREDENTIAL_ENV_VARS`), so
claude-sdk and claude-native pick it up exactly as they would a token injected
through the deployment's harness-credentials Secret.

The token is long-lived (``setup-token`` issues a ~1 year token), so one fetch
per host launch is enough — no refresher. A connected owner's token takes
precedence over a fleet-wide one from the Secret: the per-user connection is
the more specific grant.

Trust boundary: as for the other broker integrations, the sandbox. Any process
there can read the exported token; disconnecting stops future vends but cannot
revoke an already-exported token.
"""

from __future__ import annotations

import logging
import os

import httpx

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

_logger = logging.getLogger(__name__)

_TIMEOUT_S = 15.0
CLAUDE_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _fetch(server_url: str, host_id: str, host_token: str) -> dict | None:
    """Fetch the Claude broker payload, or ``None`` on any failure."""
    url = f"{server_url.rstrip('/')}/v1/hosts/{host_id}/credentials/claude"
    try:
        resp = httpx.get(url, headers={MANAGED_HOST_TOKEN_HEADER: host_token}, timeout=_TIMEOUT_S)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def configure_host_claude(server_url: str, host_id: str) -> bool:
    """Export the owner's brokered Claude token as ``CLAUDE_CODE_OAUTH_TOKEN``.

    Sandbox-only (``IS_SANDBOX=1``): a local ``omnigent host`` shares the
    developer's own Claude login, which this must never override. Best-effort
    and non-raising: a no-op when the host token is absent, the broker is
    unreachable or doesn't know ``claude`` (older server, flow disabled), or
    the owner hasn't connected — any ambient token is left untouched.

    :returns: ``True`` when the brokered token was exported.
    """
    if os.environ.get("IS_SANDBOX") != "1":
        return False
    host_token = (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()
    if not host_token:
        return False
    data = _fetch(server_url, host_id, host_token)
    if not data or not data.get("connected") or not data.get("token"):
        return False
    token = str(data["token"])
    if os.environ.get(CLAUDE_TOKEN_ENV_VAR) not in (None, "", token):
        _logger.info(
            "Claude credential: owner's connected subscription replaces the deployment's %s",
            CLAUDE_TOKEN_ENV_VAR,
        )
    os.environ[CLAUDE_TOKEN_ENV_VAR] = token
    _logger.info("Claude credential: exported the owner's brokered subscription token")
    return True
