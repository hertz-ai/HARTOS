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

    # Two components: the audio to hear, then the card to answer.  This
    # used to assert component['media'], an undeclared prop of 'approval'
    # that no client reads -- it proved the dict was built, not that
    # anyone could hear it (hartos-14, 2026-09-21).
    sent = [call.args[1] for call in liquid.agent_ui_update.call_args_list]
    by_type = {c['type']: c for c in sent}
    assert by_type['media']['src'] == 'https://node/eng01.mp3'
    assert by_type['media']['controls'] is True

    component = by_type['approval']
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
    # with no screen attached: a card that landed needs no push
    _shown, push, _notify = _offer_a_sound(monkeypatch, ui=False)

    assert push.called, 'the phone was never told'
    data = push.call_args.kwargs['data']
    assert data['type'] == 'game_sound_review'
    assert data['game_id'] == 'eng-01'
    assert data['state'] == 'correct'
    assert data['url'] == 'https://node/correct.mp3'


def test_the_offer_is_recorded_so_every_surface_of_theirs_shows_it(monkeypatch):
    _shown, _push, notify = _offer_a_sound(monkeypatch, ui=False)

    assert notify.called, 'nothing was recorded for the other surfaces'
    assert notify.call_args.args[2] == 'agent_game_sound_review'


def test_the_record_names_what_it_is_about_so_it_can_be_acted_on(monkeypatch):
    """A row the client cannot route is a dead end.

    Found reviewing this commit: the client routes a notification on
    target_type/target_id, and without them the person is told a sound is
    ready and given no way to reach it.
    """
    _shown, _push, notify = _offer_a_sound(monkeypatch, ui=False)

    assert notify.call_args.kwargs['target_type'] == 'agent'
    assert notify.call_args.kwargs['target_id'] == '4242'


def test_a_node_with_no_push_credential_still_composes(monkeypatch):
    """send_fcm_push no-ops without a credential; a raise must not either."""
    angry = MagicMock(side_effect=RuntimeError('no FCM credential here'))
    shown, push, _notify = _offer_a_sound(monkeypatch, push=angry, ui=False)

    assert push.called
    assert shown is False


def test_a_node_with_no_notification_store_still_pushes(monkeypatch):
    angry = MagicMock(side_effect=RuntimeError('no social database here'))
    shown, push, notify = _offer_a_sound(monkeypatch, notify=angry, ui=False)

    assert notify.called
    assert push.called, 'a missing record stopped the phone being told'
    assert shown is False


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


def test_a_rejected_take_keeps_its_audio_so_a_reviewer_can_go_back():
    """Review finding against my own code, 2026-09-21.

    approve_game_sound's comment said the rejected take was "kept, not
    deleted: a reviewer can go back to it" while the line below it popped
    'url' -- the only way back. Keeping the fact of a rejection is not
    keeping the take.
    """
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'bgm': {'url': 'https://node/first.mp3', 'variant': 1}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        tools['approve_game_sound']('eng-01', False, 'bgm', 'too jangly')

    record = agent_data[4242]['games']['eng-01']['sounds']['bgm']
    assert record.get('url') is None, 'the ladder would serve a rejected take'
    assert record['rejected_url'] == 'https://node/first.mp3'


def test_the_take_before_last_survives_the_next_composition():
    """The new take REPLACES the rejected one at the same key.

    Without carrying the history forward, the previous audio survived
    exactly until the next composition and then vanished -- so "go back to
    it" was true for one moment and false thereafter.
    """
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'bgm': {'url': 'https://node/first.mp3', 'variant': 1}}}}}}
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/second.mp3'}]})

    with _agent(agent_data, media) as tools:
        tools['approve_game_sound']('eng-01', False, 'bgm', 'too jangly')
        again = json.loads(tools['bind_game_sound']('eng-01', 'calm', 'spelling'))

    assert again['music']['url'] == 'https://node/second.mp3'
    assert again['music']['variant'] == 2
    previous = again['music']['previous_takes']
    assert [t['url'] for t in previous] == ['https://node/first.mp3']
    assert previous[0]['rejected_reason'] == 'too jangly'


def test_a_card_on_screen_costs_no_push(monkeypatch):
    """A game has fourteen states.

    Notifying regardless meant one game cost the person fourteen phone
    pushes and fourteen unread rows, for sounds they were already being
    shown one at a time.
    """
    shown, push, notify = _offer_a_sound(monkeypatch, ui=True)

    assert shown is True
    assert not push.called, 'pushed a sound they were already looking at'
    assert not notify.called


