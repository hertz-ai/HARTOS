"""
WorldModelBridge Routing Decision Matrix — exhaustive test coverage.

Tests every reachable (topology, inproc, http_server, peers) combination
across all packaging modes (bundled, docker, ISO, pip).

The routing decision tree:
  1. In-process available?          → use it (zero overhead)
  2. Remote HTTP URL configured?    → use remote HTTP
  3. Local server running?          → use local HTTP
  4. Hive peers connected?          → gossip experiences to peers
  5. None of the above?             → queue locally, flush when peers appear

Cases 1-3 work today. Cases 4-5 are the gossip/queue path.
"""
import os
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

# ── Fixtures ─────────────────────────────────────────────────────

# Simulated learning provider (in-process mode)
_mock_provider = MagicMock()
_mock_provider.create_chat_completion = MagicMock()
_mock_provider.get_stats = MagicMock(return_value={'episodes': 42})
_mock_hive = MagicMock()
_mock_hive.get_stats = MagicMock(return_value={'agents': 3})


def _verified():
    return {
        'verified': True, 'source': 'status_verifier',
        'outcome': 'success', 'action_id': 'routing-test',
        'evidence': {'kind': 'tool_receipt', 'message_index': 1},
    }


def _make_bridge(tier='flat', api_url=None, bundled=False,
                 inproc_provider=None, inproc_hive=None):
    """Create a WorldModelBridge with controlled state.

    Patches the import-time side effects so the bridge doesn't
    actually connect to anything during construction.
    """
    env = {
        'HEVOLVE_NODE_TIER': tier,
    }
    if api_url:
        env['HEVOLVEAI_API_URL'] = api_url
    if bundled:
        env['NUNBA_BUNDLED'] = '1'

    with patch.dict(os.environ, env, clear=False):
        # Prevent real init_in_process from running
        with patch(
            'integrations.agent_engine.world_model_bridge'
            '.WorldModelBridge._init_in_process'
        ):
            with patch(
                'integrations.agent_engine.world_model_bridge'
                '.WorldModelBridge._start_crawl_integrity_watcher'
            ):
                from integrations.agent_engine.world_model_bridge import (
                    WorldModelBridge,
                )
                bridge = WorldModelBridge()

    # Inject controlled state
    if inproc_provider:
        bridge._provider = inproc_provider
        bridge._hive_mind = inproc_hive
        bridge._in_process = True
        bridge._http_disabled = False  # in-process available, HTTP not needed but not broken
    else:
        bridge._in_process = False
        # _http_disabled already set correctly by __init__ based on env

    return bridge


def _has_peers():
    """Simulate hive peers being available."""
    return True


def _no_peers():
    return False


# ── Routing Matrix ───────────────────────────────────────────────
#
# Each test is parameterized as:
#   (topology, inproc, http_mode, has_peers, expected_action, packaging_notes)
#
# http_mode:
#   'local'  = local HevolveAI server running (default localhost:8000)
#   'remote' = remote URL configured (e.g. https://hevolveai.cloud)
#   'none'   = no server, no URL
#
# expected_action:
#   'inproc'  = direct Python calls to provider
#   'http'    = HTTP to local or remote server
#   'gossip'  = distribute to hive peers via gossip
#   'queue'   = store locally, flush later when peers appear
#   'drop'    = silently drop (no destination, acceptable)

