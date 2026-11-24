"""
HevolveSocial - Authentication
JWT token generation, password hashing, and @require_auth decorator.
Uses security.jwt_manager for hardened token management.
"""
import hashlib
import hmac
import os
import sys
import stat
import secrets
import logging
import time
from functools import wraps

from flask import request, g, jsonify, has_app_context, has_request_context

try:
    import jwt as pyjwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False

logger = logging.getLogger('hevolve_social')

# Use hardened JWT manager when available
_jwt_manager = None

def _get_jwt_manager():
    global _jwt_manager
    if _jwt_manager is not None:
        return _jwt_manager
    if _jwt_manager is False:
        return None  # already tried and failed — don't log again
    try:
        from security.jwt_manager import JWTManager
        _jwt_manager = JWTManager()
        logger.info("Using hardened JWTManager")
        return _jwt_manager
    except Exception as e:
        logger.warning(f"JWTManager unavailable ({e}), using legacy JWT")
        _jwt_manager = False  # sentinel: don't retry
        return None

# Legacy fallback values - fail closed if not configured
SECRET_KEY = os.environ.get('SOCIAL_SECRET_KEY', '')
if not SECRET_KEY:
    # Auto-generate and persist so tokens survive restarts.
    # Stored next to the database file (writable user dir).
    def _load_or_create_secret_key():
        # Single-sourced with the JWTManager reader (core.platform_paths) so the
        # writer and reader never diverge on WHERE the key lives (#98e).
        from core.platform_paths import social_secret_key_write_target
        key_file = social_secret_key_write_target()
        try:
            if os.path.exists(key_file):
                with open(key_file, 'r') as f:
                    key = f.read().strip()
                if len(key) >= 32:
                    return key
            # Generate new key and persist
            key = secrets.token_hex(32)
            os.makedirs(os.path.dirname(key_file), exist_ok=True)
            with open(key_file, 'w') as f:
                f.write(key)
            # Restrict file permissions to owner read/write only (600)
            try:
                os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
            except (OSError, NotImplementedError):
                pass  # Windows doesn't support POSIX chmod the same way
            logger.info(f"Generated persistent secret key at {key_file}")
            return key
        except (PermissionError, OSError) as e:
            logger.warning(f"Cannot persist secret key ({e}), using ephemeral")
            return secrets.token_hex(32)
    SECRET_KEY = _load_or_create_secret_key()
    if not os.environ.get('SOCIAL_SECRET_KEY'):
        logger.info("SOCIAL_SECRET_KEY not set — using auto-generated persistent key")

TOKEN_EXPIRY = 30 * 24 * 3600  # 30 days — desktop app needs long-lived sessions


PBKDF2_ITERATIONS = 600_000  # OWASP 2023 minimum for PBKDF2-SHA256


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"{salt}:{hashed.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or ':' not in stored:
        return False
    salt, hashed = stored.split(':', 1)
    # Support both old (100K) and new (600K) iteration counts
    for iterations in (PBKDF2_ITERATIONS, 100_000):
        check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
        if hmac.compare_digest(check.hex(), hashed):
            return True
    return False


def generate_api_token() -> str:
    return secrets.token_urlsafe(64)


def generate_jwt(user_id: str, username: str, role: str = 'flat',
                 tenant_id: str = None, lane: str = None, rid: str = None) -> str:
    """Generate a LOCAL-scoped JWT (Layer 1). Works offline, survives kill switch.

    Optional `tenant_id` adds a 'tid' claim used by central-cloud
    deploys to scope all subsequent queries to that tenant.
    Flat / regional pass tenant_id=None — the claim is omitted and
    every downstream query treats the request as untenanted (NULL
    pass-through).  See plan Part E.1.

    Optional `lane` adds a 'lane' claim: the node's own word about the
    turn it mints this token for (core.request_lane reads it; only
    dispatch._internal_auth_headers passes it, for the metered API's
    self-POST on the customer's behalf), and `rid` the 'rid' claim that
    binds the word to that one turn's request id (task #157).  No login
    path sets either, so a person's own token never names a lane.  Both
    signers add the claims through security.jwt_manager.turn_claims, and
    a token carrying the lane word lives TURN_TOKEN_EXPIRY seconds
    (token_lifetime), not the session's TOKEN_EXPIRY.

    SECURITY: When the hardened JWTManager is available, ALL JWTs
    are issued through it — including tenant-scoped tokens, which
    are the most security-sensitive scope. Earlier revisions of
    this function bypassed JWTManager when tenant_id was set; that
    bypass is removed because it inverted the threat model
    (cloud tokens getting weaker hardening than flat tokens).
    JWTManager.generate_access_token now accepts tenant_id directly
    (security/jwt_manager.py).
    """
    mgr = _get_jwt_manager()
    if mgr:
        return mgr.generate_access_token(str(user_id), username,
                                         tenant_id=tenant_id, lane=lane, rid=rid)
    if HAS_JWT:
        import uuid
        from security.jwt_manager import token_lifetime, turn_claims
        now = int(time.time())
        payload = {
            'user_id': str(user_id),
            'username': username,
            'role': role or 'flat',
            'jti': str(uuid.uuid4()),
            'iat': now,
            'exp': now + token_lifetime(TOKEN_EXPIRY, lane),
            'type': 'access',
            'scope': 'local',
        }
        payload.update(turn_claims(tenant_id, lane, rid))
        return pyjwt.encode(payload, SECRET_KEY, algorithm='HS256')
    return generate_api_token()


