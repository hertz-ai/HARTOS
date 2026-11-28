"""A message sent while the agent is busy is kept and read, never dropped.

Measured live 2026-11-26 on the desktop (server.log.1, build #5), the speech
agent 95568353503 being built for user 10202: at 10:52:26, 10:55:43,
11:06:28 and 11:21:37 IST the owner's message was answered "This conversation
is still answering an earlier message, so this message was not read. Please
send the same message again once that answer arrives." (another turn of the
build was running a tool: ask_person waiting on a file_request card, and at
11:21 a send_message_to_user).  Between 10:00 and 11:21 the agent got 78
messages and gave 14 replies: dozens of turns drove ONE group chat at once.
The owner: "it shd be automatically associated and understood in n
context, does this happen only in agent creation?"  It did not: the plan
writer's interview and REUSE held the same way.

Now /chat admits one turn at a time per agent conversation, its person first
(hart_intelligence_entry._admit_chat_turn, hartos.helper.admit_turn;
turn_exclusive v2, reviewed by hartos-17).  A person's message that finds the
conversation busy is answered at once with what happens to it, its files are
the agent's at once, and every message kept meanwhile is read in ONE turn
once the conversation is free: /chat again, through the same door and auth,
its answer published as the agent's message (no request waits for it).  An
open-mic final that finds it busy is not judged then (the judge needs the
model the running turn holds): kept when the agent has a question open to the
person, and judged when its turn runs; with no question open, recorded heard
and judged once when the conversation is free (#303, hartos-91's review,
2026-11-28: the night before, 18 of 18 finals answered on a judge that timed
out were TV, room talk and decode loops, each a turn of its own).  Background
work is held while a person's turn runs or waits, and one already running
stops at its next round, since the waiting turn holds the foreground.

Real /chat (hart_intelligence_entry's Flask app, its auth, its door and its
teardown), real turn registry; a turn is stopped at the first thing it does
past the door (thread_local_data.set_task_source), where it is recorded.

    python -m pytest tests/unit/test_a_message_sent_while_the_agent_is_busy_is_read.py --noconftest -q
"""
import hashlib
import threading
import time
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.timeout(240)

SEND_AGAIN = 'send the same message again'
WAIT = 20


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


class _TurnEnds(Exception):
    """Ends a turn where the seam stops it."""


class _Seam:
    """Stands in for the first thing a turn does past /chat's door
    (thread_local_data.set_task_source): records each turn that got there,
    holds the first one until released, and ends every turn there."""

    def __init__(self):
        self.turns = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, *_a, **_k):
        from flask import request
        body = request.get_json(silent=True) or {}
        with self._lock:
            self.turns.append({'prompt': body.get('prompt'),
                               'request_id': body.get('request_id'),
                               'headers': {k.lower(): v for k, v in request.headers.items()}})
            first = len(self.turns) == 1
        self.entered.set()
        if first:
            self.release.wait(WAIT)
        raise _TurnEnds()

    def wait_for(self, n):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.turns) >= n:
                    return True
            time.sleep(0.05)
        return False


@pytest.fixture
def seam(hie):
    """The seam, with what a reply reaches for kept on this process: no
    voice is made and no chat row leaves (the live app's router)."""
    s = _Seam()
    from integrations.social import chat_messages, rate_limiter
    with patch.object(hie.thread_local_data, 'set_task_source', side_effect=s), \
            patch.object(rate_limiter._limiter, 'check', return_value=True), \
            patch.object(hie, '_tts_synthesize_and_publish'), \
            patch.object(chat_messages, 'persist_and_publish_async'):
        yield s
        s.release.set()
        _settle(hie)


def _settle(hie):
    """Every waiting turn of this file ended, nothing marked."""
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name.startswith('kept-turn-')]:
            break
        time.sleep(0.05)


def _body(text, request_id, user='u-busy', agent='p-busy', **extra):
    return {'user_id': user, 'prompt_id': agent, 'prompt': text,
            'request_id': request_id, **extra}


def _ask(hie, body, headers=None):
    """One /chat request on this thread; returns (status, json).  A turn the
    seam ended is a 500 whether or not the app propagates the exception."""
    try:
        with hie.app.test_client() as client:
            answer = client.post('/chat', json=body, headers=headers or {})
    except _TurnEnds:
        return 500, {}
    return answer.status_code, (answer.get_json(silent=True) or {})


def _post(hie, body, headers=None):
    """One /chat request on its own thread, as the server runs it."""
    t = threading.Thread(target=_ask, args=(hie, body, headers), daemon=True)
    t.start()
    return t


def _waiting_threads():
    return [t for t in threading.enumerate() if t.name.startswith('kept-turn-')]


def _a_persons_turn_is_running(hie, seam, user='u-busy', agent='p-busy'):
    first = _post(hie, _body('first message', 'msg_1795670000001_aaaaaa', user, agent))
    assert seam.entered.wait(WAIT), 'the first turn never ran'
    return first


# -- a person's messages to a busy conversation ---------------------------------

def test_a_message_to_a_busy_conversation_is_kept_not_sent_back(hie, seam):
    """THE REGRESSION (10:52:26): the message is answered at once with what
    happens to it, and it is not run beside the running turn."""
    first = _a_persons_turn_is_running(hie, seam)

    status, reply = _ask(hie, _body('I already uploaded all photos',
                                    'msg_1795670546333_m4evcj'))

    assert len(seam.turns) == 1, 'the message ran beside the running turn'
    assert status == 200, reply
    text = reply.get('response') or reply.get('text') or ''
    assert SEND_AGAIN not in text, text
    assert text.startswith('This conversation is still answering an earlier message'), text
    assert 'I have this message too' in text
    seam.release.set()
    first.join(WAIT)


