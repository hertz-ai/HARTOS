"""A phone allowed on a desktop acts as the account it was allowed as, and
as no other (#306).

Owner, 2026-11-23: "credits are for entire family but if there is another
account allowed then treat as the user trusted users for same machine to
leverage the compute of local first".  A phone the owner allowed is a
trusted user of this desktop's compute AS ITS OWN ACCOUNT: the one its token
named when it asked.  The phone writes that user_id itself, and the gate
(auth.verify_device_jwt) checked the key and never the account, so a phone
allowed as 40021 was admitted as 10077, as nobody, or as this desktop's
owner, the answerer of the owner's own asks (auth.allowed_device_owner,
person_ask.answer_from_request).

The ask now keeps the account (user_consents.paired_user_id, v60), the
grant copies it the way it copies the phone's label, and the gate admits
that account only.  A grant from before v60 names no account: the phone is
asked about again, on one card, and never bound on first use.

Real verify_device_jwt, file_device_access_ask, allowed_device_owner, the
gate and the consent store; the phone and the app are
test_device_access_gate's.
"""
from unittest.mock import patch

import jwt as pyjwt
import pytest

from integrations.social import auth
from integrations.social import consent_service as cs
from integrations.social import discovery
from integrations.social.consent_service import (
    ConsentService, device_fingerprint, device_scope, open_ask)
from integrations.social.models import User, UserConsent, db_session
from tests.unit.test_device_access_gate import (  # noqa: F401
    LAN, OWNER, Phone, _bearer, _desktop_env, desktop, pair)

PAIRED_AS, OTHER = '40021', '10077'


def _paired(user_id=PAIRED_AS):
    """A phone that asked while signed in as ``user_id``, and was allowed."""
    phone = Phone(user_id=user_id)
    pair(phone)
    return phone


def _as(phone, user_id):
    """The same phone, same key, minting for another account."""
    other = Phone(user_id=user_id, username=phone.username)
    other.key, other.public_hex = phone.key, phone.public_hex
    return other


def _verdict(phone):
    with db_session() as db:
        return auth.verify_device_jwt(db, phone.token(), OWNER)


def _claims(phone):
    return pyjwt.decode(phone.token(), options={'verify_signature': False})


def _device_rows():
    with db_session() as db:
        rows = db.query(UserConsent).filter_by(consent_type='device_access').all()
        return [(r.id, r.granted_at is not None, r.paired_user_id) for r in rows]


# ── the account the phone asked as ─────────────────────────────────────────

def test_the_ask_and_the_grant_keep_the_account_the_phone_asked_as():
    _paired()
    rows = _device_rows()
    assert sorted(granted for _, granted, _ in rows) == [False, True]  # ask, grant
    assert {paired for _, _, paired in rows} == {PAIRED_AS}


def test_the_paired_account_is_admitted():
    verdict = _verdict(_paired())
    assert verdict['status'] == 'ok'
    assert verdict['payload']['user_id'] == PAIRED_AS


@pytest.mark.parametrize('signed_as', [OTHER, OWNER])
def test_a_paired_phone_cannot_mint_for_another_account(signed_as):
    verdict = _verdict(_as(_paired(), signed_as))
    assert verdict['status'] == 'invalid', (
        f'a phone paired as {PAIRED_AS} was verified as user {signed_as!r}')


@pytest.mark.parametrize('signed_as', ['', None])
def test_a_paired_phone_naming_no_account_is_told_to_update(signed_as):
    verdict = _verdict(_as(_paired(), signed_as))
    assert verdict['status'] == 'update_app', verdict


def test_a_paired_phone_cannot_sign_itself_in_as_the_owner():
    phone = _paired()
    assert auth.allowed_device_owner(_as(phone, OWNER).token()) is None


def test_a_phone_paired_as_the_owner_still_answers_for_the_owner():
    assert auth.allowed_device_owner(_paired(OWNER).token()) == OWNER


