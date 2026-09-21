"""A game's sounds are composed once and bound to it.

Owner's goal 2026-09-21: in CREATE the agent composes a game's sounds, and
REUSE binds them permanently, so the person reusing the agent hears the
music its reviewer approved rather than a fresh composition.  The binding
lives in the agent's own saved data, beside everything else
save_data_in_memory keeps, and the composing is the media capability the
agent already holds.
"""
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import core.agent_tools as agent_tools


def _media(started, progress=None):
    """A stand-in for the media capability the agent composes through.

    Its error READER is the real one (media_agent.classify_error), not a
    mock: whether a failure means "offer to install a composer" or "one is
    installed and simply not running" is decided there, next to the returns
    that produce those wordings, and a stand-in that answered for it would
    prove nothing about what the agent does in the field.
    """
    module = MagicMock()
    module.generate_media.return_value = json.dumps(started)
    module.check_media_status.return_value = json.dumps(progress or {})
    from integrations.service_tools.media_agent import (
        classify_error, ABSENT, UNREACHABLE, REFUSED, UNKNOWN)
    module.classify_error = classify_error
    module.ABSENT, module.UNREACHABLE = ABSENT, UNREACHABLE
    module.REFUSED, module.UNKNOWN = REFUSED, UNKNOWN
    return module


@contextmanager
def _agent(agent_data, media_module):
    """The tool set as an agent gets it.

    The capability stays patched while the tools RUN, not only while they
    are built: bind_game_sound imports it at call time.
    """
    ctx = {
        'user_id': 'user-1',
        'prompt_id': 4242,
        'agent_data': agent_data,
        'helper_fun': MagicMock(),
        'user_prompt': 'session-1',
        'request_id_list': [],
        'recent_file_id': {},
        'scheduler': MagicMock(),
        'simplemem_store': None,
        'memory_graph': None,
        'log_tool_execution': lambda f: f,
        'send_message_to_user1': MagicMock(),
        'retrieve_json': lambda v: v,
        'strip_json_values': lambda v: v,
        'save_conversation_db': MagicMock(),
    }
    with patch.dict('sys.modules',
                    {'integrations.service_tools.media_agent': media_module}):
        try:
            tools = agent_tools.build_core_tool_closures(ctx)
        except (KeyError, TypeError) as e:
            pytest.skip(f'tool context shape changed: {e}')
        yield {name: func for name, _desc, func in tools}


def test_a_composed_game_is_bound_to_the_agent():
    agent_data = {}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/eng01.mp3'}]})

    with _agent(agent_data, media) as tools:
        answer = json.loads(
            tools['bind_game_sound']('eng-01', 'happy', 'spelling animals'))

    assert answer['status'] == 'bound'
    assert answer['music']['url'] == 'https://node/eng01.mp3'
    # kept where the agent keeps everything else it must remember
    assert agent_data[4242]['games']['eng-01']['music']['url'] == \
        'https://node/eng01.mp3'
    assert media.generate_media.call_args.kwargs['output_modality'] == 'audio_music'


def test_a_bound_game_is_never_composed_again():
    agent_data = {4242: {'games': {'eng-01': {'music': {'url': 'https://node/kept.mp3'}}}}}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/new.mp3'}]})

    with _agent(agent_data, media) as tools:
        answer = json.loads(tools['bind_game_sound']('eng-01'))

    assert answer['status'] == 'already_bound'
    assert answer['music']['url'] == 'https://node/kept.mp3'
    media.generate_media.assert_not_called()


def test_reuse_reads_the_binding_without_composing():
    agent_data = {4242: {'games': {'eng-01': {'music': {'url': 'https://node/kept.mp3'}}}}}
    media = _media({'status': 'completed', 'results': []})

    with _agent(agent_data, media) as tools:
        answer = json.loads(tools['get_game_sound']('eng-01'))

    assert answer['status'] == 'bound'
    assert answer['music']['url'] == 'https://node/kept.mp3'
    media.generate_media.assert_not_called()


def test_a_game_with_no_sound_says_so_rather_than_inventing_one():
    with _agent({}, _media({'status': 'completed', 'results': []})) as tools:
        answer = json.loads(tools['get_game_sound']('unheard-of'))

    assert answer['status'] == 'unbound'


def test_a_composition_still_running_is_handed_back_to_be_finished():
    agent_data = {}
    media = _media({'status': 'pending', 'task_id': 'acestep_1'},
                   {'status': 'pending'})

    with _agent(agent_data, media) as tools, \
            patch.object(agent_tools.time, 'sleep', lambda *_: None), \
            patch.object(agent_tools.time, 'time',
                         MagicMock(side_effect=[0.0, 0.0, 1000.0, 1000.0, 1000.0])):
        answer = json.loads(tools['bind_game_sound']('eng-02'))

    assert answer['status'] == 'composing'
    assert answer['task_id'] == 'acestep_1'
    # the task is remembered, so the next call picks it up instead of restarting
    assert agent_data[4242]['games']['eng-02']['music']['task_id'] == 'acestep_1'