def test_three_messages_while_busy_are_read_as_one_turn_in_order(hie, seam):
    """Three messages during one running turn: all three answered at once,
    ONE waiting turn (no request waits), and once the running turn ends one
    turn reads all three in order, with the first one's credentials and the
    newest one's request id."""
    first = _a_persons_turn_is_running(hie, seam)
    started = time.monotonic()
    for i, rid in enumerate(('msg_1795670600001_bbbbbb', 'msg_1795670600002_cccccc',
                             'msg_1795670600003_dddddd')):
        status, reply = _ask(hie, _body(f'message {i + 1}', rid),
                             headers={'X-Probe': f'request {i + 1}'})
        assert status == 200 and SEND_AGAIN not in (reply.get('response') or ''), reply
    assert time.monotonic() - started < WAIT
    assert len(seam.turns) == 1, 'a kept message ran beside the running turn'
    assert len(_waiting_threads()) == 1

    seam.release.set()
    first.join(WAIT)
    assert seam.wait_for(2), 'the kept messages were never read'
    read = seam.turns[1]
    assert read['prompt'] == 'message 1\nmessage 2\nmessage 3'
    assert read['request_id'] == 'msg_1795670600003_dddddd'
    assert read['headers'].get('x-probe') == 'request 1'
    _settle(hie)
    from hartos.helper import conversation_taken
    assert not conversation_taken('u-busy_p-busy', hie._CHAT_TURN_GROUP)


def test_a_resend_of_a_kept_message_is_read_once(hie, seam):
    first = _a_persons_turn_is_running(hie, seam)
    for rid in ('msg_1795670700001_eeeeee', 'msg_1795670700002_ffffff'):
        _ask(hie, _body('her name is Zoey', rid))
    seam.release.set()
    first.join(WAIT)
    assert seam.wait_for(2)
    _settle(hie)
    assert [t['prompt'] for t in seam.turns] == ['first message', 'her name is Zoey']


def test_a_turn_that_ended_in_an_error_frees_its_conversation(hie, seam):
    """The door's mark leaves with its request, however the request ends."""
    seam.release.set()
    _ask(hie, _body('first message', 'msg_1795670800001_gggggg'))
    from hartos.helper import conversation_taken
    assert not conversation_taken('u-busy_p-busy', hie._CHAT_TURN_GROUP)
    status, reply = _ask(hie, _body('second message', 'msg_1795670800002_hhhhhh'))
    assert len(seam.turns) == 2, 'the next message was kept for a turn that had ended'


def test_another_agent_of_the_same_person_runs_at_once(hie, seam):
    first = _a_persons_turn_is_running(hie, seam)
    _ask(hie, _body('hello', 'msg_1795670900001_iiiiii', agent='p-other'))
    assert len(seam.turns) == 2
    seam.release.set()
    first.join(WAIT)


# -- a photo sent while busy ------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    from core import user_file_store as ufs
    s = ufs.UserFileStore(str(tmp_path / 'user_files'), disk_free=lambda _p: 1 << 40)
    monkeypatch.setattr(ufs, '_STORE', s)
    return s


def test_a_photo_sent_while_busy_is_the_agents_for_the_kept_turn(hie, seam, store):
    """hartos-17's must (b): message 2 carries a photo while the agent is
    busy; the turn that reads it lists the photo (get_user_uploaded_file)."""
    data = b'a photo of Appa ' * 64
    xid = hashlib.sha256(data).hexdigest()
    assert store.offer('u-busy', 'IMG_appa.jpg', len(data), xid, 'image/jpeg',
                       'chat_attachment')['type'] == 'file_accept'
    assert store.put_chunk('u-busy', xid, 0, data, xid)['type'] == 'file_ack'
    assert store.commit('u-busy', xid)['type'] == 'file_done'
    first = _a_persons_turn_is_running(hie, seam)

    status, reply = _ask(hie, _body('this is Appa', 'msg_1795671000001_jjjjjj', attachments=[
        {'file_id': xid, 'sha256': xid, 'name': 'IMG_appa.jpg', 'mime': 'image/jpeg',
         'size': len(data)}]))
    assert status == 200 and len(seam.turns) == 1, reply

    from tests.unit.test_core_tools_uuid_user_id import _ctx, _on_the_persons_turn, _tool
    ctx = dict(_ctx('u-busy'), prompt_id='p-busy')
    listed = _on_the_persons_turn('msg_1795671000001_jjjjjj',
                                  _tool(ctx, 'get_user_uploaded_file'))
    assert xid in listed, listed
    seam.release.set()
    first.join(WAIT)


# -- the open mic, and background work -------------------------------------------

def _judge(verdicts, judged):
    """decide_open_mic_turn's stand-in: each call takes the next verdict,
    (respond, judge) where judge is 'yes', 'no' or 'unknown'."""
    def decide(user_id, prompt_id, words, **_k):
        judged.append(words)
        respond, judge = verdicts.pop(0)
        return respond, f'ask_open=0,last_agent_s=none,turns=0,judge={judge}', judge
    return decide