def test_the_gate_admits_the_paired_account_and_no_other(desktop):
    phone = _paired()
    mine = desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()),
                        json={'user_id': PAIRED_AS, 'text': 'hi'})
    assert mine.status_code == 200
    assert mine.get_json()['auth'] == 'device'
    forged = desktop.post('/chat', environ_base=LAN,
                          headers=_bearer(_as(phone, OTHER).token()),
                          json={'user_id': OTHER, 'text': 'hi'})
    assert forged.status_code == 403, forged.get_json()
    body = forged.get_json()
    assert body['error'] == 'not_paired_account'
    assert 'another account' in body['message']


# ── an ask that names no account ───────────────────────────────────────────

@pytest.mark.parametrize('no_user', [None, ''])
def test_an_ask_naming_no_account_is_refused_with_what_to_do(desktop, no_user):
    nobody = Phone(user_id=no_user)
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(nobody.token()))
    assert r.status_code == 403, r.get_data(as_text=True)
    assert r.get_json()['error'] == 'update_app'
    assert 'Update the app to connect to this desktop' in r.get_json()['message']
    emit.assert_not_called()
    assert _device_rows() == []


def test_the_ask_path_itself_files_nothing_without_an_account():
    nobody = Phone(user_id=None)
    with patch.object(cs, '_emit') as emit, db_session() as db:
        auth.file_device_access_ask(db, OWNER, nobody.public_hex, _claims(nobody))
    emit.assert_not_called()
    assert _device_rows() == []


# ── every other kind of grant ──────────────────────────────────────────────

@pytest.mark.parametrize('consent_type,scope', [
    # Not camera or screen: their grant writes the desktop's embodied-feed
    # config to disk (_embodied_feed_from_consent).
    ('computer_control', '*'), ('credential', 'secret:MAILBOX'),
    ('data_access', '*'), ('cloud_egress', 'vision')])
def test_a_grant_of_any_other_kind_names_no_paired_account(consent_type, scope):
    with db_session() as db:
        ConsentService.request_consent(db, OWNER, consent_type, scope=scope,
                                       reason='an agent asks')
        ConsentService.grant_consent(db, OWNER, consent_type, scope=scope)
        rows = db.query(UserConsent).filter_by(consent_type=consent_type).all()
        assert len(rows) == 2
        assert [r.paired_user_id for r in rows] == [None, None]


# ── a grant from before the desktop kept the account ───────────────────────

def _legacy(phone, with_ask):
    """The rows a desktop wrote for ``phone`` before v60: its ask, filed the
    old way with no account (or none at all, a grant written straight from
    the privacy page), and the owner's grant, neither naming an account."""
    scope = device_scope(phone.public_hex)
    with db_session() as db:
        if with_ask:
            ConsentService.request_consent(
                db, OWNER, 'device_access', scope=scope,
                reason="A phone wants to use this computer's agents.",
                requester_name=phone.username)
        ConsentService.grant_consent(db, OWNER, 'device_access', scope=scope)
    assert {paired for _, _, paired in _device_rows()} == {None}


