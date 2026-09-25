"""
Calculator tool — safe arithmetic evaluation, in-process (no external
service).

2026-08-19: 'calculator' had been listed as an allowed fast-tier tool in
tool_allowlist.py since before this file existed, but was never actually
implemented anywhere in the codebase (confirmed by exhaustive grep, same
gap as get_time -- see time_tool.py). Closes it the same way: a
native_handler, no HTTP server.

Uses ast.parse + a restricted node-type walk, NOT eval()/exec() -- the
expression string comes from an LLM's tool-call arguments, an untrusted
input path, and eval() on that is a real code-injection surface.
"""

import ast
import json
import operator
from typing import Optional

from .registry import ServiceToolInfo, service_tool_registry

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node):
    """Evaluate an ast node restricted to arithmetic -- numbers, +-*/, //,
    %, **, unary +/-, and parentheses (ast handles grouping natively).
    Anything else (names, calls, attributes, subscripts, comparisons, ...)
    raises, which the caller turns into a clear error rather than ever
    reaching Python's real eval()."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f'non-numeric constant: {node.value!r}')
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f'unsupported expression element: {type(node).__name__}')


def _native_calculator(params_json: str) -> str:
    """Execute in-process. Returns the numeric result, or a clear error."""
    try:
        params = json.loads(params_json) if isinstance(params_json, str) else params_json
    except (json.JSONDecodeError, TypeError):
        params = {}
    if not isinstance(params, dict):
        params = {}

    expression = params.get('expression')
    if not isinstance(expression, str) or not expression.strip():
        return 'No "expression" provided -- pass a plain arithmetic expression, e.g. "12 * (7 + 3)".'

    try:
        tree = ast.parse(expression, mode='eval')
        result = _eval_node(tree.body)
    except ZeroDivisionError:
        return f'Cannot evaluate {expression!r}: division by zero.'
    except Exception as e:
        return (f'Could not evaluate {expression!r}: {e}. '
                'Only plain arithmetic is supported (+, -, *, /, //, %, **, parentheses).')

    return f'{expression} = {result}'


class CalculatorTool:
    """Register safe arithmetic evaluation as a native tool (in-process)."""

    NAME = 'calculator'

    @classmethod
    def create_tool_info(cls, base_url: Optional[str] = None) -> ServiceToolInfo:
        return ServiceToolInfo(
            name=cls.NAME,
            description=(
                "Evaluate a plain arithmetic expression exactly (+, -, *, "
                "/, //, %, **, parentheses). Always call this for any "
                "calculation instead of computing it mentally -- it is "
                "exact where a language model's own arithmetic is not."
            ),
            base_url=base_url or 'native://in-process',
            endpoints={
                cls.NAME: {
                    'path': f'/{cls.NAME}',
                    'method': 'POST',
                    'description': (
                        'Evaluate a plain arithmetic expression. Input: '
                        '"expression" (string), e.g. "12 * (7 + 3)". '
                        'Returns the exact numeric result.'
                    ),
                    'params_schema': {
                        'expression': {
                            'type': 'string',
                            'description': 'Arithmetic expression to evaluate',
                        },
                    },
                    'native_handler': _native_calculator,
                },
            },
            health_endpoint=None,  # No external service to check
            tags=['math', 'arithmetic', 'calculator'],
            timeout=5,
        )

    @classmethod
    def register(cls, base_url: Optional[str] = None) -> bool:
        """Register the calculator tool with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