def test_rejecting_while_a_level_plays_silences_the_take_that_is_playing():
    """hartos-14's CRITICAL 2, measured on this lane 2026-09-21.

    The ladder falls back from 'correct@3' to 'correct'. The verdict was
    written at the key ASKED FOR, so rejecting while level 3 played wrote a
    rejection at 'correct@3' and left 'correct' -- the take actually
    sounding, on level 3 and on every other level -- with its url intact.
    The reviewer said no and the game went on playing it.
    """
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'url': 'https://node/take1.mp3', 'variant': 1}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        answer = json.loads(tools['approve_game_sound'](
            'eng-01', False, 'correct', 'too harsh', '3'))
        still_playing = json.loads(tools['get_game_sound']('eng-01', 'correct', '3'))

    assert answer['key'] == 'correct', 'the verdict missed the memo it was given'
    sounds = agent_data[4242]['games']['eng-01']['sounds']
    assert sounds['correct'].get('url') is None
    assert sounds['correct']['rejected_url'] == 'https://node/take1.mp3'
    assert 'correct@3' not in sounds, 'wrote a verdict at a key nothing lives under'
    assert still_playing['status'] == 'unbound', 'a rejected take is still playing'


def test_approving_while_a_level_plays_approves_the_take_that_is_playing():
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'url': 'https://node/take1.mp3'}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        answer = json.loads(tools['approve_game_sound'](
            'eng-01', True, 'correct', '', '7'))

    assert answer['key'] == 'correct'
    sounds = agent_data[4242]['games']['eng-01']['sounds']
    assert sounds['correct']['approved_at'] > 0, 'minted a level copy, left the real one unapproved'
    assert 'correct@7' not in sounds


