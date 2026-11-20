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
def client(trail, audit_db, monkeypatch):
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
