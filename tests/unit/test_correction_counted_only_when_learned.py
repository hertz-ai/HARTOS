"""A correction counts only when HevolveAI says it was LEARNED (log defect M19-B).

Measured on the live node, api_server_20260922.log: all 5 corrections failed
inside HevolveAI ("No sensor encoding available"), the server still answered
HTTP 200, and WorldModelBridge.submit_correction counted every 200 as
total_corrections += 1. That counter feeds benchmark_registry,
federated_aggregator and ip_service.

HevolveAI reports two verdicts: 'success' means the correction was CAPTURED,
'learned' means the learning step ran and learned. Only 'learned' counts.
The first fix counted on 'success' and so still counted every captured-but-
not-learned correction, on the HTTP path and on the in-process path.

These tests call the real submit_correction; only the HTTP post and the
in-process send_expert_correction are stubbed.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from integrations.agent_engine.world_model_bridge import WorldModelBridge


def _http_bridge():
    bridge = WorldModelBridge()
    bridge._in_process = False
    bridge._http_disabled = False
    bridge._api_url = 'http://localhost:8000'   # local: no consent gate
    return bridge


def _reply(body):
    resp = MagicMock(status_code=200)
    resp.json.return_value = body
    return resp


# The reply HevolveAI sends since it reports its learn verdict.
FAILED = {'status': 'failed', 'success': False, 'learned': False,
          'message': 'Correction received but not learned: no sensor encoding '
                     'available: the learning step was skipped',
          'statistics': {'success': True, 'learned': False,
                         'reason': 'no sensor encoding available'}}
LEARNED = {'status': 'success', 'success': True, 'learned': True,
           'message': 'Correction received and learned',
           'statistics': {'success': True, 'learned': True, 'correction_id': 4}}
# hevolveai 2c4e622: top-level 'success' is the provider's CAPTURED flag.
CAPTURED_2C4E622 = {'status': 'success', 'success': True,
                    'message': 'Correction received and learned',
                    'statistics': {'success': True, 'correction_id': 4}}
# An older HevolveAI: always "success"; only its statistics say anything.
OLD_FAILED = {'status': 'success', 'message': 'Correction received and learned',
              'statistics': {'success': False, 'error': 'No sensor encoding'}}
OLD_SUCCESS = {'status': 'success', 'message': 'Correction received and learned',
               'statistics': {'success': True, 'correction_id': 4}}


@pytest.mark.parametrize('body,learned', [
    (FAILED, False),
    (LEARNED, True),
    # 1. 'learned' decides, and only a literal True
    ({'status': 'success', 'success': True, 'learned': 'true'}, False),
    ({'status': 'success', 'success': True, 'learned': 1}, False),
    ({'status': 'success', 'success': True, 'learned': False}, False),
    # 2. a bare top-level 'success' is captured, not learned
    (CAPTURED_2C4E622, False),
    ({'status': 'success', 'success': None,
      'statistics': {'success': True}}, False),
    # 3. older server: statistics.learned, then statistics.success
    ({'status': 'success', 'statistics': {'success': True, 'learned': True}}, True),
    ({'status': 'success', 'statistics': {'success': True, 'learned': False}}, False),
    ({'status': 'success', 'statistics': {'learned': 'true'}}, False),
    (OLD_FAILED, False),
    (OLD_SUCCESS, True),
    ({'status': 'success', 'statistics': {'success': 'true'}}, False),
    ({'status': 'success', 'statistics': {'success': 1}}, False),
    ({'status': 'success', 'statistics': 'learned'}, False),
    ({'status': 'success'}, False),
])
def test_http_correction_counts_only_a_learned_reply(body, learned):
    bridge = _http_bridge()
    with patch('integrations.agent_engine.world_model_bridge.pooled_post',
               return_value=_reply(dict(body))):
        result = bridge.submit_correction('London', 'Paris')
    assert bridge._stats['total_corrections'] == (1 if learned else 0)
    assert result['success'] is learned
    assert result['learned'] is learned


@pytest.fixture
def in_process_send(monkeypatch):
    """A stand-in for hevolveai.embodied_ai.rl_ef.send_expert_correction."""
    holder = {}
    mod = types.ModuleType('hevolveai.embodied_ai.rl_ef')
    mod.send_expert_correction = lambda **kw: holder['result']
    for name in ('hevolveai', 'hevolveai.embodied_ai'):
        monkeypatch.setitem(sys.modules, name,
                            sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, 'hevolveai.embodied_ai.rl_ef', mod)
    return holder


@pytest.mark.parametrize('result,learned', [
    # the provider since it reports its learn verdict
    ({'success': True, 'learned': True, 'correction_id': 1}, True),
    ({'success': True, 'learned': False, 'correction_id': 1,
      'reason': 'no sensor encoding available'}, False),
    ({'success': False, 'learned': False, 'error': 'boom',
      'reason': 'the correction was not captured: boom'}, False),
    ({'success': True, 'learned': 1}, False),
    # an older provider: 'success' is the captured flag, not a learn
    ({'success': True, 'correction_id': 1}, False),
    ({'success': False, 'error': 'No sensor encoding'}, False),
    # not a dict at all
    (None, False),
    ('learned', False),
    (True, False),
])
def test_in_process_correction_counts_only_a_learned_result(
        in_process_send, result, learned):
    in_process_send['result'] = result
    bridge = WorldModelBridge()
    bridge._in_process = True
    bridge._provider = object()
    bridge._http_disabled = True        # a non-dict must not fall to HTTP
    out = bridge.submit_correction('London', 'Paris')
    assert bridge._stats['total_corrections'] == (1 if learned else 0)
    assert out['success'] is learned
    assert out['learned'] is learned