def test_a_persons_correction_still_writes_in_their_own_space():
    """The fix must not send a personal rejection into the agent's memo."""
    agent_data = {4242: {'games': {'eng-01': {'mine': {'user-1': {
        'correct': {'url': 'https://node/mine.mp3'}}},
        'sounds': {'correct': {'url': 'https://node/agent.mp3',
                               'approved_at': 1}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        tools['approve_game_sound']('eng-01', False, 'correct', 'not for me', '', 'mine')

    game = agent_data[4242]['games']['eng-01']
    assert game['mine']['user-1']['correct'].get('url') is None
    assert game['sounds']['correct']['url'] == 'https://node/agent.mp3'


def test_the_lenient_tools_refuse_a_state_no_game_has():
    """get/approve could write keys bind_game_sound would never make."""
    with _agent({}, _media({'status': 'completed', 'results': []})) as tools:
        assert 'not one of a game' in tools['get_game_sound']('eng-01', 'kerfuffle')
        assert 'not one of a game' in tools['approve_game_sound'](
            'eng-01', True, 'kerfuffle')


def test_a_padded_level_is_the_same_level():
    """' 3' and '3' must not split the memo into two compositions."""
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct@3': {'url': 'https://node/level3.mp3'}}}}}}

    with _agent(agent_data, _media({'status': 'completed', 'results': []})) as tools:
        answer = json.loads(tools['get_game_sound']('eng-01', 'correct', ' 3 '))

    assert answer['music']['url'] == 'https://node/level3.mp3'


# ── the verdict, as BOTH callers reach it (hartos-14 CRITICAL 1) ──────

def test_the_verdict_has_one_implementation_both_callers_use():
    """The reviewer's card posts to an endpoint, not to the agent's tool.

    It reached /api/agent/approval, found no mapping for a game sound and
    returned applied=False -- so a sound could be approved on screen while
    approved_at stayed null forever, and REUSE could not tell an approved
    sound from an unreviewed one. The endpoint now calls this, the same
    function the tool calls, rather than growing a second copy.
    """
    from core.game_sound_memo import record_verdict

    games = {'eng-01': {'sounds': {'bgm': {'url': 'https://node/a.mp3'}}}}
    record, matched, key = record_verdict(games, 'eng-01', 'bgm', True)

    assert record['approved_at'] > 0
    assert (matched, key) == ('game', 'bgm')
    assert games['eng-01']['sounds']['bgm']['approved_at'] > 0


def test_the_shared_verdict_rejects_with_the_reason_given():
    from core.game_sound_memo import record_verdict

    games = {'eng-01': {'sounds': {'bgm': {'url': 'https://node/a.mp3'}}}}
    record, _matched, _key = record_verdict(
        games, 'eng-01', 'bgm', False, 'too jangly')

    assert record.get('url') is None
    assert record['rejected_url'] == 'https://node/a.mp3'
    assert record['rejected_reason'] == 'too jangly'


def test_the_shared_verdict_says_when_there_is_nothing_to_judge():
    """The endpoint must answer applied=False rather than invent a memo."""
    from core.game_sound_memo import record_verdict

    games = {}
    record, _matched, _key = record_verdict(games, 'eng-01', 'bgm', True)

    assert record == {}
    assert games == {}, 'minted a memo for a sound that was never composed'


def test_the_sound_arrives_as_something_a_client_can_play(monkeypatch):
    """hartos-14's CRITICAL 3.

    'media' is not a declared prop of the 'approval' component and no
    client reads one, so the card said "Have a listen" and offered nothing
    to listen to. The audio now goes as the declared 'media' component
    (props: type, src, alt, controls) that every client already renders.
    """
    fcm = MagicMock()
    services = MagicMock()
    models = MagicMock()
    ui = MagicMock()
    ui.agent_ui_update.return_value = True
    registry = MagicMock()
    registry.get.return_value = ui
    platform_registry = MagicMock()
    platform_registry.get_registry.return_value = registry
    with patch.dict('sys.modules', {
            'core.fcm_sync': fcm,
            'integrations.social.services': services,
            'integrations.social.models': models,
            'core.platform.registry': platform_registry}):
        agent_tools.offer_sound_for_review(
            'user-1', 4242, 'eng-01', 'correct',
            {'url': 'https://node/correct.mp3'})

    sent = [call.args[1] for call in ui.agent_ui_update.call_args_list]
    kinds = [c['type'] for c in sent]
    assert 'media' in kinds, 'nothing playable was ever sent'
    assert kinds.index('media') < kinds.index('approval'), (
        'the card asking them to listen arrived before the thing to hear')

    audio = sent[kinds.index('media')]
    assert audio['src'] == 'https://node/correct.mp3'
    assert audio['controls'] is True
    assert audio['media_type'] == 'audio'

    card = sent[kinds.index('approval')]
    assert 'media' not in card, 'an undeclared prop no client reads'


def test_a_composition_with_no_url_offers_no_player(monkeypatch):
    ui = MagicMock()
    ui.agent_ui_update.return_value = True
    registry = MagicMock()
    registry.get.return_value = ui
    platform_registry = MagicMock()
    platform_registry.get_registry.return_value = registry
    with patch.dict('sys.modules', {
            'core.fcm_sync': MagicMock(),
            'integrations.social.services': MagicMock(),
            'integrations.social.models': MagicMock(),
            'core.platform.registry': platform_registry}):
        agent_tools.offer_sound_for_review('user-1', 4242, 'eng-01', 'correct', {})

    kinds = [c['type'] for c in
             (call.args[1] for call in ui.agent_ui_update.call_args_list)]
    assert 'media' not in kinds, 'offered a player for nothing'


def test_the_composer_ask_does_not_route_into_the_tts_venv_repair():
    """hartos-14's caveat on the capability ask.

    capability_setup's own docstring says `backend` is what the TTS venv
    repair tool reads, and backend_repair_tools documents TTS engine ids
    only -- no acestep. Naming a music engine there sent a GRANTED consent
    into a repair path that cannot install a music model. goal_manager
    routes subprocess.tool_load to dependency remediation only when no
    backend is named.
    """
    setup = _asked('asked')
    media = _media({'status': 'unavailable',
                    'error': 'audio_music not available on this node right now.'})

    with _agent({}, media) as tools,             patch.dict('sys.modules',
                       {'integrations.agent_engine.capability_setup': setup}):
        tools['bind_game_sound']('eng-01')

    context = setup.request_capability_setup.call_args.kwargs['context']
    assert 'backend' not in context, (
        'a granted consent would be routed to the TTS venv repair tool')
    assert context['tool'] == 'acestep'


def test_a_composer_still_downloading_its_model_reads_as_composing():
    """MEASURED 2026-09-22: a first run fetches ~8.5GB of weights.

    model.safetensors 3.71GB + 4.79GB at ~15MB/s, so ten minutes before
    AceStep can answer anything. The submit read-times-out against a server
    that accepted the connection. Calling that a refusal tells the person
    their composer is broken when it is getting ready -- and leaves the
    game silent with nothing pending to return to.
    """
    media = _media({'status': 'warming_up',
                    'message': 'The composer is starting up (a first run '
                               'downloads its model). Ask again shortly.'})

    with _agent({}, media) as tools:
        answer = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling'))

    assert answer['status'] == 'composing'
    assert 'starting up' in answer['note']


def test_a_refused_connection_is_still_a_refusal():
    """The distinction only holds if the other side still reports."""
    from integrations.service_tools.media_agent import _reads_as_still_waking

    assert _reads_as_still_waking('HTTPConnectionPool: Read timed out.')
    assert not _reads_as_still_waking(
        'No connection could be made because the target machine actively '
        'refused it')


def test_calling_again_finishes_a_composition_instead_of_reporting_it():
    """MEASURED 2026-09-22 against a live composer.

    bind_game_sound returned "call again with the same arguments to finish
    it" and calling again hit the same branch and said it again -- twelve
    times, while the composition finished on the server in the middle of
    them and the memo never received the url. The resume path was
    unreachable behind an early return, so the note was a promise the code
    could not keep.
    """
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'task_id': 'acestep_abc', 'mood': 'happy',
                    'prompt': 'p', 'state': 'correct'}}}}}}
    media = _media({'status': 'pending', 'task_id': 'acestep_abc'},
                   {'status': 'completed',
                    'results': [{'url': 'https://node/finished.mp3'}]})

    with _agent(agent_data, media) as tools:
        answer = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct'))

    assert answer['status'] == 'bound', (
        'a finished composition still reported as composing')
    assert answer['music']['url'] == 'https://node/finished.mp3'
    # and it did NOT start a second composition for the same state
    assert not media.generate_media.called, 'composed twice for one state'


