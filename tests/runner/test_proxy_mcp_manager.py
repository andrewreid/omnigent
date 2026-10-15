"""Unit tests for :class:`ProxyMcpManager`.

Covers:
- ``schemas_for`` short-circuit on empty ``mcp_servers``
- ``schemas_for`` happy path: JSON-RPC response → ``McpSchemasResult``
- ``inputSchema`` normalization (None, missing-properties)
- ``schemas_for`` soft errors: HTTP 500 and RPC error body → ``failures`` dict
- ``call_tool`` happy path: text content extracted from result
- ``call_tool`` isError=True → JSON error string (not raised)
- ``call_tool`` -32000 RPC error → JSON error string (soft error, not raised)
- ``call_tool`` non-32000 RPC error → raises RuntimeError
- ``call_tool`` network failure → raises RuntimeError
- ``call_tool`` dropped request → prompt retry on a live tunnel, reconnect wait otherwise
- runner built-ins: local execution, per-phase server policy, ASK fallback,
  the no-policies hint, fail-closed checks, and exactly-once execution
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from omnigent.runner import mcp_execution_registry as mcp_execution_registry_mod
from omnigent.runner import pending_approvals, policy_proxy
from omnigent.runner.mcp_execution_registry import (
    MCP_OPERATION_ID_PARAM,
    RUNNER_MCP_EXECUTION_DETACHED_CODE,
    McpExecutionRegistry,
    McpExecutionResult,
)
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.spec.types import AgentSpec, MCPServerConfig

# ── Helpers ────────────────────────────────────────────────────────────────


@dataclass
class _Call:
    """A single captured HTTP call made through the stub transport.

    :param url: The request URL path, e.g. ``"/v1/sessions/conv_1/mcp"``.
    :param body: The parsed JSON body of the request.
    """

    url: str
    body: dict[str, Any]


class _StubTransport(httpx.AsyncBaseTransport):
    """httpx async transport backed by a list of scripted responses.

    Each call to ``handle_async_request`` pops and returns the next
    response from the queue and records the request in ``calls``.

    :param responses: Pre-scripted :class:`httpx.Response` objects, in
        the order they will be returned.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        """Create the stub transport with a list of canned responses.

        :param responses: The responses to return in order.
        """
        self._responses = list(responses)
        self.calls: list[_Call] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Return the next scripted response and record the request.

        :param request: The outgoing request.
        :returns: The next response in the queue.
        :raises IndexError: If the queue is exhausted (test setup error).
        """
        body = json.loads(request.content)
        self.calls.append(_Call(url=str(request.url), body=body))
        return self._responses.pop(0)


def _json_resp(data: dict[str, Any], status: int = 200) -> httpx.Response:
    """Build an httpx.Response with a JSON body.

    :param data: The JSON body dict.
    :param status: The HTTP status code; defaults to 200.
    :returns: A :class:`httpx.Response` with the encoded body.
    """
    return httpx.Response(
        status_code=status,
        headers={"Content-Type": "application/json"},
        content=json.dumps(data).encode(),
    )


def _make_spec(*names: str) -> AgentSpec:
    """Build an AgentSpec with one HTTP MCPServerConfig per name.

    :param names: Server names, e.g. ``"github"``, ``"jira"``.
    :returns: :class:`AgentSpec` with ``mcp_servers`` populated.
    """
    return AgentSpec(
        spec_version=1,
        name="test-agent",
        mcp_servers=[
            MCPServerConfig(name=n, transport="http", url=f"http://mcp/{n}") for n in names
        ],
    )


def _empty_spec() -> AgentSpec:
    """Build an AgentSpec with no MCP servers.

    :returns: :class:`AgentSpec` with an empty ``mcp_servers`` list.
    """
    return AgentSpec(spec_version=1, name="test-agent")


def _make_manager(transport: _StubTransport) -> ProxyMcpManager:
    """Build a ProxyMcpManager backed by the stub transport.

    :param transport: The stub transport to use for the httpx client.
    :returns: A :class:`ProxyMcpManager` bound to session ``"conv_test"``.
    """
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    return ProxyMcpManager(session_id="conv_test", ap_client=client)


def _input_required(elicitation_id: str, request_state: str, rpc_id: int) -> httpx.Response:
    """Build a policy-ASK ``InputRequiredResult`` for one elicitation."""
    return _json_resp(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "resultType": "input_required",
                "inputRequests": {
                    elicitation_id: {
                        "method": "elicitation/create",
                        "params": {
                            "message": "Approve shell command?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"approved": {"type": "boolean"}},
                                "required": ["approved"],
                            },
                        },
                    }
                },
                "requestState": request_state,
            },
        }
    )


async def _wait_until_registered(elicitation_id: str) -> None:
    """Wait for ``call_tool`` to park on the given approval."""
    for _ in range(1000):
        if elicitation_id in pending_approvals._pending:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"approval {elicitation_id} was never registered")


class _RetainedExecutionTransport(httpx.AsyncBaseTransport):
    """Server stand-in that runs each tool step through the runner registry.

    Each request consumes one scripted outcome: ``"input_required"`` answers
    with a policy ASK; ``"refuse"`` fails to connect before reaching the
    server; ``"detached"``, ``"drop"`` and ``"ok"`` first execute (or reattach
    to) the operation, then reply with a detached RPC error, a dropped
    connection, or the tool result.
    """

    def __init__(self, registry: McpExecutionRegistry, outcomes: list[str]) -> None:
        self.calls: list[_Call] = []
        self.external_invocations = 0
        self._registry = registry
        self._outcomes = list(outcomes)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(_Call(url=str(request.url), body=body))
        outcome = self._outcomes.pop(0)
        if outcome == "input_required":
            return _input_required("elicit_live", "live-state", body["id"])
        if outcome == "refuse":
            raise httpx.ConnectError("connection refused")

        async def _retained_work() -> McpExecutionResult:
            self.external_invocations += 1
            return McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

        await self._registry.execute(
            session_id="conv_test",
            operation_id=body["params"][MCP_OPERATION_ID_PARAM],
            step="initial",
            params={"name": body["params"]["name"], "arguments": {}},
            run=_retained_work,
        )
        if outcome == "drop":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        if outcome == "detached":
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
                        "message": "Runner MCP execution detached.",
                    },
                }
            )
        return _json_resp(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": "completed"}], "isError": False},
            }
        )


# ── schemas_for ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schemas_for_empty_spec_returns_empty_without_network() -> None:
    """``schemas_for`` must return empty result without HTTP call when spec has no MCP servers.

    Failure means the proxy hits the network (or crashes) on specs that
    declare no MCP servers — agents without MCP tools should never trigger
    MCP proxy requests.
    """
    transport = _StubTransport([])  # no responses queued — would raise if called
    manager = _make_manager(transport)

    result = await manager.schemas_for(_empty_spec())

    assert result == McpSchemasResult(schemas=[], tool_names=set(), failures={}), (
        "Empty spec must return empty McpSchemasResult without calling the proxy"
    )
    assert transport.calls == [], "No HTTP request should be sent when mcp_servers is empty"


@pytest.mark.asyncio
async def test_schemas_for_happy_path_parses_tools() -> None:
    """``schemas_for`` must parse a JSON-RPC tools/list response into McpSchemasResult.

    Failure means ProxyMcpManager's response parsing is broken — the harness
    would see no tools and never dispatch any MCP tool calls.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "github__search",
                        "description": "Search GitHub",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "github__create_issue",
                        "description": "Create an issue",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                        },
                    },
                ]
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.tool_names == {"github__search", "github__create_issue"}, (
        "Both tool names must appear in tool_names set"
    )
    assert result.failures == {}, "No failures expected on a clean response"
    assert len(result.schemas) == 2, "One schema per tool must be returned"

    search_schema = next(s for s in result.schemas if s["name"] == "github__search")
    assert search_schema["type"] == "function"
    assert search_schema["description"] == "Search GitHub"
    assert search_schema["parameters"]["properties"]["query"] == {"type": "string"}, (
        "inputSchema.properties must be forwarded as parameters.properties"
    )

    # Verify the request body was well-formed JSON-RPC 2.0
    assert len(transport.calls) == 1, "Exactly one HTTP call should be made"
    call = transport.calls[0]
    assert call.body["method"] == "tools/list"
    assert call.body["jsonrpc"] == "2.0"
    assert "/v1/sessions/conv_test/mcp" in call.url


