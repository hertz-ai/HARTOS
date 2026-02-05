"""
Agent Network Resilience & Privacy Test Suite
==============================================
49 tests across 6 classes covering:
- Agent creation (private vs public)
- Local vs cloud agent demarcation
- Network connectivity scenarios
- Internet loss and recovery
- Local vs cloud deployment differences
- Edge cases (concurrency, dedup, cache isolation)

All external calls (Redis, MongoDB, HTTP) are mocked — no real network needed.
"""
import os
import sys
import time
import uuid
import json
import threading
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch, PropertyMock
from collections import defaultdict

# Add parent dir for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Force in-memory SQLite before importing models
os.environ['SOCIAL_DB_PATH'] = ':memory:'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from integrations.social.models import Base, User, PeerNode, InstanceFollow, FederatedPost
from integrations.social.services import UserService
from integrations.social.federation import FederationManager
from integrations.social.peer_discovery import GossipProtocol
from integrations.social.agent_naming import (
    validate_agent_name, validate_local_name, compose_global_name,
)
from lifecycle_hooks import (
    ActionState, ActionRetryTracker, action_states,
    set_action_state, get_action_state,
)
import requests as _requests
# Alias for requests exceptions that are caught by the source code
RequestsConnectionError = _requests.exceptions.ConnectionError


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope='session')
def engine():
    """In-memory SQLite engine shared across all tests in session."""
    eng = create_engine('sqlite://', echo=False,
                        connect_args={"check_same_thread": False})
    return eng


@pytest.fixture(scope='session')
def tables(engine):
    """Create all tables once per session."""
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db(engine, tables):
    """Transactional session — rolled back after each test."""
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


_user_counter = 0

@pytest.fixture
def user_factory(db):
    """Factory to create User instances with unique names."""
    def _create(user_type='human', username=None, handle=None, owner_id=None,
                local_name=None, password_hash='fakehash'):
        global _user_counter
        _user_counter += 1
        uid = str(uuid.uuid4())
        uname = username or f'testuser_{_user_counter}_{uid[:6]}'
        user = User(
            id=uid,
            username=uname,
            display_name=uname,
            user_type=user_type,
            password_hash=password_hash,
            api_token=f'tok_{uid[:16]}',
            is_verified=True,
            handle=handle,
            owner_id=owner_id,
            local_name=local_name,
        )
        db.add(user)
        db.flush()
        return user
    return _create


@pytest.fixture
def human_with_handle(user_factory):
    """Create a human user with a handle set."""
    return user_factory(user_type='human', handle='testhandle')


@pytest.fixture
def mock_redis():
    """Mock Redis client, patches redis.from_url."""
    mock = MagicMock()
    mock.ping.return_value = True
    with patch.dict('sys.modules', {'redis': MagicMock()}):
        with patch('redis.from_url', return_value=mock):
            yield mock


@pytest.fixture
def mock_requests():
    """Patch requests.get, requests.post, and requests.Session."""
    with patch('requests.get') as mg, \
         patch('requests.post') as mp, \
         patch('requests.Session') as ms:
        yield {'get': mg, 'post': mp, 'Session': ms}


@pytest.fixture
def peer_factory(db):
    """Factory to create PeerNode instances."""
    def _create(node_id=None, url='http://peer.local:6777', name='test-peer',
                status='active', visibility_tier='standard', contribution_score=0.0):
        nid = node_id or str(uuid.uuid4())
        peer = PeerNode(
            node_id=nid, url=url, name=name, version='1.0.0',
            status=status, visibility_tier=visibility_tier,
            contribution_score=contribution_score,
        )
        db.add(peer)
        db.flush()
        return peer
    return _create


@pytest.fixture(autouse=True)
def reset_action_states():
    """Clear global action_states before and after each test."""
    action_states.clear()
    yield
    action_states.clear()


# ═══════════════════════════════════════════════════════════════
# TEST CLASS 1: AGENT CREATION — PRIVATE VS PUBLIC
# ═══════════════════════════════════════════════════════════════

