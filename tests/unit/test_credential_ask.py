"""A credential the agent needs is asked for on the consent card, and the
agent only ever gets its alias back.

Owner requirement (2026-09-25): "when need to ask a credential ... our consent
overlay shd do that ... and the llm shd respond back with the pseudo alias
used so that actual data is encrypted and used only deterministically", and
"the consent card shd have way to enter the pass and then click accept".

Before this, Request_Resource (both copies: the LangChain tool in
hart_intelligence_entry and request_resource in core.agent_tools) returned a
RESOURCE_REQUEST:{json} marker that only the Demopage chat page turned into a
modal.  No consent row, nothing on the floating companion, and the model was
told nothing it could use in place of the value.

Now both copies call ai_key_vault.request_credential, which files ONE
'credential' ask through ConsentService (scope 'secret:<NAME>') and answers
with the {{secret:NAME}} alias.  These tests run the real ConsentService on an
in-memory database, the real vault and the real request_resource closure.
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import pytest  # noqa: E402

from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402

OWNER = 'owner-cred'
SECRET = 'Tr0ub4dor&3-horse'
ASK = '{"key_name": "site_password", "label": "Site password", ' \
      '"used_by": "the login step", "description": "Needed to sign in."}'


@pytest.fixture(autouse=True)
def world(monkeypatch):
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    monkeypatch.delenv('SITE_PASSWORD', raising=False)
    from security.secrets_manager import SecretsManager
    from hartos import ai_key_vault
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    # get_ai_key_vault keeps its own module-level instance beside the class one.
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    emitted = []
    from integrations.social import consent_service
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None: emitted.append((topic, dict(data))))
    yield emitted
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    os.environ.pop('SITE_PASSWORD', None)
    Base.metadata.drop_all(engine)


def _asks():
    with db_session() as db:
        return [(r.consent_type, r.scope, r.agent_id, bool(r.granted))
                for r in db.query(UserConsent).filter_by(user_id=OWNER).all()]


def test_credential_is_a_consent_type():
    from integrations.social.consent_service import CONSENT_TYPES
    assert 'credential' in CONSENT_TYPES


def test_a_missing_credential_is_asked_on_the_card_and_answered_with_the_alias(world):
    from hartos.ai_key_vault import request_credential
    out = request_credential(ASK, agent_id='42')

    assert '{{secret:SITE_PASSWORD}}' in out
    assert 'RESOURCE_REQUEST' not in out, 'no second surface: the card is the ask'
    assert _asks() == [('credential', 'secret:SITE_PASSWORD', '42', False)]
    requests = [d for t, d in world if t == 'consent.request']
    assert len(requests) == 1
    assert requests[0]['scope'] == 'secret:SITE_PASSWORD'
    assert 'Site password' in requests[0]['reason']


def test_asking_again_files_no_second_row(world):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    request_credential(ASK, agent_id='42')
    assert len(_asks()) == 1


def test_a_stored_credential_is_answered_with_the_alias_never_the_value(world):
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    get_ai_key_vault().store_credential('site_password', SECRET)

    out = request_credential(ASK, agent_id='42')
    assert SECRET not in out
    assert '{{secret:SITE_PASSWORD}}' in out
    assert _asks() == [], 'nothing to ask for'


def test_a_declined_ask_says_so_and_is_not_asked_again(world):
    from hartos.ai_key_vault import request_credential
    from integrations.social.consent_service import ConsentService
    request_credential(ASK, agent_id='42')
    with db_session(commit=True) as db:
        ConsentService.revoke_consent(db, OWNER, 'credential',
                                      'secret:SITE_PASSWORD', '42')
    world.clear()

    out = request_credential(ASK, agent_id='42')
    assert 'said no' in out
    assert [t for t, _ in world if t == 'consent.request'] == []


@pytest.mark.parametrize('agent', ['42', None])
def test_a_rejected_credential_is_asked_for_again(world, agent):
    """Owner 2026-09-25: "agent shd ask user when login attempts fails".
    The stored value was rejected by the site, so the card comes back even
    though the owner already answered it once (the card's grant writes a
    row for no agent, the same one consent_api writes)."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    from integrations.social.consent_service import ConsentService
    request_credential(ASK, agent_id=agent)
    get_ai_key_vault().store_credential('site_password', SECRET)
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'credential', 'secret:SITE_PASSWORD')
    world.clear()

    out = request_credential(ASK[:-1] + ', "rejected": true}', agent_id=agent)
    assert SECRET not in out
    assert '{{secret:SITE_PASSWORD}}' in out
    assert 'Asked the owner' in out
    requests = [d for t, d in world if t == 'consent.request']
    assert len(requests) == 1
    assert requests[0]['scope'] == 'secret:SITE_PASSWORD'
    assert 'rejected' in requests[0]['reason']


