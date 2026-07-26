"""Mock-LLM e2e happy path for the holly coding orchestrator (examples/holly).

``CONTRIBUTING.md`` requires a PR adding new user-facing functionality to ship
at least one e2e happy-path test, and a newly shipped example agent is exactly
that. This file is holly's, and it is deliberately small: two cases.

1. The bundle boots as a REGISTERED agent and completes a one-shot turn against
   the mock LLM.
2. A scripted ``sys_session_send`` from the mock brain reaches a sub-agent — the
   child session row exists with the expected agent, and the dispatch that
   created it carried the expected ``args.purpose``.

Both reuse the polly mock harness (``tests/e2e/test_polly_e2e``): a throwaway
LOCAL server booted from this working tree (which carries the in-tree
``omnigent.inner.nessie.policies`` module holly's guardrails resolve
server-side), the bundle rewritten onto the ``openai-agents`` harness wired to
the mock LLM server, and a one-shot ``omnigent run`` subprocess against it.
``rewrite_sub_agent_harnesses`` swaps the native worker harnesses
(``claude-native`` / ``codex-native`` / ``pi``) for ``openai-agents`` so no real
CLI binary has to be on PATH. Neither network nor credentials are needed: the
``${GITHUB_TOKEN}`` the spec interpolates is supplied as a dummy by this file.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT
-------------------------------------------
Review sequencing — that review runs before publication. Nothing in the runtime
enforces that ordering; it is prompt discipline, which is this bundle's whole
honesty premise. ``blast_radius`` runs with ``gate_pushes: false`` and inspects
shell text only, and it still denies catastrophic push variants (``--force*``,
``--delete``, ``--mirror``, ``--prune``) — but refusing a force-push is not an
ordering gate on a plain ``git push``, which is ungated. Against a scripted mock
brain the brain does exactly what the script says, so an ordering assertion
would be asserting the script rather than holly. Proving holly actually
sequences review before publication takes real CLIs and real judgement — a live
recipe, not a CI test.

Run::

    GITHUB_TOKEN=dummy pytest tests/e2e/test_holly_e2e.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.test_polly_e2e import (
    _MOCK_BRAIN_MODEL,
    _REPO,
    _SERVER_BOOT_TIMEOUT_SEC,
    _free_port,
    _mock_env,
    _mock_polly_spec_dir,
    _wait_for_health,
)

_HOLLY = _REPO / "examples" / "holly"
# Mock runs do no real inference, but the bundle registers three sub-agents.
_RUN_TIMEOUT_SEC = 300
# Long enough to prove a real reply came back, short enough to flag an empty turn.
_MIN_REPLY_CHARS = 12

# holly's github MCP header interpolates ``${GITHUB_TOKEN}`` and an unresolved
# variable is a hard parse error by design. A dummy is supplied rather than
# relaxing the spec or depending on the developer's real token; nothing in this
# file contacts github.
_DUMMY_GITHUB_TOKEN = "ghp_dummy_token_for_holly_e2e"

# The worker the dispatch test targets, and the purpose it declares.
# ``pi`` is holly's read-mostly worker; ``review`` is in the root spec's
# ``headless_subagent_purpose_guard`` allowlist, so the guard ALLOWs it.
_DISPATCH_AGENT = "pi"
_DISPATCH_PURPOSE = "review"


def _api(base_url: str, path: str) -> dict[str, Any]:
    """
    GET a local-server API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