def generate_jwt_with_tenant(user_id: str, username: str, role: str,
                             tenant_id: str) -> str:
    """Convenience wrapper for cloud signups — signs JWT with tid claim."""
    return generate_jwt(user_id, username, role, tenant_id=tenant_id)


def generate_hive_jwt(user_id: str, username: str, role: str = 'flat') -> str:
    """Generate a HIVE-scoped JWT (Layer 2). Ed25519-signed for cross-node verification.

    Dies with master key revocation (kill switch). Requires certificate chain.
    """
    mgr = _get_jwt_manager()
    if mgr:
        return mgr.generate_hive_token(str(user_id), username)
    # Fallback: if JWTManager unavailable, produce a local token
    # (hive features degrade gracefully)
    logger.warning("JWTManager unavailable, falling back to local token for hive request")
    return generate_jwt(user_id, username, role)


def verify_hive_jwt(token: str, issuer_public_key_hex: str) -> dict:
    """Verify a HIVE-scoped JWT from another node using its Ed25519 public key.

    Returns payload dict on success, empty dict on failure.

    TRUSTED KEY ONLY — NEVER a key from the request (#59).  ``issuer_public_key_hex``
    MUST be resolved from a trusted source (a peer's registered
    PeerNode.public_key by the claimed node_id, or a key that traces to the
    node's master trust anchor via the delegation chain), the way
    discovery._sender_signature_valid resolves it.
    Passing a caller-supplied key is self-certification: it always verifies and
    proves nothing.  That was the admin-takeover in the /api/social/auth/
    sync-user route (#59: a caller signed with its own key, sent that key as
    ``node_public_key``, and created a role-'central' user); the route was
    deleted on 10-06, nothing ever called it.  This function has NO production
    caller (grep: only tests); a new route MUST NOT reuse the key-as-parameter
    shape — resolve the key by identity first.
    """
    mgr = _get_jwt_manager()
    if mgr:
        result = mgr.verify_hive_token(token, issuer_public_key_hex)
        return result or {}
    return {}


#: verify_device_jwt's word for a phone whose token names no account: it can
#: never be allowed as one (#306), and every shipped app names one, so the
#: phone is told to update (security.middleware words it; a PeerLink HELLO
#: is refused with it as its reason).
UPDATE_APP = 'update_app'
UPDATE_APP_WORDS = "Update the app to connect to this desktop."
#: The reason beside 'pending' when the phone's grant names no account (it
#: was allowed before v60): the owner is asked again, never bound on first
#: use (#306).
REALLOW = 'reallow'
#: The reason beside 'pending' when the allowed phone's token names another
#: account than the one it was allowed as: the owner is asked whether the
#: phone may switch, and until they allow it the phone is its allowed
#: account only (rv_2c18).
SWITCH = 'switch'
#: The reason beside 'denied' when the owner said no to that switch.
SWITCH_DENIED = 'switch_denied'
#: The reason beside 'denied' when the owner said no to confirming a phone
#: allowed before v60 as the account it is signed in as (its grant stays on
#: file: taking the phone away is a revoke).
REALLOW_DENIED = 'reallow_denied'


def device_token_user(claims):
    """The account a phone's token names, as text, or None when it names
    none (no user_id, None or ''): str(None) reads "None", a name any caller
    can send, so a token with no user is held as no user."""
    user = (claims or {}).get('user_id')
    return None if user in (None, '') else str(user)


