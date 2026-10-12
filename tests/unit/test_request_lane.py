"""Which lane a /chat turn runs on: core.request_lane.lane_for_chat.

Owner policy 2026-10-12: a person's conversation runs on the free model
unless it is revenue-bearing; revenue-bearing turns may use the expert model
and are never throttled.  Two things make a turn revenue-bearing and both
are things /chat can verify itself:

  * the caller's JWT carries the commerce tenant claim (a McGroce shopper or
    store owner, minted by POST /api/commerce/session);
  * the metered API endpoints' own self-POST says so for a paying tier, and
    the request is the node itself: its own key (auth_source 'api_key') or
    the JWT it mints for its system identity, neither of which an outside
    client holds.

Anything else, including a body that merely claims 'lane': 'revenue' over a
stranger's JWT or body auth, is a user turn.  The daemon lane is not decided
here: the request id's 'daemon_' prefix decides that on the wire
(is_genuine_user_request), and register_chat_lane leaves the node's own
daemon turn to it.
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


# ─── the node talking to itself (is_internal_caller) ─────────────────────────
# _internal_auth_headers sends the node's key when HEVOLVE_API_KEY is set and
# otherwise a JWT for the node's own system identity (review of 7086adff8,
# finding 2: the keyless case computed 'user' for every paying call).

SYSTEM = ('system_daemon',)


def test_internal_caller_is_the_nodes_key_or_its_system_jwt():
    from core.request_lane import is_internal_caller
    assert is_internal_caller('api_key', {}, ())
    assert is_internal_caller('jwt', {'user_id': 'system_daemon'}, SYSTEM)
    assert not is_internal_caller('jwt', {'user_id': 'u1'}, SYSTEM)
    assert not is_internal_caller('jwt', {}, SYSTEM)
    assert not is_internal_caller('body', None, SYSTEM)
    assert not is_internal_caller(None, {'user_id': 'system_daemon'}, ())   # no identity named


def test_paid_tier_claim_is_trusted_from_the_system_jwt_too():
    body = {'prompt': 'x', 'lane': 'revenue'}
    assert lane_for_chat('tok', body, 'jwt', decode_jwt=_decoder({'user_id': 'system_daemon'}),
                         tenant=TENANT, system_identities=SYSTEM) == 'revenue'
    assert lane_for_chat('tok', body, 'jwt', decode_jwt=_decoder({'user_id': 'u1'}),
                         tenant=TENANT, system_identities=SYSTEM) == 'user'


# ─── /chat's one registration decision (register_chat_lane) ──────────────────

@pytest.fixture
def lanes():
    from core import llm_outbound_logger as lol
    with lol._request_lanes_lock:
        lol._request_lanes.clear()
    yield lol
    with lol._request_lanes_lock:
        lol._request_lanes.clear()


def _genuine(rid):
    from integrations.agent_engine.dispatch import is_genuine_user_request
    return is_genuine_user_request(rid)


@pytest.mark.parametrize('auth, tok, dec', [
    ('jwt', 'tok', _decoder({'user_id': 'system_daemon'})),   # keyless node: system JWT
    ('api_key', '', None),                                      # keyed node
    ('body', '', None),                                         # bundled desktop / flat: no credential
    ('none', '', None),
    (None, '', None),
])
def test_the_nodes_own_daemon_turn_is_left_to_the_prefix_rule(lanes, auth, tok, dec):
    """dispatch_goal registered revenue for a goal that earns; its turn reaches
    /chat as the node itself with a daemon_ id and must not be demoted
    (review of 7086adff8 finding 1: it was registered 'user'; review of
    8947e2753 M2: the bundled desktop's in-process adapter sends no
    credential at all and was demoted too)."""
    from core.request_lane import register_chat_lane
    lanes.register_request_lane('daemon_goal-rev-1', 'revenue')
    assert register_chat_lane('daemon_goal-rev-1', tok, {'prompt': 'x'}, auth,
                              decode_jwt=dec, tenant=TENANT,
                              system_identities=SYSTEM, is_genuine=_genuine) is None
    assert lanes.lane_for_request('daemon_goal-rev-1') == 'revenue'
    # A plain goal's turn: nothing registered, the prefix rule says daemon.
    assert register_chat_lane('daemon_goal-plain-1', tok, {'prompt': 'x'}, auth,
                              decode_jwt=dec, tenant=TENANT,
                              system_identities=SYSTEM, is_genuine=_genuine) is None
    assert lanes.lane_for_request('daemon_goal-plain-1') == ''


@pytest.mark.parametrize('auth, tok, dec', [
    ('jwt', 'tok', _decoder({'user_id': 'system_daemon'})),
    ('api_key', '', None),
])
def test_the_nodes_own_inner_call_without_a_claim_keeps_the_turns_lane(lanes, auth, tok, dec):
    """Review of 8947e2753, C1: when a revenue turn uses the expert, the
    speculative dispatcher calls /chat again from inside the node with the
    person's request id, the node's credential and NO lane field; that call
    re-registered the shopper's turn as 'user' and the Claude call went out
    capped.  An internal caller that makes no claim confirms the lane that
    is there and registers nothing."""
    from core.request_lane import register_chat_lane
    lanes.register_request_lane('msg_shopper_1', 'revenue')            # the outer, shopper call
    inner = {'user_id': 'c1', 'prompt': 'add milk', 'model_config': [{'model': 'claude-sonnet-4-5'}],
             'speculative': False, 'request_id': 'msg_shopper_1'}
    assert register_chat_lane('msg_shopper_1', tok, inner, auth, decode_jwt=dec, tenant=TENANT,
                              system_identities=SYSTEM, is_genuine=_genuine) == 'revenue'
    assert lanes.lane_for_request('msg_shopper_1') == 'revenue'
    # A person's turn stays a user turn through the same inner call.
    lanes.register_request_lane('msg_person_1', 'user')
    assert register_chat_lane('msg_person_1', tok, dict(inner, request_id='msg_person_1'), auth,
                              decode_jwt=dec, tenant=TENANT, system_identities=SYSTEM,
                              is_genuine=_genuine) == 'user'
    # No earlier registration and no claim: nothing is registered, the id
    # rule decides on the wire.
    assert register_chat_lane('msg_nobody', tok, dict(inner, request_id='msg_nobody'), auth,
                              decode_jwt=dec, tenant=TENANT, system_identities=SYSTEM,
                              is_genuine=_genuine) is None
    assert lanes.lane_for_request('msg_nobody') == ''


def test_a_stranger_naming_a_goals_request_id_does_not_inherit_its_lane(lanes):
    """Review finding 3 (security): with keep_existing a stranger's JWT turn
    that named a revenue goal's id got the revenue lane."""
    from core.request_lane import register_chat_lane
    lanes.register_request_lane('daemon_goal-rev-1', 'revenue')
    lane = register_chat_lane('daemon_goal-rev-1', 'tok', {'prompt': 'x', 'lane': 'revenue'}, 'jwt',
                              decode_jwt=_decoder({'user_id': 'stranger'}), tenant=TENANT,
                              system_identities=SYSTEM, is_genuine=_genuine)
    assert lane == 'user'
    assert lanes.lane_for_request('daemon_goal-rev-1') == 'user'


