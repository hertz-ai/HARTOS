"""A node with no FCM credential hands its push to central's relay.

A consumer install has no Firebase service account (and must not).  Central's
confirmation.py holds one and already pushes unacknowledged pending messages;
hand_push_to_central publishes the push as such a pending message, carrying its
own title and data so the phone gets what was meant to be sent.
"""
from unittest.mock import MagicMock, patch

from core import fcm_sync


def _bus():
    bus = MagicMock()
    return bus


def _run(user_id='9003054371', data=None, central_id=None):
    bus = _bus()
    with patch('core.peer_link.message_bus.get_message_bus', return_value=bus), \
            patch.object(fcm_sync, 'resolve_central_id', return_value=central_id):
        ok = fcm_sync.hand_push_to_central(user_id, 'A new game sound', 'ready', data)
    return ok, bus


def test_publishes_a_pending_confirmation_central_understands():
    ok, bus = _run(data={'type': 'game_sound_review', 'agent_id': 'a1', 'url': 'u'})
    assert ok is True
    topic, msg = bus.publish.call_args.args[0], bus.publish.call_args.args[1]
    assert topic == 'task.confirmation'
    assert msg['confirmation'] is False            # pending, not an ack
    assert msg['topic_name'] == 'com.hertzai.pupit.9003054371'
    assert msg['bot_type'] == 'Hevolve'             # central sends via sendPush
    assert msg['text'] == ['ready']                 # central reads text[0]
    assert msg['push_title'] == 'A new game sound'
    assert msg['request_id'].startswith('push-')


def test_carries_the_pushs_own_data_as_strings_and_the_privacy_notice():
    _, bus = _run(data={'type': 'game_sound_review', 'agent_id': 7, 'url': 'u'})
    push_data = bus.publish.call_args.args[1]['push_data']
    assert push_data['type'] == 'game_sound_review'
    assert push_data['agent_id'] == '7'             # FCM data values are strings
    assert push_data['privacy_tier_skipped'] == 'true'
    assert push_data['privacy_notice']


def test_stays_off_the_local_ui_and_peer_links():
    _, bus = _run()
    kw = bus.publish.call_args.kwargs
    assert kw['skip_sse'] is True and kw['skip_peerlink'] is True


def test_uses_the_mapped_central_id_for_a_local_uuid():
    _, bus = _run(user_id='local-uuid', central_id='9003054371')
    assert bus.publish.call_args.args[1]['topic_name'] == 'com.hertzai.pupit.9003054371'


def test_a_non_numeric_id_is_not_handed_off():
    # central does int() on the topic suffix; a non-numeric one would raise in
    # its loop, so it must never be sent.
    ok, bus = _run(user_id='local-uuid', central_id=None)
    assert ok is False
    bus.publish.assert_not_called()


def test_a_bus_failure_never_raises():
    with patch('core.peer_link.message_bus.get_message_bus', side_effect=RuntimeError('x')), \
            patch.object(fcm_sync, 'resolve_central_id', return_value=None):
        assert fcm_sync.hand_push_to_central('9003054371', 't', 'b') is False


def test_send_fcm_push_hands_off_when_it_has_no_credential():
    with patch.object(fcm_sync, '_fcm_credential', return_value=(None, None)), \
            patch.object(fcm_sync, 'hand_push_to_central', return_value=True) as h:
        assert fcm_sync.send_fcm_push('9003054371', 't', 'b', data={'k': 'v'}) is True
    h.assert_called_once_with('9003054371', 't', 'b', {'k': 'v'})


def test_send_fcm_push_does_not_hand_off_when_relay_is_off():
    with patch.object(fcm_sync, '_fcm_credential', return_value=(None, None)), \
            patch.object(fcm_sync, 'hand_push_to_central') as h:
        assert fcm_sync.send_fcm_push('9003054371', 't', 'b', relay=False) is False
    h.assert_not_called()


def test_send_fcm_push_with_a_credential_still_sends_directly():
    with patch.object(fcm_sync, '_fcm_credential', return_value=('tok', 'proj')), \
            patch.object(fcm_sync, 'get_local_fcm_token', return_value='dev'), \
            patch.object(fcm_sync, '_post_fcm_message', return_value=True) as post, \
            patch.object(fcm_sync, 'hand_push_to_central') as h:
        assert fcm_sync.send_fcm_push('9003054371', 't', 'b') is True
    post.assert_called_once()
    h.assert_not_called()                           # no double push
