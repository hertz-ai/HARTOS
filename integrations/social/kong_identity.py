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
"""
import logging
import os
import time
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger('hevolve_social')

_TIMEOUT = 5


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
    resp = requests.get(f'{admin}{path}', timeout=_TIMEOUT)
    if resp.status_code != 200:
        return None
    body = resp.json()
    return body if isinstance(body, dict) else None


def email_for_token(access_token: str) -> Optional[str]:
    """The email address of the Kong account behind ``access_token``,
    lowercased, or None when that cannot be proven."""
    admin = _admin_url()
    if not access_token or not admin:
        return None
    try:
        # By PATH segment.  Kong 2.8 ignores ?access_token= and returns the
        # whole paginated list, so a query-form lookup reads row 0 and names
        # an unrelated account.
        token_row = _admin_get(
            admin, '/oauth2_tokens/' + quote(access_token, safe=''))
        # Only a row Kong echoes back as this very token, still in its lease.
        if not token_row or token_row.get('access_token') != access_token:
            return None
        if not _is_live(token_row):
            return None
        credential_id = (token_row.get('credential') or {}).get('id')
        if not credential_id:
            return None
        credential = _admin_get(
            admin, '/oauth2/' + quote(str(credential_id), safe=''))
        consumer_id = ((credential or {}).get('consumer') or {}).get('id')
        if not consumer_id:
            return None
        consumer = _admin_get(
            admin, '/consumers/' + quote(str(consumer_id), safe=''))
        username = ((consumer or {}).get('username') or '').strip().lower()
        return username or None
    except Exception as exc:
        # The exception's type only: a requests error can carry the URL, and
        # the URL carries the token.
        logger.warning('Kong token lookup failed: %s', type(exc).__name__)
        return None