@pytest.fixture
def local_holly_server(tmp_path: Path) -> Iterator[str]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    The server carries the in-tree ``omnigent.inner.nessie.policies`` module
    that holly's guardrails resolve server-side, so the workflow doesn't 500 the
    way it does against a server without them. Own sqlite DB + artifact dir
    under ``tmp_path`` keep it isolated from the developer's real state.

    Mirrors the polly fixtures; duplicated rather than imported because pytest
    fixtures don't cross modules without a conftest, and this file must stay
    droppable next to its siblings.

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: The base URL of the running server.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    import os

    env = {
        **os.environ,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        # The runner re-parses the bundle, so the token has to reach it too.
        "GITHUB_TOKEN": _DUMMY_GITHUB_TOKEN,
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "GITHUB_TOKEN",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'holly_e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        cwd=str(_REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, time.monotonic() + _SERVER_BOOT_TIMEOUT_SEC)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _mock_holly_spec_dir(
    tmp_path: Path,
    mock_llm_server_url: str,
    *,
    rewrite_sub_agent_harnesses: bool = False,
) -> Path:
    """
    Copy the holly bundle into *tmp_path* and rewrite it onto the mock LLM.

    Thin wrapper over the shared polly helper with ``polly_src`` pointed at
    ``examples/holly``.

    :param tmp_path: Per-test temp dir to write the spec copy into.
    :param mock_llm_server_url: The mock LLM server base URL.
    :param rewrite_sub_agent_harnesses: Replace the workers' native CLI
        harnesses with ``openai-agents`` so a child session is created without
        the binary being on PATH.
    :returns: Path to the copied holly bundle directory.
    """
    return _mock_polly_spec_dir(
        tmp_path,
        mock_llm_server_url,
        polly_src=_HOLLY,
        rewrite_sub_agent_harnesses=rewrite_sub_agent_harnesses,
    )