def test_room_talk_with_no_question_open_is_heard_then_judged_once_free(hie, seam):
    """Agreed with #289, kept by #303: the room must never become the agent's
    next turn on a guess, and the judge would wait for the model the running
    turn holds (72 of 129 finals were never judged on 11-26).  With no
    question open, the final is recorded heard at once and judged once when
    the conversation is free; room talk then runs no turn."""
    first = _a_persons_turn_is_running(hie, seam)
    judged, heard = [], []
    with patch('integrations.audio.addressee.open_question', return_value=None), \
            patch('integrations.audio.addressee.decide_open_mic_turn',
                  side_effect=_judge([(False, 'no')], judged)), \
            patch('integrations.audio.addressee.record_heard',
                  side_effect=lambda *a, **k: heard.append((a, k))):
        status, reply = _ask(hie, _body('We map, we map', 'msg_1795671100001_kkkkkk',
                                        open_mic=True))
        assert judged == [], 'the judge was asked while the conversation was busy'
        assert reply.get('not_addressed') is True and status == 200, reply
        assert heard and heard[0][0][1] == 'We map, we map'
        seam.release.set()
        first.join(WAIT)
        _settle(hie)
    assert judged == ['We map, we map'], 'judged other than once when free'
    assert len(seam.turns) == 1, 'room talk became a turn'
    assert len(heard) == 1, 'recorded heard twice'


def test_an_answer_to_the_agents_question_while_busy_is_kept_and_answered(hie, seam):
    """#303, hartos-91's review: the agent asked "Say ball" and its turn is
    still running its rounds when the child says "ball".  The final is kept,
    not recorded as room talk, judged when its turn runs, and answered."""
    first = _a_persons_turn_is_running(hie, seam)
    judged, heard = [], []
    with patch('integrations.audio.addressee.open_question', return_value='Say ball'), \
            patch('integrations.audio.addressee.decide_open_mic_turn',
                  side_effect=_judge([(True, 'yes')], judged)), \
            patch('integrations.audio.addressee.record_heard',
                  side_effect=lambda *a, **k: heard.append((a, k))):
        status, reply = _ask(hie, _body('ball', 'msg_1795671100002_llllll',
                                        open_mic=True))
        assert status == 200 and reply.get('kept') is True, reply
        assert judged == [] and heard == []
        seam.release.set()
        first.join(WAIT)
        assert seam.wait_for(2), 'the answer was never read'
        _settle(hie)
    assert judged == ['ball']
    assert seam.turns[1]['prompt'] == 'ball'
    assert seam.turns[1]['request_id'] == 'msg_1795671100002_llllll'


def test_room_talk_kept_under_an_open_question_is_heard_when_judged(hie, seam):
    """Kept because a question was open, judged room talk when its turn runs:
    no turn, and the words are on record as heard then."""
    first = _a_persons_turn_is_running(hie, seam)
    judged, heard = [], []
    with patch('integrations.audio.addressee.open_question', return_value='Say ball'), \
            patch('integrations.audio.addressee.decide_open_mic_turn',
                  side_effect=_judge([(False, 'no')], judged)), \
            patch('integrations.audio.addressee.record_heard',
                  side_effect=lambda *a, **k: heard.append((a, k))):
        _ask(hie, _body('Yes. Bravo, guys. We will be right back.',
                        'msg_1795671100003_mmmmmm', open_mic=True))
        seam.release.set()
        first.join(WAIT)
        _settle(hie)
    assert judged == ['Yes. Bravo, guys. We will be right back.']
    assert len(seam.turns) == 1, 'room talk became a turn'
    assert len(heard) == 1 and heard[0][1].get('request_id') == 'msg_1795671100003_mmmmmm'


def test_a_free_conversations_final_the_judge_cannot_judge_is_judged_again(hie, seam):
    """hartos-91: a judge that times out is no verdict.  With no question
    open the final is heard, runs no turn, and is judged once more later
    (_judge_heard_words_again); that verdict, if addressed, answers it once."""
    seam.release.set()
    judged, again = [], []
    with patch('integrations.audio.addressee.decide_open_mic_turn',
               side_effect=_judge([(False, 'unknown')], judged)), \
            patch('integrations.audio.addressee.record_heard'), \
            patch.object(hie, '_judge_heard_words_again',
                         side_effect=lambda *a, **k: again.append(a)):
        status, reply = _ask(hie, _body('can you help me', 'msg_1795671100004_nnnnnn',
                                        open_mic=True))
    assert reply.get('not_addressed') is True and status == 200, reply
    assert seam.turns == [], 'a final nobody judged became a turn'
    assert len(again) == 1 and again[0][4] == 'can you help me'

    read = []
    with patch('integrations.audio.addressee.decide_open_mic_turn',
               side_effect=_judge([(True, 'yes')], judged)), \
            patch.object(hie, '_read_kept_messages',
                         side_effect=lambda *a: read.append(a)):
        with hie.app.test_request_context('/chat', json={}):
            hie._judge_heard_words_again({'prompt': 'can you help me'}, 'u-busy',
                                         'p-busy', 'msg_1795671100004_nnnnnn',
                                         'can you help me')
        deadline = time.monotonic() + WAIT
        while not read and time.monotonic() < deadline:
            time.sleep(0.05)
    assert len(read) == 1
    assert [k['words'] for k in read[0][2]] == ['can you help me']


def test_a_daemon_turn_is_held_while_a_persons_turn_runs(hie, seam):
    from core.agent_tools import is_held_for_another_turn
    first = _a_persons_turn_is_running(hie, seam)

    status, reply = _ask(hie, _body('the goal tick', 'daemon_goal-1'))

    assert len(seam.turns) == 1, 'background work ran beside the person'
    assert is_held_for_another_turn(reply.get('response')), reply
    seam.release.set()
    first.join(WAIT)


