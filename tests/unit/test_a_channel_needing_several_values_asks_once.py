"""A channel that needs several values is asked for all of them on ONE card,
and the owner's answer starts it.

Before this, post_to_channel answered "Nothing asked" for every channel that
needs several values (chat_connect_blocker: SEVERAL_CREDENTIALS), so an agent
that needed Mattermost or Rocket.Chat could never get it connected from the
owner's phone.  259a6411d made register_channel start such a channel from all
its values when its start reads each one (starts_from_its_values); this asks
for them:

  * one 'credential' ask (scope 'secret:<CHANNEL>_SETUP'), its push carrying
    data.fields, the channel's required settings in its form's order, as a
    JSON string [{name, label, secret}];
  * the answer (POST /api/social/consent) carries ``values``, every field and
    nothing else, and starts the channel through register_channel, which
    keeps them sealed on the channel's binding as the connect form does;
  * a start that failed asks again on the same card;
  * a single-value ask carries no fields, as before.

Review of 2ba06d991 (rv_chan, REJECTED), forward-fixed here:
  * the desktop card shows the same fields (its note carries them), and its
    answer {consent_type, request_id, values}, the body Android's card sends
    (f2d640b49), is taken by its request_id;
  * nothing stored under the card's name '<CHANNEL>_SETUP' (the vault, the
    environment, the pending list) is ever its answer, and a yes by type and
    scope alone answers nothing, so a failed answer never stops the next ask;
  * a channel whose start needs a setting no card asks for (Matrix's
    MATRIX_USER_ID) is not carded while it is missing;
  * a card's fields come from the channel's own setup form, never from what
    an agent's ask passes.

These tests run the real post_to_channel ask, request_credential,
ConsentService and consent blueprint on an in-memory database; only the live
adapter wiring and its watcher are stubbed.
"""
import json
import os
import sys

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402
# The real _emit, taken before the world fixture stubs it: the desktop's note.
from integrations.social.consent_service import _emit as _real_emit  # noqa: E402

OWNER = 'owner-several'
STRANGER = 'stranger-several'
NODE = 'cccc9d3e55f0a7c2'       # this node's id (fleet_command.self_node_id)
SCOPE = 'secret:MATTERMOST_SETUP'
VALUES = {'server_url': 'https://mm.example', 'access_token': 'pat-123'}


class _FakeApi:
    def __init__(self):
        self._channels = {}

    def _save_config(self):
        pass


@pytest.fixture(autouse=True)
def world(monkeypatch):
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    monkeypatch.delenv('MATTERMOST_SERVER_URL', raising=False)
    from security import node_integrity
    monkeypatch.setattr(node_integrity, 'get_public_key_hex',
                        lambda: NODE + 'cd' * 24)
    from security.secrets_manager import SecretsManager
    from hartos import ai_key_vault
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    emitted = []
    from integrations.social import consent_service
    monkeypatch.setattr(consent_service, '_emit',
                        lambda topic, data, msg_id=None:
                        emitted.append((topic, dict(data), msg_id)))
    # The live adapter, its watcher and the admin config file are the
    # boundaries; the binding row is written to the in-memory database.
    from integrations.channels import agent_tools
    monkeypatch.setattr('integrations.channels.admin.api.get_api',
                        lambda: _FakeApi())
    wire = MagicMock(return_value={'success': True})
    monkeypatch.setattr('integrations.social.api_channels._wire_live_adapter',
                        wire)
    monkeypatch.setattr(agent_tools, '_report_when_live', MagicMock())
    monkeypatch.setattr(agent_tools, '_STARTS', {})
    yield SimpleNamespace(emitted=emitted, wire=wire)
    ai_key_vault.AIKeyVault.reset()
    SecretsManager.reset()
    Base.metadata.drop_all(engine)


@pytest.fixture
def phone(monkeypatch):
    """The overlay's door: the /api/social consent blueprint, signed in with
    the account token the phone holds.  An answer names the node it goes to,
    as the phone's build does (node_id from the push)."""
    from flask import Flask
    from integrations.social import auth, consent_api
    from integrations.social.models import get_db
    app = Flask(__name__)
    app.register_blueprint(consent_api.consent_bp)
    users = {'owner': OWNER, 'stranger': STRANGER}
    monkeypatch.setattr(auth, '_get_user_from_token', lambda token: (
        (SimpleNamespace(id=users[token], is_admin=False, is_moderator=False),
         get_db()) if token in users else (None, None)))
    client = app.test_client()

    def post(body, who='owner'):
        body = dict({'node_id': NODE, 'consent_type': 'credential'}, **body)
        return client.post('/api/social/consent', json=body,
                           headers={'Authorization': 'Bearer ' + who})
    return post