@pytest.mark.asyncio
async def test_schemas_for_normalizes_null_input_schema() -> None:
    """A tool with ``inputSchema: null`` must normalize to ``{type: object, properties: {}}``.

    Failure means tools without an inputSchema crash the LLM provider
    call with a missing-properties validation error.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [{"name": "gh__ping", "description": "Ping", "inputSchema": None}]
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("gh"))

    assert result.schemas[0]["parameters"] == {"type": "object", "properties": {}}, (
        "null inputSchema must normalize to object with empty properties"
    )


@pytest.mark.asyncio
async def test_schemas_for_injects_empty_properties_when_missing() -> None:
    """An object inputSchema without ``properties`` must get ``properties: {}`` injected.

    Failure means some LLM providers reject the schema (required key missing)
    for tools that accept no parameters.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "gh__noop",
                        "description": "No-op",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("gh"))

    params = result.schemas[0]["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}, "Missing properties key must be injected as empty dict"


@pytest.mark.asyncio
async def test_schemas_for_http_error_returns_failure() -> None:
    """An HTTP 500 from the proxy must surface as a failure, not raise.

    Failure means an Omnigent server error crashes the harness instead of surfacing
    as a graceful tool-unavailable message to the LLM.
    """
    transport = _StubTransport([httpx.Response(status_code=500)])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.schemas == [], "No schemas on proxy error"
    assert result.tool_names == set(), "No tool names on proxy error"
    assert "proxy" in result.failures, "Error must surface in failures['proxy']"


@pytest.mark.asyncio
async def test_schemas_for_rpc_error_body_returns_failure() -> None:
    """A JSON-RPC error body from the proxy must surface as failures, not raise.

    Failure means RPC protocol errors (e.g. from an MCP pool miss) crash
    the harness instead of returning a graceful empty-tools result.
    """
    rpc_error = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
    )
    transport = _StubTransport([rpc_error])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.schemas == []
    assert "proxy" in result.failures
    assert "-32601" in result.failures["proxy"], (
        "Error code must appear in the failure message for diagnostics"
    )
    assert "Method not found" in result.failures["proxy"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rpc_body", "expected_message"),
    [
        ({"jsonrpc": "2.0", "id": 1, "error": []}, "non-object RPC error"),
        ({"jsonrpc": "2.0", "id": 1, "result": []}, "non-object tools/list result"),
    ],
)
async def test_schemas_for_malformed_rpc_objects_return_failure(
    rpc_body: dict[str, Any],
    expected_message: str,
) -> None:
    """Malformed JSON-RPC objects surface as schema failures rather than empty success."""
    transport = _StubTransport([_json_resp(rpc_body)])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.schemas == []
    assert result.tool_names == set()
    assert expected_message in result.failures["proxy"]


