"""Items and child-session routes."""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    Query,
    Request,
)

from omnigent.db.utils import shared_read_scope
from omnigent.entities import PagedList
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import (
    STALE_CURSOR_RESPONSE,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import (
    _child_session_summary_from_conversation,
)
from omnigent.server.routes._sessions.orchestration import (
    _child_session_summaries_from_conversations,
    _latest_message_previews,
)
from omnigent.server.schemas import (
    ChildSessionList,
    DescendantSessionList,
    DescendantSessionSummary,
    PaginatedList,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import DescendantConversation
from omnigent.stores.permission_store import PermissionStore


def register_items_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the items routes on router."""

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
        responses={200: {"model": PaginatedList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_session_items(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """
        List items in a session with cursor-based pagination.

        Delegates to the conversation items store — session_id is
        the conversation_id. Same pagination contract as
        ``GET /v1/conversations/{id}/items``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return
            (1-1000, default 100).
        :param after: Cursor — return items after this item ID,
            e.g. ``"msg_abc123"``.
        :param before: Cursor — return items before this item ID.
        :param order: Sort order, ``"asc"`` (chronological,
            default) or ``"desc"``.
        :returns: A :class:`PaginatedList` of conversation items.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [m.to_api_dict() for m in page.data]
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/child_sessions ────────────────

    @router.get(
        "/sessions/{session_id}/child_sessions",
        response_model=None,
        responses={200: {"model": ChildSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_child_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        tool: str | None = Query(default=None),
        session_name: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List sub-agent (child) sessions under a parent session.

        Returns a page of :class:`ChildSessionSummary` objects
        derived from child conversations (``kind="sub_agent"``,
        ``parent_conversation_id=session_id``) plus each child's
        latest task. Powers the web / REPL debug surfaces' "child
        sessions" panel without parsing parent
        ``function_call_output`` JSON handles. Pagination contract
        matches :func:`list_session_items` so existing client code
        can reuse the same cursor logic.

        :param request: Inbound HTTP request; carries the caller
            identity used to authorize READ on the parent session.
        :param session_id: Parent session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of children to return
            (1-1000, default 20 — sub-agent fan-out is typically
            sparse compared to conversation items).
        :param after: Cursor — return children whose id appears
            after this one in sort order,
            e.g. ``"conv_child123"``.
        :param before: Cursor — return children before this one.
        :param order: Sort direction, ``"desc"`` (newest-first,
            default) or ``"asc"``. Sort column is ``created_at``.
        :param tool: When set, only return children whose title
            starts with this agent type (the segment before the
            ``":"``). Combined with ``session_name`` to form the
            exact title ``"{tool}:{session_name}"`` for server-side
            filtering.
        :param session_name: When set alongside ``tool``, only
            return children whose title matches
            ``"{tool}:{session_name}"`` exactly.
        :returns: A :class:`PaginatedList` of
            :class:`ChildSessionSummary` objects.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        # Require READ on the parent before listing its children (no cross-user enumeration).
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        title_filter: str | None = None
        if tool and session_name:
            title_filter = f"{tool}:{session_name}"
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order=order,
            sort_by="created_at",
            title=title_filter,
        )
        data = await _child_session_summaries_from_conversations(
            page.data,
            session_id,
            conversation_store,
        )
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/descendants ───────────────────

    @router.get(
        "/sessions/{session_id}/descendants",
        response_model=None,
        responses={200: {"model": DescendantSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_descendant_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
        max_depth: int | None = Query(default=None, ge=1),
        include_previews: bool = Query(default=True),
    ) -> PaginatedList:
        """
        List every sub-agent beneath a session, at any depth.

        One call returns the whole subtree, so clients need not walk
        ``child_sessions`` once per node. The subtree is walked in the
        database, so cost follows the subtree and page, not the whole spawn
        tree. Rows are ordered breadth-first (by depth, then creation time),
        so every parent precedes its children. Archived sessions are
        traversed even when excluded, so a live session under an archived
        one is still returned.

        :param request: Inbound HTTP request; carries the caller identity
            used to authorize READ on ``session_id``.
        :param session_id: Subtree root, e.g. ``"conv_abc123"``. May be
            mid-tree; only its own descendants are returned.
        :param limit: Maximum rows per page (1-1000, default 100).
        :param after: Cursor — the ``last_id`` of the previous page.
        :param include_archived: Also return archived descendants.
        :param max_depth: Deepest level to return (1 = direct children).
            Unbounded when omitted.
        :param include_previews: Read ``last_message_preview`` for each row.
            ``false`` skips the message read for callers that only need
            tree shape and status, e.g. the CLI navigator's poll.
        :returns: A :class:`PaginatedList` of
            :class:`DescendantSessionSummary` objects.
        :raises OmnigentError: 403/404 if the caller lacks READ on
            ``session_id`` or it does not exist; 400 ``stale_cursor`` if
            ``after`` is no longer in the subtree.
        """
        user_id = _get_user_id(request, auth_provider)
        # Sub-agents inherit access from their parent chain, so READ here covers the subtree.
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        authorized = access.conversation

        def _load_page() -> tuple[PagedList[DescendantConversation], dict[str, str | None]]:
            # One pooled connection per engine for every read below.
            with shared_read_scope():
                root = authorized
                if root is None:
                    # Permissions off or an admin caller: nothing was read yet.
                    root = conversation_store.get_conversation(session_id)
                    if root is None:
                        raise _session_not_found()
                # Bound to the authorized row's tree: a session recreated
                # elsewhere after the check lists nothing.
                page = conversation_store.list_descendant_conversations(
                    session_id,
                    root_conversation_id=root.root_conversation_id,
                    limit=limit,
                    after=after,
                    include_archived=include_archived,
                    max_depth=max_depth,
                )
                convs = [row.conversation for row in page.data]
                previews = (
                    _latest_message_previews(convs, conversation_store) if include_previews else {}
                )
                return page, previews

        page, previews = await asyncio.to_thread(_load_page)
        data: list[DescendantSessionSummary] = []
        for row in page.data:
            conv = row.conversation
            summary = _child_session_summary_from_conversation(
                conv,
                # Every descendant was reached through its parent link.
                conv.parent_conversation_id or session_id,
                previews.get(conv.id),
            ).model_dump(exclude={"object"})
            if not include_previews:
                # The builder can derive a preview from labels; the opt-out omits it.
                summary["last_message_preview"] = None
            data.append(
                DescendantSessionSummary(**summary, depth=row.depth, archived=conv.archived)
            )
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )
