"""
Tests for the C1 ``conversation:`` snapshot parameter on
:func:`build_policy_engine`.

Covers the behavioral surface a query-count test can't: that a supplied
snapshot's VALUES are authoritative and never refreshed, while its LINEAGE
is validated (a reused id under a different root or parent is rejected);
that the ``None`` path still observes the latest DB state; the
wrong-conversation guard; and the archived-root hybrid fallback's
correctness (not just its query count — see
``tests/stores/test_policy_engine_query_counts.py`` for the query-count
pins).
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from omnigent.policies.schema import (
    SESSION_COST_ASK_APPROVED_STATE_KEY,
    SESSION_COST_UNPRICED_APPROVED_KEY,
)
from omnigent.runtime.policies.builder import build_policy_engine
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

_SUBAGENT_COST_POLICY = FunctionPolicySpec(
    name="subagent_budget",
    on=None,
    function=FunctionRef(
        path="omnigent.policies.builtins.cost.subagent_cost_budget",
        arguments={"max_cost_usd": 1000.0},
    ),
)


def _make_spec(name: str = "conv-snapshot-agent") -> AgentSpec:
    return AgentSpec(spec_version=1, name=name)


def test_conversation_id_mismatch_raises_value_error(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A supplied ``conversation`` whose ``.id`` differs from
    ``conversation_id`` raises immediately.

    Guards the call-site threading contract: a misplumbed snapshot would
    otherwise silently seed the engine with another session's labels /
    state / model, so this must fail loud rather than proceeding.
    """
    conv_a = conversation_store.create_conversation()
    conv_b = conversation_store.create_conversation()

    with pytest.raises(ValueError, match=conv_b.id):
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=conv_a.id,
            conversation_store=conversation_store,
            conversation=conv_b,
        )


def test_supplied_snapshot_is_authoritative_over_later_db_mutation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A supplied snapshot's VALUES are used as-is, even when the DB has
    since changed. Its lineage is separately validated (see
    ``_verify_supplied_snapshot_lineage``), but no value it carries is
    re-fetched.

    If an implementer "helpfully" refreshed those values against the DB,
    this would fail (the engine would observe the mutated state instead of
    the frozen snapshot the caller passed in) — their freshness is the
    caller's responsibility per the C1 contract, not the builder's.
    """
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "1"})
    conversation_store.set_session_state(conv.id, {"turn_count": 1})
    conversation_store.set_session_usage(conv.id, {"total_cost_usd": 0.1})
    conversation_store.update_conversation(conv.id, model_override="model-a")
    snapshot = conversation_store.get_conversation(conv.id)

    # Mutate the DB after the snapshot was taken.
    conversation_store.set_labels(conv.id, {"integrity": "0"})
    conversation_store.set_session_state(conv.id, {"turn_count": 99})
    conversation_store.set_session_usage(conv.id, {"total_cost_usd": 0.9})
    conversation_store.update_conversation(conv.id, model_override="model-b")

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=conv.id,
        conversation_store=conversation_store,
        conversation=snapshot,
    )

    assert engine.labels["integrity"] == "1"
    assert engine.session_state["turn_count"] == 1
    # Usage aggregation walks a freshly-loaded spawn tree for sibling/
    # descendant sums, but conversation_id's OWN contribution must still
    # come from the supplied snapshot, not the tree's fresh re-read of the
    # same row — otherwise a caller-supplied snapshot would be silently
    # overridden for usage even though labels/session_state respect it.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.1)
    assert engine.model == "model-a"


def test_conversation_none_reflects_latest_db_state(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``conversation=None`` (the default) always fetches fresh — the engine
    observes whatever is persisted at build time, not a stale snapshot.

    The counterpart to the authoritative-snapshot test above: without a
    supplied snapshot, the builder must still do its own fetch and see
    current state (this is the post-lock-rebuild mechanism's foundation).
    """
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"integrity": "0"})

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )

    assert engine.labels["integrity"] == "0"


