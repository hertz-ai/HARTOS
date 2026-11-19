"""A binding's reply carries no credential, sealed or plain.

GET /api/social/channels/bindings answered each row's metadata_json, which for
a mailbox holds what register_channel wrote there: the password (sealed under
the vault's key, or PLAIN on a node with no key), the servers and the address
(sealed too).  It goes only to the person's own client (the listing filters by
user), but a credential never leaves the server (design D8-c, hartos-43).

ONE serializer, flask_integration.binding_for_client, answers every route that
returns a binding: it keeps the non-secret display fields (the address, the
servers, a display name) opened when sealed, and nothing else.

Real routes, the real connect tool, channel registry and EmailAdapter on a
running loop; mocked: the mail servers.  Both vault states are run.
"""
import json

import pytest

from tests.unit.test_each_goal_sends_from_its_own_mailbox import (  # noqa: F401
    HOSTS, MCGROCE, PASSWORDS, _mailbox, channels, mail, mailer, node, web)


@pytest.fixture(params=['sealed', 'plain'])
def vault(request, tmp_path, monkeypatch):
    """A node whose vault has its key ('sealed'), or has none ('plain': the
    credential is kept in plain text, as a node without HEVOLVE_MASTER_KEY
    keeps it)."""
    from security import secrets_manager
    from security.secrets_manager import SecretsManager
    monkeypatch.setattr(secrets_manager, '_SALT_PATH', str(tmp_path / 'salt'))
    monkeypatch.setattr(secrets_manager, '_VAULT_PATH', str(tmp_path / 'vault'))
    if request.param == 'sealed':
        monkeypatch.setenv('HEVOLVE_MASTER_KEY', 'test-only-vault-passphrase')
    else:
        monkeypatch.delenv('HEVOLVE_MASTER_KEY', raising=False)
    SecretsManager.reset()
    yield request.param
    SecretsManager.reset()


def _stored_row(user_id, address):
    from integrations.social.models import UserChannelBinding, db_session
    with db_session(commit=False) as db:
        row = db.query(UserChannelBinding).filter_by(
            user_id=user_id, channel_type='email', channel_sender_id=address).one()
        return dict(row.metadata_json)


def test_the_bindings_listing_carries_no_credential_of_a_mailbox(node, web, vault):
    seller = web.sign_up('seller')
    assert web.connect(seller, MCGROCE).get_json()['success'] is True
    _mailbox(node.channels, MCGROCE)
    stored = _stored_row(seller['id'], MCGROCE)
    password = PASSWORDS[MCGROCE]
    # The test means something only if the row does hold the credential.
    if vault == 'sealed':
        assert stored['password'].startswith('fernet:') and password not in json.dumps(stored)
    else:
        assert stored['password'] == password

    listed = web.client.get('/api/social/channels/bindings', headers=seller['auth'])

    text = listed.get_data(as_text=True)
    assert listed.status_code == 200
    assert password not in text and 'fernet:' not in text
    (row,) = listed.get_json()['data']
    assert row['metadata_json'] == {'email_address': MCGROCE,
                                    'imap_host': f'imap.{HOSTS[MCGROCE]}',
                                    'smtp_host': f'smtp.{HOSTS[MCGROCE]}'}
    assert row['is_active'] is True and row['channel_sender_id'] == MCGROCE


def test_no_route_that_returns_a_binding_carries_what_the_client_put_in_its_metadata(node, web):
    """The create, preferred and list replies all go through the one
    serializer: a token, a password and a sealed-looking blob a client stored
    in its own binding's metadata are not echoed; a display name is."""
    person = web.sign_up('person')
    secrets = {'bot_token': 'TOKEN-4411-SECRET', 'password': 'hunter2-SECRET',
               'api_key': 'KEY-8890-SECRET', 'blob': 'fernet:gAAAAAB-SECRET'}

    created = web.client.post('/api/social/channels/bindings', headers=person['auth'], json={
        'channel_type': 'web', 'channel_sender_id': 'tab-1',
        'metadata': {**secrets, 'display_name': 'My browser'}})
    binding_id = created.get_json()['data']['id']
    preferred = web.client.put(f'/api/social/channels/bindings/{binding_id}/preferred',
                               headers=person['auth'])
    # Re-pairing the same channel and sender answers the existing row.
    code = web.client.post('/api/social/channels/pair/generate',
                           headers=person['auth']).get_json()['data']['code']
    paired = web.client.post('/api/social/channels/pair/verify', headers=person['auth'], json={
        'code': code, 'channel': 'web', 'sender_id': 'tab-1'})
    listed = web.client.get('/api/social/channels/bindings', headers=person['auth'])

    assert created.status_code == 201 and preferred.status_code == 200
    assert paired.status_code == 200, paired.get_json()
    for reply in (created, preferred, paired, listed):
        text = reply.get_data(as_text=True)
        assert not [v for v in secrets.values() if v in text] and 'fernet:' not in text
        assert 'My browser' in text


def test_a_name_that_is_a_sealed_blob_is_not_shown(node, web):
    """A value that is sealed and stays sealed is never a display field."""
    person = web.sign_up('person')

    web.client.post('/api/social/channels/bindings', headers=person['auth'], json={
        'channel_type': 'web', 'channel_sender_id': 'tab-2',
        'metadata': {'display_name': 'fernet:gAAAAAB-DISPLAY-SECRET', 'smtp_port': 587}})
    listed = web.client.get('/api/social/channels/bindings', headers=person['auth'])

    (row,) = listed.get_json()['data']
    assert 'DISPLAY-SECRET' not in listed.get_data(as_text=True)
    assert row['metadata_json'] == {'smtp_port': 587}
