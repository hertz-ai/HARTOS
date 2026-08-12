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
    # imports the canonical audit + emit helpers from the service module
    assert re.search(r"from \.consent_service import .*\b_audit\b", body), \
        "UI revoke must use the canonical _audit helper"
    assert re.search(r"from \.consent_service import .*\b_emit\b", body), \
        "UI revoke must use the canonical _emit helper"
    # and actually fires the revoked audit + broadcast
    assert 'consent.revoked' in body, \
        "UI revoke must emit consent.revoked like the service path"


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


def test_emit_helper_is_best_effort_so_it_cannot_fail_the_revoke():
    """The added calls must never raise into the request path."""
    # emit swallows a broken event bus AND a broken notification path
    cs._emit('consent.revoked', {'user_id': '', 'consent_type': 'data_access',
                                 'scope': '*', 'agent_id': None})
    # _audit likewise degrades to the in-memory fallback and never raises
    cs._audit('consent', actor_id='u1', action='consent.revoked:data_access',
              detail={'scope': '*', 'agent_id': None})
