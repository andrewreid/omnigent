"""The runner credential route vends only while the runner's managed host is live.

Exercises the production owner resolver wired by ``create_app`` against real
SQLite stores outside accounts mode, where ownership comes from the session
bound to the runner. Host deletion clears the session's ``host_id`` but not its
``runner_id``, so a binding token left over after teardown must stop working.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_OWNER = "alice@example.com"
_BINDING = "managed-runner-binding-token"
_LEVEL_OWNER = 4


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.server.routes.host_credentials as hc

    async def resolver(user_id: str, *, store, client):
        return {"token": f"access-for-{user_id}"}

    monkeypatch.setattr(hc, "_broker_resolvers", lambda: {"chatgpt": resolver})


def _app(db_uri: str, tmp_path: Path, hosts: HostStore, convs: SqlAlchemyConversationStore):
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=convs,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        host_store=hosts,
    )
    app.state.chatgpt_store = object()
    return app


def _bind_managed_runner(
    db_uri: str, hosts: HostStore, convs, *, expires_in: int = 3600
) -> tuple[str, str]:
    host = hosts.register_managed_host(
        host_id=uuid.uuid4().hex,
        name="managed-1",
        user_id=_OWNER,
        token="launch-token",
        provider="kubernetes",
        sandbox_id="sbx-1",
        token_expires_at=int(time.time()) + expires_in,
    )
    runner_id = token_bound_runner_id(_BINDING)
    conv = convs.create_conversation(host_id=host.host_id, runner_id=runner_id, workspace="/work")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(_OWNER)
    perms.grant(_OWNER, conv.id, _LEVEL_OWNER)
    return runner_id, host.host_id


async def _vend(app, runner_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            f"/v1/runners/{runner_id}/credentials/chatgpt",
            headers={RUNNER_TUNNEL_TOKEN_HEADER: _BINDING},
        )


async def test_vends_while_the_managed_host_is_live_and_stops_after_deletion(
    db_uri: str, tmp_path: Path, broker: None
) -> None:
    hosts = HostStore(db_uri)
    convs = SqlAlchemyConversationStore(db_uri)
    app = _app(db_uri, tmp_path, hosts, convs)
    runner_id, host_id = _bind_managed_runner(db_uri, hosts, convs)

    live = await _vend(app, runner_id)
    assert live.status_code == 200, live.text
    assert live.json()["token"] == f"access-for-{_OWNER}"

    hosts.delete_host(host_id)

    gone = await _vend(app, runner_id)
    assert gone.status_code == 401


async def test_stops_once_the_launch_credential_is_revoked(
    db_uri: str, tmp_path: Path, broker: None
) -> None:
    # A failed relaunch revokes the launch credential but keeps the host row
    # and its session binding.
    hosts = HostStore(db_uri)
    convs = SqlAlchemyConversationStore(db_uri)
    app = _app(db_uri, tmp_path, hosts, convs)
    runner_id, host_id = _bind_managed_runner(db_uri, hosts, convs)
    assert (await _vend(app, runner_id)).status_code == 200

    hosts.revoke_launch_token(host_id)

    assert (await _vend(app, runner_id)).status_code == 401


async def test_expired_launch_credential_gets_nothing(
    db_uri: str, tmp_path: Path, broker: None
) -> None:
    hosts = HostStore(db_uri)
    convs = SqlAlchemyConversationStore(db_uri)
    app = _app(db_uri, tmp_path, hosts, convs)
    runner_id, _ = _bind_managed_runner(db_uri, hosts, convs, expires_in=-60)

    assert (await _vend(app, runner_id)).status_code == 401


async def test_runner_on_an_unmanaged_host_gets_nothing(
    db_uri: str, tmp_path: Path, broker: None
) -> None:
    hosts = HostStore(db_uri)
    convs = SqlAlchemyConversationStore(db_uri)
    app = _app(db_uri, tmp_path, hosts, convs)
    laptop = uuid.uuid4().hex
    hosts.upsert_on_connect(laptop, "laptop", _OWNER)
    runner_id = token_bound_runner_id(_BINDING)
    conv = convs.create_conversation(host_id=laptop, runner_id=runner_id, workspace="/work")
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(_OWNER)
    perms.grant(_OWNER, conv.id, _LEVEL_OWNER)

    assert (await _vend(app, runner_id)).status_code == 401
