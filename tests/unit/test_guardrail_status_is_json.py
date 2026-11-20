"""GET /api/agent-engine/guardrails answers JSON a signed-in user can read.

COMPUTE_CAPS and WORLD_MODEL_BOUNDS have been read-only mappings
(types.MappingProxyType) since 3d468794.  jsonify cannot encode one, so the
route answered 500 to every caller who got past @require_auth: Nunba's
tests/test_chat_tts_agent_api.py measured "TypeError: Object of type
mappingproxy is not JSON serializable" with a real login token.

The route body runs here without the auth wrapper, which functools.wraps
keeps as ``__wrapped__``, so no decorator is patched for the session.

    python -m pytest tests/unit/test_guardrail_status_is_json.py -q
"""
import os
import sys

from flask import Flask

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from integrations.agent_engine.api import get_guardrail_status  # noqa: E402
from security.hive_guardrails import COMPUTE_CAPS, WORLD_MODEL_BOUNDS  # noqa: E402


def _answer():
    app = Flask(__name__)
    with app.test_request_context('/api/agent-engine/guardrails'):
        return get_guardrail_status.__wrapped__().get_json()


def test_the_status_is_json_with_the_caps_and_bounds():
    data = _answer()
    assert data['success'] is True
    assert data['compute_caps'] == dict(COMPUTE_CAPS)
    # JSON has no tuple: the prohibited categories come back as a list.
    assert data['world_model_bounds'] == {
        k: list(v) if isinstance(v, tuple) else v
        for k, v in WORLD_MODEL_BOUNDS.items()}


def test_the_caps_stay_read_only_where_they_are_defined():
    """The answer is a copy: the guardrail values themselves stay frozen."""
    _answer()
    try:
        COMPUTE_CAPS['max_influence_weight'] = 1e9
    except TypeError:
        return
    raise AssertionError('COMPUTE_CAPS became writable')
