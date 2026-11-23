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


def test_values_without_the_node_they_are_for_are_refused(world, phone):
    _ask_post()
    (push,) = _pushes(world)
    resp = phone({'request_id': push['data']['request_id'],
                  'values': VALUES, 'node_id': None})
    assert resp.status_code == 404
    world.wire.assert_not_called()


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
