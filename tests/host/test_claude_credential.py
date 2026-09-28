"""Tests for exporting the brokered Claude subscription token on a sandbox host."""

from __future__ import annotations

import os

import pytest

from omnigent.host import claude_credential
from omnigent.host.claude_credential import CLAUDE_TOKEN_ENV_VAR, configure_host_claude
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.delenv(CLAUDE_TOKEN_ENV_VAR, raising=False)
    return monkeypatch


def _broker(monkeypatch: pytest.MonkeyPatch, payload: dict | None) -> list[tuple]:
    calls: list[tuple] = []

    def fake_fetch(server_url: str, host_id: str, host_token: str) -> dict | None:
        calls.append((server_url, host_id, host_token))
        return payload

    monkeypatch.setattr(claude_credential, "_fetch", fake_fetch)
    return calls


def test_exports_the_brokered_token(sandbox: pytest.MonkeyPatch) -> None:
    calls = _broker(sandbox, {"connected": True, "owner": "a@x", "token": "sk-ant-oat01-t"})
    assert configure_host_claude("https://srv", "host1") is True
    assert calls == [("https://srv", "host1", "launch-tok")]
    assert os.environ[CLAUDE_TOKEN_ENV_VAR] == "sk-ant-oat01-t"


def test_connected_owner_overrides_a_deployment_wide_token(sandbox: pytest.MonkeyPatch) -> None:
    sandbox.setenv(CLAUDE_TOKEN_ENV_VAR, "sk-ant-oat01-fleet")
    _broker(sandbox, {"connected": True, "token": "sk-ant-oat01-owner"})
    assert configure_host_claude("https://srv", "host1") is True
    assert os.environ[CLAUDE_TOKEN_ENV_VAR] == "sk-ant-oat01-owner"


@pytest.mark.parametrize("payload", [None, {"connected": False}, {"connected": True}])
def test_not_connected_leaves_ambient_token_alone(
    sandbox: pytest.MonkeyPatch, payload: dict | None
) -> None:
    sandbox.setenv(CLAUDE_TOKEN_ENV_VAR, "sk-ant-oat01-fleet")
    _broker(sandbox, payload)
    assert configure_host_claude("https://srv", "host1") is False
    assert os.environ[CLAUDE_TOKEN_ENV_VAR] == "sk-ant-oat01-fleet"


def test_noop_outside_a_sandbox(sandbox: pytest.MonkeyPatch) -> None:
    sandbox.delenv("IS_SANDBOX")
    calls = _broker(sandbox, {"connected": True, "token": "sk-ant-oat01-t"})
    assert configure_host_claude("https://srv", "host1") is False
    assert calls == []


def test_noop_without_a_launch_token(sandbox: pytest.MonkeyPatch) -> None:
    sandbox.delenv(HOST_TOKEN_ENV_VAR)
    calls = _broker(sandbox, {"connected": True, "token": "sk-ant-oat01-t"})
    assert configure_host_claude("https://srv", "host1") is False
    assert calls == []
