#!/usr/bin/env python3
"""
Acceptance gate for the ASSEMBLED design-sync agent.

Loads the example via the REAL omnigent loader:
  1. parse(dir)                -> AgentSpec, print validation errors (must be [])
  2. ToolManager(spec, workdir)-> registered tools (must INCLUDE design_sync)
  3. resolve_function_policy   -> construct the guardrail (must NOT raise)

Run: uv run python examples/design-sync/tests/acceptance_gate.py
"""

import sys
from pathlib import Path

from omnigent.policies.function import resolve_function_policy
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

    # 2. Build ToolManager against real code; list tools
    tm = ToolManager(spec, workdir=EXAMPLE_DIR, sandbox_enabled=False)
    tool_names = sorted(tm._tools.keys())
    print(f"\n[2] ToolManager built {len(tool_names)} tools:")
    for n in tool_names:
        print(f"      - {n}")
    has_design_sync = any("design_sync" in n for n in tool_names)
    print(f"    design_sync present: {has_design_sync}")
    if not has_design_sync:
        print("!! design_sync tool MISSING")
        return 1

    # 3. Construct the guardrail policy (must NOT raise)
    policies = spec.guardrails.policies or []
    print(f"\n[3] guardrail policy count: {len(policies)}")
    constructed = []
    for p in policies:
        fp = resolve_function_policy(p)
        constructed.append(fp)
        print(f"    constructed policy {p.name!r} -> {type(fp).__name__} (no raise)")

    # 4. Behavioural proof: the policy must DENY every mutation-capable
    #    registered builtin and ABSTAIN (None -> ALLOW) only on design_sync +
    #    genuinely read-only builtins. Classification audited against
    #    omnigent/runner/tool_dispatch.py + omnigent/tools/builtins/.
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
        return fp._callable({"type": "tool_call", "target": tool})

    print("\n[4] deny/abstain matrix over registered tools:")
    failures = []

    for tool in MUTATION:
        res = decide(tool)
        denied = bool(res) and res.get("result") == "deny"
        print(f"    DENY   {tool:<26} -> {res}")
        if not denied:
            failures.append(f"{tool} NOT denied (got {res})")

    for tool in READ_ONLY + ALLOWED:
        res = decide(tool)
        print(f"    ALLOW  {tool:<26} -> {res}")
        if res is not None:
            failures.append(f"{tool} should abstain (got {res})")

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
        f"    (all {len(MUTATION)} mutation tools denied; "
        f"all {len(READ_ONLY) + 1} read-only/allowed tools abstain; "
        f"classification == {len(registered)} registered tools)"
    )

    print("\n== ACCEPTANCE GATE PASSED ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