def _ask_post(channel='mattermost'):
    """post_to_channel's connect-or-ask for ``channel``, not connected."""
    from integrations.agent_engine.marketing_tools import _connect_or_ask
    from integrations.channels.metadata import get_channel_metadata
    return json.loads(_connect_or_ask(
        channel, get_channel_metadata(channel),
        SimpleNamespace(get_status=lambda: None)))


def _pushes(world):
    """Every consent ask's push, as consent_prompt_push builds it."""
    from integrations.social.consent_service import consent_prompt_push
    return [consent_prompt_push(d, m.split(':', 1)[-1])
            for t, d, m in world.emitted if t == 'consent.request' and m]


def _row(request_id):
    with db_session() as db:
        row = db.query(UserConsent).filter(UserConsent.id == request_id).first()
        return SimpleNamespace(scope=row.scope, granted=row.granted)


def test_one_card_asks_for_every_value_the_channel_needs(world):
    said = _ask_post()
    assert said.get('needs_owner') is True
    (push,) = _pushes(world)
    assert json.loads(push['data']['fields']) == [
        {'name': 'server_url', 'label': 'Mattermost Server URL',
         'secret': False},
        {'name': 'access_token', 'label': 'Personal Access Token',
         'secret': True},
    ]
    assert _row(push['data']['request_id']).scope == SCOPE
    world.wire.assert_not_called()


def test_the_answer_with_every_value_starts_the_channel(world, phone):
    _ask_post()
    (push,) = _pushes(world)
    rid = push['data']['request_id']
    resp = phone({'request_id': rid, 'values': VALUES})
    assert resp.status_code == 201, resp.get_json()
    world.wire.assert_called_once_with(
        'mattermost', 'pat-123', {'server_url': 'https://mm.example'})
    assert resp.get_json()['data']['scope'] == SCOPE
    # Kept where the channel's start keeps them (its sealed binding), never
    # a second copy in the vault under names nothing else reads.
    from hartos.ai_key_vault import get_ai_key_vault
    vault = get_ai_key_vault()
    assert vault.get_tool_key('MATTERMOST_ACCESS_TOKEN') == ''
    assert vault.get_tool_key('MATTERMOST_SETUP') == ''


def test_the_typed_values_are_never_logged_or_returned(world, phone, caplog):
    import logging
    _ask_post()
    (push,) = _pushes(world)
    with caplog.at_level(logging.DEBUG):
        resp = phone({'request_id': push['data']['request_id'],
                      'values': VALUES})
    assert 'pat-123' not in caplog.text
    assert 'pat-123' not in resp.get_data(as_text=True)


@pytest.mark.parametrize('values', [
    {'server_url': 'https://mm.example'},                       # one missing
    dict(VALUES, extra='x'),                                    # one too many
    {'server_url': 'https://mm.example', 'access_token': '  '},  # one empty
    'pat-123',                                                  # not a map
])
def test_an_answer_that_is_not_exactly_the_cards_fields_starts_nothing(
        world, phone, values):
    _ask_post()
    (push,) = _pushes(world)
    rid = push['data']['request_id']
    resp = phone({'request_id': rid, 'values': values})
    assert resp.status_code == 400
    assert 'pat-123' not in resp.get_data(as_text=True)
    world.wire.assert_not_called()
    assert _row(rid).granted is False


def test_one_value_does_not_answer_the_card_for_several(world, phone):
    _ask_post()
    (push,) = _pushes(world)
    resp = phone({'request_id': push['data']['request_id'],
                  'scope': SCOPE, 'value': 'pat-123'})
    assert resp.status_code == 400
    world.wire.assert_not_called()


def test_only_the_owner_starts_the_channel(world, phone, monkeypatch):
    _ask_post()
    (push,) = _pushes(world)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'someone-else')
    resp = phone({'request_id': push['data']['request_id'], 'values': VALUES})
    assert resp.status_code == 403
    world.wire.assert_not_called()


