"""Remote compute is paid by the requester and earned by the operator.

Owner rulings 2026-09-26:
  (a) the metered boundary is work that goes to "hive nodes ... that's not
      their node", tracked inside HARTOS;
  (b) "proportinal to compute spent and earned": ONE measured quantity per
      exchange is debited from the requester and credited to the serving
      operator, spend == earn before the 90/9/1 split;
  (c) "for local person'a work zero spark earned": own node, a SAME_USER
      node, or a local model costs 0 and earns 0.

Each node records its own half on its own node: the requesting node debits
its person when the result returns, the serving node credits its operator
when it serves someone else.  Both measure the exchange with
budget_gate.exchange_tokens from the same request and response.

Every test runs the real code against a real SQLite schema: the wallet and
the MeteredAPIUsage rows are observed, never a mock's call args standing in
for a balance.  The two-node tests run the requester's code against one
database and the server's against another, the server half driven by the
very request the requester sent.
"""
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.social import models as _models  # noqa: E402
from integrations.social.models import (  # noqa: E402
    Base, User, PeerNode, ResonanceWallet, ResonanceTransaction,
    MeteredAPIUsage,
)

REQUESTER = 'user-requester'
OPERATOR = 'user-operator'
SERVING_NODE = 'node-of-operator'
REQUESTER_NODE = 'node-of-requester'


def _factory():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db_factory(monkeypatch):
    """The requesting node's database: every get_db() in the code under test
    and every db the test opens share it."""
    factory = _factory()
    monkeypatch.setattr(_models, 'get_db', lambda: factory())
    # No PeerLink manager is running in a unit test: no link proves anything
    # unless a test installs one.
    fake_mgr = MagicMock()
    fake_mgr.get_link.return_value = None
    monkeypatch.setattr('core.peer_link.link_manager.get_link_manager',
                        lambda: fake_mgr)
    monkeypatch.setenv('HEVOLVE_NODE_ID', REQUESTER_NODE)
    monkeypatch.delenv('HEVOLVE_USER_ID', raising=False)
    yield factory


@contextmanager
def _session(factory):
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _seed(factory, requester_spark=1000, operator_id=OPERATOR,
          node_id=SERVING_NODE, operator_spark=0):
    with _session(factory) as db:
        for uid in {REQUESTER, OPERATOR, operator_id} - {None}:
            db.add(User(id=uid, username=uid, user_type='human'))
        db.add(ResonanceWallet(user_id=REQUESTER, spark=requester_spark,
                               spark_lifetime=requester_spark))
        if operator_id and operator_id != REQUESTER:
            db.add(ResonanceWallet(user_id=operator_id, spark=operator_spark,
                                   spark_lifetime=operator_spark))
        db.add(PeerNode(node_id=node_id, url='http://peer.example:6777',
                        node_operator_id=operator_id))


def _spark(factory, user_id):
    with _session(factory) as db:
        w = db.query(ResonanceWallet).filter_by(user_id=user_id).first()
        return w.spark if w else 0


def _rows(factory):
    with _session(factory) as db:
        return [(r.requester_user_id, r.operator_id, r.node_id,
                 r.tokens_in, r.tokens_out, r.estimated_spark_cost,
                 r.settlement_status, r.task_source)
                for r in db.query(MeteredAPIUsage).all()]


def _tokens_for_spark(n):
    """Measured tokens that price at exactly n Spark under the canonical rate."""
    from integrations.agent_engine.budget_gate import spark_per_1k_compute_tokens
    return int(round(n * 1000 / spark_per_1k_compute_tokens()))


def _settle(factory):
    from integrations.agent_engine.revenue_aggregator import settle_metered_api_costs
    with _session(factory) as db:
        return settle_metered_api_costs(db)


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


# ─── the rate: an EXISTING compute-to-Spark conversion, not a new number ───

class TestCanonicalRate:

    def test_rate_is_composed_from_the_two_existing_conversions(self):
        from integrations.agent_engine.budget_gate import spark_per_1k_compute_tokens
        from integrations.social.hosting_reward_service import GPU_SECONDS_PER_1K_TOKENS
        from integrations.social.resonance_engine import AWARD_TABLE
        expected = (GPU_SECONDS_PER_1K_TOKENS / 3600.0
                    * AWARD_TABLE['compute_hour']['spark'])
        assert spark_per_1k_compute_tokens() == pytest.approx(expected)
        assert expected > 0

    def test_rate_reads_the_award_table_live(self, monkeypatch):
        from integrations.agent_engine.budget_gate import spark_per_1k_compute_tokens
        from integrations.social import resonance_engine
        base = spark_per_1k_compute_tokens()
        monkeypatch.setitem(resonance_engine.AWARD_TABLE, 'compute_hour',
                            {'spark': resonance_engine.AWARD_TABLE['compute_hour']['spark'] * 2})
        assert spark_per_1k_compute_tokens() == pytest.approx(base * 2)

    def test_compute_stats_use_the_same_tokens_to_gpu_seconds_constant(self):
        from integrations.social.hosting_reward_service import (
            HostingRewardService, GPU_SECONDS_PER_1K_TOKENS)
        peer = MagicMock(gpu_hours_served=0, total_inferences=0,
                         energy_kwh_contributed=0, metered_api_costs_absorbed=0)
        usage = MagicMock(tokens_in=3000, tokens_out=600, actual_usd_cost=0)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = peer
        db.query.return_value.filter.return_value.all.return_value = [usage]
        with patch('integrations.agent_engine.model_registry.model_registry') as reg:
            reg.get_total_energy_kwh.return_value = 0.0
            out = HostingRewardService.aggregate_compute_stats(db, 'n1')
        assert out['gpu_hours_added'] == pytest.approx(
            round(3.6 * GPU_SECONDS_PER_1K_TOKENS / 3600.0, 4))