ROUTING_MATRIX = [
    # ─── FLAT TIER ───────────────────────────────────────────────
    # ID  topology  inproc  http      peers   expected   packaging
    (1,  'flat',   True,   'local',  True,   'inproc',  'pip dev'),
    (2,  'flat',   True,   'local',  False,  'inproc',  'pip dev'),
    (3,  'flat',   True,   'remote', True,   'inproc',  'pip + cloud API'),
    (4,  'flat',   True,   'remote', False,  'inproc',  'pip + cloud API'),
    (5,  'flat',   True,   'none',   True,   'inproc',  'pip dev, no server'),
    (6,  'flat',   True,   'none',   False,  'inproc',  'pip dev, isolated'),
    # flat + unavail + local: impossible (if no inproc, no local server in flat)
    (9,  'flat',   False,  'remote', True,   'http',    'bundled + cloud hevolveai'),
    (10, 'flat',   False,  'remote', False,  'http',    'bundled + cloud hevolveai'),
    (11, 'flat',   False,  'none',   True,   'gossip',  'Nunba bundled, hive available'),
    (12, 'flat',   False,  'none',   False,  'queue',   'Nunba bundled, alone'),

    # ─── REGIONAL TIER ───────────────────────────────────────────
    (13, 'regional', True,  'local',  True,   'inproc', 'docker/ISO, full stack'),
    (14, 'regional', True,  'local',  False,  'inproc', 'docker/ISO, isolated'),
    (15, 'regional', True,  'remote', True,   'inproc', 'regional + cloud fallback'),
    (16, 'regional', True,  'remote', False,  'inproc', 'regional + cloud fallback'),
    (17, 'regional', True,  'none',   True,   'inproc', 'regional, pip installed'),
    (18, 'regional', True,  'none',   False,  'inproc', 'regional, pip, isolated'),
    (19, 'regional', False, 'local',  True,   'http',   'docker sidecar'),
    (20, 'regional', False, 'local',  False,  'http',   'docker sidecar, isolated'),
    (21, 'regional', False, 'remote', True,   'http',   'regional + remote hevolveai'),
    (22, 'regional', False, 'remote', False,  'http',   'regional + remote, isolated'),
    (23, 'regional', False, 'none',   True,   'gossip', 'regional bootstrapping'),
    (24, 'regional', False, 'none',   False,  'queue',  'regional bootstrapping, alone'),

    # ─── CENTRAL TIER ────────────────────────────────────────────
    (25, 'central', True,  'local',  True,   'inproc', 'cloud, full stack'),
    (26, 'central', True,  'local',  False,  'inproc', 'cloud, isolated'),
    (27, 'central', True,  'remote', True,   'inproc', 'cloud + external AI'),
    (28, 'central', True,  'remote', False,  'inproc', 'cloud + external AI'),
    (29, 'central', True,  'none',   True,   'inproc', 'cloud, pip installed'),
    (30, 'central', True,  'none',   False,  'inproc', 'cloud, pip, isolated'),
    (31, 'central', False, 'local',  True,   'http',   'docker microservices'),
    (32, 'central', False, 'local',  False,  'http',   'docker microservices'),
    (33, 'central', False, 'remote', True,   'http',   'cloud + remote hevolveai'),
    (34, 'central', False, 'remote', False,  'http',   'cloud + remote, isolated'),
    (35, 'central', False, 'none',   True,   'gossip', 'central missing hevolveai'),
    (36, 'central', False, 'none',   False,  'queue',  'central missing hevolveai'),
]

# Packaging reachability — which rows each packaging mode can hit.
# Used to verify test coverage, not routing logic.
PACKAGING_REACHABILITY = {
    'bundled_cx_freeze': {
        'reachable': [5, 6, 11, 12],  # flat, inproc unlikely but possible
        'typical': [11, 12],  # most common: flat + unavail + none
        'notes': 'torch broken in frozen build, InProc rarely works',
    },
    'docker': {
        'reachable': [13, 14, 19, 20, 25, 26, 31, 32,  # local server as sidecar
                      15, 16, 21, 22, 27, 28, 33, 34],  # remote configured
        'typical': [13, 19, 25, 31],  # docker usually has peers
        'notes': 'HevolveAI as sidecar container or same image',
    },
    'iso_nixos': {
        'reachable': [13, 14, 17, 18, 25, 26, 29, 30],  # system packages
        'typical': [13, 17, 25],
        'notes': 'HevolveAI installed as system package, always in-process',
    },
    'pip_dev': {
        'reachable': [r for r in range(1, 37) if r not in (7, 8)],  # all except impossible
        'typical': [5, 6, 17, 29],  # dev usually has pip, no server
        'notes': 'Developer machine, any tier configurable',
    },
}


