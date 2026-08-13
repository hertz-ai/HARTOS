"""Parallel-path fix #4 (revoke half): the JWT consent UI surface
(``consent_api.revoke_consent``) flipped ``revoked_at`` on its own append-only
row but — unlike the canonical ``ConsentService.revoke_consent`` — wrote NO
immutable-audit entry and emitted NO ``consent.revoked`` event. A UI revoke thus
left no compliance trail and never reached WAMP/SSE listeners or the user's other
devices, drifting from the service path.

The UI surface deliberately keeps its OWN row model (append-only: ``granted``
stays True, ``revoked_at`` is the tombstone), which is NOT the service's
``granted=False`` + agent_id-keyed model — so delegating the WRITE would corrupt
the append-only audit trail. What must not drift are the observable SIDE EFFECTS:
this pins that the UI revoke now routes its audit + broadcast through the SAME
canonical helpers the service uses, while still NOT delegating the write.
"""
import re
from pathlib import Path

from integrations.social import consent_service as cs


def _revoke_body() -> str:
    src = (Path(cs.__file__).resolve().parent
           / 'consent_api.py').read_text(encoding='utf-8')
    m = re.search(r"def revoke_consent\(\):.*?(?=\n@|\ndef |\Z)", src, re.DOTALL)
    assert m, "revoke_consent not found"
    return m.group(0)


def test_ui_revoke_routes_side_effects_through_canonical_helpers():
    body = _revoke_body()
    # fires the side effects through the service's PUBLIC entry point, NOT by
    # reaching into its underscore-private _audit/_emit (that left parity
    # "maintained by the caller remembering to do it" — review feedback).
    assert 'ConsentService.announce_revocation(' in body, \
        "UI revoke must call the public ConsentService.announce_revocation"
    assert not re.search(r"import .*\b_audit\b", body) and \
        not re.search(r"import .*\b_emit\b", body), \
        "UI revoke must NOT import the private _audit/_emit symbols"


def test_ui_revoke_keeps_its_append_only_row_model():
    body = _revoke_body()
    # still the tombstone write (unchanged behaviour)
    assert 'revoked_at' in body, "UI revoke must still set revoked_at"
    # must NOT adopt the service's granted=False model (would break append-only)
    assert not re.search(r"\.granted\s*=\s*False", body), \
        "UI revoke must not flip granted=False — that corrupts the append-only trail"
    # must NOT delegate the WRITE to the service (row models are incompatible).
    # Check for an actual CALL (open paren) so a comment that merely NAMES the
    # service method for context does not trip this.
    assert 'ConsentService.revoke_consent(' not in body, \
        "UI revoke keeps its own row selection; only side effects are shared"


def test_announce_revocation_is_public_and_best_effort():
    """The public entry point fires the audit + broadcast and never raises into
    the request path — so any caller (service or UI) gets identical, structural
    parity without touching module privates."""
    assert callable(getattr(cs.ConsentService, 'announce_revocation', None)), \
        "announce_revocation must be a public ConsentService method"
    # must not raise even with a degenerate/empty payload (event bus + audit are
    # both best-effort under the hood)
    cs.ConsentService.announce_revocation('', 'data_access', '*', None)


def test_service_revoke_routes_side_effects_through_the_public_method():
    """ConsentService.revoke_consent must fire the side effects via
    announce_revocation, so the two writers share ONE emit path structurally."""
    import re as _re
    import inspect
    src = inspect.getsource(cs.ConsentService.revoke_consent)
    assert 'announce_revocation(' in src, \
        "service revoke must go through announce_revocation, not inline _audit/_emit"