# ── call_tool ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_happy_path_returns_text() -> None:
    """``call_tool`` must extract and return text content from a successful result.

    Failure means MCP tool results are dropped and the LLM sees empty responses,
    breaking the agent's ability to act on tool output.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "Found 3 results"},
                    {"type": "text", "text": "Page 1 of 1"},
                ],
                "isError": False,
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)
    spec = _make_spec("github")

    output = await manager.call_tool(spec, "github__search", {"query": "asyncio"})

    assert output == "Found 3 results\nPage 1 of 1", (
        "Multiple text blocks must be joined with newline"
    )
    # Verify the request was well-formed
    call = transport.calls[0]
    assert call.body["method"] == "tools/call"
    assert call.body["params"]["name"] == "github__search"
    assert call.body["params"]["arguments"] == {"query": "asyncio"}


@pytest.mark.asyncio
async def test_call_tool_recreates_approval_after_server_reconnect() -> None:
    """A reconnect discards stale requestState and repeats the original call."""
    old_elicitation = "elicit_old_server"
    new_elicitation = "elicit_new_server"
    transport = _StubTransport(
        [
            _input_required(old_elicitation, "old-state", 1),
            _input_required(new_elicitation, "new-state", 2),
            _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "content": [{"type": "text", "text": "approval-resumed"}],
                        "isError": False,
                    },
                }
            ),
        ]
    )
    manager = _make_manager(transport)
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(
        manager.call_tool(_make_spec("github"), "sys_os_shell", {"command": "printf ok"})
    )
    try:
        await _wait_until_registered(old_elicitation)
        assert pending_approvals.notify_server_reconnect() == 1
        await _wait_until_registered(new_elicitation)
        assert pending_approvals.resolve(new_elicitation, approved=True)

        assert await task == "approval-resumed"
    finally:
        if not task.done():
            task.cancel()
        pending_approvals.reset_for_tests()

    assert [call.body["id"] for call in transport.calls] == [1, 2, 3]
    first_params, replay_params, retry_params = [call.body["params"] for call in transport.calls]
    assert replay_params == first_params
    assert "requestState" not in replay_params
    assert retry_params["requestState"] == "new-state"
    assert new_elicitation in retry_params["inputResponses"]
    assert old_elicitation not in retry_params["inputResponses"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["detached", "drop"])
async def test_call_tool_reattaches_expired_execution_after_server_disconnect(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A reconnect wait pins completed work past its normal retention window.

    Covers both the server reporting a detached runner request and the POST
    itself dropping while the tunnel is down.
    """
    clock = 0.0
    monkeypatch.setattr(mcp_execution_registry_mod, "_COMPLETED_TTL_S", 1.0)
    monkeypatch.setattr(mcp_execution_registry_mod, "monotonic", lambda: clock)
    registry = McpExecutionRegistry()
    transport = _RetainedExecutionTransport(registry, [failure, "ok"])
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
        server_connected=lambda: False,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(manager.call_tool(None, "github__deploy", {}))
    try:
        for _ in range(1000):
            if pending_approvals._server_reconnect_waiters:
                break
            await asyncio.sleep(0.001)
        assert pending_approvals._server_reconnect_waiters
        clock = 2.0
        pending_approvals.notify_server_reconnect()

        assert await task == "completed"
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.external_invocations == 1
    assert [call.body["id"] for call in transport.calls] == [1, 2]
    first_params, retry_params = [call.body["params"] for call in transport.calls]
    assert retry_params == first_params
    assert first_params[MCP_OPERATION_ID_PARAM].startswith("mcpop_")


@pytest.mark.asyncio
async def test_call_tool_retries_dropped_request_promptly_on_live_tunnel() -> None:
    """A dropped POST on a live tunnel reattaches within seconds, not after 120 s."""
    registry = McpExecutionRegistry()
    transport = _RetainedExecutionTransport(registry, ["drop", "ok"])
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
        server_connected=lambda: True,
    )
    pending_approvals.reset_for_tests()
    try:
        result = await asyncio.wait_for(
            manager.call_tool(None, "sys_os_read", {}),
            timeout=5.0,
        )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert result == "completed"
    assert transport.external_invocations == 1
    assert [call.body["id"] for call in transport.calls] == [1, 2]
    first_params, retry_params = [call.body["params"] for call in transport.calls]
    assert retry_params == first_params


