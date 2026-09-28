"""ChatGPT subscription OAuth: config and client for the device-code connect flow.

The server owns the user's ChatGPT refresh chain (a fresh device-code login, so
the user's own ``codex`` login elsewhere is untouched) and vends short-lived
access tokens to their sandboxes. There is no OAuth client a third party can
register for subscription entitlement, so this runs under codex's own client id
(overridable, as codex's ``--experimental_client-id`` is). Endpoints and payloads
mirror ``codex-rs/login`` at ``rust-v0.154.0``. See
``designs/SUBSCRIPTION_BROKER.md`` ("ChatGPT provider + device-code seam").
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
from typing import Any

import httpx

from omnigent.connections.refresh import RefreshRejected

_logger = logging.getLogger(__name__)

#: Opt-in switch for the Settings → ChatGPT connect flow.
CHATGPT_SUBSCRIPTION_ENV_VAR = "OMNIGENT_CHATGPT_SUBSCRIPTION_CONNECT"
#: HMAC key for the device-flow handle. Set it when running several replicas.
CHATGPT_STATE_KEY_ENV_VAR = "OMNIGENT_CHATGPT_STATE_KEY"
DEFAULT_ISSUER = "https://auth.openai.com"
#: codex's first-party OAuth client (``CLIENT_ID`` in codex-rs/login).
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_HTTP_TIMEOUT_S = 15.0
_USER_AGENT = "omnigent"
_AUTH_CLAIMS = "https://api.openai.com/auth"
# Refresh failures that mean the chain is dead (codex classifies these as permanent).
_REJECTED_CODES = frozenset(
    {"invalid_grant", "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"}
)


class ChatgptOAuthError(Exception):
    """A ChatGPT auth-service interaction failed."""


class ChatgptRefreshRejected(ChatgptOAuthError, RefreshRejected):
    """The refresh token was rejected (expired, reused or revoked); reconnect."""


@dataclasses.dataclass(frozen=True)
class ChatgptConfig:
    """Where and as whom the device-code flow runs.

    :param issuer: Auth service origin, e.g. ``"https://auth.openai.com"``.
    :param client_id: OAuth client id the tokens are issued to.
    :param state_key: HMAC key for the signed device-flow handle.
    """

    issuer: str = DEFAULT_ISSUER
    client_id: str = DEFAULT_CLIENT_ID
    state_key: str = ""

    @classmethod
    def from_env(cls) -> ChatgptConfig | None:
        """The config when :data:`CHATGPT_SUBSCRIPTION_ENV_VAR` is truthy, else ``None``."""
        raw = (os.environ.get(CHATGPT_SUBSCRIPTION_ENV_VAR) or "").strip().lower()
        if raw not in ("1", "true", "yes", "on"):
            return None
        state_key = (os.environ.get(CHATGPT_STATE_KEY_ENV_VAR) or "").strip()
        if not state_key:
            # In-flight connects (≤15 min) won't survive a restart or another replica.
            _logger.warning(
                "%s is unset; using a per-process key for ChatGPT connect handles",
                CHATGPT_STATE_KEY_ENV_VAR,
            )
            state_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        return cls(
            issuer=(os.environ.get("OMNIGENT_CHATGPT_ISSUER") or DEFAULT_ISSUER).rstrip("/"),
            client_id=os.environ.get("OMNIGENT_CHATGPT_CLIENT_ID") or DEFAULT_CLIENT_ID,
            state_key=state_key,
        )


@dataclasses.dataclass(frozen=True)
class DeviceCode:
    """A started device-code login: show ``user_code`` at ``verification_url``."""

    device_auth_id: str
    user_code: str
    verification_url: str
    interval_s: int


@dataclasses.dataclass(frozen=True)
class ChatgptTokenSet:
    """Tokens plus the non-secret identity read from their claims.

    :param access_expires_at: Access-token ``exp`` (epoch seconds), or ``None``.
    :param account_id: ChatGPT workspace/account id (``chatgpt_account_id``).
    """

    access_token: str
    refresh_token: str | None
    id_token: str | None
    access_expires_at: int | None
    account_id: str | None
    plan_type: str | None
    email: str | None


def _jwt_claims(token: str | None) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (it came straight from the issuer)."""
    if not token or token.count(".") < 2:
        return {}
    seg = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_set_from_payload(
    payload: dict[str, Any], *, previous: ChatgptTokenSet | None = None
) -> ChatgptTokenSet:
    """Build a :class:`ChatgptTokenSet`; fields a refresh omits carry over from *previous*."""
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ChatgptOAuthError("ChatGPT token response missing access_token")
    id_token = payload.get("id_token") or (previous.id_token if previous else None)
    id_claims = _jwt_claims(id_token)
    auth = id_claims.get(_AUTH_CLAIMS) or _jwt_claims(access).get(_AUTH_CLAIMS) or {}
    exp = _jwt_claims(access).get("exp")
    return ChatgptTokenSet(
        access_token=access,
        refresh_token=payload.get("refresh_token")
        or (previous.refresh_token if previous else None),
        id_token=id_token,
        access_expires_at=int(exp) if isinstance(exp, int | float) else None,
        account_id=auth.get("chatgpt_account_id") or (previous.account_id if previous else None),
        plan_type=auth.get("chatgpt_plan_type") or (previous.plan_type if previous else None),
        email=id_claims.get("email") or (previous.email if previous else None),
    )