def test_snapshot_vs_none_path_golden_equality_root_and_child(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Building with a supplied (matching, un-mutated) snapshot vs. with
    ``conversation=None`` must produce an identical engine seed, for both
    a root session and a sub-agent session — including the subtree usage
    view (only computed when a subagent_cost_budget policy is present) and
    ``root_conversation_id``, not just labels/session_state/usage/model.

    Regression this pins: the snapshot-sourced path (``dict(conv.labels)``
    etc.) must derive exactly the same values the ``None`` path's own
    fetch would — a subtle divergence in one path (e.g. forgetting a
    fallback default, or computing ``initial_subtree_usage``/
    ``root_conversation_id`` differently between the two paths) wouldn't
    show up in either path alone.
    """
    root = conversation_store.create_conversation()
    conversation_store.set_labels(root.id, {"integrity": "1"})
    conversation_store.set_session_state(root.id, {"turn_count": 3})
    conversation_store.set_session_usage(root.id, {"total_cost_usd": 0.2})
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.1})

    for conversation_id in (root.id, child.id):
        snapshot = conversation_store.get_conversation(conversation_id)
        with_snapshot = build_policy_engine(
            spec=_make_spec(),
            conversation_id=conversation_id,
            conversation_store=conversation_store,
            conversation=snapshot,
            default_policies=[_SUBAGENT_COST_POLICY],
        )
        with_none = build_policy_engine(
            spec=_make_spec(),
            conversation_id=conversation_id,
            conversation_store=conversation_store,
            conversation=None,
            default_policies=[_SUBAGENT_COST_POLICY],
        )
        assert with_snapshot.labels == with_none.labels
        assert with_snapshot.session_state == with_none.session_state
        assert with_snapshot.usage == with_none.usage
        assert with_snapshot.model == with_none.model
        assert with_snapshot._root_conversation_id == with_none._root_conversation_id
        assert with_snapshot._root_conversation_id == root.id
        # Both engines must have actually computed a subtree usage view
        # (not both trivially None) for this equality to mean anything.
        assert with_snapshot._subtree_usage is not None
        assert with_snapshot._subtree_usage == with_none._subtree_usage


def test_archived_root_fallback_still_inherits_cost_approval(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A live child whose root has been archived still inherits the root's
    cost-approval keys via the independent fallback fetch.

    This is the hybrid-fork resolution: an archived root is invisible to
    the (active-only) tree load, so the free tree-map lookup must fall
    back to the old independent ``get_conversation(root_id)`` call rather
    than silently dropping the inheritance (which would re-ASK a
    previously-approved spend, or worse, re-gate it).
    """
    root = conversation_store.create_conversation()
    conversation_store.set_session_state(
        root.id,
        {
            SESSION_COST_ASK_APPROVED_STATE_KEY: 0.05,
            SESSION_COST_UNPRICED_APPROVED_KEY: True,
        },
    )
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.update_conversation(root.id, archived=True)

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )

    assert engine.session_state.get(SESSION_COST_ASK_APPROVED_STATE_KEY) == 0.05
    assert engine.session_state.get(SESSION_COST_UNPRICED_APPROVED_KEY) is True