# ─── the measure: a peer's report can lower it, never raise it ───

class TestExchangeTokens:

    def test_inflated_prompt_claim_is_capped_at_the_counted_prompt(self):
        """Reviewer probe 1: prompt_tokens worth 900 Spark for 'q'."""
        from core.token_utils import count_tokens_for_text
        from integrations.agent_engine.budget_gate import exchange_tokens
        tin, tout = exchange_tokens(
            'q', 'ok', {'prompt_tokens': _tokens_for_spark(900),
                        'completion_tokens': 0}, 1500)
        assert tin == count_tokens_for_text('q')
        assert tout == 0

    def test_completion_claim_is_capped_at_max_tokens(self):
        from integrations.agent_engine.budget_gate import exchange_tokens
        assert exchange_tokens('q', 'ok', {'prompt_tokens': 1,
                                           'completion_tokens': 10 ** 7},
                               1500)[1] == 1500

    def test_a_lower_honest_claim_is_used(self):
        from core.token_utils import count_tokens_for_text
        from integrations.agent_engine.budget_gate import exchange_tokens
        p = 'hello there ' * 50
        assert count_tokens_for_text(p) > 7
        assert exchange_tokens(p, 'r', {'prompt_tokens': 7,
                                        'completion_tokens': 3}, 1500) == (7, 3)

    def test_counted_when_no_usage(self):
        from core.token_utils import count_tokens_for_text
        from integrations.agent_engine.budget_gate import exchange_tokens
        p, r = 'hello there ' * 50, 'general kenobi ' * 20
        assert exchange_tokens(p, r, None, None) == (
            count_tokens_for_text(p), count_tokens_for_text(r))
        assert exchange_tokens(p, r, None, 5) == (count_tokens_for_text(p), 5)

    def test_completion_exchange_reads_messages_and_first_choice(self):
        from core.token_utils import count_tokens_for_text
        from integrations.agent_engine.budget_gate import completion_exchange
        req = {'messages': [{'role': 'user', 'content': 'why ' * 40}],
               'max_tokens': 10}
        resp = {'choices': [{'message': {'content': 'because ' * 30}}]}
        assert completion_exchange(req, resp) == (
            count_tokens_for_text('why ' * 40), 10)


# ─── the requester's half ───