def test_values_for_another_node_are_refused(world, phone):
    _ask_post()
    (push,) = _pushes(world)
    resp = phone({'request_id': push['data']['request_id'],
                  'values': VALUES, 'node_id': 'ffff' + NODE[4:]})
    assert resp.status_code == 404
    world.wire.assert_not_called()


def test_values_naming_the_scope_but_not_the_node_are_refused(world, phone):
    # As a value is (f2d640b49): an answer that names one names both.
    _ask_post()
    (push,) = _pushes(world)
    resp = phone({'request_id': push['data']['request_id'],
                  'values': VALUES, 'scope': SCOPE, 'node_id': None})
    assert resp.status_code == 404
    world.wire.assert_not_called()


# ── The desktop card (rv_chan 2ba06d991 (a)) ─────────────────────────

def test_the_desktop_note_carries_the_cards_fields(world, monkeypatch):
    """The desktop card is drawn from the ask's note (consent_service._emit
    -> on_notification), not from the phone's push: the note names the same
    fields, the same JSON string, and the request_id the answer goes by."""
    _ask_post()
    ((topic, data, msg_id),) = [e for e in world.emitted
                                if e[0] == 'consent.request']
    notes = []
    from integrations.social import realtime
    monkeypatch.setattr(realtime, 'on_notification',
                        lambda uid, note, push=None: notes.append(note))
    monkeypatch.setattr('core.platform.events.emit_event',
                        lambda *a, **k: None)
    _real_emit(topic, data, msg_id=msg_id)
    (note,) = notes
    (push,) = _pushes(world)
    assert note['fields'] == push['data']['fields']
    assert [f['name'] for f in json.loads(note['fields'])] == [
        'server_url', 'access_token']
    assert note['request_id'] == push['data']['request_id']


def test_the_desktop_answer_starts_the_channel(world, phone):
    """The desktop card answers as Android's card does (f2d640b49): its type,
    the ask's request_id and what was typed, naming no scope and no node, so
    it is taken by its request_id."""
    _ask_post()
    (push,) = _pushes(world)
    rid = push['data']['request_id']
    resp = phone({'consent_type': 'credential', 'request_id': rid,
                  'values': VALUES, 'node_id': None})
    assert resp.status_code == 201, resp.get_json()
    world.wire.assert_called_once_with(
        'mattermost', 'pat-123', {'server_url': 'https://mm.example'})
    assert resp.get_json()['data']['scope'] == SCOPE


def test_a_yes_by_type_and_scope_does_not_answer_the_card(world, phone):
    """The old desktop card's yes ({consent_type, scope}, after storing one
    value under the card's name): it starts nothing, so it answers nothing,
    and the next post asks again on the same card."""
    _ask_post()
    (push,) = _pushes(world)
    rid = push['data']['request_id']
    resp = phone({'consent_type': 'credential', 'scope': SCOPE,
                  'node_id': None})
    assert resp.status_code == 400
    world.wire.assert_not_called()
    with db_session() as db:
        assert not db.query(UserConsent).filter(
            UserConsent.scope == SCOPE, UserConsent.granted.is_(True)).count()
    said = _ask_post()
    assert said.get('needs_owner') is True
    assert _pushes(world)[-1]['data']['request_id'] == rid


@pytest.mark.parametrize('how', ['store', 'hold', 'environment'])
def test_a_value_under_the_cards_name_never_answers_it(world, monkeypatch, how):
    """One value kept under '<CHANNEL>_SETUP' (the old desktop card's vault
    store, the pending list's submit, Nunba's export, the environment) is
    not the channel's values: the vault keeps none, and the next post asks
    again on the card."""
    from hartos.ai_key_vault import get_ai_key_vault
    _ask_post()
    (first,) = _pushes(world)
    vault = get_ai_key_vault()
    if how == 'store':
        vault.store_credential('MATTERMOST_SETUP', 'pat-123')
    elif how == 'hold':
        vault.hold_credential('MATTERMOST_SETUP', 'pat-123')
    else:
        monkeypatch.setenv('MATTERMOST_SETUP', 'pat-123')
    if how != 'environment':
        assert vault.get_tool_key('MATTERMOST_SETUP') == ''
        assert vault.owner_credential('MATTERMOST_SETUP') == ''
    said = _ask_post()
    assert said.get('needs_owner') is True
    assert 'is stored on this computer' not in said['owner_answer']
    again = _pushes(world)
    assert len(again) == 2
    assert again[-1]['data']['request_id'] == first['data']['request_id']