def test_archived_root_and_sibling_usage_excluded_from_both_aggregate_views(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    An archived root's AND an archived sibling's usage are excluded from
    BOTH the session-wide aggregate and the subtree-scoped aggregate,
    matching pre-C1 behavior — the tree walk stays active-only.

    The C1 refactor routes both the hybrid root-state lookup and both
    usage views (session-wide, subtree) through the same shared tree
    load, so this pins that the archived-exclusion behavior wasn't
    accidentally "harmonized" away by that consolidation, for every
    archived node in the tree (not just the root) and for both views
    (not just the session-wide one) — matching c1-implementation-
    contract.md §7 item 15.
    """
    root = conversation_store.create_conversation()
    conversation_store.set_session_usage(root.id, {"total_cost_usd": 0.50})
    live_child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.set_session_usage(live_child.id, {"total_cost_usd": 0.07})
    archived_sibling = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.set_session_usage(archived_sibling.id, {"total_cost_usd": 0.20})
    live_sibling = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.set_session_usage(live_sibling.id, {"total_cost_usd": 0.03})
    conversation_store.update_conversation(root.id, archived=True)
    conversation_store.update_conversation(archived_sibling.id, archived=True)

    # subagent_cost_budget makes the builder also compute the subtree-scoped
    # view — without it, initial_subtree_usage would be None and the
    # subtree-exclusion half of this test would be vacuous.
    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=live_child.id,
        conversation_store=conversation_store,
        default_policies=[_SUBAGENT_COST_POLICY],
    )

    # Session-wide: archived root (0.50) and archived sibling (0.20) both
    # excluded; live_child (0.07) and live_sibling (0.03) both included.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.10)
    # Subtree (live_child + its descendants, of which there are none): just
    # 0.07. Distinct from the session-wide 0.10 above, proving the subtree
    # view is genuinely scoped, not an alias for the session-wide sum.
    assert engine._subtree_usage is not None
    assert engine._subtree_usage["total_cost_usd"] == pytest.approx(0.07)


def test_subtree_and_session_wide_split_correct_across_paginated_tree(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With a subagent_cost_budget policy present (so both the session-wide
    AND subtree-scoped usage views are computed) and a tree spanning more
    than one page, both views are correct AND only one paginated tree walk
    occurs.

    This is the scenario the pre-C1 builder got wrong performance-wise: a
    subagent_cost_budget policy used to trigger a *second*, independent
    paginated walk (:func:`load_session_usage` called again from
    ``_subtree_usage_seed``). Regression this pins: correctness of the
    split (subtree sums only the node + descendants; session-wide sums
    the whole tree) survives sharing one tree load, and the sharing
    itself actually happens (one ``list_conversations`` call sequence,
    not two).
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore as _Store,
    )

    root = conversation_store.create_conversation()
    conversation_store.set_session_usage(root.id, {"total_cost_usd": 0.10})
    mid = conversation_store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)
    conversation_store.set_session_usage(mid.id, {"total_cost_usd": 0.05})
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=mid.id
    )
    conversation_store.set_session_usage(grandchild.id, {"total_cost_usd": 0.02})
    # Pad the tree past one page (_SUBTREE_USAGE_PAGE_SIZE == 100) so the
    # walk must paginate.
    for _ in range(100):
        conversation_store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)

    call_count = 0
    original = _Store.list_conversations

    def _counting_list_conversations(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_Store, "list_conversations", _counting_list_conversations)

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=mid.id,
        conversation_store=conversation_store,
        default_policies=[_SUBAGENT_COST_POLICY],
    )

    # Session-wide: root (0.10) + mid (0.05) + grandchild (0.02) + 100 pad
    # siblings (0 each) = 0.17.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.17)
    # Subtree from mid: mid (0.05) + grandchild (0.02) only, NOT root or
    # the pad siblings.
    assert engine._subtree_usage is not None
    assert engine._subtree_usage["total_cost_usd"] == pytest.approx(0.07)
    # 103 rows total (root + mid + grandchild + 100 pad) => 2 pages. Exactly
    # one paginated walk (2 calls to list_conversations), not two independent
    # walks (4 calls).
    assert call_count == 2, call_count


def test_supplied_snapshot_for_archived_conversation_excludes_its_own_usage(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Supplying a snapshot for an ARCHIVED conversation must NOT reinstate
    its own usage into the aggregate — matching ``conversation=None``
    exactly, for both an archived root and an archived child.

    Regression this pins: the snapshot-authority substitution added to
    respect a supplied snapshot's usage (see
    ``test_supplied_snapshot_is_authoritative_over_later_db_mutation``)
    must exclude conversation_id's own contribution whenever the
    SUPPLIED SNAPSHOT's own ``archived`` flag is ``True`` — a caller
    happening to supply a snapshot for an archived conversation (fresh or
    stale) must see the same 0-contribution the ``None`` path sees, not
    have that pre-existing exclusion silently overridden. See
    ``test_supplied_snapshot_archived_flag_wins_over_concurrent_db_state``
    for the harder case: the snapshot's own flag must win even when the
    live DB's archived state has since changed underneath it.
    """
    # Case 1: archived ROOT, snapshot supplied for the root itself.
    root = conversation_store.create_conversation()
    conversation_store.set_session_usage(root.id, {"total_cost_usd": 0.50})
    conversation_store.update_conversation(root.id, archived=True)
    root_snapshot = conversation_store.get_conversation(root.id)

    with_snapshot = build_policy_engine(
        spec=_make_spec(),
        conversation_id=root.id,
        conversation_store=conversation_store,
        conversation=root_snapshot,
    )
    with_none = build_policy_engine(
        spec=_make_spec(),
        conversation_id=root.id,
        conversation_store=conversation_store,
        conversation=None,
    )
    assert with_snapshot.usage["total_cost_usd"] == with_none.usage["total_cost_usd"] == 0.0

    # Case 2: active root + an archived CHILD, snapshot supplied for the
    # archived child itself.
    root2 = conversation_store.create_conversation()
    conversation_store.set_session_usage(root2.id, {"total_cost_usd": 0.30})
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root2.id
    )
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.20})
    conversation_store.update_conversation(child.id, archived=True)
    child_snapshot = conversation_store.get_conversation(child.id)

    with_snapshot2 = build_policy_engine(
        spec=_make_spec(),
        conversation_id=child.id,
        conversation_store=conversation_store,
        conversation=child_snapshot,
    )
    with_none2 = build_policy_engine(
        spec=_make_spec(),
        conversation_id=child.id,
        conversation_store=conversation_store,
        conversation=None,
    )
    # Both must equal 0.30 (only the active root counted; the archived
    # child's own 0.20 is excluded from the session-wide sum either way).
    assert with_snapshot2.usage["total_cost_usd"] == pytest.approx(0.30)
    assert with_none2.usage["total_cost_usd"] == pytest.approx(0.30)
    assert with_snapshot2.usage["total_cost_usd"] == with_none2.usage["total_cost_usd"]


