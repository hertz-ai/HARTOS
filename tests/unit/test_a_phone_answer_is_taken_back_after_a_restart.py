"""A revoked phone answer leaves no value in the vault, across a restart too.

Review rv5dd of 5dd7e7331, 22 Nov 2026, finding 1 (measured): the record of
which vault values a consent answer wrote lived only in the running process.
A phone answer, then a fresh process, then a privacy-page revoke (200): the
alias stopped resolving, but get_tool_key and SecretsManager.get_secret
still returned the value, secrets.enc still held it, and for a name the
process reads from its environment (NEWS_API_KEY) preload_env had put it in
os.environ, where it stayed.  Central restarts on every deploy.

Owner ruling: a phone-answered credential is read back before its grant, a
revoke stops it resolving, and that holds across a restart too.

Now the record (each answer's value, the vault's value before it,
os.environ's before it) is kept in the vault itself, under its one reserved
entry (security.secrets_manager.ANSWER_RECORD_KEY), written in the same
atomic save as the value.

Also here (rv5dd minors): the revoke's check of the grants left and its
removal run under one lock, and each take-back is one audit-trail line
naming the credential, never its value.

Real: request_credential's consent store, the consent blueprint, the vault
and SecretsManager with a temp vault dir and a fake vault key; the restart
is two child processes over one file database.  Every value here is fake.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)

from tests.unit.test_the_phone_answers_a_credential_ask import (  # noqa: E402,F401 (fixtures)
    NAME, OWNER, SCOPE, phone, world)

TYPED = 'fake-phone-answer-8R'
BEFORE = 'fake-value-the-node-had-2D'
LOCAL = 'fake-value-typed-on-this-computer-5T'
NEWS = 'NEWS_API_KEY'               # a name the process reads from os.environ
FAKE_VAULT_KEY = 'fake-vault-key-for-a-test-only'


@pytest.fixture
def vault_dir(monkeypatch, tmp_path):
    from security import secrets_manager
    from security.secrets_manager import SecretsManager
    monkeypatch.setattr(secrets_manager, '_VAULT_PATH', str(tmp_path / 'secrets.enc'))
    monkeypatch.setattr(secrets_manager, '_SALT_PATH', str(tmp_path / 'secrets.salt'))
    monkeypatch.setenv('HEVOLVE_MASTER_KEY', FAKE_VAULT_KEY)
    monkeypatch.delenv(NEWS, raising=False)
    SecretsManager.reset()
    yield tmp_path
    SecretsManager.reset()
    os.environ.pop(NEWS, None)


def _readers(name):
    """What each reader returns: get_tool_key, SecretsManager.get_secret,
    os.environ."""
    from hartos.ai_key_vault import get_ai_key_vault
    from security.secrets_manager import SecretsManager
    return (get_ai_key_vault().get_tool_key(name),
            SecretsManager.get_instance().get_secret(name),
            os.environ.get(name))


def _on_disk():
    """secrets.enc as the next start reads it."""
    from security.secrets_manager import SecretsManager
    return dict(SecretsManager()._cache)


def _restart(monkeypatch, *names):
    """A new process in this one: no singleton survives, os.environ holds
    none of ``names``, and the vault is preloaded as every start does."""
    from hartos import ai_key_vault
    from security.secrets_manager import SecretsManager
    SecretsManager.reset()
    ai_key_vault.AIKeyVault.reset()
    monkeypatch.setattr(ai_key_vault, '_instance', None)
    for name in names:
        os.environ.pop(name, None)
    return ai_key_vault.get_ai_key_vault().preload_env()


def _file_ask(scope):
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    with db_session() as db:
        return ConsentService.request_consent(db, OWNER, 'credential',
                                              scope=scope).id


def _answer(phone, rid, value):
    return phone('', {'consent_type': 'credential', 'request_id': rid,
                      'value': value})


def _revoke(phone, name):
    return phone('/revoke', {'consent_type': 'credential',
                             'scope': 'secret:' + name})


# ── The restart, in two real processes ────────────────────────────────

_CHILD = r'''
import json, os, sys
phase, root, tmp, name, value = sys.argv[1:6]
sys.path.insert(0, root)
os.chdir(root)
for var in ('HEVOLVE_DB_URL', 'DATABASE_URL', 'SOCIAL_DB_PATH', name):
    os.environ.pop(var, None)
os.environ['HEVOLVE_DB_PATH'] = os.path.join(tmp, 'social.db')
os.environ['HEVOLVE_AUDIT_DIR'] = os.path.join(tmp, 'audit')
os.environ['HEVOLVE_MASTER_KEY'] = 'fake-vault-key-for-a-test-only'
os.environ['HEVOLVE_OWNER_USER_ID'] = 'owner-phone'
from security import secrets_manager
secrets_manager._VAULT_PATH = os.path.join(tmp, 'secrets.enc')
secrets_manager._SALT_PATH = os.path.join(tmp, 'secrets.salt')
from hartos.ai_key_vault import get_ai_key_vault
vault = get_ai_key_vault()
vault.preload_env()                       # what every start does first
from integrations.social.models import Base, get_engine, db_session, get_db
Base.metadata.create_all(get_engine())
from integrations.social import auth, consent_api, consent_service
consent_service._emit = lambda *a, **k: None
# The node has a key of its own, so a value answer can name it.
from security import node_integrity
node_integrity.get_public_key_hex = lambda: 'bbbb9d3e55f0a7c2' + 'cd' * 24
from types import SimpleNamespace
from flask import Flask
app = Flask(__name__)
app.register_blueprint(consent_api.consent_bp)
auth._get_user_from_token = lambda token: (
    (SimpleNamespace(id='owner-phone', is_admin=False, is_moderator=False),
     get_db()) if token == 'owner' else (None, None))
client = app.test_client()
def post(path, body):
    return client.post('/api/social/consent' + path, json=body,
                       headers={'Authorization': 'Bearer owner'}).status_code
from security.secrets_manager import SecretsManager
def readers():
    return {'get_tool_key': vault.get_tool_key(name) == value,
            'get_secret': SecretsManager.get_instance().get_secret(name) == value,
            'environ': os.environ.get(name) == value}
out = {'before': readers()}
if phase == 'answer':
    with db_session() as db:
        rid = consent_service.ConsentService.request_consent(
            db, 'owner-phone', 'credential', scope='secret:' + name).id
    out['status'] = post('', {'consent_type': 'credential',
                              'request_id': rid, 'scope': 'secret:' + name,
                              'node_id': 'bbbb9d3e55f0a7c2', 'value': value})
else:
    out['status'] = post('/revoke', {'consent_type': 'credential',
                                     'scope': 'secret:' + name})
out['after'] = readers()
print('RESULT ' + json.dumps(out))
'''


def _child(phase, folder, name, value):
    """One process of the node: it starts (preload_env), does ``phase``
    and says what each reader returned, never the value."""
    script = os.path.join(folder, 'node_process.py')
    with open(script, 'w', encoding='utf-8') as f:
        f.write(_CHILD)
    done = subprocess.run([sys.executable, script, phase, ROOT, folder, name, value],
                          capture_output=True, text=True, timeout=180)
    lines = [l for l in done.stdout.splitlines() if l.startswith('RESULT ')]
    assert lines, f'the {phase} process said nothing (rc {done.returncode}): ' \
                  f'{done.stderr[-2000:]}'
    return json.loads(lines[-1][len('RESULT '):])


@pytest.mark.parametrize('name', [NAME, NEWS])
def test_a_revoke_after_a_restart_leaves_the_value_nowhere(monkeypatch, name):
    folder = tempfile.mkdtemp(prefix='hartos_vault_restart_')
    try:
        answered = _child('answer', folder, name, TYPED)
        assert answered['status'] == 201
        assert answered['after']['get_tool_key'] is True

        revoked = _child('revoke', folder, name, TYPED)

        assert revoked['before']['get_tool_key'] is True, 'the restart kept it'
        if name == NEWS:
            assert revoked['before']['environ'] is True, 'preload_env put it there'
        assert revoked['status'] == 200
        assert revoked['after'] == {'get_tool_key': False, 'get_secret': False,
                                    'environ': False}
        from security import secrets_manager
        monkeypatch.setattr(secrets_manager, '_VAULT_PATH',
                            os.path.join(folder, 'secrets.enc'))
        monkeypatch.setattr(secrets_manager, '_SALT_PATH',
                            os.path.join(folder, 'secrets.salt'))
        monkeypatch.setenv('HEVOLVE_MASTER_KEY', FAKE_VAULT_KEY)
        on_disk = _on_disk()
        assert name not in on_disk
        assert not any(TYPED in str(v) for v in on_disk.values()), \
            'secrets.enc no longer holds the value anywhere'
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ── What a take-back after a restart puts back ────────────────────────

def test_the_value_from_before_the_answer_comes_back_after_a_restart(
        world, phone, vault_dir, monkeypatch):
    from hartos.ai_key_vault import get_ai_key_vault
    from security.secrets_manager import SecretsManager
    SecretsManager.get_instance().set_secret(NEWS, BEFORE)  # the node had it
    get_ai_key_vault().preload_env()
    assert os.environ.get(NEWS) == BEFORE
    assert _answer(phone, _file_ask('secret:' + NEWS), TYPED).status_code == 201
    assert _readers(NEWS) == (TYPED, TYPED, TYPED)

    assert _restart(monkeypatch, NEWS) >= 1
    assert _readers(NEWS) == (TYPED, TYPED, TYPED)
    assert _revoke(phone, NEWS).status_code == 200

    assert _readers(NEWS) == (BEFORE, BEFORE, BEFORE)
    assert _on_disk().get(NEWS) == BEFORE
    assert not any(TYPED in str(v) for v in _on_disk().values())


def test_a_value_typed_on_this_computer_after_the_answer_stays(
        world, phone, vault_dir, monkeypatch):
    """A localhost /api/credentials/submit after the answer is the owner's
    own value: it clears the answer's record, so the revoke after a restart
    leaves it."""
    from hartos.ai_key_vault import get_ai_key_vault
    assert _answer(phone, _file_ask(SCOPE), TYPED).status_code == 201
    get_ai_key_vault().store_credential(NAME, LOCAL)

    _restart(monkeypatch, NAME)
    assert _revoke(phone, NAME).status_code == 200

    assert _readers(NAME)[:2] == (LOCAL, LOCAL)
    assert _on_disk().get(NAME) == LOCAL
    assert not any(TYPED in str(v) for v in _on_disk().values())


def test_the_answer_record_is_never_a_secret(world, phone, vault_dir, monkeypatch):
    """The record holds earlier values: it stays inside the encrypted file,
    and no reader, listing or export of the vault hands it out."""
    from hartos.ai_key_vault import get_ai_key_vault
    from security import secrets_manager
    from security.secrets_manager import SecretsManager
    key = getattr(secrets_manager, 'ANSWER_RECORD_KEY', None)
    assert key, 'the vault names its answer record once'
    assert _answer(phone, _file_ask('secret:' + NEWS), TYPED).status_code == 201
    raw_record = _on_disk().get(key)
    assert raw_record and TYPED in raw_record, 'the record is on disk'

    _restart(monkeypatch, NEWS)
    sm = SecretsManager.get_instance()
    vault = get_ai_key_vault()
    assert sm.get_secret(key) == '' and sm.has_secret(key) is False
    assert vault.get_tool_key(key) == ''
    assert key not in os.environ and raw_record not in os.environ.values()
    assert '{{secret:' + key not in str(vault.mask_secrets([raw_record]))
    with open(str(vault_dir / 'secrets.enc'), 'rb') as f:
        raw = f.read()
    assert TYPED.encode() not in raw and key.encode() not in raw


# ── The revoke's check and removal under one lock (rv5dd minor) ───────

def test_an_answer_stored_while_a_revoke_reads_the_grants_keeps_its_value(world):
    """take_back_answer(revoked=True) read the grants left outside the lock:
    an answer stored between that read and the removal was removed with the
    revoked one, though its own grant was still to come."""
    from hartos.ai_key_vault import get_ai_key_vault
    vault = get_ai_key_vault()
    vault.store_credential(NAME, TYPED, granted_only=True)
    newer = 'fake-newer-phone-answer-6W'
    racer = []

    def grants_left():
        th = threading.Thread(target=vault.store_credential, args=(NAME, newer),
                              kwargs={'granted_only': True})
        th.start()
        th.join(0.5)                    # the new answer lands now, if it can
        racer.append(th)
        return set()
    vault._granted_names = grants_left

    vault.take_back_answer(NAME, revoked=True)
    for th in racer:
        th.join(10)

    assert racer and not racer[0].is_alive()
    assert vault.get_tool_key(NAME) == newer


# ── Each take-back on the audit trail, by name only (rv5dd minor) ─────

def test_each_take_back_is_one_audit_line_naming_only_the_credential(
        world, phone, monkeypatch):
    from unittest.mock import patch
    from integrations.social import consent_api
    from security import audit_trail
    lines = []
    monkeypatch.setattr(audit_trail, 'record',
                        lambda kind, **fields: lines.append((kind, fields)))
    rid = _file_ask(SCOPE)
    with patch.object(consent_api.ConsentService, 'grant_consent',
                      side_effect=RuntimeError('database is locked')):
        try:
            _answer(phone, rid, TYPED)
        except RuntimeError:
            pass
    assert _answer(phone, rid, TYPED).status_code == 201
    assert _revoke(phone, NAME).status_code == 200

    taken = [f for k, f in lines if k == 'action' and f.get('name') == NAME]
    assert len(taken) == 2, lines
    assert all(f.get('via') == 'take_back_answer' for f in taken)
    assert TYPED not in repr(lines)


# ── What the key's listeners hear (review rv_4625 of 4625bfb58) ───────
# Before: the phone's store announced the key before its read-back and its
# grant, and a take-back announced nothing, so a phone answer whose grant
# failed left a voice restored whose key was gone (measured: the next lines
# cost failed worker starts).

@pytest.fixture
def heard():
    """What on_credential_stored and on_credential_removed listeners hear,
    in order, by name."""
    from hartos import ai_key_vault
    removed = getattr(ai_key_vault, 'on_credential_removed', None)
    assert callable(removed), 'a taken-back key has its own signal'
    events = []

    def on_stored(name):
        events.append(('stored', name))

    def on_removed(name):
        events.append(('removed', name))
    ai_key_vault.on_credential_stored(on_stored)
    removed(on_removed)
    yield events
    for registry, fn in ((ai_key_vault._STORED_LISTENERS, on_stored),
                         (getattr(ai_key_vault, '_REMOVED_LISTENERS', []), on_removed)):
        if fn in registry:
            registry.remove(fn)


def test_a_phone_answer_whose_grant_fails_tells_the_listeners_nothing(
        world, phone, heard):
    from unittest.mock import patch
    from integrations.social import consent_api
    rid = _file_ask(SCOPE)
    with patch.object(consent_api.ConsentService, 'grant_consent',
                      side_effect=RuntimeError('database is locked')):
        try:
            _answer(phone, rid, TYPED)
        except RuntimeError:
            pass

    assert heard == []


def test_a_granted_phone_answer_is_stored_once_and_a_revoke_removes_it(
        world, phone, heard):
    assert _answer(phone, _file_ask(SCOPE), TYPED).status_code == 201
    assert heard == [('stored', NAME)]

    assert _revoke(phone, NAME).status_code == 200
    assert heard == [('stored', NAME), ('removed', NAME)]


def test_a_revoke_that_brings_the_earlier_value_back_is_announced_too(
        world, phone, vault_dir, heard):
    """The earlier value may be the one a gate set aside: it checks again."""
    from security.secrets_manager import SecretsManager
    SecretsManager.get_instance().set_secret(NEWS, BEFORE)
    assert _answer(phone, _file_ask('secret:' + NEWS), TYPED).status_code == 201

    assert _revoke(phone, NEWS).status_code == 200

    assert heard == [('stored', NEWS), ('removed', NEWS)]
    assert _readers(NEWS)[:2] == (BEFORE, BEFORE)


def test_a_value_typed_on_this_computer_is_still_announced_at_once(world, heard):
    """/api/credentials/submit (localhost) stores and announces as before."""
    from hartos.ai_key_vault import get_ai_key_vault
    get_ai_key_vault().store_credential(NAME, LOCAL)
    assert heard == [('stored', NAME)]


# ── A refused answer takes back nothing of an earlier one (review rv4a1) ──
# Measured: the owner's phone answer is granted; the node restarts; the same
# name is set in the environment; the owner answers a new ask and gets the
# 409.  The 409 then took back the newest record, the earlier GRANTED value,
# from memory and disk, and logged "taken back (its grant did not stand);
# the value from before the answer is back".

@pytest.mark.parametrize('second', ['fake-second-phone-answer-7Y', TYPED])
def test_a_refused_answer_after_a_restart_keeps_the_earlier_granted_value(
        world, phone, vault_dir, monkeypatch, caplog, second):
    """Also when the new answer is the same text as the granted one: only
    what THIS answer wrote may go, and a refused store wrote nothing."""
    import logging
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import db_session
    from security.secrets_manager import SecretsManager
    assert _answer(phone, _file_ask(SCOPE), TYPED).status_code == 201
    _restart(monkeypatch, NAME)
    monkeypatch.setenv(NAME, 'fake-set-by-the-environment-1E')
    with db_session() as db:
        rid = ConsentService.request_consent(db, OWNER, 'credential',
                                             scope=SCOPE, reask=True).id

    with caplog.at_level(logging.INFO):
        resp = _answer(phone, rid, second)

    assert resp.status_code == 409
    assert SecretsManager.get_instance()._cache.get(NAME) == TYPED
    assert _on_disk().get(NAME) == TYPED
    assert not [r.getMessage() for r in caplog.records
                if 'taken back' in r.getMessage()]
    monkeypatch.delenv(NAME)
    assert _readers(NAME)[:2] == (TYPED, TYPED)


# ── A store that fails says why (owner 2026-11-22: real failure reasons) ──

def _replace_refused(monkeypatch, error):
    """secrets.enc cannot be replaced: every os.replace raises ``error``."""
    def replace(src, dst):
        raise error
    monkeypatch.setattr(os, 'replace', replace)


@pytest.mark.parametrize('error, said', [
    (PermissionError(13, 'Access is denied'), 'Answer again in a moment'),
    (OSError(28, 'No space left on device'), 'disk is full'),
])
def test_a_phone_answer_whose_save_fails_says_why(
        world, phone, vault_dir, monkeypatch, caplog, error, said):
    import logging
    from hartos.ai_key_vault import refused_answer
    rid = _file_ask(SCOPE)
    _replace_refused(monkeypatch, error)

    with caplog.at_level(logging.INFO):
        resp = _answer(phone, rid, TYPED)

    assert resp.status_code == 500
    told = resp.get_json()['error']
    assert type(error).__name__ in told and said in told, told
    assert type(error).__name__ in refused_answer(NAME)     # the agent hears it
    assert TYPED not in told and TYPED not in refused_answer(NAME)
    assert _readers(NAME)[:2] == ('', '')
    # Its own save failed, so the disk never held it, and the take-back's
    # log says so rather than that secrets.enc "keeps it".
    lines = [r.getMessage() for r in caplog.records
             if NAME in r.getMessage() and 'secrets.enc' in r.getMessage()]
    assert any('never held it' in m for m in lines), lines
    assert not any('still holds' in m or 'keeps it' in m for m in lines), lines
    assert not any(TYPED in m for m in caplog.messages)
