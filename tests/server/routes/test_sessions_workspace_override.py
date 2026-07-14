"""Deny-by-default restriction for a multipart/CLI workspace override.

``_create_session_from_bundle`` honors ``metadata.workspace`` only on
the fully-validated, co-located local no-host surface and rejects it on
every other topology (rather than validating against the wrong
filesystem):

- host_id set  -> reject (the runner launches on a separate host).
- not a local single-user server -> reject (a remote state server is
  not co-located with the runner, so server-side realpath would resolve
  the wrong filesystem).

Only the co-located local case reaches the canonical
``validate_workspace_no_host`` check and is persisted.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import sessions
from omnigent.server.schemas import CreatedSessionResponse, SessionCreateMetadata
from omnigent.stores import ConversationStore
from omnigent.stores.artifact_store import ArtifactStore


class _FakeArtifactStore:
    def put(self, location: str, data: bytes) -> None:
        return None


# Reject tests never touch the stores (the deny-by-default gate fires
# first); accept test routes writes through monkeypatched helpers.
_STORE = cast(ConversationStore, object())
_ARTIFACTS = cast(ArtifactStore, _FakeArtifactStore())


def _meta(**kw: Any) -> SessionCreateMetadata:
    return SessionCreateMetadata(**kw)


def test_workspace_with_host_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """FIX 1: a multipart workspace + host_id is refused, never persisted."""
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    with pytest.raises(OmnigentError) as ei:
        sessions._create_session_from_bundle(
            _STORE,  # conversation_store — never reached (gate fires first)
            _ARTIFACTS,
            _meta(workspace="/work/repo", host_id="host_abc"),
            b"bundle-bytes",
        )
    assert ei.value.code == ErrorCode.INVALID_INPUT
    assert "host_id" in ei.value.message


def test_workspace_on_non_colocated_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX 2: without the local single-user marker (remote server), reject."""
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
    with pytest.raises(OmnigentError) as ei:
        sessions._create_session_from_bundle(
            _STORE,
            _ARTIFACTS,
            _meta(workspace="/work/repo"),
            b"bundle-bytes",
        )
    assert ei.value.code == ErrorCode.INVALID_INPUT
    assert "co-located local server" in ei.value.message


def test_colocated_local_workspace_is_validated_and_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    FIX 2 (positive): a co-located local session with a valid workspace
    still passes — canonically validated and persisted as the canonical
    path (no regression for ``omnigent run --workspace`` locally).
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    repo = tmp_path / "repo"
    repo.mkdir()

    # Stub bundle parsing: relative os_env.cwd (Patricia-style) imposes
    # no boundary, so any existing absolute dir validates.
    monkeypatch.setattr(
        sessions,
        "validate_agent_bundle",
        lambda *a, **k: SimpleNamespace(
            name="patricia", description=None, os_env=SimpleNamespace(cwd=".")
        ),
    )

    captured: dict[str, Any] = {}

    def _fake_persist(
        conversation_store: Any,
        artifact_store: Any,
        metadata: SessionCreateMetadata,
        **kw: Any,
    ) -> CreatedSessionResponse:
        captured["workspace"] = metadata.workspace
        return CreatedSessionResponse(
            session_id="conv_1", agent_id="ag_1", agent_name="patricia"
        )

    monkeypatch.setattr(sessions, "_persist_stored_session_bundle", _fake_persist)

    result = sessions._create_session_from_bundle(
        _STORE,
        _ARTIFACTS,
        _meta(workspace=str(repo)),
        b"bundle-bytes",
    )
    assert result.session_id == "conv_1"
    # Persisted workspace is the canonical (realpath) directory.
    assert captured["workspace"] == os.path.realpath(str(repo))
