"""Provider-agnostic per-user integration credential store.

Backs every "Connect …" integration (GitHub App today, MCP connectors later):
one encrypted secret blob + non-secret metadata per
``(workspace_id, user_id, provider, account_id)``. The secret material is the
only thing encrypted (via a :class:`~omnigent.stores.credential_store.secret_cipher.SecretCipher`);
it is the sole place ciphertext ⇄ plaintext crosses, so callers work with plain
:class:`~omnigent.entities.ProviderConnection` entities. Provider-specific
typed façades (e.g. :class:`~omnigent.connections.github.GithubConnectionStore`)
sit on top of this. See ``designs/CREDENTIAL_STORE.md``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import SqlConnection, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import ProviderConnection
from omnigent.stores.credential_store.secret_cipher import SecretCipher

_logger = logging.getLogger(__name__)

# Refresh-coordination state kept in the row's plaintext metadata, so no
# migration is needed. Never token material.
_LEASE_HOLDER = "refresh_lease_holder"
_LEASE_UNTIL = "refresh_lease_until"
_NEEDS_RECONNECT = "needs_reconnect"


def _enc_context(
    workspace_id: int, user_id: str, provider: str, account_id: str
) -> dict[str, str]:
    """The row's identity, passed to the cipher so each row encrypts under its
    own derived key (per-user encryption). Decryption must use the same
    identity, so it is always taken from the row being read."""
    return {
        "workspace_id": str(workspace_id),
        "user_id": user_id,
        "provider": provider,
        "account_id": account_id,
    }


def _safe_json_obj(raw: str | None) -> dict[str, Any] | None:
    """Parse a JSON object column, returning ``None`` on empty/corrupt/non-object
    data instead of raising.

    The vend path is deliberately soft-fail: :meth:`SecretCipher.decrypt`
    already degrades a wrong key to ``None`` (⇒ reconnect), so the JSON parse
    that immediately follows must not re-introduce a 500 on a truncated or
    malformed column.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class CredentialStore:
    """SQLAlchemy-backed persistence for per-user integration credentials.

    :param storage_location: SQLAlchemy database URI (shares the pool with the
        other stores via :func:`get_or_create_engine`).
    :param secret_cipher: Cipher for the secret blob at rest.
    """

    def __init__(self, storage_location: str, secret_cipher: SecretCipher) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.credential_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.credential_store",
            immediate=True,
        )
        self._cipher = secret_cipher

    def _build_entity(
        self, row: SqlConnection, secret: dict[str, Any] | None
    ) -> ProviderConnection:
        """Build an entity from a row and an already-resolved *secret*.

        Metadata is parsed soft-fail (a corrupt column reads as ``{}`` rather
        than 500-ing the caller).
        """
        return ProviderConnection(
            user_id=row.user_id,
            provider=row.provider,
            account_id=row.account_id,
            secret=secret,
            metadata=_safe_json_obj(row.metadata_json) or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _to_entity(self, row: SqlConnection, *, with_secret: bool) -> ProviderConnection:
        """Convert an ORM row to an entity, optionally decrypting the secret."""
        secret: dict[str, Any] | None = None
        if with_secret:
            context = _enc_context(row.workspace_id, row.user_id, row.provider, row.account_id)
            plaintext = self._cipher.decrypt(row.secret_enc, context=context)
            # ``None`` (wrong key/context or corrupt) degrades to "reconnect",
            # not a 500; the JSON parse is soft-fail for the same reason.
            secret = _safe_json_obj(plaintext) if plaintext is not None else None
        return self._build_entity(row, secret)

    def upsert(
        self,
        user_id: str,
        provider: str,
        *,
        secret: dict[str, Any],
        metadata: dict[str, Any],
        account_id: str = "",
    ) -> ProviderConnection:
        """Create or replace a user's connection for *provider*.

        Idempotent on ``(user_id, provider, account_id)``: reconnecting
        overwrites the secret and metadata in place, preserving ``created_at``.
        Concurrency-safe — two simultaneous connects (a double-clicked
        "Connect") don't 500 on a primary-key violation; the loser retries as an
        update.
        """
        now = now_epoch()
        workspace_id = current_workspace_id()
        pk = (workspace_id, user_id, provider, account_id)
        context = _enc_context(workspace_id, user_id, provider, account_id)
        secret_enc = self._cipher.encrypt(json.dumps(secret), context=context)
        metadata_json = json.dumps(metadata)

        def _apply(session: Session, row: SqlConnection | None) -> SqlConnection:
            if row is None:
                row = SqlConnection(
                    user_id=user_id,
                    provider=provider,
                    account_id=account_id,
                    secret_enc=secret_enc,
                    metadata_json=metadata_json,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.secret_enc = secret_enc
                row.metadata_json = metadata_json
                row.updated_at = now
            return row

        def initial_write(session: Session) -> ProviderConnection:
            require_active_account(session, user_id)
            row = _apply(session, session.get(SqlConnection, pk))
            session.flush()
            return self._build_entity(row, secret)

        try:
            return run_write_transaction(self._session_immediate, "upsert", initial_write)
        except IntegrityError:
            # A concurrent reconnect inserted the same PK between our get and
            # flush. Retry as an update in a fresh managed transaction.
            pass

        def conflict_write(session: Session) -> ProviderConnection:
            require_active_account(session, user_id)
            row = _apply(session, session.get(SqlConnection, pk))
            session.flush()
            return self._build_entity(row, secret)

        return run_write_transaction(self._session_immediate, "upsert_retry", conflict_write)

    def update_secret(
        self,
        user_id: str,
        provider: str,
        *,
        secret: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        account_id: str = "",
        lease_holder: str | None = None,
    ) -> bool:
        """Persist a refreshed secret (and optional metadata) for an existing row.

        Returns ``True`` when a row was updated, ``False`` when the connection
        was removed between read and refresh. The caller must not treat
        ``False`` as success: providers that rotate refresh tokens (GitHub does)
        have already spent the old one, so a silently-dropped refresh wedges the
        user until they reconnect — worth surfacing, not swallowing.

        :param lease_holder: When set, commit only if this holder still owns the
            row's refresh lease (see :meth:`acquire_refresh_lease`), and release
            it in the same write. ``False`` then also means the lease was lost.
        """
        workspace_id = current_workspace_id()
        context = _enc_context(workspace_id, user_id, provider, account_id)
        secret_enc = self._cipher.encrypt(json.dumps(secret), context=context)
        updated_at = now_epoch()

        def write(session: Session) -> bool:
            require_active_account(session, user_id)
            row = self._locked_row(session, workspace_id, user_id, provider, account_id)
            if row is None:
                return False
            current = _safe_json_obj(row.metadata_json) or {}
            if lease_holder is not None and current.get(_LEASE_HOLDER) != lease_holder:
                return False
            new_meta = dict(metadata) if metadata is not None else current
            # Fresh tokens clear the reconnect flag. Only the holder's own
            # commit releases the lease; an unleased write keeps someone else's.
            new_meta.pop(_NEEDS_RECONNECT, None)
            for key in (_LEASE_HOLDER, _LEASE_UNTIL):
                new_meta.pop(key, None)
                if lease_holder is None and key in current:
                    new_meta[key] = current[key]
            row.secret_enc = secret_enc
            row.metadata_json = json.dumps(new_meta)
            row.updated_at = updated_at
            return True

        updated = run_write_transaction(self._session_immediate, "update_secret", write)
        if not updated:
            _logger.warning(
                "credential_store: update_secret found no %s connection for the user "
                "(removed mid-refresh); the refreshed secret was discarded",
                provider,
            )
        return updated

    @staticmethod
    def _locked_row(
        session: Session, workspace_id: int, user_id: str, provider: str, account_id: str
    ) -> SqlConnection | None:
        """Read the row under a row lock for a short read-modify-write.

        ``FOR UPDATE`` on Postgres/MySQL/CockroachDB; SQLite ignores it, and the
        immediate write transaction already serializes writers there.
        """
        return session.execute(
            select(SqlConnection)
            .where(
                SqlConnection.workspace_id == workspace_id,
                SqlConnection.user_id == user_id,
                SqlConnection.provider == provider,
                SqlConnection.account_id == account_id,
            )
            .with_for_update()
        ).scalar_one_or_none()

    def _update_metadata(
        self,
        operation: str,
        user_id: str,
        provider: str,
        account_id: str,
        mutate: Callable[[dict[str, Any]], bool],
    ) -> bool:
        """Apply *mutate* to the row's metadata in one locked write.

        *mutate* edits the dict in place and returns whether to persist it.
        :returns: ``False`` when there is no row or *mutate* declined.
        """
        workspace_id = current_workspace_id()

        def write(session: Session) -> bool:
            row = self._locked_row(session, workspace_id, user_id, provider, account_id)
            if row is None:
                return False
            meta = _safe_json_obj(row.metadata_json) or {}
            if not mutate(meta):
                return False
            row.metadata_json = json.dumps(meta)
            return True

        return run_write_transaction(self._session_immediate, operation, write)

    def acquire_refresh_lease(
        self,
        user_id: str,
        provider: str,
        *,
        holder: str,
        ttl_s: int,
        account_id: str = "",
    ) -> bool:
        """Claim the right to refresh this connection's token for *ttl_s* seconds.

        Serializes refreshes across processes and server replicas without
        holding a DB lock across the upstream call: the claim is one short write,
        the refresh happens outside any transaction, and the result commits via
        :meth:`update_secret` with ``lease_holder``. A lease past its expiry can
        be taken over, so a crashed holder never wedges the connection.

        :returns: ``True`` when *holder* now owns the lease.
        """
        now = now_epoch()

        def claim(meta: dict[str, Any]) -> bool:
            if int(meta.get(_LEASE_UNTIL) or 0) > now and meta.get(_LEASE_HOLDER) != holder:
                return False
            meta[_LEASE_HOLDER] = holder
            meta[_LEASE_UNTIL] = now + ttl_s
            return True

        return self._update_metadata("acquire_refresh_lease", user_id, provider, account_id, claim)

    def release_refresh_lease(
        self, user_id: str, provider: str, *, holder: str, account_id: str = ""
    ) -> bool:
        """Drop *holder*'s lease without writing a secret. No-op if not held."""

        def release(meta: dict[str, Any]) -> bool:
            if meta.get(_LEASE_HOLDER) != holder:
                return False
            meta.pop(_LEASE_HOLDER, None)
            meta.pop(_LEASE_UNTIL, None)
            return True

        return self._update_metadata(
            "release_refresh_lease", user_id, provider, account_id, release
        )

    def mark_needs_reconnect(self, user_id: str, provider: str, *, account_id: str = "") -> bool:
        """Flag that the provider rejected the refresh token; clears any lease.

        Refreshes stop until the user reconnects (a reconnect's upsert rewrites
        the metadata, clearing the flag). See ``designs/CREDENTIAL_STORE.md``: a
        rotated-away credential degrades to "reconnect", never a retry storm.
        """

        def flag(meta: dict[str, Any]) -> bool:
            meta[_NEEDS_RECONNECT] = True
            meta.pop(_LEASE_HOLDER, None)
            meta.pop(_LEASE_UNTIL, None)
            return True

        return self._update_metadata("mark_needs_reconnect", user_id, provider, account_id, flag)

    def get(
        self,
        user_id: str,
        provider: str,
        *,
        account_id: str = "",
        with_secret: bool = False,
    ) -> ProviderConnection | None:
        """Look up a user's connection for *provider*.

        :param with_secret: When ``True``, decrypt the secret onto the returned
            entity (vend path). When ``False`` (default), ``secret`` is ``None``
            — the safe shape for status endpoints that must not surface secrets.
        :returns: ``None`` iff there is no connection row. A returned entity
            always exists; disambiguate its ``secret``: with ``with_secret=False``
            it is ``None`` by construction, while with ``with_secret=True`` a
            ``None`` secret means the ciphertext could not be decrypted (wrong
            key/context or corrupt) — treat that as "reconnect", not "no row".
        """
        with self._session("get") as session:
            row = session.get(
                SqlConnection, (current_workspace_id(), user_id, provider, account_id)
            )
            return self._to_entity(row, with_secret=with_secret) if row is not None else None

    def delete(self, user_id: str, provider: str, *, account_id: str = "") -> bool:
        """Remove a user's connection for *provider*. Returns ``True`` if a row went."""

        def write(session: Session) -> bool:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    delete(SqlConnection).where(
                        SqlConnection.workspace_id == current_workspace_id(),
                        SqlConnection.user_id == user_id,
                        SqlConnection.provider == provider,
                        SqlConnection.account_id == account_id,
                    )
                ),
            )
            return result.rowcount > 0

        return run_write_transaction(self._session_immediate, "delete", write)

    def list_for_user(self, user_id: str) -> list[ProviderConnection]:
        """All of a user's connections (metadata only), across providers."""
        with self._session("list_for_user") as session:
            rows = (
                session.execute(
                    select(SqlConnection).where(
                        SqlConnection.workspace_id == current_workspace_id(),
                        SqlConnection.user_id == user_id,
                    )
                )
                .scalars()
                .all()
            )
            return [self._to_entity(r, with_secret=False) for r in rows]

    def list_all(self, *, provider: str | None = None) -> list[ProviderConnection]:
        """All connections (metadata only), optionally filtered by *provider* — tests/admin."""
        with self._session("list_all") as session:
            stmt = select(SqlConnection).where(
                SqlConnection.workspace_id == current_workspace_id()
            )
            if provider is not None:
                stmt = stmt.where(SqlConnection.provider == provider)
            rows = session.execute(stmt).scalars().all()
            return [self._to_entity(r, with_secret=False) for r in rows]
