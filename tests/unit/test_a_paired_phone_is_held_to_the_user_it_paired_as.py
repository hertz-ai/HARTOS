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
    """Not admitted: the owner is asked whether the phone may switch
    (rv_2c18 follow-up 2), and until then it is its allowed account only."""
    verdict = _verdict(_as(_paired(), signed_as))
    assert verdict['status'] == 'pending', (
        f'a phone paired as {PAIRED_AS} was verified as user {signed_as!r}')
    assert verdict['reason'] == 'switch'


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
    assert body['error'] == 'consent_pending' and body['reason'] == 'switch'
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
    assert _verdict(_as(phone, OWNER))['status'] == 'pending'   # a switch, asked
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
    """The reason on the card the phone's first ask sends.  The key's
    fingerprint rides beside it (requester_fingerprint), which the card
    shows once, in its own block, never also in the sentence (rv_2c18)."""
    phone = Phone(user_id=signed_as, username=username)
    with patch.object(cs, '_emit') as emit, db_session() as db:
        auth.file_device_access_ask(db, OWNER, phone.public_hex, _claims(phone))
    (topic, ask), _ = emit.call_args
    assert topic == 'consent.request'
    assert ask['requester_fingerprint'] == device_fingerprint(phone.public_hex)
    assert device_fingerprint(phone.public_hex) not in ask['reason']
    assert phone.public_hex not in ask['reason']
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


def test_the_card_names_the_owner_who_has_no_account_row_here():
    _, reason = _card(OWNER)
    assert (f"This phone says it is signed in as this computer's owner "
            f"({OWNER}). Allow only if this is your own phone.") in reason


def test_the_card_names_an_account_on_this_desktop():
    _local_account(OTHER, 'Giri')
    phone, reason = _card(OTHER)
    assert (f'This phone says it is signed in as Giri ({OTHER}). '
            'Allow only if this is your own phone.') in reason


def test_the_card_shows_an_unknown_account_by_its_id_not_a_name():
    """The phone's own name for itself is its label, never the account's
    name: an account this desktop does not know is shown as its id."""
    phone, reason = _card('99999', username='Giri')
    assert 'This phone says it is signed in as account 99999.' in reason
    assert 'signed in as Giri' not in reason
    assert '"Giri"' in reason                     # the label, said as its claim


def test_the_card_for_a_phone_allowed_before_v60_asks_to_confirm_it():
    """rv_2c18: a phone allowed before this desktop kept its account is no
    new phone: its card says it was allowed before and the owner is asked
    to confirm it as the account it says it is signed in as."""
    phone = Phone(user_id=PAIRED_AS, username='Pixel')
    _legacy(phone, True)
    with patch.object(cs, '_emit') as emit, db_session() as db:
        auth.file_device_access_ask(db, OWNER, phone.public_hex, _claims(phone))
    (topic, ask), _ = emit.call_args
    reason = ask['reason']
    assert ask['requester_reconfirm'] is True
    assert 'requester_switch_from' not in ask
    assert ('This phone (it says its name is "Pixel") was allowed on this '
            'computer before') in reason
    assert f'signed in as account {PAIRED_AS}' in reason
    assert 'confirm' in reason
    assert "wants to use this computer's agents" not in reason
    assert device_fingerprint(phone.public_hex) not in reason


@pytest.mark.parametrize('with_ask', [True, False], ids=['its-ask', 'no-ask'])
def test_a_no_on_the_confirm_card_keeps_the_phone_on_file(desktop, social, paced,
                                                          with_ask):
    """rv_2c18 (measured on 2c18dc713: "Don't allow" on this card revoked
    every grant for the phone): the no is to the ask it names.  The grant
    from before stays on file, the phone is told the owner did not confirm
    it, and it is not asked about again for that account."""
    phone = Phone(user_id=PAIRED_AS)
    _legacy(phone, with_ask)
    with patch.object(cs, '_emit'):
        _call(desktop, phone)
    ((ask_id, paired),) = _open_asks()
    assert paired == PAIRED_AS
    r = _answer(social, '/api/social/consent/decline', phone, 'request_id', ask_id)
    assert r.status_code == 200, r.get_json()
    assert _bound() == [None]
    sent, recording = _record()
    with recording:
        refused = _call(desktop, phone)
    assert refused.status_code == 403
    assert refused.get_json()['error'] == 'consent_denied'
    assert refused.get_json()['reason'] == 'reallow_denied'
    assert sent == []


