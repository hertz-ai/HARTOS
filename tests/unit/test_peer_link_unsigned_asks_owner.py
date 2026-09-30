"""An unsigned peer is refused by ASKING the owner, not by going quiet.

Owner directive 2026-09-21, given mid-fix: "do not gulp, the consent shd be
shown when a flag gates a useful logic."  Enforcement mode is that flag and
peer linking is that useful logic.  Unset, it used to ADMIT the stranger with
no log at all; correcting the default to 'hard'
([[test_peer_link_enforcement_default]]) would REFUSE just as silently, which
is the same defect wearing the other sign.

The shape to match was already in the same function, measured live at
11:22:36 that day: a phone whose key is not yet granted emits consent.request
for device_access to two connected clients and is THEN refused ("Device HELLO
refused: pending"), so the owner's Allow admits it on the next attempt.  These
tests hold the unsigned-peer refusal to that same behaviour.

    python -m pytest tests/unit/test_peer_link_unsigned_asks_owner.py -q
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from core.peer_link import link as link_mod  # noqa: E402
from core.peer_link.link import PeerLink, TrustLevel  # noqa: E402


class _AskRecorder:
    """Stands in for the boot-installed ask; records what it was told."""

    def __init__(self, answer=False, boom=False):
        self.answer, self.boom, self.calls = answer, boom, []

    def __call__(self, peer_id, address):
        self.calls.append((peer_id, address))
        if self.boom:
            raise RuntimeError('consent path is down')
        return self.answer


@pytest.fixture(autouse=True)
def _unconfigured_and_no_leaks(monkeypatch):
    """Hard by default (the state of every unconfigured node), and the module
    -level hook is put back however the run found it."""
    monkeypatch.delenv('HEVOLVE_ENFORCEMENT_MODE', raising=False)
    before = link_mod._PEER_ADMISSION_ASK
    yield
    link_mod.set_peer_admission_ask(before)


@pytest.fixture
def link():
    lk = PeerLink('peer_abc12345', '10.0.0.1:6777', TrustLevel.PEER)
    lk._ws = MagicMock()
    return lk


@pytest.fixture
def security_patches():
    """Only the security boundary the handshake calls out to, mirroring
    test_peer_link._handshake_security_patches."""
    with patch('security.node_integrity.get_public_key_hex',
               return_value='ourpub'), \
         patch('security.node_integrity.sign_json_payload',
               return_value='oursig'), \
         patch('security.node_integrity.verify_json_signature',
               return_value=True), \
         patch('security.channel_encryption.get_x25519_public_hex',
               return_value='ourx25519'), \
         patch.object(PeerLink, '_get_local_capabilities',
                      return_value={'cpu_count': 4}):
        yield


def _unsigned_hello(**over):
    h = {'type': 'hello', 'node_id': 'peer_abc12345',
         'ed25519_public': 'aa' * 32, 'x25519_public': '',
         'trust_requested': 'peer', 'protocol_version': 1,
         'timestamp': 123.0, 'signature': ''}
    h.update(over)
    return h


def test_the_owner_is_asked_before_the_refusal(link, security_patches):
    """The point of the whole change: refusing is right, refusing SILENTLY is
    not.  The ask carries what the owner needs to tell one stranger from
    another -- which peer, at which address."""
    ask = _AskRecorder(answer=False)
    link_mod.set_peer_admission_ask(ask)
    assert link._complete_handshake(_unsigned_hello()) is False
    assert ask.calls == [('peer_abc12345', '10.0.0.1:6777')]
    link._ws.send.assert_not_called()


def test_an_allow_admits_the_peer_on_its_next_try(link, security_patches):
    """device_access's behaviour, for the peer that proved nothing: the owner
    said yes, so the next handshake completes and the ack goes out."""
    link_mod.set_peer_admission_ask(_AskRecorder(answer=True))
    assert link._complete_handshake(_unsigned_hello()) is True
    assert link._ws.send.called


def test_with_no_ask_installed_it_refuses(link, security_patches):
    """Fail closed, exactly as _DEVICE_VERIFIER does: a node whose consent
    path was never wired must not admit strangers on the grounds that nobody
    can be asked."""
    link_mod.set_peer_admission_ask(None)
    assert link._complete_handshake(_unsigned_hello()) is False


def test_a_broken_ask_refuses_rather_than_admits(link, security_patches):
    """A consent path that raises must not become an admission path."""
    ask = _AskRecorder(boom=True)
    link_mod.set_peer_admission_ask(ask)
    assert link._complete_handshake(_unsigned_hello()) is False
    assert len(ask.calls) == 1


def test_a_signed_peer_is_never_asked_about(link, security_patches):
    """The ask belongs to the unsigned branch only.  A peer that proved its
    key is admitted on that proof, and asking anyway would train the owner to
    click Allow."""
    ask = _AskRecorder(answer=False)
    link_mod.set_peer_admission_ask(ask)
    assert link._complete_handshake(_unsigned_hello(signature='peersig')) is True
    assert ask.calls == []


def test_soft_enforcement_does_not_ask(monkeypatch, link, security_patches):
    """Soft is the local-dev posture: it admits, as it always did, and puts no
    card in front of anyone."""
    monkeypatch.setenv('HEVOLVE_ENFORCEMENT_MODE', 'soft')
    ask = _AskRecorder(answer=False)
    link_mod.set_peer_admission_ask(ask)
    assert link._complete_handshake(_unsigned_hello()) is True
    assert ask.calls == []


def test_the_outgoing_handshake_asks_too(link):
    """Both directions, one rule.  An unsigned hello_ack from a peer WE
    dialled is the same absence of proof as an unsigned inbound hello."""
    ask = _AskRecorder(answer=False)
    link_mod.set_peer_admission_ask(ask)
    link._ws_send = MagicMock()
    link._ws_recv = MagicMock(return_value=json.dumps({
        'type': 'hello_ack', 'ed25519_public': 'aa' * 32,
        'x25519_public': '', 'protocol_version': 1, 'capabilities': {},
        'timestamp': 123.0}).encode('utf-8'))
    with patch('security.node_integrity.get_public_key_hex',
               return_value='ourpub'), \
         patch('security.node_integrity.sign_json_payload',
               return_value='oursig'), \
         patch('security.channel_encryption.get_x25519_public_hex',
               return_value='ourx25519'), \
         patch.object(PeerLink, '_get_local_capabilities',
                      return_value={'cpu_count': 4}):
        assert link._perform_handshake() is False
    assert ask.calls == [('peer_abc12345', '10.0.0.1:6777')]


class TestTheAskIsTheCanonicalOne:
    """It must be a real row on a registered type, or the card never renders:
    _validate_consent_type rejects an unregistered type and the whole ask is
    denied silently -- the trap documented on 'cloud_capability'."""

    def test_peer_admission_is_a_registered_consent_type(self):
        from integrations.social.consent_service import (
            CONSENT_TYPES, _validate_consent_type)
        assert 'peer_admission' in CONSENT_TYPES
        _validate_consent_type('peer_admission')  # must not raise

    def test_boot_wires_the_ask_to_check_or_request(self, monkeypatch):
        """The installed closure asks the ONE consent service, scoped to the
        peer's host, with a reason that says no identity was proved."""
        import hartos.hartos_bootstrap as boot
        monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-1')
        captured = {}

        def fake_check_or_request(db, user_id, consent_type, scope='*',
                                  agent_id=None, reason='', **kw):
            captured.update(user_id=user_id, consent_type=consent_type,
                            scope=scope, reason=reason)
            return False

        installed = {}
        mgr = MagicMock()
        mgr.set_peer_admission_ask.side_effect = (
            lambda fn: installed.update(fn=fn))
        with patch('core.peer_link.link_manager.get_link_manager',
                   return_value=mgr), \
             patch('integrations.social.consent_service.ConsentService'
                   '.check_or_request', side_effect=fake_check_or_request), \
             patch('integrations.social.models.db_session'):
            boot._install_peer_admission_ask()
            assert installed.get('fn'), 'the ask was never installed'
            assert installed['fn']('peer_abc12345',
                                   '192.168.0.43:50398') is False

        assert captured['consent_type'] == 'peer_admission'
        assert captured['scope'] == 'peer:192.168.0.43'
        assert captured['user_id'] == 'owner-1'
        assert 'no identity' in captured['reason']

    def test_a_node_with_no_owner_asks_nobody_and_admits_nobody(
            self, monkeypatch):
        """Central has no owner to ask, so it refuses; it must not fall
        through to admitting."""
        import hartos.hartos_bootstrap as boot
        monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
        installed = {}
        mgr = MagicMock()
        mgr.set_peer_admission_ask.side_effect = (
            lambda fn: installed.update(fn=fn))
        asked = MagicMock()
        with patch('core.peer_link.link_manager.get_link_manager',
                   return_value=mgr), \
             patch('integrations.social.consent_service.ConsentService'
                   '.check_or_request', asked):
            boot._install_peer_admission_ask()
            assert installed['fn']('peer_abc12345', '10.0.0.1:6777') is False
        asked.assert_not_called()

    def test_boot_installs_it_alongside_the_device_verifier(self):
        """A seam nobody calls is a seam that fails closed forever: bootstrap
        must install this one where it installs the device verifier."""
        import inspect
        import hartos.hartos_bootstrap as boot
        src = inspect.getsource(boot._run_bootstrap)
        assert '_install_device_verifier()' in src, (
            'the device verifier moved; this guard is anchored to it and can '
            'no longer prove the two installers are siblings')
        assert '_install_peer_admission_ask()' in src, (
            'boot never installs the admission ask, so every unsigned '
            'handshake refuses without the owner ever being shown a card')