@pytest.mark.asyncio
async def test_call_tool_live_tunnel_retry_keeps_parked_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked approval outlasts the retry budget; its unsent resume POST retries."""
    monkeypatch.setattr("omnigent.runner.proxy_mcp_manager._LIVE_TUNNEL_RETRY_DELAYS_S", (0.01,))
    registry = McpExecutionRegistry()
    transport = _RetainedExecutionTransport(registry, ["input_required", "refuse", "ok"])
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
        server_connected=lambda: True,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(manager.call_tool(None, "sys_os_shell", {}))
    try:
        await _wait_until_registered("elicit_live")
        await asyncio.sleep(0.1)
        assert not task.done()
        assert pending_approvals.resolve("elicit_live", approved=True)

        assert await asyncio.wait_for(task, timeout=5.0) == "completed"
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.external_invocations == 1
    assert [call.body["id"] for call in transport.calls] == [1, 2, 3]
    _ask_params, approved_params, retry_params = [call.body["params"] for call in transport.calls]
    assert approved_params["requestState"] == "live-state"
    assert retry_params == approved_params


@pytest.mark.asyncio
async def test_call_tool_does_not_resend_approval_dropped_after_send() -> None:
    """The server consumes an approval before executing, so a sent resume is not replayed."""
    registry = McpExecutionRegistry()
    transport = _RetainedExecutionTransport(registry, ["input_required", "drop"])
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
        server_connected=lambda: True,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(manager.call_tool(None, "sys_os_shell", {}))
    try:
        await _wait_until_registered("elicit_live")
        assert pending_approvals.resolve("elicit_live", approved=True)

        with pytest.raises(RuntimeError, match="completion is unknown"):
            await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.external_invocations == 1
    assert [call.body["id"] for call in transport.calls] == [1, 2]


@pytest.mark.asyncio
async def test_call_tool_is_error_returns_json_error_string() -> None:
    """``isError=True`` in result must be returned as a JSON error string, not raised.

    Failure means tool-reported errors raise RuntimeError and crash the harness
    instead of surfacing cleanly to the LLM as a tool result.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Repository not found"}],
                "isError": True,
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    output = await manager.call_tool(_make_spec("github"), "github__get_repo", {})

    parsed = json.loads(output)
    assert parsed == {"error": "Repository not found"}, (
        "isError=True must yield a JSON error object for the LLM to interpret"
    )


@pytest.mark.asyncio
async def test_call_tool_minus_32000_returns_json_error_not_raises() -> None:
    """RPC code -32000 (tool denial / server error) must return JSON string, not raise.

    Failure means policy denials (which use -32000) crash the harness instead
    of letting the LLM see the denial message.
    """
    rpc_error = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "Policy DENIED: push to main blocked"},
        }
    )
    transport = _StubTransport([rpc_error])
    manager = _make_manager(transport)

    output = await manager.call_tool(_make_spec("github"), "github__push", {})

    parsed = json.loads(output)
    assert parsed == {"error": "Policy DENIED: push to main blocked"}, (
        "-32000 must be returned as a soft JSON error, not raised"
    )


@pytest.mark.asyncio
async def test_call_tool_non_32000_rpc_error_raises() -> None:
    """An unexpected RPC error code (not -32000) must raise RuntimeError.

    Failure means protocol errors (invalid request, parse error) are silently
    swallowed and the LLM never learns the tool call failed.
    """
    rpc_error = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    )
    transport = _StubTransport([rpc_error])
    manager = _make_manager(transport)

    with pytest.raises(RuntimeError, match="-32600"):
        await manager.call_tool(_make_spec("github"), "github__search", {})


@pytest.mark.asyncio
async def test_call_tool_non_object_rpc_error_raises_precise_error() -> None:
    """A malformed JSON-RPC error remains fail-closed with a precise diagnostic."""
    transport = _StubTransport([_json_resp({"jsonrpc": "2.0", "id": 1, "error": []})])
    manager = _make_manager(transport)

    with pytest.raises(RuntimeError, match="non-object RPC error"):
        await manager.call_tool(_make_spec("github"), "github__search", {})