def test_the_alias_answer_says_how_to_report_a_rejection(world):
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    get_ai_key_vault().store_credential('site_password', SECRET)
    assert '"rejected": true' in request_credential(ASK, agent_id='42')


def test_a_rejection_after_the_owner_said_no_is_not_asked_again(world):
    from hartos.ai_key_vault import request_credential
    from integrations.social.consent_service import ConsentService
    request_credential(ASK, agent_id='42')
    with db_session(commit=True) as db:
        ConsentService.revoke_consent(db, OWNER, 'credential',
                                      'secret:SITE_PASSWORD', '42')
    world.clear()
    out = request_credential(ASK[:-1] + ', "rejected": true}', agent_id='42')
    assert 'said no' in out
    assert [t for t, _ in world if t == 'consent.request'] == []


REJECTED = ASK[:-1] + ', "rejected": true}'


@pytest.fixture
def card(monkeypatch):
    """The consent card's own door: the /api/social/consent blueprint
    (integrations.social.consent_api), signed in as the owner, on the same
    database the ask was filed in.  Nunba's consentApi.grant / .decline post
    exactly these bodies."""
    from types import SimpleNamespace
    from flask import Flask
    from integrations.social import auth, consent_api
    from integrations.social.models import get_db
    app = Flask(__name__)
    app.register_blueprint(consent_api.consent_bp)
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        (SimpleNamespace(id=OWNER, is_admin=False, is_moderator=False), get_db())
        if token == 'owner' else (None, None)))
    client = app.test_client()

    def post(path, body):
        return client.post('/api/social/consent' + path, json=body,
                           headers={'Authorization': 'Bearer owner'})
    return post


@pytest.mark.parametrize('agent', ['42', None])
def test_a_no_on_the_card_ends_the_asking_end_to_end(world, card, agent):
    """Owner ruling: consent must be able to say no.  The card's "Don't
    allow" posts /consent/decline with the ask's own {consent_type, scope,
    agent_id}; after that neither a plain nor a rejected-login call shows
    the card again, and the agent is told the owner said no."""
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=agent)
    asked = [d for t, d in world if t == 'consent.request']
    assert len(asked) == 1
    ask = asked[0]

    resp = card('/decline', {'consent_type': ask['consent_type'],
                             'scope': ask['scope'], 'agent_id': ask['agent_id']})
    assert resp.status_code == 200, resp.get_json()
    world.clear()

    for call in (ASK, REJECTED):
        out = request_credential(call, agent_id=agent)
        assert 'said no' in out
    assert [t for t, _ in world if t == 'consent.request'] == []


