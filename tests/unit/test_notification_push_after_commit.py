"""A notification's live push goes out only for a row that committed.

NotificationService.create / mark_read / mark_all_read / mark_dismissed each
used to hang an ``event.listen(db, 'after_commit', fn, once=True)`` on the
session.  A once-listener stays on the session until SOME commit fires it, so
after a rollback (or a close without a commit) the next, unrelated commit of
the same session pushed a notification whose row was never written.  Measured
at 66a0648ee: create, rollback, commit -> 0 rows on disk, 1 push.

They now go through the one after-commit queue beside db_session
(integrations.social.models.after_commit), which the consent grant uses too
and which drops its queue when the outer transaction ends uncommitted.

    python -m pytest tests/unit/test_notification_push_after_commit.py -q
"""
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def factory(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import Notification

    engine = create_engine(f"sqlite:///{tmp_path / 'notif.db'}")
    Notification.__table__.create(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def pushed(monkeypatch):
    """What reached the live pipe (realtime is the boundary)."""
    import integrations.social.realtime as rt

    sent = []
    monkeypatch.setattr(rt, 'on_notification',
                        lambda uid, d: sent.append(('new', uid, dict(d))))
    monkeypatch.setattr(rt, 'on_notification_read',
                        lambda uid, ids: sent.append(('read', uid, list(ids))))
    return sent


def _rows(db):
    from integrations.social.models import Notification
    return db.query(Notification).count()


def test_a_committed_notification_is_pushed_once(factory, pushed):
    from integrations.social.services import NotificationService

    db = factory()
    try:
        n = NotificationService.create(db, 'u1', 'comment',
                                       source_user_id='u2', target_type='post',
                                       target_id='p9', message='hi')
        assert pushed == [], 'pushed before the row committed'
        db.commit()
        db.commit()
        on_disk = n.to_dict()
    finally:
        db.close()

    # The whole notification, as committed: every surface renders the
    # message and routes on type / target from this one push.
    assert pushed == [('new', 'u1', on_disk)]
    assert on_disk['message'] == 'hi' and on_disk['type'] == 'comment'
    assert on_disk['user_id'] == 'u1'


def test_a_rolled_back_notification_is_never_pushed(factory, pushed):
    from integrations.social.services import NotificationService

    db = factory()
    try:
        NotificationService.create(db, 'u1', 'comment', message='hi')
        db.rollback()
        db.commit()
        assert _rows(db) == 0
    finally:
        db.close()

    assert pushed == []


def test_a_notification_closed_without_commit_is_never_pushed(factory, pushed):
    from integrations.social.services import NotificationService

    db = factory()
    NotificationService.create(db, 'u1', 'comment', message='hi')
    db.close()
    try:
        db.commit()
    finally:
        db.close()

    assert pushed == []


def test_read_and_dismiss_fan_out_only_after_commit(factory, pushed):
    from integrations.social.services import NotificationService

    db = factory()
    try:
        a = NotificationService.create(db, 'u1', 'comment', message='a')
        b = NotificationService.create(db, 'u1', 'comment', message='b')
        db.commit()
        pushed.clear()

        NotificationService.mark_read(db, [a.id], 'u1')
        NotificationService.mark_dismissed(db, [b.id], 'u1')
        db.rollback()
        db.commit()
        assert pushed == [], 'a rolled-back read state was fanned out'

        NotificationService.mark_read(db, [a.id], 'u1')
        NotificationService.mark_dismissed(db, [b.id], 'u1')
        assert pushed == []
        db.commit()
        assert pushed == [('read', 'u1', [a.id]), ('read', 'u1', [b.id])]

        pushed.clear()
        c = NotificationService.create(db, 'u1', 'comment', message='c')
        db.commit()
        pushed.clear()
        NotificationService.mark_all_read(db, 'u1')
        db.rollback()
        db.commit()
        assert pushed == []
        NotificationService.mark_all_read(db, 'u1')
        db.commit()
        # b was dismissed, not read, so it is still unread here.
        assert [(k, u, sorted(ids)) for k, u, ids in pushed] == [
            ('read', 'u1', sorted([b.id, c.id]))]
    finally:
        db.close()


def test_nothing_to_mark_fans_out_nothing(factory, pushed):
    """An empty id list, or a user with nothing unread, has no read state
    to announce: no 'notification.read' push with an empty id list."""
    from integrations.social.services import NotificationService

    db = factory()
    try:
        NotificationService.mark_read(db, [], 'u1')
        NotificationService.mark_all_read(db, 'u1')
        db.commit()
    finally:
        db.close()

    assert pushed == []


# ── The helper itself: models.after_commit ──────────────────────────────
# SQLAlchemy 2.0 dispatches after_commit when a SAVEPOINT commits too
# (orm/session.py SessionTransaction.commit: ``if self._parent is None or
# self.nested``).  A savepoint commit is not durable: the outer transaction
# can still roll back, and until it ends the flushed rows hold SQLite's
# write lock.  So only the outermost commit may run the queue.


def test_a_savepoint_commit_runs_nothing_and_an_outer_rollback_drops_it(factory):
    from integrations.social.models import after_commit

    db = factory()
    runs = []
    try:
        db.connection()
        after_commit(db, lambda: runs.append('effect'))
        with db.begin_nested():
            pass
        assert runs == [], 'ran on a savepoint commit, before the outer one'
        db.rollback()
        db.commit()
    finally:
        db.close()

    assert runs == []


def test_a_savepoint_commit_then_the_outer_commit_runs_it_once(factory):
    from integrations.social.models import after_commit

    db = factory()
    runs = []
    try:
        db.connection()
        after_commit(db, lambda: runs.append('effect'))
        with db.begin_nested():
            after_commit(db, lambda: runs.append('inside'))
        assert runs == []
        db.commit()
        db.commit()
    finally:
        db.close()

    assert runs == ['effect', 'inside']


def test_a_notification_is_not_pushed_on_a_savepoint_commit(factory, pushed):
    """Measured at 79928c2fc: create, savepoint, commit pushed the same id
    twice; create, savepoint, rollback pushed a row that never reached
    disk."""
    from integrations.social.services import NotificationService

    db = factory()
    try:
        NotificationService.create(db, 'u1', 'comment', message='gone')
        with db.begin_nested():
            pass
        assert pushed == []
        db.rollback()
        assert _rows(db) == 0

        n = NotificationService.create(db, 'u1', 'comment', message='kept')
        with db.begin_nested():
            pass
        assert pushed == []
        db.commit()
        on_disk = n.to_dict()
    finally:
        db.close()

    assert pushed == [('new', 'u1', on_disk)]


def test_a_failing_effect_is_logged_and_the_rest_still_run(factory, caplog):
    import logging
    from integrations.social.models import after_commit

    def _boom():
        raise RuntimeError('bus down')

    db = factory()
    runs = []
    try:
        db.connection()
        after_commit(db, _boom)
        after_commit(db, lambda: runs.append('next'))
        with caplog.at_level(logging.WARNING,
                             logger='integrations.social.models'):
            db.commit()
    finally:
        db.close()

    assert runs == ['next']
    failed = [r for r in caplog.records
              if r.name == 'integrations.social.models'
              and r.levelno == logging.WARNING]
    assert len(failed) == 1, caplog.text
    assert failed[0].exc_info and 'bus down' in str(failed[0].exc_info[1])
