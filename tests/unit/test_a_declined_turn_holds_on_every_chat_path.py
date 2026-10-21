"""A turn's agent-work answer holds on every /chat path, and a turn starts clean.

Task #120, third round.  The review of df2474851 measured three behaviour
changes of the second round that no test pinned, each on the real /chat:

1. A Creation Mode or Plan Mode flag left on a reused worker thread by an
   EARLIER turn routed the next turn: on the parent a plain question was
   answered with the creation question and ``Agent_status`` 'Creation Mode'
   (or 'Plan Mode'); the handlers after get_ans read the flags, never who set
   them.  /chat now clears both when a turn starts.
2. With ``HEVOLVE_DRAFT_FIRST=1`` in the node's env (it overrides the body's
   ``draft_first`` False), the draft classifier's confident create verdict
   routed a turn that declined agent work into CREATE: a paid call got the
   interview as its billed answer.  The create route now also requires the
   turn's agent-tools answer; a person's turn still reaches Creation Mode.
3. /chat read its body field against five spellings, so 'no' and 'FALSE' did
   not decline; it reads the one boolean reader now
   (core.config_cache.flag_value).

Each test drives the real /chat handler with its two answer paths stubbed
apart (the plain answer, get_ans; the agent-building turn, gather_info), the
pattern of tests/unit/test_metered_chat_is_a_conversation.py, with the draft
dispatcher under the test's control and every outbound side effect stubbed.
"""
import json
import os
import sys
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

PLAIN = 'Paris is the capital of France.'
QUESTION = 'What should I call it?'
DRAFT_SAYS_CREATE = {'response': 'I will set that agent up.', 'draft_confidence': 0.95,
                     'is_create_agent': True, 'delegate': 'local', 'is_casual': False}


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


def _turn_allows_agent_work(hie, get_ans_kwargs):
    """The turn's agent-work answer as get_tools would read it: the request
    thread-local (df2474851 and later); on an older tree, where the flag
    travelled to get_ans as a keyword, that keyword (so these tests fail on
    behaviour there, not on a missing name)."""
    reader = getattr(hie.thread_local_data, 'get_agent_tools', None)
    if reader is not None:
        return reader()
    return get_ans_kwargs.get('agent_tools', True)


@pytest.fixture(autouse=True)
def _a_turn_that_allows_everything(hie):
    """The thread the test client runs the request on is this one: leave it
    the way a person's turn leaves it."""
    yield
    setter = getattr(hie.thread_local_data, 'set_agent_tools', None)
    if setter is not None:
        setter(True)
    hie.thread_local_data.clear_creation_flags()
    hie.thread_local_data.clear_agentic_flags()


def _body(**fields):
    body = {'user_id': 'decline_u1', 'prompt': 'What is the capital of France?',
            'prompt_id': None, 'request_id': 'decline-r1', 'preferred_lang': 'en'}
    body.update(fields)
    return body


def _chat(hie, monkeypatch, body, draft=None, before=None):
    """POST ``body`` to the real /chat.  ``draft`` is what the dispatcher
    answers (silent by default, so the turn takes the normal path);
    ``before`` runs on the request thread just before the post (what an
    earlier turn left behind).  Returns (status, json, seen): ``seen`` is
    what get_ans and gather_info saw."""
    import hartos.gather_agentdetails as gad
    from integrations.social import chat_messages as _cm

    monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
    monkeypatch.setattr(hie, '_persist_language', lambda *a, **k: None)
    monkeypatch.setattr(hie, '_cleanup_stale_agents', lambda: None)
    monkeypatch.setattr(hie, '_tts_synthesize_and_publish', lambda *a, **k: None)
    monkeypatch.setattr(_cm, 'persist_and_publish_async', lambda *a, **k: None)
    monkeypatch.setattr(hie, 'publish_async', lambda *a, **k: None)
    # The Crossbar HTTP leg itself, not only the name: the message bus's
    # HTTP transport calls _http_crossbar_publish directly, and
    # core.safe_hartos_attr resolves publish_async through the
    # hart_intelligence facade when an earlier test in the process loaded
    # it, past the stub above (a batch run left two publish threads blocked
    # on the desktop bus's socket and pytest's hang guard forced the exit).
    monkeypatch.setattr(hie, '_http_crossbar_publish', lambda *a, **k: None)
    monkeypatch.setattr(hie, '_tune_resonance_after_chat', lambda *a, **k: {})
    # The create path's own side effects: the cloud record lookup for a new
    # agent (offline here), the lifecycle record and the flowchart push.
    def _no_cloud(*a, **k):
        raise ConnectionError('no cloud DB in this test')
    monkeypatch.setattr(hie, 'pooled_get', _no_cloud)
    monkeypatch.setattr(hie, '_record_lifecycle', lambda *a, **k: None)
    monkeypatch.setattr(hie, '_push_workflow_flowchart', lambda *a, **k: None)
    seen = {'get_ans': 0, 'gather_info': 0, 'agent_tools': None,
            'creation_flag_at_get_ans': None, 'agentic_flag_at_get_ans': None}

    def fake_get_ans(casual_conv, *a, **k):
        seen['get_ans'] += 1
        seen['agent_tools'] = _turn_allows_agent_work(hie, k)
        seen['creation_flag_at_get_ans'] = hie.thread_local_data.get_creation_requested()
        seen['agentic_flag_at_get_ans'] = hie.thread_local_data.get_agentic_requested()
        return PLAIN

    def fake_gather_info(*a, **k):
        seen['gather_info'] += 1
        return json.dumps({'status': 'pending', 'question': QUESTION})

    monkeypatch.setattr(hie, 'get_ans', fake_get_ans)
    monkeypatch.setattr(gad, 'gather_info', fake_gather_info)
    dispatcher = MagicMock()
    dispatcher.dispatch_draft_first.return_value = (
        dict(draft) if draft is not None else {'response': ''})
    with patch('integrations.agent_engine.speculative_dispatcher.'
               'get_speculative_dispatcher', return_value=dispatcher):
        if before is not None:
            before()
        r = hie.app.test_client().post('/chat', json=body)
    return r.status_code, (r.get_json() or {}), seen


