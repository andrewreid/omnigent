"""Built-in CEL expression policy.

A factory that compiles a user-submitted CEL expression into a
policy callable. CEL is non-Turing-complete, side-effect-free,
and guaranteed to terminate — no sandbox escapes, no infinite
loops, no file I/O.

The expression receives the full ``PolicyEvent`` dict as an
``event`` variable and must return a map with a ``result`` key
(``"DENY"``, ``"ASK"``, or ``"ALLOW"``) and an optional
``"reason"`` and/or ``"state_updates"`` key. ``null`` or a map without a
``result`` abstains; any other value (e.g. a bare ``bool``) abstains and logs
a warning, because it is almost always a mistake.
``state_updates`` must be a list of maps shaped
``{"key": string, "action": "set"|"increment"|"delete"|"append", "value": any}``.
``value`` is required for ``set``, ``increment``, and ``append``, and ignored
for ``delete``.
Keys beginning with ``_policy_`` belong to the runtime and cannot be changed
by CEL expressions.

Register statically in an agent's YAML, or dynamically on a running
session via the policy API.

Static, in an agent ``config.yaml`` (``policies:`` block, parsed by
:mod:`omnigent.inner.loader` — ``handler`` + ``factory_params``)::

    policies:
      block_shell:
        type: function
        handler: omnigent.policies.builtins.cel.cel_policy
        factory_params:
          expression: >-
            event.type == "tool_call" && event.data.name == "sys_os_shell"
            ? {"result": "DENY"} : {"result": "ALLOW"}
          reason: Shell access is blocked.

Static, in a bundled agent spec (``guardrails.policies``, parsed by
:mod:`omnigent.spec.parser` — note the different spelling: a
``function`` mapping with ``path`` + ``arguments``; this parser does
NOT read ``factory_params``)::

    guardrails:
      policies:
        block_shell:
          type: function
          function:
            path: omnigent.policies.builtins.cel.cel_policy
            arguments:
              expression: >-
                event.type == "tool_call" && event.data.name == "sys_os_shell"
                ? {"result": "DENY"} : {"result": "ALLOW"}
              reason: Shell access is blocked.

Dynamic, via the session policy API:

.. code-block:: http

    POST /v1/sessions/{session_id}/policies
    {
        "name": "block_shell",
        "type": "python",
        "handler": "omnigent.policies.builtins.cel.cel_policy",
        "factory_params": {
            "expression": "event.target == 'sys_os_shell' ? {'result': 'DENY'} : null",
            "reason": "Shell access is blocked."
        }
    }

CEL reference: https://cel.dev/overview/cel-overview
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping, Sequence

try:
    import celpy
    import celpy.celtypes
    from celpy import Environment, Runner
    from celpy.adapter import json_to_cel
    from celpy.celparser import CELParseError
    from celpy.evaluation import CELEvalError
except ImportError:
    celpy = None  # type: ignore[assignment]

from omnigent.policies.schema import (
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
    acts_on,
    is_reserved_state_key,
    request_user_text,
)
from omnigent.spec.types import Phase

_log = logging.getLogger(__name__)

# Engines are rebuilt for every evaluation, so compiled programs are reused
# across builds from a bounded cache. A multi-KB expression's AST is up to ~1 MB.
_PROGRAM_CACHE_SIZE = 64


def cel_policy(
    *,
    expression: str,
    reason: str = "Denied by policy.",
) -> PolicyCallable:
    """Factory: compile a CEL expression into a policy callable.

    The expression must return a map with a ``result`` key
    (``"DENY"``, ``"ASK"``, or ``"ALLOW"``) and an optional
    ``"reason"`` and/or ``"state_updates"`` key. Returning ``null``
    or a map without a valid ``result`` abstains (ALLOW). Other
    non-decision values (e.g. a bare ``bool``) and unknown ``result``
    values also abstain, and log a warning once per expression.
    ``state_updates`` must be a list of maps shaped
    ``{"key": string, "action": "set"|"increment"|"delete"|"append", "value": any}``.
    ``value`` is required for ``set``, ``increment``, and ``append``,
    and ignored for ``delete``.

    :param expression: CEL expression evaluated per policy event.
        The ``event`` variable is the full
        :class:`~omnigent.policies.schema.PolicyEvent` dict.
        Must return a map, e.g.::

            event.type == "tool_call"
              ? {"result": "ASK", "reason": "Approve?"}
              : {"result": "ALLOW"}

            {"result": "ALLOW",
             "state_updates": [
                 {"key": "call_count", "action": "increment", "value": 1}
             ]}

    :param reason: Fallback reason for DENY/ASK results when
        the map omits a ``"reason"`` key, e.g.
        ``"Shell access is blocked."``.
    :returns: A policy callable following the
        :class:`PolicyCallable` contract.
    :raises ValueError: If the expression has CEL syntax errors.
    """
    if celpy is None:
        raise ImportError(
            "cel-python is required for CEL policies but is not installed. "
            "Install it with: pip install cel-python"
        )

    # Environment() raises the recursion limit deep expressions need; cache
    # hits skip it, so keep that side effect on every build.
    Environment()
    try:
        prog = _compile(expression)
    except CELParseError as exc:
        _log.warning("CEL compile error: %s", exc)
        raise ValueError(f"CEL policy: compile error in expression: {exc}") from exc

    _result_key = celpy.celtypes.StringType("result")
    _reason_key = celpy.celtypes.StringType("reason")
    _state_updates_key = celpy.celtypes.StringType("state_updates")

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        # llm_client is a live object used by Python policy callables;
        # CEL expressions cannot call methods on it and json_to_cel would
        # raise ValueError trying to convert it.
        cel_event = {k: v for k, v in event.items() if k != "llm_client"}
        # REQUEST-phase ``data`` is now the structured dict
        # ({"user_content", "attachments"}) from the input gate, but CEL
        # expressions authored against the request phase expect the user text as
        # a bare string (e.g. ``event.data.contains("secret")``). Project it back
        # to that string so existing request-phase CEL policies keep matching — a
        # map here would silently fail-open (string ops raise -> abstain -> ALLOW,
        # and ``==`` against a string is just false).
        if event.get("type") == "request":
            cel_event["data"] = request_user_text(event.get("data"))
        try:
            result = prog.evaluate({"event": json_to_cel(cel_event)})
        except (CELEvalError, ValueError, TypeError):
            _log.debug(
                "CEL policy eval error on event type %r, abstaining",
                event.get("type"),
            )
            return None

        if not isinstance(result, celpy.celtypes.MapType):
            if result is not None and not isinstance(result, celpy.celtypes.NullType):
                _warn_non_decision(expression, f"a {type(result).__name__} instead of a map")
            return None

        if _result_key not in result:
            return None
        verdict = str(result[_result_key]).upper()
        if verdict not in ("DENY", "ASK", "ALLOW"):
            # Name only the type: the value can carry event content.
            result_type = type(result[_result_key]).__name__
            _warn_non_decision(expression, f"a {result_type} result other than DENY, ASK or ALLOW")
            return None

        out: PolicyResponse = {"result": verdict}  # type: ignore[typeddict-item]
        if _reason_key in result:
            out["reason"] = str(result[_reason_key])
        elif verdict != "ALLOW":
            out["reason"] = reason
        if _state_updates_key in result:
            state_updates = _plain_cel_value(result[_state_updates_key])
            if not isinstance(state_updates, list):
                raise TypeError("CEL policy: state_updates must be a list of maps.")
            # Approval checkpoints may only be written by trusted runtime policies.
            for update in state_updates:
                if isinstance(update, dict):
                    key = update.get("key")
                    if isinstance(key, str) and is_reserved_state_key(key):
                        raise ValueError("CEL policy: state_updates cannot change reserved keys.")
            out["state_updates"] = state_updates  # type: ignore[typeddict-item]
        return out

    phases = _phases_with_effect(expression)
    return evaluate if phases is None else acts_on(*phases)(evaluate)


@functools.lru_cache(maxsize=_PROGRAM_CACHE_SIZE)
def _compile(expression: str) -> Runner:
    """
    Parse and program a CEL expression, memoised per process.

    The interpreted runner is reentrant: each ``evaluate`` call builds its
    own evaluator and activation and only reads the shared AST. Parse
    errors are not cached, so invalid expressions raise on every call.

    :param expression: CEL source text.
    :returns: A runner ready to evaluate.
    :raises CELParseError: If the expression has syntax errors.
    """
    env = Environment()
    return env.program(env.compile(expression))


# Parser rules that wrap a single child; the phase analysis looks through them.
_WRAPPER_RULES = frozenset(
    {
        "expr",
        "conditionalor",
        "conditionaland",
        "relation",
        "addition",
        "multiplication",
        "unary",
        "member",
        "primary",
        "paren_expr",
    }
)

# A value the phase analysis cannot know without the rest of the event.
_UNKNOWN = object()


@functools.lru_cache(maxsize=_PROGRAM_CACHE_SIZE)
def _phases_with_effect(expression: str) -> frozenset[Phase] | None:
    """
    Return the phases on which an expression might return a decision.

    An expression abstains on a phase when, with ``event.type`` fixed to that
    phase, it is known to yield something other than a map, such as the
    ``null`` branch of ``event.type == "tool_call" && ... ? {...} : null``.
    Anything the analysis cannot follow counts as a possible decision.

    :param expression: A CEL source that :func:`_compile` accepts.
    :returns: The phases left, or ``None`` when no phase can be ruled out.
    """
    ast = _compile(expression).ast
    phases = frozenset(phase for phase in Phase if _static_value(ast, phase.value) is _UNKNOWN)
    return None if phases == frozenset(Phase) else phases


def _static_value(node: object, event_type: str) -> object:
    """
    Evaluate the part of a CEL parse tree that depends only on ``event.type``.

    :param node: A ``lark`` tree node or token from the CEL parser.
    :param event_type: The value of ``event.type``, e.g. ``"tool_result"``.
    :returns: A ``str``, ``bool``, ``None`` (CEL ``null``) or ``list`` the node
        evaluates to whatever the rest of the event holds, else ``_UNKNOWN``.
        A node that may raise is ``_UNKNOWN`` unless every outcome is known.
    """
    while (
        getattr(node, "data", None) in _WRAPPER_RULES and len(getattr(node, "children", ())) == 1
    ):
        node = node.children[0]  # type: ignore[attr-defined]
    kind = getattr(node, "data", None)
    children: list[object] = list(getattr(node, "children", ()))
    if kind == "literal" and len(children) == 1:
        return _literal_value(children[0])
    if kind == "member_dot" and len(children) == 2:
        target = children[0]
        while getattr(target, "data", None) in _WRAPPER_RULES and len(target.children) == 1:  # type: ignore[attr-defined]
            target = target.children[0]  # type: ignore[attr-defined]
        is_event = getattr(target, "data", None) == "ident" and [
            str(child)
            for child in target.children  # type: ignore[attr-defined]
        ] == ["event"]
        return event_type if is_event and str(children[1]) == "type" else _UNKNOWN
    if kind == "expr" and len(children) == 3:
        condition = _static_value(children[0], event_type)
        if isinstance(condition, bool):
            return _static_value(children[1] if condition else children[2], event_type)
        branches = (_static_value(children[1], event_type), _static_value(children[2], event_type))
        # A condition that errs or is not a bool makes the whole expression err.
        return None if _UNKNOWN not in branches else _UNKNOWN
    if kind in ("conditionalor", "conditionaland") and len(children) == 2:
        # CEL's && and || are commutative over errors: false && x is false.
        absorbing = kind == "conditionalor"
        operands = [_static_value(child, event_type) for child in children]
        if any(operand is absorbing for operand in operands):
            return absorbing
        if all(operand is (not absorbing) for operand in operands):
            return not absorbing
        return _UNKNOWN
    if kind == "unary" and len(children) >= 2:
        operators = children[:-1]
        if not all(getattr(op, "data", None) == "unary_not" for op in operators):
            return _UNKNOWN
        operand = _static_value(children[-1], event_type)
        if not isinstance(operand, bool):
            return _UNKNOWN
        return operand if len(operators) % 2 == 0 else not operand
    if kind == "relation" and len(children) == 2:
        operator = children[0]
        operator_kind = getattr(operator, "data", None)
        if len(getattr(operator, "children", ())) != 1:
            return _UNKNOWN
        left = _static_value(operator.children[0], event_type)  # type: ignore[attr-defined]
        right = _static_value(children[1], event_type)
        if operator_kind == "relation_in" and isinstance(left, str) and isinstance(right, list):
            return left in right
        if isinstance(left, str) and isinstance(right, str):
            if operator_kind == "relation_eq":
                return left == right
            if operator_kind == "relation_ne":
                return left != right
        return _UNKNOWN
    if kind == "list_lit":
        items = [
            _static_value(item, event_type)
            for exprlist in children
            for item in getattr(exprlist, "children", ())
        ]
        return items if all(isinstance(item, str) for item in items) else _UNKNOWN
    return _UNKNOWN


def _literal_value(token: object) -> object:
    """
    Return a plain ``null``, ``bool`` or escape-free string literal's value.

    :param token: The literal's ``lark`` token, e.g. ``'"tool_call"'``.
    :returns: The value, or ``_UNKNOWN`` for other literals.
    """
    kind = getattr(token, "type", None)
    text = str(token)
    if kind == "NULL_LIT":
        return None
    if kind == "BOOL_LIT":
        return text == "true"
    if kind == "STRING_LIT" and len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        body = text[1:-1]
        if text[0] not in body and "\\" not in body:
            return body
    return _UNKNOWN


@functools.lru_cache(maxsize=_PROGRAM_CACHE_SIZE)
def _warn_non_decision(expression: str, description: str) -> None:
    """
    Warn that an expression returned a value that is not a decision.

    Memoised so each distinct problem logs once per process rather than on
    every event, since policy engines are rebuilt for every evaluation.

    :param expression: CEL source text, truncated in the log message.
    :param description: What the expression returned, e.g.
        ``"a BoolType instead of a map"``.
    """
    _log.warning(
        "CEL policy expression returned %s, so the policy abstained. "
        'Return a map such as {"result": "DENY"}. Expression: %.200s',
        description,
        expression,
    )


def _plain_cel_value(value: object) -> object:
    """
    Convert CEL wrapper values back to JSON-like Python values.

    :param value: A value returned from ``celpy`` evaluation.
    :returns: A plain ``dict``/``list``/scalar value suitable for
        :class:`FunctionPolicy` response coercion.
    """
    if celpy is not None:
        if isinstance(value, celpy.celtypes.BoolType):
            return bool(value)
        if isinstance(value, celpy.celtypes.NullType):
            return None
        if isinstance(value, celpy.celtypes.StringType):
            return str(value)
        if isinstance(value, celpy.celtypes.BytesType):
            return bytes(value)
        if isinstance(value, (celpy.celtypes.IntType, celpy.celtypes.UintType)):
            return int(value)
        if isinstance(value, celpy.celtypes.DoubleType):
            return float(value)
    if isinstance(value, Mapping):
        return {str(_plain_cel_value(k)): _plain_cel_value(v) for k, v in value.items()}
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, Sequence):
        return [_plain_cel_value(item) for item in value]
    return value


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, object]] = (
    []
    if celpy is None
    else [
        {
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "kind": "factory",
            "name": "CEL Expression Policy",
            "description": (
                "Evaluate a CEL (Common Expression Language) expression against "
                "every policy event. The expression receives the full event as "
                '`event` and must return a map with `result` ("DENY", "ASK", or '
                '"ALLOW") plus optional `reason` and/or `state_updates` keys. '
                "`state_updates` must be a list of maps with `key`, `action`, "
                'and `value`; valid actions are "set", "increment", "delete", '
                'and "append". `value` is ignored for "delete". '
                "CEL is non-Turing-complete and side-effect-free."
            ),
            "params_schema": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "CEL expression. The `event` variable holds the PolicyEvent dict. "
                            "Must return a map: "
                            '{"result": "DENY"|"ASK"|"ALLOW", "reason": "...", '
                            '"state_updates": [...]}. '
                            "`state_updates` must be a list of maps shaped "
                            '{"key": string, "action": '
                            '"set"|"increment"|"delete"|"append", "value": any}; '
                            "`value` is required for set/increment/append and ignored "
                            "for delete. "
                            "Keys beginning with `_policy_` are reserved for the runtime. "
                            "Attempting to update one denies the event and discards this "
                            "CEL result's entire state-update list. "
                            "Event fields: "
                            'event.type ("request"|"tool_call"|"tool_result"|'
                            '"response"|"llm_request"|"llm_response"|"output_logged"); '
                            "event.target (tool name on tool_call/tool_result, null otherwise); "
                            "event.data (phase-specific: string for request/response, "
                            '{"name": str, "arguments": map} for tool_call, '
                            '{"result": any} for tool_result, '
                            '{"model": str, "messages_count": int, "tools_count": int,'
                            ' "system_prompt_preview": str, "last_user_message": str}'
                            " for llm_request); "
                            "event.context.actor.run_as (user email); "
                            "event.context.usage.total_cost_usd (session spend). "
                            "Example: "
                            'event.type == "tool_call" && event.data.name == "sys_os_shell" '
                            '? {"result": "DENY", "reason": "Shell blocked."} '
                            ': {"result": "ALLOW"}'
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Fallback reason for DENY/ASK when the map omits a reason key."
                        ),
                        "default": "Denied by policy.",
                    },
                },
                "required": ["expression"],
            },
        },
    ]
)
