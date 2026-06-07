"""#114: ONE tool-log impl serves both contracts — the autogen JSON envelope
(default) and the LangChain plain-string error + Tool.name override. Behavioural:
call the real log_tool_execution on raising/normal funcs and observe the returned
string + the name handed to the UI emit. No grep tests.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.tool_logging as tl  # noqa: E402
from core.tool_logging import log_tool_execution  # noqa: E402


def _capture_emits(monkeypatch):
    seen = []
    monkeypatch.setattr(tl, '_emit_tool_call_stage', lambda n: seen.append(n))
    return seen


def test_default_returns_json_envelope_on_error(monkeypatch):
    _capture_emits(monkeypatch)

    @log_tool_execution
    def boom(x):
        raise ValueError('nope')

    out = boom('a')
    assert out.startswith('Tool execution failed:')       # autogen envelope
    assert '"error_type": "ValueError"' in out
    assert '"tool_function": "boom"' in out


def test_plain_errors_returns_plain_string(monkeypatch):
    _capture_emits(monkeypatch)

    def boom(x):
        raise ValueError('nope')

    wrapped = log_tool_execution(boom, name='Search CRM', plain_errors=True)
    out = wrapped('a')
    assert out == "Tool 'Search CRM' encountered an error: nope"   # LangChain


def test_name_override_drives_the_ui_emit(monkeypatch):
    seen = _capture_emits(monkeypatch)
    wrapped = log_tool_execution(lambda x: 'ok', name='Search CRM',
                                 plain_errors=True)
    assert wrapped('a') == 'ok'
    assert seen == ['Search CRM']      # the Tool.name, not the closure name


def test_default_name_is_func_name(monkeypatch):
    seen = _capture_emits(monkeypatch)

    @log_tool_execution
    def my_tool(x):
        return 'ok'

    assert my_tool('a') == 'ok'
    assert seen == ['my_tool']


def test_success_coerces_nonstring(monkeypatch):
    _capture_emits(monkeypatch)

    @log_tool_execution
    def numty(x):
        return 42

    assert numty('a') == '42'          # autogen contract: string result


if __name__ == '__main__':
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    mp = _MP()
    test_default_returns_json_envelope_on_error(mp); print('PASS envelope')
    test_plain_errors_returns_plain_string(mp); print('PASS plain')
    test_name_override_drives_the_ui_emit(mp); print('PASS name-emit')
    test_default_name_is_func_name(mp); print('PASS default-name')
    test_success_coerces_nonstring(mp); print('PASS coerce')
    print('OK 5/5')
