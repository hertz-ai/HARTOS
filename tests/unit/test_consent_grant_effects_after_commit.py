"""A consent grant's side effects run after its row commits, never inside it.

Measured on the live desktop 2026-09-25 (logwf defect 2): the owner pressed
Allow on the screen-capture card.  grant_consent flushed the INSERT, which
takes SQLite's one write lock, and then, in the same open transaction,
started the screen feed (_embodied_feed_from_consent -> admin _save_config ->
VisionService.start).  That feed start never returned, so the row never
committed and every other writer in the process failed with "database is
locked" (busy_timeout 3 s) for about 41 minutes: audit entries fell back to
memory, health rounds and goal notifications were lost, and the grant itself
was lost, so the card came back after every Allow.

These tests use a real file-backed SQLite database in WAL mode, the same
shape the desktop runs, and a second connection standing in for every other
writer in the process.

    python -m pytest tests/unit/test_consent_grant_effects_after_commit.py -q
"""
import os
import sqlite3
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

UID = '10'
AGENT = '88659566083'
SCOPE = 'display:1'


@pytest.fixture
def dbfile(tmp_path):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from integrations.social.models import UserConsent

    path = str(tmp_path / 'consent.db')
    engine = create_engine(f'sqlite:///{path}')

    @event.listens_for(engine, 'connect')
    def _wal(conn, _rec):
        cur = conn.cursor()
        cur.executescript('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=200;')
        cur.close()

    UserConsent.__table__.create(engine)
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE other_writer (v TEXT)')
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield path, factory
    engine.dispose()


def _other_writer(path):
    """What every other writer in the process sees at this moment: 'ok' when
    it can write, else the error text; plus the granted rows it can read."""
    conn = sqlite3.connect(path, timeout=0.2)
    try:
        try:
            conn.execute("INSERT INTO other_writer VALUES ('x')")
            conn.commit()
            wrote = 'ok'
        except sqlite3.OperationalError as e:
            wrote = str(e)
        granted = conn.execute(
            'SELECT COUNT(*) FROM user_consents WHERE granted = 1').fetchone()[0]
        return wrote, granted
    finally:
        conn.close()


@pytest.fixture
def effects(monkeypatch, dbfile):
    """Record each grant side effect, and what another writer saw while the
    slowest one (the feed start) was running."""
    import integrations.social.consent_service as cs

    path, _ = dbfile
    seen = {'emit': [], 'copilot': [], 'feed': [], 'order': []}

    def _emit(topic, data, msg_id=None):
        seen['order'].append('emit')
        seen['emit'].append((topic, dict(data)))

    def _copilot(consent_type, granted):
        seen['order'].append('copilot')
        seen['copilot'].append((consent_type, granted))

    def _feed(consent_type, granted):
        seen['order'].append('feed')
        seen['feed'].append((consent_type, granted, _other_writer(path)))

    monkeypatch.setattr(cs, '_emit', _emit)
    monkeypatch.setattr(cs, '_copilot_switch_from_consent', _copilot)
    monkeypatch.setattr(cs, '_embodied_feed_from_consent', _feed)
    return seen


def test_the_feed_starts_after_the_grant_commits_and_holds_no_lock(dbfile, effects):
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        db.commit()
    finally:
        db.close()

    assert len(effects['feed']) == 1
    consent_type, granted, (wrote, visible) = effects['feed'][0]
    assert (consent_type, granted) == ('screen_capture', True)
    assert wrote == 'ok', f'another writer was locked out: {wrote}'
    assert visible == 1, 'the feed started before the grant was on disk'
    assert effects['emit'] == [('consent.granted', {
        'user_id': UID, 'consent_type': 'screen_capture',
        'scope': '*', 'agent_id': None})]
    assert effects['copilot'] == [('screen_capture', True)]


def test_a_promoted_per_agent_ask_also_starts_its_feed_after_commit(dbfile, effects):
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    db = factory()
    try:
        ConsentService.request_consent(db, UID, 'screen_capture',
                                       scope=SCOPE, agent_id=AGENT)
        db.commit()
        effects['emit'].clear()
        row = ConsentService.grant_consent(db, UID, 'screen_capture',
                                           scope=SCOPE, agent_id=AGENT)
        db.commit()
    finally:
        db.close()

    assert row.granted is True
    assert len(effects['feed']) == 1
    _, _, (wrote, visible) = effects['feed'][0]
    assert wrote == 'ok', f'another writer was locked out: {wrote}'
    assert visible == 1
    # The whole payload: a surface dismisses the card for THIS agent's ask
    # by matching agent_id and scope, so neither may be lost or widened.
    assert effects['emit'] == [('consent.granted', {
        'user_id': UID, 'consent_type': 'screen_capture',
        'scope': SCOPE, 'agent_id': AGENT})]
    assert effects['copilot'] == [('screen_capture', True)]


def test_a_new_per_agent_grant_names_its_agent_and_scope(dbfile, effects):
    """The insert branch (no ask on file) carries the same payload."""
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture',
                                     scope=SCOPE, agent_id=AGENT)
        db.commit()
    finally:
        db.close()

    assert effects['emit'] == [('consent.granted', {
        'user_id': UID, 'consent_type': 'screen_capture',
        'scope': SCOPE, 'agent_id': AGENT})]
    assert [(t, g) for t, g, _ in effects['feed']] == [('screen_capture', True)]


