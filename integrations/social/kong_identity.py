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
back to believing the request.  The one other answer is KongBusy: Kong was
not asked at all (see below), so nothing is known about the token, and a
caller says "try again", never "not proven".

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
    not kept, so a Kong outage is never remembered as "not proven".  Asking
    Kong on every request instead cost every signed-in turn 3 Admin round
    trips, and a Kong outage right after a proven turn refused the turns
    the kept answer had served (review rv_5local of 224e10f3f).  A token
    revoked in Kong is taken for at most that minute, which is no wider
    than live: link-hevolve swaps the token for a HARTOS JWT that lives
    auth.TOKEN_EXPIRY (30 days), and revoking the Kong token does not end
    that JWT;
  - only a token Kong failed to answer is paced: asking about it again
    spends its own small budget (LIMITS['kong_lookup'], under its hash),
    and past that budget the answer is KongBusy.  A first ask, and a token
    Kong has answered, are never paced, and no budget is shared between
    tokens.  Shared budgets refused real logins: one per client address was
    one for every phone, since central's gateway names no client (docker's
    userland proxy hands HARTOS 172.21.0.1 for every peer,
    core.auth_local.client_address; hartos-73 via hevolve-66e6,
    2026-11-20), and one for the whole node let 61 distinct junk Bearers
    refuse the next phone's first login and held 80 first logins in a
    minute to 60 (review rv_5local of 787964a11).  A distinct junk token
    costs one Kong lookup, its no is kept, and it leaves no budget behind.
"""
import hashlib
import logging
import math
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

#: Tokens Kong could not answer on their last ask, by sha256 of the token.
#: Only a token here spends its own budget when asked about again; a
#: success or a definite no takes it out.
_unanswered = TTLCache(ttl_seconds=_ANSWER_SECONDS, max_size=4096,
                       name='kong_unanswered_tokens')

#: Asks a token's own budget paced since the last one was logged, so a
#: retry storm writes one line a minute rather than one per request.
_paced_report = {'logged_at': 0.0, 'count': 0}


class _KongUnsure(Exception):
    """Kong answered neither yes nor no (a status other than 200 or 404)."""


class KongBusy(Exception):
    """Kong was not asked about the token: it failed the token's earlier
    asks and the token's own budget is spent.  Nothing is known about the
    token, so a caller answers "busy, try again in ``retry_after``
    seconds", never "not proven".

    ``message`` is the one sentence both doors answer with (central's gate,
    link-hevolve) and ``code`` the one machine code beside it: installed
    apps show an answer's ``error`` as it is, so that field carries the
    sentence, never the code."""

    code = 'login_check_busy'

    def __init__(self, retry_after: int):
        super().__init__(f'Kong lookup paced; try again in {retry_after} s')
        self.retry_after = retry_after
        self.message = ('Your sign-in could not be checked just now: the '
                        'account service did not answer. Try again in '
                        f'{retry_after} seconds.')


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


def _note_paced() -> None:
    """Log an ask a token's budget paced, at most once a minute, with how
    many there were.  Pacing refuses no credential, so the line says
    pacing."""
    _paced_report['count'] += 1
    now = time.time()
    if now - _paced_report['logged_at'] < 60:
        return
    logger.warning("Kong token lookup paced %d time(s) in the last minute "
                   "(a token Kong failed to answer, asked again past its "
                   "own budget)", _paced_report['count'])
    _paced_report.update(logged_at=now, count=0)


def _may_ask_again(key: str) -> bool:
    """Whether Kong may be asked again now about the token hashed to
    ``key``, which it failed to answer last time: that token's own budget."""
    from .rate_limiter import LIMITS, get_limiter
    cfg = LIMITS['kong_lookup']
    if get_limiter().check(key, 'kong_lookup', cfg['max_tokens'],
                           cfg['refill_rate']):
        return True
    _note_paced()
    return False


def _retry_after() -> int:
    """Whole seconds until a spent token budget allows another ask."""
    from .rate_limiter import LIMITS
    rate = LIMITS['kong_lookup']['refill_rate']
    return max(1, math.ceil(1 / rate)) if rate > 0 else _ANSWER_SECONDS


def email_for_token(access_token: str) -> Optional[str]:
    """The email address of the Kong account behind ``access_token``,
    lowercased, or None when that cannot be proven.  Raises KongBusy when
    Kong was not asked, because it failed this token's earlier asks and the
    token's own budget is spent."""
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
    if key in _unanswered and not _may_ask_again(key):
        raise KongBusy(_retry_after())
    try:
        email, lease_end = _ask_kong(admin, access_token)
    except Exception as exc:
        # The exception's type only: a requests error can carry the URL, and
        # the URL carries the token.
        logger.warning('Kong token lookup failed: %s', type(exc).__name__)
        _unanswered[key] = True
        return None
    _unanswered.pop(key, None)
    _answers[key] = (email, min(time.time() + _ANSWER_SECONDS, lease_end))
    return email