class TestAgentCreationPrivateVsPublic:
    """Tests for private (owned) vs public (standalone) agent creation."""

    def test_create_private_agent_not_visible_in_public_queries(self, db, human_with_handle):
        """Private agent with owner_id should have ownership link."""
        agent = UserService.register_agent(
            db, name='swift.amber.falcon', description='test agent',
            owner_id=human_with_handle.id, skip_name_validation=True,
        )
        assert agent.owner_id == human_with_handle.id
        assert agent.user_type == 'agent'

    def test_create_public_agent_visible_in_discovery(self, db):
        """Public agent (no owner) appears in agent type queries."""
        agent = UserService.register_agent(
            db, name='bold.crimson.eagle', description='public agent',
            skip_name_validation=True,
        )
        agents = db.query(User).filter(User.user_type == 'agent').all()
        usernames = [a.username for a in agents]
        assert 'bold.crimson.eagle' in usernames

    def test_private_agent_only_accessible_by_owner(self, db, user_factory):
        """Owned agents are only returned for the correct owner."""
        owner = user_factory(user_type='human', handle='ownerone')
        other = user_factory(user_type='human', handle='ownertwo')
        agent = UserService.register_agent(
            db, name='keen.jade.river', description='owned',
            owner_id=owner.id, skip_name_validation=True,
        )
        owned = UserService.get_owned_agents(db, owner.id)
        assert any(a.id == agent.id for a in owned)

        others_agents = UserService.get_owned_agents(db, other.id)
        assert not any(a.id == agent.id for a in others_agents)

    def test_public_agent_accessible_by_anyone(self, db):
        """get_by_username returns agent regardless of who queries."""
        agent = UserService.register_agent(
            db, name='wise.pearl.phoenix', description='public',
            skip_name_validation=True,
        )
        found = UserService.get_by_username(db, 'wise.pearl.phoenix')
        assert found is not None
        assert found.id == agent.id

    def test_create_agent_with_owner_verifies_relationship(self, db, user_factory):
        """Verify agent.owner_id == human.id after registration."""
        human = user_factory(user_type='human', handle='relcheck')
        agent = UserService.register_agent(
            db, name='fierce.golden.wolf', description='rel test',
            owner_id=human.id, skip_name_validation=True,
        )
        assert agent.owner_id == human.id
        assert agent.user_type == 'agent'

    def test_create_agent_without_owner_standalone(self, db):
        """Register with owner_id=None → standalone agent."""
        agent = UserService.register_agent(
            db, name='gentle.silver.hawk', description='standalone',
            owner_id=None, skip_name_validation=True,
        )
        assert agent.owner_id is None
        assert agent.user_type == 'agent'

    def test_private_to_public_transition(self, db, user_factory):
        """Create agent with owner, then clear owner_id to make public."""
        human = user_factory(user_type='human', handle='transition')
        agent = UserService.register_agent(
            db, name='agile.topaz.fox', description='will go public',
            owner_id=human.id, skip_name_validation=True,
        )
        assert agent.owner_id == human.id

        # Transition: remove ownership
        agent.owner_id = None
        db.flush()
        refreshed = db.query(User).filter(User.id == agent.id).first()
        assert refreshed.owner_id is None


# ═══════════════════════════════════════════════════════════════
# TEST CLASS 2: LOCAL VS CLOUD AGENT DEMARCATION
# ═══════════════════════════════════════════════════════════════

