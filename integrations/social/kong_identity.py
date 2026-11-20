"""Whose Hevolve access token this is, asked of Kong itself.

A Hevolve login (Hevolve_Database /data/login + /data/varify_otp) hands the
client an opaque OAuth2 client_credentials token with no claims in it, so
HARTOS cannot read an identity out of the token.  Kong minted it, and Kong is
the only authority on whether it is still live and which credential minted
it.  Hevolve_Database registers every account as a Kong consumer whose
username is the account's email address, with the account's oauth2
credential under that consumer (sql/crud.py), and the login mints its token
against that credential (sql/otp.py).  So token -> credential -> consumer ->
username is the email address the token proves.

This mirrors Hevolve_Database's sql/kong_auth.py (client_id_for_token and
_is_live), restated here because HARTOS cannot import that repository.  Keep
the two in step: the path form, the echo check and the lease check are the
same rules, for the reasons recorded there.

Every failure is None, and a caller treats None as "not proven": the caller
mints HARTOS credentials, so a lookup that cannot reach Kong must never fall
back to believing the request.

Kong is asked as little as the answer allows.  Central's API gate asks this
for every Bearer its own JWT check refuses (security.middleware
_admit_hevolve_login), and a phone's turn passes that gate twice, the
phone's path and then /chat in the same process: measured by review rv_e97b
of f5e4358e2 and 0e8a7ef5a, a login cost 3 Kong Admin calls per request and
6 per phone turn, 5 junk Bearers cost 5 lookups, and nothing paced them.  So:
  - a token shaped like a JWT is never sent: Kong mints none (485 of 485
    access tokens in the Kong database export are 32 characters with no
    dot), and an expired HARTOS JWT in Kong's admin URL would reach its log;
  - Kong's definite answer about a token is kept for _ANSWER_SECONDS, never
    past the token's own lease, under a hash of the token; a failed call is
    not kept, so a Kong outage is never remembered as "not proven";
  - asks are paced by budgets in the social rate limiter, and none of them
    is per client address: central's gateway names no client (docker's
    userland proxy hands HARTOS 172.21.0.1 for every peer,
    core.auth_local.client_address), so one budget per address was one
    budget for every phone, and one caller's junk Bearers refused every
    Hevolve login (hartos-73 via hevolve-66e6, 2026-11-20).  Instead:
      * every token has its own small budget (LIMITS['kong_lookup'], under
        its hash).  Kong's answer is kept otherwise, so this only paces a
        token's retries while Kong cannot answer;
      * a token Kong has never proven also spends from one budget for the
        whole node (LIMITS['kong_new_token']), sized above the most tokens
        Kong has minted in a minute.  A token Kong proved is remembered
        while it is used (_proven) and never spends it again, so junk can
        delay only a first login, never a phone that has signed in before.
"""
import hashlib
import logging
import os
import time
from typing import Optional, Tuple
from urllib.parse import quote

import requests

from core.session_cache import TTLCache

logger = logging.getLogger('hevolve_social')

_TIMEOUT = 5

#: How long Kong's answer about one token is kept, in seconds.  A token Kong
#: revokes is still taken for at most this long.
_ANSWER_SECONDS = 60

#: Kong's answers by sha256 of the token: (email or None, wall-clock end).
#: The store's own TTL only bounds memory; _remembered checks the end.
_answers = TTLCache(ttl_seconds=_ANSWER_SECONDS, max_size=4096,
                    name='kong_token_answers')

#: How long a token Kong proved stays exempt from the node's new-token
#: budget after its last use, in seconds.  Hevolve's /chat tokens never
#: expire (expires_in 0 on all 485 in the Kong export), so a phone keeps
#: one token for as long as it stays signed in.
_PROVEN_SECONDS = 24 * 3600

#: Tokens Kong has said yes to, by sha256: the lease end.  Only Kong's yes
#: adds an entry and its no removes one, so junk can neither join nor push
#: a real token out.
_proven = TTLCache(ttl_seconds=_PROVEN_SECONDS, max_size=4096,
                   name='kong_proven_tokens')

#: Refusals since the last one was logged, so a flood writes one line a
#: minute rather than one per request.
_refusals = {'logged_at': 0.0, 'count': 0}


class _KongUnsure(Exception):
    """Kong answered neither yes nor no (a status other than 200 or 404)."""


def _admin_url() -> str:
    """Kong's Admin API, from KONG_ADMIN_URL and nowhere else.

    No default on purpose.  The port a default would name (8001 in
    integrations/gateway/kong_onboard.py) is a waitress app on deepbox, not
    Kong, and a node without Kong has nothing there to trust.  Unset means no
    email can be proven on this node, which is what a desktop is.
    """
    return (os.environ.get('KONG_ADMIN_URL') or '').strip().rstrip('/')