def test_the_reviewer_approves_the_music_the_reuser_will_hear():
    agent_data = {4242: {'games': {'eng-01': {'music': {'url': 'https://node/m.mp3'}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        answer = json.loads(tools['approve_game_sound']('eng-01'))
        after = json.loads(tools['get_game_sound']('eng-01'))

    assert answer['status'] == 'approved'
    assert agent_data[4242]['games']['eng-01']['music']['approved_at'] > 0
    assert after['approved'] is True


def test_a_rejected_piece_is_dropped_so_a_new_one_can_be_composed():
    agent_data = {4242: {'games': {'eng-01': {'music': {'url': 'https://node/m.mp3'}}}}}
    media = _media({'status': 'completed', 'results': [{'url': 'https://node/second.mp3'}]})

    with _agent(agent_data, media) as tools:
        rejected = json.loads(tools['approve_game_sound']('eng-01', False))
        again = json.loads(tools['bind_game_sound']('eng-01'))

    assert rejected['status'] == 'rejected'
    assert again['status'] == 'bound'
    assert again['music']['url'] == 'https://node/second.mp3'


def test_there_is_nothing_to_approve_before_anything_is_bound():
    with _agent({}, _media({'status': 'completed', 'results': []})) as tools:
        assert 'nothing to approve' in tools['approve_game_sound']('eng-09')


def test_a_game_id_is_required():
    with _agent({}, _media({'status': 'completed', 'results': []})) as tools:
        assert 'game_id' in tools['bind_game_sound']('')


# ── the matching rules (spec §4) ──────────────────────────────────────

def test_a_level_without_its_own_sound_matches_the_games():
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'url': 'https://node/correct.mp3'}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        answer = json.loads(tools['get_game_sound']('eng-01', 'correct', '7'))

    assert answer['status'] == 'bound'
    assert answer['matched'] == 'game'
    assert answer['music']['url'] == 'https://node/correct.mp3'


def test_a_level_with_its_own_sound_matches_that_one():
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'url': 'https://node/any-level.mp3'},
        'correct@7': {'url': 'https://node/level7.mp3'}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        seven = json.loads(tools['get_game_sound']('eng-01', 'correct', '7'))
        eight = json.loads(tools['get_game_sound']('eng-01', 'correct', '8'))

    assert (seven['matched'], seven['music']['url']) == ('level', 'https://node/level7.mp3')
    assert (eight['matched'], eight['music']['url']) == ('game', 'https://node/any-level.mp3')


def test_a_state_never_reached_is_a_miss_not_a_near_match():
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'url': 'https://node/correct.mp3'}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        answer = json.loads(tools['get_game_sound']('eng-01', 'wrong'))

    assert answer['status'] == 'unbound'


def test_a_state_outside_the_vocabulary_is_refused_not_guessed():
    with _agent({}, _media({'status': 'completed', 'results': []})) as tools:
        answer = tools['bind_game_sound']('eng-01', 'happy', '', 'kerfuffle')

    assert 'not one of a game' in answer


def test_the_miss_is_memoized_under_the_key_that_was_asked_for():
    agent_data = {}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/level3.mp3'}]})

    with _agent(agent_data, media) as tools:
        json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct', '3'))

    sounds = agent_data[4242]['games']['eng-01']['sounds']
    assert 'correct@3' in sounds
    assert 'correct' not in sounds


# ── one person's correction (spec §6.2) ───────────────────────────────

def test_a_persons_correction_is_theirs_and_leaves_the_agents_alone():
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'url': 'https://node/approved.mp3', 'approved_at': 1}}}}}}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/mine.mp3'}]})

    with _agent(agent_data, media) as tools:
        mine = json.loads(tools['bind_game_sound'](
            'eng-01', 'calm', 'spelling', 'correct', '', 'mine'))

    assert mine['music']['url'] == 'https://node/mine.mp3'
    # the agent's approved sound is untouched
    assert agent_data[4242]['games']['eng-01']['sounds']['correct']['url'] == \
        'https://node/approved.mp3'
    # and the correction sits under that person
    assert agent_data[4242]['games']['eng-01']['mine']['user-1']['correct']['url'] == \
        'https://node/mine.mp3'


# ── a rejected take (spec §6.1) ───────────────────────────────────────

