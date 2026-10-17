"""A correction reaches HevolveAI WITH its provenance (hevolveai C323, Master 11.460).

WorldModelBridge.submit_correction takes a ``context`` (who asked, which
agent, which memory) and lost it on both paths before reaching the learner:
the in-process call to send_expert_correction never passed it, and on the HTTP
path HevolveAI's CorrectionRequest had no ``context`` field, so pydantic
dropped it. HevolveAI now files a correction's fact under its source and
judges that source against what the node later observes (earned trust), so
the provenance is the input the trust is earned for.

An installed HevolveAI older than C323 has no ``provenance`` parameter: the
correction must still be sent, without it, rather than lost to a TypeError.

These tests call the real submit_correction; only the in-process
send_expert_correction and the HTTP post are stubbed.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from integrations.agent_engine.world_model_bridge import WorldModelBridge

CONTEXT = {'user_id': 'u-1', 'source_agent': 'agent-7', 'memory_id': 'm-42'}


def _install(monkeypatch, fn):
    mod = types.ModuleType('hevolveai.embodied_ai.rl_ef')
    mod.send_expert_correction = fn
    for name in ('hevolveai', 'hevolveai.embodied_ai'):
        monkeypatch.setitem(sys.modules, name,
                            sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, 'hevolveai.embodied_ai.rl_ef', mod)


def _in_process_bridge():
    bridge = WorldModelBridge()
    bridge._in_process = True
    bridge._provider = object()
    bridge._http_disabled = True
    return bridge


def test_in_process_correction_passes_its_context_as_provenance(monkeypatch):
    seen = {}

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance=None):
        seen.update(provenance=provenance, expert_id=expert_id)
        return {'success': True, 'learned': True, 'fact_ids': ['f1']}

    _install(monkeypatch, send_expert_correction)
    out = _in_process_bridge().submit_correction(
        'London', 'Paris', expert_id='u-1', context=dict(CONTEXT))
    assert seen['provenance'] == CONTEXT
    assert seen['expert_id'] == 'u-1'
    assert out['learned'] is True and out['fact_ids'] == ['f1']


def test_in_process_correction_without_context_sends_no_provenance(monkeypatch):
    seen = {}

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance='unset'):
        seen['provenance'] = provenance
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    _in_process_bridge().submit_correction('London', 'Paris')
    assert seen['provenance'] == 'unset'


def test_an_older_hevolveai_still_receives_the_correction(monkeypatch):
    """No provenance parameter (a build before C323): sent without it."""
    calls = []

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None):
        calls.append(corrected_response)
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    bridge = _in_process_bridge()
    out = bridge.submit_correction('London', 'Paris', context=dict(CONTEXT))
    assert calls == ['Paris']
    assert out['learned'] is True
    assert bridge._stats['total_corrections'] == 1


def test_http_correction_body_carries_the_context():
    bridge = WorldModelBridge()
    bridge._in_process = False
    bridge._http_disabled = False
    bridge._api_url = 'http://localhost:8000'
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'success': True, 'learned': True}
    with patch('integrations.agent_engine.world_model_bridge.pooled_post',
               return_value=resp) as post:
        bridge.submit_correction('London', 'Paris', context=dict(CONTEXT))
    assert post.call_args.kwargs['json']['context'] == CONTEXT