@pytest.mark.asyncio
async def test_call_tool_network_error_raises() -> None:
    """A network failure must raise RuntimeError containing the tool name and session.

    Failure means network errors are swallowed, leaving the harness
    in an undefined state with no feedback about the failed call.
    """

    class _FailingTransport(httpx.AsyncBaseTransport):
        """Transport that always raises a connection error."""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            """Simulate a network failure.

            :param request: Ignored.
            :raises httpx.ConnectError: Always.
            """
            raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=_FailingTransport(), base_url="http://ap")
    manager = ProxyMcpManager(session_id="conv_test", ap_client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await manager.call_tool(_make_spec("github"), "github__search", {})

    error_msg = str(exc_info.value)
    assert "github__search" in error_msg, "Tool name must appear in the error message"
    assert "conv_test" in error_msg, "Session id must appear in the error message"


# ── dispatch timeout nesting ────────────────────────────────────────────────


def test_proxy_call_timeout_exceeds_forward_timeout() -> None:
    """The outer MCP proxy timeout must exceed the AP→runner timeout."""
    from omnigent.runner.tool_dispatch import (
        _OS_ENV_SHELL_DEFAULT_TIMEOUT_S,
        _RUNNER_EXECUTION_TIMEOUT_S,
        MCP_PROXY_CALL_TIMEOUT_S,
        MCP_PROXY_FORWARD_TIMEOUT_S,
    )

    assert MCP_PROXY_FORWARD_TIMEOUT_S > _OS_ENV_SHELL_DEFAULT_TIMEOUT_S, (
        "AP→runner read timeout must exceed the default sys_os_shell timeout; "
        "otherwise a valid synchronous shell tool can be cut off by transport."
    )
    assert MCP_PROXY_FORWARD_TIMEOUT_S > _RUNNER_EXECUTION_TIMEOUT_S, (
        "AP→runner read timeout must exceed the runner execution timeout, not "
        "just the default shell timeout, because sys_os_shell accepts longer "
        "caller-provided timeouts."
    )
    assert MCP_PROXY_FORWARD_TIMEOUT_S < MCP_PROXY_CALL_TIMEOUT_S, (
        "Runner→AP outer timeout must exceed AP→runner forwarding timeout so "
        "the inner hop fails first with the useful runner-side error."
    )


@pytest.mark.asyncio
async def test_call_tool_uses_configured_read_timeout() -> None:
    """``call_tool`` must POST with the configured proxy read timeout."""
    from omnigent.runner.tool_dispatch import MCP_PROXY_CALL_TIMEOUT_S

    captured: dict[str, object] = {}

    class _TimeoutCapturingTransport(httpx.AsyncBaseTransport):
        """Transport that records the request's timeout extension."""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            """Record the timeout extension and return an empty-result response.

            :param request: The outgoing request whose timeout is captured.
            :returns: A minimal successful JSON-RPC result.
            """
            captured["timeout"] = request.extensions.get("timeout")
            return _json_resp({"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    client = httpx.AsyncClient(transport=_TimeoutCapturingTransport(), base_url="http://ap")
    manager = ProxyMcpManager(session_id="conv_test", ap_client=client)

    await manager.call_tool(_make_spec("github"), "github__search", {})

    timeout = captured["timeout"]
    assert isinstance(timeout, dict), "httpx records the timeout as a per-op dict"
    assert timeout["read"] == MCP_PROXY_CALL_TIMEOUT_S, (
        "ProxyMcpManager must pass MCP_PROXY_CALL_TIMEOUT_S as the read timeout; "
        "otherwise call_tool may regress to httpx's shorter default."
    )
    assert timeout["connect"] == 10.0, "Connect timeout stays short to fail fast on a dead server"


# ── runner built-ins: local execution with server policy ───────────────────


class _PolicyServer(httpx.AsyncBaseTransport):
    """Stub Omnigent server for local built-in dispatch.

    ``/policies/evaluate`` answers from a per-phase queue of responses (or
    exceptions to raise); ``/mcp`` answers from ``mcp_responses``. Every
    request is recorded in ``calls``.
    """

    def __init__(
        self,
        verdicts: dict[str, list[httpx.Response | Exception]] | None = None,
        mcp_responses: list[httpx.Response] | None = None,
    ) -> None:
        self.verdicts = verdicts or {}
        self.mcp_responses = list(mcp_responses or [])
        self.calls: list[_Call] = []
        self.evaluate_gate: asyncio.Event | None = None

    def phases(self) -> list[str]:
        """Return the evaluated phases in request order."""
        return [
            call.body["event"]["type"] for call in self.calls if "/policies/evaluate" in call.url
        ]

    def mcp_calls(self) -> list[_Call]:
        """Return the requests that reached the session MCP endpoint."""
        return [call for call in self.calls if call.url.endswith("/mcp")]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(_Call(url=str(request.url), body=body))
        if request.url.path.endswith("/policies/evaluate"):
            if self.evaluate_gate is not None:
                await self.evaluate_gate.wait()
            queued = self.verdicts[body["event"]["type"]].pop(0)
            if isinstance(queued, Exception):
                raise queued
            return queued
        return self.mcp_responses.pop(0)


def _verdict(action: str, **fields: object) -> httpx.Response:
    """Build a ``/policies/evaluate`` response, e.g. ``_verdict("DENY", reason="x")``."""
    return _json_resp(
        {"result": f"POLICY_ACTION_{action}", "_omnigent_return_ask": True, **fields}
    )


class _RecordingExecutor:
    """Local executor stub that records each built-in execution."""

    def __init__(self, output: str = "file contents") -> None:
        self.output = output
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, session_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((session_id, tool_name, arguments))
        return self.output


def _local_manager(
    server: _PolicyServer,
    executor: _RecordingExecutor,
    *,
    session_id: str | None = None,
    execution_registry: McpExecutionRegistry | None = None,
    server_connected: Callable[[], bool] | None = None,
) -> ProxyMcpManager:
    """Build a manager that runs built-ins through *executor*."""
    client = httpx.AsyncClient(transport=server, base_url="http://ap-server")
    return ProxyMcpManager(
        session_id=session_id or f"conv_{uuid.uuid4().hex[:12]}",
        ap_client=client,
        execution_registry=execution_registry,
        server_connected=server_connected,
        local_executor=executor,
    )


def _mcp_text(text: str) -> httpx.Response:
    """Build a session-MCP ``tools/call`` result carrying *text*."""
    return _json_resp(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
    )


@pytest.mark.asyncio
async def test_builtin_runs_locally_while_mcp_and_server_tools_use_mcp_endpoint() -> None:
    """Built-ins skip ``/mcp`` but are still policy-checked; MCP tools keep ``/mcp``."""
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [_verdict("ALLOW")],
            "PHASE_TOOL_RESULT": [_verdict("ALLOW")],
        },
        mcp_responses=[_mcp_text("issues"), _mcp_text("advice")],
    )
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)

    assert await manager.call_tool(None, "sys_os_read", {"path": "a.txt"}) == "file contents"
    assert executor.calls == [(manager._session_id, "sys_os_read", {"path": "a.txt"})]
    assert server.phases() == ["PHASE_TOOL_CALL", "PHASE_TOOL_RESULT"]
    assert server.mcp_calls() == []
    result_event = server.calls[1].body["event"]
    assert result_event["data"] == {"result": "file contents"}
    assert result_event["request_data"] == {"name": "sys_os_read", "arguments": {"path": "a.txt"}}
    assert server.calls[0].body["_omnigent_return_ask"] is True

    assert await manager.call_tool(_make_spec("github"), "github__search", {}) == "issues"
    assert await manager.call_tool(None, "sys_advise_models", {}) == "advice"
    assert [call.body["params"]["name"] for call in server.mcp_calls()] == [
        "github__search",
        "sys_advise_models",
    ]
    assert len(executor.calls) == 1, "MCP and server-executed tools must not run locally"


