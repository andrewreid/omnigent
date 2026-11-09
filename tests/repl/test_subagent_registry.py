"""
Unit tests for the REPL's sub-agent tree plumbing in ``omnigent.repl._repl``.

Covers the two testable seams of the ``↓`` sub-agents feature:

* :func:`_apply_child_session_event` — maps ``session.created`` /
  ``session.child_session.updated`` SSE events onto the host registry,
  filtered by the active conversation id.
* :func:`_refresh_subagent_tree` — fetches the tree via the shared SDK
  recursion (``client.sessions.child_sessions_tree``) and seeds the host
  registry (the deeper-level poll).

The live triggers live in the ``_render_session_event`` closure inside
``run_repl`` (not callable in isolation), so a source-inspection guard
catches the wiring being dropped — mirroring
``tests/repl/test_agent_switch_refresh.py``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from omnigent_client import PaginatedList
from omnigent_client._sessions import SessionsNamespace
from omnigent_ui_sdk.terminal._host import TerminalHost

from omnigent.repl import _repl
from omnigent.repl._repl import _apply_child_session_event, _refresh_subagent_tree
from omnigent.server.schemas import (
    SessionChildSessionUpdatedEvent,
    SessionCreatedEvent,
)


def _host() -> TerminalHost:
    return TerminalHost(model_name="test")


def test_session_created_registers_launching_child() -> None:
    """``session.created`` for the active session registers a busy child."""
    host = _host()
    event = SessionCreatedEvent(
        type="session.created",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        agent_id="ag_reviewer",
        parent_session_id="conv_main",
    )
    handled = _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    assert handled is True
    assert host.has_active_subagents() is True
    tree = host.subagent_tree()
    assert [(n.session_id, n.parent_id) for n, _ in tree] == [("conv_c1", "conv_main")]


def test_child_updated_merges_summary_with_parent() -> None:
    """``session.child_session.updated`` upserts the child under the active
    session as parent, carrying the partial summary fields."""
    host = _host()
    event = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={
            "id": "conv_c1",
            "tool": "reviewer",
            "title": "reviewer:auth",
            "busy": True,
            "current_task_status": "in_progress",
        },
    )
    handled = _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    assert handled is True
    assert host.active_subagent_count() == 1
    _sid, label = host.subagent_menu_rows()[0]
    assert "reviewer:auth" in label
    assert "Working" in label


def test_child_event_for_other_parent_is_ignored() -> None:
    """An event whose carrier isn't the active session or a node in its tree
    is consumed (handled) but must NOT mutate the registry — it can't be
    placed, so it can't pollute the tree."""
    host = _host()
    event = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_OTHER",
        child_session_id="conv_c99",
        child={"id": "conv_c99", "busy": True, "current_task_status": "in_progress"},
    )
    handled = _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    assert handled is True  # still a child event -> caller stops dispatch
    assert host.has_active_subagents() is False
    assert host.subagent_tree() == []


def _grandchild_update(carrier: str, child_id: str) -> SessionChildSessionUpdatedEvent:
    return SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id=carrier,
        child_session_id=child_id,
        child={"id": child_id, "busy": True, "current_task_status": "in_progress"},
    )


def test_relayed_deeper_event_attaches_under_its_known_parent() -> None:
    """The server relays a deeper sub-agent's status edge to every ancestor's
    stream with its own parent as carrier; the REPL places it there without a
    fetch, down to the depth cap."""
    host = _host()
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_running("conv_c1"))

    for carrier, child_id in (("conv_c1", "conv_g1"), ("conv_g1", "conv_gg1")):
        handled = _apply_child_session_event(
            _grandchild_update(carrier, child_id),
            active_conversation_id="conv_main",
            host=host,
        )
        assert handled is True
    assert [(n.session_id, depth) for n, depth in host.subagent_tree()] == [
        ("conv_c1", 1),
        ("conv_g1", 2),
        ("conv_gg1", 3),
    ]

    # A carrier already at the depth cap would put its child past the tree the
    # REPL shows, so the event is consumed without a new row.
    _apply_child_session_event(
        _grandchild_update("conv_gg1", "conv_too_deep"),
        active_conversation_id="conv_main",
        host=host,
    )
    assert "conv_too_deep" not in {n.session_id for n, _ in host.subagent_tree()}


