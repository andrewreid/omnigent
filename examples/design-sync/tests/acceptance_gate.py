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

    # Prove the constructed policy actually DENIES a mutation tool and
    # ABSTAINS on design_sync (belt-and-suspenders behavioural check).
    fp = constructed[0]
    deny_evt = {"type": "tool_call", "target": "sys_os_shell"}
    allow_evt = {"type": "tool_call", "target": "design_sync"}
    deny_res = fp._callable(deny_evt)
    allow_res = fp._callable(allow_evt)
    print(f"    sys_os_shell -> {deny_res}")
    print(f"    design_sync  -> {allow_res}")
    assert deny_res and deny_res.get("result") == "deny", "expected deny on sys_os_shell"
    assert allow_res is None, "expected abstain (None) on design_sync"

    print("\n== ACCEPTANCE GATE PASSED ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