class TestChargeRemoteCompute:

    def test_other_persons_node_debits_requester_by_measured_units(self, db_factory):
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100)
        tin, tout = _tokens_for_spark(2), _tokens_for_spark(1)
        moved = charge_remote_compute(REQUESTER, SERVING_NODE, tin, tout,
                                      source='test', ref_id='r1', model_id='m')
        assert moved == 3
        assert _spark(db_factory, REQUESTER) == 97
        rows = _rows(db_factory)
        assert rows == [(REQUESTER, OPERATOR, SERVING_NODE, tin, tout, 3,
                         'debited', 'hive_compute')]
        with _session(db_factory) as db:
            spent = db.query(ResonanceTransaction).filter_by(
                user_id=REQUESTER, source_type='hive_compute_spent').all()
        assert [t.amount for t in spent] == [-3]

    def test_the_requester_node_never_credits_the_operator(self, db_factory):
        """The operator's credit is the serving node's, on its own ledger."""
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100)
        charge_remote_compute(REQUESTER, SERVING_NODE, _tokens_for_spark(4), 0,
                              source='t')
        _settle(db_factory)
        _settle(db_factory)
        assert _spark(db_factory, OPERATOR) == 0
        assert _spark(db_factory, REQUESTER) == 96

    def test_an_aged_row_is_never_left_pending_nor_paid(self, db_factory):
        """Reviewer probe 2: a row older than settlement's 24 h window stayed
        'pending' with its requester debited.  A ledger row is complete when
        written; settlement never touches it, at any age."""
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100)
        moved = charge_remote_compute(REQUESTER, SERVING_NODE,
                                      _tokens_for_spark(5), 0, source='t')
        with _session(db_factory) as db:
            for r in db.query(MeteredAPIUsage).all():
                r.created_at = datetime.utcnow() - timedelta(hours=25)
        _settle(db_factory)
        assert moved == 5
        assert _spark(db_factory, REQUESTER) == 95
        assert _spark(db_factory, OPERATOR) == 0
        assert [r[6] for r in _rows(db_factory)] == ['debited']

    def test_settlement_still_pays_a_metered_api_row(self, db_factory):
        """Only the compute ledger is excluded: api_cost_recovery rows pay."""
        from integrations.agent_engine.revenue_aggregator import SPARK_PER_USD
        _seed(db_factory)
        with _session(db_factory) as db:
            db.add(MeteredAPIUsage(node_id=SERVING_NODE, operator_id=OPERATOR,
                                   model_id='gpt-4o', task_source='hive',
                                   actual_usd_cost=0.05,
                                   settlement_status='pending'))
        _settle(db_factory)
        assert _spark(db_factory, OPERATOR) == int(0.05 * SPARK_PER_USD)

    def test_fractions_carry_until_a_whole_spark_is_owed(self, db_factory):
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100)
        sixty_percent = int(_tokens_for_spark(1) * 0.6)
        assert charge_remote_compute(REQUESTER, SERVING_NODE, sixty_percent, 0,
                                     source='t') == 0
        assert _spark(db_factory, REQUESTER) == 100
        assert [r[6] for r in _rows(db_factory)] == ['carried']
        assert charge_remote_compute(REQUESTER, SERVING_NODE, sixty_percent, 0,
                                     source='t') == 1
        assert _spark(db_factory, REQUESTER) == 99

    def test_own_node_costs_nothing(self, db_factory):
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100, operator_id=REQUESTER)
        assert charge_remote_compute(REQUESTER, SERVING_NODE,
                                     _tokens_for_spark(5), 0, source='t') == 0
        assert _spark(db_factory, REQUESTER) == 100
        assert _rows(db_factory) == []

    def test_same_user_link_costs_nothing(self, db_factory, monkeypatch):
        from core.peer_link.link import PeerLink, TrustLevel
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100)
        monkeypatch.setenv('HEVOLVE_USER_ID', REQUESTER)
        link = PeerLink(SERVING_NODE, 'peer.example:6777', TrustLevel.SAME_USER)
        mgr = MagicMock()
        mgr.get_link.side_effect = lambda pid: link if pid == SERVING_NODE else None
        monkeypatch.setattr('core.peer_link.link_manager.get_link_manager',
                            lambda: mgr)
        assert charge_remote_compute(REQUESTER, SERVING_NODE,
                                     _tokens_for_spark(5), 0, source='t') == 0
        assert _spark(db_factory, REQUESTER) == 100
        assert _rows(db_factory) == []

    def test_peer_link_is_not_ownership(self, db_factory, monkeypatch):
        """A PEER link to someone else's node proves nothing: charged."""
        from core.peer_link.link import PeerLink, TrustLevel
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100)
        monkeypatch.setenv('HEVOLVE_USER_ID', REQUESTER)
        link = PeerLink(SERVING_NODE, 'peer.example:6777', TrustLevel.PEER)
        mgr = MagicMock()
        mgr.get_link.return_value = link
        monkeypatch.setattr('core.peer_link.link_manager.get_link_manager',
                            lambda: mgr)
        assert charge_remote_compute(REQUESTER, SERVING_NODE,
                                     _tokens_for_spark(2), 0, source='t') == 2

    def test_unknown_operator_moves_nothing(self, db_factory):
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=100)
        assert charge_remote_compute(REQUESTER, 'node-nobody-knows',
                                     _tokens_for_spark(3), 0, source='t') == 0
        assert _spark(db_factory, REQUESTER) == 100
        assert _rows(db_factory) == []

    def test_insufficient_spark_runs_the_work_and_records_it_unfunded(self, db_factory):
        """Wallet semantics (ResonanceService.spend_spark): all or nothing.
        No friction: the work already ran; the row says 'unfunded' and that
        amount is not billed again."""
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory, requester_spark=2)
        assert charge_remote_compute(REQUESTER, SERVING_NODE,
                                     _tokens_for_spark(5), 0, source='t') == 0
        assert _spark(db_factory, REQUESTER) == 2
        assert [r[6] for r in _rows(db_factory)] == ['unfunded']
        assert charge_remote_compute(REQUESTER, SERVING_NODE,
                                     _tokens_for_spark(1), 0, source='t') == 1
        assert _spark(db_factory, REQUESTER) == 1

    def test_nothing_measured_moves_nothing(self, db_factory):
        from integrations.agent_engine.budget_gate import charge_remote_compute
        _seed(db_factory)
        assert charge_remote_compute(REQUESTER, SERVING_NODE, 0, 0, source='t') == 0
        assert charge_remote_compute('', SERVING_NODE, 10 ** 6, 0, source='t') == 0
        assert _rows(db_factory) == []


# ─── the serving node's half ───

@pytest.fixture
def server_db(db_factory, monkeypatch):
    """The same schema seen as the SERVING node: this node is SERVING_NODE,
    operated by OPERATOR."""
    monkeypatch.setenv('HEVOLVE_NODE_ID', SERVING_NODE)
    _seed(db_factory, requester_spark=0)
    return db_factory


