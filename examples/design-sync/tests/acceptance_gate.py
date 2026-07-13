#!/usr/bin/env python3
"""
Acceptance gate for the ASSEMBLED design-sync agent.

Loads the example via the REAL omnigent loader/registry:
  1. parse(dir)                       -> AgentSpec, validation errors (must be [])
  2. bundle-upload allowlist check    -> handler MUST be a REGISTERED policy
                                         handler (this is what the real launch
                                         path enforces; round-5's unregistered
                                         make_fixed_action_callable failed here)
  3. ToolManager(spec, workdir)       -> registered tools (must INCLUDE design_sync)
  4. resolve_function_policy          -> construct the CEL guardrail (must NOT raise)
                                         and DENY the 7 mutation tools / ALLOW rest

Run: uv run python examples/design-sync/tests/acceptance_gate.py
"""

import sys
from pathlib import Path

from omnigent.policies.function import resolve_function_policy
from omnigent.policies.registry import is_registered_handler
from omnigent.spec.parser import parse
from omnigent.spec.validator import validate
from omnigent.tools.manager import ToolManager

EXAMPLE_DIR = Path(__file__).resolve().parent.parent


def main() -> int:
    print(f"== Loading assembled agent from {EXAMPLE_DIR} ==\n")

    # 1. Parse + validate
    spec = parse(EXAMPLE_DIR)
    result = validate(spec)
    errors = list(getattr(result, "errors", result))
    print(f"[1] validation errors: {errors}")
    print(f"    executor.type={spec.executor.type!r} model={spec.executor.model!r}")
    print(f"    os_env declared: {spec.os_env is not None}")
    if errors:
        print("!! validation FAILED")
        return 1

    # 1b. Harness-spawn name: mirror the real spawn resolution
    #     (omnigent/runner/app.py: `config.get("harness") or type`, canonicalized)
    #     and confirm it lands on the REGISTERED harness name `claude-sdk`.
    #     Round-7 boot blocker: without config.harness this fell back to the
    #     underscore `claude_sdk`, which is not a registered harness.
    from omnigent.harness_aliases import canonicalize_harness

    raw_harness = spec.executor.config.get("harness") or spec.executor.type
    harness_name = canonicalize_harness(raw_harness) or raw_harness
    print(f"    spawn harness_name={harness_name!r} (registered harness name)")
    if harness_name != "claude-sdk":
        print(f"!! harness name is {harness_name!r}, expected 'claude-sdk' (would fail boot)")
        return 1

    # 2. Bundle-upload allowlist: every guardrail handler must be REGISTERED.
    #    This mirrors omnigent.spec._reject_unregistered_spec_policy_handlers,
    #    the check that rejected round-5's make_fixed_action_callable at launch.
    print("\n[2] registered-policy-handler check (bundle-upload guard):")
    policies = spec.guardrails.policies or []
    handler_failures = []
    for p in policies:
        path = p.function.path
        reg = is_registered_handler(path)
        print(f"    {p.name!r}: handler={path} registered={reg}")
        if not reg:
            handler_failures.append(path)
    if handler_failures:
        print(f"!! UNREGISTERED handler(s) — bundle upload would be REJECTED: {handler_failures}")
        return 1

    # 3. Build ToolManager against real code; list tools
    tm = ToolManager(spec, workdir=EXAMPLE_DIR, sandbox_enabled=False)
    tool_names = sorted(tm._tools.keys())
    print(f"\n[3] ToolManager built {len(tool_names)} tools:")
    for n in tool_names:
        print(f"      - {n}")
    has_design_sync = any("design_sync" in n for n in tool_names)
    print(f"    design_sync present: {has_design_sync}")
    if not has_design_sync:
        print("!! design_sync tool MISSING")
        return 1

    # 4. Construct the guardrail policy (must NOT raise)
    print(f"\n[4] guardrail policy count: {len(policies)}")
    constructed = []
    for p in policies:
        fp = resolve_function_policy(p)
        constructed.append(fp)
        print(f"    constructed policy {p.name!r} -> {type(fp).__name__} (no raise)")

    # 5. Behavioural proof: the CEL policy must DENY every mutation-capable
    #    registered builtin and ALLOW design_sync + the read-only builtins.
    #    Classification audited across all 19 registered tools.
    MUTATION = [
        "update_comment",
        "sys_add_policy",
        "sys_agent_download",
        "sys_cancel_task",
        "browser_navigate",
        "browser_click",
        "browser_type",
    ]
    READ_ONLY = [
        "browser_snapshot",
        "browser_screenshot",
        "list_comments",
        "load_skill",
        "read_skill_file",
        "sys_policy_registry",
        "sys_agent_get",
        "sys_agent_list",
        "sys_session_get_history",
        "sys_session_get_info",
        "sys_session_list",
    ]
    ALLOWED = ["design_sync"]

    fp = constructed[0]

    def decide(tool):
        # CEL reads event.data.name on a tool_call event; target set too.
        return fp._callable(
            {"type": "tool_call", "target": tool, "data": {"name": tool, "arguments": {}}}
        )

    print("\n[5] deny/allow matrix over registered tools (CEL policy):")
    failures = []

    for tool in MUTATION:
        res = decide(tool)
        denied = bool(res) and res.get("result") == "DENY"
        print(f"    DENY   {tool:<26} -> {res}")
        if not denied:
            failures.append(f"{tool} NOT denied (got {res})")

    for tool in READ_ONLY + ALLOWED:
        res = decide(tool)
        # CEL returns an explicit {"result":"ALLOW"} (not None-abstain).
        allowed = res is None or res.get("result") == "ALLOW"
        print(f"    ALLOW  {tool:<26} -> {res}")
        if not allowed:
            failures.append(f"{tool} should be ALLOW (got {res})")

    # Completeness: the classified set must EXACTLY equal the registered set,
    # so no registered tool is silently unclassified.
    classified = set(MUTATION) | set(READ_ONLY) | set(ALLOWED)
    registered = set(tool_names)
    if classified != registered:
        failures.append(
            f"classification != registered tools; "
            f"missing={registered - classified} extra={classified - registered}"
        )

    if failures:
        print("\n!! POLICY MATRIX FAILED:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(
        f"    (all {len(MUTATION)} mutation tools DENY; "
        f"all {len(READ_ONLY) + 1} read-only/allowed tools ALLOW; "
        f"classification == {len(registered)} registered tools)"
    )

    print("\n== ACCEPTANCE GATE PASSED ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