def verify_device_jwt(db, token: str, owner_id: str) -> dict:
    """Verify a hive-shaped token from a person's phone against the key the
    desktop owner allowed for it (#111).

    The phone signs the same token nodes exchange (scope 'hive', node_sig
    over the canonical payload, verify_hive_jwt) with its own PeerLink
    Ed25519 key, and carries that key's hex in a ``node_public_key`` claim.
    The claim is read ONLY to find the row: the key on file is the owner's
    GRANTED ``device_access`` consent whose scope is that key
    (consent_service.device_scope), the row the owner wrote with "Always
    allow" on the ask.  The signature is verified against the key read back
    from that row (ConsentService.active_grant, an exact-scope lookup), so
    the claim can select a row and nothing more, and a blanket '*' grant
    admits no device.

    Before any ask is filed the token must verify against the key it
    CLAIMS: that proves the caller holds that key, so nobody can file asks
    in the name of another phone's key, and an ask always names a key its
    sender can answer for.  (Holding a key is not identity; only the
    owner's grant is.)

    The phone is admitted as ONE account: the user_id its token named when
    it asked, which the grant keeps (paired_user_id, #306; owner
    2026-11-23, a phone allowed on a desktop is a trusted user of it as its
    own account).  The phone writes that claim itself, so a token naming
    any other account, the owner's included, is never admitted however
    well it is signed: the owner is asked whether the phone may switch to
    it ('pending', reason SWITCH, rv_2c18), and until they allow it the
    phone is its allowed account only; a no stands ('denied', reason
    SWITCH_DENIED).  The key alone never un-pairs the account it is
    allowed as.  A grant from before v60 names no account: the owner is
    asked to confirm the phone as the account it says ('pending', reason
    REALLOW; a no: 'denied', REALLOW_DENIED), never bound to what it
    claims.  A token naming no account can never be allowed as one
    (UPDATE_APP).

    Returns ``{'status': 'ok', 'payload': ..., 'public_key': ...}``,
    ``{'status': 'pending', 'public_key': ..., 'claims': ...}`` (no grant
    yet, REALLOW or SWITCH: the caller files the ask), ``{'status':
    'denied', 'public_key': ...}`` (the owner said no; to a switch:
    SWITCH_DENIED), ``{'status': UPDATE_APP, 'public_key': ...}``, or
    ``{'status': 'invalid'}`` (not a device token, or the signature does
    not verify).
    """
    from .consent_service import ConsentService, device_scope
    if not HAS_JWT or not token or not owner_id:
        return {'status': 'invalid'}
    try:
        claims = pyjwt.decode(token, options={'verify_signature': False,
                                              'verify_exp': False},
                              algorithms=['HS256'])
    except Exception:
        return {'status': 'invalid'}
    scope = device_scope(claims.get('node_public_key'))
    if scope is None or claims.get('scope') != 'hive':
        return {'status': 'invalid'}
    row = ConsentService.active_grant(db, owner_id, 'device_access',
                                      scope=scope, agent_id=None)
    if row is None:
        claimed_key = scope[len('device:'):]
        if not verify_hive_jwt(token, claimed_key):
            return {'status': 'invalid'}
        declined = ConsentService.declined(db, owner_id, 'device_access',
                                           scope=scope, agent_id=None)
        if declined:
            return {'status': 'denied', 'public_key': claimed_key,
                    'claims': claims}
        if device_token_user(claims) is None:
            return {'status': UPDATE_APP, 'public_key': claimed_key}
        return {'status': 'pending', 'public_key': claimed_key,
                'claims': claims}
    granted_key = row.scope[len('device:'):]
    payload = verify_hive_jwt(token, granted_key)
    if not payload:
        logger.warning("device token for granted key %s... did not verify",
                       granted_key[:16])
        return {'status': 'invalid'}
    signed_as = device_token_user(payload)
    if signed_as is None:
        return {'status': UPDATE_APP, 'public_key': granted_key}
    bound = row.paired_user_id
    if bound is None or signed_as != str(bound):
        # Allowed before v60 (no account on file: confirm it), or allowed
        # as another account (a switch): asked about, never bound here.
        asked, said_no = ((REALLOW, REALLOW_DENIED) if bound is None
                          else (SWITCH, SWITCH_DENIED))
        if ConsentService.device_ask_declined(db, owner_id, scope, signed_as):
            logger.info("device %s... (allowed as %s): the owner said no to it "
                        "as %s", granted_key[:16], bound or 'no account',
                        signed_as)
            return {'status': 'denied', 'reason': said_no,
                    'public_key': granted_key}
        logger.warning("device %s... is allowed as %s and signed as %s; not "
                       "admitted, the owner is asked (%s)", granted_key[:16],
                       bound or 'no account', signed_as, asked)
        return {'status': 'pending', 'reason': asked,
                'public_key': granted_key, 'claims': payload}
    return {'status': 'ok', 'payload': payload, 'public_key': granted_key}


def file_device_access_ask(db, owner_id: str, public_key: str, claims: dict) -> None:
    """File the one canonical owner-consent ask for a proven phone key.

    Both the HTTP gate and PeerLink admission call this only after
    ``verify_device_jwt`` has verified proof of possession.  Keeping the
    wording, scope, record, and realtime fanout here prevents the two
    transports from drifting into separate device-trust flows.

    The ask keeps the account the phone's token names (paired_user_id), the
    one the owner's yes admits it as (#306), and its card says which: each
    account the phone asks as has its own ask, so a card always names the
    account its yes would admit.  A phone already allowed as another
    account asks to switch (rv_2c18): its card says it is that same phone
    (its name and key) moving from the account it is allowed as, and that
    a yes ends that account on the phone; while it waits it is not sent
    again.  A token naming no account files nothing: verify_device_jwt
    answers it UPDATE_APP first.  A grant from before v60 stands without an
    account: the card says the phone was allowed before and asks the owner
    to confirm it as the account it says (requester_reconfirm); its ask is
    sent again, re-opened (request_consent), so a phone that keeps
    reconnecting still shows the owner one card.  The key's fingerprint
    rides beside the words (requester_fingerprint), shown once by the card.
    """
    from .consent_service import ConsentService, device_scope

    account = device_token_user(claims)
    if account is None:
        logger.warning("device %s... asked with no account in its token; no "
                       "ask filed (%s)", str(public_key)[:16], UPDATE_APP)
        return
    scope = device_scope(public_key)
    bound = ConsentService.active_grant(db, owner_id, 'device_access',
                                        scope=scope, agent_id=None)
    switch_from = (bound.paired_user_id if bound is not None
                   and bound.paired_user_id not in (None, account) else None)
    reconfirm = bound is not None and bound.paired_user_id is None
    name = ' '.join(str(claims.get('username') or '').split())[:100]
    ConsentService.request_consent(
        db, owner_id, 'device_access', scope=scope,
        reason=_device_ask_reason(db, owner_id, name, account,
                                  switch_from, reconfirm),
        requester_name=name, paired_user_id=account, switch_from=switch_from,
        reconfirm=reconfirm)


