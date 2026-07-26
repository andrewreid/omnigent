"""Small mock-LLM e2e smoke for the polly coding orchestrator (examples/polly).

Mock mode: boots a throwaway LOCAL server from this working tree (which carries
the in-tree ``omnigent.inner.nessie.policies`` module the RUNNER resolves polly's
guardrails from), rewrites the polly bundle's executor to use
``openai-agents`` harness wired to the mock LLM server, and runs a one-shot
``omnigent run`` subprocess against it. This exercises the parts a structural
spec-load test can't — bundle load, server-side guardrail policy resolution,
and a turn streaming back through the run path — without requiring real OAuth
credentials or proprietary model access.

Why a local server (not bare ``omnigent run``): polly's guardrail policies
(``omnigent.inner.nessie.policies`` — the package keeps its historical
name) are resolved SERVER-SIDE when the workflow executes. Bare ``omnigent
run`` routes to the developer's configured default server (the shared
``omnigent`` prod app), which may not carry the in-tree policy module, so
the turn 500s at event-execution. We therefore stand up a throwaway local
``omnigent server`` from this working tree - which DOES carry the polly
code - and point ``run --server`` at it.

Mock helpers and fixture names are exported from this module so the sibling
cost-advisor and subagent-model tests can re-use them directly:

    from tests.e2e.test_polly_e2e import (
        _SERVER_BOOT_TIMEOUT_SEC,
        _free_port,
        _wait_for_health,
        _mock_env,
        _mock_polly_spec_dir,
    )

Run it manually after touching the polly bundle, its skills, or the
openai-agents auth paths::

    pytest tests/e2e/test_polly_e2e.py -v
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml

# tests/e2e/test_polly_e2e.py -> repo root is 2 parents up.
_REPO = Path(__file__).resolve().parents[2]
_POLLY = _REPO / "examples" / "polly"
_RUN_TIMEOUT_SEC = 180
_SERVER_BOOT_TIMEOUT_SEC = 90
# Long enough to prove a real reply came back, short enough to flag an empty turn.
_MIN_REPLY_CHARS = 12

# Model key for the polly brain in mock mode. The mock server routes responses
# by the ``model`` field in the POST /v1/responses body, so the spec's executor
# model must match the key used in configure_mock_llm.
_MOCK_BRAIN_MODEL = "mock-polly-brain"


def _free_port() -> int:
    """
    Reserve an ephemeral localhost port for the local server.

    :returns: A port number the OS just confirmed is free. There is a small
        window between close and the server re-binding it; acceptable for a
        single-process opt-in smoke.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, deadline: float) -> None:
    """
    Block until the local server answers HTTP, or fail past ``deadline``.

    :param base_url: e.g. ``"http://127.0.0.1:8811"``.
    :param deadline: ``time.monotonic()`` value past which to give up.
    """
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as err:  # not up yet
            last_err = err
        time.sleep(1)
    raise TimeoutError(f"local server at {base_url} never became healthy: {last_err}")


