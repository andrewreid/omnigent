"""Tests for the AWS KMS-backed at-rest secret cipher.

These use an in-memory fake KMS client (no AWS, no moto) to exercise
:class:`KmsSecretCipher`'s wrapping, encryption-context binding, and soft-fail
behaviour. Real KMS semantics are covered by the demo end-to-end verification.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import pytest
from botocore.exceptions import ClientError

from omnigent.stores.credential_store.secret_cipher import (
    CREDENTIAL_KMS_KEY_ENV_VAR,
    KmsSecretCipher,
    build_secret_cipher,
)

CTX_ALICE = {"workspace_id": "0", "user_id": "alice", "provider": "github", "account_id": ""}
CTX_BOB = {"workspace_id": "0", "user_id": "bob", "provider": "github", "account_id": ""}


class _FakeKmsClient:
    """In-memory stand-in for boto3's KMS client.

    ``encrypt`` records ``(key_id, encryption_context, plaintext)`` under an
    opaque blob; ``decrypt`` returns the plaintext only when the *same*
    encryption context is presented, otherwise raises the ``ClientError`` KMS
    raises for a context/key mismatch.
    """

    def __init__(self) -> None:
        self.store: dict[bytes, tuple[str, dict[str, str], bytes]] = {}
        self._n = 0

    def encrypt(self, *, KeyId: str, Plaintext: bytes, EncryptionContext: dict[str, str]) -> Any:
        self._n += 1
        blob = f"ct-{self._n}".encode()
        self.store[blob] = (KeyId, dict(EncryptionContext), Plaintext)
        return {"CiphertextBlob": blob, "KeyId": KeyId}

    def decrypt(
        self, *, CiphertextBlob: bytes, EncryptionContext: dict[str, str], KeyId: str | None = None
    ) -> Any:
        entry = self.store.get(CiphertextBlob)
        if entry is None or entry[1] != dict(EncryptionContext):
            raise ClientError(
                {"Error": {"Code": "InvalidCiphertextException", "Message": "mismatch"}},
                "Decrypt",
            )
        return {"Plaintext": entry[2], "KeyId": entry[0]}

    def generate_data_key(
        self, *, KeyId: str, KeySpec: str, EncryptionContext: dict[str, str]
    ) -> Any:
        assert KeySpec == "AES_256"
        key = os.urandom(32)
        wrapped = self.encrypt(KeyId=KeyId, Plaintext=key, EncryptionContext=EncryptionContext)
        return {"Plaintext": key, "CiphertextBlob": wrapped["CiphertextBlob"], "KeyId": KeyId}


def _cipher() -> KmsSecretCipher:
    return KmsSecretCipher("alias/omnigent-credentials", client=_FakeKmsClient())


def test_encrypt_decrypt_roundtrip() -> None:
    box = _cipher()
    ct = box.encrypt("ghu_secret_token", context=CTX_ALICE)
    assert ct != "ghu_secret_token"
    assert box.decrypt(ct, context=CTX_ALICE) == "ghu_secret_token"


def test_wrong_context_returns_none() -> None:
    """A secret encrypted for one identity can't be read under another's."""
    box = _cipher()
    ct = box.encrypt("ghu_alice", context=CTX_ALICE)
    assert box.decrypt(ct, context=CTX_BOB) is None


def test_context_is_required() -> None:
    # There is no unbound/global-key path: the identity is mandatory.
    box = _cipher()
    with pytest.raises(TypeError):
        box.encrypt("x")  # type: ignore[call-arg]


def test_empty_context_values_dropped() -> None:
    # KMS rejects empty encryption-context values; account_id="" is dropped
    # consistently, and a named account still produces a distinct context.
    client = _FakeKmsClient()
    box = KmsSecretCipher("k", client=client)
    ct = box.encrypt("x", context=CTX_ALICE)
    (_key, stored_ctx, _pt) = client.store[next(iter(client.store))]
    assert "account_id" not in stored_ctx
    assert stored_ctx == {"workspace_id": "0", "user_id": "alice", "provider": "github"}
    # A named account yields a different, still-decryptable context.
    named = {**CTX_ALICE, "account_id": "org1"}
    assert box.decrypt(box.encrypt("y", context=named), context=named) == "y"
    assert box.decrypt(ct, context=named) is None


def test_corrupt_ciphertext_returns_none() -> None:
    box = _cipher()
    assert box.decrypt("not-valid-base64!!", context=CTX_ALICE) is None


def test_unknown_blob_returns_none() -> None:
    # Valid base64 but not a real KMS blob (InvalidCiphertextException) → reconnect.
    box = _cipher()
    assert box.decrypt("aGVsbG8=", context=CTX_ALICE) is None


def test_operational_error_propagates() -> None:
    # AccessDenied / disabled key are operational, not per-row: they must NOT be
    # masked as "reconnect" (which wouldn't help); they propagate.
    class _DenyingClient(_FakeKmsClient):
        def decrypt(self, **kwargs: Any) -> Any:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Decrypt"
            )

    box = KmsSecretCipher("k", client=_DenyingClient())
    ct = box.encrypt("x", context=CTX_ALICE)
    with pytest.raises(ClientError):
        box.decrypt(ct, context=CTX_ALICE)


def test_build_secret_cipher_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CREDENTIAL_KMS_KEY_ENV_VAR, raising=False)
    assert build_secret_cipher() is None


def test_build_secret_cipher_disabled_with_whitespace_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_KMS_KEY_ENV_VAR, "   \t\n")
    assert build_secret_cipher() is None


def test_build_secret_cipher_from_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CREDENTIAL_KMS_KEY_ENV_VAR, "arn:aws:kms:us-east-1:1:key/abc")
    cipher = build_secret_cipher()
    assert isinstance(cipher, KmsSecretCipher)


# --- envelope for secrets over the KMS Encrypt limit -------------------------

_BIG = "x" * 5000  # a full OAuth token set can exceed KMS Encrypt's 4096 bytes


def test_small_secret_keeps_the_direct_kms_format() -> None:
    sealed = _cipher().encrypt('{"access_token": "t"}', context=CTX_ALICE)
    assert not sealed.startswith("kmsenv1:")


def test_large_secret_round_trips_through_an_envelope() -> None:
    box = _cipher()
    sealed = box.encrypt(_BIG, context=CTX_ALICE)
    assert sealed.startswith("kmsenv1:")
    assert _BIG not in sealed
    assert box.decrypt(sealed, context=CTX_ALICE) == _BIG


def test_envelope_is_bound_to_its_row_identity() -> None:
    box = _cipher()
    sealed = box.encrypt(_BIG, context=CTX_ALICE)
    assert box.decrypt(sealed, context=CTX_BOB) is None


def test_tampered_envelope_reads_as_unreadable() -> None:
    box = _cipher()
    sealed = box.encrypt(_BIG, context=CTX_ALICE)
    body = json.loads(base64.b64decode(sealed.removeprefix("kmsenv1:")))
    data = bytearray(base64.b64decode(body["data"]))
    data[0] ^= 0x01
    body["data"] = base64.b64encode(bytes(data)).decode()
    tampered = "kmsenv1:" + base64.b64encode(json.dumps(body).encode()).decode()

    assert box.decrypt(tampered, context=CTX_ALICE) is None
    assert box.decrypt("kmsenv1:not-base64!", context=CTX_ALICE) is None
