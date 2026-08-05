"""
``build_policy_engine`` — construct a :class:`PolicyEngine` for a
workflow.

Called at the top of ``_run_agent_loop``. Seeds any
``LabelDef.initial`` values that are not already present in
``conversation_labels`` using an
``INSERT ... ON CONFLICT DO NOTHING`` semantic so that two
concurrent workflows on the same conversation (the v2 case
tracked in POLICIES.md Open Q #6) never clobber each other's
view of a label's first value.

Phase 2 scope: zero-policy and declared-policy paths both work;
concrete Policy subclasses land in Phases 3+, and this builder
will start instantiating them as those phases ship.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import cachetools

from omnigent.entities import Conversation
from omnigent.entities import Policy as StoredPolicy
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.context_window import fetch_model_pricing
from omnigent.policies.base import Policy
from omnigent.policies.function import resolve_function_policy
from omnigent.policies.schema import (
    SESSION_COST_ASK_APPROVED_STATE_KEY,
    SESSION_COST_UNPRICED_APPROVED_KEY,
)
from omnigent.policies.types import PolicyLLMClient
from omnigent.runtime.credentials.databricks import resolve_databricks_workspace
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    LabelDef,
    LLMConfig,
    Phase,
    PolicySpec,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.policy_store import PolicyStore

_logger = logging.getLogger(__name__)

# Dotted path of the per-user daily cost-budget factory. The engine is
# seeded with the session owner's daily-cost rollup ONLY when a policy
# set includes this handler — otherwise the owner + daily-cost lookups
# are skipped entirely, so sessions/deployments that don't use it pay
# nothing extra per evaluation.
_USER_DAILY_COST_POLICY_PATH = "omnigent.policies.builtins.cost.user_daily_cost_budget"

# Dotted path of the per-subagent cost-budget factory. The engine is
# seeded with the subtree-scoped usage ONLY when a policy set includes
# this handler — otherwise the subtree usage lookup is skipped.
_SUBAGENT_COST_POLICY_PATH = "omnigent.policies.builtins.cost.subagent_cost_budget"

# Hardcoded policy that always ASKs before sys_add_policy executes.
# Injected unconditionally into every engine so agents cannot add
# policies without user approval.
_ASK_ON_ADD_POLICY_SPEC = FunctionPolicySpec(
    name="__ask_on_add_policy",
    on=None,
    function=FunctionRef(
        path="omnigent.policies.builtins.safety.ask_on_add_policy",
        arguments=None,
    ),
)

# Bounded cache of ``(workspace_id, conversation_id) -> session owner``. The
# owner (``LEVEL_OWNER`` grantee) is immutable for a session's lifetime, so
# caching it avoids a ``session_permissions`` lookup on every
# per-tool-call engine build. Keyed by workspace to match the
# workspace-scoped store read. Only non-``None`` owners are cached (a
# session is granted its owner atomically at creation, so ``None`` is a
# transient single-user/pre-grant state, not worth caching).
_SESSION_OWNER_CACHE: cachetools.LRUCache[tuple[int, str], str] = cachetools.LRUCache(maxsize=4096)

# TTL cache of ``workspace_id -> list[PolicySpec]`` for DB-stored default
# policies. Default policies are admin-managed and change infrequently, so
# a short TTL (30 s) avoids one ``list_defaults()`` DB query per tool-call
# evaluation while still propagating changes within half a minute.
_DEFAULT_POLICY_SPECS_CACHE: cachetools.TTLCache[int, list[PolicySpec]] = cachetools.TTLCache(
    maxsize=256, ttl=30
)

# Guards publication into _DEFAULT_POLICY_SPECS_CACHE against the same
# fill-after-invalidate race as the session cache below. Counted separately
# from _SESSION_POLICY_CACHE_GENERATION because the two caches are evicted by
# disjoint events: sharing one counter would make a session-policy mutation
# suppress an unrelated in-flight default publish on the hottest path.
_DEFAULT_POLICY_CACHE_LOCK = threading.Lock()
_DEFAULT_POLICY_CACHE_GENERATION = 0

# Invalidation-based LRU cache of ``(workspace_id, conversation_id) -> list[PolicySpec]``
# for session-scoped policies. Unlike defaults, session policies can be added
# mid-session (via sys_add_policy), so a TTL would delay enforcement. Instead,
# the cache is explicitly invalidated whenever a session policy is mutated via
# the CRUD routes. Keyed by workspace to prevent cross-tenant leakage.
# Bounded (LRU, 4096 entries) to match _SESSION_OWNER_CACHE and prevent unbounded
# growth — LRU eviction handles sessions that end without any policy mutation.
_SESSION_POLICY_SPECS_CACHE: cachetools.LRUCache[tuple[int, str], list[PolicySpec]] = (
    cachetools.LRUCache(maxsize=4096)
)

# Guards publication into _SESSION_POLICY_SPECS_CACHE against the
# fill-after-invalidate race: a loader that read the store before a mutation
# committed must not install its result after that mutation's eviction ran.
# The counter advances on every invalidation (LRU and test-only clears are
# not mutations, so they do not need to block a publish); a loader publishes
# only if it has not moved. See _load_session_policy_specs_batch.
_SESSION_POLICY_CACHE_LOCK = threading.Lock()
_SESSION_POLICY_CACHE_GENERATION = 0


def canonical_conversation_id(conversation_id: str) -> str:
    """
    Return the canonical bare-hex form of a caller-supplied conversation id.

    Ids reach the builder in spellings that do not agree. Values read back
    through the ``Uuid16`` type decorator are bare hex, but route path
    parameters carry whatever the client sent — dashed and legacy-prefixed
    uuids are both accepted upstream — and an :class:`~omnigent.entities.Conversation`
    assembled outside the store carries unvalidated ``str`` fields, since
    the dataclass constrains none of its three id attributes. Comparing or
    caching two spellings of one id makes an accepted URL either fail
    outright or observe a stale entry.

    Applied per entry point, not module-wide: each public function that
    compares a caller-supplied id against DB-shaped values is responsible
    for canonicalising its own (or, like :func:`load_session_usage`, for
    using the id off the row it just fetched). There is no ambient
    guarantee here for a function that forgets.

    :param conversation_id: Any accepted spelling of a conversation id.
    :returns: The bare 32-char hex form, or the input verbatim if it is not
        a uuid at all (preserving "unknown id = not found").
    """
    from omnigent.db.db_models import normalize_uuid

    return normalize_uuid(conversation_id) or conversation_id


def _canonicalize_snapshot_ids(
    conversation: Conversation,
    canonical_id: str,
) -> Conversation:
    """
    Return *conversation* with its own id and lineage ids in bare-hex form.

    :class:`Conversation` is a plain dataclass whose ``id``,
    ``root_conversation_id`` and ``parent_conversation_id`` are unvalidated
    ``str`` fields. A snapshot the caller assembled by hand can therefore
    carry any accepted spelling, while the builder matches all three against
    tree rows read back through ``Uuid16`` as bare hex. Normalising here keeps
    mismatch out of every downstream comparison rather than repeating the
    call at each of them.

    A copy is returned; the caller's object is not mutated.

    :param conversation: The caller-supplied snapshot.
    :param canonical_id: The already-canonicalised ``conversation_id``,
        verified equal to ``conversation.id``.
    :returns: A copy with all three ids canonical.
    """
    import dataclasses

    parent_id = conversation.parent_conversation_id
    return dataclasses.replace(
        conversation,
        id=canonical_id,
        root_conversation_id=canonical_conversation_id(conversation.root_conversation_id),
        parent_conversation_id=(
            canonical_conversation_id(parent_id) if parent_id is not None else None
        ),
    )


def _usage_tree_with_snapshot_authority(
    *,
    tree: list[Conversation],
    conversation_id: str,
    supplied_snapshot: Conversation | None,
) -> list[Conversation]:
    """
    Return the spawn tree with the target row replaced by the caller's snapshot.

    Usage aggregation obeys the same snapshot-authority rule as labels,
    ``session_state`` and ``model_override``: when the caller explicitly
    supplied a snapshot, ``conversation_id``'s own contribution is frozen
    to it rather than taken from the tree's fresh re-read of that row.
    Every other node (root, siblings, descendants) is unavoidably a fresh
    read, because the caller supplied no snapshot for those.

    Inclusion is decided from the snapshot's own ``archived`` flag, never
    from whether ``conversation_id`` is still present in the freshly-loaded
    tree — that would be the live DB re-check the "no freshness check"
    contract forbids. A snapshot captured while active counts even if the
    DB has since archived or deleted the row; one captured while archived
    stays excluded even if the DB has since unarchived it. So the
    tree-fetched row is dropped first (stale relative to the snapshot
    either way) and the snapshot re-added only when it says active.

    *supplied_snapshot* must be ``None`` unless the caller passed a
    conversation. When the builder fetched the row itself, substituting it
    here would disturb the pre-existing behaviour that excludes an archived
    conversation from its own usage sum on that path.

    :param tree: Freshly-loaded active spawn-tree rows.
    :param conversation_id: Canonical id of the conversation being built.
    :param supplied_snapshot: The caller's snapshot with canonical ids, or
        ``None`` when the caller supplied none.
    :returns: The row list to aggregate usage over.
    """
    if supplied_snapshot is None:
        return tree
    without_target = [c for c in tree if c.id != conversation_id]
    if supplied_snapshot.archived:
        return without_target
    # The snapshot must carry canonical ids: the subtree sums match on
    # ``.id``, so another spelling drops the target's own spend.
    return [*without_target, supplied_snapshot]


def _session_policy_cache_key(conversation_id: str) -> tuple[int, str]:
    """
    Build the session-policy cache key for *conversation_id*.

    The sole constructor of keys for ``_SESSION_POLICY_SPECS_CACHE``. Reads
    and invalidations must agree on spelling or a mutation silently leaves a
    stale entry behind, so canonicalisation lives here rather than at each
    call site.

    :param conversation_id: Any accepted spelling of a conversation id.
    :returns: The ``(workspace_id, canonical_id)`` cache key.
    """
    from omnigent.db.db_models import current_workspace_id

    return (current_workspace_id(), canonical_conversation_id(conversation_id))


def _needs_user_daily_cost(specs: list[PolicySpec]) -> bool:
    """
    Return whether any policy in *specs* is the per-user daily cost-budget.

    Drives the conditional injection: only when this returns ``True``
    does :func:`build_policy_engine` resolve the owner and read the
    daily-cost rollup.

    :param specs: The merged policy specs for the engine.
    :returns: ``True`` when a :class:`FunctionPolicySpec` references the
        ``user_daily_cost_budget`` factory.
    """
    return any(
        isinstance(s, FunctionPolicySpec)
        and s.function is not None
        and s.function.path == _USER_DAILY_COST_POLICY_PATH
        for s in specs
    )


def _needs_subtree_usage(specs: list[PolicySpec]) -> bool:
    """
    Return whether any policy in *specs* is the per-subagent cost-budget.

    Drives the conditional injection: only when this returns ``True``
    does :func:`build_policy_engine` compute the subtree usage seed.

    :param specs: The merged policy specs for the engine.
    :returns: ``True`` when a :class:`FunctionPolicySpec` references the
        ``subagent_cost_budget`` factory.
    """
    return any(
        isinstance(s, FunctionPolicySpec)
        and s.function is not None
        and s.function.path == _SUBAGENT_COST_POLICY_PATH
        for s in specs
    )


def _normalize_usage_for_engine(usage: dict[str, float]) -> dict[str, float]:
    """
    Normalize a usage dict for injection into the policy engine.

    Removes display-only fields (``by_model``) and converts the
    enforcement-cost field (``policy_cost_usd``) to the engine's
    canonical ``total_cost_usd`` key. Both operations are idempotent:
    if a field is absent, the operation is a no-op.

    :param usage: The usage dict to normalize (modified in-place).
    :returns: The normalized dict (same object, for chaining).
    """
    usage.pop("by_model", None)
    policy_cost = usage.pop("policy_cost_usd", None)
    if policy_cost is not None:
        usage["total_cost_usd"] = policy_cost
    return usage


def _subtree_usage_seed(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, float]:
    """
    SUBTREE-scoped usage seed for the per-subagent cost budget.

    Unlike :func:`_policy_usage_seed` (which seeds from the whole session
    tree via ``root_conversation_id``), this seeds from ``conversation_id``
    itself — so the budget gates on this conversation's own subtree cost
    (itself + its descendants), not the whole session.

    :param conversation_id: Conversation to seed the subtree usage for,
        e.g. ``"conv_child"``.
    :param conversation_store: Store to read the subtree usage from.
    :returns: Subtree usage seed dict; when an enforcement cost exists its
        ``total_cost_usd`` is the enforcement total.
    """
    usage = load_session_usage(conversation_id, conversation_store)
    return _normalize_usage_for_engine(usage)


def _resolve_session_owner_cached(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> str | None:
    """
    Resolve a session's owner, caching the immutable result.

    :param conversation_id: The session, e.g. ``"conv_abc123"``.
    :param conversation_store: Store for the owner lookup.
    :returns: The owner user id, or ``None`` when the session has no
        owner grant (single-user mode).
    """
    from omnigent.db.db_models import current_workspace_id

    # The store read below is workspace-scoped, so the cache in front of it
    # must be too: without the workspace, the same id in a second tenant
    # serves the first tenant's owner and seeds the wrong user's spend.
    key = (current_workspace_id(), canonical_conversation_id(conversation_id))
    owner: str | None = _SESSION_OWNER_CACHE.get(key)
    if owner is not None:
        return owner
    owner = conversation_store.get_session_owner(conversation_id)
    if owner is not None:
        _SESSION_OWNER_CACHE[key] = owner
    return owner


def _load_user_daily_cost(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, float | str]:
    """
    Read the session owner's per-UTC-day cost rollup as the engine seed.

    Resolves the owner (cached) and reads ``{cost_usd, ask_approved_usd}``
    for today (UTC), tagged with the owner's ``user_id`` so the budget
    policy can name whose spend tripped the gate. When the session has no
    owner grant (single-user mode), returns zeros (and no ``user_id``) so
    the per-user daily budget never trips — consistent with the write
    path, which also no-ops without an owner.

    :param conversation_id: The session, e.g. ``"conv_abc123"``.
    :param conversation_store: Store for the owner + daily-cost lookups.
    :returns: ``{"cost_usd": <float>, "ask_approved_usd": <float>,
        "user_id": <owner>}``; ``user_id`` omitted in single-user mode.
    """
    from omnigent.db.utils import now_epoch, utc_day

    owner = _resolve_session_owner_cached(conversation_id, conversation_store)
    if owner is None:
        return {"cost_usd": 0.0, "ask_approved_usd": 0.0}
    state: dict[str, float | str] = dict(
        conversation_store.get_daily_cost_state(owner, utc_day(now_epoch()))
    )
    state["user_id"] = owner
    return state


def any_policies_apply(
    *,
    spec: AgentSpec,
    conversation_id: str,
    root_conversation_id: str | None,
    default_policies: list[PolicySpec] | None,
    policy_store: PolicyStore | None,
    phase: Phase | None = None,
    tool_name: str | None = None,
) -> bool:
    """Return ``True`` when at least one policy would run for this evaluation.

    Cheaper than building a full :class:`PolicyEngine`: only checks whether
    the combined policy list is non-empty. Used as a fast-path guard in
    ``POST /policies/evaluate`` to skip the engine build (and the associated
    conversation-store reads for labels/state/usage) when nothing would fire.

    Reads from the same caches as :func:`build_policy_engine`, so the check
    is O(1) for warm cache hits.

    The policy set checked here must match the one
    :func:`build_policy_engine` assembles, or the fast path silently skips
    enforcement. That includes the ROOT conversation's stored policies, which
    a sub-agent inherits — so a caller that cannot supply lineage gets
    ``True`` (build the engine) rather than a fast-path ALLOW. Requiring the
    parameter only makes its *omission* loud; passing ``None`` has to be safe
    on its own, because absent lineage is indistinguishable here from a root
    that carries policies.

    :param spec: The agent's parsed spec.
    :param conversation_id: Conversation id, e.g. ``"conv_abc123"``.
    :param root_conversation_id: ``root_conversation_id`` of the conversation
        being evaluated, so an inheriting sub-agent is checked against its
        root's policies too. ``None`` means the lineage is unknown and forces
        the slow path.
    :param default_policies: Server-wide policies from ``RuntimeCaps``.
    :param policy_store: Session-scoped policy store; ``None`` means no DB
        policies are configured.
    :param phase: The evaluation phase, if known.
    :param tool_name: The tool being called (for ``PHASE_TOOL_CALL`` events).
    :returns: ``False`` only when no policy the engine would assemble — agent
        guardrails, server defaults, DB-stored defaults, or this
        conversation's or its root's stored session policies — could produce
        anything other than ALLOW/UNSPECIFIED for this evaluation.
    """
    # The engine unconditionally injects _ASK_ON_ADD_POLICY_SPEC so agents
    # cannot silently install session policies. Never fast-path sys_add_policy
    # TOOL_CALL events — they must always reach the engine for that gate.
    if phase == Phase.TOOL_CALL and tool_name == "sys_add_policy":
        return True
    if spec.guardrails and spec.guardrails.policies:
        return True
    if default_policies:
        return True
    if _load_default_policy_specs(policy_store):
        return True
    # Sub-agents inherit their root's stored policies, so a policy-free child
    # under a policy-carrying root must not be fast-pathed. Without lineage
    # the root's policies cannot be checked at all, and "no lineage" is not
    # evidence of "no inherited policy" — fail towards building the engine.
    if root_conversation_id is None:
        return True
    # Batched and cache-aware through the same LRU build_policy_engine uses,
    # so this is a cache hit on any call after the first for the session.
    session_ids = [canonical_conversation_id(conversation_id)]
    root_id = canonical_conversation_id(root_conversation_id)
    if root_id not in session_ids:
        session_ids.append(root_id)
    specs_by_session = _load_session_policy_specs_batch(session_ids, policy_store)
    return any(specs_by_session.get(sid) for sid in session_ids)


def build_policy_engine(
    *,
    spec: AgentSpec,
    conversation_id: str,
    conversation_store: ConversationStore,
    connection_override: dict[str, str] | None = None,
    default_policies: list[PolicySpec] | None = None,
    policy_store: PolicyStore | None = None,
    server_llm: LLMConfig | None = None,
    host_connection: dict[str, str] | None = None,
    conversation: Conversation | None = None,
) -> PolicyEngine:
    """
    Construct the :class:`PolicyEngine` for one workflow.

    When ``spec.guardrails`` is ``None`` (no guardrails
    declared), *default_policies* is empty, and no session
    policies are stored, returns a no-op engine with empty
    policies and labels — the four enforcement sites still
    call through, they just always ALLOW.

    When declared labels have an ``initial`` value and no row
    exists yet in ``conversation_labels``, seeds via
    ``ConversationStore.set_labels`` — but only for keys not
    already persisted, so existing label state is never
    clobbered. The hot cache is built from the freshly seeded
    snapshot.

    Policy run order: session policies (from the CRUD API)
    first, then agent spec policies, then *default_policies*
    (server-wide admin policies). This lets user-configured
    session policies short-circuit on DENY before agent or
    admin policies run, and gives admin policies the last
    word on ALLOW/ASK decisions.

    For sub-agent conversations, session policies from the
    root (top-level) conversation are inherited and prepended
    before any child-specific session policies. This ensures
    guardrails set on the parent session (e.g. via
    ``sys_add_policy``) also govern spawned sub-agents.
    Policies with the same ``name`` on both root and child
    are deduplicated (child wins).

    :param spec: The parsed agent spec.
    :param conversation_id: The conversation this workflow is
        running on, e.g. ``"conv_abc123"``.
    :param conversation_store: The store used for label reads
        and writes. Held by the engine for the life of the
        workflow.
    :param connection_override: Fallback ``{"base_url", "api_key"}``
        used by prompt policies whose spec declares no
        ``llm.connection``. Explicit policy / agent connections
        still win.
    :param default_policies: Server-wide policies appended after
        per-agent policies. Sourced from ``RuntimeCaps.default_policies``
        (parsed from the server ``--config`` YAML at startup).
        ``None`` and ``[]`` both mean no server-wide policies.
    :param policy_store: Session-scoped policy store. When
        provided, enabled policies for ``conversation_id`` are
        loaded and inserted between agent and admin policies in
        the evaluation order.
    :param server_llm: Server-level LLM configuration from
        ``RuntimeCaps.llm``. When provided, a
        :class:`~omnigent.policies.types.PolicyLLMClient` is
        constructed and injected into every function policy's
        ``event["llm_client"]``. ``None`` means no server-level
        LLM — function policies see ``None``.
    :param host_connection: Per-request ``{"base_url", "api_key"}``
        dict resolved from the caller's auth token (e.g. via
        :attr:`RuntimeCaps.policy_llm_connection_factory`). When
        provided, takes precedence over any connection derived from
        ``server_llm.connection`` / ``server_llm.profile``, so LLM
        calls are billed to the request caller rather than a static
        service credential. ``None`` falls back to the server-level
        connection.
    :param conversation: A pre-fetched :class:`Conversation` snapshot for
        ``conversation_id``. Its VALUES (labels, ``session_state``,
        ``session_usage``, ``archived``, ``model_override``) are
        authoritative and deliberately not refreshed — the caller owns
        their freshness. Its LINEAGE (``root_conversation_id`` /
        ``parent_conversation_id``) is validated, because a stale lineage
        swaps which policy, usage and approval domain applies rather than
        producing a stale value within the right one; see
        :func:`_verify_supplied_snapshot_lineage`. When ``None`` (the
        default), the builder fetches the conversation itself via
        ``conversation_store.get_conversation``.
    :returns: A :class:`PolicyEngine` ready for evaluation.
    :raises ValueError: If *conversation* is supplied and its ``id`` does
        not match *conversation_id*, or its lineage no longer matches the
        current row.
    """
    # Canonicalise at the two entry points — the id parameter and the
    # supplied snapshot's own three ids — so every comparison, cache key,
    # tree lookup and store call below is against DB-shaped bare hex. Route
    # path parameters carry whatever spelling the client sent, and a
    # snapshot's ids are plain strings the type does not constrain.
    conversation_id = canonical_conversation_id(conversation_id)
    conv: Conversation | None
    if conversation is not None:
        if canonical_conversation_id(conversation.id) != conversation_id:
            raise ValueError(
                f"conversation.id {conversation.id!r} does not match "
                f"conversation_id {conversation_id!r}"
            )
        conv = _canonicalize_snapshot_ids(conversation, conversation_id)
    else:
        conv = conversation_store.get_conversation(conversation_id)
    root_conversation_id = conv.root_conversation_id if conv is not None else conversation_id
    is_child = root_conversation_id != conversation_id

    # One spawn-tree walk feeds the lineage guard below, the session-wide
    # usage seed and (for a sub-agent) the root session_state fallback —
    # avoids the two independent paginated tree walks the pre-C1 builder
    # used to perform.
    tree = _load_tree_conversations(root_conversation_id, conversation_store)
    tree_by_id = {c.id: c for c in tree}
    # Ahead of the policy load, the label seed and every write below. The
    # tree read above is the one thing that necessarily precedes it — it is
    # what supplies the row the guard compares against — so this is not
    # "before anything reads the snapshot's lineage", only before anything
    # acts on it. That is the property that matters: a stale snapshot would
    # otherwise seed an initial label into the wrong incarnation and raise
    # afterwards.
    if conv is not None and conversation is not None:
        _verify_supplied_snapshot_lineage(
            conversation_id=conversation_id,
            snapshot_root_id=root_conversation_id,
            snapshot_parent_id=conv.parent_conversation_id,
            tree_row=tree_by_id.get(conversation_id),
            conversation_store=conversation_store,
        )

    guardrails = spec.guardrails
    agent_policy_specs: list[PolicySpec] = list(guardrails.policies or []) if guardrails else []
    # Session policies are per-conversation, but sub-agents must inherit
    # the root conversation's policies so that guardrails set on the
    # top-level session (e.g. via sys_add_policy) also govern spawned
    # children. Batch both the child's and (for a sub-agent) the root's
    # stored policies into one query, then prepend root policies (root
    # policies run first, then any child-specific overrides, matching the
    # cost-budget root-seeding pattern below).
    session_ids = [root_conversation_id, conversation_id] if is_child else [conversation_id]
    specs_by_session = _load_session_policy_specs_batch(session_ids, policy_store)
    session_policy_specs = list(specs_by_session.get(conversation_id, []))
    if is_child:
        root_policy_specs = list(specs_by_session.get(root_conversation_id, []))
        # Deduplicate: skip root policies already present on the child
        # (keyed by policy name) to avoid double-evaluation.
        child_names = {p.name for p in session_policy_specs}
        root_policy_specs = [p for p in root_policy_specs if p.name not in child_names]
        session_policy_specs = root_policy_specs + session_policy_specs
    db_default_policy_specs = _load_default_policy_specs(policy_store)
    admin_policy_specs: list[PolicySpec] = db_default_policy_specs + list(default_policies or [])
    all_policy_specs = session_policy_specs + agent_policy_specs + admin_policy_specs

    # Always require user approval before sys_add_policy executes.
    # Appended unconditionally so the guard is present even when
    # no other guardrails are declared (the noop-engine path below
    # is no longer reachable since all_policy_specs is never empty).
    all_policy_specs.append(_ASK_ON_ADD_POLICY_SPEC)

    label_defs = (guardrails.labels or {}) if guardrails else {}
    snapshot_labels = dict(conv.labels) if conv is not None else {}
    initial_labels = _seed_and_load_labels(
        conversation_id=conversation_id,
        label_defs=label_defs,
        conversation_store=conversation_store,
        snapshot_labels=snapshot_labels,
    )
    initial_session_state = dict(conv.session_state) if conv is not None else {}
    # Usage obeys the same snapshot-authority rule as labels/session_state/
    # model: a caller-supplied snapshot freezes its own row's contribution.
    usage_tree = _usage_tree_with_snapshot_authority(
        tree=tree,
        conversation_id=conversation_id,
        supplied_snapshot=conv if conversation is not None else None,
    )
    # The cost-budget approval is per-SESSION: the whole spawn tree shares one
    # soft-threshold gate. A sub-agent runs as its own conversation, so seed its
    # approved-checkpoint from the ROOT conversation — otherwise approving on the
    # parent wouldn't carry to the sub-agent and it would re-ask at the same
    # threshold. Other session_state stays per-conversation; the matching
    # write-back is routed to the root by PolicyEngine.apply_state_updates.
    if is_child:
        # Active root → free lookup from the tree already loaded above (the
        # tree walk is active-only, so an archived root is invisible to it —
        # archiving a root is a plain flag flip with no cascade, so a live
        # child can still have an archived root). Fall back to the
        # independent fetch only for that rare archived-root case, byte-for-
        # byte identical to the pre-C1 behavior.
        root_conv = tree_by_id.get(root_conversation_id)
        if root_conv is not None:
            root_state = dict(root_conv.session_state)
        else:
            root_state = _load_session_state(root_conversation_id, conversation_store)
        for _root_key in (
            SESSION_COST_ASK_APPROVED_STATE_KEY,
            SESSION_COST_UNPRICED_APPROVED_KEY,
        ):
            if _root_key in root_state:
                initial_session_state[_root_key] = root_state[_root_key]
    # Gating is SESSION-wide: seed from the whole spawn-tree total so a
    # sub-agent gates against the session's full spend (parent + siblings),
    # not just its own subtree. The cost read is the enforcement total
    # (in-flight sub-agent spend).
    initial_usage = _normalize_usage_for_engine(
        _aggregate_usage_from_tree(usage_tree, root_conversation_id)
    )
    # Conditional injection (#1a): only compute subtree usage when a
    # subagent_cost_budget policy is present.
    initial_subtree_usage = (
        _normalize_usage_for_engine(_aggregate_usage_from_tree(usage_tree, conversation_id))
        if _needs_subtree_usage(all_policy_specs)
        else None
    )
    # Conditional injection (#1): only pay the owner + daily-cost lookups
    # when a per-user daily cost-budget policy is actually present.
    initial_user_daily_cost = (
        _load_user_daily_cost(conversation_id, conversation_store)
        if _needs_user_daily_cost(all_policy_specs)
        else None
    )
    initial_model = (conv.model_override if conv is not None else None) or (
        spec.llm.model if spec.llm else None
    )
    # Pass the full ModelPricing so the engine can price cache-read and
    # cache-write tokens at their own rates via compute_llm_cost().
    token_pricing = fetch_model_pricing(spec.llm.model) if spec.llm else None
    server_connection = _resolve_server_llm_connection(server_llm)
    # host_connection carries the per-request caller token (billed to
    # the caller). It takes precedence over the static server-level
    # connection so policy LLM calls are attributed to the right
    # identity. Falls back to server_connection when absent.
    policy_connection = host_connection or server_connection
    llm_client = _build_policy_llm_client(server_llm, policy_connection)
    # Fall back to the server's gateway connection for prompt-policy
    # classifiers (else they default to api.openai.com).
    effective_connection_override = connection_override or server_connection
    return PolicyEngine(
        policies=[
            _instantiate_policy(
                s,
                agent_llm=spec.llm,
                connection_override=effective_connection_override,
            )
            for s in all_policy_specs
        ],
        label_defs=label_defs,
        ask_timeout=guardrails.ask_timeout if guardrails else DEFAULT_ASK_TIMEOUT,
        conversation_id=conversation_id,
        initial_labels=initial_labels,
        initial_session_state=initial_session_state,
        initial_usage=initial_usage,
        initial_subtree_usage=initial_subtree_usage,
        initial_user_daily_cost=initial_user_daily_cost,
        token_pricing=token_pricing,
        initial_model=initial_model,
        conversation_store=conversation_store,
        root_conversation_id=root_conversation_id,
        llm_client=llm_client,
    )


def _resolve_server_llm_connection(
    server_llm: LLMConfig | None,
) -> dict[str, str] | None:
    """
    Resolve the server-level LLM connection dict.

    Returns ``server_llm.connection`` directly when present;
    otherwise resolves ``server_llm.profile`` to a Databricks
    workspace connection. ``None`` when no server LLM is
    configured or it declares neither a connection nor a profile.

    :param server_llm: The server-level :class:`LLMConfig` from
        ``RuntimeCaps.llm``, or ``None``.
    :returns: A ``{"base_url", "api_key"}`` dict, or ``None``.
    :raises OSError: When ``profile`` is set but cannot be resolved.
    """
    if server_llm is None:
        return None
    if server_llm.connection is not None:
        return server_llm.connection
    if server_llm.profile is not None:
        return _resolve_databricks_connection(server_llm.profile)
    return None


def _build_policy_llm_client(
    server_llm: LLMConfig | None,
    connection: dict[str, str] | None,
) -> PolicyLLMClient | None:
    """
    Construct a :class:`PolicyLLMClient` from server-level LLM config.

    Returns ``None`` when no server-level ``llm:`` config is present.
    The :class:`~omnigent.llms.client.Client` is instantiated lazily
    here (no constructor args — auth routes per-call via
    ``connection_params``).

    :param server_llm: The server-level :class:`LLMConfig` from
        ``RuntimeCaps.llm``. ``None`` when the server config has no
        ``llm:`` block.
    :param connection: The connection dict already resolved by
        :func:`_resolve_server_llm_connection` (shared with the
        classifier connection-override fallback).
    :returns: A :class:`PolicyLLMClient` wrapping the client with
        pre-bound model/connection/timeout, or ``None``.
    """
    if server_llm is None:
        return None
    from omnigent.llms.client import Client

    primary = _normalize_policy_model(server_llm.model)
    fallbacks = [_normalize_policy_model(m) for m in server_llm.fallback_models]

    # The resolved ``connection`` (api_key / profile creds) is shared
    # across the primary and every fallback. It is provider-specific,
    # so a fallback on a different provider would be handed the wrong
    # credentials. Warn at build time rather than failing mid-request.
    if connection is not None:
        primary_provider = _model_provider(primary)
        mismatched = sorted(
            {_model_provider(m) for m in fallbacks if _model_provider(m) != primary_provider}
        )
        if mismatched:
            _logger.warning(
                "Policy llm: connection is configured for provider %r but "
                "fallback_models target %s; the shared connection likely "
                "won't authenticate those providers. Use same-provider "
                "fallbacks, or rely on environment defaults (no connection).",
                primary_provider,
                mismatched,
            )

    return PolicyLLMClient(
        _client=Client(),
        _model=primary,
        _connection=connection,
        _request_timeout=server_llm.request_timeout,
        _fallback_models=fallbacks,
    )


def _normalize_policy_model(model: str) -> str:
    """
    Apply the ``databricks-`` → ``databricks/`` provider-prefix fixup.

    Models prefixed with ``databricks-`` (e.g.
    ``databricks-claude-sonnet-4-6``) need the ``databricks/``
    provider prefix so the LLM adapter routes through
    ``DatabricksAdapter`` (Chat Completions) rather than
    ``OpenAIAdapter`` (Responses API). Without this, the request
    hits ``/responses`` on the Databricks gateway → 400. Applied
    uniformly to the primary model and every fallback so the
    fallback path routes the same way as the primary.

    :param model: A model id from the server ``llm:`` config,
        possibly a bare ``databricks-`` name.
    :returns: The model id with the ``databricks/`` prefix applied
        when needed; otherwise unchanged.
    """
    if "/" not in model and model.startswith("databricks-"):
        return f"databricks/{model}"
    return model


def _model_provider(model: str) -> str:
    """
    Extract the provider prefix from a normalized model id.

    :param model: A provider-prefixed model id, e.g.
        ``"databricks/claude-sonnet-4"`` or ``"openai/gpt-4o-mini"``.
    :returns: The provider segment before the first ``/`` (e.g.
        ``"openai"``), or the whole string when unprefixed.
    """
    return model.split("/", 1)[0] if "/" in model else model


def _resolve_databricks_connection(profile: str) -> dict[str, str]:
    """
    Resolve a Databricks CLI profile to a connection dict.

    Uses
    :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`
    to resolve the profile to workspace host + bearer token, then
    builds the ``{"base_url": ..., "api_key": ...}`` dict that the
    LLM adapter expects.

    :param profile: The Databricks CLI profile name,
        e.g. ``"my-workspace"``.
    :returns: A connection dict with ``base_url`` (workspace host
        + ``/serving-endpoints``) and ``api_key`` (bearer token),
        e.g. ``{"base_url": "https://host/serving-endpoints",
        "api_key": "dapi..."}``.
    :raises OSError: When the profile cannot be resolved.
    """
    creds = resolve_databricks_workspace(profile)
    return {
        "base_url": creds.host + "/serving-endpoints",
        "api_key": creds.token,
    }


def _instantiate_policy(
    spec: PolicySpec,
    *,
    agent_llm: LLMConfig | None,
    connection_override: dict[str, str] | None = None,
) -> Policy:
    """
    Dispatch a :class:`PolicySpec` to the matching runtime
    :class:`Policy` subclass.

    :param spec: The declarative spec.
    :param agent_llm: The agent-level ``llm:`` config. Used
        as the default backend for :class:`PromptPolicy`
        when the policy didn't declare its own ``llm:``
        override. Unused for Function policies.
    :param connection_override: Forwarded to the prompt classifier
        as a fallback when the policy / agent declare no connection.
    :returns: A :class:`Policy` subclass instance bound to
        the spec.
    :raises NotImplementedError: When ``spec`` is not a
        known :class:`PolicySpec` subclass — parser bug
        protection.
    """
    if isinstance(spec, FunctionPolicySpec):
        return resolve_function_policy(spec)
    raise NotImplementedError(
        f"Policy type {type(spec).__name__} for {spec.name!r} is not "
        f"a known subclass of PolicySpec (FunctionPolicySpec).",
    )


def _build_noop_engine(
    *,
    conversation_id: str,
    conversation_store: ConversationStore,
) -> PolicyEngine:
    """
    Build an engine for an agent with no guardrails declared.

    Kept as a named helper rather than inlined so the
    zero-policy path is grep-able ("why is every phase
    returning ALLOW?" → search for ``_build_noop_engine``).

    :param conversation_id: The conversation for the workflow.
    :param conversation_store: Writes from this engine still
        go through the store — useful if a later turn of the
        same conversation runs under an updated spec that
        does declare guardrails.
    :returns: An engine with zero policies and an empty label
        cache.
    """
    # We still read the persisted labels and session_state (if any)
    # so an engine upgrade mid-conversation sees state its
    # predecessor wrote.
    existing = _load_existing_labels(conversation_id, conversation_store)
    initial_session_state = _load_session_state(conversation_id, conversation_store)
    # Inert here (no policies read usage), but kept identical to the live
    # engine's seed so the "engine usage == session-wide total" invariant
    # holds uniformly across both builders.
    initial_usage = _policy_usage_seed(conversation_id, conversation_store)
    return PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=DEFAULT_ASK_TIMEOUT,
        conversation_id=conversation_id,
        initial_labels=existing,
        initial_session_state=initial_session_state,
        initial_usage=initial_usage,
        conversation_store=conversation_store,
    )


def _seed_and_load_labels(
    *,
    conversation_id: str,
    label_defs: dict[str, LabelDef],
    conversation_store: ConversationStore,
    snapshot_labels: dict[str, str],
) -> dict[str, str]:
    """
    Seed declared initial values and return the current snapshot.

    Race-safe across concurrent writers: seeding goes through
    ``seed_labels``, an insert-if-absent write, so a value stored
    between this caller's snapshot and the seed keeps its place.
    The snapshot filter below only avoids pointless writes; it is
    the store's ``DO NOTHING`` that decides who wins.

    :param conversation_id: The conversation to seed.
    :param label_defs: Per-key declarations from the spec.
        Keys with ``initial is None`` are skipped (those
        labels start unset until a policy writes them).
    :param conversation_store: Target for the seed insert, and for the
        re-read below when a write happens.
    :param snapshot_labels: The caller's already-resolved label snapshot
        (from the conversation passed to / fetched by
        :func:`build_policy_engine`). Used as-is when nothing needs
        seeding — no extra query. Authoritative for every key it carries,
        seeding or not.
    :returns: Full post-seed snapshot of the conversation's labels.
    """
    existing = snapshot_labels
    to_seed = {
        key: ldef.initial
        for key, ldef in label_defs.items()
        if ldef.initial is not None and key not in existing
    }
    if to_seed:
        conversation_store.seed_labels(conversation_id, to_seed)
        # Re-read only to learn which value won the seed insert, then add
        # back exactly the seeded keys. The snapshot is authoritative about
        # ABSENCE too — a key it does not carry is one the caller resolved as
        # unset, not a gap to fill from a later DB write — so nothing else
        # from the fresh read may enter.
        fresh = _load_existing_labels(conversation_id, conversation_store)
        existing = {
            **snapshot_labels,
            **{key: fresh[key] for key in to_seed if key in fresh},
        }
    return existing


def _load_existing_labels(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, str]:
    """
    Load the current persisted label state.

    Empty dict when the conversation has no labels yet (or
    when the conversation itself does not exist yet — the
    caller is responsible for ordering conversation creation
    before engine build).

    :param conversation_id: Conversation to load.
    :param conversation_store: Store to read from.
    :returns: ``{key: value}`` map. Empty when nothing
        persisted.
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    return dict(conv.labels)


def _load_session_state(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Load the current persisted session state.

    Empty dict when the conversation has no session state yet
    (or when the conversation itself does not exist yet — the
    caller is responsible for ordering conversation creation
    before engine build).

    :param conversation_id: Conversation to load,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read from.
    :returns: Session state dict. Empty when nothing persisted.
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    return dict(conv.session_state)


# Page size for walking a spawn tree when summing sub-agent usage.
# Sub-agent trees are small in practice, but we still paginate so a
# large tree is not silently truncated (see load_session_usage).
_SUBTREE_USAGE_PAGE_SIZE = 100

# Usage counters summed across a conversation subtree. Restricted to the
# known numeric keys the PolicyEngine reads so an unexpected key in one
# conversation's persisted usage can't leak into the aggregate.
_SUMMABLE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "total_cost_usd",
)


def _merge_by_model(
    aggregate: dict[str, dict[str, float]],
    per_conv: dict[str, Any],
) -> None:
    """
    Deep-merge one conversation's ``by_model`` sub-dict into the subtree aggregate.

    Unions model keys and sums each numeric per-bucket value (the token
    counters and ``total_cost_usd``) within each model, so a parent's
    per-model view folds in sub-agents that ran a different model. Mutates
    ``aggregate`` in place.

    :param aggregate: The running subtree ``by_model`` map being built, keyed
        by raw harness model id, e.g.
        ``{"claude-sonnet-4-6": {"input_tokens": 1200}}``.
    :param per_conv: One conversation's ``session_usage["by_model"]`` dict.
        Non-dict model buckets (malformed persisted data) are skipped.
    """
    for model, bucket in per_conv.items():
        if not isinstance(bucket, dict):
            continue
        agg_bucket = aggregate.setdefault(model, {})
        for key, value in bucket.items():
            # Only sum genuine numerics; ``bool`` is an ``int`` subclass so
            # exclude it explicitly to avoid summing a stray flag as 1.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                agg_bucket[key] = agg_bucket.get(key, 0.0) + value


def load_session_usage(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Load cumulative session usage for a conversation **plus all of its
    sub-agent descendants** (the subtree total).

    A cost-ask policy on a parent must see what its sub-agents spent,
    but each conversation persists only its own usage, so this sums the
    conversation and every conversation it transitively spawned (all
    share one ``root_conversation_id``). Read-only; the per-conversation
    write path is server-side ``_accumulate_session_usage`` /
    ``_persist_native_cumulative_usage``, not this function.

    Public because the server's session snapshot / ``session.usage`` SSE
    use this per-node subtree total to DISPLAY a node's own cost (a
    sub-agent's badge shows only its subtree). Cost GATING does NOT use
    this per-node view — it seeds from the whole-tree total via
    :func:`_policy_usage_seed` (which calls this with the tree root), so a
    sub-agent gates against the full session spend rather than just its own
    subtree.

    :param conversation_id: Conversation to load,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read from.
    :returns: Summed usage dict with keys ``input_tokens``,
        ``output_tokens``, ``total_tokens``, ``total_cost_usd`` (the
        DISPLAY cost sum — statusLine ``S`` for claude-native), and
        ``policy_cost_usd`` (the ENFORCEMENT cost sum — see below; only
        keys present in at least one conversation appear). When any
        conversation in the subtree recorded a per-model breakdown, a
        nested ``by_model`` key maps each raw harness model id to its
        own summed token/cost buckets (folding in sub-agents that ran a
        different model). Empty when the conversation does not exist or
        no usage is recorded. Display callers read ``total_cost_usd``;
        the policy seed (:func:`_policy_usage_seed`) reads
        ``policy_cost_usd`` (both unaffected by ``by_model``).
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    tree = _load_tree_conversations(conv.root_conversation_id, conversation_store)
    # ``conv.id`` rather than the caller's spelling: the fetch normalises
    # through the column type, but the tree is matched in memory against bare
    # hex, so a dashed or legacy-prefixed id would find the row and then match
    # nothing in the tree — an empty sum rather than an error.
    return _aggregate_usage_from_tree(tree, conv.id)


def _aggregate_usage_from_tree(
    tree: list[Conversation],
    subtree_root_id: str,
) -> dict[str, Any]:
    """
    Sum usage across a conversation subtree from an already-loaded tree.

    Pure in-memory aggregation (no store calls) — the same sums
    :func:`load_session_usage` computes, factored out so a single tree
    load can feed both the session-wide and subtree-scoped usage views
    without a second paginated walk.

    :param tree: All conversations in the spawn tree (from
        :func:`_load_tree_conversations`); order-independent.
    :param subtree_root_id: Node to walk the subtree from — the tree's
        root conversation id for a session-wide sum, or a specific
        conversation id for a subtree-scoped sum.
    :returns: Summed usage dict, same shape as :func:`load_session_usage`
        (unnormalized — callers seeding the engine still apply
        :func:`_normalize_usage_for_engine`).
    """
    subtree_ids = _subtree_conversation_ids(tree, subtree_root_id)
    totals: dict[str, Any] = {}
    # Per-model breakdown summed across the subtree, parallel to the flat sums.
    by_model_totals: dict[str, dict[str, float]] = {}
    # Enforcement cost total, accumulated alongside the display sums so the
    # policy seed can pick it without a second tree pass.
    policy_cost_total = 0.0
    any_policy_cost = False
    for tree_conv in tree:
        if tree_conv.id not in subtree_ids:
            continue
        session_usage = tree_conv.session_usage
        for key in _SUMMABLE_USAGE_KEYS:
            value = session_usage.get(key)
            if value is not None:
                totals[key] = totals.get(key, 0.0) + value
        # Per-model sub-dict (nested ``by_model`` key) is ignored by the flat
        # ``_SUMMABLE_USAGE_KEYS`` loop above; merge it separately so the flat
        # sum (used by policy gating) stays unchanged and backward-compatible.
        per_conv_by_model = session_usage.get("by_model")
        if isinstance(per_conv_by_model, dict):
            _merge_by_model(by_model_totals, per_conv_by_model)
        # Enforcement cost: prefer this conversation's ``policy_cost_usd``
        # (claude-native's real-time figure incl. in-flight sub-agent spend),
        # else its displayed ``total_cost_usd`` (codex-native / relay don't
        # post the split). Kept separate from the ``total_cost_usd`` sum
        # above so the badge keeps the authoritative statusLine total.
        per_conv_policy_cost = session_usage.get("policy_cost_usd")
        if per_conv_policy_cost is None:
            per_conv_policy_cost = session_usage.get("total_cost_usd")
        if per_conv_policy_cost is not None:
            policy_cost_total += per_conv_policy_cost
            any_policy_cost = True
    if any_policy_cost:
        totals["policy_cost_usd"] = policy_cost_total
    if by_model_totals:
        totals["by_model"] = by_model_totals
    return totals


def _policy_usage_seed(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, float]:
    """
    SESSION-WIDE usage seed for the :class:`PolicyEngine`; cost = ENFORCEMENT total.

    Cost gating caps the **session** (the whole spawn tree), so this seeds
    from the tree-wide total — the spend rooted at ``root_conversation_id``
    — not just the subtree rooted at the node being evaluated. A sub-agent
    gated on its own subtree would miss its parent's and siblings' spend, so
    the session could overshoot its budget while the orchestrator parent is
    parked (it makes no tool calls, so its own gate never fires). For the
    root conversation this equals the per-node subtree (its subtree IS the
    whole tree), so only sub-agents change behavior.

    The cost the gate reads (``total_cost_usd`` in the returned seed) is the
    ENFORCEMENT total — ``policy_cost_usd`` when present (claude-native's
    real-time figure that reflects in-flight sub-agent spend while the
    displayed statusLine ``S`` is frozen), falling back to the displayed
    ``total_cost_usd`` for harnesses that don't post the split (codex-native,
    relay). The ``policy_cost_usd`` key is then dropped so the engine's usage
    context carries only the standard counters. Display callers use
    :func:`load_session_usage` directly (per-node subtree, authoritative
    ``total_cost_usd`` = ``S``), which is why the cost-budget gate can read a
    higher in-flight / session-wide total than a node's badge shows mid-turn.

    :param conversation_id: Conversation to seed the engine for, e.g.
        ``"conv_child"`` (a sub-agent) or ``"conv_root"`` (the session root).
    :param conversation_store: Store to read the tree usage from.
    :returns: Whole-tree usage seed dict; when an enforcement cost exists its
        ``total_cost_usd`` is the enforcement total and no ``policy_cost_usd``
        key remains. Empty when the conversation is absent or no usage is
        recorded.
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    usage = load_session_usage(conv.root_conversation_id, conversation_store)
    return _normalize_usage_for_engine(usage)


def _load_tree_conversations(
    root_conversation_id: str,
    conversation_store: ConversationStore,
) -> list[Conversation]:
    """
    Page through every conversation in one spawn tree.

    Returns all conversations sharing ``root_conversation_id`` (the
    root plus every sub-agent, any ``kind``), paginating so a large
    tree is not silently truncated. The ``root_conversation_id`` column
    is indexed, so this is a bounded indexed scan per page.

    :param root_conversation_id: The tree's root conversation id (every
        conversation in a spawn tree shares it), e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read from.
    :returns: All conversations in the tree, in store order.
    """
    convs: list[Conversation] = []
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            limit=_SUBTREE_USAGE_PAGE_SIZE,
            after=after,
            # None disables the kind filter so sub_agent conversations
            # (not just "default") are included in the tree.
            kind=None,
            root_conversation_id=root_conversation_id,
        )
        convs.extend(page.data)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return convs


def _subtree_conversation_ids(
    tree: list[Conversation],
    conversation_id: str,
) -> set[str]:
    """
    Collect a conversation id plus all its transitive sub-agent
    descendants within a spawn tree.

    Walking the subtree (rather than summing the whole ``tree``) keeps
    the aggregate correct when the policy is evaluated on a mid-tree
    sub-agent: that node sees its own spend and its children's, but not
    its parent's or siblings'.

    :param tree: All conversations in the spawn tree (from
        :func:`_load_tree_conversations`); order-independent.
    :param conversation_id: The subtree root to walk from,
        e.g. ``"conv_abc123"``.
    :returns: Set of conversation ids in the subtree rooted at
        ``conversation_id`` (always includes ``conversation_id``).
    """
    children_by_parent: dict[str, list[str]] = {}
    for tree_conv in tree:
        if tree_conv.parent_conversation_id is not None:
            children_by_parent.setdefault(tree_conv.parent_conversation_id, []).append(
                tree_conv.id
            )
    subtree: set[str] = set()
    stack = [conversation_id]
    while stack:
        node = stack.pop()
        if node in subtree:
            continue
        subtree.add(node)
        stack.extend(children_by_parent.get(node, []))
    return subtree


def _load_default_policy_specs(
    policy_store: PolicyStore | None,
) -> list[PolicySpec]:
    """
    Load enabled server-wide default policies from the store.

    These are policies created via ``POST /v1/policies`` (``session_id IS
    NULL``). They run after agent-spec policies and before YAML-based
    admin policies in the evaluation order.

    Results are cached per workspace for 30 s (see
    :data:`_DEFAULT_POLICY_SPECS_CACHE`) to avoid a ``list_defaults()``
    DB round-trip on every tool-call evaluation. The cache is keyed by
    workspace id so multi-tenant deployments never share results across
    tenants. Call :func:`invalidate_default_policy_specs_cache` after any
    mutation to make changes visible before the TTL expires.

    Loads sample a generation counter that every invalidation advances and
    install nothing if an invalidation landed while the store query was in
    flight, so a load that read before a mutation committed cannot republish
    its stale result afterwards. The caller still uses the rows it read;
    only publication is guarded, so a concurrent mutation costs a cache miss
    on the next build rather than a missed policy. Without this,
    :func:`any_policies_apply` could serve a stale-empty default set — a
    fast-path ALLOW while a committed default policy exists.

    :param policy_store: The policy store. ``None`` returns an empty list.
    :returns: List of :class:`FunctionPolicySpec` for enabled default
        policies, in ``created_at ASC`` order.
    :raises OmnigentError: If an enabled policy has an unsupported type.
    """
    if policy_store is None:
        return []
    from omnigent.db.db_models import current_workspace_id

    workspace_id = current_workspace_id()
    with _DEFAULT_POLICY_CACHE_LOCK:
        generation = _DEFAULT_POLICY_CACHE_GENERATION
        cached: list[PolicySpec] | None = _DEFAULT_POLICY_SPECS_CACHE.get(workspace_id)
    if cached is not None:
        return cached
    specs: list[PolicySpec] = []
    for policy in policy_store.list_defaults():
        if not policy.enabled:
            continue
        if policy.type != "python":
            # Skip unsupported types with a warning rather than raising.
            # A session-scoped policy of unsupported type fails loudly (blast
            # radius: one session); a default policy of unsupported type would
            # crash engine construction for every session server-wide. Log and
            # skip so a stale or manually-inserted row can't cause an outage.
            _logger.warning(
                "Skipping default policy %r (id=%r): unsupported type %r — "
                "only type='python' can be evaluated. Disable or delete this "
                "policy to suppress this warning.",
                policy.name,
                policy.id,
                policy.type,
            )
            continue
        specs.append(_stored_policy_to_spec(policy))
    with _DEFAULT_POLICY_CACHE_LOCK:
        if generation == _DEFAULT_POLICY_CACHE_GENERATION:
            _DEFAULT_POLICY_SPECS_CACHE[workspace_id] = specs
    return specs


def invalidate_default_policy_specs_cache() -> None:
    """
    Evict the current workspace's entry from the default-policy specs cache.

    Call this after any mutation (create, update, delete) of a default
    policy so the next :func:`build_policy_engine` call re-reads from the
    DB rather than serving a stale TTL entry. Scoped to the current
    workspace context via :func:`~omnigent.db.db_models.current_workspace_id`.
    """
    global _DEFAULT_POLICY_CACHE_GENERATION

    from omnigent.db.db_models import current_workspace_id

    # Advance the generation under the same lock that guards publication, so
    # a loader holding pre-commit rows cannot reinstate them afterwards.
    with _DEFAULT_POLICY_CACHE_LOCK:
        _DEFAULT_POLICY_CACHE_GENERATION += 1
        _DEFAULT_POLICY_SPECS_CACHE.pop(current_workspace_id(), None)


def invalidate_session_policy_specs_cache(conversation_id: str) -> None:
    """
    Evict a conversation's entry from the session policy specs cache.

    Call this after any mutation (create, update, delete) of a session
    policy so the next :func:`build_policy_engine` call re-reads from
    the DB. Scoped to the current workspace context.

    :param conversation_id: The session whose cache entry to evict,
        e.g. ``"conv_abc123"``. Any accepted id spelling; the key is
        canonicalised so a mutation routed through a dashed or prefixed id
        still evicts the entry the builder wrote.
    """
    global _SESSION_POLICY_CACHE_GENERATION

    key = _session_policy_cache_key(conversation_id)
    # Advance the generation under the same lock that guards publication, so
    # a loader already holding stale rows cannot reinstate them afterwards.
    with _SESSION_POLICY_CACHE_LOCK:
        _SESSION_POLICY_CACHE_GENERATION += 1
        _SESSION_POLICY_SPECS_CACHE.pop(key, None)


def _verify_supplied_snapshot_lineage(
    *,
    conversation_id: str,
    snapshot_root_id: str,
    snapshot_parent_id: str | None,
    tree_row: Conversation | None,
    conversation_store: ConversationStore,
) -> None:
    """
    Reject a caller-supplied snapshot whose lineage no longer matches the row.

    Only ``id`` equality is checked at the builder entry point, and an id can
    be reused: ``delete_conversation`` frees the key and ``create_conversation``
    accepts an explicit ``conversation_id``, so the same id can come back under
    a different parent and root. The snapshot's ``root_conversation_id`` then
    selects which policies are inherited, which tree is walked, which root's
    approval state is inherited, which usage is aggregated, and where the
    engine writes cost checkpoints back to — i.e. a stale lineage silently
    swaps the whole domain the evaluation applies to.

    That is different in kind from a stale label, session_state, usage or
    model value. Those are stale values *within the correct domain* and are
    deliberately snapshot-authoritative (see the archived/usage/model tests);
    lineage is not, so it is the only thing verified here.

    Cost: the already-loaded root tree carries the target's own fresh row, so
    both root AND parent are compared for free — tree membership alone proves
    only the root, and an id can be recreated under a different parent within
    the same root. Only an *absent* target triggers a lookup, the same shape
    as the archived-root fallback nearby. Absence is genuinely ambiguous
    (archived, deleted, or reincarnated elsewhere), and archived/deleted must
    keep their existing snapshot-authoritative behavior, so absence alone
    cannot fail.

    This is a point-in-time check, not TOCTOU elimination: the row can still
    be deleted and recreated after the check and before or during the
    evaluation. Closing that fully would need a transaction or lock spanning
    the decision, or an immutable incarnation token on the row — neither
    exists today.

    :param conversation_id: The id the caller is building an engine for.
    :param snapshot_root_id: ``root_conversation_id`` taken from the snapshot.
    :param snapshot_parent_id: ``parent_conversation_id`` from the snapshot.
    :param tree_row: The freshly-loaded row for ``conversation_id`` from the
        already-walked root tree, or ``None`` when it was absent from it.
    :param conversation_store: Store used for the absence-only re-read.
    :raises ValueError: If the current row exists under a different root or
        parent than the snapshot claims.
    """
    current = tree_row
    if current is None:
        current = conversation_store.get_conversation(conversation_id)
    if current is None:
        # Deleted, with no replacement. Snapshot authority across deletion is
        # existing, tested behavior — nothing to contradict.
        return
    if (
        current.root_conversation_id == snapshot_root_id
        and current.parent_conversation_id == snapshot_parent_id
    ):
        # Same lineage; absence from the tree just means archived (the tree
        # walk is active-only). Snapshot authority applies as designed.
        return
    raise ValueError(
        f"conversation {conversation_id!r} lineage changed since the supplied "
        f"snapshot was taken: snapshot claims root "
        f"{snapshot_root_id!r} / parent {snapshot_parent_id!r}, current row has "
        f"root {current.root_conversation_id!r} / parent "
        f"{current.parent_conversation_id!r}. Re-fetch the conversation before "
        f"building a policy engine for it."
    )


def _load_session_policy_specs_batch(
    session_ids: list[str],
    policy_store: PolicyStore | None,
) -> dict[str, list[PolicySpec]]:
    """
    Load enabled session policies for several sessions, cache-aware.

    Keys the ``(workspace_id, conversation_id)`` LRU through
    :func:`_session_policy_cache_key`, so only ids missing from the cache
    reach the store and a build whose sessions are all cached issues no
    policy query at all. Mutations evict through
    :func:`invalidate_session_policy_specs_cache`, which builds its key the
    same way.

    There is no TTL — an entry is permanent until explicitly evicted, so a
    session policy change (including ``sys_add_policy``) takes effect on the
    next engine build. Because an entry is permanent, publishing a result
    that was already stale when it was read would keep it stale indefinitely
    rather than for a bounded window. Loads therefore sample a generation
    counter that every invalidation advances, and install nothing if an
    invalidation landed while the store query was in flight; the caller
    still uses the rows it read. Only publication is guarded, so a
    concurrent mutation costs a cache miss on the next build, never a
    missed policy.

    Only ``type="python"`` policies are instantiable today. An enabled
    policy of an unsupported type (e.g. ``type="url"``) raises rather than
    being skipped, so a stored guardrail that never enforces fails loudly.

    :param session_ids: Session ids to load, in caller order; duplicates are
        collapsed.
    :param policy_store: Session-scoped policy store; ``None`` means no DB
        policies are configured and every id maps to an empty list.
    :returns: Mapping of session id to its enabled :class:`PolicySpec` list,
        each in ``created_at ASC`` order.
    :raises OmnigentError: If an enabled policy has an unsupported ``type``.
    """
    unique_ids = list(dict.fromkeys(session_ids))
    if policy_store is None or not unique_ids:
        return {sid: [] for sid in unique_ids}

    specs_by_session: dict[str, list[PolicySpec]] = {}
    misses: list[str] = []
    with _SESSION_POLICY_CACHE_LOCK:
        generation = _SESSION_POLICY_CACHE_GENERATION
        for sid in unique_ids:
            cached = _SESSION_POLICY_SPECS_CACHE.get(_session_policy_cache_key(sid))
            if cached is not None:
                specs_by_session[sid] = cached
            else:
                misses.append(sid)

    if misses:
        stored_by_session = policy_store.list_for_sessions(misses)
        loaded = {sid: _specs_from_stored(stored_by_session.get(sid, [])) for sid in misses}
        specs_by_session.update(loaded)
        with _SESSION_POLICY_CACHE_LOCK:
            if generation == _SESSION_POLICY_CACHE_GENERATION:
                for sid, specs in loaded.items():
                    _SESSION_POLICY_SPECS_CACHE[_session_policy_cache_key(sid)] = specs

    return specs_by_session


def _specs_from_stored(stored: list[StoredPolicy]) -> list[PolicySpec]:
    """
    Convert enabled stored policies to :class:`FunctionPolicySpec` instances.

    Conversion step for :func:`_load_session_policy_specs_batch` (via
    ``PolicyStore.list_for_sessions``).

    :param stored: Policies for one session, in ``created_at ASC`` order.
    :returns: List of :class:`FunctionPolicySpec` for the enabled subset.
    :raises OmnigentError: If an enabled policy has an unsupported
        ``type`` (e.g. ``type="url"``).
    """
    specs: list[PolicySpec] = []
    for policy in stored:
        if not policy.enabled:
            continue
        specs.append(_stored_policy_to_spec(policy))
    return specs


def _stored_policy_to_spec(policy: StoredPolicy) -> PolicySpec:
    """
    Convert a stored :class:`Policy` entity to a
    :class:`FunctionPolicySpec`.

    For ``type="python"``, creates a :class:`FunctionPolicySpec`
    with a :class:`FunctionRef` pointing at the stored handler
    path and optional factory params. Session policies fire on
    all phases (``on=None``) — the callable itself decides
    whether to act by inspecting ``event["type"]``.

    For ``type="url"``, raises :class:`OmnigentError` (URL policy evaluation
    is unimplemented). A stored policy that never enforces is a silent
    safety hole, so converting an unsupported type fails loudly rather than
    returning ``None``.

    :param policy: The stored session policy entity.
    :returns: A :class:`FunctionPolicySpec`.
    :raises OmnigentError: If the policy ``type`` cannot be evaluated yet
        (e.g. ``type="url"``).
    """
    if policy.type == "python":
        return FunctionPolicySpec(
            name=policy.name,
            # Session policies self-select: on=None means the
            # engine skips phase filtering and always dispatches.
            on=None,
            function=FunctionRef(
                path=policy.handler,
                arguments=policy.factory_params,
            ),
        )
    # Any non-"python" type (today only "url") cannot be evaluated yet.
    # Reject loudly and fail closed: a stored policy that silently never
    # enforces is worse than a visible failure the operator can act on.
    raise OmnigentError(
        f"Session policy {policy.name!r} (id {policy.id!r}) has unsupported "
        f"type {policy.type!r}; only type='python' policies can be evaluated "
        f"today. URL policy evaluation is a future extension. Remove or "
        f"disable this policy, since storing it does not enforce anything.",
        code=ErrorCode.INVALID_INPUT,
    )


__all__ = [
    "build_policy_engine",
    "invalidate_default_policy_specs_cache",
    "invalidate_session_policy_specs_cache",
]