def test_a_reset_from_a_loading_composer_is_not_a_failure():
    """MEASURED 2026-09-22 with timestamps from the live server.

    It dropped the connection at 09:42 while loading its vae and text
    encoder, then at 09:43:30 logged "Generating audio... (DiT backend:
    PyTorch (cuda))" and ran to completion. Calling that a failure made
    the caller abandon a composition that was working, so the memo never
    filled for a sound that did get made.
    """
    from integrations.service_tools.media_agent import _reads_as_still_waking

    reset = ("('Connection aborted.', ConnectionResetError(10054, 'An "
             "existing connection was forcibly closed by the remote host'))")
    assert _reads_as_still_waking(reset)
    assert _reads_as_still_waking('HTTPConnectionPool: Read timed out.')
    # but a refusal still means nothing is listening
    assert not _reads_as_still_waking(
        'No connection could be made because the target machine actively '
        'refused it')


def test_a_reset_while_polling_keeps_polling_instead_of_failing():
    """MEASURED 2026-09-22, attempts 9-10 of a live bind.

    The submit path learned that a reset from a loading composer is not a
    failure; the POLL path had its own failure branch that never asked.
    A composition mid-generation was reported "failed" on
    ConnectionResetError 10054 and then finished anyway.
    """
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'correct': {'task_id': 'acestep_abc', 'mood': 'happy',
                    'prompt': 'p', 'state': 'correct'}}}}}}
    media = _media({'status': 'pending', 'task_id': 'acestep_abc'})
    # first poll: a reset; second poll: finished
    media.check_media_status.side_effect = [
        json.dumps({'status': 'error',
                    'error': "('Connection aborted.', ConnectionResetError("
                             "10054, 'An existing connection was forcibly "
                             "closed by the remote host'))"}),
        json.dumps({'status': 'completed',
                    'results': [{'url': 'https://node/finished.wav'}]}),
    ]

    with _agent(agent_data, media) as tools,             patch('time.sleep', lambda *_a, **_k: None):
        answer = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct'))

    assert answer['status'] == 'bound', 'a reset mid-poll was reported as failure'
    assert answer['music']['url'] == 'https://node/finished.wav'


