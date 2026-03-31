"""
Agent Baseline Service - Unified Performance Snapshots

Captures a composite snapshot of agent performance at creation time and
whenever recipe, prompt, or intelligence changes.  Enables before/after
comparison for agent evolution tracking and CI/CD gating.

All methods are fire-and-forget -- never raise into the main execution flow.
Snapshots stored at agent_data/baselines/{prompt_id}_{flow_id}/v{N}.json.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve_social')

try:
    from helper import PROMPTS_DIR
except ImportError:
    PROMPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'prompts'))

def _resolve_baseline_dir():
    import sys as _sys
    db_path = os.environ.get('HEVOLVE_DB_PATH', '')
    if db_path and db_path != ':memory:' and os.path.isabs(db_path):
        return os.path.join(os.path.dirname(db_path), 'agent_data', 'baselines')
    if os.environ.get('NUNBA_BUNDLED') or getattr(_sys, 'frozen', False):
        try:
            from core.platform_paths import get_agent_data_dir
            return os.path.join(get_agent_data_dir(), 'baselines')
        except ImportError:
            return os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data', 'agent_data', 'baselines')
    return os.path.join('agent_data', 'baselines')

BASELINE_DIR = _resolve_baseline_dir()

# Deduplication: skip recipe_change if creation was <60s ago for same agent
_recent_snapshots: Dict[str, float] = {}   # key → timestamp
_recent_lock = threading.Lock()
_DEDUP_WINDOW_S = 60
_MAX_DEDUP_ENTRIES = 1000  # Cap to prevent memory leak

_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


def _sanitize_id(value: str) -> str:
    """Sanitize prompt_id/flow_id to prevent path traversal."""
    s = str(value).strip()
    if not _SAFE_ID_RE.match(s):
        raise ValueError(f'Invalid identifier: {s!r}')
    return s


class AgentBaselineService:
    """Singleton service for unified agent performance snapshots."""

    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(BASELINE_DIR, exist_ok=True)

    # ── Core Snapshot ────────────────────────────────────────────

    @staticmethod
    def capture_snapshot(
        prompt_id: str,
        flow_id: int,
        trigger: str,
        user_id: str = '',
        user_prompt: str = '',
    ) -> Optional[Dict]:
        """Capture a unified baseline snapshot.  Fire-and-forget.

        Args:
            prompt_id: Agent prompt identifier.
            flow_id:   Flow index within the prompt.
            trigger:   One of 'creation', 'recipe_change',
                       'prompt_change', 'intelligence_change'.
            user_id:   Owner user id (for trust/evolution lookup).
            user_prompt: Session key (for lightning lookup).
        Returns:
            The snapshot dict, or None on failure.
        """
        try:
            prompt_id = _sanitize_id(prompt_id)
            flow_id = int(flow_id)
            agent_key = f'{prompt_id}_{flow_id}'

            # Dedup: skip recipe_change if creation was recent
            with _recent_lock:
                # Evict stale entries to prevent unbounded memory growth
                if len(_recent_snapshots) > _MAX_DEDUP_ENTRIES:
                    cutoff = time.time() - _DEDUP_WINDOW_S
                    stale = [k for k, v in _recent_snapshots.items() if v < cutoff]
                    for k in stale:
                        del _recent_snapshots[k]

                if trigger == 'recipe_change':
                    last = _recent_snapshots.get(agent_key, 0)
                    if time.time() - last < _DEDUP_WINDOW_S:
                        logger.debug(
                            f'Baseline dedup: skipping recipe_change for '
                            f'{agent_key} (creation <{_DEDUP_WINDOW_S}s ago)')
                        return None
                if trigger == 'creation':
                    _recent_snapshots[agent_key] = time.time()

            version = AgentBaselineService._next_version(prompt_id, flow_id)

            snapshot: Dict = {
                'version': version,
                'prompt_id': prompt_id,
                'flow_id': flow_id,
                'trigger': trigger,
                'timestamp': time.time(),
                'metadata': AgentBaselineService._build_metadata(trigger),
                'recipe_metrics': AgentBaselineService._collect_recipe_metrics(
                    prompt_id, flow_id),
                'lightning_metrics': AgentBaselineService._collect_lightning_metrics(
                    prompt_id, user_prompt),
                'benchmark_metrics': AgentBaselineService._collect_benchmark_metrics(),
                'trust_evolution_metrics': AgentBaselineService._collect_trust_evolution_metrics(
                    user_id),
            }

            # Persist
            agent_dir = os.path.join(BASELINE_DIR, agent_key)
            os.makedirs(agent_dir, exist_ok=True)
            fpath = os.path.join(agent_dir, f'v{version}.json')
            tmp = fpath + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp, fpath)
            logger.info(f'Baseline v{version} captured for {agent_key} '
                        f'(trigger={trigger})')
            return snapshot

        except Exception as e:
            logger.debug(f'Baseline capture failed: {e}')
            return None

    # ── Metric Collectors ────────────────────────────────────────

    @staticmethod
    def _collect_recipe_metrics(prompt_id: str, flow_id: int) -> Dict:
        """Read recipe JSON and extract experience metrics."""
        try:
            recipe_path = os.path.join(PROMPTS_DIR, f'{prompt_id}_{flow_id}_recipe.json')
            if not os.path.exists(recipe_path):
                return {}
            with open(recipe_path, 'r') as f:
                recipe = json.load(f)

            actions = recipe.get('actions', [])
            meta = recipe.get('experience_meta', {})
            per_action: Dict = {}
            total_dur = 0.0

            for action in actions:
                aid = str(action.get('action_id', 0))
                exp = action.get('experience', {})
                avg_dur = exp.get('avg_duration_seconds', 0)
                total_dur += avg_dur
                per_action[aid] = {
                    'avg_duration_seconds': avg_dur,
                    'success_rate': exp.get('success_rate', 1.0),
                    'run_count': exp.get('run_count', 0),
                    'tool_stats': exp.get('tool_stats', {}),
                    'dead_ends_count': len(exp.get('dead_ends', [])),
                    'effective_fallbacks_count': len(
                        exp.get('effective_fallbacks', [])),
                }

            return {
                'action_count': len(actions),
                'total_expected_duration_seconds': round(total_dur, 2),
                'total_runs': meta.get('total_runs', 0),
                'bottleneck_action_id': meta.get('bottleneck_action_id'),
                'per_action': per_action,
            }
        except Exception as e:
            logger.debug(f'Recipe metric collection failed: {e}')
            return {}

    @staticmethod
    def _collect_lightning_metrics(prompt_id: str, user_prompt: str) -> Dict:
        """Read Agent Lightning spans and compute aggregate metrics."""
        try:
            from integrations.agent_lightning import is_enabled, LightningStore
            if not is_enabled():
                return {}

            agent_id = f'create_recipe_assistant_{user_prompt}' \
                if user_prompt else f'create_recipe_assistant_{prompt_id}'
            store = LightningStore(agent_id, backend='json')
            spans = store.list_spans(limit=100, status='success')
            if not spans:
                spans = store.list_spans(limit=100)
            if not spans:
                return {}

            rewards: List[float] = []
            error_count = 0
            durations: List[float] = []

            for span in spans:
                if span.get('status') == 'error':
                    error_count += 1
                dur = span.get('duration', 0)
                if dur:
                    durations.append(dur)
                for event in span.get('events', []):
                    if event.get('type') == 'reward':
                        r = event.get('data', {}).get('reward', 0)
                        rewards.append(r)

            execution_count = len(spans)
            avg_reward = sum(rewards) / len(rewards) if rewards else 0.0

            # Trend: compare first half vs second half
            trend = 'stable'
            if len(rewards) >= 10:
                mid = len(rewards) // 2
                first_half = sum(rewards[:mid]) / mid
                second_half = sum(rewards[mid:]) / (len(rewards) - mid)
                if second_half > first_half * 1.10:
                    trend = 'improving'
                elif second_half < first_half * 0.90:
                    trend = 'declining'

            return {
                'avg_reward': round(avg_reward, 4),
                'total_reward': round(sum(rewards), 4),
                'reward_trend': trend,
                'execution_count': execution_count,
                'error_rate': round(error_count / max(1, execution_count), 3),
                'avg_duration_ms': round(
                    (sum(durations) / len(durations) * 1000)
                    if durations else 0, 1),
            }
        except Exception as e:
            logger.debug(f'Lightning metric collection failed: {e}')
            return {}

    @staticmethod
    def _collect_benchmark_metrics() -> Dict:
        """Read latest benchmark results from registry."""
        try:
            from integrations.agent_engine.benchmark_registry import (
                get_benchmark_registry)
            registry = get_benchmark_registry()
            results = registry.get_latest_results()
            # Flatten to {adapter_name: {metric: value}}
            flat: Dict = {}
            for name, result in results.items():
                metrics = result.get('metrics', {})
                flat[name] = {
                    k: v.get('value', 0) if isinstance(v, dict) else v
                    for k, v in metrics.items()
                }
            return flat
        except Exception as e:
            logger.debug(f'Benchmark metric collection failed: {e}')
            return {}

    @staticmethod
    def _collect_trust_evolution_metrics(user_id: str) -> Dict:
        """Query social DB for trust and evolution data."""
        if not user_id:
            return {}
        try:
            from integrations.social.models import get_db
            db = get_db()
            try:
                # Trust
                trust_data: Dict = {}
                try:
                    from integrations.social.rating_service import RatingService
                    ts = RatingService.get_trust_score(db, user_id)
                    if ts:
                        trust_data['composite_trust'] = ts.get(
                            'composite_trust', 0)
                except Exception:
                    pass

                # Evolution
                evo_data: Dict = {}
                try:
                    from integrations.social.agent_evolution_service import (
                        AgentEvolutionService)
                    evo = AgentEvolutionService.get_evolution(db, user_id)
                    if evo:
                        evo_data = {
                            'generation': evo.get('generation', 1),
                            'specialization_path': evo.get(
                                'specialization_path'),
                            'spec_tier': evo.get('spec_tier'),
                            'evolution_xp': evo.get('evolution_xp', 0),
                        }
                except Exception:
                    pass

                return {**trust_data, **evo_data}
            finally:
                db.close()
        except Exception as e:
            logger.debug(f'Trust/evolution metric collection failed: {e}')
            return {}

    @staticmethod
    def _build_metadata(trigger: str) -> Dict:
        """Compute snapshot metadata."""
        meta: Dict = {'trigger': trigger}
        try:
            _kw = dict(capture_output=True, text=True, timeout=5)
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                _kw['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'], **_kw)
            if result.returncode == 0:
                meta['git_sha'] = result.stdout.strip()
        except Exception:
            pass
        try:
            from security.node_integrity import compute_code_hash
            meta['code_hash'] = compute_code_hash()
        except Exception:
            pass
        return meta

    # ── Version Management ───────────────────────────────────────

    @staticmethod
    def _next_version(prompt_id: str, flow_id: int) -> int:
        """Scan baselines dir for existing versions, return N+1."""
        prompt_id = _sanitize_id(prompt_id)
        agent_dir = os.path.join(BASELINE_DIR, f'{prompt_id}_{flow_id}')
        if not os.path.isdir(agent_dir):
            return 1
        existing = []
        for fname in os.listdir(agent_dir):
            m = re.match(r'^v(\d+)\.json$', fname)
            if m:
                existing.append(int(m.group(1)))
        return max(existing) + 1 if existing else 1

    @staticmethod
    def get_latest_snapshot(
        prompt_id: str, flow_id: int
    ) -> Optional[Dict]:
        """Load the most recent baseline snapshot."""
        prompt_id = _sanitize_id(prompt_id)
        agent_dir = os.path.join(BASELINE_DIR, f'{prompt_id}_{flow_id}')
        if not os.path.isdir(agent_dir):
            return None
        versions = []
        for fname in os.listdir(agent_dir):
            m = re.match(r'^v(\d+)\.json$', fname)
            if m:
                versions.append(int(m.group(1)))
        if not versions:
            return None
        latest = max(versions)
        return AgentBaselineService.get_snapshot(
            prompt_id, flow_id, latest)

    @staticmethod
    def get_snapshot(
        prompt_id: str, flow_id: int, version: int
    ) -> Optional[Dict]:
        """Load a specific version."""
        prompt_id = _sanitize_id(prompt_id)
        fpath = os.path.join(
            BASELINE_DIR, f'{prompt_id}_{flow_id}', f'v{version}.json')
        if not os.path.isfile(fpath):
            return None
        try:
            with open(fpath, 'r') as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def list_snapshots(prompt_id: str, flow_id: int) -> List[Dict]:
        """List all snapshot versions with summary metadata."""
        prompt_id = _sanitize_id(prompt_id)
        agent_dir = os.path.join(BASELINE_DIR, f'{prompt_id}_{flow_id}')
        if not os.path.isdir(agent_dir):
            return []
        results = []
        for fname in sorted(os.listdir(agent_dir)):
            m = re.match(r'^v(\d+)\.json$', fname)
            if not m:
                continue
            try:
                with open(os.path.join(agent_dir, fname), 'r') as f:
                    snap = json.load(f)
                results.append({
                    'version': snap.get('version'),
                    'trigger': snap.get('trigger'),
                    'timestamp': snap.get('timestamp'),
                })
            except Exception:
                continue
        return results

    # ── Comparison ───────────────────────────────────────────────

    @staticmethod
    def compare_snapshots(
        prompt_id: str, flow_id: int,
        old_version: int, new_version: int,
    ) -> Dict:
        """Compute deltas between two snapshots."""
        old = AgentBaselineService.get_snapshot(
            prompt_id, flow_id, old_version)
        new = AgentBaselineService.get_snapshot(
            prompt_id, flow_id, new_version)
        if not old or not new:
            return {'error': 'snapshot not found'}

        def _delta(o, n, key, direction='higher'):
            ov = o.get(key, 0) or 0
            nv = n.get(key, 0) or 0
            d = nv - ov
            improved = d > 0 if direction == 'higher' else d < 0
            return {'old': ov, 'new': nv, 'delta': round(d, 4),
                    'improved': improved}

        recipe_delta: Dict = {}
        or_ = old.get('recipe_metrics', {})
        nr = new.get('recipe_metrics', {})
        recipe_delta['action_count'] = _delta(or_, nr, 'action_count', 'higher')
        recipe_delta['total_duration'] = _delta(
            or_, nr, 'total_expected_duration_seconds', 'lower')
        recipe_delta['total_runs'] = _delta(or_, nr, 'total_runs', 'higher')

        lightning_delta: Dict = {}
        ol = old.get('lightning_metrics', {})
        nl = new.get('lightning_metrics', {})
        lightning_delta['avg_reward'] = _delta(ol, nl, 'avg_reward', 'higher')
        lightning_delta['error_rate'] = _delta(ol, nl, 'error_rate', 'lower')
        lightning_delta['execution_count'] = _delta(
            ol, nl, 'execution_count', 'higher')

        trust_delta: Dict = {}
        ot = old.get('trust_evolution_metrics', {})
        nt = new.get('trust_evolution_metrics', {})
        trust_delta['composite_trust'] = _delta(
            ot, nt, 'composite_trust', 'higher')
        trust_delta['generation'] = _delta(ot, nt, 'generation', 'higher')

        return {
            'old_version': old_version,
            'new_version': new_version,
            'recipe_delta': recipe_delta,
            'lightning_delta': lightning_delta,
            'trust_delta': trust_delta,
        }

    @staticmethod
    def compute_trend(prompt_id: str, flow_id: int) -> Dict:
        """Analyze all snapshots to determine improving/declining/stable."""
        snapshots = AgentBaselineService.list_snapshots(prompt_id, flow_id)
        if len(snapshots) < 2:
            return {'trend': 'insufficient_data', 'snapshot_count': len(snapshots)}

        # Load first and latest
        first = AgentBaselineService.get_snapshot(
            prompt_id, flow_id, snapshots[0]['version'])
        latest = AgentBaselineService.get_snapshot(
            prompt_id, flow_id, snapshots[-1]['version'])
        if not first or not latest:
            return {'trend': 'error'}

        fr = first.get('lightning_metrics', {}).get('avg_reward', 0) or 0
        lr = latest.get('lightning_metrics', {}).get('avg_reward', 0) or 0

        fd = first.get('recipe_metrics', {}).get(
            'total_expected_duration_seconds', 0) or 0
        ld = latest.get('recipe_metrics', {}).get(
            'total_expected_duration_seconds', 0) or 0

        reward_trend = 'stable'
        if lr > fr * 1.10:
            reward_trend = 'improving'
        elif lr < fr * 0.90:
            reward_trend = 'declining'

        duration_trend = 'stable'
        if fd > 0:
            if ld < fd * 0.90:
                duration_trend = 'improving'
            elif ld > fd * 1.10:
                duration_trend = 'declining'

        return {
            'trend': reward_trend,
            'reward_trend': reward_trend,
            'duration_trend': duration_trend,
            'snapshot_count': len(snapshots),
        }

    @staticmethod
    def validate_against_baseline(
        prompt_id: str, flow_id: int
    ) -> Dict:
        """Compare current metrics vs latest baseline.

        Used by CI/CD (PRReviewService) to gate PR merges.
        Returns {passed: bool, regressions: []}.
        """
        latest = AgentBaselineService.get_latest_snapshot(prompt_id, flow_id)
        if not latest:
            return {'passed': True, 'regressions': [],
                    'reason': 'no baseline to compare'}

        regressions: List[str] = []

        # Collect current metrics
        current_recipe = AgentBaselineService._collect_recipe_metrics(
            prompt_id, flow_id)
        current_bench = AgentBaselineService._collect_benchmark_metrics()

        # Check recipe success rates
        baseline_actions = latest.get('recipe_metrics', {}).get(
            'per_action', {})
        current_actions = current_recipe.get('per_action', {})
        for aid, ba in baseline_actions.items():
            ca = current_actions.get(aid, {})
            old_sr = ba.get('success_rate', 1.0)
            new_sr = ca.get('success_rate', 1.0)
            if old_sr > 0 and new_sr < old_sr * 0.95:
                regressions.append(
                    f'action_{aid}_success_rate: {old_sr:.3f} → {new_sr:.3f}')

        # Check benchmark regression pass rate
        baseline_bench = latest.get('benchmark_metrics', {})
        old_reg = baseline_bench.get('regression', {})
        new_reg = current_bench.get('regression', {})
        if isinstance(old_reg, dict) and isinstance(new_reg, dict):
            old_pr = old_reg.get('pass_rate', 1.0)
            new_pr = new_reg.get('pass_rate', 1.0)
            if old_pr > 0 and new_pr < old_pr * 0.95:
                regressions.append(
                    f'regression_pass_rate: {old_pr:.3f} → {new_pr:.3f}')

        return {
            'passed': len(regressions) == 0,
            'regressions': regressions,
            'baseline_version': latest.get('version'),
        }


# ── AgentBaselineAdapter for BenchmarkRegistry ───────────────

try:
    from integrations.agent_engine.benchmark_registry import BenchmarkAdapter
except ImportError:
    BenchmarkAdapter = object  # graceful if benchmark_registry not available


class AgentBaselineAdapter(BenchmarkAdapter):
    """Benchmark adapter that reads agent baseline snapshots.
    Reports reward trends, success rate deltas, and duration improvements."""

    name = 'agent_baselines'
    tier = 'fast'

    def run(self, api_url: str = '', **kwargs) -> Dict:
        metrics: Dict = {}
        baseline_dir = Path(BASELINE_DIR)
        if not baseline_dir.exists():
            return {'metrics': metrics}

        for agent_dir in baseline_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            snapshots = sorted(agent_dir.glob('v*.json'))
            if len(snapshots) < 2:
                continue
            try:
                old = json.loads(snapshots[-2].read_text())
                new = json.loads(snapshots[-1].read_text())
            except Exception:
                continue

            key = agent_dir.name

            # Reward delta
            old_r = old.get('lightning_metrics', {}).get('avg_reward', 0) or 0
            new_r = new.get('lightning_metrics', {}).get('avg_reward', 0) or 0
            metrics[f'{key}_reward_delta'] = {
                'value': round(new_r - old_r, 4),
                'direction': 'higher', 'unit': 'score'}

            # Success rate delta
            old_sr = _avg_success_rate(
                old.get('recipe_metrics', {}).get('per_action', {}))
            new_sr = _avg_success_rate(
                new.get('recipe_metrics', {}).get('per_action', {}))
            metrics[f'{key}_success_rate_delta'] = {
                'value': round(new_sr - old_sr, 4),
                'direction': 'higher', 'unit': 'ratio'}

            # Duration improvement %
            old_d = old.get('recipe_metrics', {}).get(
                'total_expected_duration_seconds', 0) or 0
            new_d = new.get('recipe_metrics', {}).get(
                'total_expected_duration_seconds', 0) or 0
            improvement = ((old_d - new_d) / old_d * 100) if old_d > 0 else 0
            metrics[f'{key}_duration_improvement_pct'] = {
                'value': round(improvement, 2),
                'direction': 'higher', 'unit': '%'}

        return {'metrics': metrics}


def _avg_success_rate(per_action: Dict) -> float:
    """Compute average success rate across actions."""
    if not per_action:
        return 1.0
    rates = [
        v.get('success_rate', 1.0)
        for v in per_action.values()
        if isinstance(v, dict)
    ]
    return sum(rates) / len(rates) if rates else 1.0


# ── Singleton ────────────────────────────────────────────────

_service: Optional[AgentBaselineService] = None
_service_lock = threading.Lock()


def get_baseline_service() -> AgentBaselineService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = AgentBaselineService()
    return _service


# ── Fire-and-forget async helper ─────────────────────────────

_snapshot_executor: Optional[ThreadPoolExecutor] = None


def capture_baseline_async(
    prompt_id: str,
    flow_id: int,
    trigger: str,
    user_id: str = '',
    user_prompt: str = '',
):
    """Submit snapshot capture to a background thread.  Never blocks caller."""
    global _snapshot_executor
    if _snapshot_executor is None:
        _snapshot_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='baseline_snap')
    try:
        _snapshot_executor.submit(
            AgentBaselineService.capture_snapshot,
            prompt_id, flow_id, trigger, user_id, user_prompt)
    except Exception:
        pass  # fire-and-forget