class TestCreditServedCompute:

    def test_serving_someone_else_credits_the_operator(self, server_db):
        from integrations.agent_engine.budget_gate import credit_served_compute
        moved = credit_served_compute(REQUESTER, REQUESTER_NODE,
                                      _tokens_for_spark(2), _tokens_for_spark(1),
                                      source='t')
        assert moved == 3
        assert _spark(server_db, OPERATOR) == 3
        assert _rows(server_db) == [
            (REQUESTER, OPERATOR, SERVING_NODE, _tokens_for_spark(2),
             _tokens_for_spark(1), 3, 'credited', 'hive_compute_served')]

    def test_fractions_carry_on_the_server_too(self, server_db):
        from integrations.agent_engine.budget_gate import credit_served_compute
        sixty = int(_tokens_for_spark(1) * 0.6)
        assert credit_served_compute(REQUESTER, REQUESTER_NODE, sixty, 0,
                                     source='t') == 0
        assert credit_served_compute(REQUESTER, REQUESTER_NODE, sixty, 0,
                                     source='t') == 1
        assert _spark(server_db, OPERATOR) == 1

    def test_serving_its_own_operator_earns_nothing(self, server_db):
        from integrations.agent_engine.budget_gate import credit_served_compute
        assert credit_served_compute(OPERATOR, REQUESTER_NODE,
                                     _tokens_for_spark(5), 0, source='t') == 0
        assert _spark(server_db, OPERATOR) == 0
        assert _rows(server_db) == []

    def test_a_same_user_requesting_node_earns_nothing(self, server_db, monkeypatch):
        from core.peer_link.link import PeerLink, TrustLevel
        from integrations.agent_engine.budget_gate import credit_served_compute
        monkeypatch.setenv('HEVOLVE_USER_ID', REQUESTER)
        link = PeerLink(REQUESTER_NODE, 'r.example:6777', TrustLevel.SAME_USER)
        mgr = MagicMock()
        mgr.get_link.side_effect = lambda pid: link if pid == REQUESTER_NODE else None
        monkeypatch.setattr('core.peer_link.link_manager.get_link_manager',
                            lambda: mgr)
        assert credit_served_compute(REQUESTER, REQUESTER_NODE,
                                     _tokens_for_spark(5), 0, source='t') == 0
        assert _rows(server_db) == []

    def test_no_requester_or_no_operator_earns_nothing(self, db_factory, monkeypatch):
        from integrations.agent_engine.budget_gate import credit_served_compute
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-with-no-row')
        _seed(db_factory)
        assert credit_served_compute('', REQUESTER_NODE, 10 ** 6, 0, source='t') == 0
        assert credit_served_compute(REQUESTER, REQUESTER_NODE, 10 ** 6, 0,
                                     source='t') == 0
        assert _rows(db_factory) == []

    def test_served_rows_are_never_settled(self, server_db):
        from integrations.agent_engine.budget_gate import credit_served_compute
        credit_served_compute(REQUESTER, REQUESTER_NODE, _tokens_for_spark(2), 0,
                              source='t')
        _settle(server_db)
        assert _spark(server_db, OPERATOR) == 2


# ─── the wallet debit is one conditional UPDATE ───

class TestSpendSparkIsAtomic:

    def test_a_stale_balance_cannot_spend_twice(self, db_factory):
        """This session read the wallet at 10 Spark; another writer then
        spent it (a raw UPDATE, which leaves this session's copy at 10, as a
        concurrent MySQL transaction would).  The spend must check the
        database, not the stale copy: read-then-write debited 10 again from a
        balance of 0 and reported success."""
        from sqlalchemy import text
        from integrations.social.resonance_engine import ResonanceService
        _seed(db_factory, requester_spark=10)
        db = db_factory()
        try:
            wallet = ResonanceService.get_or_create_wallet(db, REQUESTER)
            assert wallet.spark == 10
            db.execute(text("UPDATE resonance_wallets SET spark = 0 "
                            "WHERE user_id = :u"), {'u': REQUESTER})
            ok, left = ResonanceService.spend_spark(db, REQUESTER, 10, 't')
            db.commit()
        finally:
            db.close()
        assert ok is False and left == 0
        assert _spark(db_factory, REQUESTER) == 0

    def test_a_funded_spend_still_debits_and_logs(self, db_factory):
        from integrations.social.resonance_engine import ResonanceService
        _seed(db_factory, requester_spark=10)
        with _session(db_factory) as db:
            assert ResonanceService.spend_spark(db, REQUESTER, 4, 't') == (True, 6)
        with _session(db_factory) as db:
            txns = db.query(ResonanceTransaction).filter_by(
                user_id=REQUESTER, source_type='t').all()
        assert [(t.amount, t.balance_after) for t in txns] == [(-4, 6)]
        assert _spark(db_factory, REQUESTER) == 6


# ─── the exits and the two halves agree ───

def _hive_expert(peer_id=SERVING_NODE, is_local=False):
    from integrations.agent_engine.model_registry import ModelBackend, ModelTier
    return ModelBackend(
        model_id=f'hive-{peer_id}-big', display_name='Hive: big',
        tier=ModelTier.EXPERT,
        config_list_entry={'model': 'big', 'api_key': 'tok',
                           'base_url': 'https://peer.example/v1',
                           'price': [0, 0], 'peer_id': peer_id},
        is_local=is_local)


def _dispatcher():
    from integrations.agent_engine.model_registry import ModelRegistry
    from integrations.agent_engine.speculative_dispatcher import SpeculativeDispatcher
    return SpeculativeDispatcher(model_registry=ModelRegistry())


class _TwoNodes:
    """The requester's database and the server's, and a switch between them:
    code runs as whichever node the test says it is."""

    def __init__(self, monkeypatch):
        self.req, self.srv = _factory(), _factory()
        self._mp = monkeypatch
        _seed(self.req, requester_spark=1000)
        _seed(self.srv, requester_spark=0)

    def be(self, node):
        factory = self.req if node == REQUESTER_NODE else self.srv
        self._mp.setattr(_models, 'get_db', lambda: factory())
        self._mp.setenv('HEVOLVE_NODE_ID', node)