def test_depth_cap_counts_from_the_tree_root_while_inside_a_child() -> None:
    """Inside child A the stream is A's own, but the cap still counts from the
    tree root: a node the root's view would never show must not slip in and
    then stay stuck there."""
    host = _host()
    host.upsert_subagent("A", parent_id="R", child=_running("A"))
    host.upsert_subagent("B", parent_id="A", child=_running("B"))

    for carrier, child_id in (("B", "C"), ("C", "D")):
        _apply_child_session_event(
            _grandchild_update(carrier, child_id),
            active_conversation_id="A",
            host=host,
            tree_root_id="R",
        )

    assert [(n.session_id, depth) for n, depth in host.subagent_tree()] == [
        ("A", 1),
        ("B", 2),
        ("C", 3),
    ]


def test_relayed_spawn_lists_a_deeper_child_as_launching() -> None:
    """A grandchild's ``session.created`` reaches the root stream too, so it is
    listed under its parent before its first status edge."""
    host = _host()
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_running("conv_c1"))
    event = SessionCreatedEvent(
        type="session.created",
        conversation_id="conv_c1",
        child_session_id="conv_g1",
        agent_id="ag_reviewer",
        parent_session_id="conv_c1",
    )

    assert _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    node, depth = host.subagent_tree()[1]
    assert (node.session_id, node.parent_id, node.status, depth) == (
        "conv_g1",
        "conv_c1",
        "launching",
        2,
    )


def test_archived_child_leaves_the_tree_with_its_subtree() -> None:
    """Archiving pushes the child's row with the archive stamp; it leaves
    ``child_sessions``, so it and what was known below it leave the tree."""
    host = _host()
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_running("conv_c1"))
    host.upsert_subagent("conv_g1", parent_id="conv_c1", child=_running("conv_g1"))
    host.upsert_subagent("conv_c2", parent_id="conv_main", child=_running("conv_c2"))
    event = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={**_done("conv_c1"), "labels": {"omnigent.archived_at": "1762400000"}},
    )

    assert _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    assert [n.session_id for n, _ in host.subagent_tree()] == ["conv_c2"]

    # A late partial delta (e.g. the runner's final idle edge during the
    # archive's undo window) must not bring it back; a full row without the
    # stamp (an unarchive) does.
    late = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={"id": "conv_c1", "busy": False},
    )
    _apply_child_session_event(late, active_conversation_id="conv_main", host=host)
    assert [n.session_id for n, _ in host.subagent_tree()] == ["conv_c2"]
    unarchived = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={**_done("conv_c1"), "labels": {}},
    )
    resyncs: list[None] = []
    _apply_child_session_event(
        unarchived,
        active_conversation_id="conv_main",
        host=host,
        request_resync=lambda: resyncs.append(None),
    )
    assert "conv_c1" in {n.session_id for n, _ in host.subagent_tree()}
    assert len(resyncs) == 1, "the subtree dropped on archive must be refetched"


def test_a_snapshot_fetched_before_an_archive_is_dropped() -> None:
    host = _host()
    host.upsert_subagent("conv_c1", parent_id="conv_main", child=_running("conv_c1"))
    generation = host.subagent_generation
    host.archive_subagent("conv_c1")

    host.seed_subagent_tree(
        "conv_main",
        [{**_running("conv_c1"), "parent_id": "conv_main"}],
        generation=generation,
    )

    assert host.subagent_tree() == []
    assert host.is_subagent_archived("conv_c1")


def test_subagent_depth_follows_known_parents_to_the_root() -> None:
    host = _host()
    host.upsert_subagent("c1", parent_id="root")
    host.upsert_subagent("g1", parent_id="c1")
    host.upsert_subagent("orphan", parent_id="unknown")

    assert host.subagent_depth("root", "root") == 0
    assert host.subagent_depth("c1", "root") == 1
    assert host.subagent_depth("g1", "root") == 2
    assert host.subagent_depth("g1", "c1") == 1
    assert host.subagent_depth("orphan", "root") is None
    assert host.subagent_depth("missing", "root") is None


def test_non_child_event_is_not_handled() -> None:
    """A non-child event returns False and leaves the registry untouched, so
    inserting the hook before the generic translation can't swallow it."""
    host = _host()
    handled = _apply_child_session_event(object(), active_conversation_id="conv_main", host=host)
    assert handled is False
    assert host.has_active_subagents() is False


