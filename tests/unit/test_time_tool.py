"""Unit tests for integrations/service_tools/time_tool.py

2026-08-19: 'get_time' was allowlisted (tool_allowlist.py) but never
implemented -- live consequence: asked "what time is it right now" at
13:27 IST, the model fabricated "10:42 AM" with full confidence, no tool
attempted. This pins the real implementation that closes that gap.
"""
import json
from datetime import datetime

from integrations.service_tools.time_tool import TimeTool, _native_get_time


class TestNativeGetTime:
    def test_returns_the_real_current_time(self):
        """The whole point: the answer must be the REAL clock, not a
        model guess. Compare against Python's own now() within a
        generous tolerance for test execution time."""
        from zoneinfo import ZoneInfo
        result = _native_get_time(json.dumps({'timezone': 'UTC'}))
        real_now = datetime.now(ZoneInfo('UTC'))
        assert real_now.strftime('%Y-%m-%d') in result
        assert real_now.strftime('%H:%M') in result  # minute-precision match

    def test_defaults_to_asia_kolkata_when_no_timezone_given(self):
        result = _native_get_time(json.dumps({}))
        assert 'Asia/Kolkata' in result

    def test_always_includes_utc_for_disambiguation(self):
        result = _native_get_time(json.dumps({'timezone': 'America/New_York'}))
        assert 'UTC:' in result

    def test_honors_an_explicit_timezone(self):
        result = _native_get_time(json.dumps({'timezone': 'UTC'}))
        assert '(UTC)' in result

    def test_bad_timezone_returns_a_clear_error_not_a_crash(self):
        result = _native_get_time(json.dumps({'timezone': 'Not/AZone'}))
        assert 'Could not resolve timezone' in result
        assert 'Not/AZone' in result

    def test_malformed_params_json_falls_back_to_default(self):
        """A native_handler always receives a JSON string per the
        registry contract (registry.py: native_handler(json.dumps(kwargs)));
        garbage input must degrade gracefully, never raise."""
        result = _native_get_time('not valid json')
        assert 'Asia/Kolkata' in result


class TestRegistration:
    def test_create_tool_info_shape(self):
        info = TimeTool.create_tool_info()
        assert info.name == 'get_time'
        assert info.base_url == 'native://in-process'
        assert info.health_endpoint is None
        assert 'get_time' in info.endpoints
        ep = info.endpoints['get_time']
        for key in ('path', 'method', 'description',
                    'params_schema', 'native_handler'):
            assert key in ep
        assert ep['native_handler'] is _native_get_time

    def test_register_returns_bool(self):
        assert isinstance(TimeTool.register(), bool)

    def test_importable_from_service_tools_package(self):
        """Wiring check (mirrors SeoAuditTool's) -- must be exported by
        the package __init__ so create_recipe/reuse_recipe can register
        it, or the tool stays dormant despite existing."""
        import integrations.service_tools as pkg
        assert pkg.TimeTool is TimeTool
        assert 'TimeTool' in pkg.__all__

    def test_registered_in_global_service_tool_registry(self):
        from integrations.service_tools import service_tool_registry
        TimeTool.register()
        funcs = service_tool_registry.get_all_tool_functions()
        assert 'get_time' in funcs
        # Flat name (endpoint key == tool name), matching the
        # already-existing tool_allowlist.py entry 'get_time' exactly --
        # not 'get_time_get_time'.
        assert funcs['get_time']() is not None
