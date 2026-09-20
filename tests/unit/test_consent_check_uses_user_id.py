"""The advisory consent check must ask about a USER, not about a session.

lifecycle_hooks keys its per-action bookkeeping on ``user_prompt``, the session
key ``f"{user_id}_{prompt_id}"``. ConsentService.check_consent's parameter is a
user id and it matches against ``user_consents.user_id``, which the consent UI
fills with bare uuids. Passing the session key therefore made the advisory
branch UNSATISFIABLE: it printed 415 "No data_access consent" lines across
Sep 18-19 on this box while a granted ``data_access`` row for the owner sat in
the table the whole time, and no grant could ever have silenced it because a
string ending in ``_<prompt_id>`` cannot equal a bare uuid.

Harmless while the check is advisory. The day anyone makes it blocking it
becomes a deny-everything gate, which is why it is pinned rather than just
fixed.
"""
import pytest

from hartos import lifecycle_hooks as lh


USER = 'd68c9dee-b324-4c04-86c4-1205a836957f'
SESSION = USER + '_4421288717'


class _FakeDB:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured(monkeypatch):
    """Drive the real set_action_state and capture the consent subject."""
    seen = []

    class _FakeConsent:
        @staticmethod
        def check_consent(db, user_id, consent_type, **kw):
            seen.append((user_id, consent_type))
            return False          # force the advisory log path

    import sys
    import types
    svc = types.ModuleType('integrations.social.consent_service')
    svc.ConsentService = _FakeConsent
    models = types.ModuleType('integrations.social.models')
    models.db_session = lambda commit=False: _FakeDB()
    monkeypatch.setitem(sys.modules, 'integrations.social.consent_service', svc)
    monkeypatch.setitem(sys.modules, 'integrations.social.models', models)
    return seen


def test_advisory_consent_check_receives_the_user_not_the_session(captured):
    """The subject handed to check_consent is the bare user id."""
    lh.set_action_state(SESSION, 1, lh.ActionState.IN_PROGRESS, reason='pin')

    assert captured, (
        "the advisory consent check did not run on IN_PROGRESS; if that block "
        "moved, move this pin with it")
    subject, ctype = captured[0]
    assert ctype == 'data_access'
    assert subject == USER, (
        "check_consent was asked about %r. Its parameter is a user id and it "
        "looks up user_consents.user_id, so a session key can never match and "
        "the branch becomes unsatisfiable." % (subject,))
    assert '_' not in subject.rsplit('-', 1)[-1], (
        "subject still carries a session suffix: %r" % (subject,))


def test_extract_ownership_is_the_one_parser():
    """The fix reuses the helper already in this module, not a second parse."""
    assert lh._extract_ownership_from_prompt(SESSION) == (USER, '4421288717')
    # A bare user id with no session suffix survives unchanged, so a caller
    # that already passes a user id is not mangled into something shorter.
    assert lh._extract_ownership_from_prompt(USER)[0] == USER