async def test_supplied_snapshot_archived_flag_wins_over_concurrent_db_state(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The SUPPLIED SNAPSHOT's own ``archived`` flag — not a live re-check
    against the current DB row — decides whether conversation_id's own
    usage counts. Three transition directions, each proving the snapshot
    wins over whatever the DB has done since:

    1. Active snapshot, DB since archived → still counts (0.4, not 0).
    2. Archived snapshot, DB since unarchived → still excluded (0, not 0.4).
    3. Active snapshot, DB row since deleted → still counts (0.4, not 0).

    Regression this pins: an earlier fix gated the substitution on
    whether conversation_id was present in the freshly-reloaded tree —
    but tree presence is a live DB re-check, which the builder's "no
    freshness check" contract on snapshot VALUES forbids. The decision
    must come from the snapshot object itself, not from re-deriving it
    against current DB state.
    """
    spec = _make_spec()

    # 1. Active snapshot, DB archived afterward.
    conv_a = conversation_store.create_conversation()
    conversation_store.set_session_usage(conv_a.id, {"total_cost_usd": 0.4})
    active_snapshot = conversation_store.get_conversation(conv_a.id)
    assert active_snapshot.archived is False
    conversation_store.update_conversation(conv_a.id, archived=True)

    engine_a = build_policy_engine(
        spec=spec,
        conversation_id=conv_a.id,
        conversation_store=conversation_store,
        conversation=active_snapshot,
    )
    assert engine_a.usage["total_cost_usd"] == pytest.approx(0.4)

    # 2. Archived snapshot, DB unarchived afterward.
    conv_b = conversation_store.create_conversation()
    conversation_store.set_session_usage(conv_b.id, {"total_cost_usd": 0.4})
    conversation_store.update_conversation(conv_b.id, archived=True)
    archived_snapshot = conversation_store.get_conversation(conv_b.id)
    assert archived_snapshot.archived is True
    conversation_store.update_conversation(conv_b.id, archived=False)

    engine_b = build_policy_engine(
        spec=spec,
        conversation_id=conv_b.id,
        conversation_store=conversation_store,
        conversation=archived_snapshot,
    )
    assert engine_b.usage["total_cost_usd"] == pytest.approx(0.0)

    # 3. Active snapshot, DB row deleted afterward.
    conv_c = conversation_store.create_conversation()
    conversation_store.set_session_usage(conv_c.id, {"total_cost_usd": 0.4})
    active_snapshot_c = conversation_store.get_conversation(conv_c.id)
    assert active_snapshot_c.archived is False
    await conversation_store.delete_conversation(conv_c.id)

    engine_c = build_policy_engine(
        spec=spec,
        conversation_id=conv_c.id,
        conversation_store=conversation_store,
        conversation=active_snapshot_c,
    )
    assert engine_c.usage["total_cost_usd"] == pytest.approx(0.4)


def test_subtree_usage_respects_supplied_snapshot_over_db_mutation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The SUBTREE usage view (only computed when a subagent_cost_budget
    policy is present) must also seed conversation_id's own contribution
    from a supplied snapshot, not a later DB mutation of the same row.

    Genuinely load-bearing for the subtree path specifically: only
    ``engine._subtree_usage`` is asserted here (not ``engine.usage``), so
    this fails if a future change threads the snapshot-authority fix into
    the session-wide aggregation call but not the subtree one — the two
    calls share ``usage_tree``, but nothing stops a future edit from
    reintroducing a second, unpatched tree reference for just one of them.
    """
    mid = conversation_store.create_conversation()
    conversation_store.set_session_usage(mid.id, {"total_cost_usd": 0.1})
    snapshot = conversation_store.get_conversation(mid.id)
    # Mutate the DB after the snapshot was taken.
    conversation_store.set_session_usage(mid.id, {"total_cost_usd": 0.9})

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=mid.id,
        conversation_store=conversation_store,
        conversation=snapshot,
        default_policies=[_SUBAGENT_COST_POLICY],
    )

    assert engine._subtree_usage is not None
    assert engine._subtree_usage["total_cost_usd"] == pytest.approx(0.1)


