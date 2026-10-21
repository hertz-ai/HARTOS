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


@pytest.fixture(autouse=True)
def _fresh_notice(monkeypatch):
    """The drop notice is said once per process; each test starts unsaid."""
    import integrations.agent_engine.world_model_bridge as wmb
    monkeypatch.setattr(wmb, '_PROVENANCE_DROP_SAID', False, raising=False)

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
    """Review of 54f203c38, MINOR-4: an in-process build older than C323
    drops the provenance; that is said, once per process, not silently."""
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


def _http_bridge():
    bridge = WorldModelBridge()
    bridge._in_process = False
    bridge._http_disabled = False
    bridge._api_url = 'http://localhost:8000'
    return bridge


def test_a_value_under_a_secret_named_key_is_withheld(monkeypatch):
    """Review of ea58905, MINOR-B: a value under a secret-named key that no
    secret pattern recognises passed raw. It is withheld by the walk's own
    secret policy (edge_privacy.scrub_secret), on both paths."""
    seen = {}

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance=None):
        seen['provenance'] = provenance
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    ctx = dict(CONTEXT, api_key='opaque-value-hunter2xyz')
    _in_process_bridge().submit_correction('London', 'Paris', context=dict(ctx))
    assert 'hunter2' not in str(seen['provenance'])
    assert seen['provenance']['memory_id'] == 'm-42'
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'success': True, 'learned': True}
    with patch('integrations.agent_engine.world_model_bridge.pooled_post',
               return_value=resp) as post:
        _http_bridge().submit_correction('London', 'Paris', context=dict(ctx))
    assert 'hunter2' not in str(post.call_args.kwargs['json']['context'])


def test_a_context_that_cannot_be_walked_does_not_cost_the_correction(monkeypatch):
    """Review of ea58905, NIT-C: a cyclic context raised RecursionError out of
    submit_correction. It is dropped instead, and the correction is sent."""
    calls = []

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance='unset'):
        calls.append(provenance)
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    cyc = {'memory_id': 'm-42'}
    cyc['self'] = cyc
    out = _in_process_bridge().submit_correction('London', 'Paris', context=cyc)
    assert out['learned'] is True and calls == ['unset']
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'success': True, 'learned': True}
    with patch('integrations.agent_engine.world_model_bridge.pooled_post',
               return_value=resp) as post:
        _http_bridge().submit_correction('London', 'Paris', context=cyc)
    assert 'context' not in post.call_args.kwargs['json']


def test_an_older_server_ignoring_the_context_is_said_once(caplog):
    """Review of ea58905, MINOR-A, and of 81a1b36, MINOR-1: production's
    bridge talks HTTP, where a HevolveAI before C323 has no context field and
    ignores it silently. A C323 server says so on every 200 reply
    (context_received), including the replies that name no fact source (a
    capture that raised, no provider for the domain). A reply without the field,
    while a context was sent, is said once."""
    import logging

    def reply(body):
        resp = MagicMock(status_code=200)
        resp.json.return_value = body
        return resp

    c323_replies = [
        {'status': 'success', 'learned': True, 'context_received': True,
         'statistics': {'learned': True, 'source': 'expert:u-1'}},
        {'status': 'failed', 'learned': False, 'context_received': True,
         'statistics': {'success': False, 'learned': False, 'error': 'boom'}},
    ]
    with caplog.at_level(logging.INFO, logger='hevolve_social'):
        for body in c323_replies:
            with patch('integrations.agent_engine.world_model_bridge.pooled_post',
                       return_value=reply(body)):
                _http_bridge().submit_correction('London', 'Paris', context=dict(CONTEXT))
        assert not [r for r in caplog.records if 'provenance' in r.getMessage()]
        for _ in range(2):
            with patch('integrations.agent_engine.world_model_bridge.pooled_post',
                       return_value=reply({'status': 'success', 'learned': True,
                                           'statistics': {'learned': True,
                                                          'source': 'expert:u-1'}})):
                _http_bridge().submit_correction('London', 'Paris', context=dict(CONTEXT))
    said = [r for r in caplog.records if 'provenance' in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in caplog.records]


def test_one_secret_rule_for_the_replay_and_the_correction():
    """Review of 81a1b36, MINOR-2: the correction context withheld a value
    under a secret-named key, the replay path (redact_experience) sent it raw.
    Both now ask edge_privacy.redact_secret_leaves."""
    from security.edge_privacy import redact_secret_leaves
    from security.secret_redactor import redact_experience
    data = {'attribution_chain': {'api_key': 'opaque-value-hunter2xyz',
                                  'step': 'search', 'token': 'none'},
            'prompt': 'hello', 'response': 'world', 'user_id': 'u-1'}
    out, n = redact_secret_leaves(data)
    assert 'hunter2' not in str(out) and n >= 1
    assert out['attribution_chain']['token'] == 'none', "a status word stays readable"
    assert out['attribution_chain']['step'] == 'search' and data['attribution_chain'][
        'api_key'] == 'opaque-value-hunter2xyz', "the input is not mutated"
    replayed = redact_experience(dict(data))
    assert 'hunter2' not in str(replayed['attribution_chain'])


def test_a_dropped_context_is_said(monkeypatch, caplog):
    """Review of 81a1b36, NIT-3: a context dropped because it could not be
    redacted is said at WARNING, not only at debug."""
    import logging

    def send_expert_correction(domain, original_response, corrected_response,
                               expert_id, confidence=1.0, explanation=None,
                               valid_until=None, signed_reward=None,
                               provenance='unset'):
        return {'success': True, 'learned': True}

    _install(monkeypatch, send_expert_correction)
    cyc = {'memory_id': 'm-42'}
    cyc['self'] = cyc
    with caplog.at_level(logging.WARNING, logger='hevolve_social'):
        _in_process_bridge().submit_correction('London', 'Paris', context=cyc)
    assert [r for r in caplog.records if 'context dropped' in r.getMessage()]