def _mock_env(
    mock_llm_server_url: str,
    *,
    env_passthrough: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    Build a subprocess env with mock LLM credentials injected.

    Strips real credential env vars (Databricks, Anthropic, Claude/Codex
    binaries) and injects ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` so the
    ``openai-agents`` harness routes to the mock LLM server. An isolated
    ``OMNIGENT_CONFIG_HOME`` prevents the spawned process from touching
    the developer's real omnigent state.

    :param mock_llm_server_url: The mock LLM server base URL, e.g.
        ``"http://127.0.0.1:12345"``.  The function appends ``/v1`` so the
        harness hits ``/v1/responses``.
    :param env_passthrough: Env vars applied AFTER the credential strip, so a
        spec that INTERPOLATES one of the stripped names still parses. holly's
        github MCP header reads ``${GITHUB_TOKEN}`` and an unresolved variable
        is a hard parse error by design, so the strip would fail the run at
        spec load. The caller supplies the value (a dummy is enough — nothing
        here contacts github), which keeps the test independent of whatever
        real token the developer happens to have exported.
    :returns: A copy of ``os.environ`` with credentials stripped and mock
        overrides set.
    """
    env = dict(__import__("os").environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    # Write an isolated config home so the spawned process doesn't inherit the
    # developer's real auth config.
    config_home = Path(tempfile.mkdtemp(prefix="omnigent-mock-config-"))
    (config_home / "config.yaml").write_text("", encoding="utf-8")
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    # Strip credentials that would shadow or conflict with mock access.
    # Covers Databricks, Anthropic/Claude, OpenAI, AWS, GCP, Azure, GitHub,
    # and any other credential vars that should not leak into mock subprocesses.
    _CREDENTIAL_VARS = (
        # Databricks
        "DATABRICKS_TOKEN",
        "DATABRICKS_HOST",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_ACCOUNT_ID",
        # Anthropic / Claude SDK
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE",
        "CLAUDECODE",
        # OpenAI / Codex (will be overridden below, but strip first)
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX",
        # AWS
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
        # GCP / Google
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCP_PROJECT",
        "GCLOUD_PROJECT",
        # Azure
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
        # GitHub
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
    )
    for stale in _CREDENTIAL_VARS:
        env.pop(stale, None)
    # Point the openai-agents harness at the mock server.
    env["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    env["OPENAI_API_KEY"] = "mock-key"
    # Re-apply after the strip: these are the vars the spec under test needs.
    env.update(env_passthrough or {})
    return env


def _mock_polly_spec_dir(
    tmp_path: Path,
    mock_llm_server_url: str,
    *,
    brain_model: str = _MOCK_BRAIN_MODEL,
    extra_executor_config: dict | None = None,
    polly_src: Path = _POLLY,
    rewrite_sub_agent_harnesses: bool = False,
) -> Path:
    """
    Copy the polly bundle into *tmp_path* and rewrite it to use the mock LLM.

    Switches the executor harness from ``claude-sdk`` to ``openai-agents``,
    sets a deterministic model key (so ``configure_mock_llm`` can target it),
    and bakes a ``connection`` block pointing at the mock server so both the
    brain harness and the runner-side cost judge call the mock rather than a
    real provider.

    :param tmp_path: Per-test temp dir to write the spec copy into.
    :param mock_llm_server_url: The mock LLM server base URL, e.g.
        ``"http://127.0.0.1:12345"``.
    :param brain_model: Model key to bake into ``executor.model``; must
        match the key passed to ``configure_mock_llm``.
    :param extra_executor_config: Optional additional keys merged into
        ``executor.config`` after the harness rewrite (e.g. ``cost_optimize``
        for the cost-advisor tests).
    :param polly_src: Source polly bundle directory; defaults to the
        shipped ``examples/polly``.
    :param rewrite_sub_agent_harnesses: When ``True``, rewrite EVERY sub-agent
        harness that needs a CLI binary on ``PATH`` to ``openai-agents``.  Use
        this when a test only needs the child *session row* to be created (e.g.
        to verify ``model_override``) and doesn't need the native binary to
        actually run — avoids failures on machines where the binary is absent
        from ``PATH``.
    :returns: Path to the copied polly bundle directory.
    """
    # Name the copy after the source bundle so a non-polly caller (holly) does
    # not end up running a directory called "polly".
    dst = tmp_path / polly_src.name
    shutil.copytree(polly_src, dst, symlinks=False)
    config_path = dst / "config.yaml"
    spec = yaml.safe_load(config_path.read_text())
    executor = spec.setdefault("executor", {})
    # Rewrite executor to use openai-agents so the mock server is honoured.
    executor_config = executor.pop("config", {}) or {}
    executor_config["harness"] = "openai-agents"
    if extra_executor_config:
        executor_config.update(extra_executor_config)
    executor["config"] = executor_config
    # Set a deterministic model key the mock server queues against.
    executor["model"] = brain_model
    # Bake an auth block (type: api_key) so the workflow layer sets
    # HARNESS_OPENAI_AGENTS_API_KEY and HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL
    # pointing at the mock server, bypassing all profile / env-var resolution.
    executor["auth"] = {
        "type": "api_key",
        "api_key": "mock-key",
        "base_url": f"{mock_llm_server_url}/v1",
    }
    # Also bake a connection block so the runner-side cost judge (which calls
    # the LLM client directly, not through the harness) also routes to mock.
    executor["connection"] = {
        "base_url": f"{mock_llm_server_url}/v1",
        "api_key": "mock-key",
    }
    config_path.write_text(yaml.safe_dump(spec, sort_keys=False))

    if rewrite_sub_agent_harnesses:
        # Rewrite each sub-agent's config.yaml so any harness needing a CLI
        # binary on PATH becomes ``openai-agents`` (SDK-based). This lets tests
        # verify the child session row is created with the correct
        # model_override without requiring the binary to be installed.
        agents_dir = dst / "agents"
        if agents_dir.is_dir():
            for sub_config in agents_dir.glob("*/config.yaml"):
                sub_spec = yaml.safe_load(sub_config.read_text())
                sub_executor = sub_spec.get("executor") or {}
                sub_cfg = sub_executor.get("config") or {}
                harness = sub_cfg.get("harness") or sub_executor.get("type") or ""
                if _harness_needs_a_cli(harness):
                    sub_cfg["harness"] = "openai-agents"
                    sub_executor["config"] = sub_cfg
                    sub_spec["executor"] = sub_executor
                    sub_config.write_text(yaml.safe_dump(sub_spec, sort_keys=False))

    return dst


def _harness_needs_a_cli(harness: str) -> bool:
    """
    Whether *harness* launches a CLI binary that must be on ``PATH``.

    Asks the installer registry instead of matching a hand-kept list of native
    harness names. The list form was a DENYLIST and failed open exactly the way
    the reviewer mandate says a denylist does: it named
    ``claude-native`` / ``codex-native`` / ``pi`` / ``cursor-native`` and their
    aliases, and silently missed the shipped ``hermes-native`` and
    ``opencode-native`` siblings, so a bundle carrying either kept its native
    harness and the test failed on any machine without that binary.

    ``required_cli_for_harness`` returns an install spec for every harness that
    needs a binary and ``None`` for the SDK harnesses (``openai-agents``,
    ``claude-sdk``, ``codex``), which is also why the rewrite target itself is
    never rewritten. Aliases are canonicalized first, so ``native-claude`` and
    ``claude-native`` answer alike.

    :param harness: Declared harness name from a sub-agent's ``executor``.
    :returns: ``True`` when the harness needs a CLI binary on ``PATH``.
    """
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.onboarding.harness_install import required_cli_for_harness

    if not harness:
        return False
    canonical = canonicalize_harness(harness) or harness
    return required_cli_for_harness(canonical) is not None


# INDEPENDENT classification for the test below, hand-maintained on purpose.
#
# The obvious oracle — ``required_cli_for_harness(h) is not None`` — is the exact
# expression ``_harness_needs_a_cli`` evaluates, so comparing them compares a
# thing to itself and passes by construction. It is also fail-open in the same
# direction as the denylist it replaced: the registry returns ``None`` for an
# UNKNOWN harness just as it does for an SDK one, so a newly declared native
# harness nobody registered yields ``False == False`` and is never rewritten.
#
# These two sets are therefore maintained by hand and deliberately not derived
# from the registry. A harness in NEITHER set fails the test, which is the point:
# shipping a new worker harness is exactly when a human should decide which kind
# it is. That failure is the maintenance cost, and it is cheaper than the silent
# pass it replaces.
_KNOWN_CLI_HARNESSES = frozenset(
    {
        "antigravity-native",
        "claude-native",
        "codex-native",
        "cursor-native",
        "goose",
        "goose-native",
        "hermes",
        "hermes-native",
        "kimi",
        "kimi-native",
        "kiro-native",
        "opencode-native",
        "pi",
        "pi-native",
        "qwen",
        "qwen-code",
        "qwen-native",
    }
)
_KNOWN_SDK_HARNESSES = frozenset(
    {
        "antigravity",
        "claude",
        "claude-sdk",
        "codex",
        "copilot",
        "cursor",
        "openai-agents",
    }
)


def test_native_harness_rewrite_covers_every_shipped_worker_harness() -> None:
    """
    Every harness a shipped example worker declares is classified, and the
    rewrite predicate agrees with that classification.

    The regression guard for the denylist this replaced — rewritten, because the
    first version of this test was the same fail-open shape one level up. It
    asked the registry the predicate itself asks, so it could only ever agree
    with it, and an unregistered native harness read as SDK to both.

    The oracle is now ``_KNOWN_CLI_HARNESSES`` / ``_KNOWN_SDK_HARNESSES`` above:
    independent of the registry, and exhaustive over the shipped population by
    construction, because a harness in neither set FAILS instead of passing
    quietly. What this test does NOT do is predict the future — it cannot know
    that some harness invented tomorrow needs a binary. It guarantees only that
    such a harness cannot arrive UNNOTICED, which is what commit 08dc63f0
    claimed and did not deliver.

    Also pins the two directions that make the predicate safe: a harness needing
    a binary is rewritten, and ``openai-agents`` (the rewrite TARGET) is not.
    """
    declared: dict[str, Path] = {}
    for config_path in sorted((_REPO / "examples").glob("*/agents/*/config.yaml")):
        spec = yaml.safe_load(config_path.read_text()) or {}
        executor = spec.get("executor") or {}
        harness = (executor.get("config") or {}).get("harness") or executor.get("type")
        if harness:
            declared.setdefault(str(harness), config_path)

    assert declared, "no shipped example workers found — the glob is wrong"

    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.onboarding.harness_install import required_cli_for_harness

    for harness, config_path in declared.items():
        canonical = canonicalize_harness(harness) or harness
        is_cli = canonical in _KNOWN_CLI_HARNESSES
        is_sdk = canonical in _KNOWN_SDK_HARNESSES
        assert is_cli != is_sdk, (
            f"{config_path.relative_to(_REPO)} declares {harness!r} "
            f"(canonical {canonical!r}), which is in neither _KNOWN_CLI_HARNESSES "
            f"nor _KNOWN_SDK_HARNESSES. Classify it: if it launches a CLI binary it "
            f"must be rewritten for mock runs, and the registry cannot tell you — it "
            f"returns None for an unregistered harness exactly as it does for an SDK "
            f"one."
        )
        assert _harness_needs_a_cli(harness) is is_cli, (
            f"{config_path.relative_to(_REPO)} declares {harness!r}: the rewrite "
            f"predicate says needs_cli={_harness_needs_a_cli(harness)} but this test's "
            f"own classification says {is_cli}. Either the harness is left native "
            f"(and fails without the binary) or it is rewritten when it did not need "
            f"to be."
        )

    # The rewrite target must never itself be a rewrite candidate.
    assert not _harness_needs_a_cli("openai-agents")
    # The shape the denylist got wrong: these are shipped and DO need a CLI.
    assert _harness_needs_a_cli("hermes-native")
    assert _harness_needs_a_cli("opencode-native")
    # And the fail-open that makes the hand-maintained sets necessary: the
    # registry cannot distinguish "needs no CLI" from "never heard of it".
    assert required_cli_for_harness("totally-unregistered-native") is None


@pytest.fixture
def local_polly_server(tmp_path: Path) -> Iterator[str]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    The server carries the in-tree ``omnigent.inner.nessie.policies`` module the
    RUNNER resolves polly's guardrails from — it builds its policy gate from the
    spec at agent start, and an unresolvable factory path denies startup — so the
    workflow doesn't 500 the way it does against the shared prod app. Own sqlite DB + artifact dir
    under ``tmp_path`` keep it isolated from the developer's real state.

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: The base URL of the running server, e.g. ``"http://127.0.0.1:8811"``.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_uri = f"sqlite:///{tmp_path / 'polly_e2e.db'}"
    artifacts = tmp_path / "artifacts"
    import os

    env = {
        **os.environ,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
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
            db_uri,
            "--artifact-location",
            str(artifacts),
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


def test_polly_orchestrator_boots_and_responds(
    local_polly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    ``omnigent run <mock-polly> --server <local> -p <prompt>``
    exits 0 and emits a non-trivial reply via the mock LLM server.

    Proves the bundle loads end-to-end against a server that carries polly's
    code: the openai-agents harness initialises, the sub-agents register
    without aborting startup, the server-side guardrail policies resolve, and
    a turn completes. A blank reply here is the exact failure that masqueraded
    as "no output" before the auth fix — so this is the regression guard for
    the substrate.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Base URL of the mock LLM server fixture.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    polly_dir = _mock_polly_spec_dir(tmp_path, mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "I am polly, a multi-agent coding orchestrator. "
                    "I handle coding tasks by planning the work and delegating "
                    "implementation to specialized sub-agents."
                )
            }
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(polly_dir),
            "--server",
            local_polly_server,
            "-p",
            "In one short sentence, what are you and how do you handle a coding task?",
        ],
        cwd=str(_REPO),
        env=_mock_env(mock_llm_server_url),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    # Exit 0 proves boot + turn completion; a harness that aborts startup,
    # or a server-side policy that fails to resolve would surface here as
    # a non-zero exit.
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    reply = result.stdout.strip()
    # A real reply, not an empty turn.
    assert len(reply) >= _MIN_REPLY_CHARS, (
        f"polly produced no/short reply ({len(reply)} chars): {reply!r}\n"
        f"--- stderr ---\n{result.stderr}"
    )
