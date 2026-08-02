"""
CI query-count regression tests for :func:`build_policy_engine` (C1).

C1 collapses several independent per-call fetches (root-conversation-id
lookup, label reload, own session_state, model override, a second
paginated spawn-tree walk, and two separate ``list_for_session`` calls)
into one conversation snapshot plus one tree load plus one batched policy
query. These tests pin the resulting non-``PRAGMA`` cursor-execute count
*exactly* per scenario so a reintroduced redundant fetch fails loudly
instead of silently regressing back toward the pre-C1 query count.

Lives under ``tests/stores`` (not ``tests/runtime/policies``) so the
Postgres/MySQL CI jobs — which scope to ``tests/stores tests/db`` — run it
too; the ``db_uri`` fixture already parametrizes over SQLite (default) and
Postgres/MySQL (``OMNIGENT_TEST_DB_URI``).

The absolute numbers below are a moving target: they count *every*
statement the build path issues, including ones this change does not own.
They were re-pinned upward once already when unrelated work added a second
policies query and a per-``get_conversation`` conversation-metadata read.
That shift is baseline drift, not a regression here — measured on the same
scenario, this change still takes the build from 79 statements / 23 pool
checkouts down to 20 / 6. When a future upstream change moves these numbers
again, re-pin them, but check the *deltas between scenarios* still hold:
the supplied-snapshot path must stay cheaper than ``conversation=None`` by
exactly one ``get_conversation``, and adding a second usage view must cost
nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.policies.builder import (
    _DEFAULT_POLICY_SPECS_CACHE,
    _SESSION_OWNER_CACHE,
    _SESSION_POLICY_SPECS_CACHE,
    build_policy_engine,
)
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    LabelDef,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

# A real subagent_cost_budget spec, used to flip on the subtree-usage
# seed (the branch that used to run a second independent tree walk).
_SUBAGENT_COST_POLICY = FunctionPolicySpec(
    name="subagent_budget",
    on=None,
    function=FunctionRef(
        path="omnigent.policies.builtins.cost.subagent_cost_budget",
        arguments={"max_cost_usd": 1000.0},
    ),
)


def _clear_builder_caches() -> None:
    """Drop every process-global memo ``build_policy_engine`` reads through."""
    _SESSION_POLICY_SPECS_CACHE.clear()
    _DEFAULT_POLICY_SPECS_CACHE.clear()
    _SESSION_OWNER_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_builder_caches() -> Iterator[None]:
    """
    Clear ``build_policy_engine``'s process-global memos around every test.

    The builder memoizes session policy specs, default policy specs, and
    session owners. ``_DEFAULT_POLICY_SPECS_CACHE`` in particular is keyed
    only on the default-policy set, so it stays warm across tests and can
    satisfy a later build without a query — making an exact count depend on
    which tests ran first in the same process. Clearing on both sides keeps
    the counts below a function of the scenario under test alone.
    """
    _clear_builder_caches()
    yield
    _clear_builder_caches()


def _make_spec(name: str = "query-count-agent") -> AgentSpec:
    return AgentSpec(spec_version=1, name=name)


@contextmanager
def _count_statements(db_uri: str) -> Iterator[list[str]]:
    """
    Count non-``PRAGMA`` cursor-execute statements issued inside the block.

    Installed immediately before the measured call and removed in a
    ``finally`` right after, so fixture setup (creating conversations,
    seeding policies) never pollutes the count. SQLite's managed-session
    ``PRAGMA`` pair (``db/utils.py``) is filtered by statement prefix;
    ``pool_pre_ping`` never fires ``before_cursor_execute`` (it pings via
    ``Dialect.do_ping`` on checkout, not a cursor execution), so it needs
    no special-casing here.

    :param db_uri: The store URI whose underlying (cached) engine to
        attach the listener to.
    :yields: The list of captured non-PRAGMA statement strings, appended
        to live as the block runs.
    """
    from sqlalchemy import event

    engine = get_or_create_engine(db_uri)
    statements: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.strip().upper().startswith("PRAGMA"):
            return
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


def test_supplied_snapshot_active_root_one_page_tree(db_uri: str) -> None:
    """
    Root session, snapshot supplied, single-page tree → the 5-statement
    baseline (2 policy queries + 1 tree-row query + 1 bulk-label query +
    1 conversation-metadata read).

    Regression this pins: no independent ``root_conversation_id`` lookup,
    no independent labels/session_state/model re-fetch — all sourced from
    the supplied snapshot and the one tree load.
    """
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    root = conversation_store.create_conversation()
    conv = conversation_store.get_conversation(root.id)

    with _count_statements(db_uri) as statements:
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=root.id,
            conversation_store=conversation_store,
            conversation=conv,
            policy_store=policy_store,
        )

    assert len(statements) == 5, statements


def test_second_build_reuses_cached_session_policies(db_uri: str) -> None:
    """
    The batched root+child policy load must compose WITH the session-policy
    LRU, not sit in front of it: a second build for the same session issues
    no ``policies`` query at all.

    Every other test here clears the caches and builds exactly once, so all
    of them measure the COLD path and none can see a batched load that
    bypasses the cache entirely. This one deliberately does not clear
    between the two builds — it is the only coverage of the hot path that
    upstream's ``any_policies_apply`` fast path depends on.
    """
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conv = conversation_store.get_conversation(child.id)

    def _build() -> None:
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=child.id,
            conversation_store=conversation_store,
            conversation=conv,
            policy_store=policy_store,
        )

    with _count_statements(db_uri) as cold:
        _build()
    cold_policy_queries = [s for s in cold if " policies" in s.lower()]
    assert cold_policy_queries, "cold build should read policies at least once"

    with _count_statements(db_uri) as hot:
        _build()
    hot_policy_queries = [s for s in hot if " policies" in s.lower()]
    assert hot_policy_queries == [], hot_policy_queries


def test_supplied_snapshot_active_child_and_root_both_usage_views(db_uri: str) -> None:
    """
    Sub-agent session with an active root, both session-wide and subtree
    usage views needed (subagent_cost_budget policy present) → still 5.

    Regression this pins: the root+child policy load is one batched query
    (not two ``list_for_session`` calls), and the root's session_state
    inheritance comes free from the already-loaded tree (no independent
    root fetch) even when a second (subtree-scoped) usage view is also
    computed from that same tree.
    """
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conv = conversation_store.get_conversation(child.id)

    with _count_statements(db_uri) as statements:
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=child.id,
            conversation_store=conversation_store,
            conversation=conv,
            policy_store=policy_store,
            default_policies=[_SUBAGENT_COST_POLICY],
        )

    assert len(statements) == 5, statements


def test_conversation_none_one_page_active_tree(db_uri: str) -> None:
    """
    ``conversation=None`` → the 5-statement baseline plus exactly one
    internal ``get_conversation`` (1 row + 1 metadata + 1 label query) = 8.

    Regression this pins: the ``None`` path fetches the conversation
    exactly once, not once per former call site (root id, labels, own
    session_state, model override each used to fetch independently).
    """
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    root = conversation_store.create_conversation()

    with _count_statements(db_uri) as statements:
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=root.id,
            conversation_store=conversation_store,
            policy_store=policy_store,
        )

    assert len(statements) == 8, statements


def test_supplied_snapshot_active_child_archived_root(db_uri: str) -> None:
    """
    Sub-agent with an *archived* root → the 5-statement baseline plus
    exactly one fallback ``get_conversation(root_id)`` (1 row + 1 metadata
    + 1 label query) = 8.

    Regression this pins: the hybrid root-state lookup. An archived root
    is invisible to the (active-only) tree, so the fallback fetch must
    fire exactly once — not zero (which would silently drop the child's
    cost-approval inheritance) and not more than once.
    """
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.update_conversation(root.id, archived=True)
    conv = conversation_store.get_conversation(child.id)

    with _count_statements(db_uri) as statements:
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=child.id,
            conversation_store=conversation_store,
            conversation=conv,
            policy_store=policy_store,
        )

    assert len(statements) == 8, statements


def test_supplied_snapshot_two_page_tree(db_uri: str) -> None:
    """
    101-row (2-page) active tree, root session → the 5-statement baseline
    plus one extra tree page (3 statements) = 8.

    Regression this pins: the tree walk is paginated exactly once (not
    twice — the pre-C1 builder ran one walk for session-wide usage and a
    second for the hybrid root-state / subtree lookups).
    """
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    root = conversation_store.create_conversation()
    for _ in range(100):
        conversation_store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)
    conv = conversation_store.get_conversation(root.id)

    with _count_statements(db_uri) as statements:
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=root.id,
            conversation_store=conversation_store,
            conversation=conv,
            policy_store=policy_store,
        )

    assert len(statements) == 8, statements


def test_initial_label_seeding_write_and_reread(db_uri: str) -> None:
    """
    A declared label with no persisted row yet triggers the seed-write +
    reread path — baseline (5, supplied snapshot / active root / one page)
    plus the UPSERT and the post-seed re-read.

    Parametrized separately from the baseline scenarios (per contract §4)
    since the write-and-reread cost is data-dependent on whether declared
    initial labels are already missing. Regression this pins: the
    seed-then-reread mechanic survives the switch to snapshot-sourced
    labels — it must still fire (not be skipped, which would silently
    drop initial-label seeding) and still cost exactly this much (not
    more, e.g. re-fetching the row before seeding too).
    """
    conversation_store = SqlAlchemyConversationStore(db_uri)
    policy_store = SqlAlchemyPolicyStore(db_uri)
    root = conversation_store.create_conversation()
    conv = conversation_store.get_conversation(root.id)
    spec = AgentSpec(
        spec_version=1,
        name="label-seed-agent",
        guardrails=GuardrailsSpec(labels={"integrity": LabelDef(initial="1")}),
    )

    with _count_statements(db_uri) as statements:
        build_policy_engine(
            spec=spec,
            conversation_id=root.id,
            conversation_store=conversation_store,
            conversation=conv,
            policy_store=policy_store,
        )

    # 5 (baseline) + 1 (set_labels UPSERT) + 3 (reread: row + metadata
    # + label) = 9.
    assert len(statements) == 9, statements
