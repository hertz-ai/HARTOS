"""T1 floor of the 100% deployed-surface goal: DRIVE every route the real app
registers, end to end through a real test_client with the OS boundary faked,
and assert the handler returns a CONTROLLED response.

Controlled means: any deliberate status (2xx success, 3xx redirect, 4xx
validation/auth refusal, 501/503 subsystem-unavailable) -- but NEVER a 500. A
500 here is an unhandled exception inside a deployed handler on a plain
request, i.e. a real crash a user can trigger; the no-silent-gulp rule says
those must be handled + logged, not thrown raw at the client.

Deeper per-domain behavioural tests (asserting WHICH os command a handler
issues, response semantics, state changes) live in the test_deep_*.py sibling
files; this file guarantees no deployed route exists untested.
"""
import pytest

ALLOWED = set(range(200, 500)) | {501, 503}      # controlled; 500 = crash

# Deterministic stand-ins for path parameters, by converter/name heuristics.
_PARAM_DEFAULTS = {
    'int': '1',
    'float': '1.0',
    'path': 'probe.txt',
}


def _fill_params(rule):
    """Substitute every <converter:name> in a rule with a safe stand-in."""
    import re
    def sub(m):
        conv = m.group(1) or 'string'
        return _PARAM_DEFAULTS.get(conv, 'test')
    return re.sub(r'<(?:([a-z]+):)?([^>]+)>', sub, rule)


def _all_cases():
    """Enumerate (method, rule) for the WHOLE deployed surface at collection
    time, so pytest -v shows one named case per route."""
    import os as _os
    import sys as _sys
    root = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', '..', '..'))
    if root not in _sys.path:
        _sys.path.insert(0, root)
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    app = LiquidUIService()._create_flask_app()
    cases = []
    for r in app.url_map.iter_rules():
        if r.endpoint == 'static':
            continue
        for m in sorted((r.methods or set()) - {'HEAD', 'OPTIONS'}):
            cases.append((m, r.rule))
    return sorted(cases)


# Blocking SSE generators: their generator waits for an EVENT before its first
# yield, and Werkzeug's test client pulls that first chunk to fire
# start_response -- so even an unbuffered open hangs forever on an idle stream
# (empirical: the first two suite runs wedged exactly here). Drive-all cannot
# seed domain events, so these two are excluded HERE and covered GENUINELY in
# test_flow_05_events_and_sinks.py (seed an event/canned journal first so the
# generator yields, read the first chunk, close). The zz completeness gate
# still requires them hit -- by that flow test, not by a hack.
BLOCKING_STREAMS = {
    '/api/shell/system/logs/stream',
    '/api/notifications/stream',
}


@pytest.mark.parametrize('method,rule',
                         [c for c in _all_cases() if c[1] not in BLOCKING_STREAMS],
                         ids=lambda v: v if isinstance(v, str) else None)
def test_route_returns_controlled_response(client, method, rule):
    url = _fill_params(rule)
    kwargs = {}
    if method in ('POST', 'PUT', 'PATCH'):
        kwargs['json'] = {}          # minimal body; a 400 is a CONTROLLED reply
    resp = client.open(url, method=method, **kwargs)
    assert resp.status_code in ALLOWED, (
        '%s %s -> %d (unhandled exception in a DEPLOYED handler; response: %r)'
        % (method, rule, resp.status_code, resp.get_data(as_text=True)[:300]))
