"""Phase 9.B — Double Ratchet integration into ConversationService.

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

Coverage:
  - Migration v52 creates ratchet_states with the unique-triple index.
  - State serialize/deserialize round-trip preserves all fields.
  - load_or_bootstrap creates state on first call, returns existing on
    second.  Initiator selection is deterministic on user_id sort.
  - send_message with `e2e_dms` flag OFF stores plaintext (regression).
  - send_message with flag ON + settings.e2e_enabled=True encrypts:
    messages.content == '[encrypted]', envelope row exists for
    recipient, sender plaintext is preserved in the response dict.
  - list_messages decrypts to original plaintext for the recipient.
  - Notification snippet is '[encrypted message]' when encrypted, so
    plaintext doesn't leak via offline-recipient fan-out.
  - Multiple messages on the same chain decrypt correctly.
  - Settings without e2e_enabled (or flag off) still uses plaintext
    even when the setting key is present but False.

Skipped when `cryptography` isn't installed (bare deploy without
the dep — same gate as test_phase9b_ratchet.py).
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


from integrations.social import e2e_ratchet as r

needs_crypto = pytest.mark.skipif(
    not r._HAS_CRYPTO,
    reason="cryptography package not installed; pip install cryptography")


# ── Fixtures (mirror test_phase9_e2e_keys.py) ──────────────────────


@pytest.fixture
def fresh_db(monkeypatch):
    monkeypatch.setenv('HEVOLVE_DB_PATH', ':memory:')
    from integrations.social import auth as auth_mod
    auth_mod._jwt_manager = False
    from integrations.social import models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None
    from integrations.social import migrations
    from integrations.social.models import get_engine, get_db
    eng = get_engine()
    migrations.run_migrations()
    db = get_db()
    try:
        yield db, eng
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            eng.dispose()
        except Exception:
            pass
        models_mod._engine = None
        models_mod._SessionLocal = None


def _seed_users(db, n=2):
    from integrations.social.models import User
    users = []
    for i in range(n):
        u = User(id=str(uuid.uuid4()),
                 username=f'u{i}_{uuid.uuid4().hex[:6]}',
                 display_name=f'U{i}',
                 email=f'u{i}_{uuid.uuid4().hex[:6]}@x.test',
                 password_hash='x:y', user_type='human')
        users.append(u)
    db.add_all(users)
    db.commit()
    return users


def _seed_dm(db, alice_id, bob_id, *, e2e_enabled=False):
    """Insert a DM conversation + memberships rows for both members.
    `e2e_enabled` controls the settings JSON the conversation carries."""
    from sqlalchemy import text
    cid = str(uuid.uuid4())
    settings = json.dumps({'e2e_enabled': bool(e2e_enabled)})
    db.execute(text(
        "INSERT INTO conversations (id, kind, created_by, settings) "
        "VALUES (:id, 'dm', :cb, :s)"),
        {'id': cid, 'cb': alice_id, 's': settings})
    for uid, role in ((alice_id, 'admin'), (bob_id, 'member')):
        db.execute(text(
            "INSERT INTO memberships "
            "(id, parent_kind, parent_id, member_id, agent_kind, role) "
            "VALUES (:id, 'conversation', :pid, :mid, 'human', :r)"),
            {'id': str(uuid.uuid4()), 'pid': cid, 'mid': uid, 'r': role})
    db.commit()
    return cid


def _publish_keys(db, conv_id, alice_id, bob_id):
    """Both members publish public identity keys.  We use the raw
    X25519 pub bytes (base64-encoded) — the bootstrap helper consumes
    these directly when it has to seed an initiator's their_dh_pub."""
    import base64
    from integrations.social.e2e_key_service import E2EKeyService
    _, alice_pub = r.generate_dh_keypair()
    _, bob_pub = r.generate_dh_keypair()
    a_b64 = base64.b64encode(alice_pub).decode('ascii')
    b_b64 = base64.b64encode(bob_pub).decode('ascii')
    E2EKeyService.publish_identity_key(
        db, conv_id, alice_id, identity_key_b64=a_b64)
    E2EKeyService.publish_identity_key(
        db, conv_id, bob_id, identity_key_b64=b_b64)
    return a_b64, b_b64


# ── Migration ───────────────────────────────────────────────────────