def test_the_pending_list_never_offers_the_card_as_one_value(world):
    """The pending list (/api/credentials/pending, one input per name) is not
    a place to answer a card for several values."""
    from hartos.ai_key_vault import get_ai_key_vault
    _ask_post()
    assert 'MATTERMOST_SETUP' not in [
        p.get('key_name') for p in get_ai_key_vault().get_pending_requests()]


# ── A setting no card asks for (rv_chan 2ba06d991 (b)) ───────────────

def test_a_channel_needing_an_operator_setting_is_not_carded(world, monkeypatch):
    """Matrix starts only with MATRIX_USER_ID, which its setup form does not
    ask for: while the operator has not set it, no answer could start it,
    so nothing is asked and the agent is told which setting is missing."""
    monkeypatch.delenv('MATRIX_USER_ID', raising=False)
    said = _ask_post('matrix')
    assert said.get('cannot_connect_here') is True
    assert said.get('needs_owner') is False
    assert 'MATRIX_USER_ID' in json.dumps(said)
    assert _pushes(world) == []


def test_the_same_channel_is_carded_once_the_operator_set_it(world, monkeypatch):
    monkeypatch.setenv('MATRIX_USER_ID', '@bot:matrix.example')
    said = _ask_post('matrix')
    assert said.get('needs_owner') is True
    (push,) = _pushes(world)
    assert [f['name'] for f in json.loads(push['data']['fields'])] == [
        'homeserver_url', 'access_token']


# ── Whose fields (rv_chan 2ba06d991 (c)) ─────────────────────────────

_AGENT_FIELDS = [{'name': 'user', 'label': 'Your bank login', 'secret': False},
                 {'name': 'pw', 'label': 'Your bank password', 'secret': True}]


def _agent_asks(monkeypatch, req):
    """An agent's Request_Resource, the node's own model saying yes."""
    from hartos import ai_key_vault
    monkeypatch.setattr(ai_key_vault, '_ask_verdict', lambda r: 'yes')
    monkeypatch.setattr(ai_key_vault, '_ask_doubted', lambda r: False)
    return ai_key_vault.request_credential_for_agent(json.dumps(req))


def test_an_agents_ask_puts_none_of_its_own_fields_on_a_card(world, monkeypatch):
    _agent_asks(monkeypatch, {
        'resource_type': 'api_key', 'key_name': 'ACME_API_KEY',
        'label': 'Acme API key', 'service': 'Acme', 'used_by': 'a step',
        'fields': _AGENT_FIELDS})
    (push,) = _pushes(world)
    assert 'fields' not in push['data']


def test_a_channels_card_shows_its_own_form_whoever_asks(world, monkeypatch):
    _agent_asks(monkeypatch, {
        'resource_type': 'channel_secret', 'channel_type': 'mattermost',
        'key_name': 'SETUP', 'label': 'Mattermost settings',
        'service': 'Mattermost', 'used_by': 'a step',
        'fields': _AGENT_FIELDS})
    (push,) = _pushes(world)
    assert [f['name'] for f in json.loads(push['data']['fields'])] == [
        'server_url', 'access_token']


def test_a_failed_start_asks_again_on_the_same_card(world, phone):
    from integrations.channels import agent_tools
    from integrations.channels.metadata import get_channel_metadata
    _ask_post()
    (first,) = _pushes(world)
    rid = first['data']['request_id']
    assert phone({'request_id': rid, 'values': VALUES}).status_code == 201
    # The adapter's login failed (its watcher records the start FAILED).
    agent_tools._record_start('mattermost', get_channel_metadata('mattermost'),
                              state=agent_tools.START_FAILED)
    said = _ask_post()
    assert said.get('value_refused') is True
    again = _pushes(world)[-1]
    assert again['data']['request_id'] == rid
    assert json.loads(again['data']['fields'])[0]['name'] == 'server_url'


def test_a_channel_whose_values_never_reach_its_start_is_not_asked(world):
    said = _ask_post('twitter')
    assert said.get('cannot_connect_here') is True
    assert _pushes(world) == []


def test_a_single_value_ask_carries_no_fields(world):
    from hartos.ai_key_vault import request_credential
    request_credential(json.dumps({'key_name': 'site_password',
                                   'label': 'Site password'}))
    (push,) = _pushes(world)
    assert 'fields' not in push['data']