def test_child_terminal_status_settles_out_of_count() -> None:
    """A terminal update marks the node finished; the running count holds it
    through the linger window (debounce) and then drops it once settled."""
    host = _host()
    busy = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={
            "id": "conv_c1",
            "tool": "reviewer",
            "busy": True,
            "current_task_status": "in_progress",
        },
    )
    done = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={"id": "conv_c1", "busy": False, "current_task_status": "completed"},
    )
    _apply_child_session_event(busy, active_conversation_id="conv_main", host=host)
    assert host.active_subagent_count() == 1
    _apply_child_session_event(done, active_conversation_id="conv_main", host=host)
    # Debounced: still counted right after completing, until the linger lapses.
    assert host.active_subagent_count() == 1
    host._subagents["conv_c1"].done_at = host._monotonic() - 100.0
    assert host.active_subagent_count() == 0


class _FakeSessions:
    """Minimal stand-in for ``client.sessions`` exposing ``child_sessions``.

    The recursion + parent tagging now live in the SDK's
    :meth:`SessionsNamespace.child_sessions_tree`, so the fake reuses that real
    implementation (bound to this fake's ``child_sessions``) — the REPL→SDK
    path is exercised end-to-end and ``calls`` still records each level fetched.
    """

    def __init__(self, by_parent: dict[str, list[dict[str, Any]]]) -> None:
        self._by_parent = by_parent
        self.calls: list[str] = []

    async def child_sessions(
        self,
        session_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
        **_server_flags: object,  # e.g. descendants=True: answered like an older server
    ) -> PaginatedList:
        self.calls.append(session_id)
        return PaginatedList(data=list(self._by_parent.get(session_id, [])), has_more=False)

    async def child_sessions_tree(
        self, session_id: str, *, max_depth: int = 3, limit: int = 100, **kwargs: Any
    ) -> list[dict[str, Any]]:
        # Delegate to the real SDK recursion — it only depends on
        # ``self.child_sessions`` — so we test the actual shared helper.
        return await SessionsNamespace.child_sessions_tree(
            self,  # type: ignore[arg-type]
            session_id,
            max_depth=max_depth,
            limit=limit,
            **kwargs,
        )


class _FakeClient:
    def __init__(self, by_parent: dict[str, list[dict[str, Any]]]) -> None:
        self.sessions = _FakeSessions(by_parent)


@pytest.mark.asyncio
async def test_refresh_subagent_tree_recurses_to_grandchildren() -> None:
    """The recursive fetch assembles a 2-level tree with correct parents —
    the mechanism that keeps grandchildren live (the SSE stream only carries
    the active session's direct children)."""
    client = _FakeClient(
        {
            "conv_main": [
                {
                    "id": "conv_child",
                    "tool": "coder",
                    "busy": True,
                    "current_task_status": "in_progress",
                },
            ],
            "conv_child": [
                {
                    "id": "conv_grand",
                    "tool": "reviewer",
                    "busy": True,
                    "current_task_status": "in_progress",
                },
            ],
        }
    )
    host = _host()
    await _refresh_subagent_tree(client, host, "conv_main")  # type: ignore[arg-type]

    tree = host.subagent_tree()
    assert [(n.session_id, n.parent_id, depth) for n, depth in tree] == [
        ("conv_child", "conv_main", 1),
        ("conv_grand", "conv_child", 2),
    ]
    assert host.active_subagent_count() == 2
    # Both the root and the discovered child were queried (recursion).
    assert "conv_main" in client.sessions.calls
    assert "conv_child" in client.sessions.calls


@pytest.mark.asyncio
async def test_refresh_subagent_tree_respects_depth_cap() -> None:
    """Descendants past ``max_depth`` are neither fetched nor surfaced."""
    client = _FakeClient(
        {
            "conv_main": [{"id": "c1", "busy": True, "current_task_status": "in_progress"}],
            "c1": [{"id": "c2", "busy": True, "current_task_status": "in_progress"}],
            "c2": [{"id": "c3", "busy": True, "current_task_status": "in_progress"}],
        }
    )
    host = _host()
    await _refresh_subagent_tree(client, host, "conv_main", max_depth=1)  # type: ignore[arg-type]
    ids = {n.session_id for n, _ in host.subagent_tree()}
    assert ids == {"c1"}  # only depth-1 children fetched
    assert "c1" not in client.sessions.calls  # never recursed into c1


