"""Each modality's one existing chokepoint puts a line on the audit trail
(security.audit_trail): conversation rows (chat, channels, camera and screen
captions), uploads, image captions, model calls, spoken replies, and the
per-agent read routes the Nunba audit page uses.

Owner, 2026-11-20: "every message, transcription, audio, video, file uploaded,
image caption, screen captioned, video captioned per agent via nunba ui".
"""
import hashlib
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from security import audit_trail
from security.audit_trail import AuditTrail


@pytest.fixture
def trail(tmp_path, monkeypatch):
    t = AuditTrail(str(tmp_path / 'trail'), start=False, role_lookup=lambda uid: None)
    monkeypatch.setattr(audit_trail, '_trail', t)
    return t


@pytest.fixture
def chat_db(tmp_path, monkeypatch):
    """The real conversation table on a real SQLite file."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from integrations.social import models
    engine = create_engine(f"sqlite:///{(tmp_path / 'chat.db').as_posix()}",
                           connect_args={'check_same_thread': False}, poolclass=NullPool)
    models.Base.metadata.create_all(
        engine, tables=[models.User.__table__, models.ConversationEntry.__table__])
    monkeypatch.setattr(models, '_engine', engine)
    monkeypatch.setattr(models, '_SessionLocal',
                        sessionmaker(bind=engine, expire_on_commit=False))
    return models


def test_a_chat_turn_written_by_the_one_writer_is_on_the_trail(trail, chat_db):
    from integrations.social import chat_messages
    row = chat_messages.persist('u7', 'user', 'transfer 500 to savings',
                                prompt_id='54', request_id='req-9')
    assert row is not None
    chat_messages.persist('u7', 'assistant', 'Done: 500 moved.', prompt_id='54',
                          request_id='req-9')
    trail.drain()
    rows = list(reversed(trail.read(agent_id='54')))  # oldest first; ties keep file order
    assert [(r['kind'], r['dir'], r['actor'], r['text']) for r in rows] == [
        ('message', 'in', 'person', 'transfer 500 to savings'),
        ('message', 'out', 'agent', 'Done: 500 moved.')]
    assert all(r['request_id'] == 'req-9' and r['user_id'] == 'u7' for r in rows)


def test_a_screen_caption_is_its_own_kind_and_the_loop_prompt_is_not_kept(trail, chat_db):
    from integrations.agent_engine.world_model_bridge import WorldModelBridge
    bridge = WorldModelBridge.__new__(WorldModelBridge)  # no network, no threads
    bridge._persist_to_conversation_entry(
        user_id='u7', prompt_id='vision_screen',
        prompt='[screen] describe what you see',
        response='A bank statement is open in the browser.', model_id='minicpm')
    trail.drain()
    rows = trail.read(user_id='u7')
    assert [(r['kind'], r['text']) for r in rows] == [
        ('screen_caption', 'A bank statement is open in the browser.')]


def test_an_upload_is_a_checksum_and_a_pointer_and_its_turn_names_the_agent(trail, tmp_path):
    from core.user_file_store import CHAT_ATTACHMENT, UserFileStore
    store = UserFileStore(root=str(tmp_path / 'files'), disk_free=lambda p: 10**12)
    data = b'%PDF-1.4 statement'
    done = store.put_file('u7', io.BytesIO(data), name='statement.pdf',
                          mime='application/pdf', purpose=CHAT_ATTACHMENT)
    xid = done['xid']
    store.bind('u7', [xid], prompt_id='54', request_id='req-9')
    trail.drain()
    rows = list(reversed(trail.read(user_id='u7')))  # oldest first; ties keep file order
    assert [r['extra']['event'] for r in rows] == ['received', 'given to an agent turn']
    assert all(r['kind'] == 'file' and r['sha256'] == hashlib.sha256(data).hexdigest()
               for r in rows)
    assert rows[0]['bytes'] == len(data) and rows[0]['ref'].endswith('.pdf')
    assert rows[1]['agent_id'] == '54' and rows[1]['request_id'] == 'req-9'
    assert 'text' not in rows[0], 'the file content was copied into the trail'


def test_an_image_caption_is_recorded_with_the_image_checksum(trail, tmp_path, monkeypatch):
    from integrations.vision import image_describe
    img = tmp_path / 'cheque.png'
    img.write_bytes(b'\x89PNG fake image bytes')
    monkeypatch.setattr(image_describe, '_infer_image', lambda p, q=None: 'A signed cheque.')
    assert image_describe.describe_image(str(img)) == 'A signed cheque.'
    trail.drain()
    row = trail.read(kinds=['image_caption'])[0]
    assert row['text'] == 'A signed cheque.' and row['dir'] == 'in'
    assert row['sha256'] == hashlib.sha256(img.read_bytes()).hexdigest()


def test_a_model_call_records_the_model_and_the_body_checksum(trail, tmp_path, monkeypatch):
    from core import llm_outbound_logger as lol
    monkeypatch.setattr(lol, '_get_log_path', lambda: str(tmp_path / 'llm_outbound.jsonl'))
    lol._close_handle()
    body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hi'}]}
    lol.log_outbound(body, response_status=200, latency_ms=12.5, source='test')
    lol._close_handle()
    trail.drain()
    row = trail.read(kinds=['llm_call'])[0]
    assert row['model'] == 'qwen' and row['dir'] == 'out'
    assert row['extra']['status'] == 200 and row['extra']['messages'] == 1
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    assert row['sha256'] == hashlib.sha256(canonical.encode()).hexdigest()


def test_a_transcription_records_the_words_and_the_audio_checksum(trail, tmp_path,
                                                                 monkeypatch):
    """The audio is hashed before whisper_transcribe returns: callers delete a
    temporary WAV right after, so the writer thread could not read it later."""
    from integrations.service_tools import whisper_tool
    wav = tmp_path / 'turn.wav'
    wav.write_bytes(b'RIFF' + b'\1' * 200)
    monkeypatch.setattr(whisper_tool, '_stt_call', lambda req: {
        'raw_json': json.dumps({'text': 'move 500 to savings', 'language': 'en'})})
    out = whisper_tool.whisper_transcribe(str(wav))
    assert json.loads(out)['text'] == 'move 500 to savings'
    expected = hashlib.sha256(wav.read_bytes()).hexdigest()
    wav.unlink()  # the caller's clean-up, before the writer runs
    trail.drain()
    row = trail.read(kinds=['transcript'])[0]
    assert row['text'] == 'move 500 to savings' and row['dir'] == 'in'
    assert row['sha256'] == expected and row['bytes'] == 204
    assert row['extra']['language'] == 'en'


def test_an_empty_or_failed_transcription_records_nothing(trail, tmp_path, monkeypatch):
    from integrations.service_tools import whisper_tool
    monkeypatch.setattr(whisper_tool, '_stt_call', lambda req: {
        'raw_json': json.dumps({'text': '', 'language': 'unknown'})})
    whisper_tool.whisper_transcribe(str(tmp_path / 'silence.wav'))
    monkeypatch.setattr(whisper_tool, '_stt_call', lambda req: {'error': 'no engine'})
    whisper_tool.whisper_transcribe(str(tmp_path / 'x.wav'))
    trail.drain()
    assert trail.read(kinds=['transcript']) == []


def test_a_spoken_reply_records_the_words_engine_and_wav(trail, tmp_path):
    from integrations.channels.media import tts_router
    wav = tmp_path / 'reply.wav'
    wav.write_bytes(b'RIFF' + b'\0' * 100)
    candidate = SimpleNamespace(engine=SimpleNamespace(engine_id='piper'))
    tts_router._audit_speech('Your balance is 500.', {'path': str(wav), 'duration': 1.2},
                             candidate, 'en', None, 'chat_response')
    trail.drain()
    row = trail.read(kinds=['speech_out'])[0]
    assert row['text'] == 'Your balance is 500.' and row['model'] == 'piper'
    assert row['sha256'] == hashlib.sha256(wav.read_bytes()).hexdigest()


# ── the read routes the Nunba audit page calls ─────────────────────────

@pytest.fixture
def audit_db():
    """One in-memory database with the goal tables the routes look up."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from integrations.social import models
    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    models.Base.metadata.create_all(engine, tables=[
        models.User.__table__, models.AgentGoal.__table__, models.CodingGoal.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def event_log(monkeypatch):
    """A fresh in-memory security event log (security.immutable_audit_log) in
    place of the process's own, which the agent page now reads too."""
    from security import immutable_audit_log
    log = immutable_audit_log.ImmutableAuditLog()
    log._use_db = False
    monkeypatch.setattr(immutable_audit_log, '_audit_log', log)
    return log


@pytest.fixture
def client(trail, audit_db, event_log, monkeypatch):
    from flask import Flask

    from integrations.social import api_audit
    user = SimpleNamespace(id='owner', is_admin=False, role='flat', is_banned=False)
    app = Flask(__name__)
    app.register_blueprint(api_audit.audit_bp)
    with patch('integrations.social.auth._get_user_from_token',
               side_effect=lambda token: (user, audit_db())):
        yield app.test_client(), api_audit


H = {'Authorization': 'Bearer t'}


def test_the_agent_trail_route_returns_its_lines_and_the_export_is_jsonl(client, trail,
                                                                        monkeypatch):
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    trail.record('message', direction='in', text='hello', user_id='owner', agent_id='54')
    trail.record('message', direction='in', text='other agent', user_id='owner',
                 agent_id='7')
    r = c.get('/api/social/audit/agents/54/trail', headers=H)
    assert r.status_code == 200
    assert [x['text'] for x in r.get_json()['data']] == ['hello']
    e = c.get('/api/social/audit/agents/54/trail/export', headers=H)
    assert e.status_code == 200 and 'ndjson' in e.headers['Content-Type']
    lines = [json.loads(x) for x in e.get_data(as_text=True).splitlines()]
    assert [x['text'] for x in lines] == ['hello'] and lines[0]['hash']


def test_a_goal_page_reads_the_lines_its_turns_carry(client, trail, audit_db,
                                                     monkeypatch):
    """The page names a goal by its id; the goal's turns record the prompt id
    they ran under (saved on the row, or derived from the goal id)."""
    from integrations.agent_engine.dispatch import prompt_id_for_goal
    from integrations.social import models
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    s = audit_db()
    s.add(models.AgentGoal(id='goal-1', goal_type='marketing', title='derived id'))
    s.add(models.AgentGoal(id='goal-2', goal_type='marketing', title='saved id',
                           prompt_id='77'))
    s.commit()
    s.close()
    trail.record('message', text='derived', user_id='owner',
                 agent_id=prompt_id_for_goal('goal-1'))
    trail.record('message', text='saved', user_id='owner', agent_id='77')
    trail.record('message', text='someone else', user_id='owner', agent_id='999')
    one = c.get('/api/social/audit/agents/goal-1/trail', headers=H).get_json()['data']
    assert [x['text'] for x in one] == ['derived']
    two = c.get('/api/social/audit/agents/goal-2/trail', headers=H).get_json()['data']
    assert [x['text'] for x in two] == ['saved']
    e = c.get('/api/social/audit/agents/goal-1/trail/export', headers=H)
    assert [json.loads(x)['text'] for x in e.get_data(as_text=True).splitlines()] == [
        'derived']


def test_a_trained_agent_page_reads_the_lines_of_its_prompt(client, trail, audit_db,
                                                            monkeypatch):
    """A trained agent is listed by its users-row id; its turns record the
    prompt id kept in users.agent_id ('prompt_id_flow_id').  Review of
    a67486cf3: this card read nothing."""
    from integrations.social import models
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    s = audit_db()
    s.add(models.User(id='agent-uuid-1', username='tutor_bot', user_type='agent',
                      agent_id='54_0'))
    s.commit()
    s.close()
    trail.record('message', text='lesson one', user_id='owner', agent_id='54')
    trail.record('message', text='another agent', user_id='owner', agent_id='540')
    rows = c.get('/api/social/audit/agents/agent-uuid-1/trail', headers=H).get_json()['data']
    assert [x['text'] for x in rows] == ['lesson one']


def test_a_limit_that_is_not_a_number_is_the_default(client, trail, monkeypatch):
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    trail.record('message', text='hello', user_id='owner', agent_id='54')
    r = c.get('/api/social/audit/agents/54/trail?limit=all', headers=H)
    assert r.status_code == 200 and [x['text'] for x in r.get_json()['data']] == ['hello']


def test_the_trail_route_keeps_the_owner_check(client, trail, monkeypatch):
    c, api_audit = client
    from flask import jsonify
    monkeypatch.setattr(api_audit, '_agent_read_refused',
                        lambda agent_id: (jsonify({'success': False}), 403))
    trail.record('message', text='secret', user_id='someone', agent_id='54')
    assert c.get('/api/social/audit/agents/54/trail', headers=H).status_code == 403
    assert c.get('/api/social/audit/agents/54/trail/export', headers=H).status_code == 403


def test_a_person_sees_only_their_own_lines_and_verify_names_no_content(client, trail):
    c, _ = client
    trail.record('message', text='mine', user_id='owner')
    trail.record('message', text='not mine', user_id='someone')
    mine = c.get('/api/social/audit/trail/mine', headers=H).get_json()['data']
    assert [x['text'] for x in mine] == ['mine']
    v = c.get('/api/social/audit/trail/verify', headers=H).get_json()['data']
    assert v['ok'] is True and 'not mine' not in json.dumps(v)


# ── one view, two stores (owner, 2026-11-21) ───────────────────────────
# The security event log (ImmutableAuditLog) keeps the events that commit
# with a database transaction; the trail keeps every agent modality.  The
# agent page and its export read both as one trail.

T1 = 1_795_248_000.0  # 2026-11-21T08:00:00Z
TRAIL_URL = '/api/social/audit/agents/54/trail'


def _line(trail, seconds, text, agent_id='54'):
    """One trail message, ``seconds`` after T1."""
    trail._clock = lambda: T1 + seconds
    trail.record('message', direction='in', text=text, user_id='owner', agent_id=agent_id)


def _event(log, seconds, event_type, actor_id, action, **kw):
    """One security event, placed ``seconds`` after T1 (stored, as log_event
    stores it, as naive UTC)."""
    log.log_event(event_type, actor_id=actor_id, action=action, **kw)
    entry = log._memory_log[-1]
    entry['created_at'] = datetime.fromtimestamp(T1 + seconds, tz=timezone.utc).replace(
        tzinfo=None).isoformat()
    return entry


def _texts(c, **query):
    return [r.get('text') for r in
            c.get(TRAIL_URL, query_string=query, headers=H).get_json()['data']]


def test_the_agent_page_reads_its_security_events_beside_its_trail_lines(
        client, trail, event_log, monkeypatch):
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    _line(trail, 0, 'hello')
    pushed = _event(event_log, 1, 'a2ui_push', '54', 'push card component',
                    detail={'type': 'card'}, target_id='54')
    _event(event_log, 2, 'goal_dispatched', 'owner', 'dispatch goal 54', target_id='54')
    _event(event_log, 3, 'agent_steered', 'owner', 'pause', target_id='999')
    _line(trail, 4, 'bye')
    body = c.get(TRAIL_URL, headers=H).get_json()
    assert [r.get('text') for r in body['data']] == [
        'bye', 'goal_dispatched: dispatch goal 54', 'a2ui_push: push card component', 'hello']
    assert body['data'][2] == {
        'kind': 'security_event', 'ts': audit_trail._iso(T1 + 1),
        'text': 'a2ui_push: push card component', 'actor': '54', 'agent_id': '54',
        'hash': pushed['entry_hash'], 'prev': pushed['prev_hash'],
        'detail': {'type': 'card'}, 'source': 'event_log'}
    assert (body['data'][1]['actor'], body['data'][1]['agent_id']) == ('owner', '54')
    assert body.get('meta', {}).get('kinds') == [
        k for k in audit_trail.KINDS if k not in ('gap', 'storage_limit', 'storage_ok')
    ] + ['security_event']
    assert trail.read(kinds=['security_event']) == [], 'an event was copied into the trail'
    e = c.get(TRAIL_URL + '/export', headers=H)
    assert [json.loads(x)['text'] for x in e.get_data(as_text=True).splitlines()] == [
        'hello', 'a2ui_push: push card component', 'goal_dispatched: dispatch goal 54', 'bye']


def test_the_kinds_window_and_limit_apply_to_security_events_too(
        client, trail, event_log, monkeypatch):
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    _line(trail, 0, 'hello')
    _event(event_log, 1, 'a2ui_push', '54', 'push one', target_id='54')
    _line(trail, 2, 'middle')
    _event(event_log, 3, 'a2ui_push', '54', 'push two', target_id='54')
    assert _texts(c, kinds='security_event') == ['a2ui_push: push two', 'a2ui_push: push one']
    assert _texts(c, kinds='message') == ['middle', 'hello']
    assert _texts(c, kinds='message,security_event', limit=2) == [
        'a2ui_push: push two', 'middle']
    assert _texts(c, since=audit_trail._iso(T1 + 1), until=audit_trail._iso(T1 + 2)) == [
        'middle', 'a2ui_push: push one']
    e = c.get(TRAIL_URL + '/export', query_string={'kinds': 'security_event'}, headers=H)
    assert [json.loads(x)['text'] for x in e.get_data(as_text=True).splitlines()] == [
        'a2ui_push: push one', 'a2ui_push: push two']


def test_a_window_that_is_not_a_date_is_refused_with_what_to_send(
        client, trail, event_log, monkeypatch):
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    for url in (TRAIL_URL, TRAIL_URL + '/export'):
        r = c.get(url, query_string={'since': 'yesterday'}, headers=H)
        assert r.status_code == 400, url
        assert 'ISO date' in r.get_json()['error'], url


def test_a_secret_in_a_security_event_is_hidden_as_the_trail_hides_one(
        client, trail, event_log, monkeypatch):
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    _event(event_log, 1, 'a2ui_push', '54', 'pushed password=hunter2xyz',
           detail={'note': 'card 4111 1111 1111 1111', 'api_key': 'sk-live-1'},
           target_id='54')
    rows = c.get(TRAIL_URL, headers=H).get_json()['data']
    assert len(rows) == 1
    assert 'hunter2xyz' not in rows[0]['text']
    assert '4111 1111 1111 1111' not in rows[0]['detail']['note']
    assert rows[0]['detail']['api_key'] == '[REDACTED]'


def test_verify_checks_the_event_log_too_and_a_broken_one_fails_the_whole(
        client, trail, event_log):
    c, _ = client
    trail.record('message', text='mine', user_id='owner')
    event_log.log_event('a2ui_push', actor_id='54', action='push one')
    event_log.log_event('a2ui_push', actor_id='54', action='push two')
    v = c.get('/api/social/audit/trail/verify', headers=H).get_json()['data']
    assert v.get('event_log') == {'ok': True, 'reason': 'Chain valid (2 entries)',
                                  'checked': 2, 'total': 2}
    assert v['ok'] is True and 'segments' in v and 'missing' in v
    event_log._memory_log[0]['action'] = 'push one (changed)'
    v = c.get('/api/social/audit/trail/verify', headers=H).get_json()['data']
    assert v.get('event_log', {}).get('ok') is False
    assert 'Chain broken' in v['event_log']['reason']
    assert v['ok'] is False
    assert v['segments'] and all(s['ok'] for s in v['segments']), 'the trail is intact'


# ── review of eab49c5e7 (rv_eab4) ──────────────────────────────────────

VERIFY_URL = '/api/social/audit/trail/verify'


def test_verify_walks_every_event_and_the_newest_of_1005_changed_breaks_it(
        client, trail, event_log):
    """P2: verify read the oldest 1000 entries and said ok over the rest,
    the very entries the page shows."""
    c, _ = client
    for i in range(1005):
        event_log.log_event('a2ui_push', actor_id='54', action=f'push {i}', target_id='54')
    v = c.get(VERIFY_URL, headers=H).get_json()['data']
    assert (v['event_log'].get('checked'), v['event_log'].get('total')) == (1005, 1005)
    assert v['ok'] is True
    event_log._memory_log[-1]['action'] = 'push 1004 (changed after the fact)'
    v = c.get(VERIFY_URL, headers=H).get_json()['data']
    assert v['event_log']['ok'] is False
    assert 'Chain broken at entry 1005' in v['event_log']['reason']
    assert v['ok'] is False


def test_verify_never_says_intact_over_events_it_did_not_read(
        client, trail, event_log, monkeypatch):
    """A walk stopped by the page's time budget reports how far it got, and
    the whole-node 'ok' stays False until every entry was read."""
    from security.immutable_audit_log import ImmutableAuditLog
    c, api_audit = client
    for i in range(1005):
        event_log.log_event('a2ui_push', actor_id='54', action=f'push {i}', target_id='54')
    monkeypatch.setattr(api_audit, 'VERIFY_BUDGET_S', 0, raising=False)
    monkeypatch.setattr(event_log, 'check_chain', lambda budget_s=None: (
        ImmutableAuditLog.check_chain(event_log, budget_s=budget_s, batch=100)))
    v = c.get(VERIFY_URL, headers=H).get_json()['data']
    assert (v['event_log']['ok'], v['event_log'].get('checked'),
            v['event_log'].get('total')) == (True, 100, 1005)
    assert v['ok'] is False, 'intact claimed over 905 entries nobody read'


def test_a_bad_window_is_refused_whatever_kinds_are_asked_for(
        client, trail, event_log, monkeypatch):
    """P1: with kinds that leave out security events, a bad since was read
    as no window and answered 200 with nothing."""
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    _line(trail, 0, 'hello')
    for url in (TRAIL_URL, TRAIL_URL + '/export', '/api/social/audit/trail/mine'):
        for query in ({'kinds': 'message', 'since': 'yesterday'},
                      {'kinds': 'message', 'until': 'tomorrow'}):
            r = c.get(url, query_string=query, headers=H)
            assert r.status_code == 400, (url, query)
            assert 'ISO date' in r.get_json()['error'], (url, query)


def test_both_stores_keep_one_window_rule(client, trail, event_log, monkeypatch):
    """P3 / P5: the trail compared its window as text and the event log as
    times, so a zoned since dropped the trail line at the very instant it
    named while the event at that instant showed, and a date-only until
    dropped that whole day from both."""
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    _line(trail, 10, 'trail line at 08:00:10Z')
    _event(event_log, 10, 'a2ui_push', '54', 'event at 08:00:10Z', target_id='54')
    both = ['trail line at 08:00:10Z', 'a2ui_push: event at 08:00:10Z']
    assert sorted(_texts(c, since='2026-11-21T13:30:10+05:30')) == sorted(both)
    assert sorted(_texts(c, until='2026-11-21T13:30:10+05:30')) == sorted(both)
    assert sorted(_texts(c, since='2026-11-21', until='2026-11-21')) == sorted(both)
    assert _texts(c, since='2026-11-21T08:00:10.000500+00:00') == []
    assert _texts(c, until='2026-11-21T08:00:09.999500+00:00') == []
    mine = c.get('/api/social/audit/trail/mine',
                 query_string={'since': '2026-11-21T13:30:10+05:30'}, headers=H)
    assert [r['text'] for r in mine.get_json()['data']] == ['trail line at 08:00:10Z']


def test_an_agent_page_shows_only_entries_whose_type_names_that_agent(
        client, trail, event_log, monkeypatch):
    """P4: core.platform.bootstrap logs action_retry_exhausted with the step
    number as actor_id, and a window op's target_id is the window's con_id;
    matched as text, both showed on agent 54's page."""
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    event_log.log_event('action_retry_exhausted', actor_id=54, action='exhausted 3 retries',
                        detail={'user_id': 'someone_else'})
    event_log.log_event('wm_window_op', actor_id='7', action='window.close target=54',
                        target_id='54')
    event_log.log_event('a2ui_push', actor_id='54', action='push card', target_id='54')
    rows = c.get(TRAIL_URL, headers=H).get_json()['data']
    assert [(r['text'], r['agent_id']) for r in rows] == [('a2ui_push: push card', '54')]
    rows = c.get('/api/social/audit/agents/7/trail', headers=H).get_json()['data']
    assert [(r['text'], r['agent_id']) for r in rows] == [
        ('wm_window_op: window.close target=54', '7')]


def test_the_page_never_offers_the_lines_the_trail_writes_about_itself(
        client, trail, event_log, monkeypatch):
    """Every line the writer writes about itself (lost records, the storage
    cap starting and ending) is a housekeeping kind, and none is offered as
    a filter: VIEW_KINDS is derived from the writer's own names, not a second
    hand-kept list."""
    c, api_audit = client
    monkeypatch.setattr(api_audit, '_agent_read_refused', lambda agent_id: None)
    trail.min_free_bytes = 0  # this machine's free disk must not hold the cap on
    trail.record('message', text='first', user_id='owner', agent_id='54')
    trail.drain()
    trail._lost = 2                                    # a gap line
    trail.max_bytes, trail._last_storage_check = 1, 0.0  # storage_limit
    trail.record('message', text='second', user_id='owner', agent_id='54')
    trail.drain()
    trail.max_bytes, trail._last_storage_check = 10**12, 0.0  # storage_ok
    trail.record('message', text='third', user_id='owner', agent_id='54')
    trail.drain()
    about_itself = {r['kind'] for r in trail.read(limit=50)} - {'message'}
    assert about_itself == set(audit_trail.HOUSEKEEPING_KINDS)
    offered = c.get(TRAIL_URL, headers=H).get_json()['meta']['kinds']
    assert not about_itself & set(offered)
    assert set(audit_trail.KINDS) - about_itself <= set(offered)
