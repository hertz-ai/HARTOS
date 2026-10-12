"""Which lane a /chat turn runs on: core.request_lane.lane_for_chat.

Owner policy 2026-10-12: a person's conversation runs on the free model
unless it is revenue-bearing; revenue-bearing turns may use the expert model
and are never throttled.  Two things make a turn revenue-bearing and both
are things /chat can verify itself:

  * the caller's JWT carries the commerce tenant claim (a McGroce shopper or
    store owner, minted by POST /api/commerce/session);
  * the metered API endpoints' own self-POST says so for a paying tier, and
    the request authenticated with the node's own key (auth_source
    'api_key'), which no outside client holds.

Anything else, including a body that merely claims 'lane': 'revenue' over a
JWT or body auth, is a user turn.  The daemon lane is not decided here: the
request id's 'daemon_' prefix decides that on the wire (is_genuine_user_request).
"""
import pytest

from core.request_lane import lane_for_chat

TENANT = 'mcgroce'


def _decoder(payload):
    def decode(token):
        assert token == 'tok'
        return payload
    return decode


def test_commerce_tenant_jwt_is_revenue():
    lane = lane_for_chat('tok', {'prompt': 'add milk'}, 'jwt',
                         decode_jwt=_decoder({'user_id': 'c1', 'tid': TENANT}),
                         tenant=TENANT)
    assert lane == 'revenue'


def test_other_tenant_or_no_tenant_claim_is_user():
    assert lane_for_chat('tok', {}, 'jwt',
                         decode_jwt=_decoder({'user_id': 'c1', 'tid': 'other'}),
                         tenant=TENANT) == 'user'
    assert lane_for_chat('tok', {}, 'jwt',
                         decode_jwt=_decoder({'user_id': 'c1'}),
                         tenant=TENANT) == 'user'


def test_paid_tier_claim_is_trusted_only_with_the_nodes_own_key():
    body = {'prompt': 'x', 'lane': 'revenue'}
    assert lane_for_chat('', body, 'api_key', decode_jwt=None, tenant=TENANT) == 'revenue'
    # The same claim from a JWT or body-authenticated caller is not believed:
    # anyone can put a word in a request body.
    assert lane_for_chat('tok', body, 'jwt',
                         decode_jwt=_decoder({'user_id': 'u'}), tenant=TENANT) == 'user'
    assert lane_for_chat('', body, 'body', decode_jwt=None, tenant=TENANT) == 'user'
    assert lane_for_chat('', body, None, decode_jwt=None, tenant=TENANT) == 'user'


def test_free_tier_self_post_is_a_user_turn():
    assert lane_for_chat('', {'prompt': 'x', 'lane': 'user'}, 'api_key',
                         decode_jwt=None, tenant=TENANT) == 'user'
    assert lane_for_chat('', {'prompt': 'x'}, 'api_key',
                         decode_jwt=None, tenant=TENANT) == 'user'


def test_decode_failure_and_junk_are_user_turns():
    def boom(token):
        raise ValueError('expired')
    assert lane_for_chat('tok', {}, 'jwt', decode_jwt=boom, tenant=TENANT) == 'user'
    assert lane_for_chat('tok', None, 'jwt', decode_jwt=lambda t: None, tenant=TENANT) == 'user'
    assert lane_for_chat(None, 'not a dict', 'api_key', decode_jwt=None, tenant=TENANT) == 'user'


def test_tenant_claim_wins_over_a_free_tier_body():
    # A McGroce shopper reached through the metered API would still be revenue.
    lane = lane_for_chat('tok', {'lane': 'user'}, 'api_key',
                         decode_jwt=_decoder({'user_id': 'c1', 'tid': TENANT}),
                         tenant=TENANT)
    assert lane == 'revenue'


# ─── Which org goals earn (dispatch.is_revenue_goal_type) ────────────────────

def test_goals_that_earn_run_on_the_revenue_lane(monkeypatch):
    from integrations.agent_engine.dispatch import is_revenue_goal_type
    monkeypatch.delenv('HEVOLVE_REVENUE_GOAL_TYPES', raising=False)
    for gt in ('marketing', 'seo', 'revenue', 'hive_growth', 'hive_economics',
               'mcgroce_merchant', 'mcgroce_shopper', 'p2p_grocery', 'p2p_rideshare',
               'P2P_Logistics'):
        assert is_revenue_goal_type(gt), gt
    for gt in ('coding', 'self_heal', 'robot', 'thought_experiment', 'trading',
               'finance', 'news', 'tutor', '', None):
        assert not is_revenue_goal_type(gt), gt


def test_owner_can_replace_the_earning_set(monkeypatch):
    from integrations.agent_engine.dispatch import is_revenue_goal_type
    monkeypatch.setenv('HEVOLVE_REVENUE_GOAL_TYPES', 'tutor, trading')
    assert is_revenue_goal_type('tutor') and is_revenue_goal_type('TRADING')
    assert not is_revenue_goal_type('marketing')