def test_subtree_usage_excludes_archived_snapshot_target_but_keeps_descendants(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    When the conversation being built for is itself given an ARCHIVED
    snapshot, the SUBTREE usage view excludes that node's own
    contribution but still includes its live descendants' usage — even
    when the DB has since UNARCHIVED that same conversation, so a fresh
    tree walk alone would (wrongly) include it.

    Topologically distinct from the "both views" archived test above
    (which archives the ROOT/a SIBLING, both outside the evaluated node's
    own subtree — a subtree-aggregation regression wouldn't touch either).
    Here the archived-per-snapshot node IS the subtree root being
    evaluated.

    Load-bearing specifically because the DB is unarchived AFTER the
    snapshot is taken: if the subtree call reverted to summing the fresh
    ``tree`` instead of the snapshot-substituted ``usage_tree``, mid's
    current (active) row WOULD be found and its 0.3 WOULD be included,
    giving 0.35 instead of the correct 0.05 — leaving the DB archived
    throughout (as an earlier version of this test did) can't
    distinguish "excluded because of snapshot authority" from "excluded
    because the DB still says archived," since both produce the same
    0.05 regardless of which code path is exercised.
    """
    root = conversation_store.create_conversation()
    mid = conversation_store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)
    conversation_store.set_session_usage(mid.id, {"total_cost_usd": 0.3})
    conversation_store.update_conversation(mid.id, archived=True)
    archived_mid_snapshot = conversation_store.get_conversation(mid.id)
    assert archived_mid_snapshot.archived is True
    # Unarchive in the DB AFTER capturing the snapshot — a fresh tree walk
    # would now find mid's row (active) and include its 0.3; only
    # snapshot-authority (keyed on the snapshot's own archived=True) still
    # excludes it.
    conversation_store.update_conversation(mid.id, archived=False)
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=mid.id
    )
    conversation_store.set_session_usage(grandchild.id, {"total_cost_usd": 0.05})

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=mid.id,
        conversation_store=conversation_store,
        conversation=archived_mid_snapshot,
        default_policies=[_SUBAGENT_COST_POLICY],
    )

    assert engine._subtree_usage is not None
    # mid's own 0.3 excluded (archived per its own snapshot, even though
    # the DB now says active); grandchild's 0.05 still included (live
    # descendant, unaffected by mid's exclusion).
    assert engine._subtree_usage["total_cost_usd"] == pytest.approx(0.05)


async def test_supplied_snapshot_rejected_when_id_recreated_under_a_different_root(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A snapshot whose id has since been deleted and recreated under a
    DIFFERENT root must be rejected, not silently trusted.

    ``id`` equality is not an incarnation check: ``delete_conversation``
    frees the primary key and ``create_conversation`` accepts an explicit
    ``conversation_id``, so the same id can come back under another parent
    and root. The snapshot's ``root_conversation_id`` drives policy
    inheritance, the tree walk, root approval-state inheritance, usage
    aggregation and the engine's cost write-back root — so trusting a stale
    one silently applies a whole different domain, unlike a stale label or
    usage value, which is deliberately snapshot-authoritative.

    Distinct from the archived and deleted cases above, which must keep
    honouring the snapshot: those leave lineage unchanged (or leave no row
    at all), and this guard only fires when a live row contradicts it.
    """
    root_a = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root_a.id
    )
    stale_snapshot = conversation_store.get_conversation(child.id)
    assert stale_snapshot.root_conversation_id == root_a.id

    # Same id comes back under an unrelated root.
    assert await conversation_store.delete_conversation(child.id) is True
    root_b = conversation_store.create_conversation()
    recreated = conversation_store.create_conversation(
        conversation_id=child.id,
        kind="sub_agent",
        parent_conversation_id=root_b.id,
    )
    assert recreated.id == child.id
    assert recreated.root_conversation_id == root_b.id

    with pytest.raises(ValueError, match="lineage changed"):
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=child.id,
            conversation_store=conversation_store,
            conversation=stale_snapshot,
        )