@pytest.mark.asyncio
async def test_builtin_tool_call_deny_never_executes() -> None:
    """A TOOL_CALL DENY returns the MCP endpoint's denial and skips the tool."""
    server = _PolicyServer(verdicts={"PHASE_TOOL_CALL": [_verdict("DENY", reason="no reads")]})
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)

    output = await manager.call_tool(None, "sys_os_read", {"path": "secret"})

    assert json.loads(output) == {"error": "Denied by policy: no reads"}
    assert executor.calls == []
    assert server.phases() == ["PHASE_TOOL_CALL"]


@pytest.mark.parametrize(
    ("result_verdict", "expected"),
    [
        (_verdict("DENY", reason="secret"), "[Result suppressed by policy: secret]"),
        (_verdict("ALLOW", data="[REDACTED]"), "[REDACTED]"),
        (_verdict("ASK", data="[REVIEW]"), "[REVIEW]"),
    ],
    ids=["deny", "redact", "ask-transform"],
)
@pytest.mark.asyncio
async def test_builtin_result_policy_shapes_output(
    result_verdict: httpx.Response, expected: str
) -> None:
    """TOOL_RESULT verdicts replace the output exactly as the MCP endpoint does."""
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [_verdict("ALLOW", data={"path": "safe.txt"})],
            "PHASE_TOOL_RESULT": [result_verdict],
        }
    )
    executor = _RecordingExecutor(output="api_key=hunter2")
    manager = _local_manager(server, executor)

    assert await manager.call_tool(None, "sys_os_read", {"path": "a.txt"}) == expected
    # TOOL_CALL's transformed arguments are what ran and what TOOL_RESULT sees.
    assert executor.calls[0][2] == {"path": "safe.txt"}
    assert server.calls[1].body["event"]["request_data"]["arguments"] == {"path": "safe.txt"}


@pytest.mark.asyncio
async def test_builtin_ask_parks_and_resumes_through_mcp_endpoint() -> None:
    """A TOOL_CALL ASK hands the call to the MCP endpoint's approval flow."""
    elicitation_id = "elicit_builtin"
    server = _PolicyServer(
        verdicts={"PHASE_TOOL_CALL": [_verdict("ASK", reason="approve?")]},
        mcp_responses=[
            _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "resultType": "input_required",
                        "inputRequests": {elicitation_id: {"params": {"requestedSchema": {}}}},
                        "requestState": "state",
                    },
                }
            ),
            _mcp_text("approved output"),
        ],
    )
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor, execution_registry=McpExecutionRegistry())
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(manager.call_tool(None, "sys_os_shell", {"command": "ls"}))
    try:
        for _ in range(1000):
            if elicitation_id in pending_approvals._pending:
                break
            await asyncio.sleep(0.001)
        assert pending_approvals.resolve(elicitation_id, approved=True)
        assert await task == "approved output"
    finally:
        if not task.done():
            task.cancel()
        pending_approvals.reset_for_tests()

    assert executor.calls == [], "the MCP endpoint executes approved calls"
    first, retry = [call.body["params"] for call in server.mcp_calls()]
    assert first["arguments"] == {"command": "ls"}
    assert retry["inputResponses"][elicitation_id]["action"] == "accept"
    assert retry[MCP_OPERATION_ID_PARAM] == first[MCP_OPERATION_ID_PARAM]


@pytest.mark.asyncio
async def test_builtin_no_policies_hint_skips_policy_round_trips() -> None:
    """After the fast path reports no policies, built-ins make no policy calls."""
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [
                _verdict("ALLOW", no_policies_ttl_s=60),
                _verdict("ASK", reason="approve policy"),
                _verdict("ALLOW"),
            ],
            "PHASE_TOOL_RESULT": [_verdict("ALLOW")],
        },
        mcp_responses=[_mcp_text("denied by user")],
    )
    executor = _RecordingExecutor(output="")
    manager = _local_manager(server, executor)
    session_id = manager._session_id

    for _ in range(3):
        output = await manager.call_tool(None, "sys_os_read", {"path": "a.txt"})
        # An empty output keeps the MCP endpoint's text-block shape.
        assert json.loads(output) == {"content": [{"type": "text", "text": ""}]}
    assert server.phases() == ["PHASE_TOOL_CALL"], "one fast-path call, then none"
    assert len(executor.calls) == 3

    # Policy and schedule changes always reach the engine.
    await manager.call_tool(None, "sys_add_policy", {"name": "p"})
    assert server.phases()[-1] == "PHASE_TOOL_CALL"

    policy_proxy.drop_no_policies_hint(session_id)
    await manager.call_tool(None, "sys_os_read", {"path": "a.txt"})
    assert server.phases()[-2:] == ["PHASE_TOOL_CALL", "PHASE_TOOL_RESULT"]