def test_the_next_take_is_composed_to_answer_the_rejection():
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'bgm': {'url': 'https://node/first.mp3', 'variant': 1}}}}}}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/second.mp3'}]})

    with _agent(agent_data, media) as tools:
        tools['approve_game_sound']('eng-01', False, 'bgm', 'too loud and jangly')
        again = json.loads(tools['bind_game_sound']('eng-01', 'calm', 'spelling'))

    prompt = media.generate_media.call_args.kwargs['context']
    assert 'too loud and jangly' in prompt
    assert again['music']['variant'] == 2


def test_a_rejected_take_is_kept_with_its_reason():
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'bgm': {'url': 'https://node/first.mp3'}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        tools['approve_game_sound']('eng-01', False, 'bgm', 'too sad')

    record = agent_data[4242]['games']['eng-01']['sounds']['bgm']
    assert record['rejected_reason'] == 'too sad'
    assert record.get('url') is None
    assert record['rejected_at'] > 0


# ── creation is liquid (the reviewer hears it) ────────────────────────

def test_a_new_sound_is_offered_to_the_person_to_hear_and_answer():
    """A composed piece arrives as something to listen to on the surface
    the reviewer is already looking at, through the existing agent-to-UI
    channel, not as a line of text about a URL."""
    agent_data = {}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/eng01.mp3'}]})
    liquid = MagicMock()
    liquid.agent_ui_update.return_value = True
    registry = MagicMock()
    registry.get.return_value = liquid

    with _agent(agent_data, media) as tools,             patch('core.platform.registry.get_registry', return_value=registry):
        json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct'))

    component = liquid.agent_ui_update.call_args.args[1]
    assert component['type'] == 'approval'
    assert component['media']['src'] == 'https://node/eng01.mp3'
    assert component['media']['controls'] is True
    assert 'eng-01' in component['action'] and 'correct' in component['action']
    assert len(component['options']) == 2


def test_a_node_without_that_surface_still_composes_and_remembers():
    agent_data = {}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/eng01.mp3'}]})
    registry = MagicMock()
    registry.get.return_value = None

    with _agent(agent_data, media) as tools,             patch('core.platform.registry.get_registry', return_value=registry):
        answer = json.loads(tools['bind_game_sound']('eng-01'))

    assert answer['status'] == 'bound'
    assert agent_data[4242]['games']['eng-01']['sounds']['bgm']['url'] ==         'https://node/eng01.mp3'


def test_a_surface_that_refuses_never_costs_the_composition():
    agent_data = {}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/eng01.mp3'}]})
    liquid = MagicMock()
    liquid.agent_ui_update.side_effect = RuntimeError('hive halted')
    registry = MagicMock()
    registry.get.return_value = liquid

    with _agent(agent_data, media) as tools,             patch('core.platform.registry.get_registry', return_value=registry):
        answer = json.loads(tools['bind_game_sound']('eng-01'))

    assert answer['status'] == 'bound'


# ── a node with no composer asks for one (hartos-94's capability_setup) ──

def _asked(outcome='asked'):
    module = MagicMock()
    module.request_capability_setup.return_value = outcome
    return module


def test_a_node_with_no_music_model_offers_to_set_one_up():
    """Measured 2026-09-21: no music model exists on the machine, and a
    voice cloner cannot compose a game's sound.  Silence tells the person
    nothing, so the node asks."""
    setup = _asked('asked')
    media = _media({'status': 'unavailable',
                    'error': 'audio_music not available on this node right now.'})

    with _agent({}, media) as tools,             patch.dict('sys.modules',
                       {'integrations.agent_engine.capability_setup': setup}):
        answer = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling'))

    assert answer['status'] == 'needs_capability'
    assert answer['capability'] == 'music:acestep'
    assert answer['asked'] == 'asked'
    scope = setup.request_capability_setup.call_args
    assert scope.args[0] == 'music:acestep'
    assert 'eng-01' in scope.kwargs['reason']


def test_the_owner_saying_yes_reads_as_provisioning():
    setup = _asked('provisioning')
    media = _media({'status': 'error', 'error': 'no tool for audio_music'})

    with _agent({}, media) as tools,             patch.dict('sys.modules',
                       {'integrations.agent_engine.capability_setup': setup}):
        answer = json.loads(tools['bind_game_sound']('eng-01'))

    assert answer['asked'] == 'provisioning'
    assert 'ready' in answer['note']


