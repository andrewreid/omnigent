"""Deny-by-default restriction for a multipart/CLI workspace override.

``_create_session_from_bundle`` honors ``metadata.workspace`` only on
the fully-validated, co-located local no-host surface and rejects it on
every other topology (rather than validating against the wrong
filesystem):

- host_id set  -> reject (the runner launches on a separate host).
- co-location NOT positively proven -> reject. The gate is the
  ``OMNIGENT_LOCAL_COLOCATED_RUNNER`` token, set ONLY by the auto-spawn
  loopback-server paths; a remote auth-disabled Docker server sets the
  single-user AUTH marker but NOT this token, so it is rejected (its
  realpath would resolve an unrelated filesystem).

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
    """FIX 1: a multipart workspace + host_id is refused, never persisted.

    Rejected even when co-located (token present) — host_id is a
    separate-host launch regardless.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_COLOCATED_RUNNER", "1")
    with pytest.raises(OmnigentError) as ei:
        sessions._create_session_from_bundle(
            _STORE,  # conversation_store — never reached (gate fires first)
            _ARTIFACTS,
            _meta(workspace="/work/repo", host_id="host_abc"),
            b"bundle-bytes",
        )
    assert ei.value.code == ErrorCode.INVALID_INPUT
    assert "host_id" in ei.value.message


def test_workspace_on_remote_docker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A remote auth-disabled Docker server sets the single-user AUTH marker
    but NOT the co-location token, so a workspace override is rejected
    rather than validated against the server's unrelated filesystem.
    """
    # Docker-style: auth marker on, co-location token absent.
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.delenv("OMNIGENT_LOCAL_COLOCATED_RUNNER", raising=False)
    with pytest.raises(OmnigentError) as ei:
        sessions._create_session_from_bundle(
            _STORE,
            _ARTIFACTS,
            _meta(workspace="/work/repo"),
            b"bundle-bytes",
        )
    assert ei.value.code == ErrorCode.INVALID_INPUT
    assert "co-located" in ei.value.message


def test_colocated_local_workspace_is_validated_and_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Positive: an auto-spawned co-located local session (co-location
    token present) with a valid workspace still passes — canonically
    validated and persisted as the canonical path (no regression for
    ``omnigent run --workspace`` locally). The single-user auth marker is
    intentionally NOT set, proving the gate keys off co-location, not
    auth posture.
    """
    monkeypatch.setenv("OMNIGENT_LOCAL_COLOCATED_RUNNER", "1")
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
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
