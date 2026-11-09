"""The snapshot backstop that ends a turn whose terminal stream event was missed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from omnigent_client import OmnigentClient

from omnigent.repl import _repl as repl_mod


def _adapter(get: AsyncMock) -> repl_mod._SessionsChatReplAdapter:
    client = Mock(spec=OmnigentClient)
    client.sessions = SimpleNamespace(get=get)
    adapter = repl_mod._SessionsChatReplAdapter(client=client, agent_name="test-agent")
    adapter._turn_done = asyncio.Event()  # send() creates it per turn
    return adapter


async def test_missed_terminal_event_is_caught_with_a_growing_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream never reports the end, so the snapshot does, checked less and less often."""
    timeouts: list[float] = []

    async def no_event(awaitable: Awaitable[object], timeout: float) -> object:
        timeouts.append(timeout)
        awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(repl_mod.asyncio, "wait_for", no_event)
    statuses = ["running", "running", "running", "running", "idle"]
    get = AsyncMock(side_effect=[SimpleNamespace(status=s) for s in statuses])
    adapter = _adapter(get)

    await adapter._wait_for_turn_done("conv_a")

    assert adapter._turn_done.is_set()
    assert timeouts == [1.0, 2.0, 4.0, 5.0, 5.0]
    # Each check reads the snapshot without items, liveness or usage.
    for call in get.await_args_list:
        assert call.args == ("conv_a",)
        assert call.kwargs == {
            "include_items": False,
            "include_liveness": False,
            "include_usage": False,
        }


async def test_a_failed_turn_also_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_event(awaitable: Awaitable[object], timeout: float) -> object:
        awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(repl_mod.asyncio, "wait_for", no_event)
    get = AsyncMock(return_value=SimpleNamespace(status="failed"))
    adapter = _adapter(get)

    await adapter._wait_for_turn_done("conv_a")

    assert get.await_count == 1


async def test_the_stream_event_ends_the_wait_without_a_snapshot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repl_mod, "_TURN_BACKSTOP_FIRST_SECONDS", 30.0)
    get = AsyncMock()
    adapter = _adapter(get)

    waiter = asyncio.create_task(adapter._wait_for_turn_done("conv_a"))
    await asyncio.sleep(0)
    adapter._turn_done.set()
    await asyncio.wait_for(waiter, timeout=5)

    get.assert_not_awaited()


async def test_cancelling_the_wait_leaves_no_task_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repl_mod, "_TURN_BACKSTOP_FIRST_SECONDS", 30.0)
    adapter = _adapter(AsyncMock())
    before = len(asyncio.all_tasks())

    waiter = asyncio.create_task(adapter._wait_for_turn_done("conv_a"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert len(asyncio.all_tasks()) == before


async def test_a_live_stream_checks_the_snapshot_only_as_a_safety_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the stream delivers the turn's events, a missed end is a dropped
    event, not a slow subscription, so the snapshot is read rarely."""
    timeouts: list[float] = []

    async def no_event(awaitable: Awaitable[object], timeout: float) -> object:
        timeouts.append(timeout)
        awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(repl_mod.asyncio, "wait_for", no_event)
    get = AsyncMock(side_effect=[SimpleNamespace(status=s) for s in ("running", "idle")])
    adapter = _adapter(get)
    adapter._turn_stream_live = True

    await adapter._wait_for_turn_done("conv_a")

    assert timeouts == [repl_mod._TURN_BACKSTOP_LIVE_SECONDS] * 2
    assert get.await_count == 2


async def test_a_stream_reconnect_checks_the_snapshot_at_once() -> None:
    """Events published while the stream was down are lost, so a reconnect
    reads the snapshot right away instead of waiting for the safety net."""
    get = AsyncMock(return_value=SimpleNamespace(status="idle"))
    adapter = _adapter(get)
    adapter._turn_stream_live = True

    waiter = asyncio.create_task(adapter._wait_for_turn_done("conv_a"))
    await asyncio.sleep(0)
    get.assert_not_awaited()
    adapter._note_stream_connected(reconnected=True)
    await asyncio.wait_for(waiter, timeout=5)

    assert adapter._turn_done.is_set()
    get.assert_awaited_once()
    assert not adapter._stream_resync.is_set()


async def test_the_pump_reports_each_subscription_and_flags_reconnects() -> None:
    """Every subscription's first event resyncs the REPL; only a reconnect
    also asks an in-flight turn to re-check, and a drop marks the stream not
    live until events flow again."""
    subscriptions: list[asyncio.Event] = []

    async def stream(_session_id: str) -> AsyncIterator[object]:
        dropped = asyncio.Event()
        subscriptions.append(dropped)
        yield SimpleNamespace(type="session.presence")
        yield SimpleNamespace(type="session.presence")
        if len(subscriptions) == 1:
            raise ConnectionError("dropped")
        await asyncio.Event().wait()

    client = Mock(spec=OmnigentClient)
    client.sessions = SimpleNamespace(stream=stream)
    adapter = repl_mod._SessionsChatReplAdapter(client=client, agent_name="test-agent")
    adapter._session_id = "conv_a"
    resync_at_connect: list[bool] = []
    adapter._on_stream_connected = lambda: resync_at_connect.append(
        adapter._stream_resync.is_set()
    )

    pump = asyncio.create_task(adapter._stream_pump())
    try:
        while len(resync_at_connect) < 2:
            await asyncio.sleep(0.05)
    finally:
        pump.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pump

    assert len(subscriptions) == 2
    assert resync_at_connect == [False, True]
    assert adapter._turn_stream_live


async def test_a_stream_drop_wakes_the_turn_wait_into_frequent_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drop during the long live-stream wait re-reads the snapshot at once,
    then checks at the short interval until the stream is back."""
    monkeypatch.setattr(repl_mod, "_TURN_BACKSTOP_FIRST_SECONDS", 0.05)
    statuses = iter(["running", "idle"])
    get = AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(status=next(statuses)))
    adapter = _adapter(get)
    adapter._turn_stream_live = True

    waiter = asyncio.create_task(adapter._wait_for_turn_done("conv_a"))
    await asyncio.sleep(0)
    adapter._note_stream_dropped()
    await asyncio.wait_for(waiter, timeout=5)

    assert get.await_count == 2
