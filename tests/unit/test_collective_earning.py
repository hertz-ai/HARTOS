"""L3 collective-earning — the SAFE inert slice (producer + pure aggregator).

Verifies the three design invariants (docs/architecture/L3_COLLECTIVE_EARNING_
DESIGN.md): user-scoped sum, idempotent (no double-credit on re-remit), and
cross-user isolation (one user's money never mixes with another's).  Pure
functions — no DB, no broadcast — so they're fully testable WITHOUT the 2-node
live verify (#150) the cross-node transport will still require.

    python -m pytest tests/unit/test_collective_earning.py --noconftest -q
"""
import unittest
from unittest.mock import patch

from integrations.agent_engine.collective_earning import (
    aggregate_collective_earnings,
    extract_earning_delta,
)


class TestExtractEarningDelta(unittest.TestCase):
    def test_none_without_attribution(self):
        # A node must never emit an unattributed earning delta.
        self.assertIsNone(extract_earning_delta(None, '', 'n1'))
        self.assertIsNone(extract_earning_delta(None, 'u1', ''))

    def test_shape_reads_canonical_revenue_query(self):
        fake = {'total_gross': 100.0, 'user_pool_share': 90.0,
                'api_revenue': 60.0, 'ad_revenue': 40.0, 'hosting_payouts': 5.0}
        with patch('integrations.agent_engine.revenue_aggregator.query_revenue_streams',
                   return_value=fake):
            d = extract_earning_delta(object(), 'u1', 'n1', period_days=30)
        self.assertEqual(d['user_id'], 'u1')
        self.assertEqual(d['node_id'], 'n1')
        self.assertEqual(d['pool_share_90'], 90.0)
        self.assertEqual(d['gross'], 100.0)
        self.assertEqual(d['period_days'], 30)
        self.assertIn('ts', d)


class TestAggregateCollectiveEarnings(unittest.TestCase):
    def _d(self, uid, nid, pool, ts, per=30, gross=None):
        return {'user_id': uid, 'node_id': nid, 'period_days': per,
                'pool_share_90': pool,
                'gross': gross if gross is not None else pool, 'ts': ts}

    def test_sums_a_users_own_nodes(self):
        out = aggregate_collective_earnings([
            self._d('u1', 'n1', 90.0, 1.0),
            self._d('u1', 'n2', 45.0, 1.0),
        ])
        self.assertEqual(out['u1']['pool_share_90'], 135.0)
        self.assertEqual(out['u1']['node_count'], 2)
        self.assertEqual(out['u1']['nodes'], ['n1', 'n2'])

    def test_idempotent_no_double_credit_on_re_remit(self):
        out = aggregate_collective_earnings([
            self._d('u1', 'n1', 90.0, 1.0),
            self._d('u1', 'n1', 90.0, 2.0),   # same node+period re-remitted
        ])
        self.assertEqual(out['u1']['pool_share_90'], 90.0)   # NOT 180
        self.assertEqual(out['u1']['node_count'], 1)

    def test_latest_ts_wins(self):
        out = aggregate_collective_earnings([
            self._d('u1', 'n1', 50.0, 5.0),   # newer
            self._d('u1', 'n1', 90.0, 1.0),   # older — must lose
        ])
        self.assertEqual(out['u1']['pool_share_90'], 50.0)

    def test_cross_user_isolation(self):
        out = aggregate_collective_earnings([
            self._d('u1', 'n1', 90.0, 1.0),
            self._d('u2', 'n2', 70.0, 1.0),
        ])
        self.assertEqual(out['u1']['pool_share_90'], 90.0)
        self.assertEqual(out['u2']['pool_share_90'], 70.0)
        self.assertNotIn('n2', out['u1']['nodes'])   # u2's node never in u1's pool

    def test_unattributed_deltas_dropped(self):
        out = aggregate_collective_earnings([
            {'user_id': '', 'node_id': 'n1', 'pool_share_90': 90.0,
             'ts': 1.0, 'period_days': 30},
            {'user_id': 'u1', 'node_id': '', 'pool_share_90': 90.0,
             'ts': 1.0, 'period_days': 30},
            self._d('u1', 'n1', 10.0, 1.0),
        ])
        self.assertEqual(set(out.keys()), {'u1'})
        self.assertEqual(out['u1']['pool_share_90'], 10.0)   # only the attributed delta

    def test_empty_and_malformed_safe(self):
        self.assertEqual(aggregate_collective_earnings([]), {})
        self.assertEqual(aggregate_collective_earnings(None), {})
        # malformed entries are skipped, never crash
        self.assertEqual(aggregate_collective_earnings([None, 42, 'x']), {})


if __name__ == '__main__':
    unittest.main()
