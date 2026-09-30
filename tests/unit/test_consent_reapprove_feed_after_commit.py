"""Approving a capability that is already granted restarts its feed after
the commit, never inside the open transaction.

record_capability_decision has two grant branches.  A new grant goes
through grant_consent, whose feed start waits for the commit
(models.after_commit).  An Approve on a grant that already stands writes no
row and only re-asserts the feed; that branch called the feed start inline.
The admin toggle's feed 'all' answers camera then screen in ONE session, so
when the camera is new and the screen already allowed, the camera INSERT is
flushed (SQLite's one write lock is taken) and the screen feed then started
inside that transaction: every other writer in the process got 'database is
locked' for as long as the feed start took.  Measured before this fix with a
real WAL file and a second writer: feed screen_capture True
('database is locked', 1).

Fixtures are the WAL database and effect recorder of
test_consent_grant_effects_after_commit, the file that pins the grant path.

    python -m pytest tests/unit/test_consent_reapprove_feed_after_commit.py -q
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.unit.test_consent_grant_effects_after_commit import (  # noqa: E402,F401
    UID, _other_writer, dbfile, effects)


def _grant_screen(factory):
    from integrations.social.consent_service import ConsentService
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        db.commit()
    finally:
        db.close()


def test_a_new_grant_then_a_reapproval_hold_no_lock_while_the_feeds_start(
        dbfile, effects):
    """The admin toggle's 'all': camera is new, screen already allowed."""
    from integrations.social.consent_service import ConsentService

    path, factory = dbfile
    _grant_screen(factory)
    effects['feed'].clear()
    effects['order'].clear()

    db = factory()
    try:
        assert ConsentService.record_capability_decision(
            db, UID, 'camera', True) == 'camera_capture'
        assert ConsentService.record_capability_decision(
            db, UID, 'screen', True) == 'screen_capture'
        assert effects['feed'] == [], 'a feed started before the commit'
        db.commit()
    finally:
        db.close()

    started = {t: (g, seen) for t, g, seen in effects['feed']}
    assert sorted(started) == ['camera_capture', 'screen_capture']
    for consent_type, (granted, (wrote, visible)) in started.items():
        assert granted is True
        assert wrote == 'ok', (
            f'{consent_type}: another writer was locked out: {wrote}')
        assert visible == 2, f'{consent_type}: started before the grant was on disk'
    # Each answer applies its feed once: one row for the camera, none added
    # for the screen, whose grant already stood.
    assert len(effects['feed']) == 2
    assert _other_writer(path)[1] == 2


def test_a_reapproval_restarts_the_feed_once_the_session_commits(dbfile, effects):
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    _grant_screen(factory)
    effects['feed'].clear()
    effects['emit'].clear()

    db = factory()
    try:
        ConsentService.record_capability_decision(db, UID, 'enable_screen', True)
        assert effects['feed'] == []
        db.commit()
    finally:
        db.close()

    assert [(t, g) for t, g, _ in effects['feed']] == [('screen_capture', True)]
    # No second row and no second consent.granted: the grant already stood.
    assert effects['emit'] == []


def test_a_reapproval_in_a_rolled_back_session_starts_nothing(dbfile, effects):
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    _grant_screen(factory)
    effects['feed'].clear()

    db = factory()
    try:
        ConsentService.record_capability_decision(db, UID, 'screen', True)
        db.rollback()
        db.commit()
    finally:
        db.close()

    assert effects['feed'] == []