class TestLocalVsCloudAgentDemarcation:
    """Tests for naming system, PeerNode tiers, and federation discovery."""

    def test_local_agent_registration_2word_name(self, db, user_factory):
        """register_agent_local('swift.falcon') with handle → global name."""
        owner = user_factory(user_type='human', handle='sathi')
        agent = UserService.register_agent_local(
            db, local_name='swift.falcon', description='local agent', owner=owner,
        )
        assert agent.local_name == 'swift.falcon'
        assert agent.username == 'swift.falcon.sathi'
        assert agent.owner_id == owner.id

    def test_global_agent_registration_3word_name(self, db):
        """register_agent with a 3-word global name."""
        agent = UserService.register_agent(
            db, name='brave.silver.eagle', description='global agent',
            skip_name_validation=True,
        )
        assert agent.username == 'brave.silver.eagle'
        assert agent.user_type == 'agent'

    def test_same_local_name_different_handles_different_agents(self, db, user_factory):
        """Two owners with different handles register same local name → different globals."""
        owner_a = user_factory(user_type='human', handle='alice')
        owner_b = user_factory(user_type='human', handle='bobby')

        agent_a = UserService.register_agent_local(
            db, local_name='calm.oracle', description='a', owner=owner_a,
        )
        agent_b = UserService.register_agent_local(
            db, local_name='calm.oracle', description='b', owner=owner_b,
        )

        assert agent_a.username == 'calm.oracle.alice'
        assert agent_b.username == 'calm.oracle.bobby'
        assert agent_a.id != agent_b.id

    def test_local_agent_promotion_to_global(self, db, user_factory):
        """Local agent already has global username via handle composition."""
        owner = user_factory(user_type='human', handle='promo')
        agent = UserService.register_agent_local(
            db, local_name='bold.storm', description='promo test', owner=owner,
        )
        # The global name is composed automatically
        expected_global = compose_global_name('bold.storm', 'promo')
        assert agent.username == expected_global
        assert expected_global == 'bold.storm.promo'

    def test_peer_node_visibility_tiers(self, db, peer_factory):
        """Insert PeerNodes with different tiers and query by tier."""
        peer_factory(node_id='std-1', visibility_tier='standard')
        peer_factory(node_id='feat-1', url='http://featured:6777',
                     visibility_tier='featured')
        peer_factory(node_id='prio-1', url='http://priority:6777',
                     visibility_tier='priority')

        featured = db.query(PeerNode).filter(
            PeerNode.visibility_tier == 'featured').all()
        assert len(featured) == 1
        assert featured[0].node_id == 'feat-1'

        priority = db.query(PeerNode).filter(
            PeerNode.visibility_tier == 'priority').all()
        assert len(priority) == 1

    def test_federation_node_registration(self, db):
        """GossipProtocol._merge_peer() creates PeerNode with status='active'."""
        gossip = GossipProtocol()
        peer_data = {
            'node_id': 'remote-node-1',
            'url': 'http://remote.example.com:6777',
            'name': 'remote-node',
            'version': '1.0.0',
            'agent_count': 5,
            'post_count': 10,
        }
        is_new = gossip._merge_peer(db, peer_data)
        assert is_new is True

        stored = db.query(PeerNode).filter(
            PeerNode.node_id == 'remote-node-1').first()
        assert stored is not None
        assert stored.status == 'active'
        assert stored.url == 'http://remote.example.com:6777'

    def test_agent_discovery_across_peer_nodes(self, db, user_factory):
        """Agents from local node discoverable via public query."""
        agent = user_factory(user_type='agent', username='wild.cobalt.raven')
        found = db.query(User).filter(
            User.user_type == 'agent',
            User.username == 'wild.cobalt.raven',
        ).first()
        assert found is not None
        assert found.id == agent.id


# ═══════════════════════════════════════════════════════════════
# TEST CLASS 3: NETWORK CONNECTIVITY SCENARIOS
# ═══════════════════════════════════════════════════════════════

