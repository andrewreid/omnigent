"""Tests for session and default policy loading in :func:`build_policy_engine`.

Verifies that enabled session policies stored via the CRUD API are
loaded by the builder, converted to :class:`FunctionPolicySpec`,
resolved to :class:`FunctionPolicy` instances, and participate in
engine evaluation alongside spec-declared policies.

Also covers DB-stored default policies (``session_id IS NULL``) and the
:func:`_load_default_policy_specs` TTL-cache behaviour.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from omnigent.entities import Policy as StoredPolicy
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.function import FunctionPolicy
from omnigent.runtime.policies.builder import (
    _DEFAULT_POLICY_SPECS_CACHE,
    _SESSION_OWNER_CACHE,
    _SESSION_POLICY_SPECS_CACHE,
    _load_default_policy_specs,
    _load_session_policy_specs_batch,
    _resolve_session_owner_cached,
    _stored_policy_to_spec,
    any_policies_apply,
    build_policy_engine,
    invalidate_default_policy_specs_cache,
    invalidate_session_policy_specs_cache,
)
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

# ── _stored_policy_to_spec ──────────────────────────────────────────────────


def test_stored_python_policy_to_spec() -> None:
    """A stored ``type="python"`` policy converts to a FunctionPolicySpec.

    The FunctionRef must carry the handler as ``path`` and
    ``factory_params`` as ``arguments``. ``on`` must be ``None``
    so the engine skips phase filtering (callable self-selects).
    """
    stored = StoredPolicy(
        id="836115190a01c5c536c2bdbbeff76c6c",
        name="rate_limit",
        session_id="0099dc8be6d82871e2e450424d46d1b7",
        scope="session",
        created_at=1000,
        type="python",
        handler="myorg.policies.rate_limit",
        factory_params={"limit": 10},
    )
    spec = _stored_policy_to_spec(stored)

    assert spec is not None
    assert isinstance(spec, FunctionPolicySpec)
    assert spec.name == "rate_limit"
    assert spec.on is None
    assert spec.function is not None
    assert spec.function.path == "myorg.policies.rate_limit"
    assert spec.function.arguments == {"limit": 10}


def test_stored_python_policy_without_factory_params() -> None:
    """A stored Python policy with no factory_params gets ``arguments=None``."""
    stored = StoredPolicy(
        id="da881c94710f663083e832772c9846a5",
        name="simple",
        session_id="0099dc8be6d82871e2e450424d46d1b7",
        scope="session",
        created_at=1000,
        type="python",
        handler="myorg.policies.simple_check",
    )
    spec = _stored_policy_to_spec(stored)

    assert spec is not None
    assert isinstance(spec, FunctionPolicySpec)
    assert spec.function is not None
    assert spec.function.arguments is None


def test_stored_url_policy_raises() -> None:
    """A stored ``type="url"`` policy is rejected loudly, not skipped.

    URL policy evaluation is unimplemented; converting one must raise
    rather than silently return ``None`` (which would let an operator
    store a guardrail that never enforces).
    """
    stored = StoredPolicy(
        id="0649a4ce3cc08828d91e43d38b2d5f4c",
        name="external",
        session_id="0099dc8be6d82871e2e450424d46d1b7",
        scope="session",
        created_at=1000,
        type="url",
        handler="https://example.com/eval",
    )
    with pytest.raises(OmnigentError) as excinfo:
        _stored_policy_to_spec(stored)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT
    assert "url" in str(excinfo.value)
    assert "external" in str(excinfo.value)


# ── _load_session_policy_specs_batch ────────────────────────────────────────


def test_load_session_policy_specs_none_store() -> None:
    """When ``policy_store`` is ``None``, every id maps to an empty list."""
    conv_id = "0099dc8be6d82871e2e450424d46d1b7"
    assert _load_session_policy_specs_batch([conv_id], None) == {conv_id: []}


def test_load_session_policy_specs_caches_result(db_uri: str) -> None:
    """A second call returns the cached result without hitting the store.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    store = SqlAlchemyPolicyStore(db_uri)
    store.create(
        policy_id="761d8d3f506e256fe5a0a871cf9599fc",
        session_id=conv.id,
        name="cache_test",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    _SESSION_POLICY_SPECS_CACHE.clear()

    first = _load_session_policy_specs_batch([conv.id], store)[conv.id]
    store.create(
        policy_id="05a4f08244fca2e47f8fcf558fac5d4c",
        session_id=conv.id,
        name="cache_test2",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    second = _load_session_policy_specs_batch([conv.id], store)[conv.id]

    assert second is first


def test_invalidate_session_policy_specs_cache(db_uri: str) -> None:
    """Invalidating the cache forces the next call to re-read from the store.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    store = SqlAlchemyPolicyStore(db_uri)
    store.create(
        policy_id="7b281600bfa993299f67187e524a49fb",
        session_id=conv.id,
        name="inv_policy1",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    _SESSION_POLICY_SPECS_CACHE.clear()

    first = _load_session_policy_specs_batch([conv.id], store)[conv.id]
    assert len(first) == 1

    store.create(
        policy_id="31ec3b5f905b29ebddd6f7a1a5570547",
        session_id=conv.id,
        name="inv_policy2",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    invalidate_session_policy_specs_cache(conv.id)

    second = _load_session_policy_specs_batch([conv.id], store)[conv.id]
    assert len(second) == 2


def test_load_session_policy_specs_filters_disabled(db_uri: str) -> None:
    """Disabled policies are excluded from the loaded specs.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    store = SqlAlchemyPolicyStore(db_uri)
    store.create(
        policy_id="fd0deac497210bc17cba2e1c66afe833",
        session_id=conv.id,
        name="enabled_policy",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    store.create(
        policy_id="96eef7369235e1bacfd949e6447f0eeb",
        session_id=conv.id,
        name="disabled_policy",
        type="python",
        handler="myorg.policies.deny_all",
        enabled=False,
    )

    specs = _load_session_policy_specs_batch([conv.id], store)[conv.id]

    assert len(specs) == 1
    assert specs[0].name == "enabled_policy"


def test_load_session_policy_specs_rejects_enabled_url(db_uri: str) -> None:
    """An enabled url-type session policy raises at load time (fail closed).

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    store = SqlAlchemyPolicyStore(db_uri)
    store.create(
        policy_id="0649a4ce3cc08828d91e43d38b2d5f4c",
        session_id=conv.id,
        name="external",
        type="url",
        handler="https://example.com/eval",
        enabled=True,
    )

    with pytest.raises(OmnigentError) as excinfo:
        _load_session_policy_specs_batch([conv.id], store)
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


# ── build_policy_engine integration ─────────────────────────────────────────


def _make_minimal_spec() -> AgentSpec:
    """Build a minimal AgentSpec with no guardrails.

    :returns: An :class:`AgentSpec` with all required fields set to
        minimal values and no guardrails.
    """
    return AgentSpec(
        spec_version=1,
        name="test-agent",
    )


def test_build_engine_includes_session_policies(db_uri: str) -> None:
    """Session policies from the store appear in the engine's policy list.

    Creates a session policy pointing at a test callable, builds the
    engine, and verifies the callable was resolved into a FunctionPolicy.

    :param db_uri: Per-test SQLite URI.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="b52655498c35d115250d7f89a3422b5f",
        session_id=conv.id,
        name="test_policy",
        type="python",
        # Point at a real callable in the test resources.
        handler="tests.resources.examples._shared.tool_functions.block_long_sleep",
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    assert isinstance(engine.policies[0], FunctionPolicy)
    assert engine.policies[0].spec.name == "test_policy"
    assert engine.policies[-1].spec.name == "__ask_on_add_policy"


def test_build_engine_no_store_returns_noop(db_uri: str) -> None:
    """Without a policy store, the engine has no policies (noop).

    :param db_uri: Per-test SQLite URI.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id="ad563e906854634c49e1a6fd2fbb31d4",
        conversation_store=conv_store,
        policy_store=None,
    )

    # No user-declared policies, but ask_on_add_policy is always present.
    assert len(engine.policies) == 1
    assert engine.policies[0].spec.name == "__ask_on_add_policy"


def test_build_engine_ordering_session_agent_admin(db_uri: str) -> None:
    """Policy evaluation order is session → agent → admin.

    Creates one policy at each layer and verifies their position
    in the engine's policy list matches the documented contract.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    # Agent-declared policy via spec guardrails.
    agent_policy = FunctionPolicySpec(
        name="agent_policy",
        on=None,
        function=FunctionRef(path=handler),
    )
    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(policies=[agent_policy]),
    )

    # Admin (server-wide default) policy.
    admin_policy = FunctionPolicySpec(
        name="admin_policy",
        on=None,
        function=FunctionRef(path=handler),
    )

    # Session policy from the store.
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="28cb2620dd5d5ba3cb7560b76843cc03",
        session_id=conv.id,
        name="session_policy",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conv_store,
        default_policies=[admin_policy],
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert names == [
        "session_policy",
        "agent_policy",
        "admin_policy",
        "__ask_on_add_policy",
    ]


# ── Sub-agent session policy inheritance ───────────────────────────────────


def test_subagent_inherits_root_session_policies(db_uri: str) -> None:
    """Session policies on the root conversation propagate to sub-agents.

    Creates a root conversation with a session policy, spawns a
    sub-agent (child conversation), and verifies that the child's
    policy engine includes the root's session policy.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()
    child_conv = conv_store.create_conversation(
        parent_conversation_id=root_conv.id,
        kind="sub_agent",
    )

    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="c6de31de238a26c347a7c3d8d5a74c3a",
        session_id=root_conv.id,
        name="root_guard",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert "root_guard" in names, f"root session policy not inherited by sub-agent; got {names}"
    # Root policy should come before the ask_on_add_policy sentinel.
    assert names.index("root_guard") < names.index("__ask_on_add_policy")


def test_subagent_deduplicates_same_name_policy(db_uri: str) -> None:
    """When root and child both have a policy with the same name, child wins.

    The root's copy is dropped to avoid double-evaluation. The
    child's version appears in the engine at the session-policy
    position.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()
    child_conv = conv_store.create_conversation(
        parent_conversation_id=root_conv.id,
        kind="sub_agent",
    )

    policy_store = SqlAlchemyPolicyStore(db_uri)
    # Same-name policy on both root and child.
    policy_store.create(
        policy_id="c6de31de238a26c347a7c3d8d5a74c3a",
        session_id=root_conv.id,
        name="shared_guard",
        type="python",
        handler=handler,
    )
    policy_store.create(
        policy_id="86507aab3e1f97f6b1bace6058204f1a",
        session_id=child_conv.id,
        name="shared_guard",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    # "shared_guard" should appear exactly once (child's version).
    assert names.count("shared_guard") == 1, (
        f"expected exactly 1 'shared_guard', got {names.count('shared_guard')} in {names}"
    )


def test_root_session_does_not_double_load(db_uri: str) -> None:
    """A root conversation (no parent) loads its own policies once.

    Ensures the root-inheritance path is a no-op when the
    conversation is already the root (``root_conversation_id == id``).

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()

    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="c6de31de238a26c347a7c3d8d5a74c3a",
        session_id=root_conv.id,
        name="root_only",
        type="python",
        handler=handler,
    )

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=root_conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert names.count("root_only") == 1, (
        f"root policy loaded {names.count('root_only')} times in {names}"
    )


# ── _load_default_policy_specs ──────────────────────────────────────────────


def test_load_default_policy_specs_none_store() -> None:
    """When ``policy_store`` is ``None``, returns an empty list."""
    assert _load_default_policy_specs(None) == []


def test_load_default_policy_specs_skips_url_type(db_uri: str) -> None:
    """A default policy with ``type='url'`` is skipped, not raised.

    Unlike session policies (where an unsupported type raises loudly),
    unsupported-type default policies must not crash engine construction
    globally — they are logged and skipped so a stale row can't cause a
    server-wide outage.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    store = SqlAlchemyPolicyStore(db_uri)
    # Insert a url-type default directly via the store (bypassing the route
    # guard that rejects url defaults at creation time).
    store.create_default(
        policy_id="fe00550b91828f5ab080225d7982fa8a",
        name="url_default",
        type="url",
        handler="https://example.com/eval",
        enabled=True,
    )
    store.create_default(
        policy_id="9630be719cf8872a30e0d820fc737c30",
        name="python_default",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    _DEFAULT_POLICY_SPECS_CACHE.clear()

    # Should not raise — url policy is skipped, python policy is included.
    specs = _load_default_policy_specs(store)

    assert len(specs) == 1
    assert specs[0].name == "python_default"


def test_load_default_policy_specs_filters_disabled(db_uri: str) -> None:
    """Disabled default policies are excluded from the loaded specs.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    store = SqlAlchemyPolicyStore(db_uri)
    store.create_default(
        policy_id="8b0c52d27883a504e03b87ac6d10abae",
        name="enabled_default",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    store.create_default(
        policy_id="b5edc7521a4113f7a2931c06458f8416",
        name="disabled_default",
        type="python",
        handler="myorg.policies.deny_all",
        enabled=False,
    )
    _DEFAULT_POLICY_SPECS_CACHE.clear()

    specs = _load_default_policy_specs(store)

    assert len(specs) == 1
    assert specs[0].name == "enabled_default"


def test_load_default_policy_specs_caches_result(db_uri: str) -> None:
    """A second call returns the cached result without hitting the store.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    store = SqlAlchemyPolicyStore(db_uri)
    store.create_default(
        policy_id="5be4b4fa96edbc18615a67e62dc34dae",
        name="cache_test",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    _DEFAULT_POLICY_SPECS_CACHE.clear()

    first = _load_default_policy_specs(store)
    # Add a second default policy directly — bypasses the cache.
    store.create_default(
        policy_id="3577c758d2840a6ed1149b2a04611222",
        name="cache_test2",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    second = _load_default_policy_specs(store)

    # Cache hit: second call returns the same object as first, missing the new policy.
    assert second is first


def test_invalidate_default_policy_specs_cache(db_uri: str) -> None:
    """Invalidating the cache forces the next call to re-read from the store.

    :param db_uri: Per-test SQLite URI from the root conftest.
    """
    store = SqlAlchemyPolicyStore(db_uri)
    store.create_default(
        policy_id="b991d86d83432c7b91fc08127e31a153",
        name="inv_policy1",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    _DEFAULT_POLICY_SPECS_CACHE.clear()

    first = _load_default_policy_specs(store)
    assert len(first) == 1

    store.create_default(
        policy_id="2574142d58ba1496e7339bc1043fa206",
        name="inv_policy2",
        type="python",
        handler="myorg.policies.allow_all",
        enabled=True,
    )
    invalidate_default_policy_specs_cache()

    second = _load_default_policy_specs(store)
    assert len(second) == 2


# ── build_policy_engine: DB default policies integration ────────────────────


def test_build_engine_includes_db_default_policies(db_uri: str) -> None:
    """DB-stored default policies appear in the engine's policy list.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create_default(
        policy_id="ed307f905af1d035ee90159d05a92d70",
        name="db_default_policy",
        type="python",
        handler=handler,
    )
    _DEFAULT_POLICY_SPECS_CACHE.clear()

    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert "db_default_policy" in names


def test_build_engine_ordering_session_agent_db_default_admin(db_uri: str) -> None:
    """Policy evaluation order is session → agent → DB default → YAML admin.

    :param db_uri: Per-test SQLite URI.
    """
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    agent_policy = FunctionPolicySpec(
        name="agent_policy",
        on=None,
        function=FunctionRef(path=handler),
    )
    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(policies=[agent_policy]),
    )
    yaml_admin_policy = FunctionPolicySpec(
        name="yaml_admin_policy",
        on=None,
        function=FunctionRef(path=handler),
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="28cb2620dd5d5ba3cb7560b76843cc03",
        session_id=conv.id,
        name="session_policy",
        type="python",
        handler=handler,
    )
    policy_store.create_default(
        policy_id="efbd7a351c1b7024b7671f1c5096cac3",
        name="db_default_policy",
        type="python",
        handler=handler,
    )
    _DEFAULT_POLICY_SPECS_CACHE.clear()

    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conv_store,
        default_policies=[yaml_admin_policy],
        policy_store=policy_store,
    )

    names = [p.spec.name for p in engine.policies]
    assert names == [
        "session_policy",
        "agent_policy",
        "db_default_policy",
        "yaml_admin_policy",
        "__ask_on_add_policy",
    ]


# ── any_policies_apply fast path ───────────────────────────────────────────


def test_fast_path_sees_inherited_root_policies(db_uri: str) -> None:
    """A policy-free child under a policy-carrying root must not be skipped.

    ``any_policies_apply`` guards the hook surface: returning ``False``
    short-circuits to unconditional ALLOW without building an engine. The
    builder inherits the ROOT's stored policies for a sub-agent, so a check
    that looked only at the child's own id would silently drop enforcement
    of every root policy.

    :param db_uri: Per-test SQLite URI.
    """
    # Both memos are process-global and survive across tests in this file;
    # a warm default-policy entry would make the fast path answer True for
    # the wrong reason.
    _SESSION_POLICY_SPECS_CACHE.clear()
    _DEFAULT_POLICY_SPECS_CACHE.clear()
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()
    child_conv = conv_store.create_conversation(
        parent_conversation_id=root_conv.id,
        kind="sub_agent",
    )

    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="9a1c0f4b7d2e48a6b3c5d7e9f1a2b3c4",
        session_id=root_conv.id,
        name="root_guard",
        type="python",
        handler=handler,
    )

    # The child itself has no stored policy — only the root does.
    assert _load_session_policy_specs_batch([child_conv.id], policy_store)[child_conv.id] == []

    applies = any_policies_apply(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        root_conversation_id=root_conv.id,
        default_policies=None,
        policy_store=policy_store,
    )
    assert applies is True, (
        "fast path skipped a child whose root carries a policy — inherited "
        "root policies would go unenforced"
    )

    # The invariant the fast path exists to preserve: it may only return
    # False when the engine it replaces would enforce nothing.
    engine = build_policy_engine(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        conversation_store=conv_store,
        policy_store=policy_store,
    )
    assert "root_guard" in [p.spec.name for p in engine.policies]


def test_fast_path_false_only_when_engine_enforces_nothing(db_uri: str) -> None:
    """With no policies anywhere, the fast path may skip the build.

    The negative half of the previous test: without it, a fast path hardwired
    to ``True`` would pass the inheritance assertion while destroying the
    optimization entirely.

    :param db_uri: Per-test SQLite URI.
    """
    _SESSION_POLICY_SPECS_CACHE.clear()
    _DEFAULT_POLICY_SPECS_CACHE.clear()
    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()
    child_conv = conv_store.create_conversation(
        parent_conversation_id=root_conv.id,
        kind="sub_agent",
    )
    policy_store = SqlAlchemyPolicyStore(db_uri)

    applies = any_policies_apply(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        root_conversation_id=root_conv.id,
        default_policies=None,
        policy_store=policy_store,
    )
    assert applies is False


def test_fast_path_without_lineage_never_reports_nothing_applies(db_uri: str) -> None:
    """``root_conversation_id=None`` must not yield a fast-path ALLOW.

    Making the parameter required only makes its *omission* loud; a caller
    that passes ``None`` still reaches the body. Absent lineage cannot
    distinguish "no root policies" from "root policies this call cannot
    see", so the only safe answer is to build the engine.

    :param db_uri: Per-test SQLite URI.
    """
    _SESSION_POLICY_SPECS_CACHE.clear()
    _DEFAULT_POLICY_SPECS_CACHE.clear()
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    root_conv = conv_store.create_conversation()
    child_conv = conv_store.create_conversation(
        parent_conversation_id=root_conv.id,
        kind="sub_agent",
    )
    policy_store = SqlAlchemyPolicyStore(db_uri)
    policy_store.create(
        policy_id="3f8b1c92d074e5a6b81f3c2d9e405a71",
        session_id=root_conv.id,
        name="root_guard",
        type="python",
        handler=handler,
    )

    applies = any_policies_apply(
        spec=_make_minimal_spec(),
        conversation_id=child_conv.id,
        root_conversation_id=None,
        default_policies=None,
        policy_store=policy_store,
    )

    assert applies is True, (
        "fast path returned False without lineage — a caller that cannot "
        "supply the root obtains an unconditional ALLOW for a child whose "
        "root carries a policy"
    )


def test_session_policy_cache_invalidates_across_id_spellings(db_uri: str) -> None:
    """A mutation routed through a dashed id evicts the canonical entry.

    The builder warms the cache with the canonical bare-hex id it derives
    from the entity, while a CRUD route invalidates using whatever spelling
    the client sent. Keying the two differently leaves the warm entry in
    place and the next build serves policies that were already deleted.

    :param db_uri: Per-test SQLite URI.
    """
    _SESSION_POLICY_SPECS_CACHE.clear()
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    store = SqlAlchemyPolicyStore(db_uri)
    store.create(
        policy_id="c40d9e17a5b2483f96e1d7c0b3a8542e",
        session_id=conv.id,
        name="first",
        type="python",
        handler="myorg.policies.allow_all",
    )

    warm = _load_session_policy_specs_batch([conv.id], store)[conv.id]
    assert [p.name for p in warm] == ["first"]

    store.create(
        policy_id="8e2a5f31c67b40d9825ef1a4c093b7d6",
        session_id=conv.id,
        name="second",
        type="python",
        handler="myorg.policies.allow_all",
    )
    dashed = str(uuid.UUID(hex=conv.id))
    assert dashed != conv.id
    invalidate_session_policy_specs_cache(dashed)

    # Ordering is ``created_at ASC, id ASC`` and both rows share a timestamp,
    # so compare as a set — the point is that the second policy is visible.
    after = _load_session_policy_specs_batch([conv.id], store)[conv.id]
    assert {p.name for p in after} == {"first", "second"}, (
        "invalidating via a dashed id left the canonical cache entry stale"
    )


def test_load_racing_an_invalidation_does_not_publish_its_stale_result(
    db_uri: str,
) -> None:
    """A load that started before a mutation must not repopulate the cache.

    The entry has no TTL, so a stale value installed after its own eviction
    is stale until the *next* mutation rather than for a bounded window.
    With the fast path reading this cache, that is an indefinite
    unconditional ALLOW for a session that does carry a policy — the
    failure class this guard exists to prevent.

    :param db_uri: Per-test SQLite URI.
    """
    _SESSION_POLICY_SPECS_CACHE.clear()
    _DEFAULT_POLICY_SPECS_CACHE.clear()
    handler = "tests.resources.examples._shared.tool_functions.block_long_sleep"

    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)

    read_done = threading.Event()
    may_publish = threading.Event()
    batch_queries: list[list[str]] = []

    class _DescheduledAfterReadStore(SqlAlchemyPolicyStore):
        """Stalls between reading the store and returning to the loader."""

        def list_for_sessions(self, session_ids: list[str]) -> Any:
            batch_queries.append(list(session_ids))
            rows = super().list_for_sessions(session_ids)
            read_done.set()
            assert may_publish.wait(timeout=30)
            return rows

    racing: dict[str, bool] = {}

    def _race() -> None:
        racing["applies"] = any_policies_apply(
            spec=_make_minimal_spec(),
            conversation_id=conv.id,
            root_conversation_id=conv.id,
            default_policies=None,
            policy_store=_DescheduledAfterReadStore(db_uri),
        )

    racer = threading.Thread(target=_race)
    racer.start()
    try:
        assert read_done.wait(timeout=30), "racing load never reached the store"
        # The mutation commits and evicts while that load is still in flight.
        policy_store.create(
            policy_id="1f2e3d4c5b6a79880011223344556677",
            session_id=conv.id,
            name="added_mid_flight",
            type="python",
            handler=handler,
        )
        invalidate_session_policy_specs_cache(conv.id)
    finally:
        may_publish.set()
        racer.join(timeout=30)
    assert not racer.is_alive()

    # The racing call read before the commit, so its own answer is allowed to
    # predate the policy; only what it leaves behind is under test.
    assert racing["applies"] is False
    assert len(batch_queries) == 1

    assert any_policies_apply(
        spec=_make_minimal_spec(),
        conversation_id=conv.id,
        root_conversation_id=conv.id,
        default_policies=None,
        policy_store=policy_store,
    ), (
        "the in-flight load republished its pre-mutation result after the "
        "eviction — the fast path now returns an unconditional ALLOW for a "
        "session carrying a policy, and no further mutation will clear it"
    )


def test_default_load_racing_an_invalidation_does_not_publish_its_stale_result(
    db_uri: str,
) -> None:
    """A default-policy load started before a create must not repopulate.

    ``any_policies_apply``'s contract names DB-stored defaults, so a stale
    empty entry republished after its own eviction grants a fast-path
    unconditional ALLOW while a committed default policy exists — bounded
    by the 30 s TTL, but a real bypass for that whole window.

    :param db_uri: Per-test database URI (SQLite by default; the
        session worker's PostgreSQL/MySQL database when OMNIGENT_TEST_DB_URI
        is set).
    """
    _SESSION_POLICY_SPECS_CACHE.clear()
    _DEFAULT_POLICY_SPECS_CACHE.clear()

    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    policy_store = SqlAlchemyPolicyStore(db_uri)

    read_done = threading.Event()
    may_publish = threading.Event()
    default_queries: list[int] = []

    class _DescheduledAfterReadStore(SqlAlchemyPolicyStore):
        """Stalls between reading the defaults and returning to the loader."""

        def list_defaults(self) -> Any:
            rows = super().list_defaults()
            default_queries.append(len(rows))
            # Stall AFTER the read has produced its rows: a probe that
            # blocked before the query would publish a fresh result and
            # report a false green.
            read_done.set()
            assert may_publish.wait(timeout=30)
            return rows

    racing: dict[str, bool] = {}

    def _race() -> None:
        racing["applies"] = any_policies_apply(
            spec=_make_minimal_spec(),
            conversation_id=conv.id,
            root_conversation_id=conv.id,
            default_policies=None,
            policy_store=_DescheduledAfterReadStore(db_uri),
        )

    racer = threading.Thread(target=_race)
    racer.start()
    try:
        assert read_done.wait(timeout=30), "racing load never reached the store"
        # The default policy commits and evicts while that load is in flight.
        policy_store.create_default(
            policy_id="9a8b7c6d5e4f30211122334455667788",
            name="default_added_mid_flight",
            type="python",
            handler="tests.resources.examples._shared.tool_functions.block_long_sleep",
            enabled=True,
        )
        invalidate_default_policy_specs_cache()
    finally:
        may_publish.set()
        racer.join(timeout=30)
    assert not racer.is_alive()

    # The racing call read before the commit, so its own answer may predate
    # the policy; only what it leaves behind is under test.
    assert racing["applies"] is False
    assert default_queries == [0]

    try:
        assert any_policies_apply(
            spec=_make_minimal_spec(),
            conversation_id=conv.id,
            root_conversation_id=conv.id,
            default_policies=None,
            policy_store=policy_store,
        ), (
            "the in-flight default load republished its pre-commit empty result "
            "after the eviction — the fast path returns an unconditional ALLOW "
            "while a committed DB default policy exists"
        )
    finally:
        # The assertion above publishes this test's default policy into a
        # process-wide TTL cache; leave it warm and a later test in the same
        # worker inherits a phantom server-wide default.
        _DEFAULT_POLICY_SPECS_CACHE.clear()


# ── _SESSION_OWNER_CACHE keying ─────────────────────────────────────────────


def test_session_owner_cache_does_not_cross_workspaces(db_uri: str) -> None:
    """The same conversation id in two workspaces resolves two owners.

    The DB read underneath is workspace-scoped; only the cache in front of
    it was not, so workspace B served workspace A's owner and a per-user
    daily-cost policy seeded the wrong user's spend.

    :param db_uri: Per-test database URI (SQLite by default; the
        session worker's PostgreSQL/MySQL database when OMNIGENT_TEST_DB_URI
        is set).
    """
    from omnigent.db.db_models import workspace_scope
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    _SESSION_OWNER_CACHE.clear()
    conv_id = "3c1d5e7f9a0b2c4d6e8f0a1b2c3d4e5f"
    conv_store = SqlAlchemyConversationStore(db_uri)
    perms = SqlAlchemyPermissionStore(db_uri)

    for workspace_id, owner in ((0, "alice@example.com"), (1, "bob@example.com")):
        with workspace_scope(workspace_id):
            conv_store.create_conversation(conversation_id=conv_id)
            perms.ensure_user(owner)
            perms.grant(owner, conv_id, 4)  # LEVEL_OWNER

    try:
        with workspace_scope(0):
            assert _resolve_session_owner_cached(conv_id, conv_store) == "alice@example.com"

        with workspace_scope(1):
            assert conv_store.get_session_owner(conv_id) == "bob@example.com"
            assert _resolve_session_owner_cached(conv_id, conv_store) == "bob@example.com", (
                "workspace 1 was served workspace 0's cached owner — the owner "
                "cache key omits the workspace, so a per-user daily-cost policy "
                "seeds the wrong tenant's user and reads the wrong spend"
            )
    finally:
        # This memo is process-wide; leaving entries behind would leak this
        # test's owners into any later test that resolves the same id.
        _SESSION_OWNER_CACHE.clear()
