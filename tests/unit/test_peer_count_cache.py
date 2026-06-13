"""peer_discovery._cached_node_count: gossip advertises agent/post counts every
round (60s). Caching the COUNT query (30s hard TTL) stops the per-tick DB hit the
2026-06-13 dig caught this loop doing in SQL fetchall. The count is a GLOBAL db
stat that tolerates <=30s staleness for advertising — never live-critical state.
"""
import os
import sys
from unittest.mock import MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import integrations.social.peer_discovery as pd  # noqa: E402


class TestCachedNodeCount:
    def test_count_query_runs_once_within_ttl(self, monkeypatch):
        calls = {'n': 0}

        def fake_count():
            calls['n'] += 1
            return 7

        fake_db = MagicMock()
        fake_db.query.return_value.filter.return_value.count.side_effect = fake_count
        monkeypatch.setattr('integrations.social.models.get_db', lambda: fake_db)

        pd._cached_node_count.cache_clear()
        assert pd._cached_node_count('agent') == 7
        assert pd._cached_node_count('agent') == 7
        assert pd._cached_node_count('agent') == 7
        assert calls['n'] == 1, f"COUNT ran {calls['n']}x (expected 1 — cache miss)"
        fake_db.close.assert_called()  # session closed, no leak

    def test_distinct_what_cached_separately(self, monkeypatch):
        fake_db = MagicMock()
        fake_db.query.return_value.filter.return_value.count.return_value = 3
        monkeypatch.setattr('integrations.social.models.get_db', lambda: fake_db)

        pd._cached_node_count.cache_clear()
        assert pd._cached_node_count('agent') == 3
        assert pd._cached_node_count('post') == 3  # different key -> not served from 'agent'
        assert fake_db.query.call_count >= 2

    def test_get_count_returns_zero_on_error(self, monkeypatch):
        # Behaviour preserved: a DB failure still yields 0, not an exception, and
        # the failure is NOT cached (ttl_cached only caches successes).
        def boom():
            raise RuntimeError('db down')
        monkeypatch.setattr('integrations.social.models.get_db', boom)
        pd._cached_node_count.cache_clear()
        gp = pd.GossipProtocol.__new__(pd.GossipProtocol)  # no __init__ needed for _get_count
        assert gp._get_count('agent') == 0
