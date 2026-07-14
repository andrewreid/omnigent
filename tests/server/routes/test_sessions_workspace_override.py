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
from starlette.requests import Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import sessions
from omnigent.server.schemas import (
    CreatedSessionResponse,
    SessionCreateMetadata,
    SessionCreateRequest,
)
from omnigent.stores import ConversationStore
from omnigent.stores.artifact_store import ArtifactStore


class _FakeArtifactStore:
    def put(self, location: str, data: bytes) -> None:
        return None


# Reject tests never touch the stores (the deny-by-default gate fires
# first); accept test routes writes through monkeypatched helpers.
_STORE = cast(ConversationStore, object())
_ARTIFACTS = cast(ArtifactStore, _FakeArtifactStore())
# The classifier only passes `request` through to a monkeypatched host
# validator (or never touches it), so a stub is sufficient.
_REQUEST = cast(Request, object())


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


def test_workspace_on_non_colocated_server_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    B2: on a non-co-located server (remote / Docker: co-location token
    absent) the multipart workspace is the implicit launch cwd that the
    CLI always uploads; it must PASS THROUGH to the existing host-runner
    path unchanged, NOT be rejected or server-validated (wrong fs). An
    explicit --workspace against a remote server is blocked earlier at
    the CLI, so it never reaches here.
    """
    # Docker-style: auth marker on, co-location token absent.
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
    monkeypatch.delenv("OMNIGENT_LOCAL_COLOCATED_RUNNER", raising=False)
    monkeypatch.setattr(
        sessions,
        "validate_agent_bundle",
        lambda *a, **k: SimpleNamespace(
            name="a", description=None, os_env=SimpleNamespace(cwd=".")
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
        return CreatedSessionResponse(session_id="c", agent_id="a", agent_name="a")

    monkeypatch.setattr(sessions, "_persist_stored_session_bundle", _fake_persist)

    # A path that does NOT exist on this server — proves no server-side
    # validation ran (it would have rejected a missing dir).
    remote_only = "/runner/only/path/not/on/server"
    sessions._create_session_from_bundle(
        _STORE, _ARTIFACTS, _meta(workspace=remote_only), b"bundle-bytes"
    )
    assert captured["workspace"] == remote_only  # stored verbatim, unvalidated


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


# ── JSON-path topology classification (_classify_and_resolve_create_workspace) ──


def _req(**kw: Any) -> SessionCreateRequest:
    kw.setdefault("agent_id", "ag_1")
    return SessionCreateRequest(**kw)


async def _boom_no_host(**kw: Any) -> str:  # pragma: no cover - must not be called
    raise AssertionError("no-host validation must not run for this topology")


async def _boom_host(**kw: Any) -> str:  # pragma: no cover - must not be called
    raise AssertionError("host validation must not run for this topology")


@pytest.mark.asyncio
async def test_classify_workspace_none_returns_none() -> None:
    assert (
        await sessions._classify_and_resolve_create_workspace(
            body=_req(), agent=object(), agent_cache=None, user_id=None, request=_REQUEST
        )
        is None
    )


@pytest.mark.asyncio
async def test_classify_managed_repo_url_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: a managed host_type + repo-URL workspace is returned untouched.

    No no-host validation runs (it would reject the non-'/' URL); the URL
    flows to the managed clone path.
    """
    monkeypatch.setattr(sessions, "_validate_session_workspace_no_host", _boom_no_host)
    url = "https://github.com/org/repo#main"
    out = await sessions._classify_and_resolve_create_workspace(
        body=_req(host_type="managed", workspace=url),
        agent=object(),
        agent_cache=None,
        user_id=None,
        request=_REQUEST,
    )
    assert out == url


@pytest.mark.asyncio
async def test_classify_external_explicit_not_colocated_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3: external + no host_id + explicit override on a non-co-located
    (e.g. remote-runner child) server is rejected, not validated on the
    wrong filesystem."""
    monkeypatch.delenv("OMNIGENT_LOCAL_COLOCATED_RUNNER", raising=False)
    monkeypatch.setattr(sessions, "_validate_session_workspace_no_host", _boom_no_host)
    with pytest.raises(OmnigentError) as ei:
        await sessions._classify_and_resolve_create_workspace(
            body=_req(workspace="/some/path"),
            agent=object(),
            agent_cache=None,
            user_id=None,
            request=_REQUEST,
        )
    assert ei.value.code == ErrorCode.INVALID_INPUT
    assert "co-located" in ei.value.message


@pytest.mark.asyncio
async def test_classify_external_explicit_colocated_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External + no host_id + explicit + co-located -> canonical no-host
    validation (Patricia / polly local path)."""
    monkeypatch.setenv("OMNIGENT_LOCAL_COLOCATED_RUNNER", "1")

    async def _fake_no_host(*, workspace: str, agent: Any, agent_cache: Any) -> str:
        return "/canonical/repo"

    monkeypatch.setattr(sessions, "_validate_session_workspace_no_host", _fake_no_host)
    monkeypatch.setattr(sessions, "_validate_session_workspace", _boom_host)
    out = await sessions._classify_and_resolve_create_workspace(
        body=_req(workspace="/some/repo"),
        agent=object(),
        agent_cache=None,
        user_id=None,
        request=_REQUEST,
    )
    assert out == "/canonical/repo"


@pytest.mark.asyncio
async def test_classify_host_id_uses_host_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """host_id set -> host-side validation (not the server-fs no-host path)."""
    monkeypatch.setattr(sessions, "_validate_session_workspace_no_host", _boom_no_host)

    async def _fake_host(**kw: Any) -> str:
        return "/host/canonical"

    monkeypatch.setattr(sessions, "_validate_session_workspace", _fake_host)
    out = await sessions._classify_and_resolve_create_workspace(
        body=_req(host_id="host_x", workspace="/on/host"),
        agent=object(),
        agent_cache=None,
        user_id="u",
        request=_REQUEST,
    )
    assert out == "/host/canonical"
