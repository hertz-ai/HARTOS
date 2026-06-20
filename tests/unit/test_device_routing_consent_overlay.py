"""request_consent must fire the Truecaller-style consent overlay via an FCM
data.type=='consent_prompt' — not only a FleetCommand the RN app sees solely
when it is open.  The native ConsentOverlayService starts ONLY on that FCM, so
this is what makes AGENT consent reach the user on mobile while they are away
from the agent's device (the agent runs on the laptop; the user acts on the
phone).  Regression guard for #169.

Behavioural: real DeviceRoutingService.request_consent; the boundary (db,
NotificationService, FleetCommandService, send_fcm_push) is mocked; we assert
the actual consent_prompt FCM payload + request_id correlation.

    python -m pytest tests/unit/test_device_routing_consent_overlay.py --noconftest -q
"""
from unittest.mock import MagicMock, patch

import integrations.social.device_routing_service as drs
from integrations.social.device_routing_service import DeviceRoutingService


def _db_with_devices(devices):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = devices
    return db


def test_request_consent_fires_consent_prompt_fcm_when_no_device_bound():
    db = _db_with_devices([])  # no DeviceBinding rows — FCM is the only reach
    with patch.object(drs, 'NotificationService') as MockNS, \
            patch('core.fcm_sync.send_fcm_push') as mock_send:
        out = DeviceRoutingService.request_consent(
            db, 'u1', 'public_exposure', 'agent1',
            description='post on your behalf to grow reach',
        )
    assert mock_send.called, 'agent consent MUST push a consent_prompt FCM'
    args, kwargs = mock_send.call_args
    assert args[0] == 'u1'                       # routed to the user
    data = kwargs['data']
    assert data['type'] == 'consent_prompt'      # the overlay's trigger
    assert data['topic_reply'] == 'com.hertzai.pupit.u1'  # reply channel
    assert data['action'] == 'public_exposure'
    assert data['request_id']
    assert out['request_id'] == data['request_id']        # caller can correlate
    assert out['method'] == 'fcm_consent_prompt'
    assert MockNS.create.called                  # persistent notice still made


def test_request_consent_threads_same_request_id_into_fleet_command():
    phone = MagicMock(); phone.form_factor = 'phone'; phone.device_id = 'dev-phone'
    db = _db_with_devices([phone])
    with patch.object(drs, 'NotificationService'), \
            patch.object(drs, 'FleetCommandService') as MockFC, \
            patch('core.fcm_sync.send_fcm_push') as mock_send:
        MockFC.push_command.return_value = {'id': 'cmd-1'}
        out = DeviceRoutingService.request_consent(
            db, 'u2', 'public_exposure', 'agent1', description='x',
        )
    fcm_data = mock_send.call_args.kwargs['data']
    fc_params = MockFC.push_command.call_args.args[3]
    assert fc_params['request_id'] == fcm_data['request_id']  # one id, both paths
    assert out['device_id'] == 'dev-phone'
    assert out['method'] == 'fleet_command'


def test_fcm_push_failure_never_breaks_the_consent_record():
    db = _db_with_devices([])
    with patch.object(drs, 'NotificationService') as MockNS, \
            patch('core.fcm_sync.send_fcm_push',
                  side_effect=RuntimeError('no edge FCM credential')):
        out = DeviceRoutingService.request_consent(
            db, 'u3', 'public_exposure', 'agent1',
        )
    assert MockNS.create.called          # notification survived the push failure
    assert out['success'] is True
    assert out['request_id']