def _is_live(token_row) -> bool:
    """Kong keeps a token row past its lease, so the lease is checked here.
    expires_in 0 means the route never expires its tokens (Hevolve's /chat
    route is configured that way)."""
    try:
        expires_in = int(token_row.get('expires_in') or 0)
    except (TypeError, ValueError):
        return False
    if expires_in <= 0:
        return True
    try:
        created_at = int(token_row.get('created_at') or 0)
    except (TypeError, ValueError):
        return False
    if created_at <= 0:
        return False
    return created_at + expires_in > time.time()


def _admin_get(admin: str, path: str) -> Optional[dict]:
    """Kong's row at ``path``; None when Kong says there is none (404).
    Any other answer is _KongUnsure, so it is never remembered as a no."""
    resp = requests.get(f'{admin}{path}', timeout=_TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise _KongUnsure(resp.status_code)
    body = resp.json()
    return body if isinstance(body, dict) else None


def _lease_end(token_row) -> float:
    """When Kong's lease on the token ends, wall clock; inf for a token whose
    route never expires them (expires_in 0)."""
    try:
        expires_in = int(token_row.get('expires_in') or 0)
        created_at = int(token_row.get('created_at') or 0)
    except (TypeError, ValueError):
        return 0.0
    return float('inf') if expires_in <= 0 else float(created_at + expires_in)


def _ask_kong(admin: str, access_token: str) -> Tuple[Optional[str], float]:
    """(email or None, lease end) from Kong's three Admin rows; raises when
    Kong cannot answer."""
    # By PATH segment.  Kong 2.8 ignores ?access_token= and returns the
    # whole paginated list, so a query-form lookup reads row 0 and names an
    # unrelated account.
    token_row = _admin_get(
        admin, '/oauth2_tokens/' + quote(access_token, safe=''))
    # Only a row Kong echoes back as this very token, still in its lease.
    if not token_row or token_row.get('access_token') != access_token:
        return None, float('inf')
    if not _is_live(token_row):
        return None, float('inf')
    credential_id = (token_row.get('credential') or {}).get('id')
    if not credential_id:
        return None, float('inf')
    credential = _admin_get(
        admin, '/oauth2/' + quote(str(credential_id), safe=''))
    consumer_id = ((credential or {}).get('consumer') or {}).get('id')
    if not consumer_id:
        return None, float('inf')
    consumer = _admin_get(
        admin, '/consumers/' + quote(str(consumer_id), safe=''))
    username = ((consumer or {}).get('username') or '').strip().lower()
    return username or None, _lease_end(token_row)


def _remembered(key: str) -> Tuple[bool, Optional[str]]:
    entry = _answers.get(key)
    if entry and entry[1] > time.time():
        return True, entry[0]
    return False, None


def _was_proven(key: str) -> bool:
    lease_end = _proven.get(key)
    return lease_end is not None and lease_end > time.time()


def _note_refusal(budget: str) -> None:
    """Log a refused ask, at most once a minute, with how many there were."""
    _refusals['count'] += 1
    now = time.time()
    if now - _refusals['logged_at'] < 60:
        return
    logger.warning('Kong token lookup refused %d time(s) in the last minute '
                   '(%s budget spent)', _refusals['count'], budget)
    _refusals.update(logged_at=now, count=0)


def _may_ask_kong(key: str, proven: bool) -> bool:
    """Whether Kong may be asked about the token hashed to ``key`` now: the
    node's new-token budget unless Kong has proven the token before, then
    the token's own budget."""
    from .rate_limiter import LIMITS, get_limiter
    limiter = get_limiter()
    if not proven:
        cfg = LIMITS['kong_new_token']
        if not limiter.check('node', 'kong_new_token', cfg['max_tokens'],
                             cfg['refill_rate']):
            _note_refusal('new-token')
            return False
    cfg = LIMITS['kong_lookup']
    if not limiter.check(key, 'kong_lookup', cfg['max_tokens'],
                         cfg['refill_rate']):
        _note_refusal('per-token')
        return False
    return True


def email_for_token(access_token: str) -> Optional[str]:
    """The email address of the Kong account behind ``access_token``,
    lowercased, or None when that cannot be proven."""
    admin = _admin_url()
    if not access_token or not admin:
        return None
    if '.' in access_token:
        # A JWT (header.payload.signature).  Kong's tokens have no dot.
        return None
    key = hashlib.sha256(access_token.encode('utf-8')).hexdigest()
    known, email = _remembered(key)
    if known:
        return email
    if not _may_ask_kong(key, _was_proven(key)):
        return None
    try:
        email, lease_end = _ask_kong(admin, access_token)
    except Exception as exc:
        # The exception's type only: a requests error can carry the URL, and
        # the URL carries the token.
        logger.warning('Kong token lookup failed: %s', type(exc).__name__)
        return None
    _answers[key] = (email, min(time.time() + _ANSWER_SECONDS, lease_end))
    if email:
        _proven[key] = lease_end
    else:
        _proven.pop(key, None)
    return email