def _device_ask_reason(db, owner_id: str, name: str, account: str,
                       switch_from=None, reconfirm=False) -> str:
    """What the owner's card says about a phone's ask: the name the phone
    gives itself, then the account it says it is signed in as (#306); for
    a phone allowed as ``switch_from``, that it is that phone switching,
    and what a yes ends; for one allowed before v60 (``reconfirm``), that
    it was allowed before and is to be confirmed (rv_2c18).  The key's
    fingerprint is not in the words: the card shows it once, beside them."""
    # The name is the phone's own claim: say so, never state it as fact.
    named = f' (it says its name is "{name}")' if name else ''
    if switch_from is not None:
        was = _account_words(db, owner_id, switch_from)
        return (f"This phone{named} is allowed on this computer as {was}. "
                "It now says it is signed in as "
                f"{_account_words(db, owner_id, account)} and asks to switch. "
                "Allow only if this is your own phone: once allowed, "
                f"{was} stops working on this phone.")
    if reconfirm:
        return (f"This phone{named} was allowed on this computer before. It "
                "says it is signed in as "
                f"{_account_words(db, owner_id, account)}; confirm it as that "
                "account. Allow only if this is your own phone.")
    known = _account_words(db, owner_id, account, known_only=True)
    if known:
        claim = (f"This phone says it is signed in as {known}. "
                 "Allow only if this is your own phone.")
    else:
        claim = (f"This phone says it is signed in as account {account}. "
                 "Allow it only if the phone is yours.")
    return f"A phone{named} wants to use this computer's agents. {claim}"


def _account_words(db, owner_id: str, account: str, known_only=False):
    """An account as a card names it: "<name> (<id>)" for one this desktop
    knows (a user here, or its owner), else "account <id>", never a name
    the phone supplied (#306).  ``known_only``: '' for one it does not
    know."""
    from .models import User
    user = db.query(User).filter(User.id == account).first()
    known = ' '.join(str(getattr(user, 'display_name', '') or
                         getattr(user, 'username', '') or '').split())[:100]
    if not known and account == str(owner_id):
        known = "this computer's owner"
    if known:
        return f"{known} ({account})"
    return '' if known_only else f"account {account}"


def allowed_device_owner(token: str):
    """This desktop's owner, when ``token`` is from a phone they allowed and
    signed as them; else None.  Never files an ask (the API gate does, on
    the phone's first call).

    The person-ask door's answerer for a phone
    (integrations.agent_engine.person_ask.answer_from_request).  The token
    is checked here by verify_device_jwt, the gate's own check, and not read
    off the gate's verdict on flask.g: Nunba runs this node's routes
    in-process through test_client(), which carries the Authorization header
    and nothing the gate set on g, so on a desktop the verdict never reached
    the door (review of ec468c821, C1).

    The phone is admitted as the account it was allowed as
    (verify_device_jwt, #306), so it speaks for the owner only when the
    owner allowed it signed in as the owner, on a card that said so.  A
    phone allowed as any other account names nobody here (review of
    ec468c821, C2), and one minting the owner's id is refused by the
    verdict itself.
    """
    owner = (os.environ.get('HEVOLVE_OWNER_USER_ID') or '').strip()
    if not token or not owner:
        return None
    from .models import db_session
    try:
        with db_session(commit=False) as db:
            verdict = verify_device_jwt(db, token, owner)
    except Exception:
        logger.warning("device token check failed; it names nobody", exc_info=True)
        return None
    if verdict.get('status') != 'ok':
        return None
    signed_as = str((verdict.get('payload') or {}).get('user_id') or '')
    if signed_as != owner:
        logger.warning("device %s... signed as user %r, not this desktop's owner; "
                       "it names nobody", verdict['public_key'][:16], signed_as)
        return None
    return owner

def generate_token_pair(user_id: str, username: str, role: str = 'flat') -> dict:
    """Generate access + refresh token pair."""
    mgr = _get_jwt_manager()
    if mgr:
        return mgr.generate_token_pair(str(user_id), username)
    return {
        'access_token': generate_jwt(user_id, username, role),
        'refresh_token': generate_api_token(),
        'token_type': 'bearer',
        'expires_in': TOKEN_EXPIRY,
    }