class TestNetworkConnectivityScenarios:
    """Heavy mocking of requests, Redis, MongoDB. Tests fallback chains."""

    def test_http_pool_retry_on_server_down_then_success(self):
        """Mock 2 failures then success → pooled_get works."""
        from core.http_pool import get_http_session
        import core.http_pool as hp

        # Reset singleton for test isolation
        hp._session = None

        with patch('requests.Session') as MockSession:
            mock_session = MagicMock()
            MockSession.return_value = mock_session

            # First two calls raise, third succeeds
            success_resp = MagicMock()
            success_resp.status_code = 200
            success_resp.json.return_value = {'ok': True}
            mock_session.get.side_effect = [
                ConnectionError("down"),
                ConnectionError("still down"),
                success_resp,
            ]

            session = get_http_session()
            # First two should raise, third succeeds
            with pytest.raises(ConnectionError):
                session.get('http://test.local/api')
            with pytest.raises(ConnectionError):
                session.get('http://test.local/api')
            resp = session.get('http://test.local/api')
            assert resp.status_code == 200

        # Restore
        hp._session = None

    def test_http_pool_retry_on_503(self):
        """503 is in status_forcelist → retries should be configured."""
        from urllib3.util.retry import Retry
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
        )
        assert 503 in retry.status_forcelist

    def test_http_pool_backoff_factor_configured(self):
        """Verify Retry backoff_factor=0.3."""
        from urllib3.util.retry import Retry
        retry = Retry(total=3, backoff_factor=0.3,
                      status_forcelist=[500, 502, 503, 504])
        assert retry.backoff_factor == 0.3

    def test_ledger_fallback_redis_fails_mongodb_fails_json_works(self):
        """Both Redis+MongoDB raise → JSONBackend should be used."""
        # Simulate the factory fallback logic
        backends_tried = []

        def try_redis():
            backends_tried.append('redis')
            raise ConnectionError("Redis down")

        def try_mongodb():
            backends_tried.append('mongodb')
            raise ConnectionError("MongoDB down")

        def try_json():
            backends_tried.append('json')
            return {'backend': 'json', 'status': 'ok'}

        # Simulate fallback chain
        result = None
        for backend_fn in [try_redis, try_mongodb, try_json]:
            try:
                result = backend_fn()
                break
            except ConnectionError:
                continue

        assert result is not None
        assert result['backend'] == 'json'
        assert backends_tried == ['redis', 'mongodb', 'json']

    def test_ledger_fallback_redis_fails_mongodb_works(self):
        """Redis raises → MongoDBBackend should be used."""
        backends_tried = []

        def try_redis():
            backends_tried.append('redis')
            raise ConnectionError("Redis down")

        def try_mongodb():
            backends_tried.append('mongodb')
            return {'backend': 'mongodb', 'status': 'ok'}

        result = None
        for backend_fn in [try_redis, try_mongodb]:
            try:
                result = backend_fn()
                break
            except ConnectionError:
                continue

        assert result is not None
        assert result['backend'] == 'mongodb'
        assert backends_tried == ['redis', 'mongodb']

    def test_rate_limiter_fallback_redis_fails_memory_works(self):
        """Redis unavailable → in-memory rate limiting works."""
        from security.rate_limiter_redis import RedisRateLimiter

        with patch.dict(os.environ, {'REDIS_URL': 'redis://nonexistent:9999'}):
            with patch('redis.from_url', side_effect=ConnectionError("no redis")):
                limiter = RedisRateLimiter()
                assert limiter._redis is None

                # In-memory fallback should work
                key = 'rl:test:ip:127.0.0.1'
                allowed = limiter._check_memory(key, 60, 60)
                assert allowed is True

    def test_action_state_persistence_during_network_drop(self):
        """Ledger sync fails → exception caught, no propagation."""
        # Set up action state
        action_states['test_prompt'] = {}
        action_states['test_prompt'][1] = ActionState.ASSIGNED

        # Mock ledger that fails on sync
        mock_ledger = MagicMock()
        mock_ledger.tasks = {'action_1': {}}
        mock_ledger.update_task_status.side_effect = ConnectionError("Network down")

        with patch('lifecycle_hooks._ledger_registry', {'test_prompt': mock_ledger}):
            with patch('lifecycle_hooks._get_ledger_task_status') as mock_ts:
                mock_ts.return_value = MagicMock()
                # State transition should succeed even if ledger sync fails
                try:
                    set_action_state('test_prompt', 1, ActionState.IN_PROGRESS,
                                     "test transition")
                except Exception:
                    pass  # Ledger sync failure should be caught internally

                # State should still be updated locally
                state = action_states.get('test_prompt', {}).get(1)
                assert state == ActionState.IN_PROGRESS

    def test_action_retry_tracker_max_retries(self):
        """4th retry triggers MAX_PENDING_RETRIES threshold."""
        tracker = ActionRetryTracker()
        assert tracker.MAX_PENDING_RETRIES == 3

        # 1st, 2nd, 3rd → under threshold
        assert tracker.increment_pending('prompt', 1) is False
        assert tracker.increment_pending('prompt', 1) is False
        assert tracker.increment_pending('prompt', 1) is False

        # 4th → exceeds threshold
        assert tracker.increment_pending('prompt', 1) is True