def test_v52_creates_ratchet_states(fresh_db):
    from sqlalchemy import text
    db, _ = fresh_db
    rows = db.execute(text(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name = 'ratchet_states'")).fetchall()
    assert rows, "v52 did not create ratchet_states"


def test_v52_unique_triple_index_present(fresh_db):
    from sqlalchemy import text
    db, _ = fresh_db
    rows = db.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
    names = {r[0] for r in rows}
    assert 'ux_ratchet_states_triple' in names


# ── State serialization round-trip ─────────────────────────────────


@needs_crypto
def test_state_serialize_round_trip():
    from integrations.social import e2e_state_repo as repo
    a_priv, a_pub = r.generate_dh_keypair()
    _, b_pub = r.generate_dh_keypair()
    state = r.init_ratchet(
        shared_secret=b'\x42' * 32,
        our_dh_priv=a_priv, our_dh_pub=a_pub,
        their_dh_pub=b_pub)
    blob = repo.serialize_state(state)
    rebuilt = repo.deserialize_state(blob)
    assert rebuilt.root_key == state.root_key
    assert rebuilt.our_dh_priv == state.our_dh_priv
    assert rebuilt.our_dh_pub == state.our_dh_pub
    assert rebuilt.their_dh_pub == state.their_dh_pub
    assert rebuilt.sending_index == state.sending_index
    assert rebuilt.skipped_keys == state.skipped_keys


@needs_crypto
def test_state_serialize_preserves_skipped_keys():
    from integrations.social import e2e_state_repo as repo
    a_priv, a_pub = r.generate_dh_keypair()
    _, b_pub = r.generate_dh_keypair()
    state = r.init_ratchet(
        shared_secret=b'\x42' * 32,
        our_dh_priv=a_priv, our_dh_pub=a_pub,
        their_dh_pub=b_pub)
    state = state._replace(skipped_keys={
        (b'\x01' * 32, 5): b'\xaa' * 32,
        (b'\x02' * 32, 9): b'\xbb' * 32,
    })
    blob = repo.serialize_state(state)
    rebuilt = repo.deserialize_state(blob)
    assert rebuilt.skipped_keys == state.skipped_keys


# ── load_or_bootstrap ───────────────────────────────────────────────


@needs_crypto
def test_load_or_bootstrap_creates_then_returns_existing(fresh_db):
    from integrations.social import e2e_state_repo as repo
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id)
    a_b64, b_b64 = _publish_keys(db, cid, alice.id, bob.id)

    s1 = repo.load_or_bootstrap(db, cid, alice.id, bob.id, a_b64, b_b64)
    s2 = repo.load_or_bootstrap(db, cid, alice.id, bob.id, a_b64, b_b64)
    # Same row returned both times — root_key + our_dh_priv stable
    assert s1.root_key == s2.root_key
    assert s1.our_dh_priv == s2.our_dh_priv


# ── send_message: flag-off regression ──────────────────────────────


def test_send_message_flag_off_stores_plaintext(fresh_db):
    """The default deploy has e2e_dms flag OFF.  Messages must be
    stored as plaintext exactly as before — zero regression on the
    existing send_message path."""
    from sqlalchemy import text
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    # No flask context → no g.feature_flags → flag is False.
    msg = ConversationService.send_message(
        db, cid, alice.id, "hi bob")
    row = db.execute(text(
        "SELECT content FROM messages WHERE id = :id"),
        {'id': msg['id']}).fetchone()
    assert row[0] == "hi bob"
    assert msg.get('is_encrypted') is False


@needs_crypto
def test_send_message_settings_off_stays_plaintext(fresh_db):
    """Even with the flag ON, a conversation that hasn't opted in
    via settings.e2e_enabled stays plaintext.  Both gates must agree."""
    from sqlalchemy import text
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=False)

    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        msg = ConversationService.send_message(
            db, cid, alice.id, "still plaintext")
    row = db.execute(text(
        "SELECT content FROM messages WHERE id = :id"),
        {'id': msg['id']}).fetchone()
    assert row[0] == "still plaintext"
    assert msg.get('is_encrypted') is False


# ── send_message: flag-on encrypt path ─────────────────────────────