def test_run_repl_wires_subagent_plumbing() -> None:
    """Source-inspection guard: ``run_repl`` must wire the event hook, the
    inline-menu select callback (switching sessions), and the poll task.

    These triggers live in closures that can't be invoked in isolation, so a
    refactor could silently drop them while the helper unit tests above keep
    passing. Mirrors ``test_agent_switch_refresh.test_both_triggers_*``.
    """
    src = inspect.getsource(_repl.run_repl)
    assert "_apply_child_session_event(" in src, (
        "run_repl no longer applies child-session events — the state badge "
        "and ↓ menu will never populate."
    )
    assert "host.on_subagent_select = _open_subagent_by_id" in src, (
        "the inline ↓ menu select callback was dropped — Enter would no "
        "longer switch into the chosen sub-agent."
    )
    assert "host.active_session_id_getter" in src, (
        "the active-session getter is no longer wired — Left-arrow can't "
        "tell when the user is inside a sub-agent, so back-to-top breaks."
    )
    assert "session.view_session(" in src, (
        "the select callback must use view_session (read-only re-point), NOT "
        "switch_to_session — moving the runner binding into a sub-agent "
        "orphans the parent and the sub-agent's result is never delivered."
    )
    assert "interactive=interactive" in src and "is_subagent_chattable" in src, (
        "the select callback dropped interactive-child mode — Enter on a "
        "chattable child can no longer co-drive (chat with) it."
    )
    assert "_subagent_poll_loop" in src, (
        "the background tree poll (deeper levels) is no longer started"
    )
    assert src.count("subagent_poll_wake.set()") >= 2, (
        "user input must wake the tree fetch, or a backed-off poll lags a "
        "sub-agent that just resumed"
    )
    assert "_request_subagent_resync()" in src and "(resync and root_id is not None)" in src, (
        "an event the tree can't place must force a fetch even when nothing "
        "is active, or the new sub-agent never appears"
    )
    assert "subagent_pushes[0] != pushes_before" in src, (
        "a snapshot fetched while pushed events arrived may predate them and "
        "must be followed by a corrective fetch"
    )
    assert "carrier not in unplaced_carriers" in src, (
        "a carrier deeper than the tree shows would request a full walk on every event"
    )
    assert "session._on_stream_connected = _request_subagent_resync" in src, (
        "a stream reconnect no longer resyncs the tree — events dropped while "
        "disconnected would leave it stale until the safety-net fetch"
    )
    assert "_sync_subagent_root" in src and "_readonly_view" in src, (
        "the root-tracking dropped its read-only-view source of truth — the "
        "root could go stale vs the live main session, making '← back' show "
        "on the top-level session."
    )
    assert "_interactive_child" in src, (
        "the root-tracking no longer guards on _interactive_child — co-driving "
        "a child could re-root the selector onto it, breaking Left-arrow back."
    )
    assert "polled_root" in src and "has_active_subagents" in src, (
        "the discovery poll regressed — a resumed / switched session with "
        "existing children won't repopulate the selector without fresh SSE, "
        "or the poll no longer gates on active work and spins forever idle."
    )


# ── poll cost: settled subtrees, change detection, backoff ───────────────


def _settle(host: TerminalHost, *session_ids: str) -> None:
    """Age finished nodes past the linger window so they count as settled."""
    for sid in session_ids:
        host._subagents[sid].done_at = host._monotonic() - 100.0


def _done(sid: str) -> dict[str, Any]:
    return {"id": sid, "busy": False, "current_task_status": "completed"}


def _running(sid: str) -> dict[str, Any]:
    return {"id": sid, "busy": True, "current_task_status": "in_progress"}


@pytest.mark.asyncio
async def test_child_sessions_tree_skips_children_of_unexpanded_rows() -> None:
    sessions = _FakeSessions(
        {"root": [_done("a"), _done("b")], "a": [_done("a1")], "b": [_done("b1")]}
    )

    nodes = await sessions.child_sessions_tree("root", expand=lambda row: row["id"] != "a")

    assert [n["id"] for n in nodes] == ["a", "b", "b1"]
    assert "a" not in sessions.calls
    assert {"root", "b"} <= set(sessions.calls)