def _run_holly_turn(
    base_url: str,
    prompt: str,
    mock_llm_server_url: str,
    *,
    holly_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """
    Run one headless holly turn against the local server.

    :param base_url: Local server base URL.
    :param prompt: The ``-p`` one-shot prompt.
    :param mock_llm_server_url: Mock LLM server base URL for env injection.
    :param holly_dir: The holly bundle to run.
    :returns: The completed ``omnigent run`` process.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(holly_dir),
            "--server",
            base_url,
            "-p",
            prompt,
        ],
        cwd=str(_REPO),
        # _mock_env strips GITHUB_TOKEN with the other credentials; holly's
        # spec interpolates it, so it is passed back through as a dummy.
        env=_mock_env(
            mock_llm_server_url,
            env_passthrough={"GITHUB_TOKEN": _DUMMY_GITHUB_TOKEN},
        ),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _holly_parent_id(base_url: str) -> str:
    """
    Find the holly parent session on the throwaway server.

    The server DB is per-test, so the only holly session is ours.

    :param base_url: Local server base URL.
    :returns: The parent conversation id.
    """
    sessions = _api(base_url, "/v1/sessions").get("data", [])
    parents = [s["id"] for s in sessions if s.get("agent_name") == "holly"]
    assert parents, (
        f"no session registered under the agent name 'holly' among "
        f"{len(sessions)} sessions: {[s.get('agent_name') for s in sessions]}"
    )
    return parents[0]


def test_holly_orchestrator_boots_and_responds(
    local_holly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    ``omnigent run <mock-holly> --server <local> -p <prompt>`` exits 0, emits a
    non-trivial reply, and the turn is recorded under the agent name ``holly``.

    Proves the bundle loads end-to-end: the spec parses (``${GITHUB_TOKEN}``
    resolves), the openai-agents harness initialises, all three sub-agents
    register without aborting startup, and a turn completes. The session lookup
    is what makes this "as a registered agent" rather than merely "a process
    exited 0" — a bundle that ran under some other name would leave no ``holly``
    row.

    It does NOT cover the guardrail policies. Those resolve server-side when a
    dispatch is EVALUATED, so a turn with no tool call never reaches them:
    breaking ``blast_radius``'s factory path leaves this test green and fails
    the dispatch test below. That one is the guard for policy resolution.

    :param local_holly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Base URL of the mock LLM server fixture.
    :param tmp_path: Per-test temp dir for the mock holly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    holly_dir = _mock_holly_spec_dir(tmp_path, mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "I am holly, a multi-agent coding orchestrator. I plan the "
                    "work, split it up, and delegate every coding task to my "
                    "claude_code / codex / pi sub-agents."
                )
            }
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = _run_holly_turn(
        local_holly_server,
        "In one short sentence, what are you and how do you handle a coding task?",
        mock_llm_server_url,
        holly_dir=holly_dir,
    )

    # Exit 0 proves boot + turn completion; a harness that aborts startup or a
    # server-side policy that fails to resolve surfaces here as a non-zero exit.
    assert result.returncode == 0, (
        f"holly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    reply = result.stdout.strip()
    # A real reply, not an empty turn.
    assert len(reply) >= _MIN_REPLY_CHARS, (
        f"holly produced no/short reply ({len(reply)} chars): {reply!r}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    # Registered under its own name, not run as an anonymous ad-hoc spec.
    _holly_parent_id(local_holly_server)


def test_holly_dispatch_reaches_a_sub_agent(
    local_holly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A scripted ``sys_session_send`` reaches a sub-agent: the child session row
    exists for the dispatched worker, and the dispatch carried its purpose.

    This is the delegation chain holly is built on, end to end — mock tool call
    -> ``sys_session_send`` args -> ``headless_subagent_purpose_guard`` ->
    ``POST /v1/sessions`` -> persisted child row. The purpose half is load
    bearing rather than decorative: the guard DENIES a dispatch whose
    ``args.purpose`` is missing or outside the spec's allowlist, so a child row
    existing at all is proof the guarded dispatch went through, and the
    transcript is where the purpose value itself is observable (nothing
    persists it on the child row). Evaluating the guard is also what forces the
    whole ``omnigent.inner.nessie.policies`` set to resolve on the server, so a
    broken factory path fails here and nowhere else in this file.

    What it does NOT prove is any ORDERING — see the module docstring. The mock
    brain dispatches because the script says to.

    :param local_holly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Base URL of the mock LLM server fixture.
    :param tmp_path: Per-test temp dir for the mock holly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    # rewrite_sub_agent_harnesses=True replaces the native worker harnesses with
    # ``openai-agents`` so the child session row is created even when no
    # ``claude`` / ``codex`` / ``pi`` binary is on PATH — e.g. on CI. The test
    # needs the row, not the worker process.
    holly_dir = _mock_holly_spec_dir(
        tmp_path, mock_llm_server_url, rewrite_sub_agent_harnesses=True
    )
    tag = uuid.uuid4().hex[:8]

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-review-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": _DISPATCH_AGENT,
                                "title": "review-readme-heading",
                                "args": {
                                    "purpose": _DISPATCH_PURPOSE,
                                    "input": "Report the first heading line of README.md.",
                                },
                            }
                        ),
                    }
                ]
            },
            # After the tool result arrives, end the turn.
            {"text": "Dispatched the reviewer. Waiting on the inbox."},
            # Synthesis once the worker completes (or fails fast under mock).
            {"text": "Reviewer done."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = _run_holly_turn(
        local_holly_server,
        "Dispatch one review task to pi.",
        mock_llm_server_url,
        holly_dir=holly_dir,
    )
    assert result.returncode == 0, (
        f"holly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _holly_parent_id(local_holly_server)

    # (a) The dispatch reached a sub-agent: exactly one child, the worker named.
    kids = _api(local_holly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    tools = sorted(k.get("tool") or "" for k in kids)
    assert tools == [_DISPATCH_AGENT], (
        f"expected exactly one {_DISPATCH_AGENT!r} child session, got {tools}; "
        f"run stdout tail: {result.stdout[-400:]!r}"
    )

    # (b) The dispatch that created it declared the purpose the guard allows.
    items = _api(local_holly_server, f"/v1/sessions/{parent}/items").get("data", [])
    dispatched = []
    for item in items:
        if item.get("type") == "function_call" and "session_send" in str(item.get("name", "")):
            raw = item.get("arguments")
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            dispatched.append((parsed.get("agent"), (parsed.get("args") or {}).get("purpose")))
    assert (_DISPATCH_AGENT, _DISPATCH_PURPOSE) in dispatched, (
        f"no sys_session_send to {_DISPATCH_AGENT!r} with purpose "
        f"{_DISPATCH_PURPOSE!r} in the parent transcript; saw {dispatched}"
    )