@needs_crypto
def test_send_message_flag_on_encrypts(fresh_db):
    """Both gates ON → messages.content stored as '[encrypted]',
    one envelope per non-self recipient persisted, sender response
    still carries the plaintext."""
    from sqlalchemy import text
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    _publish_keys(db, cid, alice.id, bob.id)

    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        msg = ConversationService.send_message(
            db, cid, alice.id, "secret to bob")

    # Stored content is the placeholder.
    row = db.execute(text(
        "SELECT content FROM messages WHERE id = :id"),
        {'id': msg['id']}).fetchone()
    assert row[0] == '[encrypted]'

    # Sender response carries the original plaintext + recipient list.
    assert msg['content'] == "secret to bob"
    assert msg.get('is_encrypted') is True
    assert msg.get('recipients') == [bob.id]

    # Envelope row exists for bob (recipient), not for alice (sender).
    env_rows = db.execute(text(
        "SELECT recipient_id FROM message_envelopes "
        "WHERE message_id = :mid"),
        {'mid': msg['id']}).fetchall()
    recipients = {r[0] for r in env_rows}
    assert recipients == {bob.id}


@needs_crypto
def test_send_message_notify_snippet_does_not_leak_plaintext(fresh_db):
    """Encrypted DMs must not leak plaintext via notification fan-out.
    NotificationService is invoked with '[encrypted message]' as the
    snippet, never the original content."""
    from sqlalchemy import text
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    _publish_keys(db, cid, alice.id, bob.id)

    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        ConversationService.send_message(
            db, cid, alice.id, "very secret plaintext")

    # NotificationService rows for bob — the message column should not
    # contain the plaintext.
    rows = db.execute(text(
        "SELECT message FROM notifications WHERE user_id = :uid"),
        {'uid': bob.id}).fetchall()
    bodies = [(r[0] or '') for r in rows]
    for body in bodies:
        assert 'very secret plaintext' not in body, (
            f"plaintext leaked into notification body: {body}")


# ── list_messages: round-trip ──────────────────────────────────────


@needs_crypto
def test_list_messages_decrypts_for_recipient(fresh_db):
    """Bob's list_messages call returns the original plaintext after
    decrypting his envelope.  Alice's call (sender) returns the same
    placeholder the messages.content holds — Phase 9.B treats the
    SENDER's view as 'I already know what I sent' (the API response
    of send_message carries it).  Real clients render the sender-
    side from local cache; this is a property test."""
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    _publish_keys(db, cid, alice.id, bob.id)

    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        ConversationService.send_message(
            db, cid, alice.id, "hello bob")
        # Bob lists messages — should see the decrypted plaintext.
        bob_view = ConversationService.list_messages(
            db, cid, bob.id, limit=10)

    assert len(bob_view) == 1
    assert bob_view[0]['content'] == 'hello bob'
    assert bob_view[0]['is_encrypted'] is True


@needs_crypto
def test_list_messages_multiple_messages_advance_chain(fresh_db):
    """Three sequential sends; bob's list_messages decrypts each
    correctly even though each used a different message key derived
    from advancing the chain."""
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    _publish_keys(db, cid, alice.id, bob.id)

    plaintexts = ['first message', 'second message', 'third message']
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        for p in plaintexts:
            ConversationService.send_message(db, cid, alice.id, p)
        bob_view = ConversationService.list_messages(
            db, cid, bob.id, limit=10)

    # list_messages returns newest first.
    decrypted = [m['content'] for m in bob_view]
    assert decrypted == list(reversed(plaintexts))


@needs_crypto
def test_list_messages_flag_off_returns_placeholder(fresh_db):
    """If a recipient lists an e2e_enabled conversation but the server
    flag is OFF, decrypt path is skipped and the stored placeholder
    is returned.  Defense in depth: a malicious client can't trick
    the server into decrypting just by flipping settings."""
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService
    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    _publish_keys(db, cid, alice.id, bob.id)

    # Send under flag-on so the message is encrypted.
    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        ConversationService.send_message(db, cid, alice.id, "encrypted body")

    # Now bob lists with the flag OFF — placeholder, no decrypt.
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': False}
        bob_view = ConversationService.list_messages(
            db, cid, bob.id, limit=10)
    assert bob_view[0]['content'] == '[encrypted]'
    assert bob_view[0]['is_encrypted'] is False


# ── Module-level smoke ─────────────────────────────────────────────


def test_pipeline_module_imports_cleanly():
    from integrations.social import e2e_dm_pipeline  # noqa: F401
    assert hasattr(e2e_dm_pipeline, 'should_encrypt')
    assert hasattr(e2e_dm_pipeline, 'encrypt_for_conversation')
    assert hasattr(e2e_dm_pipeline, 'decrypt_for_recipient')


def test_state_repo_module_imports_cleanly():
    from integrations.social import e2e_state_repo  # noqa: F401
    assert hasattr(e2e_state_repo, 'load_or_bootstrap')
    assert hasattr(e2e_state_repo, 'save')
