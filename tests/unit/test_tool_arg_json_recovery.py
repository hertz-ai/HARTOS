"""#134: the 4B sometimes emits prose-wrapped / fenced tool-call arguments
instead of bare JSON. The custom function executor (helper.py:2949-2967) tries
autogen's strict json.loads first, then falls back to retrieve_json (the
canonical lenient extractor), then a graceful error message — so a "prose-as-
args" turn RECOVERS instead of 500-ing. (Live rate is 0 on the current b9581
model; this guards the recovery mechanism that historically mitigated the 52%.)

Behavioral: drive retrieve_json (the recovery leg) with the messy forms the 4B
produces; assert usable JSON comes back.
"""
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from helper import retrieve_json  # noqa: E402


def _as_dict(x):
    if isinstance(x, str):
        return json.loads(x)
    return x


def test_recovers_clean_json():
    assert _as_dict(retrieve_json('{"city": "Paris"}')) == {'city': 'Paris'}


def test_recovers_fenced_json_block():
    out = retrieve_json('```json\n{"q": "weather", "n": 3}\n```')
    assert _as_dict(out) == {'q': 'weather', 'n': 3}


def test_recovers_prose_wrapped_json():
    out = retrieve_json(
        'Sure! Here are the arguments: {"city": "Paris", "days": 3}')
    assert _as_dict(out) == {'city': 'Paris', 'days': 3}