def test_a_no_after_a_rejected_login_ends_the_asking_end_to_end(world, card):
    """The loop F3 left open: Accept (vault + grant), the site rejects it,
    the card comes back; a no on THAT card must end it too, although the
    combination already holds a grant."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    request_credential(ASK, agent_id=None)
    get_ai_key_vault().store_credential('site_password', SECRET)
    assert card('', {'consent_type': 'credential',
                     'scope': 'secret:SITE_PASSWORD'}).status_code == 201
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id=None)

    resp = card('/decline', {'consent_type': 'credential',
                             'scope': 'secret:SITE_PASSWORD', 'agent_id': None})
    assert resp.status_code == 200, resp.get_json()
    world.clear()

    assert 'said no' in request_credential(REJECTED, agent_id=None)
    assert [t for t, _ in world if t == 'consent.request'] == []


@pytest.mark.parametrize('agent', ['42', None])
def test_allow_asking_again_undoes_a_no_end_to_end(world, card, agent):
    """Owner rule: a credential "no" must be undoable, with no friction.  The
    privacy page's "Allow asking again" posts /consent/reopen for the
    credential; the next time the agent needs it, the card comes back."""
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id=agent)
    assert card('/decline', {'consent_type': 'credential',
                             'scope': 'secret:SITE_PASSWORD',
                             'agent_id': agent}).status_code == 200
    assert 'said no' in request_credential(ASK, agent_id=agent)

    resp = card('/reopen', {'consent_type': 'credential',
                            'scope': 'secret:SITE_PASSWORD'})
    assert resp.status_code == 200, resp.get_json()
    world.clear()

    assert 'Asked the owner' in request_credential(ASK, agent_id=agent)
    assert len([t for t, _ in world if t == 'consent.request']) == 1


def test_allow_asking_again_after_a_no_on_a_re_ask(world, card):
    """The no given on the card that came back after a rejected login
    revoked the earlier grant; reopening makes it askable again without
    turning that grant back on."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    from integrations.social.consent_service import ConsentService
    request_credential(ASK, agent_id=None)
    get_ai_key_vault().store_credential('site_password', SECRET)
    card('', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    request_credential(REJECTED, agent_id=None)
    card('/decline', {'consent_type': 'credential',
                      'scope': 'secret:SITE_PASSWORD', 'agent_id': None})

    assert card('/reopen', {'consent_type': 'credential',
                            'scope': 'secret:SITE_PASSWORD'}).status_code == 200
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id=None)
    with db_session() as db:
        assert ConsentService.active_grant(
            db, OWNER, 'credential', 'secret:SITE_PASSWORD') is None