# ── one install, another account (rv_2c18 follow-up 2) ─────────────────────
#
# A phone allowed as X signs in as Y on the same install: the owner is asked
# whether it may switch.  The key alone never un-pairs X: X stays bound and
# working until the owner allows Y on that card, a no leaves X as it was,
# each account the phone tries is asked about once, and a yes binds Y.

OWNER_AUTH = {'Authorization': 'Bearer OWNER-TOKEN'}
THIRD = '20033'


@pytest.fixture
def social(monkeypatch):
    """The owner's consent API, the routes the owner's cards answer on
    (Allow: POST /consent, Don't allow: POST /consent/decline), signed in
    as OWNER the way test_device_access_listing stands it up."""
    from types import SimpleNamespace

    from flask import Flask

    from integrations.social import consent_api
    from integrations.social.models import get_db

    def resolve(token):
        if token != 'OWNER-TOKEN':
            return None, get_db()
        return SimpleNamespace(id=OWNER, is_admin=False, is_moderator=False), get_db()

    monkeypatch.setattr(auth, '_get_user_from_token', resolve)
    app = Flask('owner')
    app.config['TESTING'] = True
    app.register_blueprint(consent_api.consent_bp)
    return app.test_client()


@pytest.fixture
def paced(monkeypatch):
    """The gate's per-client ask budget never runs out here."""
    monkeypatch.setattr(discovery, 'check_client_rate', lambda *a, **k: True)


def _call(desktop, phone):
    return desktop.post('/chat', environ_base=LAN, headers=_bearer(phone.token()),
                        json={'user_id': phone.user_id, 'text': 'hi'})


def _record():
    sent = []
    return sent, patch.object(cs, '_emit', side_effect=lambda topic, data, msg_id=None:
                              sent.append((topic, dict(data), msg_id)))


def _open_asks():
    with db_session() as db:
        rows = db.query(UserConsent).filter(
            UserConsent.consent_type == 'device_access', open_ask()).all()
        return [(r.id, r.paired_user_id) for r in rows]


def _bound():
    """The accounts of the phone's grants still standing, oldest first."""
    with db_session() as db:
        rows = db.query(UserConsent).filter(
            UserConsent.consent_type == 'device_access',
            UserConsent.granted_at.isnot(None),
            UserConsent.revoked_at.is_(None),
        ).order_by(UserConsent.granted_at).all()
        return [r.paired_user_id for r in rows]


def _answer(social, path, phone, how, ask_id):
    """The owner's answer, by the card's request_id or (an older card) by
    the phone's scope."""
    body = ({'request_id': ask_id} if how == 'request_id' else
            {'consent_type': 'device_access',
             'scope': device_scope(phone.public_hex), 'agent_id': None})
    with patch.object(cs, '_emit'):
        return social.post(path, json=body, headers=OWNER_AUTH)


def test_a_switch_ask_leaves_the_bound_account_working(desktop, paced):
    """(a) Filing the ask un-pairs nothing: X stays bound and admitted, Y is
    refused with its reason, and Y is nobody the owner's asks hear from."""
    phone = _paired()
    other = _as(phone, OTHER)
    with patch.object(cs, '_emit'):
        r = _call(desktop, other)
    assert r.status_code == 403, r.get_data(as_text=True)
    assert r.get_json()['error'] == 'consent_pending'
    assert r.get_json()['reason'] == 'switch'
    assert [paired for _, paired in _open_asks()] == [OTHER]
    assert _bound() == [PAIRED_AS]
    assert _call(desktop, phone).status_code == 200
    assert _verdict(phone)['status'] == 'ok'
    assert auth.allowed_device_owner(other.token()) is None


