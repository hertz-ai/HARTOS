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


def test_a_context_that_is_not_a_dict_does_not_lose_the_correction(monkeypatch):
    """Review of 54f203c38, MINOR-2: dict() on a string context raised inside
    the in-process try and the correction fell through to a disabled HTTP
    path. The receiver keeps only a dict, so a non-dict context is dropped and
    the correction is sent."""
    seen = {}

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance='unset'):
        seen['provenance'] = provenance
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    out = _in_process_bridge().submit_correction('London', 'Paris', context='m-42')
    assert out['learned'] is True and seen['provenance'] == 'unset'


SECRET = 'sk-ant-api03-' + 'A' * 40


def test_a_secret_in_the_context_is_redacted_on_both_paths(monkeypatch):
    """Review of 54f203c38, MINOR-3: the correction texts were redacted, the
    context was not. It is now redacted by the same content walk the replay
    path uses (edge_privacy.map_content + redact_secrets)."""
    seen = {}

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance=None):
        seen['provenance'] = provenance
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    ctx = dict(CONTEXT, note=f'key is {SECRET}')
    _in_process_bridge().submit_correction('London', 'Paris', context=ctx)
    assert SECRET not in str(seen['provenance'])
    assert seen['provenance']['memory_id'] == 'm-42'
    assert ctx['note'].endswith(SECRET), "the caller's dict is not mutated"

    bridge = WorldModelBridge()
    bridge._in_process = False
    bridge._http_disabled = False
    bridge._api_url = 'http://localhost:8000'
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'success': True, 'learned': True}
    with patch('integrations.agent_engine.world_model_bridge.pooled_post',
               return_value=resp) as post:
        bridge.submit_correction('London', 'Paris', context=dict(ctx))
    body = post.call_args.kwargs['json']
    assert SECRET not in str(body['context']) and body['context']['memory_id'] == 'm-42'


def test_an_older_build_dropping_the_provenance_is_said_once(monkeypatch, caplog):
    """Review of 54f203c38, MINOR-4: the installed production build is older
    than C323, so the drop is said, once per process, not silently."""
    import logging
    import integrations.agent_engine.world_model_bridge as wmb

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None):
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    monkeypatch.setattr(wmb, '_PROVENANCE_DROP_SAID', False, raising=False)
    bridge = _in_process_bridge()
    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        bridge.submit_correction('London', 'Paris', context=dict(CONTEXT))
        bridge.submit_correction('London', 'Paris', context=dict(CONTEXT))
    said = [r for r in caplog.records if 'provenance' in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in caplog.records]


def test_the_learner_gets_its_own_copy_of_the_context(monkeypatch):
    """Review of 54f203c38, NIT-7: the provenance handed in-process is a copy;
    what the learner does to it never reaches the caller's dict."""
    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance=None):
        provenance['memory_id'] = 'changed'
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    ctx = dict(CONTEXT)
    _in_process_bridge().submit_correction('London', 'Paris', context=ctx)
    assert ctx == CONTEXT