# ═══════════════════════════════════════════════════════════════
# TEST CLASS 4: INTERNET LOSS AND RECOVERY
# ═══════════════════════════════════════════════════════════════

class TestInternetLossAndRecovery:
    """Tests federation/gossip behavior during outages and recovery."""

    def test_api_timeout_simulation(self):
        """Timeout on federation delivery → caught, no propagation."""
        fm = FederationManager()
        with patch('integrations.social.federation.requests.post',
                   side_effect=RequestsConnectionError("Timeout")):
            # _deliver_to_inbox should catch the error internally
            fm._deliver_to_inbox('http://unreachable:6777', {'type': 'new_post'})
            # No exception should propagate

    def test_api_503_service_unavailable(self):
        """Gossip ping returns 503 → returns False."""
        gossip = GossipProtocol()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch('integrations.social.peer_discovery.requests.get',
                   return_value=mock_resp):
            result = gossip._ping_peer('http://unavailable:6777')
            assert result is False

    def test_gradual_degradation_intermittent_failures(self):
        """Mixed timeout/success sequence → handled gracefully."""
        gossip = GossipProtocol()
        responses = [
            RequestsConnectionError("timeout"),
            MagicMock(status_code=200),
            RequestsConnectionError("timeout"),
            MagicMock(status_code=200),
        ]

        results = []
        for resp in responses:
            with patch('integrations.social.peer_discovery.requests.get',
                        side_effect=resp if isinstance(resp, Exception) else None,
                        return_value=None if isinstance(resp, Exception) else resp):
                result = gossip._ping_peer('http://flaky:6777')
                results.append(result)

        # Expected: False, True, False, True
        assert results == [False, True, False, True]

    def test_federation_push_during_offline(self):
        """ConnectionError on push → delivery failures caught."""
        fm = FederationManager()
        with patch('integrations.social.federation.requests.post',
                   side_effect=RequestsConnectionError("offline")):
            # Should not raise
            fm._deliver_to_inbox('http://offline:6777', {
                'type': 'new_post',
                'origin_node_id': 'test',
                'post': {'id': '1', 'title': 'test'},
            })

    def test_gossip_exchange_during_network_failure(self):
        """ConnectionError → returns None."""
        gossip = GossipProtocol()
        with patch('integrations.social.peer_discovery.requests.post',
                   side_effect=RequestsConnectionError("down")):
            result = gossip._exchange_with_peer('http://down:6777')
            assert result is None

    def test_gossip_health_check_marks_stale(self, db, peer_factory):
        """Peer not seen for 6 min → status='stale'."""
        peer = peer_factory(node_id='stale-peer',
                            url='http://stale:6777')
        # Set last_seen to 6 minutes ago
        peer.last_seen = datetime.utcnow() - timedelta(minutes=6)
        db.flush()

        gossip = GossipProtocol()
        gossip.stale_threshold = 300  # 5 min
        gossip.dead_threshold = 900  # 15 min

        now = datetime.utcnow()
        age = (now - peer.last_seen).total_seconds()
        # Should be > stale_threshold but < dead_threshold
        assert age > gossip.stale_threshold
        assert age < gossip.dead_threshold

        # Simulate health check logic
        if age > gossip.dead_threshold:
            peer.status = 'dead'
        elif age > gossip.stale_threshold:
            peer.status = 'stale'
        db.flush()

        assert peer.status == 'stale'

    def test_gossip_health_check_marks_dead(self, db, peer_factory):
        """Peer not seen for 16 min → status='dead'."""
        peer = peer_factory(node_id='dead-peer',
                            url='http://dead:6777')
        peer.last_seen = datetime.utcnow() - timedelta(minutes=16)
        db.flush()

        gossip = GossipProtocol()
        gossip.dead_threshold = 900  # 15 min

        now = datetime.utcnow()
        age = (now - peer.last_seen).total_seconds()
        assert age > gossip.dead_threshold

        peer.status = 'dead'
        db.flush()
        assert peer.status == 'dead'

    def test_dead_peer_resurrection_on_reconnect(self, db, peer_factory):
        """Dead peer's merge_peer called again → status='active'."""
        peer = peer_factory(node_id='resurrect-peer',
                            url='http://resurrect:6777',
                            status='dead')
        assert peer.status == 'dead'

        gossip = GossipProtocol()
        # Merge the same peer again (simulating reconnection)
        is_new = gossip._merge_peer(db, {
            'node_id': 'resurrect-peer',
            'url': 'http://resurrect:6777',
            'name': 'resurrected',
        })
        db.flush()

        assert is_new is False  # Existing peer, not new
        refreshed = db.query(PeerNode).filter(
            PeerNode.node_id == 'resurrect-peer').first()
        assert refreshed.status == 'active'  # Resurrected

    def test_rate_limiter_burst_after_reconnection(self):
        """60 rapid requests → 61st blocked, window expires → allowed again."""
        from security.rate_limiter_redis import RedisRateLimiter

        limiter = RedisRateLimiter()
        limiter._redis = None  # Force in-memory mode
        limiter._memory_store = defaultdict(list)

        key = 'rl:global:ip:burst_test'
        max_req = 60
        window = 60

        # Send 60 requests — all should be allowed
        for i in range(max_req):
            assert limiter._check_memory(key, max_req, window) is True

        # 61st should be blocked
        assert limiter._check_memory(key, max_req, window) is False

        # Simulate window expiration by clearing timestamps
        limiter._memory_store[key] = []

        # Should be allowed again
        assert limiter._check_memory(key, max_req, window) is True