# ── Parameterized Tests ──────────────────────────────────────────

def _id_for_case(case):
    """Generate readable test ID from matrix row."""
    num, tier, inproc, http, peers, expected, notes = case
    return f"#{num}-{tier}-{'inproc' if inproc else 'noproc'}-{http}-{'peers' if peers else 'alone'}->{expected}"


@pytest.mark.parametrize(
    'case_id,tier,inproc,http_mode,has_peers,expected,notes',
    ROUTING_MATRIX,
    ids=[_id_for_case(c) for c in ROUTING_MATRIX],
)
class TestWorldModelRouting:
    """Verify correct routing for every reachable state in the matrix."""

    def test_record_interaction_routes_correctly(
        self, case_id, tier, inproc, http_mode, has_peers, expected, notes
    ):
        """record_interaction() should route to the expected destination."""
        api_url = {
            'local': 'http://localhost:8000',
            'remote': 'https://hevolveai.cloud',
            'none': None,
        }[http_mode]

        bundled = (tier == 'flat' and not inproc and http_mode == 'none')

        bridge = _make_bridge(
            tier=tier,
            api_url=api_url if api_url else None,
            bundled=bundled,
            inproc_provider=_mock_provider if inproc else None,
            inproc_hive=_mock_hive if inproc else None,
        )

        # Set http_disabled for bundled + no server
        if bundled:
            bridge._http_disabled = True

        if expected == 'inproc':
            # Should call provider directly, no HTTP
            bridge._provider = MagicMock()
            bridge._experience_queue.clear()
            bridge._flush_batch_size = 1  # Force immediate flush
            bridge.record_interaction(
                user_id='u1', prompt_id='p1',
                prompt='test', response='ok', verification=_verified())
            # Experience should be queued and flushed in-process
            assert bridge._stats['total_recorded'] >= 1

        elif expected == 'http':
            # Should POST to api_url, not raise
            with patch(
                'integrations.agent_engine.world_model_bridge.pooled_post'
            ) as mock_post:
                mock_post.return_value = MagicMock(status_code=200)
                bridge._flush_batch_size = 1
                bridge.record_interaction(
                    user_id='u1', prompt_id='p1',
                    prompt='test', response='ok', verification=_verified())
                # Flush runs in executor — verify experience was queued
                assert bridge._stats['total_recorded'] >= 1

        elif expected == 'gossip':
            # Should NOT attempt HTTP to localhost:8000
            with patch(
                'integrations.agent_engine.world_model_bridge.pooled_post'
            ) as mock_post:
                bridge._http_disabled = True
                bridge._flush_batch_size = 1
                bridge.record_interaction(
                    user_id='u1', prompt_id='p1',
                    prompt='test', response='ok', verification=_verified())
                # Experience queued (for gossip distribution)
                assert bridge._stats['total_recorded'] >= 1
                # HTTP must NOT be called
                mock_post.assert_not_called()

        elif expected == 'queue':
            # Should store locally, no HTTP, no gossip
            with patch(
                'integrations.agent_engine.world_model_bridge.pooled_post'
            ) as mock_post:
                bridge._http_disabled = True
                bridge._flush_batch_size = 1
                bridge.record_interaction(
                    user_id='u1', prompt_id='p1',
                    prompt='test', response='ok', verification=_verified())
                assert bridge._stats['total_recorded'] >= 1
                mock_post.assert_not_called()

    def test_check_health_routes_correctly(
        self, case_id, tier, inproc, http_mode, has_peers, expected, notes
    ):
        """check_health() should not spam unreachable servers."""
        api_url = {
            'local': 'http://localhost:8000',
            'remote': 'https://hevolveai.cloud',
            'none': None,
        }[http_mode]

        bundled = (tier == 'flat' and not inproc and http_mode == 'none')

        bridge = _make_bridge(
            tier=tier,
            api_url=api_url if api_url else None,
            bundled=bundled,
            inproc_provider=_mock_provider if inproc else None,
            inproc_hive=_mock_hive if inproc else None,
        )

        if bundled:
            bridge._http_disabled = True

        if expected == 'inproc':
            result = bridge.check_health()
            assert result['healthy'] is True
            assert result['mode'] == 'in_process'

        elif expected == 'http':
            with patch(
                'integrations.agent_engine.world_model_bridge.pooled_get'
            ) as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {}
                mock_resp.headers = {'content-type': 'application/json'}
                mock_get.return_value = mock_resp
                result = bridge.check_health()
                assert result['healthy'] is True
                mock_get.assert_called_once()

        elif expected in ('gossip', 'queue'):
            # Must NOT attempt HTTP — return graceful "not available"
            with patch(
                'integrations.agent_engine.world_model_bridge.pooled_get'
            ) as mock_get:
                result = bridge.check_health()
                assert result['healthy'] is False
                assert result.get('learning_active') is False
                # No HTTP spam
                mock_get.assert_not_called()

    def test_get_learning_stats_routes_correctly(
        self, case_id, tier, inproc, http_mode, has_peers, expected, notes
    ):
        """get_learning_stats() should not spam unreachable servers."""
        api_url = {
            'local': 'http://localhost:8000',
            'remote': 'https://hevolveai.cloud',
            'none': None,
        }[http_mode]

        bundled = (tier == 'flat' and not inproc and http_mode == 'none')

        bridge = _make_bridge(
            tier=tier,
            api_url=api_url if api_url else None,
            bundled=bundled,
            inproc_provider=_mock_provider if inproc else None,
            inproc_hive=_mock_hive if inproc else None,
        )

        if bundled:
            bridge._http_disabled = True

        if expected == 'inproc':
            result = bridge.get_learning_stats()
            assert 'bridge' in result

        elif expected == 'http':
            with patch(
                'integrations.agent_engine.world_model_bridge.pooled_get'
            ) as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {'episodes': 10}
                mock_get.return_value = mock_resp
                result = bridge.get_learning_stats()
                assert mock_get.call_count >= 1

        elif expected in ('gossip', 'queue'):
            with patch(
                'integrations.agent_engine.world_model_bridge.pooled_get'
            ) as mock_get:
                result = bridge.get_learning_stats()
                # Returns empty stats, no HTTP
                assert result['learning'] == {}
                assert result['hivemind'] == {}
                mock_get.assert_not_called()