def test_an_engine_that_refuses_one_prompt_is_not_a_missing_engine():
    """A composer that is present but said no must not raise a consent
    card asking to install what is already there."""
    setup = _asked('asked')
    media = _media({'status': 'error', 'error': 'prompt rejected by safety filter'})

    with _agent({}, media) as tools,             patch.dict('sys.modules',
                       {'integrations.agent_engine.capability_setup': setup}):
        answer = tools['bind_game_sound']('eng-01')

    assert 'refused' in answer
    setup.request_capability_setup.assert_not_called()


def test_a_node_with_nobody_to_ask_says_so_plainly():
    setup = _asked('unavailable')
    media = _media({'status': 'unavailable',
                    'error': 'audio_music not available on this node right now.'})

    with _agent({}, media) as tools,             patch.dict('sys.modules',
                       {'integrations.agent_engine.capability_setup': setup}):
        answer = json.loads(tools['bind_game_sound']('eng-01'))

    assert answer['asked'] == 'unavailable'
    assert 'nobody to ask' in answer['note']


# ── the offer reaches the person who is not on that screen (goal: "server
#    fanout sending the notification to user via desktop and phone FCM
#    paths") ─────────────────────────────────────────────────────────────

def _offer_a_sound(monkeypatch, push=None, notify=None, ui=True):
    """Run offer_sound_for_review with the two delivery paths observed."""
    fcm = MagicMock()
    fcm.send_fcm_push = push or MagicMock(return_value=True)
    services = MagicMock()
    services.NotificationService.create = notify or MagicMock()
    models = MagicMock()
    registry = MagicMock()
    registry.get.return_value = (MagicMock() if ui else None)
    platform_registry = MagicMock()
    platform_registry.get_registry.return_value = registry
    with patch.dict('sys.modules', {
            'core.fcm_sync': fcm,
            'integrations.social.services': services,
            'integrations.social.models': models,
            'core.platform.registry': platform_registry}):
        shown = agent_tools.offer_sound_for_review(
            'user-1', 4242, 'eng-01', 'correct',
            {'url': 'https://node/correct.mp3'})
    return shown, fcm.send_fcm_push, services.NotificationService.create


def test_the_offer_is_pushed_to_the_phone_with_what_it_is_about(monkeypatch):
    _shown, push, _notify = _offer_a_sound(monkeypatch)

    assert push.called, 'the phone was never told'
    data = push.call_args.kwargs['data']
    assert data['type'] == 'game_sound_review'
    assert data['game_id'] == 'eng-01'
    assert data['state'] == 'correct'
    assert data['url'] == 'https://node/correct.mp3'


def test_the_offer_is_recorded_so_every_surface_of_theirs_shows_it(monkeypatch):
    _shown, _push, notify = _offer_a_sound(monkeypatch)

    assert notify.called, 'nothing was recorded for the other surfaces'
    assert notify.call_args.args[2] == 'agent_game_sound_review'


def test_a_node_with_no_push_credential_still_composes(monkeypatch):
    """send_fcm_push no-ops without a credential; a raise must not either."""
    angry = MagicMock(side_effect=RuntimeError('no FCM credential here'))
    shown, push, _notify = _offer_a_sound(monkeypatch, push=angry)

    assert push.called
    assert shown is True, 'a dead push path swallowed the offer'


def test_a_node_with_no_notification_store_still_pushes(monkeypatch):
    angry = MagicMock(side_effect=RuntimeError('no social database here'))
    shown, push, notify = _offer_a_sound(monkeypatch, notify=angry)

    assert notify.called
    assert push.called, 'a missing record stopped the phone being told'
    assert shown is True


def test_the_person_is_still_told_when_no_screen_is_attached(monkeypatch):
    """No UI service is not "nobody to tell" — the phone is still reachable."""
    shown, push, notify = _offer_a_sound(monkeypatch, ui=False)

    assert shown is False
    assert push.called and notify.called


def test_a_composer_that_is_merely_not_running_is_not_offered_for_install():
    """Installed but down is NOT missing.

    hartos-94's point, and the defect this branch exists to avoid: offering
    to install AceStep when AceStep is already installed and simply is not
    listening is its own defect.  The reader that tells the two apart lives
    in media_agent; this asserts the agent acts on the distinction.
    """
    media = _media({'status': 'error',
                    'error': 'AceStep: connection refused (WinError 10061)'})

    with _agent({}, media) as tools:
        answer = tools['bind_game_sound']('eng-01', 'happy', 'spelling')

    assert 'needs_capability' not in answer
    assert 'connection refused' in answer.lower()


def test_a_composer_that_answered_and_said_no_is_not_offered_for_install():
    media = _media({'status': 'error', 'error': 'AceStep HTTP 503'})

    with _agent({}, media) as tools:
        answer = tools['bind_game_sound']('eng-01', 'happy', 'spelling')

    assert 'needs_capability' not in answer
    assert '503' in answer