@pytest.mark.asyncio
async def test_no_policies_hint_needs_honoured_allow_and_ends_on_reconnect() -> None:
    """Only an echoed fast-path ALLOW arms the hint, and a server reconnect clears it."""
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [
                _verdict("UNSPECIFIED", no_policies_ttl_s=60),
                _verdict("ALLOW", no_policies_ttl_s=60),
                _verdict("ALLOW"),
            ],
            "PHASE_TOOL_RESULT": [_verdict("ALLOW")],
        }
    )
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)

    assert "error" in json.loads(await manager.call_tool(None, "sys_os_read", {}))
    assert not policy_proxy.no_policies_apply(manager._session_id)
    await manager.call_tool(None, "sys_os_read", {})
    assert policy_proxy.no_policies_apply(manager._session_id)

    policy_proxy.drop_all_no_policies_hints()
    await manager.call_tool(None, "sys_os_read", {})
    assert server.phases() == ["PHASE_TOOL_CALL"] * 3 + ["PHASE_TOOL_RESULT"]


@pytest.mark.asyncio
async def test_no_policies_hint_holds_until_its_ttl_then_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipped calls ride one report for the whole TTL; the next call after it re-checks."""
    clock = 0.0
    monkeypatch.setattr(policy_proxy, "monotonic", lambda: clock)
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [
                _verdict("ALLOW", no_policies_ttl_s=120),
                _verdict("ALLOW", no_policies_ttl_s=120),
            ],
        }
    )
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)

    for clock in (0.0, 30.0, 60.0, 119.0):
        await manager.call_tool(None, "sys_os_read", {})
    assert server.phases() == ["PHASE_TOOL_CALL"]

    clock = 121.0
    await manager.call_tool(None, "sys_os_read", {})
    clock = 200.0
    await manager.call_tool(None, "sys_os_read", {})
    assert server.phases() == ["PHASE_TOOL_CALL"] * 2, "expiry re-checks once and re-arms"
    assert len(executor.calls) == 6


@pytest.mark.asyncio
async def test_deny_added_inside_the_skip_window_is_enforced() -> None:
    """A pushed policy change ends the skip, so the new DENY applies to the next call."""
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [
                _verdict("ALLOW", no_policies_ttl_s=120),
                _verdict("DENY", reason="reads blocked"),
            ],
        }
    )
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)
    await manager.call_tool(None, "sys_os_read", {})
    await manager.call_tool(None, "sys_os_read", {})
    assert len(executor.calls) == 2

    # What the runner does on a session ``policies_changed`` push.
    policy_proxy.drop_no_policies_hint(manager._session_id)

    output = await manager.call_tool(None, "sys_os_read", {})
    assert json.loads(output) == {"error": "Denied by policy: reads blocked"}
    assert len(executor.calls) == 2


def test_answer_requested_before_a_reconnect_does_not_arm_the_hint() -> None:
    """A fast-path answer from the old server cannot re-arm a hint dropped on reconnect."""
    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    epoch = policy_proxy.no_policies_hint_epoch(session_id)
    policy_proxy.drop_all_no_policies_hints()
    policy_proxy.note_no_policies(session_id, 120, epoch)
    assert not policy_proxy.no_policies_apply(session_id)


@pytest.mark.asyncio
async def test_builtin_falls_back_to_mcp_endpoint_on_server_without_flag() -> None:
    """A server that ignores ``_omnigent_return_ask`` keeps the MCP endpoint path."""
    server = _PolicyServer(
        verdicts={"PHASE_TOOL_CALL": [_json_resp({"result": "POLICY_ACTION_ALLOW"})]},
        mcp_responses=[_mcp_text("via mcp")],
    )
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)

    assert await manager.call_tool(None, "sys_os_read", {"path": "secret.txt"}) == "via mcp"
    assert executor.calls == []
    assert server.mcp_calls()[0].body["params"]["arguments"] == {"path": "secret.txt"}


@pytest.mark.asyncio
async def test_policy_change_during_evaluation_does_not_arm_stale_hint() -> None:
    """A fast-path answer computed before a policy change cannot skip later checks."""
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [_verdict("ALLOW", no_policies_ttl_s=60)],
            "PHASE_TOOL_RESULT": [_verdict("DENY", reason="new policy")],
        }
    )
    server.evaluate_gate = asyncio.Event()
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)
    task = asyncio.create_task(manager.call_tool(None, "sys_os_read", {"path": "a.txt"}))
    for _ in range(1000):
        if server.calls:
            break
        await asyncio.sleep(0.001)
    policy_proxy.drop_no_policies_hint(manager._session_id)
    server.evaluate_gate.set()

    assert await task == "[Result suppressed by policy: new policy]"
    assert not policy_proxy.no_policies_apply(manager._session_id)


@pytest.mark.parametrize(
    ("verdicts", "executions"),
    [
        ({"PHASE_TOOL_CALL": [_json_resp({"detail": "boom"}, status=500)]}, 0),
        ({"PHASE_TOOL_CALL": [_json_resp({"unexpected": True})]}, 0),
        ({"PHASE_TOOL_CALL": [_verdict("UNSPECIFIED")]}, 0),
        (
            {
                "PHASE_TOOL_CALL": [_verdict("ALLOW")],
                "PHASE_TOOL_RESULT": [_json_resp({"detail": "boom"}, status=500)],
            },
            1,
        ),
    ],
    ids=["tool-call-500", "tool-call-malformed", "tool-call-no-agent", "tool-result-500"],
)
@pytest.mark.asyncio
async def test_builtin_fails_closed_when_policy_unavailable(
    verdicts: dict[str, list[httpx.Response | Exception]], executions: int
) -> None:
    """An unevaluable TOOL_CALL never runs the tool; an unevaluable result is withheld."""
    executor = _RecordingExecutor(output="sensitive")
    manager = _local_manager(_PolicyServer(verdicts=verdicts), executor)

    output = await manager.call_tool(None, "sys_os_read", {"path": "a.txt"})

    assert "error" in json.loads(output)
    assert "sensitive" not in output
    assert len(executor.calls) == executions


@pytest.mark.asyncio
async def test_builtin_policy_transport_loss_waits_for_server_and_runs_once() -> None:
    """A lost server during TOOL_RESULT retries the check, never the tool."""
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [_verdict("ALLOW")],
            "PHASE_TOOL_RESULT": [httpx.ConnectError("tunnel dropped"), _verdict("ALLOW")],
        }
    )
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor, execution_registry=McpExecutionRegistry())
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(manager.call_tool(None, "sys_os_read", {"path": "a.txt"}))
    try:
        for _ in range(1000):
            if pending_approvals._server_reconnect_waiters:
                break
            await asyncio.sleep(0.001)
        pending_approvals.notify_server_reconnect()
        assert await task == "file contents"
    finally:
        if not task.done():
            task.cancel()
        pending_approvals.reset_for_tests()

    assert len(executor.calls) == 1
    assert server.phases() == ["PHASE_TOOL_CALL", "PHASE_TOOL_RESULT", "PHASE_TOOL_RESULT"]


@pytest.mark.parametrize("dropped_phase", ["PHASE_TOOL_CALL", "PHASE_TOOL_RESULT"])
@pytest.mark.asyncio
async def test_builtin_policy_check_dropped_on_live_tunnel_retries_promptly(
    dropped_phase: str,
) -> None:
    """A check dropped while the tunnel stays up is retried without a reconnect wait."""
    verdicts: dict[str, list[httpx.Response | Exception]] = {
        "PHASE_TOOL_CALL": [_verdict("ALLOW")],
        "PHASE_TOOL_RESULT": [_verdict("ALLOW")],
    }
    verdicts[dropped_phase].insert(0, httpx.ReadError("request dropped"))
    server = _PolicyServer(verdicts=verdicts)
    executor = _RecordingExecutor()
    manager = _local_manager(
        server,
        executor,
        execution_registry=McpExecutionRegistry(),
        server_connected=lambda: True,
    )
    pending_approvals.reset_for_tests()

    output = await asyncio.wait_for(
        manager.call_tool(None, "sys_os_read", {"path": "a.txt"}), timeout=5.0
    )

    assert output == "file contents"
    assert len(executor.calls) == 1
    assert server.phases().count(dropped_phase) == 2


@pytest.mark.asyncio
async def test_builtin_result_check_keeps_its_own_live_tunnel_retries() -> None:
    """Retries spent on TOOL_CALL do not push a dropped TOOL_RESULT into a reconnect wait."""
    dropped = [httpx.ReadError("request dropped") for _ in range(3)]
    server = _PolicyServer(
        verdicts={
            "PHASE_TOOL_CALL": [*dropped, _verdict("ALLOW")],
            "PHASE_TOOL_RESULT": [httpx.ReadError("request dropped"), _verdict("ALLOW")],
        }
    )
    executor = _RecordingExecutor()
    manager = _local_manager(
        server,
        executor,
        execution_registry=McpExecutionRegistry(),
        server_connected=lambda: True,
    )
    pending_approvals.reset_for_tests()

    output = await asyncio.wait_for(manager.call_tool(None, "sys_os_read", {}), timeout=10.0)

    assert output == "file contents"
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_builtin_cancelled_during_policy_check_never_executes() -> None:
    """Cancelling a dispatch parked on its TOOL_CALL check does not run the tool."""
    server = _PolicyServer(verdicts={"PHASE_TOOL_CALL": [_verdict("ALLOW")]})
    server.evaluate_gate = asyncio.Event()
    executor = _RecordingExecutor()
    manager = _local_manager(server, executor)
    task = asyncio.create_task(manager.call_tool(None, "sys_os_shell", {"command": "rm x"}))
    for _ in range(1000):
        if server.calls:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    server.evaluate_gate.set()
    await asyncio.sleep(0)

    assert executor.calls == []