@pytest.mark.parametrize('with_ask', [True, False], ids=['its-ask', 'no-ask'])
def test_a_grant_from_before_v60_is_asked_again_on_one_card(desktop, monkeypatch,
                                                            with_ask):
    phone = Phone(user_id=PAIRED_AS)
    _legacy(phone, with_ask)
    monkeypatch.setattr(discovery, 'check_client_rate', lambda *a, **k: True)
    sent = []
    with patch.object(cs, '_emit', side_effect=lambda topic, data, msg_id=None:
                      sent.append((topic, dict(data), msg_id))):
        replies = [desktop.post('/chat', environ_base=LAN,
                                headers=_bearer(phone.token()),
                                json={'user_id': PAIRED_AS, 'text': 'hi'})
                   for _ in range(3)]
    for r in replies:
        assert r.status_code == 403, r.get_data(as_text=True)
        assert r.get_json()['error'] == 'consent_pending'
        assert r.get_json()['reason'] == 'reallow'
        assert 'allow this phone again' in r.get_json()['message']
    cards = [msg_id for topic, _, msg_id in sent if topic == 'consent.request']
    assert len(cards) == 3 and len(set(cards)) == 1, cards   # one card, re-sent
    with db_session() as db:
        waiting = db.query(UserConsent).filter(
            UserConsent.consent_type == 'device_access', open_ask()).all()
        assert len(waiting) == 1
        (ask,) = waiting
        assert ask.paired_user_id == PAIRED_AS
        assert cards[0] == cs.ask_msg_id(ask.id)    # the card answers that ask
        # never bound on first use: the old grant still names nobody
        grants = db.query(UserConsent).filter(
            UserConsent.consent_type == 'device_access',
            UserConsent.granted_at.isnot(None)).all()
        assert [g.paired_user_id for g in grants] == [None]
    assert _verdict(phone)['status'] == 'pending'

    # The owner allows it on that card: the phone is its account, only.
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'device_access',
                                     scope=device_scope(phone.public_hex))
    assert _verdict(phone)['status'] == 'ok'
    assert _verdict(_as(phone, OWNER))['status'] == 'invalid'
    with db_session() as db:
        assert db.query(UserConsent).filter(
            UserConsent.consent_type == 'device_access', open_ask()).count() == 0


def test_a_phone_with_no_account_on_a_grant_from_before_v60_is_told_to_update(desktop):
    nobody = Phone(user_id=None)
    _legacy(nobody, True)
    with patch.object(cs, '_emit') as emit:
        r = desktop.post('/chat', environ_base=LAN, headers=_bearer(nobody.token()))
    assert r.status_code == 403
    assert r.get_json()['error'] == 'update_app'
    emit.assert_not_called()


# ── the owner's card ───────────────────────────────────────────────────────

def _card(signed_as, username='Pixel'):
    """The reason on the card the phone's first ask sends."""
    phone = Phone(user_id=signed_as, username=username)
    with patch.object(cs, '_emit') as emit, db_session() as db:
        auth.file_device_access_ask(db, OWNER, phone.public_hex, _claims(phone))
    (topic, ask), _ = emit.call_args
    assert topic == 'consent.request'
    return phone, ask['reason']


def _local_account(user_id, name):
    with db_session() as db:
        db.add(User(id=user_id, username=f'u{user_id}', display_name=name,
                    user_type='human'))


def test_the_card_says_plainly_when_the_phone_claims_the_owners_account():
    _local_account(OWNER, 'Sathish')
    phone, reason = _card(OWNER)
    assert (f'This phone says it is signed in as Sathish ({OWNER}). '
            'Allow only if this is your own phone.') in reason
    assert '"Pixel"' in reason
    assert device_fingerprint(phone.public_hex) in reason
    assert phone.public_hex not in reason


def test_the_card_names_the_owner_who_has_no_account_row_here():
    _, reason = _card(OWNER)
    assert (f"This phone says it is signed in as this computer's owner "
            f"({OWNER}). Allow only if this is your own phone.") in reason


def test_the_card_names_an_account_on_this_desktop():
    _local_account(OTHER, 'Giri')
    phone, reason = _card(OTHER)
    assert (f'This phone says it is signed in as Giri ({OTHER}). '
            'Allow only if this is your own phone.') in reason
    assert device_fingerprint(phone.public_hex) in reason


def test_the_card_shows_an_unknown_account_by_its_id_not_a_name():
    """The phone's own name for itself is its label, never the account's
    name: an account this desktop does not know is shown as its id."""
    phone, reason = _card('99999', username='Giri')
    assert 'This phone says it is signed in as account 99999.' in reason
    assert 'signed in as Giri' not in reason
    assert '"Giri"' in reason                     # the label, said as its claim
    assert device_fingerprint(phone.public_hex) in reason