def test_the_switch_card_is_the_same_phone_moving_accounts():
    """(b) The card names the phone it already knows (its label and key)
    moving from X to Y, and says Allow ends X on that phone: never a new
    phone's card."""
    _local_account(PAIRED_AS, 'Asha')
    phone = _paired()
    with patch.object(cs, '_emit') as emit, db_session() as db:
        auth.file_device_access_ask(db, OWNER, phone.public_hex,
                                    _claims(_as(phone, OTHER)))
    (topic, ask), _ = emit.call_args
    assert topic == 'consent.request'
    reason = ask['reason']
    assert ask['requester_switch_from'] == PAIRED_AS
    assert ask['requester_fingerprint'] == device_fingerprint(phone.public_hex)
    assert f'"{phone.username}"' in reason
    assert device_fingerprint(phone.public_hex) not in reason   # its own block
    assert f'is allowed on this computer as Asha ({PAIRED_AS})' in reason
    assert f'now says it is signed in as account {OTHER}' in reason
    assert f'Asha ({PAIRED_AS}) stops working on this phone' in reason
    assert "wants to use this computer's agents" not in reason


@pytest.mark.parametrize('how', ['request_id', 'scope'])
def test_a_no_to_the_switch_leaves_the_bound_account(desktop, social, paced, how):
    """(c) Don't allow, on the card or an older card answering by scope:
    X stays bound and working, Y is refused with its reason and is not
    asked about again."""
    phone = _paired()
    other = _as(phone, OTHER)
    with patch.object(cs, '_emit'):
        _call(desktop, other)
    ((ask_id, _),) = _open_asks()
    r = _answer(social, '/api/social/consent/decline', phone, how, ask_id)
    assert r.status_code == 200, r.get_json()
    assert _bound() == [PAIRED_AS]
    assert _call(desktop, phone).status_code == 200
    sent, recording = _record()
    with recording:
        refused = _call(desktop, other)
    assert refused.status_code == 403
    assert refused.get_json()['error'] == 'consent_denied'
    assert refused.get_json()['reason'] == 'switch_denied'
    assert sent == []
    assert _open_asks() == []


def test_one_switch_ask_per_account_and_a_repeat_sends_nothing(desktop, paced):
    """(d) Y asked three times and Z twice: one card each, never re-sent."""
    phone = _paired()
    y, z = _as(phone, OTHER), _as(phone, THIRD)
    sent, recording = _record()
    with recording:
        for caller in (y, y, y, z, z, y):
            assert _call(desktop, caller).status_code == 403
    cards = [msg_id for topic, _, msg_id in sent if topic == 'consent.request']
    assert len(cards) == 2 and len(set(cards)) == 2, cards
    assert sorted(paired for _, paired in _open_asks()) == sorted([OTHER, THIRD])
    assert _bound() == [PAIRED_AS]


@pytest.mark.parametrize('how', ['request_id', 'scope'])
def test_allow_re_pairs_the_phone_to_the_new_account(desktop, social, paced, how):
    """(e) The yes binds Y: the gate, and every reader of its verdict
    (allowed_device_owner, the payload a later check reads), now see Y,
    and X is the account that has to ask."""
    phone = _paired()
    other = _as(phone, OTHER)
    with patch.object(cs, '_emit'):
        _call(desktop, other)
    ((ask_id, _),) = _open_asks()
    r = _answer(social, '/api/social/consent', phone, how, ask_id)
    assert r.status_code == 201, r.get_json()
    verdict = _verdict(other)
    assert verdict['status'] == 'ok' and verdict['payload']['user_id'] == OTHER
    assert _verdict(phone)['status'] == 'pending'
    assert _verdict(phone)['reason'] == 'switch'
    assert _open_asks() == []
    assert _call(desktop, other).status_code == 200
    refused = _call(desktop, phone)
    assert refused.status_code == 403 and refused.get_json()['reason'] == 'switch'