class TestHiveExpertExit:

    def test_request_names_the_requester(self, db_factory):
        _seed(db_factory, requester_spark=100)
        body = {'choices': [{'message': {'content': 'ok'}}]}
        with patch('requests.post', return_value=_resp(200, body)) as post:
            _dispatcher()._dispatch_expert_langchain(
                _hive_expert(), 'q', REQUESTER, None, 'general', None)
        headers = post.call_args.kwargs['headers']
        assert headers['X-Hart-Requester-User'] == REQUESTER
        assert headers['X-Hart-Requester-Node'] == REQUESTER_NODE

    def test_inflated_usage_cannot_drain_the_requester(self, db_factory):
        """Reviewer probe 1, end to end: 1000 Spark stays 1000."""
        from core.token_utils import count_tokens_for_text
        _seed(db_factory, requester_spark=1000)
        body = {'choices': [{'message': {'content': 'ok'}}],
                'usage': {'prompt_tokens': _tokens_for_spark(900),
                          'completion_tokens': 0}}
        with patch('requests.post', return_value=_resp(200, body)):
            _dispatcher()._dispatch_expert_langchain(
                _hive_expert(), 'q', REQUESTER, None, 'general', None)
        _settle(db_factory)
        assert _spark(db_factory, REQUESTER) == 1000
        assert [(r[3], r[4]) for r in _rows(db_factory)] == [
            (count_tokens_for_text('q'), 0)]

    @pytest.mark.parametrize('resp', [
        _resp(500, {}),
        _resp(200, {'choices': []}),
        _resp(200, {'choices': [{'message': {'content': ''}}],
                    'usage': {'prompt_tokens': 10 ** 7, 'completion_tokens': 0}}),
    ])
    def test_failed_hive_call_charges_nothing(self, db_factory, resp):
        _seed(db_factory, requester_spark=100)
        with patch('requests.post', return_value=resp):
            out = _dispatcher()._dispatch_expert_langchain(
                _hive_expert(), 'q', REQUESTER, None, 'general', None)
        assert out == ''
        assert _spark(db_factory, REQUESTER) == 100
        assert _rows(db_factory) == []

    def test_local_model_charges_nothing(self, db_factory, monkeypatch):
        _seed(db_factory, requester_spark=100)
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
        with patch('requests.post', return_value=_resp(200, {'response': 'local answer'})), \
                patch('integrations.agent_engine.dispatch._internal_auth_headers',
                      return_value={}):
            out = _dispatcher()._dispatch_expert_langchain(
                _hive_expert(is_local=True), 'q', REQUESTER, None, 'general', None)
        assert out == 'local answer'
        assert _spark(db_factory, REQUESTER) == 100
        assert _rows(db_factory) == []

    def test_spend_equals_earn_across_two_nodes(self, monkeypatch):
        """The server half runs INSIDE the requester's HTTP call, on the
        server's own database, fed the exact request the requester sent and
        answering with a usage block that overstates the prompt.  After
        many exchanges the requester's debit equals the operator's credit,
        and neither node wrote the other's wallet."""
        from integrations.agent_engine.budget_gate import credit_served_completion
        nodes = _TwoNodes(monkeypatch)
        fake_mgr = MagicMock()
        fake_mgr.get_link.return_value = None
        monkeypatch.setattr('core.peer_link.link_manager.get_link_manager',
                            lambda: fake_mgr)
        answer = 'x ' * 1400

        def _serve(url, headers=None, json=None, timeout=None):
            reply = {'choices': [{'message': {'content': answer}}],
                     'usage': {'prompt_tokens': 10 ** 7, 'completion_tokens': 1400}}
            nodes.be(SERVING_NODE)
            try:
                credit_served_completion(headers, json, reply)
            finally:
                nodes.be(REQUESTER_NODE)
            return _resp(200, reply)

        nodes.be(REQUESTER_NODE)
        prompt = 'word ' * 60000
        with patch('requests.post', side_effect=_serve):
            for _ in range(12):
                _dispatcher()._dispatch_expert_langchain(
                    _hive_expert(), prompt, REQUESTER, None, 'general', None)
        spent = 1000 - _spark(nodes.req, REQUESTER)
        earned = _spark(nodes.srv, OPERATOR)
        assert spent > 0
        assert spent == earned
        assert _spark(nodes.req, OPERATOR) == 0      # no cross-node credit
        assert [r[3:5] for r in _rows(nodes.req)] == [r[3:5] for r in _rows(nodes.srv)]

    def test_discovery_records_the_serving_peer_on_the_backend(self):
        from integrations.agent_engine.hive_expert_discovery import HiveExpertDiscovery
        from integrations.agent_engine.model_registry import ModelRegistry
        reg = ModelRegistry()
        disc = HiveExpertDiscovery(registry=reg)
        with patch.object(HiveExpertDiscovery, '_verify_peer_trust', return_value=True), \
                patch.object(HiveExpertDiscovery, '_ping_latency', return_value=12.0):
            n = disc.on_peer_announce({
                'peer_id': 'peer-xyz', 'endpoint': 'https://peer.example',
                'models': [{'model_id': 'big', 'tier': 'expert',
                            'verified_baseline': 0.9}]})
        assert n == 1
        backend = reg.get_model('hive-peer-xyz-big')
        assert backend.config_list_entry['peer_id'] == 'peer-xyz'