def decode_jwt(token: str) -> dict:
    """Decode a JWT token (any scope). Backward compatible.

    Returns payload dict with 'scope' key ('local' for pre-upgrade tokens
    without scope, or the actual scope value). Empty dict on failure.
    """
    mgr = _get_jwt_manager()
    if mgr:
        result = mgr.decode_token(token, expected_type='access')
        if result:
            # Ensure scope is always present for callers
            result.setdefault('scope', 'local')
        return result or {}
    if HAS_JWT:
        try:
            payload = pyjwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            if payload.get('type') not in ('access', None):
                return {}  # Reject non-access tokens (e.g. refresh tokens)
            payload.setdefault('scope', 'local')
            return payload
        except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
            return {}
    return {}


def revoke_token(token: str):
    """Revoke a JWT token (add to blocklist)."""
    mgr = _get_jwt_manager()
    if mgr:
        mgr.revoke_token(token)


def _note_refusal(reason: str) -> None:
    """Why _get_user_from_token is answering None for a credential it was
    given, left on g for the caller that turns that None into the request's
    refusal (log_refused_credential reads it)."""
    if has_app_context():
        g.token_refusal = reason


def user_refusal(user):
    """Why ``user`` (a User row, or None for no account) may not act on this
    node, or None when they may: 'no_user', 'banned'.

    The ONE "may this person act here" rule.  Every door that admits a
    person by their account, or mints them a token for one, asks it: the
    Bearer doors through _get_user_from_token (JWT, api_token and the Kong
    fallback), user_id_for_token, the steering caller (dashboard_service
    .steering_caller), the phone's device-link door
    (phone_call._person_refusal), central's gate for a phone's Hevolve
    login (security.middleware._admit_hevolve_login), and the five
    token-minting doors, the password login (services.UserService.login),
    the Hevolve link (api.link_hevolve), a guest's device re-registration
    (api.guest_register, existing device_id), a guest's recovery code
    (api.guest_recover) and an external bot's re-registration
    (external_bot_bridge.ExternalBotRegistry.register_bot).  A refusal
    added here reaches them all; each door keeps its own reply shape, and
    one that shows the person why words it through user_refusal_words.
    Before, each door re-read is_banned for itself and the phone door ran
    its own SQL, so a refusal added to one never reached the others
    (parallel-path audit 2026-10-27, item 4).

    Doors that do NOT ask, by design, because they never read the account:
    the API gate (security.middleware._require_api_key_or_bearer) and POST
    /chat admit a JWT by its decoded payload until it expires, and
    /auth/refresh (api.refresh_token) re-mints from a refresh token through
    JWTManager alone.  A ban reaches those holders when their token expires
    or is revoked (revoke_token), not before; whether they should read the
    row on every turn is the owner's call (a users-row read per turn).  The
    create paths of guest_register and link_hevolve make a new account, so
    there is nothing to ask about yet.
    Tenancy (require_auth's cloud-mode check) is a door's own concern and
    stays there; a listing that filters banned users out of search results
    is a different question (who is shown), not this one (who may act).
    """
    if user is None:
        return 'no_user'
    if getattr(user, 'is_banned', False):
        return 'banned'
    return None


def user_refusal_words(why) -> str:
    """The rule's answer as said to a person: its word with the underscores
    read as spaces ('no_user' -> 'no user'), so a door that shows why ("Account
    banned", "Account is on leave") composes its reply through one reading and
    a mechanism token never reaches them."""
    return str(why or '').replace('_', ' ')