def test_a_daemon_turn_stops_at_its_round_and_the_waiting_person_runs():
    """hartos-17's must (c): a daemon turn on the conversation leaves at its
    next round boundary, because the waiting person's turn holds the
    foreground mark that background_work_must_yield reads."""
    from core.foreground import foreground_active
    from hartos.helper import admit_turn, leave_admitted_turn
    up, group = 'u-daemon_p-daemon', 'chat'
    assert not foreground_active()
    order, ran = [], threading.Event()

    def daemon_turn():
        assert admit_turn(up, group, person=False) == 'run'
        try:
            deadline = time.monotonic() + WAIT
            while not foreground_active() and time.monotonic() < deadline:
                time.sleep(0.01)          # one round, then the boundary check
        finally:
            order.append('daemon left')
            leave_admitted_turn(up, group)

    d = threading.Thread(target=daemon_turn, daemon=True)
    d.start()
    time.sleep(0.2)

    def read(kept, overdue=False):
        order.append(f'person read {[k["words"] for k in kept]}, overdue={overdue}')
        ran.set()
    assert admit_turn(up, group, person=True, kept={'words': 'hello'}, run_kept=read) == 'kept'
    assert ran.wait(WAIT), 'the waiting person never ran'
    d.join(WAIT)
    assert order == ['daemon left', "person read ['hello'], overdue=False"]


def test_the_kept_turns_answer_is_published_not_returned(hie):
    """hartos-17's must 1: the page gives up on a chat silent for 180 s, so
    the answer of the turn that read the kept messages goes as the agent's
    message, never on a request's socket."""
    sent, published = [], []
    kept = [{'body': {'user_id': '10202', 'prompt_id': '95568353503', 'prompt': 'a',
                      'open_mic': False, 'media_mode': 'audio'},
             'request_id': 'msg_1', 'words': 'I already uploaded all photos',
             'headers': {'Authorization': 'Bearer t1'}, 'remote_addr': '192.168.0.40'},
            {'body': {'user_id': '10202', 'prompt_id': '95568353503', 'prompt': 'b'},
             'request_id': 'msg_2', 'words': 'mom and dad too',
             'headers': {'Authorization': 'Bearer t2'}, 'remote_addr': '192.168.0.41'}]
    with patch.object(hie, '_replay_chat',
                      side_effect=lambda b, h, a=None: sent.append((b, h, a)) or (
                          200, {'response': 'I see the five photos you sent.'})), \
            patch('core.peer_link.crossbar_publish.publish_agent_message',
                  side_effect=lambda **k: published.append(k) or True):
        hie._read_kept_messages('10202', '95568353503', kept)

    body, headers, remote_addr = sent[0]
    assert body['prompt'] == 'I already uploaded all photos\nmom and dad too'
    assert body['request_id'] == 'msg_2' and 'open_mic' not in body
    assert body['media_mode'] == 'audio'
    assert headers == {'Authorization': 'Bearer t1'}, 'the credential was not carried'
    assert remote_addr == '192.168.0.40', 'the replay did not come from where the request did'
    assert published == [{'text': 'I see the five photos you sent.', 'user_id': '10202',
                          'request_id': 'msg_2', 'prompt_id': '95568353503'}]


# -- review rv_303's MUSTs -------------------------------------------------------

def test_the_replay_meets_the_gate_as_the_request_did(hie, seam):
    """MUST-4, rv_303 round 3: nothing the gate decided is carried past it.
    The replay comes from where the kept request came, with its Host and its
    credential (_kept_request; as _phone_turn_here replays a phone's turn),
    so the gate verifies it again, revocation and all, and a caller on
    another machine never turns local.  A failure in the view is a 500, not
    an exception out of the waiting turn."""
    from flask import request
    body = _body('hello', 'msg_1795671200001_oooooo', user='u-phone')
    seen, environs = [], []

    def at_the_seam(*_a, **_k):
        seen.append((request.remote_addr, request.host,
                     request.headers.get('Authorization')))
        raise _TurnEnds()
    real_context = hie.app.test_request_context
    with patch.object(hie.thread_local_data, 'set_task_source', side_effect=at_the_seam), \
            patch.dict(hie.app.config, {'PROPAGATE_EXCEPTIONS': False, 'TESTING': False}):
        status, _reply = hie._replay_chat(
            body, {'Host': 'localhost:5000', 'Authorization': 'Bearer t1'}, '127.0.0.1')
        with patch.object(hie.app, 'test_request_context',
                          side_effect=lambda *a, **k: environs.append(k.get('environ_base'))
                          or real_context(*a, **k)):
            hie._replay_chat(body, {}, '192.168.0.40')
    assert seen[0] == ('127.0.0.1', 'localhost:5000', 'Bearer t1'), seen
    assert status == 500
    assert environs == [{'REMOTE_ADDR': '192.168.0.40'}], environs
    with hie.app.test_request_context('/chat', method='POST', json={},
                                      headers={'Host': 'hartos.sock'},
                                      environ_base={'REMOTE_ADDR': '192.168.0.40'}):
        kept = hie._kept_request({}, 'msg_1795671200001_oooooo', 'hello')
    assert kept['remote_addr'] == '192.168.0.40'
    assert kept['headers'].get('Host') == 'hartos.sock', 'Host was not kept for the gate'
    assert 'verified' not in kept, 'a gate verdict is carried past the gate'