def _error_code(resp: httpx.Response) -> str | None:
    """The OAuth error code from a JSON body (``{"error": "…"}`` or ``{"error": {"code": …}}``)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("code") if isinstance(err.get("code"), str) else None
    if isinstance(err, str):
        return err
    return body.get("code") if isinstance(body.get("code"), str) else None


class ChatgptOAuthClient:
    """HTTP client for the ChatGPT auth service's device-code and token endpoints."""

    def __init__(
        self, config: ChatgptConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._config = config
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S,
            transport=self._transport,
            headers={"User-Agent": _USER_AGENT},
        )

    async def request_device_code(self) -> DeviceCode:
        """Start a device-code login."""
        url = f"{self._config.issuer}/api/accounts/deviceauth/usercode"
        async with self._http() as http:
            resp = await http.post(url, json={"client_id": self._config.client_id})
        if resp.status_code == 404:
            raise ChatgptOAuthError(
                "Device code login isn't available. Enable it in ChatGPT → Settings → Security."
            )
        if resp.status_code != 200:
            raise ChatgptOAuthError(f"device code request failed ({resp.status_code})")
        data = resp.json()
        user_code = data.get("user_code") or data.get("usercode")
        if not data.get("device_auth_id") or not user_code:
            raise ChatgptOAuthError("device code response missing fields")
        try:
            interval = max(1, int(str(data.get("interval") or 5).strip()))
        except ValueError:
            interval = 5
        return DeviceCode(
            device_auth_id=str(data["device_auth_id"]),
            user_code=str(user_code),
            verification_url=f"{self._config.issuer}/codex/device",
            interval_s=interval,
        )

    async def poll_device_code(
        self, device_auth_id: str, user_code: str
    ) -> ChatgptTokenSet | None:
        """One poll: ``None`` while the user hasn't approved yet, else the tokens."""
        url = f"{self._config.issuer}/api/accounts/deviceauth/token"
        async with self._http() as http:
            resp = await http.post(
                url, json={"device_auth_id": device_auth_id, "user_code": user_code}
            )
            if resp.status_code in (403, 404):
                return None
            if resp.status_code != 200:
                raise ChatgptOAuthError(f"device code poll failed ({resp.status_code})")
            grant = resp.json()
            # Exchange the issued authorization code with its PKCE verifier.
            token = await http.post(
                f"{self._config.issuer}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": grant.get("authorization_code", ""),
                    "redirect_uri": f"{self._config.issuer}/deviceauth/callback",
                    "client_id": self._config.client_id,
                    "code_verifier": grant.get("code_verifier", ""),
                },
            )
        if token.status_code != 200:
            raise ChatgptOAuthError(f"device code exchange failed ({token.status_code})")
        tokens = token_set_from_payload(token.json())
        if not tokens.refresh_token:
            raise ChatgptOAuthError("device code exchange returned no refresh token")
        return tokens

    async def refresh(self, previous: ChatgptTokenSet) -> ChatgptTokenSet:
        """Rotate *previous*'s refresh token for fresh tokens.

        :raises ChatgptRefreshRejected: The chain is dead (401, ``invalid_grant``,
            or an expired/reused/revoked refresh token).
        :raises ChatgptOAuthError: Any other (transient) failure.
        """
        if not previous.refresh_token:
            raise ChatgptOAuthError("no refresh token stored")
        async with self._http() as http:
            resp = await http.post(
                f"{self._config.issuer}/oauth/token",
                json={
                    "client_id": self._config.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": previous.refresh_token,
                },
            )
        if resp.status_code == 200:
            return token_set_from_payload(resp.json(), previous=previous)
        code = _error_code(resp)
        if resp.status_code == 401 or (code or "").lower() in _REJECTED_CODES:
            raise ChatgptRefreshRejected(
                f"ChatGPT refused the refresh token ({code or resp.status_code})"
            )
        raise ChatgptOAuthError(f"ChatGPT token refresh failed ({resp.status_code})")

    async def revoke(self, refresh_token: str) -> None:
        """Revoke the refresh chain upstream, which also kills its access tokens.

        Best-effort: raises :class:`ChatgptOAuthError` on failure; callers log it.
        """
        async with self._http() as http:
            resp = await http.post(
                f"{self._config.issuer}/oauth/revoke",
                json={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": self._config.client_id,
                },
            )
        if resp.status_code >= 300:
            raise ChatgptOAuthError(f"ChatGPT token revoke failed ({resp.status_code})")
