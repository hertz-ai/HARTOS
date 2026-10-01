"""A node with no FCM credential hands its push to central's relay.

A consumer install has no Firebase service account (and must not).  Central's
confirmation.py holds one and already pushes unacknowledged pending messages;
hand_push_to_central publishes the push as such a pending message, carrying its
own title and data so the phone gets what was meant to be sent.  Two callers
opt in (``relay=True``): the game-sound offer and the consent prompt.
"""
from unittest.mock import MagicMock, patch

from core import fcm_sync


def _bus(accepted=True):
    bus = MagicMock()
    # the bus counts what a crossbar transport actually accepted
    bus.get_stats.side_effect = [{'delivered_crossbar': 0},
                                 {'delivered_crossbar': 1 if accepted else 0}]
    return bus


def _run(user_id='9003054371', data=None, central_id=None, accepted=True):
    bus = _bus(accepted)
    with patch('core.peer_link.message_bus.get_message_bus', return_value=bus), \
            patch.object(fcm_sync, 'resolve_central_id', return_value=central_id):
        ok = fcm_sync.hand_push_to_central(user_id, 'A new game sound', 'ready', data)
    return ok, bus


def _msg(bus):
    return bus.publish.call_args.args[1]


# ── message shape ────────────────────────────────────────────────────

def test_publishes_a_pending_confirmation_central_understands():
    ok, bus = _run(data={'type': 'game_sound_review', 'agent_id': 'a1', 'url': 'u'})
    assert ok is True
    assert bus.publish.call_args.args[0] == 'task.confirmation'
    msg = _msg(bus)
    assert msg['confirmation'] is False            # pending, not an ack
    assert msg['topic_name'] == 'com.hertzai.pupit.9003054371'
    assert msg['bot_type'] == 'Hevolve'            # central sends via sendPush
    assert msg['text'] == ['ready']                # central reads text[0]
    assert msg['push_title'] == 'A new game sound'
    assert msg['request_id'].startswith('push-')


def test_carries_the_pushs_own_data_as_strings_and_the_privacy_notice():
    _, bus = _run(data={'type': 'game_sound_review', 'agent_id': 7, 'url': 'u'})
    push_data = _msg(bus)['push_data']
    assert push_data['type'] == 'game_sound_review'
    assert push_data['agent_id'] == '7'            # FCM data values are strings
    assert push_data['privacy_tier_skipped'] == 'true'
    assert push_data['privacy_notice']


def test_a_game_sound_offer_does_not_send_a_reply_topic_either():
    _, bus = _run(data={'type': 'game_sound_review', 'agent_id': 'a1',
                        'topic_reply': 'com.hertzai.pupit.9003054371'})
    assert 'topic_reply' not in _msg(bus)['push_data']
    assert _msg(bus)['push_data']['type'] == 'game_sound_review'


def test_stays_off_the_local_ui_and_peer_links():
    _, bus = _run()
    kw = bus.publish.call_args.kwargs
    assert kw['skip_sse'] is True and kw['skip_peerlink'] is True


def test_uses_the_mapped_central_id_for_a_local_uuid():
    _, bus = _run(user_id='local-uuid', central_id='9003054371')
    assert _msg(bus)['topic_name'] == 'com.hertzai.pupit.9003054371'


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


def test_the_return_says_whether_a_transport_accepted_it():
    assert _run(accepted=True)[0] is True
    # e.g. no WAMP/HTTP bridge configured: nothing left the node
    assert _run(accepted=False)[0] is False


def test_the_relayed_message_survives_the_egress_scrub_with_its_routing_keys():
    # The confirmation URI is shared, so the bus sends a scrubbed copy; central
    # does int() on topic_name's suffix and keys on user_id, so those must
    # survive the scrub.
    from security.edge_privacy import scrub_for_egress
    _, bus = _run(user_id='9003054371', data={'type': 'game_sound_review'})
    scrubbed = scrub_for_egress(_msg(bus))
    assert scrubbed['topic_name'] == 'com.hertzai.pupit.9003054371'
    assert scrubbed['user_id'] == '9003054371'
    assert scrubbed['confirmation'] is False


# ── a consent prompt: central's existing consent path ────────────────

CONSENT = {'type': 'consent_prompt', 'request_id': 'req-abc', 'user_id': '9003054371',
           'topic_reply': 'com.hertzai.pupit.9003054371', 'action': 'public_exposure',
           'agent_id': 'a1'}


def test_a_consent_prompt_is_shaped_for_centrals_consent_path():
    ok, bus = _run(data=CONSENT)
    msg = _msg(bus)
    assert ok is True
    assert msg['bot_type'] == 'consent_prompt'     # what central tags the push by
    assert msg['action'] == 'consent_prompt'
    assert msg['request_id'] == 'req-abc'          # the phone's answer maps back by this id
    # type is central's to decide; a relayed push_data type is dropped there
    assert 'type' not in msg['push_data']
    # central derives or drops these, and the egress scrub rewrites a 10-digit id
    # inside them (topic_reply became 'pupit.[PHONE_REDACTED]')
    for key in ('topic_reply', 'user_id', 'request_id', 'action'):
        assert key not in msg['push_data']


def test_a_consent_prompt_without_an_id_still_gets_a_unique_one():
    data = {k: v for k, v in CONSENT.items() if k != 'request_id'}
    msg = _msg(_run(data=data)[1])
    assert msg['request_id'].startswith('push-')


# ── send_fcm_push: relay is opt-in ───────────────────────────────────

def test_send_fcm_push_hands_off_when_it_has_no_credential_and_relay_is_on():
    with patch.object(fcm_sync, '_fcm_credential', return_value=(None, None)), \
            patch.object(fcm_sync, 'hand_push_to_central', return_value=True) as h:
        assert fcm_sync.send_fcm_push('9003054371', 't', 'b', data={'k': 'v'},
                                      relay=True) is True
    h.assert_called_once_with('9003054371', 't', 'b', {'k': 'v'})


def test_relay_is_opt_in_so_nothing_else_starts_relaying_by_accident():
    with patch.object(fcm_sync, '_fcm_credential', return_value=(None, None)), \
            patch.object(fcm_sync, 'hand_push_to_central') as h:
        assert fcm_sync.send_fcm_push('9003054371', 't', 'b') is False
    h.assert_not_called()


def test_send_fcm_push_with_a_credential_still_sends_directly():
    with patch.object(fcm_sync, '_fcm_credential', return_value=('tok', 'proj')), \
            patch.object(fcm_sync, 'get_local_fcm_token', return_value='dev'), \
            patch.object(fcm_sync, '_post_fcm_message', return_value=True) as post, \
            patch.object(fcm_sync, 'hand_push_to_central') as h:
        assert fcm_sync.send_fcm_push('9003054371', 't', 'b', relay=True) is True
    post.assert_called_once()
    h.assert_not_called()                          # no double push


def test_a_consent_prompt_that_could_not_be_pushed_is_logged_loudly(caplog):
    import logging
    with patch.object(fcm_sync, '_fcm_credential', return_value=(None, None)), \
            patch.object(fcm_sync, 'hand_push_to_central', return_value=False), \
            caplog.at_level(logging.WARNING, logger='hevolve.fcm_sync'):
        assert fcm_sync.send_fcm_push('9003054371', 't', 'b', data=CONSENT,
                                      relay=True) is False
    assert 'consent prompt' in caplog.text and 'was not queued' in caplog.text