async def test_supplied_snapshot_rejected_when_id_recreated_under_a_different_parent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Same root, different parent must be rejected too.

    Presence in the root's tree proves only the ROOT. An id can be deleted
    and recreated under a different parent WITHIN the same root, which the
    tree-membership check alone accepts: the row is still in that tree.
    Parent drives subtree topology — ``_subtree_conversation_ids`` decides
    from it which nodes count toward the subtree usage seed a
    ``subagent_cost_budget`` gates on — so a stale parent silently scopes
    enforcement to the wrong branch.

    The freshly-walked tree already carries this row, so both root and
    parent are compared without an extra query.
    """
    root = conversation_store.create_conversation()
    branch_a = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    branch_b = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=branch_a.id
    )
    stale_snapshot = conversation_store.get_conversation(grandchild.id)
    assert stale_snapshot.parent_conversation_id == branch_a.id

    # Same id, same root, reparented under the sibling branch.
    assert await conversation_store.delete_conversation(grandchild.id) is True
    recreated = conversation_store.create_conversation(
        conversation_id=grandchild.id,
        kind="sub_agent",
        parent_conversation_id=branch_b.id,
    )
    assert recreated.id == grandchild.id
    assert recreated.root_conversation_id == stale_snapshot.root_conversation_id
    assert recreated.parent_conversation_id != stale_snapshot.parent_conversation_id

    with pytest.raises(ValueError, match="lineage changed"):
        build_policy_engine(
            spec=_make_spec(),
            conversation_id=grandchild.id,
            conversation_store=conversation_store,
            conversation=stale_snapshot,
        )


def test_seeding_a_missing_label_keeps_snapshot_authority_for_the_others(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Seeding one absent label must not refresh the labels the snapshot carries.

    The two behaviours overlap here in a way neither the stale-label nor the
    seeding test exercises alone: the snapshot carries ``frozen``, the DB has
    since moved it, AND a *different* declared label is missing and so
    triggers the post-seed re-read. Taking that re-read wholesale silently
    replaces ``frozen`` too, destroying the snapshot authority the supplied
    path deliberately promises.
    """
    conv = conversation_store.create_conversation()
    conversation_store.set_labels(conv.id, {"frozen": "old"})
    snapshot = conversation_store.get_conversation(conv.id)
    assert snapshot.labels["frozen"] == "old"

    # DB moves on after the caller captured its snapshot.
    conversation_store.set_labels(conv.id, {"frozen": "new"})
    assert conversation_store.get_conversation(conv.id).labels["frozen"] == "new"

    # An unrelated declared label is absent, so seeding (and the re-read) runs.
    spec = AgentSpec(
        spec_version=1,
        name="conv-snapshot-agent",
        guardrails=GuardrailsSpec(labels={"integrity": LabelDef(initial="1")}),
    )
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        conversation=snapshot,
    )

    assert engine.labels["integrity"] == "1", "declared initial label was not seeded"
    assert engine.labels["frozen"] == "old", (
        "seeding an unrelated label discarded snapshot authority for 'frozen'"
    )


