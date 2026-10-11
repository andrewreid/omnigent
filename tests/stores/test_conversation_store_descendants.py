"""Tests for ``SqlAlchemyConversationStore.list_descendant_conversations``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from omnigent.errors import StaleCursorError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _child(store: SqlAlchemyConversationStore, parent_id: str, title: str) -> str:
    return store.create_conversation(
        kind="sub_agent", title=title, parent_conversation_id=parent_id
    ).id


def _rows(
    store: SqlAlchemyConversationStore, conversation_id: str, **kwargs: Any
) -> list[tuple[str, str | None, int]]:
    page = store.list_descendant_conversations(conversation_id, limit=1000, **kwargs)
    return [(d.conversation.id, d.conversation.parent_conversation_id, d.depth) for d in page.data]


@contextmanager
def _statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if not statement.startswith("PRAGMA"):
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _record)


@pytest.mark.parametrize("store_fixture", ["conversation_store", "split_db_conversation_store"])
def test_lists_only_the_subtree_breadth_first_with_depth(
    store_fixture: str, request: pytest.FixtureRequest
) -> None:
    """Depths are relative to the requested node; siblings and ancestors never appear."""
    store: SqlAlchemyConversationStore = request.getfixturevalue(store_fixture)
    root = store.create_conversation(kind="default", title="root").id
    a = _child(store, root, "r:a")
    b = _child(store, root, "r:b")
    a1 = _child(store, a, "r:a1")
    a1x = _child(store, a1, "r:a1x")
    _child(store, b, "r:b1")

    rows = _rows(store, root)
    assert {r for r in rows if r[2] == 1} == {(a, root, 1), (b, root, 1)}
    assert [r[2] for r in rows] == sorted(r[2] for r in rows)  # parents first
    assert (a1x, a1, 3) in rows and len(rows) == 5

    assert _rows(store, a) == [(a1, a, 1), (a1x, a1, 2)]
    assert _rows(store, a1x) == []
    assert _rows(store, "0" * 32) == []
    labelled = store.list_descendant_conversations(a).data[0].conversation
    assert labelled.title == "r:a1"


def test_walks_through_archived_nodes(conversation_store: SqlAlchemyConversationStore) -> None:
    """An archived intermediate is omitted by default but its live child is still found."""
    root = conversation_store.create_conversation(kind="default", title="root").id
    mid = _child(conversation_store, root, "r:mid")
    live = _child(conversation_store, mid, "r:live")
    conversation_store.update_conversation(mid, archived=True)

    assert _rows(conversation_store, root) == [(live, mid, 2)]
    assert _rows(conversation_store, root, include_archived=True) == [
        (mid, root, 1),
        (live, mid, 2),
    ]


def test_max_depth_bounds_the_walk(conversation_store: SqlAlchemyConversationStore) -> None:
    root = conversation_store.create_conversation(kind="default", title="root").id
    child = _child(conversation_store, root, "r:c")
    _child(conversation_store, child, "r:g")

    assert _rows(conversation_store, root, max_depth=1) == [(child, root, 1)]


def test_root_binding_returns_nothing_for_another_tree(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A node no longer in the named tree lists nothing rather than a foreign subtree."""
    root = conversation_store.create_conversation(kind="default", title="root").id
    other = conversation_store.create_conversation(kind="default", title="other").id
    node = _child(conversation_store, root, "r:node")
    _child(conversation_store, node, "r:leaf")

    assert len(_rows(conversation_store, node, root_conversation_id=root)) == 1
    assert _rows(conversation_store, node, root_conversation_id=other) == []


