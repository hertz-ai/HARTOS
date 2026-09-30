"""A system agent's persona must reach get_ans even when draft-first is silent.

The /chat persona branch (is_system_agent) puts flows[0].system_prompt into
custom_prompt and nulls prompt_id.  The custom_prompt ladder before get_ans
then fell to its bare `else: custom_prompt = Hevolve` and replaced the persona
whenever draft-first did not answer: live 2026-09-13, "Spider-Man" routed to
casual chat, the dispatcher returned confidence 0.0, and get_ans replied
"Hi! I'm Qwen, a large-scale language model".  The first version of the fix
bound the new flag only inside that branch, and every /chat without a
prompt_id then raised UnboundLocalError at the ladder (live 13:38).  The
second kept prompt_id 0 for the turn, so get_ans built its identity block
from no agent config ("You are Hevolve") and the persona reached the model
only as context; the agent's own id goes back to the turn now.

These drive the real /chat handler and the real get_ans.  Only the draft
dispatcher (made silent), memory/tools/LLM construction and the identity
builder (it records nothing and stops the turn) are stubbed.

    python -m pytest tests/unit/test_system_agent_persona_survives.py -q
"""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

PERSONA = 'You are Spider-Man, the friendly neighbourhood hero. Stay in character.'


class _Stop(Exception):
    """Raised by the stubbed identity builder: the turn got that far."""


@pytest.fixture(scope='module')
def hie():
    with pytest.MonkeyPatch.context() as mp:
        if not os.environ.get('HEVOLVE_CACHE_DIR'):
            mp.setenv('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())
        import hart_intelligence_entry
        yield hart_intelligence_entry


@pytest.fixture
def prompts_dir(tmp_path, hie, monkeypatch):
    (tmp_path / '9001.json').write_text(json.dumps({
        'name': 'Spider-Man', 'goal': 'chat as Spider-Man',
        'is_system_agent': True, 'prompt_id': 9001,
        'flows': [{'system_prompt': PERSONA}]}))
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    from core import cache_loaders
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(tmp_path))
    cache_loaders._agent_config_cache.clear()
    return tmp_path


def _body(**kw):
    body = {'user_id': 'persona_u1', 'prompt': 'hi', 'request_id': 'persona-r1',
            'preferred_lang': 'en'}
    body.update(kw)
    return body


def _drive(hie, monkeypatch, body, answer=None):
    """POST /chat through the real handler; return what reached get_ans.

    With ``answer``, get_ans returns ``answer()`` instead of running and the
    keyword arguments of the reply are recorded."""
    seen = {}

    def stop_at_identity(agent_config=None, *_a, **_k):
        seen['identity_config'] = agent_config
        raise _Stop()

    draft = MagicMock()
    draft.dispatch_draft_first.return_value = {'response': ''}  # draft did not answer
    real_get_ans = hie.get_ans

    def spy_get_ans(casual_conv, req_tool, user_id, query, custom_prompt, preferred_lang):
        seen['custom_prompt'] = custom_prompt
        seen['casual_conv'] = casual_conv
        if answer is not None:
            return answer()
        return real_get_ans(casual_conv, req_tool, user_id=user_id, query=query,
                            custom_prompt=custom_prompt, preferred_lang=preferred_lang)

    def record_reply(*_a, **kw):
        seen['reply'] = kw
        return hie.jsonify({})

    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    patches = [
        patch('integrations.agent_engine.speculative_dispatcher.get_speculative_dispatcher',
              return_value=draft),
        patch.object(hie, 'build_identity_prompt', stop_at_identity),
        patch.object(hie, 'get_memory', lambda *a, **k: MagicMock()),
        patch.object(hie, 'get_tools', lambda *a, **k: []),
        patch.object(hie, 'CustomGPT', MagicMock()),
        patch.object(hie, 'publish_chat_stage', lambda *a, **k: None),
        patch.object(hie, '_persist_language', lambda *a, **k: None),
        patch.object(hie, 'get_ans', spy_get_ans),
    ]
    if answer is not None:
        patches.append(patch.object(hie, '_chat_reply', record_reply))
    for p in patches:
        p.start()
    try:
        try:
            seen['status'] = hie.app.test_client().post('/chat', json=body).status_code
        except _Stop:
            seen['status'] = 'reached-identity'
    finally:
        for p in reversed(patches):
            p.stop()
    return seen


def test_a_system_agents_persona_reaches_get_ans_when_draft_is_silent(
        hie, prompts_dir, monkeypatch):
    seen = _drive(hie, monkeypatch, _body(prompt_id='9001', casual_conv=False))
    assert seen['status'] == 'reached-identity', seen
    assert seen['custom_prompt'] == PERSONA, 'the persona was replaced before get_ans'
    assert seen['casual_conv'] is True
    assert (seen['identity_config'] or {}).get('name') == 'Spider-Man', (
        'get_ans built the identity block from another agent config')


def test_a_turn_without_a_prompt_id_still_gets_the_default_persona(
        hie, prompts_dir, monkeypatch):
    """The ladder reads the flag on EVERY request; bound only inside the
    persona branch, this turn raised UnboundLocalError (live 13:38)."""
    seen = _drive(hie, monkeypatch, _body(prompt_id=None, casual_conv=True))
    assert seen['status'] == 'reached-identity', seen
    assert seen['custom_prompt'] == hie.Hevolve
    assert seen['identity_config'] is None


def test_a_probe_turn_keeps_the_probe_template(hie, prompts_dir, monkeypatch):
    seen = _drive(hie, monkeypatch, _body(prompt_id=None, casual_conv=True, probe=True))
    assert seen['status'] == 'reached-identity', seen
    assert seen['custom_prompt'] == hie.PROBE_TEMPLATE


def test_the_persona_does_not_carry_into_the_next_request(hie, prompts_dir, monkeypatch):
    first = _drive(hie, monkeypatch, _body(prompt_id='9001'))
    second = _drive(hie, monkeypatch,
                    _body(prompt_id=None, casual_conv=True, request_id='persona-r2'))
    assert first['custom_prompt'] == PERSONA
    assert second['custom_prompt'] == hie.Hevolve
    assert second['identity_config'] is None


def test_a_plan_proposed_in_a_system_agents_chat_gets_a_new_agent_id(
        hie, prompts_dir, monkeypatch):
    """A plan proposed in the persona chat is for a NEW agent.  With the
    persona's own id, approving it would land back in the persona chat."""
    def propose_plan():
        hie.thread_local_data.set_agentic_routing('track my runs', ['log a run'], None)
        return 'Here is a plan.'
    seen = _drive(hie, monkeypatch, _body(prompt_id='9001'), answer=propose_plan)
    assert seen['reply'].get('Agent_status') == 'Plan Mode', seen
    assert str(seen['reply'].get('prompt_id')) not in ('9001', '0', 'None'), seen['reply']