def _get_user_from_token(token: str):
    """Look up user by API token or JWT (local or hive scope).

    For hive tokens from other nodes: the HS256 decode will fail (different
    secret), so we fall through to the API token lookup. Cross-node hive
    verification should use verify_hive_jwt() explicitly in the endpoint.

    Sets Flask g.token_scope to 'local', 'hive', or 'api_token' for callers.
    Also sets g.token_tenant_id to the JWT 'tid' claim (None for legacy
    tokens or API-token auth — flat/regional pass-through).
    """
    from .models import get_db, User

    # Try JWT first (works for local tokens and hive tokens issued by THIS node)
    payload = decode_jwt(token)
    if payload and 'user_id' in payload:
        db = get_db()
        try:
            user = db.query(User).filter(User.id == payload['user_id']).first()
            if user_refusal(user) is None:
                try:
                    g.token_scope = payload.get('scope', 'local')
                    g.token_node_id = payload.get('node_id', '')
                    g.token_tenant_id = payload.get('tid')
                except RuntimeError:
                    pass  # Outside request context (testing)
                return user, db
        finally:
            pass  # keep session open for request lifecycle
        _note_refusal('jwt_user_banned' if user else 'jwt_user_missing')
        return None, db

    # Fall back to raw API token lookup
    db = get_db()
    # 2026-06-20: skip the api_token query when token is empty so a
    # User row with NULL/empty api_token doesn't falsely match the
    # Kong-stripped-header case (where we route through here with
    # token='').  Empty-token requests should reach the Kong fallback
    # below instead of accidentally authenticating as someone else.
    user = None
    if token:
        user = db.query(User).filter(User.api_token == token).first()
    if user_refusal(user) is None:
        try:
            g.token_scope = 'api_token'
            g.token_node_id = ''
            g.token_tenant_id = None  # API tokens are pre-tenancy
        except RuntimeError:
            pass
        return user, db

    # ── Kong gateway fallback (2026-06-20) ────────────────────────────
    # If both JWT decode + api_token lookup failed AND the request was
    # pre-authenticated by Kong, trust Kong's identity headers as a
    # last resort.  This unblocks the cloud central tier where Kong is
    # the single source of truth for auth and HARTOS would otherwise
    # reject every request because the JWT was signed by Kong's key
    # (not HARTOS's SECRET_KEY).
    #
    # Standard Kong key-auth / jwt plugin sets:
    #   X-Consumer-Custom-ID  : external user identifier (email/uuid)
    #   X-Consumer-Username   : human-readable username
    #   X-Anonymous-Consumer  : "true" if no credentials were provided
    #
    # Only honored when HEVOLVE_TRUST_KONG=true (off by default — flat
    # deployments don't want the Kong header path enabled).  In cloud
    # mode where Kong fronts every request, set HEVOLVE_TRUST_KONG=true
    # in the HARTOS env.
    if os.environ.get('HEVOLVE_TRUST_KONG', '').lower() == 'true':
        try:
            is_anon = (request.headers.get('X-Anonymous-Consumer', '')
                       .lower() == 'true')
            if not is_anon:
                kong_custom_id = request.headers.get('X-Consumer-Custom-ID', '')
                kong_username = request.headers.get('X-Consumer-Username', '')
                kong_user = None
                # Prefer custom_id (typically the user's stable external id).
                if kong_custom_id:
                    # custom_id may be the email, uuid, or username depending
                    # on how the Kong consumer was provisioned — try all three.
                    kong_user = (
                        db.query(User).filter(User.email == kong_custom_id).first()
                        or db.query(User).filter(User.id == kong_custom_id).first()
                        or db.query(User).filter(User.username == kong_custom_id).first()
                    )
                if kong_user is None and kong_username:
                    kong_user = (
                        db.query(User).filter(User.username == kong_username).first()
                        or db.query(User).filter(User.email == kong_username).first()
                    )
                if user_refusal(kong_user) is None:
                    try:
                        g.token_scope = 'kong'
                        g.token_node_id = ''
                        # Kong does not currently propagate tenant — flat for now.
                        g.token_tenant_id = None
                    except RuntimeError:
                        pass
                    logger.info(
                        f"Kong fallback auth: user={kong_user.username} "
                        f"custom_id={kong_custom_id} username={kong_username}"
                    )
                    return kong_user, db
        except Exception as e:
            # Kong fallback must never raise — auth still 401s the same way.
            logger.warning(f"Kong fallback auth failed: {e}")
    if token:
        _note_refusal('api_token_user_banned' if user else 'unknown_token')
    return None, db


def user_id_for_token(token):
    """The user id a bearer token belongs to, or None.

    The public form of _get_user_from_token, for callers that need only the
    id and are not decorated routes (an SSE stream, a sync endpoint in the
    desktop app).  Accepts everything require_auth accepts: a local JWT, a
    hive JWT from this node, and a stored api_token.  The last is how a cloud
    login works on a desktop: the login sync stores the Kong-issued token as
    the user's api_token.  Decoding the token as a JWT instead rejects every
    cloud login (measured 2026-09-25: /agents/sync 401 while /api/social/
    auth/me answered 200 for the same token).  The DB session is closed here.
    """
    if not token:
        return None
    user, db = _get_user_from_token(token)
    try:
        if user_refusal(user) is not None:
            return None
        return str(user.id)
    finally:
        if db is not None:
            db.close()


def email_never_proven(user) -> bool:
    """True when nobody proved ``user``'s email, so the address may not
    stand for the person who owns it.

    /auth/register is the only door that makes an account with a password,
    and it takes the email as typed, proving none of it; the door that
    proves an email (user_for_hevolve_token, from Kong) makes its accounts
    with no password, and a row's email cannot change after it is made.  So
    a password beside an email means the email was never proven.  The ONE
    rule for every place that lets an email stand for its owner: a Hevolve
    login (user_for_hevolve_token) and central's recipe store, whose prompt
    list names an agent's owner by email
    (hart_intelligence_entry._sync_account_email)."""
    return bool(getattr(user, 'password_hash', None))