def test_allow_asking_again_is_not_a_yes(world, card):
    """Reopening only takes the no back: nothing is granted, and the
    credential is not an owner-entered one until the owner types it."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    from integrations.social.consent_service import ConsentService
    request_credential(ASK, agent_id='42')
    card('/decline', {'consent_type': 'credential',
                      'scope': 'secret:SITE_PASSWORD', 'agent_id': '42'})
    card('/reopen', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    with db_session() as db:
        assert not ConsentService.check_consent(
            db, OWNER, 'credential', 'secret:SITE_PASSWORD', '42')
        assert not ConsentService.declined(
            db, OWNER, 'credential', 'secret:SITE_PASSWORD', '42')
    assert 'SITE_PASSWORD' not in get_ai_key_vault().owner_credential_names()


def test_reopen_never_brings_a_revoked_grant_back(world, card):
    """The privacy page's own revoke (consent_api.revoke_consent) keeps
    granted=True and sets revoked_at.  Reopening that row must not make it
    an active grant again."""
    from integrations.social.consent_service import ConsentService
    from hartos.ai_key_vault import get_ai_key_vault
    card('', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    assert card('/revoke', {'consent_type': 'credential',
                            'scope': 'secret:SITE_PASSWORD'}).status_code == 200
    assert card('/reopen', {'consent_type': 'credential',
                            'scope': 'secret:SITE_PASSWORD'}).status_code == 200
    with db_session() as db:
        assert ConsentService.active_grant(
            db, OWNER, 'credential', 'secret:SITE_PASSWORD') is None
    assert 'SITE_PASSWORD' not in get_ai_key_vault().owner_credential_names()


def test_reopen_leaves_other_credentials_declined(world, card):
    from hartos.ai_key_vault import request_credential
    other = ASK.replace('site_password', 'other_key')
    request_credential(ASK, agent_id='42')
    request_credential(other, agent_id='42')
    for scope in ('secret:SITE_PASSWORD', 'secret:OTHER_KEY'):
        card('/decline', {'consent_type': 'credential', 'scope': scope, 'agent_id': '42'})
    card('/reopen', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    assert 'said no' in request_credential(other, agent_id='42')


def test_reopen_with_nothing_declined_is_404_and_needs_a_type(world, card):
    assert card('/reopen', {'consent_type': 'credential',
                            'scope': 'secret:NEVER_ASKED'}).status_code == 404
    assert card('/reopen', {'scope': 'secret:X'}).status_code == 400
    assert card('/reopen', {'consent_type': 'bogus', 'scope': 'x'}).status_code == 400


def _grant_times(n, when=None):
    """n Accepts on the card: each one appends a granted row (agent None),
    the same row consent_api.grant_consent writes."""
    from integrations.social.consent_service import ConsentService
    with db_session(commit=True) as db:
        for _ in range(n):
            row = ConsentService.grant_consent(db, OWNER, 'credential',
                                               'secret:SITE_PASSWORD')
            if when is not None:
                row.granted_at = when
    return n


def test_re_asks_after_a_rejection_are_bounded(world):
    """7f3d5468f re-asked after every rejected login with no limit.  After
    MAX_CREDENTIAL_ENTRIES values the owner typed were all rejected, the card
    is not shown again and the agent is told to stop and tell the user."""
    from hartos.ai_key_vault import MAX_CREDENTIAL_ENTRIES, request_credential
    request_credential(ASK, agent_id='42')
    _grant_times(MAX_CREDENTIAL_ENTRIES - 1)
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id='42')
    assert len([t for t, _ in world if t == 'consent.request']) == 1

    _grant_times(1)
    world.clear()
    out = request_credential(REJECTED, agent_id='42')
    assert [t for t, _ in world if t == 'consent.request'] == []
    assert f'{MAX_CREDENTIAL_ENTRIES} values' in out
    assert 'tell the user' in out.lower()
    assert SECRET not in out


def test_the_bound_counts_only_recent_entries(world):
    """Entries from an earlier day do not count: a password changed next
    month is asked for again."""
    from datetime import datetime, timedelta
    from hartos.ai_key_vault import (CREDENTIAL_ENTRY_WINDOW_S,
                                     MAX_CREDENTIAL_ENTRIES, request_credential)
    request_credential(ASK, agent_id='42')
    old = datetime.utcnow() - timedelta(seconds=CREDENTIAL_ENTRY_WINDOW_S + 60)
    _grant_times(MAX_CREDENTIAL_ENTRIES, when=old)
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id='42')
    assert len([t for t, _ in world if t == 'consent.request']) == 1


def test_the_bound_counts_this_credential_only(world):
    from hartos.ai_key_vault import MAX_CREDENTIAL_ENTRIES, request_credential
    from integrations.social.consent_service import ConsentService
    request_credential(ASK, agent_id='42')
    with db_session(commit=True) as db:
        for _ in range(MAX_CREDENTIAL_ENTRIES):
            ConsentService.grant_consent(db, OWNER, 'credential', 'secret:OTHER_KEY')
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id='42')


def test_the_bound_counts_entered_values_not_asks(world):
    """Asks are rows too (one pending row per asking agent).  Only values the
    owner typed count: three agents asking plus one entry is one entry."""
    from hartos.ai_key_vault import request_credential
    for agent in ('1', '2', '3'):
        request_credential(ASK, agent_id=agent)
    _grant_times(1)
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id='1')


def test_the_stop_says_rolling_day_and_the_way_back(world):
    """After the stop the user is told where to go instead of waiting a day:
    Privacy settings, "Allow asking again"."""
    from hartos.ai_key_vault import MAX_CREDENTIAL_ENTRIES, request_credential
    request_credential(ASK, agent_id='42')
    _grant_times(MAX_CREDENTIAL_ENTRIES)
    out = request_credential(REJECTED, agent_id='42')
    assert 'last 24 hours' in out
    assert 'today' not in out
    assert 'Allow asking again' in out and 'Privacy settings' in out


def test_allow_asking_again_ends_the_stop(world, card):
    """The way back after the stop is the same button as after a no: reopen,
    and the next rejected login asks the owner on the card again."""
    from hartos.ai_key_vault import MAX_CREDENTIAL_ENTRIES, request_credential
    request_credential(ASK, agent_id='42')
    _grant_times(MAX_CREDENTIAL_ENTRIES)
    assert 'Asked the owner' not in request_credential(REJECTED, agent_id='42')
    assert card('/reopen', {'consent_type': 'credential',
                            'scope': 'secret:SITE_PASSWORD'}).status_code == 200
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id='42')
    assert len([t for t, _ in world if t == 'consent.request']) == 1


def test_the_stop_is_not_dodged_by_asking_without_rejected(world):
    from hartos.ai_key_vault import MAX_CREDENTIAL_ENTRIES, request_credential
    request_credential(ASK, agent_id='42')
    _grant_times(MAX_CREDENTIAL_ENTRIES)
    request_credential(REJECTED, agent_id='42')
    world.clear()
    request_credential(ASK, agent_id='42')
    assert [t for t, _ in world if t == 'consent.request'] == []


# ── A name the process already holds is not a credential ───────────

@pytest.mark.parametrize('name', ['PATH', 'HTTPS_PROXY', 'NUNBA_CI'])
def test_a_setting_of_this_computer_is_never_asked_for(world, monkeypatch, name):
    """Review of Nunba 670aed3f: asking for PATH put a card up, the grant made
    PATH an owner credential, and {{secret:PATH}} resolved to the system
    value.  A name the process holds that the owner never entered is refused
    before any card, and the refusal says nothing a name that does not exist
    would not get (secrets review: "is a setting of this computer" confirmed
    the variable exists, the F1 oracle again)."""
    from hartos.ai_key_vault import request_credential
    ask = '{"key_name": "%s", "label": "x"}' % name
    monkeypatch.setenv(name, 'system-value')
    held = request_credential(ask, agent_id='42')
    assert 'system-value' not in held
    assert [t for t, _ in world if t == 'consent.request'] == []
    assert [a for a in _asks() if a[1] == 'secret:' + name] == []

    monkeypatch.delenv(name)
    assert held == request_credential(ask, agent_id='42'), \
        'a held name is answered exactly as a name nothing holds'


def test_a_credential_the_owner_entered_before_is_still_asked_again(world, monkeypatch):
    """The card's first entry puts SITE_PASSWORD in the environment (Nunba
    export_to_env); a rejected login must still reach the card."""
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    _grant_times(1)
    monkeypatch.setenv('SITE_PASSWORD', 'what-the-owner-typed')
    world.clear()
    assert 'Asked the owner' in request_credential(REJECTED, agent_id='42')


def test_store_credential_never_replaces_a_setting(world, monkeypatch):
    """/api/credentials/submit (store_credential) set os.environ for any
    name.  It now leaves a name the process holds that it did not store
    alone, and answers exactly as for any other name, so the endpoint does
    not tell a caller which variables exist."""
    from hartos.ai_key_vault import get_ai_key_vault
    vault = get_ai_key_vault()
    monkeypatch.setenv('PATH_TEST_SETTING', 'system-value')
    held = vault.store_credential('PATH_TEST_SETTING', 'typed')
    assert os.environ['PATH_TEST_SETTING'] == 'system-value'
    assert 'PATH_TEST_SETTING' not in vault.owner_credential_names()
    assert vault._secrets_manager()._cache.get('PATH_TEST_SETTING') is None
    monkeypatch.delenv('FRESH_TEST_NAME', raising=False)
    assert held == 'PATH_TEST_SETTING'
    assert vault.store_credential('fresh_test_name', 'typed') == 'FRESH_TEST_NAME'
    os.environ.pop('FRESH_TEST_NAME', None)


def test_store_credential_replaces_its_own_value(world):
    from hartos.ai_key_vault import get_ai_key_vault
    vault = get_ai_key_vault()
    vault.store_credential('site_password', 'wrong')
    vault.store_credential('site_password', 'right')
    assert vault.get_tool_key('SITE_PASSWORD') == 'right'


# ── A value entered for an agent stays in the vault, not the environment ──
#
# A card entry for an unset name (NUNBA_CI, HTTPS_PROXY...) used to reach
# os.environ (Nunba export_to_env's setdefault; store_credential's own
# injection), where the process reads it as configuration.  Now a value the
# owner enters for an agent lives in the vault and reaches a tool only
# through its alias.  Only a name the process legitimately reads from its
# environment (security.secrets_manager.SECRET_KEYS) is also put there.

@pytest.mark.parametrize('name', ['SITE_PASSWORD', 'NUNBA_CI', 'HTTPS_PROXY'])
def test_a_stored_credential_never_enters_the_environment(world, monkeypatch, name):
    from hartos.ai_key_vault import get_ai_key_vault
    monkeypatch.delenv(name, raising=False)
    vault = get_ai_key_vault()
    vault.store_credential(name, 'typed-by-the-owner')
    assert name not in os.environ
    assert vault.get_tool_key(name) == 'typed-by-the-owner'
    assert vault.resolve_aliases('{{secret:%s}}' % name) == 'typed-by-the-owner'


def test_a_name_the_process_reads_from_env_still_reaches_it(world, monkeypatch):
    from hartos.ai_key_vault import get_ai_key_vault
    from security.secrets_manager import SECRET_KEYS
    assert 'NEWS_API_KEY' in SECRET_KEYS
    monkeypatch.delenv('NEWS_API_KEY', raising=False)
    get_ai_key_vault().store_credential('NEWS_API_KEY', 'news-DUMMY')
    assert os.environ['NEWS_API_KEY'] == 'news-DUMMY'


@pytest.mark.parametrize('name', ['SITE_PASSWORD', 'NUNBA_CI'])
def test_a_held_card_value_resolves_without_the_environment(world, card, monkeypatch, name):
    """Nunba's desktop vault keeps what the card stored and hands it to this
    vault (hold_credential) instead of os.environ; once the card's grant
    names it, the alias resolves to it."""
    from hartos.ai_key_vault import get_ai_key_vault
    monkeypatch.delenv(name, raising=False)
    vault = get_ai_key_vault()
    vault.hold_credential(name, 'held-DUMMY-value')
    assert name not in os.environ
    assert vault.resolve_aliases('{{secret:%s}}' % name) == '{{secret:%s}}' % name
    card('', {'consent_type': 'credential', 'scope': 'secret:' + name})
    assert vault.resolve_aliases('{{secret:%s}}' % name) == 'held-DUMMY-value'
    vault.hold_credential(name, 'held-again-DUMMY')
    assert vault.resolve_aliases('{{secret:%s}}' % name) == 'held-again-DUMMY'
    assert name not in os.environ


# ── A no on a re-ask card wins for every agent ─────────────────────

@pytest.mark.parametrize('agent', ['42', None])
def test_a_no_on_a_re_ask_card_ends_the_saved_value_for_every_agent(world, card, agent):
    """Review finding 3: with no agent the no revoked the saved value; for
    agent 42 the rejected value stayed saved and the agent was told it "is
    stored".  The owner's no now wins the same way for both."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    request_credential(ASK, agent_id=agent)
    get_ai_key_vault()._stored.discard('SITE_PASSWORD')
    get_ai_key_vault().hold_credential('SITE_PASSWORD', SECRET)  # the card's vault
    card('', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    assert 'is stored' in request_credential(ASK, agent_id=agent)
    request_credential(REJECTED, agent_id=agent)

    assert card('/decline', {'consent_type': 'credential',
                             'scope': 'secret:SITE_PASSWORD',
                             'agent_id': agent}).status_code == 200
    world.clear()
    out = request_credential(ASK, agent_id=agent)
    assert 'said no' in out
    assert get_ai_key_vault().resolve_aliases('{{secret:SITE_PASSWORD}}') \
        == '{{secret:SITE_PASSWORD}}'
    assert [t for t, _ in world if t == 'consent.request'] == []


def test_a_no_for_one_agent_wins_over_a_value_entered_for_another(world, card):
    """Review m1: agent 42 was told no, then the owner entered the value on
    agent 7's card.  The no still stands for 42 (and the privacy page keeps
    listing it); 7 gets the alias."""
    from hartos.ai_key_vault import get_ai_key_vault, request_credential
    request_credential(ASK, agent_id='42')
    card('/decline', {'consent_type': 'credential',
                      'scope': 'secret:SITE_PASSWORD', 'agent_id': '42'})
    request_credential(ASK, agent_id='7')
    get_ai_key_vault().hold_credential('SITE_PASSWORD', SECRET)
    card('', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})

    assert 'said no' in request_credential(ASK, agent_id='42')
    assert 'is stored' in request_credential(ASK, agent_id='7')


