"""
Signed messages between McGroce's Tomcat and HARTOS -- the one place the MACs
on both directions are built.

The shared secret (``COMMERCE_SESSION_SECRET`` here, ``hartos.commerce.secret``
on McGroce) never travels.  Each purpose has its own key derived from it, so
a MAC made for one purpose is never accepted for another:

  request key  HMAC-SHA256(secret, "mcgroce-hartos/request/v1")
               McGroce signs POST /api/commerce/session; HARTOS signs its
               answer, bound to the request's nonce.
  mandate key  HMAC-SHA256(secret, "mcgroce-hartos/mandate/v1")
               HARTOS signs the attestation that it took the money for a
               mandate and consumed it; McGroce checks it before it places
               the order (POST /api/v1/cart/checkout/ap2).

Request   X-Commerce-Timestamp  unix seconds
          X-Commerce-Nonce      32-64 lowercase hex, never reused
          X-Commerce-Signature  hex HMAC(request key, METHOD "\\n" ROUTE "\\n"
                                TIMESTAMP "\\n" NONCE "\\n" hex sha256(body))
          ROUTE is the route as HARTOS declares it ("/api/commerce/session"),
          not the URL a proxy may have prefixed.
Answer    X-Commerce-Signature  hex HMAC(request key, "RESP" "\\n" NONCE "\\n"
                                hex sha256(answer body))
Mandate   X-Commerce-Mandate    B64 "." hex HMAC(mandate key, B64), where B64
                                is the unpadded base64url of a compact JSON
                                object (mandate_attestation's docstring).

A request is refused when its timestamp is more than SIGNATURE_WINDOW_S away
from this clock, when its MAC is wrong, or when its nonce was already seen.
The nonce memory is per process, like the bindings and mandates this module
guards (COMMERCE_MCGROCE_FOLLOWUPS.md item 10: run the commerce gateway as
one process); a request signed before this process started is refused too,
so a restart, which empties that memory, never reopens a replay.

tests/unit/test_commerce_signing.py pins fixed vectors for all three; McGroce's
HartosSigning test carries the same vectors.
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from typing import Any, Dict, Mapping, Optional, Tuple

REQUEST_KEY_LABEL = b'mcgroce-hartos/request/v1'
MANDATE_KEY_LABEL = b'mcgroce-hartos/mandate/v1'

TIMESTAMP_HEADER = 'X-Commerce-Timestamp'
NONCE_HEADER = 'X-Commerce-Nonce'
SIGNATURE_HEADER = 'X-Commerce-Signature'
MANDATE_HEADER = 'X-Commerce-Mandate'

#: How far a request's timestamp may sit from this clock, either way.
SIGNATURE_WINDOW_S = 300
#: A nonce is remembered for longer than any timestamp it could pass with.
NONCE_TTL_S = 2 * SIGNATURE_WINDOW_S
#: How long McGroce accepts an attestation after HARTOS signed it.  It is
#: minted right before the one call that uses it.
ATTESTATION_TTL_S = 120

_NONCE_RE = re.compile(r'^[0-9a-f]{32,64}$')
_MAX_NONCES = 50_000


def derive_key(secret: str, label: bytes) -> bytes:
    return hmac.new(secret.encode('utf-8'), label, hashlib.sha256).digest()


def _mac(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode('utf-8'), hashlib.sha256).hexdigest()


def request_signature(secret: str, method: str, route: str, timestamp,
                      nonce: str, body: bytes) -> str:
    return _mac(derive_key(secret, REQUEST_KEY_LABEL), '\n'.join((
        method.upper(), route, str(timestamp), nonce,
        hashlib.sha256(body or b'').hexdigest())))


def response_signature(secret: str, nonce: str, body: bytes) -> str:
    return _mac(derive_key(secret, REQUEST_KEY_LABEL), '\n'.join((
        'RESP', nonce, hashlib.sha256(body or b'').hexdigest())))


class NonceCache:
    """Nonces seen within NONCE_TTL_S, since ``not_before`` (when this
    memory began: a request signed earlier may have been seen by a process
    that is gone).  Thread-safe."""

    def __init__(self, ttl_s: int = NONCE_TTL_S, max_size: int = _MAX_NONCES,
                 not_before: Optional[float] = None):
        self.ttl_s = ttl_s
        self.max_size = max_size
        self.not_before = int(time.time() if not_before is None else not_before)
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, nonce: str, now: float) -> bool:
        """True the first time ``nonce`` is offered; False on a replay, or
        when the memory is full of live nonces (refusing beats forgetting
        one that could then be replayed)."""
        with self._lock:
            if len(self._seen) >= self.max_size:
                self._seen = {n: t for n, t in self._seen.items() if t > now}
            if nonce in self._seen and self._seen[nonce] > now:
                return False
            if len(self._seen) >= self.max_size:
                return False
            self._seen[nonce] = now + self.ttl_s
            return True


_nonces = NonceCache()


def verify_request(secret: str, method: str, route: str,
                   headers: Mapping[str, str], body: bytes,
                   now: Optional[float] = None,
                   nonces: Optional[NonceCache] = None
                   ) -> Tuple[bool, str, Optional[str]]:
    """(ok, reason, nonce) for a request McGroce signed.

    The MAC is checked before the nonce is remembered, so a caller without
    the key cannot use up nonces.
    """
    now = time.time() if now is None else now
    nonces = nonces or _nonces
    nonce = str(headers.get(NONCE_HEADER) or '').strip().lower()
    given = str(headers.get(SIGNATURE_HEADER) or '').strip().lower()
    try:
        timestamp = int(str(headers.get(TIMESTAMP_HEADER) or '').strip())
    except ValueError:
        return False, 'no timestamp', None
    if not _NONCE_RE.match(nonce):
        return False, 'no nonce', None
    if abs(now - timestamp) > SIGNATURE_WINDOW_S:
        return False, 'timestamp outside the signing window', None
    if timestamp < nonces.not_before:
        return False, 'signed before this process started', None
    expected = request_signature(secret, method, route, timestamp, nonce, body)
    if not hmac.compare_digest(given.encode('ascii', 'replace'),
                               expected.encode('ascii')):
        return False, 'bad signature', None
    if not nonces.claim(nonce, now):
        return False, 'nonce already used', None
    return True, 'ok', nonce


def mandate_attestation(secret: str, claims: Dict[str, Any]) -> str:
    """The X-Commerce-Mandate value for a consumed mandate.

    ``claims``: v (1), mid (mandate id), pid (payment id), cid (the McGroce
    customer id, a string), oid (the McGroce cart's order id, a string),
    lines (sorted [sku id string, quantity, unit price 2-decimal string]),
    amt (the total as a 2-decimal string), cur (ISO currency), st
    ("consumed"), iat / exp (unix seconds), gw (gateway) and txn (the
    gateway's transaction id; either may be null).  Encoded as
    compact JSON with sorted keys; the MAC covers the base64url text exactly
    as sent, so McGroce never re-serialises before checking it.
    """
    raw = json.dumps(claims, separators=(',', ':'), sort_keys=True,
                     ensure_ascii=True).encode('utf-8')
    b64 = base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
    return f'{b64}.{_mac(derive_key(secret, MANDATE_KEY_LABEL), b64)}'