def user_for_hevolve_token(db, token, email=None, name='', phone_number=''):
    """The account a Hevolve login token proves, found or created: ``(user,
    created)``, or ``(None, False)`` when nothing is proven.

    A Hevolve OTP login hands the client an opaque Kong OAuth2 token; Kong is
    asked which account minted it (kong_identity.email_for_token), and only
    that account's email is used.  ``email``, when given, must be the one the
    token proves (link-hevolve passes the body's email).  The account is
    found by email, the one field the legacy `user` table and User share, or
    created under it.  The ONE place this question is answered: link-hevolve
    mints a JWT on it, and central's API gate admits a phone's turn on it
    (security.middleware), so the two can never disagree about who a token
    is.  Whether the account may act is the caller's question (user_refusal).
    kong_identity.KongBusy passes through: Kong was not asked, so nothing
    is proven or refused, and each caller answers it in its own shape.

    An account whose email was never proven (email_never_proven) is never
    the answer: binding the login Kong proves for that email to it would
    hand a stranger who registered someone's email first that person's
    turns (review rv_e97b of f5e4358e2).  Such a login is refused, never
    bound and never given a second account under the same email.
    """
    import re
    from .kong_identity import email_for_token
    from .models import User

    proven = email_for_token(token) if token else None
    if not proven or (email is not None and email != proven):
        return None, False
    user = db.query(User).filter(User.email == proven).first()
    if user and email_never_proven(user):
        # WARNING, not INFO: central logs nothing below it after boot, and
        # this is the trace of an email held by an unproven account.
        logger.warning("Hevolve login refused: its email belongs to a password "
                       "account (user %s), whose email was never proven",
                       user.id)
        return None, False
    if user:
        return user, False
    base = re.sub(r'[^a-z0-9_]', '', (name or phone_number or proven.split('@')[0])
                  .strip().lower().replace(' ', '_')) or 'user'
    username = base
    while db.query(User).filter(User.username == username).first():
        username = f"{base}_{secrets.token_hex(3)}"
    user = User(
        username=username,
        display_name=name or username,
        email=proven,
        user_type='human',
        api_token=generate_api_token(),
        is_verified=True,
    )
    db.add(user)
    db.flush()
    db.commit()
    return user, True


def log_refused_credential(token=None, reason=None) -> None:
    """Say, once, that a request is refused because no way in accepted the
    credential it sent: where, why, and the credential's shape, never the
    credential itself (owner rule H-0711: an error is seen in the log).

    Called where a caller turns _get_user_from_token's None into the
    request's refusal.  A caller for which None is not a refusal (a loopback
    caller that falls back to the desktop's owner, an optional login) does
    not call it, so a request that goes on to succeed logs nothing.
    ``reason`` defaults to the one _get_user_from_token left on g.  A
    request that sent no credential was not refused one, and logs nothing.
    ``token`` defaults to the Bearer the request sent, for a refusal decided
    where the token is not at hand (approver_from_request's callers, the API
    gate's final answer).
    """
    if token is None and has_request_context():
        header = request.headers.get('Authorization', '')
        token = header[7:].strip() if header.startswith('Bearer ') else ''
    if not token:
        return
    where = 'a call outside a request'
    if has_request_context():
        from core.auth_local import client_key
        where = f'{request.method} {request.path} from {client_key()}'
        reason = reason or g.get('token_refusal')
    logger.warning(
        "Refused the credential sent to %s (%s; %d segment(s), %d chars)",
        where, reason or 'unknown', token.count('.') + 1, len(token))


def _sign_in_required(error: str):
    """require_auth's 401: no token, or one that no longer verifies.

    ``needs_sign_in`` is the structured answer a client keys on; the
    ``error`` prose is for people and logs and may change.  Nunba's
    steerOutcome matched the prose and would silently show a raw sentence
    the day it drifted (hart_intelligence_entry already words its own 401
    'Invalid or expired token.', with a period).
    """
    return jsonify({'success': False, 'error': error,
                    'needs_sign_in': True}), 401


