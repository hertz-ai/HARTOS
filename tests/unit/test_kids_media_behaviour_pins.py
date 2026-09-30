"""Behaviour pins for kids-media changes that no test could catch.

hartos-3a's review (F11) found mutants that survived every test: each of
these changes could be reverted and the suite would stay green.  Each test
here drives the function itself -- none reads source text -- and each was
mutation-checked when it landed.

    python -m pytest tests/unit/test_kids_media_behaviour_pins.py -q
"""
import json
from unittest.mock import MagicMock, patch

import integrations.service_tools.media_agent as ma


def _submit_payload(**over):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': {'task_id': 't1', 'status': 'queued'}, 'code': 200}
    with patch.object(ma, '_node_has_any', return_value=True), \
            patch.object(ma, '_start_tool', return_value={'running': True}), \
            patch.object(ma, '_get_tool_base_url', return_value='http://127.0.0.1:1'), \
            patch('core.http_pool.pooled_post', return_value=resp) as post:
        ma._generate_audio_music('a chime', '', 2, '')
    return post.call_args


def test_the_composer_is_asked_for_wav():
    """41cd45501: mp3 needs ffmpeg, which is absent; AceStep composed and
    then threw the take away while reporting success."""
    assert _submit_payload().kwargs['json']['audio_format'] == 'wav'


def test_a_cold_composer_is_given_time_to_answer_its_own_submit():
    """58182e544: the HTTP thread blocks while the model loads, so a short
    timeout turned an ACCEPTED submit into a reported failure."""
    assert _submit_payload().kwargs['timeout'] >= 120


def test_success_with_nothing_saved_is_not_done():
    """41cd45501: AceStep reported success after its save threw the audio
    away; 'completed' with no file would bind music that does not exist."""
    item = {'task_id': 'abc', 'status': 1, 'progress_text': 'done',
            'result': json.dumps([{'file': '', 'status': 1}])}
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'data': [item], 'code': 200}
    with patch.object(ma, '_get_tool_base_url', return_value='http://127.0.0.1:1'), \
            patch('core.http_pool.pooled_post', return_value=resp):
        out = json.loads(ma.check_media_status('acestep_abc'))
    assert out['status'] == 'error', out
    assert 'no artifact' in out['error'], out


def test_a_runtime_started_sidecar_is_found_on_the_port_it_was_given():
    """adb624be9: only the service registry was asked, so a sidecar HARTOS
    started itself (port assigned at launch) resolved to 'not running'."""
    empty_registry = MagicMock(_tools={})
    runtime = MagicMock()
    runtime.get_tool_status.return_value = {'running': True, 'port': 63734}
    with patch('integrations.service_tools.registry.service_tool_registry', empty_registry), \
            patch('integrations.service_tools.runtime_manager.runtime_tool_manager', runtime):
        assert ma._get_tool_base_url('acestep') == 'http://127.0.0.1:63734'
    runtime.get_tool_status.return_value = {'running': False, 'port': 63734}
    with patch('integrations.service_tools.registry.service_tool_registry', empty_registry), \
            patch('integrations.service_tools.runtime_manager.runtime_tool_manager', runtime):
        assert ma._get_tool_base_url('acestep') is None


def test_a_reset_reads_as_busy_even_without_connection_aborted():
    """0e80eee1e: its fixture also said 'Connection aborted.', which the
    older clause already matched, so the new clauses were never exercised."""
    assert ma._reads_as_still_waking(
        "ConnectionResetError(10054, 'An existing connection was forcibly closed')")
    assert ma._reads_as_still_waking('[WinError 10054] forcibly closed')
    assert not ma._reads_as_still_waking('[WinError 10061] actively refused')


def test_a_degraded_segment_says_which_kind_of_no():
    """2f4ff9e22: 'offline' for a node that has the engine and is only busy
    sends the reader to install what is already here."""
    with patch.object(ma, '_node_has_any', return_value=True):
        assert 'installed but cannot run right now' in ma._degraded_reason('tts', 'TTS')
    with patch.object(ma, '_node_has_any', return_value=False):
        assert ma._degraded_reason('tts', 'TTS') == 'TTS service offline'


def test_a_notification_has_a_title_a_person_can_read():
    """5c8c08d70: clients render `title || type`, so a person saw the slug
    'agent_game_sound_review' as the headline."""
    from integrations.social.models import Notification as cls
    assert cls(type='agent_game_sound_review').title_for_humans() == 'A new game sound'
    title = cls(type='some_new_kind').title_for_humans()
    assert '_' not in title and title.lower().startswith('some new kind'), title