# --------------------------------------------------------------------------
# 1. a flag left by an earlier turn
# --------------------------------------------------------------------------

@pytest.mark.parametrize('leftover,mode', [
    pytest.param('creation', 'Creation Mode', id='creation flag left behind'),
    pytest.param('agentic', 'Plan Mode', id='plan flag left behind'),
])
def test_a_flag_left_by_an_earlier_turn_does_not_route_this_turn(
        hie, monkeypatch, leftover, mode):
    """The handlers after get_ans read the thread-local flags and clear them
    only there; a turn that ended before them (an exception, an early
    return) left its flag for the next turn on the same worker thread.  On
    the parent this plain question came back as the creation question with
    'Creation Mode' (or as 'Plan Mode')."""
    def leave():
        if leftover == 'creation':
            hie.thread_local_data.set_creation_requested(
                description='an agent an earlier turn asked for', autonomous=False)
        else:
            hie.thread_local_data.set_agentic_routing(
                'a plan an earlier turn asked for', ['step one'], None)
    monkeypatch.delenv('HEVOLVE_DRAFT_FIRST', raising=False)
    status, reply, seen = _chat(hie, monkeypatch, _body(), before=leave)
    assert status == 200, reply
    assert reply.get('Agent_status') != mode, reply
    assert reply.get('response') == PLAIN, reply
    assert seen['get_ans'] == 1 and seen['gather_info'] == 0, seen
    assert seen['creation_flag_at_get_ans'] is False
    assert seen['agentic_flag_at_get_ans'] is False


# --------------------------------------------------------------------------
# 2. the draft's create verdict on a declined turn
# --------------------------------------------------------------------------

def test_the_drafts_create_verdict_does_not_route_a_declined_turn_into_create(
        hie, monkeypatch):
    """HEVOLVE_DRAFT_FIRST=1 overrides the body's draft_first False, so the
    draft is consulted for a paid call; its confident create verdict used to
    set create_agent and the customer got the interview (the parent answered
    'Creation Mode' and called gather_info).  A declined turn reaches get_ans
    instead."""
    monkeypatch.setenv('HEVOLVE_DRAFT_FIRST', '1')
    status, reply, seen = _chat(
        hie, monkeypatch, _body(agent_tools=False, draft_first=False),
        draft=DRAFT_SAYS_CREATE)
    assert status == 200, reply
    assert reply.get('Agent_status') != 'Creation Mode', reply
    assert reply.get('response') == PLAIN, reply
    assert seen['gather_info'] == 0 and seen['get_ans'] == 1, seen
    assert seen['agent_tools'] is False


def test_a_persons_turn_still_reaches_creation_mode_on_the_drafts_verdict(
        hie, monkeypatch):
    """The control: the same verdict on a person's turn (no agent_tools
    field) still routes into CREATE, so the decline is the only thing that
    changed."""
    monkeypatch.setenv('HEVOLVE_DRAFT_FIRST', '1')
    status, reply, seen = _chat(hie, monkeypatch, _body(), draft=DRAFT_SAYS_CREATE)
    assert status == 200, reply
    assert reply.get('Agent_status') == 'Creation Mode', reply
    assert reply.get('response') == QUESTION, reply
    assert seen['gather_info'] == 1 and seen['get_ans'] == 0, seen


# --------------------------------------------------------------------------
# 3. the body field, through /chat
# --------------------------------------------------------------------------

@pytest.mark.parametrize('field,expected', [
    pytest.param('no', False, id="'no' declines"),
    pytest.param('FALSE', False, id="'FALSE' declines"),
    pytest.param(0, False, id='0 declines'),
    pytest.param(False, False, id='false declines'),
    pytest.param('nah', True, id='junk keeps both'),
    pytest.param(None, True, id='null keeps both'),
    pytest.param('absent', True, id='no field keeps both'),
])
def test_the_body_field_is_read_by_the_one_boolean_reader(hie, monkeypatch, field, expected):
    """What get_tools would read for the turn, after /chat parsed the body
    with core.config_cache.flag_value: the env flags' spellings decline, junk
    and an absent field keep a person's tools (on the parent 'no' and
    'FALSE' did not decline)."""
    monkeypatch.delenv('HEVOLVE_DRAFT_FIRST', raising=False)
    body = _body() if field == 'absent' else _body(agent_tools=field)
    status, reply, seen = _chat(hie, monkeypatch, body)
    assert status == 200, reply
    assert reply.get('response') == PLAIN, reply
    assert seen['agent_tools'] is expected, seen
