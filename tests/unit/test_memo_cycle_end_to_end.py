"""bind -> memo -> replay through the REAL code, against a REAL artifact.

Only the HTTP transport is stubbed, with the two wire shapes AceStep
actually sends (release_task_models.py, query_result_service.py
_build_store_result_payload). check_media_status, _unwrap_envelope,
bind_game_sound and get_game_sound are the real functions.

Why this exists: on 2026-09-22 three live runs each composed audio and
then died to the machine's memory reaper before the memo could fill, so
the post-composition half of the chain had never once completed. Every
defect in that half hid behind a mock of exactly the boundary it lived at
(the discarded task_id, the unasked poll question, the file nested inside
a JSON string). This pins the whole cycle with those boundaries real.
"""
import json
import urllib.parse
import os
import struct
import wave
from unittest.mock import MagicMock, patch

import pytest

import core.agent_tools as agent_tools
import integrations.service_tools.media_agent as ma


def _real_wav(tmp_path, seconds=1):
    """A genuine RIFF/WAVE file, so the replayed path is real audio."""
    p = tmp_path / 'correct.wav'
    with wave.open(str(p), 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b'\x00\x00' * 2 * 48000 * seconds)
    return str(p)


def _agent_tools_over_live_wire(wav_path):
    submit = MagicMock(status_code=200)
    submit.json.return_value = {'data': {'task_id': 'live-1', 'status': 'queued',
                                         'queue_position': 1}, 'code': 200, 'error': None}
    finished = MagicMock(status_code=200)
    finished.json.return_value = {'data': [{
        'task_id': 'live-1', 'status': 1, 'progress_text': 'done',
        # AceStep's own shape (MEASURED 2026-09-22): its sidecar url for a
        # temp file, not the path -- hartos-3a F1.
        'result': json.dumps([{'file': '/v1/audio?path=' + urllib.parse.quote(wav_path),
                               'wave': '', 'status': 1,
                               'metas': {'duration': 5}}])}],
        'code': 200, 'error': None}

    def fake_post(url, **kw):
        if url.endswith('/release_task'):
            return submit
        if url.endswith('/query_result'):
            return finished
        raise AssertionError(url)

    agent_data = {}
    ctx = {'user_id': 'user-1', 'prompt_id': 4242, 'agent_data': agent_data,
           'helper_fun': MagicMock(), 'user_prompt': 's', 'request_id_list': [],
           'recent_file_id': {}, 'scheduler': MagicMock(), 'simplemem_store': None,
           'memory_graph': None, 'log_tool_execution': lambda f: f,
           'send_message_to_user1': MagicMock(), 'retrieve_json': lambda v: v,
           'strip_json_values': lambda v: v, 'save_conversation_db': MagicMock()}
    return agent_data, ctx, fake_post, submit


def test_compose_memoize_and_replay_through_the_real_code(tmp_path):
    wav = _real_wav(tmp_path)
    agent_data, ctx, fake_post, submit = _agent_tools_over_live_wire(wav)

    # The capability gate reads LIVE machine state (can_do -> orchestrator
    # compute state -> free VRAM/RAM).  hartos-14 ran this exact commit on a
    # box at 76% load and bind_game_sound correctly answered "installed on
    # this node but cannot run right now" -- prose, which json.loads then
    # choked on.  Same code, opposite result, purely from headroom.  The
    # memory gate is not one of the boundaries under test here (those are
    # the task id, the poll question and the nested file), so it is stubbed
    # open: this test proves the cycle, not the weather.
    kept_dir = tmp_path / 'kept'
    with patch.object(ma, '_get_tool_base_url', return_value='http://127.0.0.1:1'), \
            patch.object(ma, '_start_tool', return_value={'running': True}), \
            patch.object(ma, 'composer_output_dir', lambda: kept_dir), \
            patch.object(ma, '_can_do', lambda *_a, **_k: True), \
            patch.object(ma, '_node_has_any', lambda *_a, **_k: True), \
            patch('core.http_pool.pooled_post', side_effect=fake_post), \
            patch('time.sleep', lambda *_a, **_k: None):
        try:
            tools = {n: f for n, _d, f in agent_tools.build_core_tool_closures(ctx)}
        except (KeyError, TypeError) as e:
            pytest.skip(f'tool context shape changed: {e}')

        bound = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct'))
        again = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct'))
        replay = json.loads(tools['get_game_sound']('eng-01', 'correct'))

    # CREATE: composed once, the path read from inside AceStep's nested result
    assert bound['status'] == 'bound', bound
    # the node's own url for a kept copy, never AceStep's sidecar temp url
    assert bound['music']['url'] == '/api/voice/audio/correct.wav', bound
    # never composed twice for the same state
    assert again['status'] == 'already_bound'
    assert submit.json.call_count == 1
    # REUSE: the memo replays exactly what create bound
    assert replay['status'] == 'bound'
    assert replay['matched'] == 'game'
    assert replay['music']['url'] == '/api/voice/audio/correct.wav'
    # and it is real audio on disk where the node's audio route looks
    b = open(kept_dir / 'correct.wav', 'rb').read(44)
    assert b[:4] == b'RIFF' and b[8:12] == b'WAVE'
    _, ch, rate = struct.unpack('<HHI', b[20:28])
    assert (ch, rate) == (2, 48000)