@pytest.mark.asyncio
async def test_refresh_skips_settled_subtrees_and_keeps_their_rows() -> None:
    client = _FakeClient({"root": [_done("a")], "a": [_done("a1")]})
    host = _host()
    await _refresh_subagent_tree(client, host, "root")  # type: ignore[arg-type]
    _settle(host, "a", "a1")
    client.sessions.calls.clear()

    await _refresh_subagent_tree(client, host, "root", expand_all=False)  # type: ignore[arg-type]

    assert client.sessions.calls == ["root"]
    assert {n.session_id for n, _ in host.subagent_tree()} == {"a", "a1"}


@pytest.mark.asyncio
async def test_refresh_reads_busy_unknown_and_forced_subtrees() -> None:
    client = _FakeClient(
        {
            "root": [_done("settled"), _done("forced"), _running("busy")],
            "settled": [_done("s1")],
            "forced": [_done("f1")],
            "busy": [_running("x1")],
        }
    )
    host = _host()
    await _refresh_subagent_tree(client, host, "root")  # type: ignore[arg-type]
    _settle(host, "settled", "s1", "forced", "f1")
    client.sessions._by_parent["root"].append(_done("new"))
    client.sessions._by_parent["new"] = [_done("n1")]
    client.sessions.calls.clear()

    await _refresh_subagent_tree(  # type: ignore[arg-type]
        client, host, "root", expand_all=False, force_expand={"forced"}
    )

    assert {"settled", "s1"}.isdisjoint(client.sessions.calls)
    assert {"root", "forced", "busy", "new"} <= set(client.sessions.calls)
    assert {n.session_id for n, _ in host.subagent_tree()} >= {"s1", "f1", "x1", "n1"}


@pytest.mark.asyncio
async def test_refresh_fingerprint_changes_only_when_rows_change() -> None:
    client = _FakeClient({"root": [_running("a")]})
    host = _host()

    first = await _refresh_subagent_tree(client, host, "root")  # type: ignore[arg-type]
    same = await _refresh_subagent_tree(client, host, "root")  # type: ignore[arg-type]
    client.sessions._by_parent["root"] = [_done("a")]
    changed = await _refresh_subagent_tree(client, host, "root")  # type: ignore[arg-type]

    assert first is not None and first == same
    assert changed != first


@pytest.mark.asyncio
async def test_refresh_reports_a_failed_fetch() -> None:
    class _Failing(_FakeSessions):
        async def child_sessions(self, session_id: str, **_: object) -> PaginatedList:
            raise RuntimeError("server down")

    client = _FakeClient({})
    client.sessions = _Failing({})

    assert await _refresh_subagent_tree(client, _host(), "root") is None  # type: ignore[arg-type]


def test_poll_delay_backs_off_until_a_change() -> None:
    delay = _repl._SUBAGENT_POLL_SECONDS
    delays = []
    for _ in range(5):
        delay = _repl._next_subagent_poll_delay(delay, changed=False)
        delays.append(delay)

    assert delays == [4.0, 8.0, 10.0, 10.0, 10.0]
    assert _repl._next_subagent_poll_delay(10.0, changed=True) == _repl._SUBAGENT_POLL_SECONDS


def test_tree_waits_for_the_safety_net_unless_inside_a_subagent() -> None:
    """At the root the stream pushes the tree, so only the safety net times
    out; inside a sub-agent the backoff poll keeps the rest of the tree fresh."""
    assert (
        _repl._subagent_wait_seconds(4.0, observing_subagent=False)
        == _repl._SUBAGENT_SAFETY_POLL_SECONDS
    )
    assert _repl._subagent_wait_seconds(4.0, observing_subagent=True) == 4.0


def test_subtree_settled_needs_a_known_node_with_nothing_running_below() -> None:
    host = _host()
    host.seed_subagent_tree(
        "root",
        [{**_done("a"), "parent_id": "root"}, {**_running("a1"), "parent_id": "a"}],
    )
    _settle(host, "a")

    assert not host.subagent_subtree_settled("unknown")
    assert not host.subagent_subtree_settled("a")
    host.upsert_subagent("a1", parent_id="a", child=_done("a1"))
    _settle(host, "a1")
    assert host.subagent_subtree_settled("a")
    assert "a" in host.subagent_ancestors("a1")
