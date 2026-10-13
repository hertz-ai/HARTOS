"""A real api_keys table for the commercial API's tests.

The routes read and write a key through SQLAlchemy: ``filter_by`` on the id
AND the user id, and (from the round after fd2b4773c) one conditional
UPDATE for the tier a key bought.  A MagicMock session answers every filter
with the same object and every write with nothing, so the review of
fd2b4773c dropped the user_id filter from the browser route and twenty
tests still passed, and the tier race it measured could not be written as
a test at all.  This is one SQLite file per test (threads open their own
sessions on it), the social schema created on it, two people and one free
key; the engine is the test's, never the node's.
"""
import threading
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

OWNER, OTHER, KEY = 'u-1', 'u-2', 'k1'


@pytest.fixture
def key_db(tmp_path):
    """The table for one test, with get_db() handing out its sessions.
    A test module takes it with ``from tests.unit.commercial_db import
    key_db`` (pytest collects fixtures a module imports)."""
    db = ApiKeyDb(tmp_path)
    try:
        with patch('integrations.social.models.get_db', side_effect=db.session):
            yield db
    finally:
        db.dispose()


class ApiKeyDb:
    def __init__(self, tmp_path):
        from integrations.social.models import Base, CommercialAPIKey, User
        self._CommercialAPIKey = CommercialAPIKey
        self.engine = create_engine(
            f"sqlite:///{tmp_path / 'commercial.db'}",
            connect_args={'check_same_thread': False, 'timeout': 30})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as s:
            s.add_all([
                User(id=OWNER, username='buyer', email='buyer@test',
                     password_hash='x', user_type='human'),
                User(id=OTHER, username='other', email='other@test',
                     password_hash='x', user_type='human'),
                CommercialAPIKey(id=KEY, user_id=OWNER, key_hash='h-k1',
                                 key_prefix='hk_k1', tier='free',
                                 rate_limit_per_day=100, monthly_quota=3000),
            ])
            s.commit()
        self._fail_commits = 0
        self._fail_lock = threading.Lock()
        event.listen(self.Session, 'before_commit', self._maybe_fail)

    def _maybe_fail(self, session):
        with self._fail_lock:
            if self._fail_commits <= 0:
                return
            self._fail_commits -= 1
        raise RuntimeError('db locked')

    def session(self):
        """A fresh Session: what get_db() and _get_user_from_token hand a
        route, one per request (and one per thread in a race)."""
        return self.Session()

    def key(self, key_id=KEY):
        """``(tier, rate_limit_per_day, monthly_quota)`` as the table holds
        them now, read through a session of its own; None when gone."""
        with self.Session() as s:
            k = s.get(self._CommercialAPIKey, key_id)
            return None if k is None else (k.tier, k.rate_limit_per_day, k.monthly_quota)

    def put_key_on(self, tier, key_id=KEY):
        from integrations.agent_engine.commercial_api import TIER_CONFIG
        cfg = TIER_CONFIG[tier]
        with self.Session() as s:
            k = s.get(self._CommercialAPIKey, key_id)
            k.tier = tier
            k.rate_limit_per_day = cfg['rate_limit_per_day']
            k.monthly_quota = cfg['monthly_quota']
            s.commit()

    def fail_next_commit(self, times=1):
        """The next ``times`` commits on any session raise before they
        write (a locked file, a lost connection): the bump that follows a
        recorded payment fails, which is the case a retry must finish."""
        with self._fail_lock:
            self._fail_commits = times

    def dispose(self):
        event.remove(self.Session, 'before_commit', self._maybe_fail)
        self.engine.dispose()