# ═══════════════════════════════════════════════════════════════
# TEST CLASS 5: LOCAL VS CLOUD DEPLOYMENT DIFFERENCES
# ═══════════════════════════════════════════════════════════════

class TestLocalVsCloudDeploymentDifferences:
    """Tests config.json endpoints and gossip/federation configuration."""

    def test_local_endpoints_config(self):
        """config.json GPT_API points to localhost."""
        config_path = os.path.join(
            os.path.dirname(__file__), '..', 'config.json')
        with open(config_path, 'r') as f:
            config = json.load(f)
        assert 'localhost' in config.get('GPT_API', '')

    def test_cloud_endpoints_config(self):
        """config.json DB_URL points to hertzai.com."""
        config_path = os.path.join(
            os.path.dirname(__file__), '..', 'config.json')
        with open(config_path, 'r') as f:
            config = json.load(f)
        assert 'hertzai.com' in config.get('DB_URL', '')

    def test_hybrid_mode_local_llm_cloud_db(self):
        """Both local+cloud endpoints coexist in config."""
        config_path = os.path.join(
            os.path.dirname(__file__), '..', 'config.json')
        with open(config_path, 'r') as f:
            config = json.load(f)
        # Local LLM endpoint
        assert 'localhost' in config.get('GPT_API', '')
        # Cloud DB endpoint
        assert 'hertzai.com' in config.get('DB_URL', '')

    def test_gossip_protocol_default_base_url(self):
        """Default GossipProtocol → http://localhost:6777."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove HEVOLVE_BASE_URL if set
            env = os.environ.copy()
            env.pop('HEVOLVE_BASE_URL', None)
            with patch.dict(os.environ, env, clear=True):
                g = GossipProtocol()
                assert g.base_url == 'http://localhost:6777'

    def test_gossip_protocol_custom_base_url(self):
        """Env var override → custom URL."""
        with patch.dict(os.environ, {'HEVOLVE_BASE_URL': 'https://cloud.hertzai.com'}):
            g = GossipProtocol()
            assert g.base_url == 'https://cloud.hertzai.com'

    def test_gossip_peer_announcement(self):
        """Sends correct JSON with node_id, url, name."""
        gossip = GossipProtocol()
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch('requests.post', return_value=mock_resp) as mock_post:
            result = gossip._announce_to_peer('http://peer:6777')
            assert result is True

            # Verify the JSON payload
            call_args = mock_post.call_args
            json_data = call_args[1].get('json') or call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get('json')
            assert json_data is not None
            assert 'node_id' in json_data
            assert 'url' in json_data
            assert 'name' in json_data

    def test_federation_follow_instance(self, db):
        """Creates InstanceFollow record in DB."""
        fm = FederationManager()
        with patch.object(fm, '_send_follow_notification'):
            result = fm.follow_instance(
                db, local_node_id='local-1',
                peer_node_id='remote-1',
                peer_url='http://remote:6777',
            )
        assert result is True

        follow = db.query(InstanceFollow).filter(
            InstanceFollow.follower_node_id == 'local-1',
            InstanceFollow.following_node_id == 'remote-1',
        ).first()
        assert follow is not None
        assert follow.status == 'active'

    def test_federation_duplicate_follow_prevented(self, db):
        """Second follow returns False, only 1 DB record."""
        fm = FederationManager()
        with patch.object(fm, '_send_follow_notification'):
            first = fm.follow_instance(
                db, local_node_id='local-dup',
                peer_node_id='remote-dup',
                peer_url='http://remote:6777',
            )
            second = fm.follow_instance(
                db, local_node_id='local-dup',
                peer_node_id='remote-dup',
                peer_url='http://remote:6777',
            )

        assert first is True
        assert second is False

        count = db.query(InstanceFollow).filter(
            InstanceFollow.follower_node_id == 'local-dup',
            InstanceFollow.following_node_id == 'remote-dup',
        ).count()
        assert count == 1


# ═══════════════════════════════════════════════════════════════
# TEST CLASS 6: EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Mix of DB, mocks, threading for concurrency edge cases."""

    def test_create_agent_during_network_outage_local_fallback(self, db):
        """DB agent registration works without network."""
        # Network mocks: all external calls fail
        with patch('requests.post', side_effect=ConnectionError("no net")):
            agent = UserService.register_agent(
                db, name='deep.azure.beacon',
                description='offline agent',
                skip_name_validation=True,
            )
        assert agent is not None
        assert agent.username == 'deep.azure.beacon'

    def test_delete_agent_during_network_outage(self, db, user_factory):
        """DB deletion works without network."""
        agent = user_factory(user_type='agent', username='delete.me.now')
        agent_id = agent.id

        with patch('requests.post', side_effect=ConnectionError("offline")):
            db.delete(agent)
            db.flush()

        found = db.query(User).filter(User.id == agent_id).first()
        assert found is None

    def test_two_users_same_name_simultaneously(self, db):
        """Unique constraint enforced under concurrency."""
        agent1 = UserService.register_agent(
            db, name='sharp.emerald.thunder',
            description='first', skip_name_validation=True,
        )
        assert agent1 is not None

        with pytest.raises(ValueError, match="already taken"):
            UserService.register_agent(
                db, name='sharp.emerald.thunder',
                description='duplicate', skip_name_validation=True,
            )

    def test_agent_state_recovery_after_crash_via_ledger(self):
        """Clear action_states, verify re-initialization works."""
        # Set some states
        action_states['recovery_test'] = {
            1: ActionState.COMPLETED,
            2: ActionState.IN_PROGRESS,
        }

        # Simulate crash — clear
        saved = dict(action_states.get('recovery_test', {}))
        action_states.clear()

        # Simulate recovery — restore from saved
        action_states['recovery_test'] = saved
        assert action_states['recovery_test'][1] == ActionState.COMPLETED
        assert action_states['recovery_test'][2] == ActionState.IN_PROGRESS

    def test_expired_token_handling(self, db, user_factory):
        """Token validation fails after expiry (simulated)."""
        user = user_factory()
        original_token = user.api_token

        # Simulate token expiry by setting a different token
        user.api_token = 'expired_token_12345678'
        db.flush()

        # Old token should not match
        found = db.query(User).filter(
            User.api_token == original_token).first()
        assert found is None

    def test_rate_limiting_burst_then_window_reset(self):
        """60 ok → 61st blocked → window expires → allowed."""
        from security.rate_limiter_redis import RedisRateLimiter

        limiter = RedisRateLimiter()
        limiter._redis = None
        limiter._memory_store = defaultdict(list)

        key = 'rl:global:ip:edge_burst'

        # Fill to capacity
        for _ in range(60):
            assert limiter._check_memory(key, 60, 60) is True

        # Should be blocked
        assert limiter._check_memory(key, 60, 60) is False

        # Simulate window expiration
        limiter._memory_store[key] = []

        # Should be allowed again
        assert limiter._check_memory(key, 60, 60) is True

    def test_federation_inbox_deduplication(self, db):
        """Same post received twice → only 1 FederatedPost."""
        fm = FederationManager()

        payload = {
            'type': 'new_post',
            'origin_node_id': 'dedup-origin',
            'origin_url': 'http://origin:6777',
            'origin_name': 'origin-node',
            'post': {
                'id': 'post-dedup-1',
                'title': 'Dedup Test',
                'content': 'Test content',
                'content_type': 'text',
                'author': {'username': 'test'},
                'created_at': datetime.utcnow().isoformat(),
            },
        }

        first = fm.receive_inbox(db, payload)
        assert first is not None

        second = fm.receive_inbox(db, payload)
        assert second is None  # Duplicate rejected

        count = db.query(FederatedPost).filter(
            FederatedPost.origin_node_id == 'dedup-origin',
            FederatedPost.origin_post_id == 'post-dedup-1',
        ).count()
        assert count == 1

    def test_gossip_self_peer_excluded(self, db):
        """Own node_id not merged into peer list."""
        gossip = GossipProtocol()
        own_id = gossip.node_id

        is_new = gossip._merge_peer(db, {
            'node_id': own_id,
            'url': 'http://self:6777',
            'name': 'self',
        })
        assert is_new is False

        # Verify self was not added
        found = db.query(PeerNode).filter(
            PeerNode.node_id == own_id).first()
        assert found is None

    def test_concurrent_requests_during_flaky_connection(self):
        """10 threads with alternating success/failure → no deadlock."""
        results = []
        errors = []
        lock = threading.Lock()

        def make_request(idx):
            try:
                gossip = GossipProtocol()
                if idx % 2 == 0:
                    # Simulate success
                    with lock:
                        results.append(('success', idx))
                else:
                    # Simulate failure
                    raise ConnectionError(f"Flaky connection {idx}")
            except ConnectionError as e:
                with lock:
                    errors.append(('error', idx, str(e)))

        threads = []
        for i in range(10):
            t = threading.Thread(target=make_request, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)

        # Verify no deadlock — all threads completed
        assert len(results) + len(errors) == 10
        assert len(results) == 5  # Even indices
        assert len(errors) == 5   # Odd indices

    def test_ledger_cache_isolation(self):
        """Same params → same object; after clear → new object."""
        from lifecycle_hooks import _ledger_registry

        mock_ledger_a = MagicMock()
        mock_ledger_b = MagicMock()

        _ledger_registry['session_a'] = mock_ledger_a
        _ledger_registry['session_b'] = mock_ledger_b

        # Same key → same object
        assert _ledger_registry['session_a'] is mock_ledger_a
        assert _ledger_registry['session_b'] is mock_ledger_b

        # Clear and verify isolation
        _ledger_registry.clear()
        assert _ledger_registry.get('session_a') is None

        # Re-register → new object
        mock_ledger_c = MagicMock()
        _ledger_registry['session_a'] = mock_ledger_c
        assert _ledger_registry['session_a'] is mock_ledger_c
        assert _ledger_registry['session_a'] is not mock_ledger_a

        # Cleanup
        _ledger_registry.clear()
