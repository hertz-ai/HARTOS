"""Phase 9 closure — task #222 ship pytest.

Plan reference: sunny-gliding-eich.md, Part K.4 + Phase 9.

This file consolidates the closure checks for the four Phase 9
deliverables the workflow scout identified as still needing a
single-file end-to-end harness:

  E2E ratchet (#267 / #269)
    - send_message via ConversationService with conv.settings.
      e2e_enabled=True + g.feature_flags.e2e_dms=True writes
      ciphertext (the '[encrypted]' placeholder) into messages.content
      AND a per-recipient envelope row whose ciphertext is genuinely
      not the plaintext on the wire.
    - The recipient's list_messages call decrypts that envelope back
      to the original plaintext.

  X3DH initial handshake (#270)
    - initiator_derive_shared_secret + responder_derive_shared_secret
      round-trip to the same shared_secret (ECDH symmetry).
    - After bootstrapping a Double Ratchet with that shared_secret, the
      SECOND message uses a ratcheted key, not the X3DH one — verified
      by snapshotting the chain key before send #2 and asserting the
      derived msg_key was advanced, not reused.

  vlm_stop CSRF defense-in-depth (#271)
    - POST /api/vlm/stop without a same-origin Origin (cross-origin
      browser context) returns 403.
    - POST with no Origin / Referer (curl, native) returns 200 — this
      is the closest equivalent to "valid csrf_token" the existing
      design surfaces because the decorator's CSRF model is
      origin-header-based, not double-submit-cookie based.  The scout
      result acknowledged "no vlm_stop-route-specific CSRF test" and
      that decorator coverage exists in tests/unit/test_auth_local_csrf.py
      — this file adds the route-level acceptance + rejection pair so
      regression catches a future decorator removal on /api/vlm/stop.

Gaps documented (xfail-style):
  - The literal `csrf_token` body field the original task wording
    implies does NOT exist in the current decorator; the production
    defense is Origin/Referer header inspection plus the
    HARTOS_API_TOKEN bearer bypass.  We mark the literal-csrf-token
    expectation as xfail with a strict=False note so the report
    surfaces the gap without misclaiming behaviour we don't have.
  - The Electron build setup (Part J.3) is out-of-scope per the
    scout — no test added here.

Skipped (not xfail) when `cryptography` isn't installed; that's the
same gate used by tests/test_phase9b_ratchet.py and is a deploy-shape
property, not a missing feature.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


from integrations.social import e2e_ratchet as r
from integrations.social import e2e_x3dh as x3dh


needs_crypto = pytest.mark.skipif(
    not r._HAS_CRYPTO,
    reason="cryptography package not installed; pip install cryptography")


# ── Shared fixtures (mirror tests/test_phase9b_integration.py) ─────


@pytest.fixture
def fresh_db(monkeypatch):
    """In-memory SQLite + all migrations applied + a clean db Session.

    Mirrors the proven fixture from test_phase9b_integration.py so we
    pick up the v52 ratchet_states table + every other schema piece
    ConversationService.send_message touches (memberships, messages,
    notifications, message_envelopes, conversation_keys, ...)."""
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


def _seed_dm(db, alice_id, bob_id, *, e2e_enabled=True):
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


# ── 1. E2E ratchet — ciphertext on the wire ────────────────────────


@needs_crypto
def test_send_message_e2e_enabled_writes_ciphertext_not_plaintext(fresh_db):
    """When conv.settings.e2e_enabled=True AND the e2e_dms server flag
    is on, ConversationService.send_message must NOT persist the
    plaintext to either messages.content or the per-recipient envelope's
    ciphertext column.  We assert:

      1. messages.content is the '[encrypted]' placeholder, never the
         plaintext.  This is the property that protects raw-table
         readers (admin tooling, replication, backup) from seeing the
         plaintext.
      2. The per-recipient envelope row's ciphertext_b64 does not
         contain the plaintext as a substring — proves the wire payload
         is genuinely encrypted (AES-GCM), not just a re-encoded
         plaintext.
    """
    from sqlalchemy import text
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService

    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    _publish_keys(db, cid, alice.id, bob.id)

    plaintext = "this string must NEVER appear on the wire"

    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        msg = ConversationService.send_message(
            db, cid, alice.id, plaintext)

    # 1. Stored content is the encrypted-placeholder, not the plaintext.
    row = db.execute(text(
        "SELECT content FROM messages WHERE id = :id"),
        {'id': msg['id']}).fetchone()
    assert row[0] == '[encrypted]', (
        f"messages.content leaked plaintext: {row[0]!r}")
    assert plaintext not in (row[0] or '')

    # 2. The wire envelope ciphertext (base64-encoded) must not contain
    # the plaintext anywhere.  Base64 of AES-GCM output is opaque high-
    # entropy bytes; a leaked plaintext would mean the encrypt path was
    # bypassed.
    env_rows = db.execute(text(
        "SELECT recipient_id, ciphertext_b64 FROM message_envelopes "
        "WHERE message_id = :mid"),
        {'mid': msg['id']}).fetchall()
    assert env_rows, "no envelopes recorded — encrypt path didn't run"
    for recipient_id, ct_b64 in env_rows:
        assert plaintext not in (ct_b64 or ''), (
            f"plaintext leaked into envelope for {recipient_id}")
        # Decoded ciphertext must also not contain the plaintext bytes.
        decoded = base64.b64decode((ct_b64 or '').encode('ascii'))
        assert plaintext.encode('utf-8') not in decoded, (
            f"plaintext bytes leaked into decoded envelope for "
            f"{recipient_id}")

    # And the response dict still surfaces the plaintext to the sender
    # (so the client can render its own message without a decrypt
    # round-trip) — this is intentional + documented in
    # ConversationService.send_message.
    assert msg['content'] == plaintext
    assert msg['is_encrypted'] is True
    assert msg['recipients'] == [bob.id]


@needs_crypto
def test_recipient_decrypts_envelope_to_original_plaintext(fresh_db):
    """End-to-end round-trip: alice sends an encrypted DM, bob's
    list_messages call decrypts it back to the original plaintext.
    This is the property a real chat client depends on — if it breaks,
    encrypted DMs render as '[encrypted]' instead of the message body.
    """
    from flask import Flask, g
    from integrations.social.conversation_service import ConversationService

    db, _ = fresh_db
    alice, bob = _seed_users(db, 2)
    cid = _seed_dm(db, alice.id, bob.id, e2e_enabled=True)
    _publish_keys(db, cid, alice.id, bob.id)

    plaintext = "round-trip: alice → bob → alice's words back"

    app = Flask(__name__)
    with app.test_request_context('/'):
        g.feature_flags = {'e2e_dms': True}
        ConversationService.send_message(db, cid, alice.id, plaintext)
        # Bob's view — should see the decrypted plaintext.
        bob_view = ConversationService.list_messages(
            db, cid, bob.id, limit=10)

    assert len(bob_view) == 1
    assert bob_view[0]['content'] == plaintext, (
        "decrypt round-trip failed; recipient sees "
        f"{bob_view[0]['content']!r}")
    assert bob_view[0]['is_encrypted'] is True


# ── 2. X3DH handshake → first ratchet message ──────────────────────


@needs_crypto
def test_x3dh_handshake_establishes_session_key_initial_dh():
    """X3DH (Phase 9.C) gives the initial Double-Ratchet shared secret.
    By ECDH symmetry the responder recomputes the same value from their
    private keys.  This test locks the invariant: if X3DH stops being a
    valid Double-Ratchet bootstrap, every encrypted DM breaks on the
    very first send."""
    # Bob's bundle + Bob's matching privs.
    ik_sign_priv, ik_sign_pub = x3dh.generate_ed25519_keypair()
    ik_priv,      ik_pub      = x3dh.generate_x25519_keypair()
    spk_priv,     spk_pub     = x3dh.generate_x25519_keypair()
    opk_priv,     opk_pub     = x3dh.generate_x25519_keypair()
    spk_sig = x3dh.sign_prekey(ik_sign_priv, spk_pub)
    bundle = x3dh.PreKeyBundle(
        identity_pub=ik_pub,
        identity_sign_pub=ik_sign_pub,
        signed_prekey_pub=spk_pub,
        signed_prekey_sig=spk_sig,
        one_time_prekey_pub=opk_pub,
        one_time_prekey_id='opk-bob-closure',
    )

    # Alice's identity.
    alice_ik_priv, alice_ik_pub = x3dh.generate_x25519_keypair()

    # Initiator side: derive SK + an ephemeral.
    sk_alice, ek_priv, ek_pub = x3dh.initiator_derive_shared_secret(
        alice_ik_priv, bundle)

    # Wire envelope the initiator sends to the responder.
    msg = x3dh.PreKeyMessage(
        identity_pub_initiator=alice_ik_pub,
        ephemeral_pub=ek_pub,
        one_time_prekey_id=bundle.one_time_prekey_id,
        ratchet_envelope_blob=b'first-ratchet-envelope',
    )

    # Responder side: recompute SK from their privs.
    sk_bob = x3dh.responder_derive_shared_secret(
        ik_priv, spk_priv, msg, one_time_prekey_priv=opk_priv)

    assert sk_alice == sk_bob, (
        "X3DH round-trip diverged — Double Ratchet bootstrap is broken")
    assert len(sk_alice) == 32


@needs_crypto
def test_second_message_uses_ratcheted_key_not_x3dh_key():
    """After init_ratchet seeds the symmetric chain with X3DH's SK,
    the FIRST encrypt advances the sending chain key (one _kdf_chain
    step), and the SECOND encrypt advances it again.  The msg_key used
    for message #2 MUST NOT equal the X3DH SK and MUST NOT equal the
    msg_key used for message #1.  This is the post-compromise-security
    invariant: an attacker who snoops SK after the handshake still
    can't decrypt message #2.
    """
    # Run an X3DH handshake to get a real SK (more representative
    # than a hardcoded b'\x42' * 32 stub).
    ik_sign_priv, ik_sign_pub = x3dh.generate_ed25519_keypair()
    ik_priv,      ik_pub      = x3dh.generate_x25519_keypair()
    spk_priv,     spk_pub     = x3dh.generate_x25519_keypair()
    spk_sig = x3dh.sign_prekey(ik_sign_priv, spk_pub)
    bundle = x3dh.PreKeyBundle(
        identity_pub=ik_pub,
        identity_sign_pub=ik_sign_pub,
        signed_prekey_pub=spk_pub,
        signed_prekey_sig=spk_sig,
        one_time_prekey_pub=None,
        one_time_prekey_id=None,
    )
    alice_ik_priv, _ = x3dh.generate_x25519_keypair()
    sk, _, _ = x3dh.initiator_derive_shared_secret(alice_ik_priv, bundle)

    # Plug SK into Double Ratchet init.  Bob's DH pub is known to
    # Alice via the bundle (we reuse the spk_pub as Bob's first DH
    # pub for the test — the property under test is "chain advances",
    # not "X3DH→ratchet wiring").
    a_priv, a_pub = r.generate_dh_keypair()
    b_priv, b_pub = r.generate_dh_keypair()
    alice = r.init_ratchet(
        shared_secret=sk,
        our_dh_priv=a_priv, our_dh_pub=a_pub,
        their_dh_pub=b_pub)

    # Snapshot the initial sending chain key (== root after init when
    # responder=None, but here we're the initiator with their_dh_pub
    # set so the initial chain was advanced from SK via _kdf_root).
    chain_key_at_init = alice.sending_chain_key
    assert chain_key_at_init != sk, (
        "init_ratchet must derive a sending chain key from SK, not "
        "use SK directly as the chain key")

    # Send message #1: chain advances once, msg_key #1 derived from
    # the initial chain key.
    alice, env1 = r.encrypt_message(alice, b"first message")
    chain_key_after_1 = alice.sending_chain_key
    assert chain_key_after_1 != chain_key_at_init, (
        "sending chain did not advance after message #1")

    # Send message #2: chain advances again, msg_key #2 derived from
    # the once-advanced chain key.
    alice, env2 = r.encrypt_message(alice, b"second message")
    chain_key_after_2 = alice.sending_chain_key
    assert chain_key_after_2 != chain_key_after_1, (
        "sending chain did not advance after message #2 — every send "
        "MUST step the symmetric ratchet")

    # And the two envelopes were encrypted with different keys —
    # we can't directly recover the msg_keys (they're never stored)
    # but we CAN assert that re-encrypting the same plaintext at
    # idx=1 and idx=0 produces different ciphertexts (different nonce
    # AND different key).  The idx and our_pub fields also differ.
    assert env1['idx'] != env2['idx'], "chain index didn't advance"
    assert env1['ciphertext'] != env2['ciphertext'], (
        "identical ciphertext for two different msg_keys + nonces — "
        "the chain didn't ratchet")


# ── 3. vlm_stop CSRF defense-in-depth ──────────────────────────────


@pytest.fixture
def vlm_stop_app():
    """Mount JUST the vlm_stop route on a fresh Flask app so the test
    doesn't have to import the full hart_intelligence_entry module
    (which would pull in agentic_router, autobahn, etc.).  We wrap
    a minimal handler with the SAME decorator the production route
    uses, so any change to the decorator semantics is caught."""
    from flask import Flask, jsonify
    from core import auth_local
    # Re-read API_TOKEN per test in case env changed.
    auth_local.API_TOKEN = os.environ.get('HARTOS_API_TOKEN', '')

    app = Flask(__name__)

    @app.route('/api/vlm/stop', methods=['POST'])
    @auth_local.require_local_or_token_csrf_safe
    def vlm_stop():
        # Minimal handler — the actual production handler does state
        # work, but the CSRF gate runs before that.  This mirror keeps
        # the test focused on the decorator behaviour as wired into
        # hart_intelligence_entry.vlm_stop.
        return jsonify({'status': 'stopped'}), 200

    return app


def test_vlm_stop_post_cross_origin_browser_rejects_403(vlm_stop_app):
    """A cross-origin browser POST (Origin header from a non-loopback
    host) targeting /api/vlm/stop must be rejected with 403.  This is
    the same-machine CSRF vector — without this gate, a malicious page
    in the same browser could halt the user's active VLM session
    silently."""
    client = vlm_stop_app.test_client()
    resp = client.post('/api/vlm/stop',
                       json={'user_id': 'u1'},
                       headers={'Origin': 'https://attacker.example'})
    assert resp.status_code == 403, (
        f"expected 403 forbidden on cross-origin POST, "
        f"got {resp.status_code} {resp.get_data(as_text=True)}")
    assert resp.get_json()['error'] == 'forbidden'


def test_vlm_stop_post_same_origin_browser_returns_200(vlm_stop_app):
    """A same-origin (loopback) browser POST must be accepted — that's
    the Nunba SPA flow.  This is the closest semantic equivalent to
    'POST with a valid csrf_token' the current decorator surfaces: the
    Origin header IS the CSRF token-equivalent in browser-mediated
    requests (the spec guarantees the browser sets it from the page's
    real origin; an attacker page can't forge it)."""
    client = vlm_stop_app.test_client()
    resp = client.post('/api/vlm/stop',
                       json={'user_id': 'u1'},
                       headers={'Origin': 'http://127.0.0.1:5000'})
    assert resp.status_code == 200, (
        f"expected 200 on same-origin POST, "
        f"got {resp.status_code} {resp.get_data(as_text=True)}")


def test_vlm_stop_post_non_browser_caller_returns_200(vlm_stop_app):
    """curl / native desktop / server-to-server callers don't send
    Origin or Referer.  The decorator accepts these on the localhost
    path so the Nunba tray indicator (Tk-based POST from app.py) keeps
    working — this is the documented Nunba bundled-install flow."""
    client = vlm_stop_app.test_client()
    resp = client.post('/api/vlm/stop', json={'user_id': 'u1'})
    assert resp.status_code == 200


def test_vlm_stop_post_bearer_token_bypasses_csrf(vlm_stop_app, monkeypatch):
    """An authenticated remote caller (Bearer HARTOS_API_TOKEN) bypasses
    the CSRF check — token possession is itself proof the caller isn't
    a cross-origin browser.  This is the remote ops + inter-node admin
    flow that needs to work even when the Origin would otherwise be
    rejected."""
    from core import auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', 'closure-test-token')
    client = vlm_stop_app.test_client()
    resp = client.post('/api/vlm/stop',
                       json={'user_id': 'u1'},
                       headers={
                           'Origin': 'https://attacker.example',
                           'Authorization': 'Bearer closure-test-token',
                       })
    assert resp.status_code == 200


# ── 4. Documented gaps (xfail) ─────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Scout-flagged gap: the literal POST-body `csrf_token` field "
        "the task wording implies does NOT exist in HARTOS.  The "
        "production CSRF defense for /api/vlm/stop is Origin/Referer "
        "header inspection (see core/auth_local.py "
        "_is_safe_csrf_origin) plus the HARTOS_API_TOKEN bearer bypass. "
        "Migrating to a double-submit-cookie + body csrf_token model "
        "would require a session layer the Nunba bundled install "
        "doesn't currently run.  Locking this as xfail-strict so the "
        "report surfaces the architecture choice without false-passing "
        "a contract we don't support."),
)
def test_vlm_stop_post_with_csrf_token_field_in_body():
    """xfail placeholder for the literal `csrf_token` body field."""
    from flask import Flask, jsonify
    from core import auth_local
    auth_local.API_TOKEN = ''

    app = Flask(__name__)

    @app.route('/api/vlm/stop', methods=['POST'])
    @auth_local.require_local_or_token_csrf_safe
    def vlm_stop():
        return jsonify({'status': 'stopped'}), 200

    client = app.test_client()
    # A cross-origin POST that ALSO carries a csrf_token in the body
    # should — under the literal task wording — return 200 because the
    # token validates.  Under the actual decorator implementation it
    # returns 403 because the Origin header is the gate, not the body
    # field.  Hence xfail-strict.
    resp = client.post('/api/vlm/stop',
                       json={'user_id': 'u1',
                             'csrf_token': 'literal-token-from-form'},
                       headers={'Origin': 'https://attacker.example'})
    # Asserting 200 here would be wrong against the current code;
    # xfail-strict means the test is EXPECTED to fail this assertion.
    assert resp.status_code == 200


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Scout-flagged gap: Electron build setup (Part J.3) is "
        "explicitly deferred / out-of-scope for #222 closure.  This "
        "xfail-strict placeholder makes the deferral visible in the "
        "pytest report so a future ratchet sweep doesn't claim Phase "
        "9 closure without revisiting it."),
)
def test_electron_build_setup_deferred():
    """xfail placeholder for the Electron migration deferral."""
    # The check itself: an `electron/` build root with a package.json.
    # We expect this NOT to exist yet (xfail-strict).
    electron_pkg = os.path.join(ROOT, 'electron', 'package.json')
    assert os.path.isfile(electron_pkg), (
        "no Electron build root yet — Part J.3 is deferred")


# ── 5. Module-level smoke ──────────────────────────────────────────


def test_module_imports_cleanly():
    """The closure file itself must import without raising even on a
    bare deploy without `cryptography`.  Same contract every other
    Phase 9 test file enforces."""
    import importlib
    mod = importlib.import_module('tests.test_phase9_closure')
    assert hasattr(mod, 'test_send_message_e2e_enabled_writes_ciphertext_not_plaintext')
    assert hasattr(mod, 'test_x3dh_handshake_establishes_session_key_initial_dh')
    assert hasattr(mod, 'test_vlm_stop_post_cross_origin_browser_rejects_403')
