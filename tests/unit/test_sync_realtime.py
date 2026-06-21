"""P4 real-time: queue_entity surfaces sync state to the OWNER's clients via the
EXISTING on_notification fan-out (chat.social WAMP + SSE that RN/web/desktop
already consume) — NO new transport, no parallel fan-out path. Opt-in per entity
(owner extractor); high-frequency/internal entities (resonance, encounter,
membership) stay silent. Behavioural; boundary mocked.

    python -m pytest tests/unit/test_sync_realtime.py --noconftest -q
"""
import types
from unittest.mock import patch

import integrations.social.sync_engine as se


def _gossip(monkeypatch):
    monkeypatch.setattr('integrations.social.peer_discovery.gossip',
                        types.SimpleNamespace(node_id='n', base_url='u', node_name='N'))


# ── queue_entity emits for owner-bearing entities ──
def test_friend_sync_emits_status_to_initiator(monkeypatch):
    _gossip(monkeypatch)
    fr = {'id': 'f1', 'user_a_id': 'ua', 'user_b_id': 'ub',
          'status': 'pending', 'initiator_id': 'ua'}
    with patch.object(se.SyncEngine, 'queue', return_value='q1'), \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=True), \
            patch('integrations.social.realtime.on_notification') as notif:
        out = se.SyncEngine.queue_entity(None, fr)
    assert out == 'q1'
    notif.assert_called_once()
    owner, payload = notif.call_args.args
    assert owner == 'ua'
    assert payload['type'] == 'sync_status'
    assert payload['entity'] == 'sync_friendship'
    assert payload['sync_status'] == 'synced'


# ── high-frequency entity stays SILENT (no owner extractor) ──
def test_resonance_sync_does_not_emit(monkeypatch):
    _gossip(monkeypatch)

    class _W:
        __tablename__ = 'resonance_wallets'
        user_id = 'u1'
        updated_at = None

        def to_dict(self):
            return {'user_id': 'u1', 'spark': 5}

    with patch.object(se.SyncEngine, 'queue', return_value='q1'), \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=True), \
            patch('integrations.social.realtime.on_notification') as notif:
        se.SyncEngine.queue_entity(None, _W())
    notif.assert_not_called()


# ── no emit when the central queue didn't accept ──
def test_emit_skipped_when_queue_returns_none(monkeypatch):
    _gossip(monkeypatch)
    fr = {'id': 'f1', 'user_a_id': 'ua', 'user_b_id': 'ub',
          'status': 'pending', 'initiator_id': 'ua'}
    with patch.object(se.SyncEngine, 'queue', return_value=None), \
            patch('integrations.social.consent_service.ConsentService.check_consent',
                  return_value=True), \
            patch('integrations.social.realtime.on_notification') as notif:
        se.SyncEngine.queue_entity(None, fr)
    notif.assert_not_called()


# ── _emit_sync_status reuses on_notification ──
def test_emit_sync_status_reuses_on_notification():
    with patch('integrations.social.realtime.on_notification') as notif:
        se.SyncEngine._emit_sync_status('ua', 'sync_post')
    notif.assert_called_once_with(
        'ua', {'type': 'sync_status', 'entity': 'sync_post', 'sync_status': 'synced'})


def test_emit_sync_status_no_owner_is_noop():
    with patch('integrations.social.realtime.on_notification') as notif:
        se.SyncEngine._emit_sync_status(None, 'sync_post')
    notif.assert_not_called()