_PENDING = "Waiting for this desktop's owner to allow this phone."
_DENIED = "This desktop's owner has not allowed this phone."
_KONG_BUSY = "The sign-in check is busy; try again in a moment."


@pytest.mark.parametrize('status, reply, says', [
    # The gate's own words, as given (owner 2026-11-09: every refusal says
    # what was really wrong): never "sign in again" for these.
    (403, {'error': 'consent_pending', 'message': _PENDING}, _PENDING),
    (403, {'error': 'consent_denied', 'message': _DENIED}, _DENIED),
    (403, {'error': 'update_app', 'message': 'UPDATE_APP_WORDS'}, 'UPDATE_APP_WORDS'),
    (503, {'error': _KONG_BUSY, 'message': _KONG_BUSY}, _KONG_BUSY),
    # No words of its own: the status's plain reason.
    (401, {'error': 'unauthorized'}, 'sign in again'),
    (403, {}, 'sign in again'),
    (429, {'error': 'Rate limit exceeded (30/min). Please wait.'}, 'too many messages'),
    (413, {'error': 'prompt too long'}, 'too long'),
    (503, {'error': 'Rate limiter unavailable'}, 'busy right now'),
])
def test_kept_words_not_read_tell_the_person_why(hie, status, reply, says):
    """rv_303 rounds 3 and 4: a replay the gate refuses (a phone un-paired or
    still waiting for the owner, a token expired while the words waited) or
    one that ends busy, rate limited or too long is never left as silence:
    the person is told what was really wrong, in the refusal's own words
    when it has them, and no answer is made up."""
    from core.agent_tools import kept_messages_not_read_reply
    if says == 'UPDATE_APP_WORDS':
        from integrations.social.auth import UPDATE_APP_WORDS
        reply = dict(reply, message=UPDATE_APP_WORDS)
        says = UPDATE_APP_WORDS
    published = []
    kept = [{'body': {'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'x'},
             'request_id': 'msg_1795671200014_ffffff', 'words': 'are you there',
             'headers': {'Authorization': 'Bearer old'}, 'remote_addr': '192.168.0.40'}]
    with patch.object(hie, '_replay_chat', return_value=(status, reply)), \
            patch('core.peer_link.crossbar_publish.publish_agent_message',
                  side_effect=lambda **k: published.append(k) or True):
        hie._read_kept_messages('u1', 'p1', kept)
    assert len(published) == 1, published
    line = published[0]['text']
    assert published[0]['request_id'] == 'msg_1795671200014_ffffff'
    assert line == kept_messages_not_read_reply(status, reply.get('message'))
    assert says in line, line
    if reply.get('message'):
        assert 'sign in again' not in line, 'a gate reason was replaced by a guess'
    assert '—' not in line


def test_a_read_reply_that_carries_a_message_is_still_the_answer(hie):
    """Only a refusal status is a refusal: a 200 whose JSON carries a
    'message' key is the answer, published as before."""
    published = []
    kept = [{'body': {'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'x'},
             'request_id': 'msg_1795671200016_hhhhhh', 'words': 'hi',
             'headers': {}, 'remote_addr': '127.0.0.1'}]
    with patch.object(hie, '_replay_chat',
                      return_value=(200, {'response': 'Hello!', 'message': 'ok'})), \
            patch('core.peer_link.crossbar_publish.publish_agent_message',
                  side_effect=lambda **k: published.append(k['text']) or True):
        hie._read_kept_messages('u1', 'p1', kept)
    assert published == ['Hello!']


def test_a_chat_nested_in_another_request_leaves_the_outer_mark(hie, seam):
    """rv_303 rounds 4 and 5 (R4-2, R5-1): a /chat run in-process inside
    another on the same thread, through a real inner request (as _replay_chat
    and dispatch._native_chat run one), pushes its own request onto the
    shared app context.  When that inner request ends, its chat() and its
    teardown release only its own mark: the outer conversation is still
    taken, so no second turn can start beside it.  Both go through the real
    door (_admit_chat_turn), so the test holds wherever the mark is kept
    (rv_303 round 6)."""
    from hartos import helper
    seam.release.set()
    group = hie._CHAT_TURN_GROUP
    outer, inner = 'u-outer_p-outer', 'u-inner_p-inner'
    outer_body = _body('outer', 'msg_1795671200017_iiiiii', user='u-outer', agent='p-outer')
    with hie.app.test_request_context('/chat', method='POST', json=outer_body,
                                      environ_base={'REMOTE_ADDR': '127.0.0.1'}):
        assert hie._admit_chat_turn(outer_body, 'u-outer', 'p-outer',
                                    'msg_1795671200017_iiiiii', 'outer') is None, \
            'the outer turn was not admitted'
        try:
            assert (outer, group) in helper.TURNS_IN_FLIGHT
            with patch.dict(hie.app.config, {'PROPAGATE_EXCEPTIONS': False,
                                             'TESTING': False}):
                hie._replay_chat(_body('inner', 'msg_1795671200018_jjjjjj',
                                       user='u-inner', agent='p-inner'), {}, '127.0.0.1')
            assert len(seam.turns) == 1, 'the inner turn never passed the door'
            assert (outer, group) in helper.TURNS_IN_FLIGHT, \
                'the inner request released the outer conversation'
            assert (inner, group) not in helper.TURNS_IN_FLIGHT
        finally:
            hie._leave_the_admitted_chat_turn()
    assert (outer, group) not in helper.TURNS_IN_FLIGHT


def test_the_admission_mark_leaves_with_chat_itself(hie, seam):
    """rv_303 round 1 m6, round 3: chat() releases its admission mark in its
    own finally, so a teardown skipped (another teardown raising) never
    strands it on a pool thread that stays alive.  Here no teardown runs at
    all: chat() is called inside the request, and the mark is gone when it
    returns, before the context is popped."""
    from hartos import helper
    seam.release.set()
    key = ('u-own_p-own', hie._CHAT_TURN_GROUP)
    with hie.app.test_request_context(
            '/chat', method='POST', json=_body('hi', 'msg_1795671200015_gggggg',
                                               user='u-own', agent='p-own'),
            environ_base={'REMOTE_ADDR': '127.0.0.1'}):
        try:
            hie.chat()
        except _TurnEnds:
            pass
        assert len(seam.turns) == 1, 'the turn never passed the door'
        assert key not in helper.TURNS_IN_FLIGHT, 'the mark outlived chat()'
        assert key not in helper.PERSON_TURNS


def test_a_waiter_that_cannot_start_keeps_nothing(hie, seam):
    """MUST-1: a thread that cannot start leaves no waiting entry, so later
    messages are not kept for an answer that never comes."""
    from hartos import helper
    first = _a_persons_turn_is_running(hie, seam)
    with patch.object(helper.threading.Thread, 'start',
                      side_effect=RuntimeError("can't start new thread")):
        status, reply = _ask(hie, _body('are you there', 'msg_1795671200002_pppppp'))
    assert ('u-busy_p-busy', hie._CHAT_TURN_GROUP) not in helper.TURN_WAITERS
    assert SEND_AGAIN in (reply.get('response') or ''), reply
    seam.release.set()
    first.join(WAIT)


def test_a_spoken_final_that_cannot_wait_is_heard_not_sent_back(hie, seam):
    """hartos-91 on MUST-1: when no waiting turn can start, a typed message
    is held with the send-again line, but a person who spoke sent nothing:
    the final is recorded heard and the page told it was not addressed."""
    from hartos import helper
    first = _a_persons_turn_is_running(hie, seam)
    heard = []
    with patch('integrations.audio.addressee.open_question', return_value='Say ball'), \
            patch('integrations.audio.addressee.record_heard',
                  side_effect=lambda *a, **k: heard.append((a, k))), \
            patch.object(helper.threading.Thread, 'start',
                         side_effect=RuntimeError("can't start new thread")):
        status, reply = _ask(hie, _body('ball', 'msg_1795671200006_tttttt', open_mic=True))
    assert status == 200 and reply.get('not_addressed') is True, reply
    assert reply.get('kept') is False, reply
    assert SEND_AGAIN not in (reply.get('response') or ''), reply
    assert len(heard) == 1 and heard[0][0][1] == 'ball'
    assert ('u-busy_p-busy', hie._CHAT_TURN_GROUP) not in helper.TURN_WAITERS
    seam.release.set()
    first.join(WAIT)


@pytest.mark.parametrize('agent', [None, ''])
def test_a_chat_with_no_agent_gets_the_same_one_re_judge(hie, seam, agent):
    """Review rv_303 m8: before #303 a plain chat's undecided final was
    answered; heard and never judged again, the person's words would be
    lost.  A chat with no agent gets the same one re-judge."""
    seam.release.set()
    again = []
    with patch('integrations.audio.addressee.decide_open_mic_turn',
               side_effect=_judge([(False, 'unknown')], [])), \
            patch('integrations.audio.addressee.record_heard'), \
            patch.object(hie, '_judge_heard_words_again',
                         side_effect=lambda *a, **k: again.append(a)):
        status, reply = _ask(hie, _body('what time is it', 'msg_1795671200007_uuuuuu',
                                        user='u-plain', agent=agent, open_mic=True))
    assert reply.get('not_addressed') is True and status == 200, reply
    assert seam.turns == [], 'a final nobody judged became a turn'
    assert len(again) == 1 and again[0][4] == 'what time is it'


def _kept_turn_recorder(helper, key):
    """run_kept / still_waiting stand-ins for the waiter tests: what each was
    called with, and the running turns at the time."""
    ran, told, done = [], [], threading.Event()

    def run_kept(requests, overdue=False):
        ran.append(([r['words'] for r in requests], overdue,
                    set(helper.TURNS_IN_FLIGHT.get(key) or {})))
        done.set()

    def still_waiting(requests):
        told.append(([r['words'] for r in requests],
                     set(helper.TURNS_IN_FLIGHT.get(key) or {})))
    return ran, told, done, run_kept, still_waiting


def _clean(helper, key):
    deadline = time.monotonic() + WAIT
    while key in helper.TURNS_IN_FLIGHT and time.monotonic() < deadline:
        time.sleep(0.05)
    return key not in helper.TURNS_IN_FLIGHT and key not in helper.PERSON_TURNS


def test_a_live_long_turn_keeps_the_conversation_and_the_person_is_told_once(
        monkeypatch):
    """Review rv_303 round 2 S1; owner 2026-11-28: the person must not be left
    "in search of what is happening", and a turn that still runs is never
    pushed aside (two turns on one group chat is the #303 defect).  Past the
    age a person's turn is waited on, the person is told once that the agent
    is still on the earlier message; the running turn keeps its marks; the
    kept words run when it leaves, not overdue."""
    from core import foreground
    from hartos import helper
    monkeypatch.setattr(foreground, 'FOREGROUND_MARKER_MAX_AGE_S', 0.3)
    conversation = 'u-long_p-long'
    key = (conversation, 'chat')
    inside, leave = threading.Event(), threading.Event()

    def long_turn():
        assert helper.admit_turn(conversation, 'chat', person=True) == 'run'
        inside.set()
        leave.wait(WAIT)
        helper.leave_admitted_turn(conversation, 'chat')
    running = threading.Thread(target=long_turn, daemon=True)
    running.start()
    assert inside.wait(WAIT)
    ran, told, done, run_kept, still_waiting = _kept_turn_recorder(helper, key)
    assert helper.admit_turn(conversation, 'chat', person=True,
                             kept={'words': 'are you there'}, run_kept=run_kept,
                             still_waiting=still_waiting) == 'kept'
    deadline = time.monotonic() + WAIT
    while not told and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert told == [(['are you there'], {running.ident})], told
    assert ran == [], 'the kept words ran beside a turn that still runs'
    assert running.ident in (helper.TURNS_IN_FLIGHT.get(key) or {})
    assert running.ident in (helper.PERSON_TURNS.get(key) or {})
    leave.set()
    assert done.wait(WAIT), 'the kept words never ran after the turn left'
    assert ran == [(['are you there'], False, set(ran[0][2]))]
    assert running.ident not in ran[0][2]
    assert len(told) == 1, 'told more than once'
    assert _clean(helper, key)


def test_a_turn_whose_thread_died_without_leaving_frees_the_conversation():
    """Review rv_303 round 2 S1: a running turn whose thread no longer exists
    ended without leaving.  Its marks are dropped at once (no age to wait
    out), the kept words run marked overdue so the person hears the earlier
    answer did not finish, and nothing is said about waiting."""
    from hartos import helper
    conversation = 'u-dead_p-dead'
    key = (conversation, 'chat')

    def dies_inside():
        assert helper.admit_turn(conversation, 'chat', person=True) == 'run'
    dead = threading.Thread(target=dies_inside, daemon=True)
    dead.start()
    dead.join(WAIT)
    assert dead.ident in (helper.TURNS_IN_FLIGHT.get(key) or {})
    ran, told, done, run_kept, still_waiting = _kept_turn_recorder(helper, key)
    assert helper.admit_turn(conversation, 'chat', person=True,
                             kept={'words': 'hello?'}, run_kept=run_kept,
                             still_waiting=still_waiting) == 'kept'
    assert done.wait(WAIT), 'the kept words waited on a dead turn'
    words, overdue, running = ran[0]
    assert words == ['hello?'] and overdue is True
    assert dead.ident not in running
    assert told == []
    assert _clean(helper, key)


def test_a_turn_that_leaves_normally_reads_the_kept_words_untold():
    """The ordinary case: the running turn leaves, the kept words run once,
    not overdue, and the person is told nothing extra."""
    from hartos import helper
    conversation = 'u-norm_p-norm'
    key = (conversation, 'chat')
    inside, leave = threading.Event(), threading.Event()

    def turn():
        assert helper.admit_turn(conversation, 'chat', person=True) == 'run'
        inside.set()
        leave.wait(WAIT)
        helper.leave_admitted_turn(conversation, 'chat')
    running = threading.Thread(target=turn, daemon=True)
    running.start()
    assert inside.wait(WAIT)
    ran, told, done, run_kept, still_waiting = _kept_turn_recorder(helper, key)
    assert helper.admit_turn(conversation, 'chat', person=True,
                             kept={'words': 'and one more'}, run_kept=run_kept,
                             still_waiting=still_waiting) == 'kept'
    leave.set()
    assert done.wait(WAIT)
    assert [(w, o) for w, o, _r in ran] == [(['and one more'], False)]
    assert told == []
    assert _clean(helper, key)


def test_kept_messages_read_after_an_unfinished_turn_tell_the_person_first(hie):
    """S1: before the kept words are read after an answer that never
    finished, the person hears, in plain words, what happened and what comes
    next (owner 2026-11-28: say it before doing the work)."""
    from core.agent_tools import kept_messages_read_after_an_unfinished_turn_reply
    published = []
    kept = [{'body': {'user_id': 'u1', 'prompt_id': 'p1', 'prompt': 'x'},
             'request_id': 'msg_1795671200008_vvvvvv', 'words': 'hello?',
             'headers': {}, 'remote_addr': '127.0.0.1'}]
    with patch.object(hie, '_replay_chat', return_value=(200, {'response': 'Hi, I am here.'})), \
            patch('core.peer_link.crossbar_publish.publish_agent_message',
                  side_effect=lambda **k: published.append(k['text']) or True):
        hie._read_kept_messages('u1', 'p1', kept, overdue=True)
    assert published == [kept_messages_read_after_an_unfinished_turn_reply(),
                         'Hi, I am here.']
    assert '—' not in published[0]


def test_the_still_working_line_goes_only_for_words_sent_to_the_agent(hie):
    """S1: the "still working" line is for messages the person sent; room
    talk the open mic recorded as heard waits without a word.  It promises
    no "now", and goes under the newest sent message's request id."""
    from core.agent_tools import still_answering_an_earlier_message_reply
    published = []
    with patch('core.peer_link.crossbar_publish.publish_agent_message',
               side_effect=lambda **k: published.append(k) or True):
        hie._say_the_kept_messages_wait('u1', 'p1', [
            {'request_id': 'msg_1795671200009_aaaaaa', 'words': 'the TV', 'heard': True}])
        hie._say_the_kept_messages_wait('u1', 'p1', [
            {'request_id': 'msg_1795671200010_bbbbbb', 'words': 'hello?'},
            {'request_id': 'msg_1795671200011_cccccc', 'words': 'the TV', 'heard': True}])
    assert [(p['text'], p['request_id']) for p in published] == [
        (still_answering_an_earlier_message_reply(), 'msg_1795671200010_bbbbbb')]
    assert ' now' not in published[0]['text'].lower()


def test_a_kept_finals_judge_runs_under_its_own_request(hie):
    """MUST-2: the judge's model call goes as the person's call, under the
    final's request id, not as daemon work; the thread's id is put back."""
    seen = []
    hie.thread_local_data.set_request_id(request_id=None)
    with patch('integrations.audio.addressee.decide_open_mic_turn',
               side_effect=lambda *a, **k: seen.append(
                   (hie.thread_local_data.get_request_id(), k.get('heard_already')))
               or (True, 'judge=yes', '')):
        assert hie._kept_final_is_for_the_agent(
            'u1', 'p1', {'words': 'ball', 'request_id': 'msg_1795671200003_qqqqqq'})
    assert seen == [('msg_1795671200003_qqqqqq', False)]
    assert hie.thread_local_data.get_request_id() is None


def _the_model_says_no(asked):
    """core.llm_json.ask_model_json's stand-in for the open-mic judge, so the
    real decide_open_mic_turn picks its judge (review rv_gaps A: the tests
    above replace decide_open_mic_turn whole and cannot see that choice)."""
    def ask(content, schema, purpose, **kwargs):
        asked.append(kwargs)
        return {'for_the_agent': False, 'reason': 'a TV programme'}
    return ask


def _quiet_signals():
    """The judge's other readers, off the disk and the mic."""
    return (patch('integrations.audio.addressee._recent_turns', return_value=[]),
            patch('integrations.audio.addressee._voice_hint', return_value=''),
            patch('integrations.audio.addressee._agent_name', return_value=None))


def test_tv_under_an_open_question_is_judged_patiently_and_heard_not_read(hie, seam):
    """Review rv_gaps A, live 2026-11-27 23:38 IST: the TV's "ご視聴ありがとう
    ございました" reached the speech agent unjudged while it waited on "Say
    ball".  With the question open the patient judge (no 15 s one-try) says
    no: the words are recorded heard, no turn runs, no later re-judge."""
    seam.release.set()
    asked, heard, again = [], [], []
    recent, voice, name = _quiet_signals()
    with patch('integrations.audio.addressee.open_question', return_value='Say ball'), \
            patch('core.llm_json.ask_model_json', side_effect=_the_model_says_no(asked)), \
            recent, voice, name, \
            patch('integrations.audio.addressee.record_heard',
                  side_effect=lambda *a, **k: heard.append(a)), \
            patch.object(hie, '_judge_heard_words_again',
                         side_effect=lambda *a, **k: again.append(a)):
        status, reply = _ask(hie, _body('ご視聴ありがとうございました',
                                        'msg_1795671100009_ssssss', open_mic=True))
    assert status == 200 and reply.get('not_addressed') is True, reply
    assert seam.turns == [], 'the TV became a turn'
    assert len(asked) == 1 and 'once' not in asked[0], asked
    assert len(heard) == 1 and heard[0][1] == 'ご視聴ありがとうございました'
    assert again == [], 'a final judged no was judged again'


def test_a_kept_final_under_an_open_question_is_judged_patiently(hie):
    """The kept path (the final arrived while the agent was busy) picks the
    same judge: one patient call, and a "no" records the words heard."""
    asked, heard = [], []
    recent, voice, name = _quiet_signals()
    with patch('integrations.audio.addressee.open_question', return_value='Say ball'), \
            patch('core.llm_json.ask_model_json', side_effect=_the_model_says_no(asked)), \
            recent, voice, name, \
            patch('integrations.audio.addressee.record_heard',
                  side_effect=lambda *a, **k: heard.append((a, k))):
        assert not hie._kept_final_is_for_the_agent(
            'u1', 'p1', {'words': 'Bravo, guys', 'request_id': 'msg_1795671200011_tttttt'})
    assert len(asked) == 1 and 'once' not in asked[0], asked
    assert len(heard) == 1 and heard[0][1].get('request_id') == 'msg_1795671200011_tttttt'


def test_one_re_judge_at_a_time_per_conversation(hie):
    """Review rv_303 m4: each re-judge is a 4B call that evicts the turn's
    cached prompt; a second heard final of the same conversation waits its
    turn as heard, logged, while the first is judged."""
    started = []
    gate = threading.Event()

    def slow_decide(*_a, **k):
        started.append(k.get('heard_already'))
        gate.wait(WAIT)
        return False, 'judge=no', ''
    with patch('integrations.audio.addressee.decide_open_mic_turn', side_effect=slow_decide):
        with hie.app.test_request_context('/chat', json={}):
            hie._judge_heard_words_again({}, 'u9', 'p9', 'msg_1795671200004_rrrrrr', 'one')
            hie._judge_heard_words_again({}, 'u9', 'p9', 'msg_1795671200005_ssssss', 'two')
        deadline = time.monotonic() + WAIT
        while not started and time.monotonic() < deadline:
            time.sleep(0.05)
        gate.set()
    assert started == [True], started
    deadline = time.monotonic() + WAIT
    while 'u9_p9' in hie._REJUDGING and time.monotonic() < deadline:
        time.sleep(0.05)
    assert 'u9_p9' not in hie._REJUDGING
