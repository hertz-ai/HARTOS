"""The canonical redactor must not let a secret-assignment slip through in clear.

When audit_log's redaction was collapsed onto the one canonical
``security.secret_redactor``, the delete of audit_log's own generic catch-all
(``(password|...|api_key|apikey)\\s*[=:]\\s*\\S+``) left a hole: every vendor
pattern in the canonical set needs a recognisable prefix or a 32+ char body, so a
short or odd-shaped value after a secret keyword reached the audit log — the one
artifact whose whole purpose is to be safe to keep and hand over. The
``heroku_key`` narrowing (anchoring on a heroku context so identifier UUIDs stay
visible) widened the same hole for ``api_key=<uuid>`` shapes.

A ``secret_assignment`` catch-all closes both at their single home. This pins:
  * secret-ish key = ANY value (short, uuid-shaped, unrecognised) is redacted;
  * a bare identifier UUID with no secret key in front stays visible (the
    "which node failed" diagnostic the heroku fix restored must survive).
"""
from security.secret_redactor import redact_secrets


_UUID = '46329c87-cbb6-4ca1-bad5-816f6007b6a0'

# Every one of these leaked through the canonical set before the fix.
_MUST_REDACT = [
    'api_key=abcd1234efgh',
    'apikey: 9f8e7d6c',
    'secret=hunter2',
    'token=shortvalue',            # bearer_token needs 32+ chars; this is shorter
    f'api_key={_UUID}',            # heroku narrowing let this through
    f'secret: {_UUID}',
    'access_key=xyz123',
    'auth_token=q1w2e3',
]

# These must stay in clear text — they are diagnostics, not secrets.
_MUST_STAY_VISIBLE = [
    f'sync: FAILED TO SIGN batch for node_id={_UUID} — sending UNSIGNED 403',
    f'hierarchy_sync: NO peer row resolves node_id={_UUID}',
    f'request_id={_UUID} trace complete',
]


def test_secret_assignments_never_reach_the_log_in_clear():
    for case in _MUST_REDACT:
        out, _ = redact_secrets(case)
        assert 'REDACTED' in out and 'hunter2' not in out and 'shortvalue' not in out, \
            f'secret leaked through the canonical redactor: {case!r} -> {out!r}'


def test_identifier_uuids_stay_visible():
    for case in _MUST_STAY_VISIBLE:
        out, _ = redact_secrets(case)
        assert _UUID in out, \
            f'identifier UUID was redacted — kills the diagnostic: {case!r} -> {out!r}'