def test_a_phone_back_on_its_earlier_account_is_asked_on_that_accounts_card(
        desktop, social, paced):
    """X, switched to Y, then back to X: X's own ask is sent again (one
    card for X, re-opened), and the yes binds X once more."""
    phone = _paired()
    other = _as(phone, OTHER)
    with patch.object(cs, '_emit'):
        _call(desktop, other)
    ((to_y, _),) = _open_asks()
    assert _answer(social, '/api/social/consent', phone, 'request_id',
                   to_y).status_code == 201
    sent, recording = _record()
    with recording:
        for _ in range(2):
            _call(desktop, phone)
    cards = [msg_id for topic, _, msg_id in sent if topic == 'consent.request']
    assert len(cards) == 1, cards
    ((back_to_x, paired),) = _open_asks()
    assert paired == PAIRED_AS
    assert _answer(social, '/api/social/consent', phone, 'request_id',
                   back_to_x).status_code == 201
    assert _verdict(phone)['status'] == 'ok'


def test_with_two_accounts_asked_an_answer_by_scope_binds_neither(desktop, social, paced):
    """Two open switch asks (Y and Z) and an Allow that names only the
    phone: which account it allows is not known, so it is refused and asks
    for the card's own answer; nothing is bound.  The card's request_id
    binds exactly the account it showed."""
    phone = _paired()
    with patch.object(cs, '_emit'):
        _call(desktop, _as(phone, OTHER))
        _call(desktop, _as(phone, THIRD))
    r = _answer(social, '/api/social/consent', phone, 'scope', None)
    assert r.status_code == 400, r.get_json()
    assert _bound() == [PAIRED_AS]
    (z_ask,) = [ask_id for ask_id, paired in _open_asks() if paired == THIRD]
    assert _answer(social, '/api/social/consent', phone, 'request_id',
                   z_ask).status_code == 201
    assert _verdict(_as(phone, THIRD))['status'] == 'ok'
    assert _verdict(_as(phone, OTHER))['status'] == 'pending'


# ── Block, then Allow, on the privacy page (rv_66auth, f7f073cad) ──────────
#
# The privacy page's Trusted Phones card answers by the phone's scope and
# shows no account (Nunba PrivacySettingsPage.jsx, consentApi.revoke and
# consentApi.grant).  A phone is never bound to an account the owner did not
# approve on that account's own card.

def _block_then_allow(social, phone):
    scope = device_scope(phone.public_hex)
    with patch.object(cs, '_emit'):
        blocked = social.post('/api/social/consent/revoke', headers=OWNER_AUTH,
                              json={'consent_type': 'device_access', 'scope': scope})
        allowed = social.post('/api/social/consent', headers=OWNER_AUTH,
                              json={'consent_type': 'device_access', 'scope': scope})
    assert blocked.status_code == 200, blocked.get_json()
    assert allowed.status_code == 201, allowed.get_json()


@pytest.mark.parametrize('switch_to', [OWNER, OTHER])
def test_block_then_allow_never_binds_the_account_a_switch_ask_waits_for(
        desktop, social, paced, switch_to):
    """A family phone allowed as its own account is signed in as the owner
    once (the switch card for the owner waits, unanswered); the owner taps
    Block, then Allow.  Measured on f7f073cad: the phone was bound as the
    OWNER and answered the owner's asks.  The Allow is for the phone as it
    was allowed: the switch is still only the switch card's to allow."""
    phone = _paired()
    switched = _as(phone, switch_to)
    with patch.object(cs, '_emit'):
        _call(desktop, switched)
    _block_then_allow(social, phone)
    assert _bound() == [PAIRED_AS]
    assert _verdict(phone)['status'] == 'ok'
    verdict = _verdict(switched)
    assert (verdict['status'], verdict.get('reason')) == ('pending', 'switch')
    assert auth.allowed_device_owner(switched.token()) is None
    assert _call(desktop, phone).status_code == 200
    assert _call(desktop, switched).status_code == 403