class TestServedCompletionRoute:

    def test_an_sdk_call_without_the_header_earns_nothing(self, server_db):
        from integrations.agent_engine.budget_gate import credit_served_completion
        req = {'messages': [{'role': 'user', 'content': 'w ' * 99999}],
               'max_tokens': 100}
        reply = {'choices': [{'message': {'content': 'ok'}}]}
        assert credit_served_completion({}, req, reply) == 0
        assert _rows(server_db) == []

    def test_an_empty_reply_earns_nothing(self, server_db):
        from integrations.agent_engine.budget_gate import credit_served_completion
        req = {'messages': [{'role': 'user', 'content': 'w ' * 99999}]}
        headers = {'X-Hart-Requester-User': REQUESTER}
        assert credit_served_completion(
            headers, req, {'choices': [{'message': {'content': ''}}]}) == 0
        assert _rows(server_db) == []


# ─── the exits: compute mesh ───

def _mesh(peer_id=SERVING_NODE):
    import threading
    from integrations.agent_engine.compute_mesh_service import (
        ComputeMeshService, MeshPeer)
    mesh = ComputeMeshService.__new__(ComputeMeshService)
    mesh._lock = threading.Lock()
    mesh._peers = {peer_id: MeshPeer(peer_id, '10.0.0.5', 'k' * 32)}
    mesh._device_id = 'dev-' + peer_id
    mesh.task_relay_port = 6796
    return mesh


class TestComputeMeshExit:

    def test_completed_offload_charges_counted_tokens(self, db_factory):
        from core.token_utils import count_tokens_for_text
        _seed(db_factory, requester_spark=100)
        prompt, answer = 'describe ' * 30, 'a cat ' * 40
        with patch('core.http_pool.pooled_post',
                   return_value=_resp(200, {'response': answer, 'model': 'q'})):
            out = _mesh().offload_inference(
                SERVING_NODE, 'llm', prompt, {'user_id': REQUESTER})
        assert out['response'] == answer
        assert [(r[0], r[1], r[3], r[4]) for r in _rows(db_factory)] == [
            (REQUESTER, OPERATOR, count_tokens_for_text(prompt),
             count_tokens_for_text(answer))]

    def test_inflated_usage_is_capped(self, db_factory):
        from core.token_utils import count_tokens_for_text
        _seed(db_factory, requester_spark=100)
        body = {'response': 'x', 'usage': {'prompt_tokens': _tokens_for_spark(90),
                                           'completion_tokens': 10 ** 6}}
        with patch('core.http_pool.pooled_post', return_value=_resp(200, body)):
            _mesh().offload_inference(SERVING_NODE, 'llm', 'p',
                                      {'user_id': REQUESTER})
        assert _spark(db_factory, REQUESTER) == 100
        from integrations.agent_engine.compute_mesh_service import MESH_INFER_MAX_TOKENS
        assert [(r[3], r[4]) for r in _rows(db_factory)] == [
            (count_tokens_for_text('p'), MESH_INFER_MAX_TOKENS)]

    def test_failed_offload_charges_nothing(self, db_factory):
        _seed(db_factory, requester_spark=100)
        with patch('core.http_pool.pooled_post', return_value=_resp(502, {})):
            out = _mesh().offload_inference(
                SERVING_NODE, 'llm', 'p' * 4000, {'user_id': REQUESTER})
        assert 'error' in out
        assert _rows(db_factory) == []

    def test_error_body_charges_nothing(self, db_factory):
        _seed(db_factory, requester_spark=100)
        with patch('core.http_pool.pooled_post',
                   return_value=_resp(200, {'error': 'Local inference failed'})):
            _mesh().offload_inference(
                SERVING_NODE, 'llm', 'p' * 4000, {'user_id': REQUESTER})
        assert _rows(db_factory) == []

    def test_the_payload_names_the_requester_and_the_cap(self, db_factory):
        from integrations.agent_engine.compute_mesh_service import MESH_INFER_MAX_TOKENS
        _seed(db_factory, requester_spark=100)
        with patch('core.http_pool.pooled_post',
                   return_value=_resp(200, {'response': 'ok'})) as post:
            _mesh().offload_inference(
                SERVING_NODE, 'llm', 'p', {'user_id': REQUESTER, 'timeout': 5})
        sent = post.call_args.kwargs['json']
        assert sent['requester_user_id'] == REQUESTER
        assert sent['requester_node_id'] == REQUESTER_NODE
        assert sent['options'] == {'timeout': 5, 'max_tokens': MESH_INFER_MAX_TOKENS}

    def test_spend_equals_earn_across_two_nodes(self, monkeypatch):
        """The requester's /mesh/infer POST lands on the server's real
        _route_infer, running on the server's own database, whose Model Bus
        answers with an inflated usage block."""
        nodes = _TwoNodes(monkeypatch)
        fake_mgr = MagicMock()
        fake_mgr.get_link.return_value = None
        monkeypatch.setattr('core.peer_link.link_manager.get_link_manager',
                            lambda: fake_mgr)
        server = _mesh(REQUESTER_NODE)
        server._compute_contribute_consented = lambda: True
        answer = 'y ' * 900

        def _post(url, json=None, timeout=None, **kw):
            if url.endswith('/v1/chat'):          # the server's own Model Bus
                return _resp(200, {'response': answer, 'model': 'q',
                                   'usage': {'prompt_tokens': 10 ** 7,
                                             'completion_tokens': 10 ** 7}})
            nodes.be(SERVING_NODE)
            try:
                status, _ct, out = server._route_infer(
                    __import__('json').dumps(json).encode('utf-8'))
            finally:
                nodes.be(REQUESTER_NODE)
            return _resp(status, __import__('json').loads(out))

        nodes.be(REQUESTER_NODE)
        prompt = 'word ' * 80000
        with patch('core.http_pool.pooled_post', side_effect=_post):
            for _ in range(10):
                _mesh().offload_inference(SERVING_NODE, 'llm', prompt,
                                          {'user_id': REQUESTER})
        spent = 1000 - _spark(nodes.req, REQUESTER)
        earned = _spark(nodes.srv, OPERATOR)
        assert spent > 0
        assert spent == earned
        assert [r[3:5] for r in _rows(nodes.req)] == [r[3:5] for r in _rows(nodes.srv)]