def test_the_metered_self_post_is_registered_by_its_body_from_either_credential(lanes):
    from core.request_lane import register_chat_lane
    for auth, tok, dec in (('api_key', '', None),
                           ('jwt', 'tok', _decoder({'user_id': 'system_daemon'}))):
        rid = 'api_1760300000_' + auth
        assert register_chat_lane(rid, tok, {'lane': 'revenue'}, auth, decode_jwt=dec,
                                  tenant=TENANT, system_identities=SYSTEM,
                                  is_genuine=_genuine) == 'revenue'
        assert lanes.lane_for_request(rid) == 'revenue'
    assert register_chat_lane('api_1760300000_free', '', {'lane': 'user'}, 'api_key',
                              tenant=TENANT, is_genuine=_genuine) == 'user'


def test_a_shopper_is_registered_revenue_and_a_person_user(lanes):
    from core.request_lane import register_chat_lane
    assert register_chat_lane('msg_1', 'tok', {}, 'jwt',
                              decode_jwt=_decoder({'user_id': 'c1', 'tid': TENANT}),
                              tenant=TENANT, is_genuine=_genuine) == 'revenue'
    assert register_chat_lane('msg_2', 'tok', {'lane': 'revenue'}, 'jwt',
                              decode_jwt=_decoder({'user_id': 'c2'}),
                              tenant=TENANT, is_genuine=_genuine) == 'user'
    assert lanes.lane_for_request('msg_1') == 'revenue'
    assert lanes.lane_for_request('msg_2') == 'user'