@pytest.mark.parametrize("spelling", ["dashed", "prefixed"])
async def test_supplied_snapshot_build_accepts_any_id_spelling(
    conversation_store: SqlAlchemyConversationStore,
    spelling: str,
) -> None:
    """A build driven by a non-canonical route id still matches its snapshot.

    Production hands the builder the raw path id alongside the canonical
    ``conv`` it already fetched. Upstream accepts dashed and legacy-prefixed
    uuids, so a bookmarked URL in either form reaches here and a raw-vs-
    canonical comparison rejects it outright.

    :param conversation_store: Store fixture.
    :param spelling: Which accepted non-canonical form to drive the build with.
    """
    conv = conversation_store.create_conversation()
    snapshot = conversation_store.get_conversation(conv.id)
    if spelling == "dashed":
        route_id = str(uuid.UUID(hex=conv.id))
    else:
        route_id = f"conv_{conv.id}"
    assert route_id != conv.id

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=route_id,
        conversation_store=conversation_store,
        conversation=snapshot,
    )

    assert engine is not None


async def test_lineage_is_validated_before_any_label_is_written(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A stale-lineage build must write nothing into the replacement row.

    The guard exists to decide which conversation this build reads and
    writes. Running it after the label seed makes it a detector rather than
    a guard: the declared initial label lands in the *replacement*
    incarnation and only then does the build raise, leaving a write behind
    that the caller was told did not happen.
    """
    root = conversation_store.create_conversation()
    branch_a = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    branch_b = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=branch_a.id
    )
    stale_snapshot = conversation_store.get_conversation(grandchild.id)

    assert await conversation_store.delete_conversation(grandchild.id) is True
    conversation_store.create_conversation(
        conversation_id=grandchild.id,
        kind="sub_agent",
        parent_conversation_id=branch_b.id,
    )

    spec = AgentSpec(
        spec_version=1,
        name="conv-snapshot-agent",
        guardrails=GuardrailsSpec(labels={"integrity": LabelDef(initial="1")}),
    )
    with pytest.raises(ValueError, match="lineage changed"):
        build_policy_engine(
            spec=spec,
            conversation_id=grandchild.id,
            conversation_store=conversation_store,
            conversation=stale_snapshot,
        )

    replacement = conversation_store.get_conversation(grandchild.id)
    assert "integrity" not in replacement.labels, (
        "the rejected build seeded a label into the replacement conversation "
        "before the lineage guard ran"
    )


def test_seeding_does_not_import_db_labels_the_snapshot_omits(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Absence in the supplied snapshot is authoritative, not a gap to fill.

    Merging the whole post-seed re-read under the snapshot fixes the
    value-conflict case but not this one: a key the snapshot does not carry
    has no snapshot value to win, so a later DB-only write flows straight
    through. Only the keys actually created by seeding may be added.
    """
    conv = conversation_store.create_conversation()
    snapshot = conversation_store.get_conversation(conv.id)
    assert snapshot.labels == {}

    # A label the caller's snapshot has no opinion on, written after it.
    conversation_store.set_labels(conv.id, {"late": "from_db"})

    # An unrelated declared label is absent, so seeding (and the re-read) runs.
    spec = AgentSpec(
        spec_version=1,
        name="conv-snapshot-agent",
        guardrails=GuardrailsSpec(labels={"integrity": LabelDef(initial="1")}),
    )
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        conversation=snapshot,
    )

    assert engine.labels["integrity"] == "1", "declared initial label was not seeded"
    assert "late" not in engine.labels, (
        "seeding pulled in a label the snapshot deliberately omitted — "
        "absence in the snapshot is authoritative too"
    )