class TestMeshCallersNameTheRequester:
    """A mesh offload is charged to options['user_id']; a caller that knows
    the user must pass it, or the work runs on someone's node for free."""

    def test_generate_video_ltx2_offload_names_the_user(self, monkeypatch):
        from tests.unit.test_core_tools_uuid_user_id import _ctx, _tool, UUID_USER

        def _down(*a, **k):
            raise OSError('local LTX-2 / ComfyUI not running')
        monkeypatch.setattr('core.agent_tools.pooled_get', _down)
        monkeypatch.setattr(
            'integrations.agent_engine.compute_config.get_compute_policy',
            lambda *a, **k: {'compute_policy': 'any'})
        mesh = MagicMock()
        mesh.offload_to_best_peer.return_value = {
            'response': 'http://peer/v.mp4', 'offloaded_to': SERVING_NODE}
        monkeypatch.setattr(
            'integrations.agent_engine.compute_mesh_service.get_compute_mesh',
            lambda: mesh)
        out = _tool(_ctx(UUID_USER), 'Generate_video')('a cat', 0, True, 'ltx2')
        assert 'hive peer' in out
        opts = mesh.offload_to_best_peer.call_args.kwargs['options']
        assert opts['user_id'] == UUID_USER

    def test_parse_visual_context_offload_names_the_user(self, tmp_path,
                                                         monkeypatch):
        """hart_intelligence_entry cannot be imported from source here, so the
        real function is compiled out of the file and run with its module
        globals stubbed (the pattern of test_liquid_ui_entry_emitters_reach_
        service).  Local VLM tiers are down, so the turn reaches the mesh."""
        import ast
        import logging
        import numpy as np
        from pathlib import Path
        from PIL import Image
        entry = Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py'
        tree = ast.parse(entry.read_text(encoding='utf-8'))
        fn = [n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == 'parse_visual_context']
        assert len(fn) == 1
        mesh = MagicMock()
        mesh.offload_to_best_peer.return_value = {'response': 'a desk'}
        monkeypatch.setattr(
            'integrations.agent_engine.compute_mesh_service.get_compute_mesh',
            lambda: mesh)
        tl = MagicMock()
        tl.get_user_id.return_value = 'u-vision'
        tl.get_request_id.return_value = 'r1'
        down = MagicMock()
        down.post.side_effect = OSError('no local VLM')
        ns = {'thread_local_data': tl, 'app': MagicMock(), 'os': os,
              'Image': Image, 'requests': down, 'logging': logging,
              'GPT_API': 'http://127.0.0.1:1/v1/chat/completions',
              'LLM_MODEL_NAME': 'm', 'LLM_AUTH_HEADERS': {},
              'get_frame': lambda uid: np.zeros((4, 4, 3), dtype=np.uint8)}
        exec(compile(ast.Module(body=fn, type_ignores=[]), str(entry), 'exec'), ns)
        monkeypatch.chdir(tmp_path)
        assert ns['parse_visual_context']('what is on my desk?') == 'a desk'
        opts = mesh.offload_to_best_peer.call_args.kwargs['options']
        assert opts['user_id'] == 'u-vision'


# ─── the node id is the gossip id every PeerNode row is keyed by ───

class TestCanonicalNodeId:

    def test_unset_env_uses_the_gossip_id(self, monkeypatch):
        from integrations.agent_engine.budget_gate import _this_node_id
        from integrations.agent_engine.hive_capability_advertiser import _local_peer_id
        monkeypatch.delenv('HEVOLVE_NODE_ID', raising=False)
        monkeypatch.setattr(
            'integrations.social.sync_engine.SyncEngine.canonical_node_id',
            staticmethod(lambda: 'gossip-uuid-1'))
        assert _this_node_id() == 'gossip-uuid-1'
        assert _local_peer_id() == 'gossip-uuid-1'

    def test_an_explicit_env_still_wins(self, monkeypatch):
        from integrations.agent_engine.budget_gate import _this_node_id
        monkeypatch.setenv('HEVOLVE_NODE_ID', 'node-prod-7')
        monkeypatch.setattr(
            'integrations.social.sync_engine.SyncEngine.canonical_node_id',
            staticmethod(lambda: 'gossip-uuid-1'))
        assert _this_node_id() == 'node-prod-7'

    def test_discovery_skips_its_own_gossip_id_announce(self, monkeypatch):
        from integrations.agent_engine.hive_expert_discovery import HiveExpertDiscovery
        from integrations.agent_engine.model_registry import ModelRegistry
        monkeypatch.delenv('HEVOLVE_NODE_ID', raising=False)
        monkeypatch.setattr(
            'integrations.social.sync_engine.SyncEngine.canonical_node_id',
            staticmethod(lambda: 'gossip-uuid-1'))
        reg = ModelRegistry()
        disc = HiveExpertDiscovery(registry=reg)
        with patch.object(HiveExpertDiscovery, '_verify_peer_trust', return_value=True), \
                patch.object(HiveExpertDiscovery, '_ping_latency', return_value=12.0):
            n = disc.on_peer_announce({
                'peer_id': 'gossip-uuid-1', 'endpoint': 'https://me.example',
                'models': [{'model_id': 'big', 'tier': 'expert',
                            'verified_baseline': 0.9}]})
        assert n == 0
        assert reg.get_model('hive-gossip-uuid-1-big') is None