# ── Reopen keeps the history and tells the other pages ─────────────

def test_reopen_keeps_when_the_no_was_given(world, card):
    """Review m4: reopen used to erase revoked_at.  The no's time stays and
    the reopen's time is recorded beside it, visible in the listing."""
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    card('/decline', {'consent_type': 'credential',
                      'scope': 'secret:SITE_PASSWORD', 'agent_id': '42'})
    card('/reopen', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    with db_session() as db:
        row = db.query(UserConsent).filter_by(
            user_id=OWNER, scope='secret:SITE_PASSWORD', agent_id='42').one()
        assert row.revoked_at is not None
        assert row.reopened_at is not None and row.reopened_at >= row.revoked_at


def test_reopen_is_announced(world, card):
    """Other open privacy pages refresh on the same consent event stream the
    grant and revoke use."""
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    card('/decline', {'consent_type': 'credential',
                      'scope': 'secret:SITE_PASSWORD', 'agent_id': '42'})
    world.clear()
    card('/reopen', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    reopened = [d for t, d in world if t == 'consent.reopened']
    assert len(reopened) == 1
    assert reopened[0]['scope'] == 'secret:SITE_PASSWORD'
    assert reopened[0]['agent_id'] is None


def test_a_no_after_a_reopen_is_a_no_again(world, card):
    from hartos.ai_key_vault import request_credential
    request_credential(ASK, agent_id='42')
    body = {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD', 'agent_id': '42'}
    card('/decline', body)
    card('/reopen', {'consent_type': 'credential', 'scope': 'secret:SITE_PASSWORD'})
    request_credential(ASK, agent_id='42')
    assert card('/decline', body).status_code == 200
    assert 'said no' in request_credential(ASK, agent_id='42')


def test_with_no_owner_nothing_is_filed(world, monkeypatch):
    from hartos.ai_key_vault import request_credential
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    out = request_credential(ASK, agent_id='42')
    assert _asks() == []
    assert '{{secret:' not in out


def test_a_value_the_card_stored_resolves_and_is_masked_once_granted(world, monkeypatch):
    """The card's value lands in os.environ (Nunba /api/vault/store); the
    grant that follows is what makes it the owner's credential.  An env var
    with no grant is not one (tests/unit/test_secret_owner_entered.py)."""
    from hartos.ai_key_vault import get_ai_key_vault
    from integrations.social.consent_service import ConsentService
    monkeypatch.setenv('SITE_PASSWORD', SECRET)
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, 'credential', 'secret:SITE_PASSWORD')
    vault = get_ai_key_vault()
    assert vault.resolve_aliases('pass {{secret:SITE_PASSWORD}}') == f'pass {SECRET}'
    assert vault.mask_secrets(f'echo {SECRET}') == 'echo {{secret:SITE_PASSWORD}}'


def test_the_autogen_request_resource_tool_files_the_same_ask(world):
    from core import agent_tools
    ctx = {
        'user_id': 'someone-remote', 'prompt_id': '42', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': 's-1', 'request_id_list': [],
        'recent_file_id': {}, 'scheduler': MagicMock(),
        'log_tool_execution': lambda f: f,
        'send_message_to_user1': MagicMock(), 'retrieve_json': lambda v: v,
        'strip_json_values': lambda v: v, 'save_conversation_db': MagicMock(),
    }
    tools = {n: f for n, _d, f in agent_tools.build_core_tool_closures(ctx)}

    out = tools['request_resource'](ASK)
    assert '{{secret:SITE_PASSWORD}}' in out
    assert 'RESOURCE_REQUEST' not in out
    # The machine's owner is asked, not the remote caller.
    assert _asks() == [('credential', 'secret:SITE_PASSWORD', '42', False)]