def require_auth(f):
    """Decorator: requires valid Bearer token. Sets g.user and g.db.

    Phase 7a: also sets g.tenant_id (from JWT 'tid' claim) and
    g.feature_flags. Cloud deployments enforce that 'tid' is present
    when HEVOLVE_CLOUD_MODE=true; flat/regional pass through with
    g.tenant_id=None which downstream code treats as untenanted
    (NULL match in tenant_id-scoped queries — see plan Part E.1).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        # 2026-06-20: when Kong strips the upstream Authorization header
        # after its own pre-auth, HARTOS sees no Bearer but still gets
        # Kong's identity headers.  Route through _get_user_from_token
        # with an empty token so the Kong fallback path inside it runs.
        # The Kong fallback itself is gated on HEVOLVE_TRUST_KONG=true,
        # so flat deployments without Kong continue to reject the
        # missing-header case as before.
        token = ''
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
        elif os.environ.get('HEVOLVE_TRUST_KONG', '').lower() != 'true':
            return _sign_in_required('Missing or invalid Authorization header')

        user, db = _get_user_from_token(token)
        if user is None:
            if db:
                db.close()
            log_refused_credential(token)
            return _sign_in_required('Invalid or expired token')

        g.user = user
        g.user_id = str(user.id)
        g.db = db
        # Tenant + feature flags. _get_user_from_token sets
        # g.token_tenant_id; we promote it to g.tenant_id for
        # downstream code. In cloud mode, missing tid is fatal.
        tenant_id = getattr(g, 'token_tenant_id', None)
        if os.environ.get('HEVOLVE_CLOUD_MODE', '').lower() == 'true' and not tenant_id:
            db.close()
            return jsonify({
                'success': False,
                'error': 'Tenant required (cloud mode)',
            }), 403
        g.tenant_id = tenant_id
        try:
            from .feature_flags import get_flags_for_tenant
            g.feature_flags = get_flags_for_tenant(db, tenant_id)
        except ImportError:
            g.feature_flags = {}
        try:
            result = f(*args, **kwargs)
            db.commit()
            return result
        except Exception as e:
            if db.is_active:
                db.rollback()
            raise
        finally:
            db.close()

    return decorated


def optional_auth(f):
    """Decorator: attaches user if token present, but doesn't require it.

    Phase 7a: sets g.tenant_id and g.feature_flags identically to
    require_auth, with the addition that anonymous (no-token)
    requests get tenant_id=None (untenanted) and flags from the
    public default set — used by listing/discovery routes that
    don't need a logged-in user but should still respect tenancy
    when a JWT happens to be presented.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            user, db = _get_user_from_token(token)
            g.user = user
            g.user_id = str(user.id) if user else None
            g.db = db
        else:
            from .models import get_db
            g.user = None
            g.user_id = None
            g.db = get_db()

        g.tenant_id = getattr(g, 'token_tenant_id', None)
        try:
            from .feature_flags import get_flags_for_tenant
            g.feature_flags = get_flags_for_tenant(g.db, g.tenant_id)
        except ImportError:
            g.feature_flags = {}

        try:
            result = f(*args, **kwargs)
            g.db.commit()
            return result
        except Exception as e:
            g.db.rollback()
            raise
        finally:
            g.db.close()

    return decorated


def require_local_or_auth(f):
    """Decorator: loopback callers pass as they are; anyone else needs a user token.

    For routes a local client calls without a session that every HARTOS node
    must ALSO be able to serve to the network. The bundled desktop's own SPA has
    always called the book routes from 127.0.0.1 with no Authorization header;
    once any node serves them, a remote caller must be a known user, scoped to
    their own data. So:

      * local  -> g.user = g.user_id = None; the route decides what a local
                  request may name (a desktop is single-user).
      * remote -> require_auth: g.user / g.user_id identify the caller.

    Composes the two canonical gates -- core.auth_local._is_local_request (the
    same loopback test, TRUSTED_PROXY included, that require_local_or_token
    uses) and require_auth -- rather than re-implementing either.
    """
    remote = require_auth(f)

    @wraps(f)
    def decorated(*args, **kwargs):
        from core.auth_local import _is_local_request
        if _is_local_request():
            g.user = None
            g.user_id = None
            return f(*args, **kwargs)
        return remote(*args, **kwargs)

    return decorated


# The User.role strings that confer elevated authority.  require_admin honors
# 'central' (below), require_moderator honors 'regional'/'central'; the extra
# 'admin'/'moderator' are kept as a conservative superset so a role STRING of
# that name is never treated as an ordinary profile field.  ONE definition,
# imported by sync_engine's role-strip (#59/#65) so the set a sync may set or
# preserve can never drift from the set the authority checks honor.  A new
# privileged role added here is stripped from syncs automatically.
PRIVILEGED_ROLES = frozenset({'central', 'regional', 'admin', 'moderator'})


def require_admin(f):
    """Decorator: requires central (cloud admin) role or is_admin flag."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if not holds_central_role(g.user):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


def require_moderator(f):
    """Decorator: requires regional/central role, or is_admin/is_moderator flag."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        user_role = getattr(g.user, 'role', None) or 'flat'
        if not (g.user.is_admin or g.user.is_moderator or user_role in ('regional', 'central')):
            return jsonify({'success': False, 'error': 'Moderator access required'}), 403
        return f(*args, **kwargs)
    return decorated


def holds_central_role(user) -> bool:
    """True when ``user`` holds the central (steward) account role: role
    'central', or the is_admin flag UserService.set_user_role keeps in step
    with it.  The ONE check require_central applies, and the one
    voting_rules.is_steward builds on; None is never central."""
    if user is None:
        return False
    return ((getattr(user, 'role', None) or 'flat') == 'central'
            or bool(getattr(user, 'is_admin', False)))


def require_central(f):
    """Decorator: requires central (cloud admin) role."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if not holds_central_role(g.user):
            return jsonify({'success': False, 'error': 'Central access required'}), 403
        return f(*args, **kwargs)
    return decorated


def require_regional(f):
    """Decorator: requires regional or central role."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        user_role = getattr(g.user, 'role', None) or 'flat'
        if user_role not in ('central', 'regional') and not (g.user.is_admin or g.user.is_moderator):
            return jsonify({'success': False, 'error': 'Regional access required'}), 403
        return f(*args, **kwargs)
    return decorated