def _respell(hex_id: str, style: str) -> str:
    """Return *hex_id* in another accepted spelling."""
    return str(uuid.UUID(hex=hex_id)) if style == "dashed" else f"conv_{hex_id}"


@pytest.mark.parametrize("style", ["dashed", "prefixed"])
def test_snapshot_lineage_in_another_spelling_is_accepted(
    conversation_store: SqlAlchemyConversationStore,
    style: str,
) -> None:
    """Correct lineage in a non-bare-hex spelling must not read as changed.

    ``Conversation`` is a plain dataclass: its three id fields are
    unvalidated ``str``, so a snapshot assembled outside the store can
    carry any accepted spelling. The lineage guard compares them against
    rows read back through ``Uuid16`` as bare hex, so an unnormalised
    comparison rejects a snapshot whose lineage is in fact unchanged.

    :param style: Alternate id spelling to build the snapshot with.
    """
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    snapshot = conversation_store.get_conversation(child.id)
    assert snapshot is not None
    respelled = dataclasses.replace(
        snapshot,
        root_conversation_id=_respell(snapshot.root_conversation_id, style),
        parent_conversation_id=_respell(snapshot.parent_conversation_id or "", style),
    )

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=child.id,
        conversation_store=conversation_store,
        conversation=respelled,
    )

    assert engine.conversation_id == child.id
    assert engine._root_conversation_id == root.id, (
        "the engine kept a non-canonical root id — writes routed to the root "
        "and every later tree match would miss the row they target"
    )


@pytest.mark.parametrize("style", ["dashed", "prefixed"])
def test_snapshot_in_another_spelling_still_contributes_its_own_usage(
    conversation_store: SqlAlchemyConversationStore,
    style: str,
) -> None:
    """A respelled snapshot must still count toward both usage views.

    The usage sums match tree rows on ``.id``, and the session-wide view
    seeds its walk from ``root_conversation_id``. An unnormalised snapshot
    is therefore invisible to the subtree sum (its own spend vanishes)
    while the session-wide walk seeds from an id no row carries and comes
    back empty. Both are silent wrong answers: a cost policy would gate
    against an understated total rather than error.

    :param style: Alternate id spelling to build the snapshot with.
    """
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.set_session_usage(root.id, {"total_cost_usd": 0.25})
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.5})
    snapshot = conversation_store.get_conversation(child.id)
    assert snapshot is not None
    respelled = dataclasses.replace(
        snapshot,
        id=_respell(snapshot.id, style),
        root_conversation_id=_respell(snapshot.root_conversation_id, style),
        parent_conversation_id=_respell(snapshot.parent_conversation_id or "", style),
    )

    engine = build_policy_engine(
        spec=_make_spec(),
        conversation_id=child.id,
        conversation_store=conversation_store,
        conversation=respelled,
        default_policies=[_SUBAGENT_COST_POLICY],
    )

    # Session-wide seed walks the whole tree from the root.
    assert engine.usage["total_cost_usd"] == pytest.approx(0.75), (
        "session-wide usage seed came back short — the tree walk was seeded "
        "from a root id spelling no row carries"
    )
    # Subtree seed matches on the snapshot's own id.
    assert engine._subtree_usage is not None
    assert engine._subtree_usage["total_cost_usd"] == pytest.approx(0.5), (
        "the snapshot's own spend was dropped from its subtree total — it "
        "was re-inserted under a spelling the subtree match cannot see"
    )
