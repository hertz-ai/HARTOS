"""integrations/commerce/signing.py -- the MAC contract McGroce's Tomcat
implements too.

The vectors below are the cross-repo contract: McGroce's HartosSigning test
carries the same inputs and outputs, so a change on either side that breaks
the other fails here or there.  They were also recomputed with Node's crypto
from the docstring alone (no HARTOS code) when they were written.
"""
import base64
import hashlib
import hmac
import json

import pytest

from integrations.commerce import signing as s

SECRET = 'test-secret-not-real'
NONCE = '0123456789abcdef0123456789abcdef'
TS = 1700000000
BODY = b'{"customerId":4242}'

REQUEST_VECTOR = '1a34141a3d0c921f5357bf68f97ffb6a3aee2f3cde8de3361e8686a3b1670bb6'
RESPONSE_VECTOR = 'ec94c6641e8f5c9acc52f1de3c2e94df407a30e8b1940f174962900f3608f63c'
CLAIMS = {'v': 1, 'mid': 'mdt_1', 'pid': 'pay_1', 'cid': '4242', 'amt': '240.00',
          'cur': 'INR', 'st': 'consumed', 'iat': TS, 'exp': TS + 600,
          'gw': 'phonepe', 'txn': 'T1'}
ATTESTATION_VECTOR = (
    'eyJhbXQiOiIyNDAuMDAiLCJjaWQiOiI0MjQyIiwiY3VyIjoiSU5SIiwiZXhwIjoxNzAwMDAwNjAw'
    'LCJndyI6InBob25lcGUiLCJpYXQiOjE3MDAwMDAwMDAsIm1pZCI6Im1kdF8xIiwicGlkIjoicGF5'
    'XzEiLCJzdCI6ImNvbnN1bWVkIiwidHhuIjoiVDEiLCJ2IjoxfQ'
    '.acb97837d1ee6ad63b168db3aaf4ba895f6902393b2390806630c54a7f110c02')


def _headers(sig, ts=TS, nonce=NONCE):
    return {s.TIMESTAMP_HEADER: str(ts), s.NONCE_HEADER: nonce,
            s.SIGNATURE_HEADER: sig}


class TestVectors:
    def test_request_signature(self):
        assert s.request_signature(SECRET, 'POST', '/api/commerce/session', TS,
                                   NONCE, BODY) == REQUEST_VECTOR

    def test_response_signature(self):
        assert s.response_signature(SECRET, NONCE, b'{"success":true}') == RESPONSE_VECTOR

    def test_mandate_attestation(self):
        assert s.mandate_attestation(SECRET, CLAIMS) == ATTESTATION_VECTOR

    def test_attestation_is_the_documented_encoding(self):
        b64, mac = ATTESTATION_VECTOR.split('.')
        claims = json.loads(base64.urlsafe_b64decode(b64 + '=' * (-len(b64) % 4)))
        assert claims == CLAIMS
        key = hmac.new(SECRET.encode(), s.MANDATE_KEY_LABEL, hashlib.sha256).digest()
        assert hmac.new(key, b64.encode(), hashlib.sha256).hexdigest() == mac

    def test_each_purpose_has_its_own_key(self):
        # A request MAC can never pass as an attestation MAC or back.
        assert (s.derive_key(SECRET, s.REQUEST_KEY_LABEL)
                != s.derive_key(SECRET, s.MANDATE_KEY_LABEL))
        assert s.derive_key(SECRET, s.REQUEST_KEY_LABEL) != SECRET.encode()


def _fresh():
    """A nonce memory that began before the vectors' timestamp."""
    return s.NonceCache(not_before=0)


class TestVerifyRequest:
    def _verify(self, headers, body=BODY, now=TS, route='/api/commerce/session',
                method='POST', nonces=None):
        return s.verify_request(SECRET, method, route, headers, body, now=now,
                                nonces=nonces or _fresh())

    def test_good_signature_passes_once(self):
        nonces = _fresh()
        ok, why, nonce = self._verify(_headers(REQUEST_VECTOR), nonces=nonces)
        assert (ok, why, nonce) == (True, 'ok', NONCE)
        again = self._verify(_headers(REQUEST_VECTOR), nonces=nonces)
        assert again == (False, 'nonce already used', None)

    @pytest.mark.parametrize('change', [
        {'body': b'{"customerId":4243}'},          # another customer
        {'route': '/api/commerce/mandates/x'},      # another route
        {'method': 'PUT'},
    ])
    def test_any_change_to_what_was_signed_fails(self, change):
        ok, why, _ = self._verify(_headers(REQUEST_VECTOR), **change)
        assert not ok and why == 'bad signature'

    def test_wrong_key_fails(self):
        sig = s.request_signature('another-secret', 'POST', '/api/commerce/session',
                                  TS, NONCE, BODY)
        assert self._verify(_headers(sig))[:2] == (False, 'bad signature')

    @pytest.mark.parametrize('skew', [-301, 301])
    def test_stale_or_future_timestamp_fails(self, skew):
        ok, why, _ = self._verify(_headers(REQUEST_VECTOR), now=TS + skew)
        assert not ok and 'window' in why

    def test_timestamp_inside_the_window_passes(self):
        assert self._verify(_headers(REQUEST_VECTOR), now=TS + 299)[0]

    @pytest.mark.parametrize('headers', [
        {},
        {s.TIMESTAMP_HEADER: 'soon', s.NONCE_HEADER: NONCE, s.SIGNATURE_HEADER: REQUEST_VECTOR},
        {s.TIMESTAMP_HEADER: str(TS), s.NONCE_HEADER: 'short', s.SIGNATURE_HEADER: REQUEST_VECTOR},
        {'X-Commerce-Secret': SECRET},              # the old plain-secret header
    ])
    def test_missing_or_malformed_headers_fail(self, headers):
        assert self._verify(headers)[0] is False

    def test_a_bad_mac_does_not_use_up_the_nonce(self):
        nonces = _fresh()
        assert not self._verify(_headers('0' * 64), nonces=nonces)[0]
        assert self._verify(_headers(REQUEST_VECTOR), nonces=nonces)[0]

    def test_a_request_signed_before_this_process_started_is_refused(self):
        # A restart empties the nonce memory; without this rule a request
        # captured just before it could be replayed just after.
        restarted = s.NonceCache(not_before=TS + 1)
        ok, why, _ = self._verify(_headers(REQUEST_VECTOR), now=TS + 2,
                                  nonces=restarted)
        assert not ok and 'before this process started' in why


class TestNonceCache:
    def test_a_nonce_is_forgotten_only_after_its_ttl(self):
        c = s.NonceCache(ttl_s=10)
        assert c.claim('a' * 32, now=100)
        assert not c.claim('a' * 32, now=109)
        assert c.claim('a' * 32, now=111)

    def test_a_full_cache_refuses_instead_of_forgetting_a_live_nonce(self):
        c = s.NonceCache(ttl_s=10, max_size=2)
        assert c.claim('a' * 32, now=0) and c.claim('b' * 32, now=0)
        assert not c.claim('c' * 32, now=5)       # both still live
        assert not c.claim('a' * 32, now=5)       # and a replay stays refused
        assert c.claim('c' * 32, now=11)          # expired ones make room
