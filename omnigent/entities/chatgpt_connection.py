"""Per-user ChatGPT subscription connection entity.

Plain dataclass returned from
:class:`~omnigent.connections.chatgpt.ChatgptConnectionStore`. Token material is
carried encrypted in the store row; the token fields hold the *decrypted* values
and are only populated on the server-side vend path, never serialized to a client.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ChatgptConnection:
    """A user's connected ChatGPT subscription (a server-owned refresh chain).

    :param user_id: The omnigent user id the connection belongs to.
    :param access_token: Decrypted access token (a JWT, ~10 day life), or ``None``
        on a metadata-only view.
    :param refresh_token: Decrypted refresh token; never leaves the server.
    :param id_token: Decrypted id token, or ``None``.
    :param account_id: ChatGPT workspace/account id (``chatgpt_account_id``).
    :param access_expires_at: Access-token expiry, epoch seconds, or ``None``.
    :param plan_type: Subscription plan, e.g. ``"pro"``.
    :param email: The ChatGPT account's email.
    :param created_at: Unix epoch seconds the connection was first made.
    :param updated_at: Unix epoch seconds of the last refresh / reconnect.
    """

    user_id: str
    access_token: str | None
    refresh_token: str | None
    id_token: str | None
    account_id: str | None
    access_expires_at: int | None
    plan_type: str | None
    email: str | None
    created_at: int
    updated_at: int
