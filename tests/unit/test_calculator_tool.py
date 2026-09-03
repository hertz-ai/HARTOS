"""Unit tests for integrations/service_tools/calculator_tool.py

2026-08-19: 'calculator' was allowlisted (tool_allowlist.py) but never
implemented -- same gap as get_time (see test_time_tool.py). Uses
ast.parse + a restricted node walk rather than eval()/exec(), since the
expression string is LLM tool-call input -- an untrusted path.
"""
import json

from integrations.service_tools.calculator_tool import (
    CalculatorTool, _native_calculator)


class TestNativeCalculator:
    def test_basic_arithmetic(self):
        result = _native_calculator(json.dumps({'expression': '12 * (7 + 3)'}))
        assert result == '12 * (7 + 3) = 120'

    def test_all_operators(self):
        cases = {
            '2 + 3': 5, '10 - 4': 6, '6 * 7': 42, '10 / 4': 2.5,
            '10 // 4': 2, '10 % 3': 1, '2 ** 10': 1024, '-5 + 2': -3,
        }
        for expr, expected in cases.items():
            result = _native_calculator(json.dumps({'expression': expr}))
            assert result == f'{expr} = {expected}', result

    def test_division_by_zero_returns_a_clear_error_not_a_crash(self):
        result = _native_calculator(json.dumps({'expression': '10 / 0'}))
        assert 'division by zero' in result

    def test_missing_expression_returns_a_clear_error(self):
        result = _native_calculator(json.dumps({}))
        assert 'No "expression" provided' in result

    def test_malformed_params_json_returns_a_clear_error_not_a_crash(self):
        result = _native_calculator('not valid json')
        assert 'No "expression" provided' in result

    # ── security: must never reach real eval()/exec() ──────────────────

    def test_rejects_function_calls(self):
        result = _native_calculator(json.dumps({
            'expression': '__import__("os").system("echo pwned")'}))
        assert 'Could not evaluate' in result
        assert 'unsupported expression element' in result

    def test_rejects_bare_names(self):
        result = _native_calculator(json.dumps({'expression': 'os'}))
        assert 'Could not evaluate' in result

    def test_rejects_attribute_access(self):
        result = _native_calculator(json.dumps({'expression': '(1).__class__'}))
        assert 'Could not evaluate' in result

    def test_rejects_string_literals(self):
        result = _native_calculator(json.dumps({'expression': '"a" * 5'}))
        assert 'Could not evaluate' in result


class TestRegistration:
    def test_create_tool_info_shape(self):
        info = CalculatorTool.create_tool_info()
        assert info.name == 'calculator'
        assert info.base_url == 'native://in-process'
        assert info.health_endpoint is None
        assert 'calculator' in info.endpoints
        ep = info.endpoints['calculator']
        for key in ('path', 'method', 'description',
                    'params_schema', 'native_handler'):
            assert key in ep
        assert ep['native_handler'] is _native_calculator

    def test_register_returns_bool(self):
        assert isinstance(CalculatorTool.register(), bool)

    def test_importable_from_service_tools_package(self):
        import integrations.service_tools as pkg
        assert pkg.CalculatorTool is CalculatorTool
        assert 'CalculatorTool' in pkg.__all__

    def test_registered_in_global_service_tool_registry(self):
        from integrations.service_tools import service_tool_registry
        CalculatorTool.register()
        funcs = service_tool_registry.get_all_tool_functions()
        assert 'calculator' in funcs
        assert funcs['calculator'](expression='1 + 1') == '1 + 1 = 2'