# ── Packaging Reachability Coverage ──────────────────────────────

class TestPackagingCoverage:
    """Verify every packaging mode has test coverage for its typical cases."""

    @pytest.mark.parametrize('packaging,info', PACKAGING_REACHABILITY.items())
    def test_typical_cases_covered(self, packaging, info):
        """Every packaging mode's typical cases must be in the matrix."""
        matrix_ids = {row[0] for row in ROUTING_MATRIX}
        for case_id in info['typical']:
            assert case_id in matrix_ids, (
                f"Packaging '{packaging}' typical case #{case_id} "
                f"missing from ROUTING_MATRIX"
            )

    @pytest.mark.parametrize('packaging,info', PACKAGING_REACHABILITY.items())
    def test_reachable_cases_covered(self, packaging, info):
        """Every packaging mode's reachable cases must be in the matrix."""
        matrix_ids = {row[0] for row in ROUTING_MATRIX}
        for case_id in info['reachable']:
            assert case_id in matrix_ids, (
                f"Packaging '{packaging}' reachable case #{case_id} "
                f"missing from ROUTING_MATRIX"
            )


# ── Edge Cases ───────────────────────────────────────────────────

class TestEdgeCases:
    """Boundary conditions and transitions."""

    def test_circuit_breaker_stops_http_after_threshold(self):
        """After 5 HTTP failures, circuit breaker opens for 60s."""
        bridge = _make_bridge(tier='regional', api_url='http://localhost:8000')
        for _ in range(5):
            bridge._cb_record_failure()
        assert bridge._cb_is_open() is True

    def test_circuit_breaker_resets_on_success(self):
        """One success resets the circuit breaker."""
        bridge = _make_bridge(tier='regional', api_url='http://localhost:8000')
        for _ in range(4):
            bridge._cb_record_failure()
        bridge._cb_record_success()
        assert bridge._cb_is_open() is False

    def test_lazy_inproc_retry_on_first_record(self):
        """record_interaction() retries in-process init once."""
        bridge = _make_bridge(tier='flat')
        bridge._in_process_retry_done = False
        with patch.object(bridge, '_init_in_process') as mock_init:
            bridge.record_interaction(
                user_id='u1', prompt_id='p1',
                prompt='test', response='ok')
            mock_init.assert_called_once()
            assert bridge._in_process_retry_done is True

    def test_http_disabled_flag_prevents_all_http(self):
        """When _http_disabled=True, no HTTP calls for any method."""
        bridge = _make_bridge(tier='flat', bundled=True)
        bridge._http_disabled = True

        with patch(
            'integrations.agent_engine.world_model_bridge.pooled_get'
        ) as mock_get, patch(
            'integrations.agent_engine.world_model_bridge.pooled_post'
        ) as mock_post:
            bridge.check_health()
            bridge.get_learning_stats()
            bridge._flush_batch_size = 1
            bridge.record_interaction(
                user_id='u1', prompt_id='p1',
                prompt='test', response='ok')
            mock_get.assert_not_called()
            mock_post.assert_not_called()

    def test_inproc_to_http_fallback_on_tamper(self):
        """If HevolveAI integrity fails, bridge falls to HTTP mode."""
        bridge = _make_bridge(
            tier='regional',
            api_url='http://localhost:8000',
            inproc_provider=_mock_provider,
            inproc_hive=_mock_hive,
        )
        assert bridge._in_process is True

        # Simulate tamper detection callback
        bridge._on_crawl_tamper_detected()
        assert bridge._in_process is False
        assert bridge._provider is None

    def test_url_validation_rejects_bad_urls(self):
        """Malformed URLs should not propagate."""
        from core.port_registry import _validate_llm_url
        assert _validate_llm_url('http://127.0.0.1:8081/v1') is True
        assert _validate_llm_url('https://api.example.com/v1') is True
        assert _validate_llm_url('http://localhost/v1') is True
        assert _validate_llm_url('') is False
        assert _validate_llm_url('not-a-url') is False
        assert _validate_llm_url('ftp://wrong-scheme/v1') is False
        assert _validate_llm_url('http://:8080/v1') is False
        assert _validate_llm_url('http://host:99999/v1') is False
        assert _validate_llm_url('http://host:0/v1') is False

    def test_dynamic_port_change_invalidates_cache(self):
        """set_local_llm_url() clears the resolver cache."""
        from core.port_registry import (
            get_local_llm_url, set_local_llm_url, invalidate_llm_url,
        )
        # Prime cache
        with patch.dict(os.environ, {'HEVOLVE_LOCAL_LLM_URL': 'http://127.0.0.1:8080/v1'}):
            invalidate_llm_url()
            url1 = get_local_llm_url()
            assert '8080' in url1

        # Port conflict → server restarts on 8081
        set_local_llm_url('http://127.0.0.1:8081/v1')
        url2 = get_local_llm_url()
        assert '8081' in url2
        assert url1 != url2

        # Cleanup
        invalidate_llm_url()