def test_keyset_pages_cover_ties_without_gaps_or_repeats(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Rows created in the same second still page exactly once each."""
    root = conversation_store.create_conversation(kind="default", title="root").id
    seeded = {_child(conversation_store, root, f"r:{i}") for i in range(7)}
    seeded |= {_child(conversation_store, next(iter(seeded)), f"g:{i}") for i in range(3)}

    seen: list[str] = []
    after: str | None = None
    while True:
        page = conversation_store.list_descendant_conversations(root, limit=3, after=after)
        seen.extend(d.conversation.id for d in page.data)
        if not page.has_more:
            break
        after = page.last_id
    assert len(seen) == len(seeded) == len(set(seen))
    assert set(seen) == seeded


def test_stale_cursor_raises(conversation_store: SqlAlchemyConversationStore) -> None:
    """A cursor outside the filtered subtree is an error, not an empty page."""
    root = conversation_store.create_conversation(kind="default", title="root").id
    _child(conversation_store, root, "r:1")
    _child(conversation_store, root, "r:2")
    outsider = conversation_store.create_conversation(kind="default", title="x").id
    # Same-second siblings tie-break on rowid or id depending on dialect, so
    # take the cursor from the store's own ordering.
    first = _rows(conversation_store, root)[0][0]
    conversation_store.update_conversation(first, archived=True)

    with pytest.raises(StaleCursorError):
        conversation_store.list_descendant_conversations(root, after=first)
    with pytest.raises(StaleCursorError):
        conversation_store.list_descendant_conversations(root, after=outsider)
    assert conversation_store.list_descendant_conversations(
        root, after=first, include_archived=True
    ).data


def test_query_count_is_independent_of_tree_size(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """One page costs a fixed handful of statements however large the tree is."""
    root = conversation_store.create_conversation(kind="default", title="root").id
    small = _child(conversation_store, root, "r:small")
    _child(conversation_store, small, "r:leaf")
    big = _child(conversation_store, root, "r:big")
    for i in range(150):
        _child(conversation_store, big, f"r:{i}")

    with _statements() as first_page:
        page = conversation_store.list_descendant_conversations(root, limit=2)
    with _statements() as next_page:
        conversation_store.list_descendant_conversations(root, limit=2, after=page.last_id)
    with _statements() as small_subtree:
        conversation_store.list_descendant_conversations(small)

    # Page ids + rows by key + labels + metadata; a cursor adds one position lookup.
    assert len(first_page) == 4
    assert len(next_page) == 5
    assert len(small_subtree) == 4


def test_corrupt_parent_cycle_neither_repeats_ids_nor_stalls_paging(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A parent cycle yields each id once, so cursor paging still terminates."""
    from sqlalchemy import update

    from omnigent.db.db_models import SqlConversation

    root = conversation_store.create_conversation(kind="default", title="root").id
    a = _child(conversation_store, root, "r:a")
    b = _child(conversation_store, a, "r:b")
    with conversation_store._conv_engine.begin() as conn:
        conn.execute(
            update(SqlConversation).where(SqlConversation.id == a).values(parent_conversation_id=b)
        )

    seen: list[str] = []
    after: str | None = None
    for _ in range(10):
        page = conversation_store.list_descendant_conversations(a, limit=1, after=after)
        seen.extend(d.conversation.id for d in page.data)
        if not page.has_more:
            break
        after = page.last_id
    else:
        pytest.fail("paging did not terminate")
    assert seen == [b]


def test_recursive_step_is_driven_by_the_parent_index(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """On SQLite the walk seeks children by parent and never scans conversations.

    Without stats, a constant workspace filter let SQLite drive the recursive
    step from the workspace prefix, scanning every session once per node.
    """
    if conversation_store._conv_engine.dialect.name != "sqlite":
        pytest.skip("query-plan shape is SQLite-specific")
    root = conversation_store.create_conversation(kind="default", title="root").id
    _child(conversation_store, root, "r:a")
    captured: list[tuple[str, Any]] = []

    def _record(_conn: Any, _cursor: Any, statement: str, params: Any, *_args: Any) -> None:
        if "RECURSIVE" in statement:
            captured.append((statement, params))

    engine = conversation_store._conv_engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        conversation_store.list_descendant_conversations(root)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    statement, params = captured[0]
    with engine.connect() as conn:
        plan = [row[-1] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params)]

    assert any(
        "idx_conversations_parent (workspace_id=? AND parent_conversation_id=?)" in step
        for step in plan
    ), plan
    assert not any(step.startswith("SCAN conversations") for step in plan), plan