# ─── migration v57: plain DDL behind an existence check ───

class TestMigrationV57:

    def _engine_without_column(self, tmp_path):
        from sqlalchemy import text
        from integrations.social import migrations as mig
        engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
        mig.Base.metadata.create_all(engine)
        with engine.connect() as conn:
            conn.execute(text("DROP INDEX IF EXISTS "
                              "ix_metered_api_usage_requester_user_id"))
            conn.execute(text("ALTER TABLE metered_api_usage "
                              "DROP COLUMN requester_user_id"))
            conn.commit()
        mig.set_schema_version(engine, 56)
        return engine

    def test_adds_column_and_index_with_plain_ddl(self, tmp_path, monkeypatch):
        from sqlalchemy import event, inspect as sa_inspect
        from integrations.social import migrations as mig
        engine = self._engine_without_column(tmp_path)
        sent = []
        event.listen(engine, 'before_cursor_execute',
                     lambda c, cur, stmt, *a: sent.append(stmt))
        monkeypatch.setattr(mig, 'get_engine', lambda: engine)
        monkeypatch.setattr(mig.Base.metadata, 'create_all', lambda *a, **k: None)
        mig.run_migrations()
        insp = sa_inspect(engine)
        assert 'requester_user_id' in {c['name'] for c in insp.get_columns('metered_api_usage')}
        assert 'ix_metered_api_usage_requester_user_id' in {
            i['name'] for i in insp.get_indexes('metered_api_usage')}
        assert mig.get_schema_version(engine) == mig.SCHEMA_VERSION
        v57 = [s for s in sent if 'requester_user_id' in s]
        assert v57 and not [s for s in v57 if 'IF NOT EXISTS' in s.upper()]

    def test_a_failed_pass_is_not_stamped_done(self, tmp_path, monkeypatch):
        from integrations.social import migrations as mig
        engine = self._engine_without_column(tmp_path)
        monkeypatch.setattr(mig, 'get_engine', lambda: engine)
        monkeypatch.setattr(mig.Base.metadata, 'create_all', lambda *a, **k: None)
        monkeypatch.setattr(mig, '_v57_requester_user_id', lambda e: False)
        mig.run_migrations()
        assert mig.get_schema_version(engine) == 56

    def test_a_second_pass_is_a_no_op(self, tmp_path, monkeypatch):
        from sqlalchemy import event
        from integrations.social import migrations as mig
        engine = self._engine_without_column(tmp_path)
        assert mig._v57_requester_user_id(engine) is True
        sent = []
        event.listen(engine, 'before_cursor_execute',
                     lambda c, cur, stmt, *a: sent.append(stmt))
        assert mig._v57_requester_user_id(engine) is True
        assert not [s for s in sent if s.lstrip().upper().startswith(('ALTER', 'CREATE'))]


# ─── settlement (metered API rows) has a scheduled caller ───

def _pending_api_row(factory):
    with _session(factory) as db:
        db.add(MeteredAPIUsage(node_id=SERVING_NODE, operator_id=OPERATOR,
                               model_id='gpt-4o', task_source='hive',
                               actual_usd_cost=0.03, settlement_status='pending'))


class TestScheduledSettlement:

    def test_daemon_tick_settles_pending_rows(self, db_factory, monkeypatch):
        from integrations.agent_engine.agent_daemon import AgentDaemon
        _seed(db_factory)
        _pending_api_row(db_factory)
        monkeypatch.setattr('integrations.agent_engine.dispatch.should_yield_to_user',
                            lambda: False)
        d = AgentDaemon()
        d._tick_count = d._remediate_every - 1   # this tick is a settlement tick
        d._tick()                                 # no goals: returns after settling
        assert _spark(db_factory, OPERATOR) == 3

    def test_off_cadence_tick_does_not_settle(self, db_factory, monkeypatch):
        from integrations.agent_engine.agent_daemon import AgentDaemon
        _seed(db_factory)
        _pending_api_row(db_factory)
        monkeypatch.setattr('integrations.agent_engine.dispatch.should_yield_to_user',
                            lambda: False)
        d = AgentDaemon()
        d._tick_count = 0
        d._tick()
        assert _spark(db_factory, OPERATOR) == 0


# ─── /api/gateway/metering reads columns that exist ───

class TestMeteringByModel:

    def test_groups_real_columns(self, db_factory):
        from integrations.agent_engine.budget_gate import metered_usage_by_model
        with _session(db_factory) as db:
            for m, tin, tout in [('a', 10, 5), ('a', 1, 1), ('b', 7, 0)]:
                db.add(MeteredAPIUsage(node_id='n', model_id=m, task_source='hive',
                                       tokens_in=tin, tokens_out=tout))
        with _session(db_factory) as db:
            got = sorted(metered_usage_by_model(db), key=lambda r: r['provider'])
        assert got == [
            {'provider': 'a', 'total_tokens': 17, 'calls': 2},
            {'provider': 'b', 'total_tokens': 7, 'calls': 1},
        ]