def test_the_broadcast_leaves_before_the_feed_starts(dbfile, effects):
    """The feed start is the effect that hung live (it never returned), so
    it runs last: every surface hears consent.granted and the copilot
    switch is set before it, and a feed that never returns cannot hold
    them back."""
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        db.commit()
    finally:
        db.close()

    assert effects['order'] == ['emit', 'copilot', 'feed']


def test_a_rolled_back_grant_turns_nothing_on(dbfile, effects):
    """The row never reached disk, so nothing may act as if it had -- not
    then, and not when the same session later commits other work."""
    from integrations.social.consent_service import ConsentService

    path, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        db.rollback()
        ConsentService.request_consent(db, UID, 'data_access')
        effects['emit'].clear()
        db.commit()
    finally:
        db.close()

    assert effects['feed'] == []
    assert effects['copilot'] == []
    assert effects['emit'] == []
    assert _other_writer(path)[1] == 0


def test_the_effects_run_once_even_when_the_session_commits_again(dbfile, effects):
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        db.commit()
        ConsentService.grant_consent(db, UID, 'camera_capture')
        db.commit()
        db.commit()
    finally:
        db.close()

    assert [(t, g) for t, g, _ in effects['feed']] == [
        ('screen_capture', True), ('camera_capture', True)]
    assert len(effects['emit']) == 2


def test_a_failing_effect_neither_fails_the_commit_nor_skips_the_rest(
        monkeypatch, dbfile, effects):
    import integrations.social.consent_service as cs
    from integrations.social.consent_service import ConsentService

    def _boom(topic, data, msg_id=None):
        raise RuntimeError('bus down')

    monkeypatch.setattr(cs, '_emit', _boom)
    path, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        db.commit()
    finally:
        db.close()

    assert _other_writer(path)[1] == 1
    assert effects['copilot'] == [('screen_capture', True)]
    assert [(t, g) for t, g, _ in effects['feed']] == [('screen_capture', True)]


def test_an_auto_grant_notice_follows_the_committed_grant(dbfile, effects):
    """consent.auto_granted offers a one-tap revoke, so it names a consent
    that must exist: it leaves after the commit, after consent.granted, and
    a rolled-back auto-grant sends nothing."""
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    db = factory()
    try:
        assert ConsentService.auto_grant_with_notice(
            db, UID, 'cloud_egress', scope='vision') is True
        assert effects['emit'] == []
        db.commit()
        assert [t for t, _ in effects['emit']] == [
            'consent.granted', 'consent.auto_granted']
        assert effects['emit'][1][1]['revoke_action'] == 'consent.revoke'

        effects['emit'].clear()
        ConsentService.auto_grant_with_notice(db, UID, 'cloud_egress',
                                              scope='social_sync')
        db.rollback()
        db.commit()
        assert effects['emit'] == []
    finally:
        db.close()


def test_a_savepoint_rolled_back_after_the_grant_keeps_the_grant_effects(
        dbfile, effects):
    """Only the outer transaction decides: a nested savepoint that rolls
    back leaves the grant in place, so its effects still run on commit."""
    from integrations.social.consent_service import ConsentService

    path, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        nested = db.begin_nested()
        ConsentService.request_consent(db, UID, 'data_access')
        nested.rollback()
        db.commit()
    finally:
        db.close()

    assert _other_writer(path)[1] == 1
    assert [(t, g) for t, g, _ in effects['feed']] == [('screen_capture', True)]


def test_a_savepoint_committed_after_the_grant_starts_nothing(dbfile, effects):
    """A savepoint commit is not the grant committing: SQLAlchemy fires
    after_commit for it too, but the outer transaction still holds the
    write lock and can still roll back.  Nothing may start until the outer
    commit, and a rolled-back outer transaction starts nothing."""
    from sqlalchemy import text
    from integrations.social.consent_service import ConsentService

    path, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        with db.begin_nested():
            db.execute(text("INSERT INTO other_writer VALUES ('nested')"))
        assert effects['order'] == [], 'started on a savepoint commit'
        db.rollback()
        db.commit()
    finally:
        db.close()

    assert effects['order'] == []
    assert _other_writer(path)[1] == 0


def test_a_savepoint_then_the_grant_commit_starts_the_feed_once(dbfile, effects):
    from sqlalchemy import text
    from integrations.social.consent_service import ConsentService

    _, factory = dbfile
    db = factory()
    try:
        ConsentService.grant_consent(db, UID, 'screen_capture')
        with db.begin_nested():
            db.execute(text("INSERT INTO other_writer VALUES ('nested')"))
        assert effects['order'] == []
        db.commit()
    finally:
        db.close()

    assert effects['order'] == ['emit', 'copilot', 'feed']
    _, _, (wrote, visible) = effects['feed'][0]
    assert wrote == 'ok', f'another writer was locked out: {wrote}'
    assert visible == 1


def test_a_grant_closed_without_a_commit_turns_nothing_on(dbfile, effects):
    """close() discards the transaction without a rollback event; the queue
    must go with it, even when the session object is used again."""
    from integrations.social.consent_service import ConsentService

    path, factory = dbfile
    db = factory()
    ConsentService.grant_consent(db, UID, 'screen_capture')
    db.close()
    try:
        ConsentService.request_consent(db, UID, 'data_access')
        effects['emit'].clear()
        db.commit()
    finally:
        db.close()

    assert effects['feed'] == []
    assert effects['emit'] == []
    assert _other_writer(path)[1] == 0
