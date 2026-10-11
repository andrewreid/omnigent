"""Shared test helpers for ``tests/repl/``.

Utilities used by more than one command-test module live here so they
don't get copy-pasted into every file.
"""

from __future__ import annotations

from typing import Any

from omnigent_client._sessions import SessionsNamespace
from rich.console import Console


class CapturingHost:
    """
    Minimal host stub that records Rich renderables as plain text.

    Used by slash-command tests that need to assert on rendered output
    without spinning up a real terminal. Each ``output()`` call is
    flushed through a recording :class:`rich.console.Console` and
    appended to ``lines``; ``text`` joins them for easy substring checks.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.console = Console(record=True, width=120)

    def output(self, renderable: object, *, soft_wrap: bool = False) -> None:
        """
        Record a renderable as plain text.

        :param renderable: Any Rich-renderable object.
        :param soft_wrap: Ignored — present to match the real
            :meth:`TerminalHost.output` signature.
        """
        self.console.print(renderable)
        self.lines.append(self.console.export_text(clear=True))

    @property
    def text(self) -> str:
        """
        All captured output joined into a single string.

        :returns: Concatenated plain-text output from every
            ``output()`` call so far.
        """
        return "".join(self.lines)


class FakeSubagentSessions:
    """
    ``client.sessions`` stand-in serving a sub-agent tree from a parent→children map.

    ``descendants`` mimics the server's subtree route (breadth-first, with
    ``parent_session_id`` and ``depth``), and ``child_sessions_tree`` runs the
    real SDK implementation on top of it, so the REPL→SDK path is exercised.
    ``calls`` records every subtree request.
    """

    def __init__(self, by_parent: dict[str, list[dict[str, Any]]]) -> None:
        self._by_parent = by_parent
        self._descendants_route_missing_until = 0.0
        self.calls: list[str] = []
        self.include_previews: list[bool] = []

    async def descendants(
        self,
        session_id: str,
        *,
        include_archived: bool = False,
        max_depth: int | None = None,
        limit: int = 100,
        include_previews: bool = True,
    ) -> list[dict[str, Any]]:
        self.calls.append(session_id)
        self.include_previews.append(include_previews)
        rows: list[dict[str, Any]] = []
        seen = {session_id}
        frontier = [session_id]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            depth += 1
            next_frontier: list[str] = []
            for parent_id in frontier:
                for child in self._by_parent.get(parent_id, []):
                    if child["id"] in seen:
                        continue
                    seen.add(child["id"])
                    rows.append({**child, "parent_session_id": parent_id, "depth": depth})
                    next_frontier.append(child["id"])
            frontier = next_frontier
        return rows

    async def child_sessions_tree(
        self,
        session_id: str,
        *,
        max_depth: int = 3,
        limit: int = 100,
        include_previews: bool = True,
    ) -> list[dict[str, Any]]:
        return await SessionsNamespace.child_sessions_tree(
            self,  # type: ignore[arg-type]
            session_id,
            max_depth=max_depth,
            limit=limit,
            include_previews=include_previews,
        )