def test_block_then_allow_restores_the_account_it_was_allowed_as(
        desktop, social, paced):
    phone = _paired()
    _block_then_allow(social, phone)
    assert _bound() == [PAIRED_AS]
    assert _call(desktop, phone).status_code == 200


# ── a no declines only its own ask, in every state (rv_66auth) ─────────────

@pytest.mark.parametrize('said_no_to', ['first', 'second'])
def test_a_no_to_one_account_of_a_phone_not_yet_allowed_leaves_the_other(
        desktop, social, paced, said_no_to):
    """Never allowed, the phone asked as Y, then as Z.  Don't allow on one
    of the two cards: that account is refused, the other card stays open
    and still allows its account."""
    first = Phone(user_id=OTHER, username='Tab')
    second = _as(first, THIRD)
    with patch.object(cs, '_emit'):
        _call(desktop, first), _call(desktop, second)
    asks = {paired: ask_id for ask_id, paired in _open_asks()}
    no, yes = (first, second) if said_no_to == 'first' else (second, first)
    r = _answer(social, '/api/social/consent/decline', no, 'request_id',
                asks[no.user_id])
    assert r.status_code == 200, r.get_json()
    assert _open_asks() == [(asks[yes.user_id], yes.user_id)]
    assert _verdict(no)['status'] == 'denied'
    assert _verdict(yes)['status'] == 'pending'
    with patch.object(cs, '_emit'):
        assert _call(desktop, yes).get_json()['error'] == 'consent_pending'
        assert _call(desktop, no).get_json()['error'] == 'consent_denied'
    assert _answer(social, '/api/social/consent', yes, 'request_id',
                   asks[yes.user_id]).status_code == 201
    assert _bound() == [yes.user_id]
    assert _verdict(yes)['status'] == 'ok'
    assert _verdict(no)['status'] == 'denied'


def test_a_no_to_one_switch_ask_leaves_the_other_waiting(desktop, social, paced):
    phone = _paired()
    y, z = _as(phone, OTHER), _as(phone, THIRD)
    with patch.object(cs, '_emit'):
        _call(desktop, y), _call(desktop, z)
    asks = {paired: ask_id for ask_id, paired in _open_asks()}
    r = _answer(social, '/api/social/consent/decline', phone, 'request_id', asks[OTHER])
    assert r.status_code == 200, r.get_json()
    assert _open_asks() == [(asks[THIRD], THIRD)]
    assert (_verdict(y)['status'], _verdict(y)['reason']) == ('denied', 'switch_denied')
    assert (_verdict(z)['status'], _verdict(z)['reason']) == ('pending', 'switch')
    assert _bound() == [PAIRED_AS]


def test_dont_allow_by_scope_on_a_phone_not_yet_allowed_refuses_every_account(
        desktop, social, paced):
    """The privacy page's Don't allow names only the phone: every account
    it asked as is refused, and no card comes back."""
    y = Phone(user_id=OTHER, username='Tab')
    z = _as(y, THIRD)
    with patch.object(cs, '_emit'):
        _call(desktop, y), _call(desktop, z)
    r = _answer(social, '/api/social/consent/decline', y, 'scope', None)
    assert r.status_code == 200, r.get_json()
    assert _open_asks() == []
    sent, recording = _record()
    with recording:
        for caller in (y, z):
            assert _call(desktop, caller).get_json()['error'] == 'consent_denied'
    assert sent == []