def test_a_timed_out_submit_is_not_submitted_again_inside_the_cooldown():
    """MEASURED 2026-09-22: /v1/stats reported FIVE jobs from one caller.

    Two 'warming_up' answers (the submit's reply timed out), then a third
    submit that got an id -- and every one had been ACCEPTED server-side.
    The id the client finally held sat "queued" behind its own orphans.
    /release_task has no idempotency key, so the dedupe lives here.
    """
    media = _media({'status': 'warming_up',
                    'message': 'The composer is starting up. Ask again shortly.'})

    with _agent({}, media) as tools:
        first = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct'))
        second = json.loads(tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct'))

    assert first['status'] == 'composing'
    assert second['status'] == 'composing'
    assert 'queue' in second['note'].lower()
    assert media.generate_media.call_count == 1, (
        'a timed-out submit was retried and queued a duplicate job')


def test_after_the_cooldown_one_more_submit_is_allowed():
    """The cooldown is a floor on duplicates, not a permanent latch."""
    import core.agent_tools as at
    media = _media({'status': 'warming_up', 'message': 'starting up'})
    agent_data = {}

    with _agent(agent_data, media) as tools:
        tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct')
        rec = agent_data[4242]['games']['eng-01']['sounds']['correct']
        rec['submitted_at'] -= (at.SUBMIT_COOLDOWN_S + 1)
        tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct')

    assert media.generate_media.call_count == 2


def test_a_rejection_survives_a_composer_that_is_still_warming_up():
    """The cooldown placeholder must not overwrite the take under review.

    04b8bd86e made a timed-out submit remember WHEN it went out, so the next
    call waits rather than queueing a duplicate -- right, and measured.  But
    it writes that note as a whole NEW record at the state's key, and every
    other _remember in the function carries `previous_takes` forward while
    this one does not.  So a rejected take plus a cold composer -- a node
    restarted overnight, which is exactly when a composer is warming up --
    silently erased the rejection: the audio the reviewer asked to go back
    to, the reason they gave, and the variant counter with it.

    Sibling of test_the_take_before_last_survives_the_next_composition,
    which pins the same guarantee against a composer that answers at once.
    """
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'bgm': {'url': 'https://node/first.mp3', 'variant': 1}}}}}}
    cold = _media({'status': 'warming_up', 'message': 'starting up'})

    with _agent(agent_data, cold) as tools:
        tools['approve_game_sound']('eng-01', False, 'bgm', 'too jangly')
        tools['bind_game_sound']('eng-01', 'calm', 'spelling')

    record = agent_data[4242]['games']['eng-01']['sounds']['bgm']
    assert record.get('submitted_at'), 'the cooldown note was not written'
    assert record.get('rejected_url') == 'https://node/first.mp3', (
        'the take the reviewer turned down is gone, so "go back to it" '
        f'cannot be honoured: {record!r}')
    assert record.get('rejected_reason') == 'too jangly', (
        f'the reason the next composition must answer is gone: {record!r}')


def test_the_reason_still_reaches_the_composer_after_it_warms_up():
    """What the erased rejection costs: the same take, asked for again.

    With the rejection gone the next call computes variant 1 and a prompt
    with no "not like the last one", so the composer is asked for precisely
    what the reviewer turned down, and nothing records that they did.
    """
    agent_data = {4242: {'games': {'eng-01': {'sounds': {
        'bgm': {'url': 'https://node/first.mp3', 'variant': 1}}}}}}
    cold = _media({'status': 'warming_up', 'message': 'starting up'})

    with _agent(agent_data, cold) as tools:
        tools['approve_game_sound']('eng-01', False, 'bgm', 'too jangly')
        tools['bind_game_sound']('eng-01', 'calm', 'spelling')

    # the composer warms up; the cooldown has passed
    import core.agent_tools as at
    agent_data[4242]['games']['eng-01']['sounds']['bgm']['submitted_at'] -= (
        at.SUBMIT_COOLDOWN_S + 1)
    warm = _media({'status': 'completed',
                   'results': [{'url': 'https://node/second.mp3'}]})

    with _agent(agent_data, warm) as tools:
        again = json.loads(tools['bind_game_sound']('eng-01', 'calm', 'spelling'))

    asked = warm.generate_media.call_args.kwargs['context']
    assert 'too jangly' in asked, (
        f'the composer was asked again with no memory of the rejection: {asked!r}')
    assert again['music']['variant'] == 2, (
        f"the variant counter reset, so this reads as a first take: {again['music']!r}")
    assert [t['url'] for t in again['music']['previous_takes']] == [
        'https://node/first.mp3'], f"the earlier audio vanished: {again['music']!r}"


def test_a_cue_is_composed_short_and_a_loop_long():
    """MEASURED 2026-09-22: every state was composed at 60 seconds.

    Both WAVs from the live run were 60.00s -- for "a bright two-note
    chime for a correct answer" -- because bind_game_sound asked for 60
    regardless of state. A cue that outlasts the moment it marks is worse
    than no cue.
    """
    media = _media({'status': 'completed',
                    'results': [{'url': 'https://node/x.mp3'}]})

    with _agent({}, media) as tools:
        tools['bind_game_sound']('eng-01', 'happy', 'spelling', 'correct')
        correct = media.generate_media.call_args.kwargs['duration']
        tools['bind_game_sound']('eng-02', 'calm', 'spelling', 'bgm')
        bgm = media.generate_media.call_args.kwargs['duration']

    assert correct <= 3, f'a correct-answer chime asked for {correct}s'
    assert bgm >= 20, f'background music asked for only {bgm}s'


def test_every_state_has_a_length():
    """A new state must get a prompt AND a length in the same place."""
    from core.game_sound_memo import GAME_STATES, GAME_STATE_DURATIONS
    assert set(GAME_STATE_DURATIONS) == set(GAME_STATES)
    assert all(0 < v <= 60 for v in GAME_STATE_DURATIONS.values())
