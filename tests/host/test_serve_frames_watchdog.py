"""Tests for the host tunnel inbound-idle watchdog."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

import omnigent.host.connect as connect_mod
from omnigent.host.connect import HostProcess
from omnigent.host.frames import HostHelloFrame, decode_host_frame
from omnigent.host.identity import HostIdentity
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)

pytestmark = pytest.mark.asyncio


class _WatchdogTunnel:
    """Fake websocket whose receive side can deliver frames or blackhole."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._incoming: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return await self._incoming.get()

    def feed(self, raw: str) -> None:
        self._incoming.put_nowait(raw)


@pytest.fixture(autouse=True)
def _fast_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        connect_mod,
        "configured_harness_map",
        lambda: {"codex-native": False},
    )
    monkeypatch.setattr(connect_mod, "HOST_TUNNEL_RECV_POLL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(connect_mod, "HOST_TUNNEL_INBOUND_IDLE_TIMEOUT_S", 0.03)
    monkeypatch.setattr(connect_mod, "TUNNEL_KEEPALIVE_PING_INTERVAL_S", 0.01)


def _make_host_process() -> HostProcess:
    return HostProcess(
        identity=HostIdentity(host_id="host_watchdog", name="watchdog-host"),
        server_url="http://localhost:8000",
    )


async def _started_serve(
    host: HostProcess,
    tunnel: _WatchdogTunnel,
) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(host._serve_frames(tunnel))  # type: ignore[arg-type]
    while not tunnel.sent:
        await asyncio.sleep(0)
    hello = decode_host_frame(tunnel.sent[0])
    assert isinstance(hello, HostHelloFrame)
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _wait_for_sent_count(tunnel: _WatchdogTunnel, count: int) -> None:
    while len(tunnel.sent) < count:
        await asyncio.sleep(0)


async def test_watchdog_arms_on_server_ping_then_raises_after_silence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = _make_host_process()
    tunnel = _WatchdogTunnel()
    caplog.set_level(logging.WARNING, logger="omnigent.host.connect")

    async for task in _started_serve(host, tunnel):
        tunnel.feed(encode_frame(PingFrame(ts=123)))
        with pytest.raises(ConnectionError, match="inbound idle timeout"):
            await asyncio.wait_for(task, timeout=0.5)

    messages = [record.getMessage() for record in caplog.records]
    fires = [msg for msg in messages if "inbound idle watchdog fired" in msg]
    assert len(fires) == 1
    assert "idle " in fires[0]
    assert "missed server pings" in fires[0]


async def test_watchdog_never_arms_without_server_ping() -> None:
    host = _make_host_process()
    tunnel = _WatchdogTunnel()

    async for task in _started_serve(host, tunnel):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.08)
        assert not task.done()


async def test_server_ping_resets_watchdog_timer() -> None:
    host = _make_host_process()
    tunnel = _WatchdogTunnel()

    async for task in _started_serve(host, tunnel):
        tunnel.feed(encode_frame(PingFrame(ts=1)))
        await _wait_for_sent_count(tunnel, 2)
        await asyncio.sleep(0.02)
        assert not task.done()

        tunnel.feed(encode_frame(PingFrame(ts=2)))
        await _wait_for_sent_count(tunnel, 3)
        await asyncio.sleep(0.02)
        assert not task.done()


async def test_server_ping_gets_pong_reply() -> None:
    host = _make_host_process()
    tunnel = _WatchdogTunnel()

    async for _task in _started_serve(host, tunnel):
        tunnel.feed(encode_frame(PingFrame(ts=456)))
        await _wait_for_sent_count(tunnel, 2)

    pong = decode_frame(tunnel.sent[1])
    assert isinstance(pong, PongFrame)
    assert pong.ts == 456


async def test_half_open_app_ping_blackhole_reconnects_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-open-style app-frame blackhole tears down and reconnects."""
    monkeypatch.setattr(connect_mod, "_RECONNECT_BASE_S", 0.01)

    reconnected = asyncio.Event()

    class _HalfOpenHost(HostProcess):
        attempts = 0

        async def _connect_and_serve(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                tunnel = _WatchdogTunnel()
                tunnel.feed(encode_frame(PingFrame(ts=1)))
                await self._serve_frames(tunnel)  # type: ignore[arg-type]
                return
            reconnected.set()
            raise asyncio.CancelledError

    host = _HalfOpenHost(
        identity=HostIdentity(host_id="host_half_open", name="half-open-host"),
        server_url="http://localhost:8000",
    )

    await asyncio.wait_for(host.run(), timeout=1.0)

    assert reconnected.is_set()
    assert host.attempts == 2
