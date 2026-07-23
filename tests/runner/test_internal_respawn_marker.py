"""Cancellation marker driven by explicit cancel origin.

An interrupt / stop forwarded to an in-process child cancels the
in-flight turn and appends a synthetic cancellation marker. The marker
text is chosen by an explicit cancel ORIGIN tag, defaulting to
``"user"``:

* A genuine user Esc (untagged, or ``origin="user"``) always yields the
  ``[System: interrupted]`` user-abandonment marker, so the next turn
  treats the interrupted request as abandoned.
* Only a system-originated cancel (``origin="system"`` — e.g. a policy
  hook declining a change) yields the neutral "interrupted by the
  system" marker, so a resumed child does not read the restart as the
  user cancelling and drop its task.

The default is ``"user"`` so an untagged cancel can never rewrite a real
user stop into the neutral marker.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner.app import _session_histories_ref

from .test_app_sessions_native import _build_fwd_blocking_app, _build_interrupt_app, _runner_client

# Distinctive substrings of the two marker texts (module constants are
# closed over inside ``create_runner_app``, so match on wording).
_ABANDONMENT_SNIPPET = "abandoned their previous request"
_NEUTRAL_SNIPPET = "interrupted by the system"


def _marker_texts(histories: list[dict[str, Any]]) -> list[str]:
    """Return the text of every synthetic system-marker user message."""
    return [
        b.get("text") or ""
        for h in histories
        if h.get("type") == "message" and h.get("role") == "user"
        for b in h.get("content", [])
        if "[System:" in (b.get("text") or "")
    ]


async def _start_blocked_turn(client: Any, conv_id: str) -> None:
    """POST a user message and wait for the turn to block on the gate."""
    body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [{"type": "input_text", "text": "do something"}],
        "harness": "openai-agents",
    }
    resp = await client.post(f"/v1/sessions/{conv_id}/events", json=body)
    assert resp.status_code == 202
    assert "buffered" not in resp.text, (
        f"turn for {conv_id} was buffered behind a prior turn: {resp.text}"
    )
    await asyncio.sleep(0.1)


async def _cancel_and_finish(
    client: Any,
    conv_id: str,
    gate: asyncio.Event,
    body: dict[str, Any],
) -> None:
    """Forward a cancel event, then release the gate so the turn finalizes."""
    resp = await client.post(f"/v1/sessions/{conv_id}/events", json=body)
    assert resp.status_code in (200, 204)
    gate.set()
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_user_esc_uses_abandonment_marker() -> None:
    """A genuine user Esc (no origin) yields the abandonment marker."""
    gate = asyncio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_user_esc"
        await _start_blocked_turn(client, conv_id)
        await _cancel_and_finish(client, conv_id, gate, {"type": "interrupt"})

    markers = _marker_texts(_session_histories_ref.get(conv_id, []))
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"
    assert _ABANDONMENT_SNIPPET in markers[0], (
        f"a genuine user interrupt must keep the abandonment marker; got: {markers[0]!r}"
    )
    assert _NEUTRAL_SNIPPET not in markers[0]


@pytest.mark.asyncio
async def test_system_origin_uses_neutral_marker() -> None:
    """An ``origin="system"`` interrupt yields the neutral marker."""
    gate = asyncio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_system_origin"
        await _start_blocked_turn(client, conv_id)
        await _cancel_and_finish(client, conv_id, gate, {"type": "interrupt", "origin": "system"})

    markers = _marker_texts(_session_histories_ref.get(conv_id, []))
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"
    assert _NEUTRAL_SNIPPET in markers[0], (
        "a system-originated cancel must inject the neutral marker, not the "
        f"user-abandonment one; got: {markers[0]!r}"
    )
    assert _ABANDONMENT_SNIPPET not in markers[0]


@pytest.mark.asyncio
async def test_user_origin_survives_later_system_interrupt() -> None:
    """A system cancel cannot rewrite a pending user stop."""
    gate = asyncio.Event()
    fwd_gate = asyncio.Event()
    app, _pm, _hc = _build_fwd_blocking_app(gate, fwd_gate)

    async with _runner_client(app) as client:
        conv_id = "conv_user_then_system"
        await _start_blocked_turn(client, conv_id)

        user_task = asyncio.create_task(
            client.post(f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"})
        )
        await asyncio.sleep(0.1)

        system_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt", "origin": "system"},
            )
        )
        await asyncio.sleep(0.1)

        fwd_gate.set()
        user_resp, system_resp = await asyncio.gather(user_task, system_task)
        assert user_resp.status_code == 204, user_resp.text
        assert system_resp.status_code == 204, system_resp.text
        gate.set()
        await asyncio.sleep(0.2)

    markers = _marker_texts(_session_histories_ref.get(conv_id, []))
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"
    assert _ABANDONMENT_SNIPPET in markers[0], (
        f"a later system interrupt must not rewrite a user stop; got: {markers[0]!r}"
    )
    assert _NEUTRAL_SNIPPET not in markers[0]


@pytest.mark.asyncio
async def test_untagged_interrupt_defaults_to_abandonment() -> None:
    """An untagged interrupt defaults to the abandonment marker (safe default).

    The origin defaults to ``"user"`` so a cancel that omits the tag can
    never be misread as a system restart and rewrite a real user stop.
    """
    gate = asyncio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_untagged"
        await _start_blocked_turn(client, conv_id)
        await _cancel_and_finish(client, conv_id, gate, {"type": "interrupt"})

    markers = _marker_texts(_session_histories_ref.get(conv_id, []))
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"
    assert _ABANDONMENT_SNIPPET in markers[0], (
        f"an untagged cancel must default to the abandonment marker; got: {markers[0]!r}"
    )
    assert _NEUTRAL_SNIPPET not in markers[0]


@pytest.mark.asyncio
async def test_stop_session_untagged_uses_abandonment_marker() -> None:
    """A ``stop_session`` (sidebar Stop, untagged) yields the abandonment marker."""
    gate = asyncio.Event()
    app, _pm, _hc = _build_interrupt_app(gate)

    async with _runner_client(app) as client:
        conv_id = "conv_stop_session"
        await _start_blocked_turn(client, conv_id)
        await _cancel_and_finish(client, conv_id, gate, {"type": "stop_session"})

    markers = _marker_texts(_session_histories_ref.get(conv_id, []))
    assert len(markers) == 1, f"expected exactly one marker, got {markers}"
    assert _ABANDONMENT_SNIPPET in markers[0], (
        f"a user stop_session must keep the abandonment marker; got: {markers[0]!r}"
    )
    assert _NEUTRAL_SNIPPET not in markers[0]
