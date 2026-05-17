"""
test_api_agent_engine_ledger.py — Tests for the /api/agent-engine/ledger/*
endpoint family in integrations/agent_engine/api.py.

Original T18 commit (4e4554e) shipped these routes against a fictional
SmartLedger.get_instance() / list_tasks() / get_stats() API and never ran
them.  The rewrite walks core.platform_paths.get_agent_data_dir() for
ledger_<agent>_<session>.json files and aggregates SmartLedger.tasks
across every per-session instance.

This test pack guards the rewrite against future regressions:

  FT  list endpoint aggregates across multiple ledgers
  FT  get-by-id walks until found, 404 if absent
  FT  stats aggregates by_status across ledgers
  FT  status filter coerces string → TaskStatus enum
  FT  agent_id filter narrows to one agent's ledgers
  FT  limit caps the response size + early-breaks the walk
  NFT filename parser rejects path-traversal payloads
  NFT filename parser rejects sibling files (benchmark_ledger.json)
  NFT corrupt ledger file is skipped, not fatal
  NFT empty agent_data dir returns success with empty list

The aggregator (_iter_ledgers) is tested directly — it's the load-bearing
helper.  Endpoints are tested via Flask test_client with @require_auth
bypassed.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask, g

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
AGENT_LEDGER_SRC = os.path.join(PROJECT_ROOT, 'agent-ledger-opensource')
if AGENT_LEDGER_SRC not in sys.path:
    sys.path.insert(0, AGENT_LEDGER_SRC)


# ─── Fixture helpers ──────────────────────────────────────────────

def _write_ledger_file(storage_dir: Path, agent_id: str, session_id: str,
                       tasks: list) -> Path:
    """Write a JSON ledger file in the format SmartLedger.save() produces."""
    ledger_file = storage_dir / f'ledger_{agent_id}_{session_id}.json'
    payload = {
        'agent_id': agent_id,
        'session_id': session_id,
        'last_updated': '2026-05-04T00:00:00',
        'task_order': [t['task_id'] for t in tasks],
        'tasks': {t['task_id']: t for t in tasks},
    }
    ledger_file.write_text(json.dumps(payload), encoding='utf-8')
    return ledger_file


def _make_task(task_id: str, description: str, status: str = 'pending') -> dict:
    """Minimal Task dict matching agent_ledger.core.Task.to_dict shape."""
    return {
        'task_id': task_id,
        'description': description,
        'status': status,
        'task_type': 'pre_assigned',
        'priority': 50,
        'created_at': '2026-05-04T00:00:00',
        'updated_at': '2026-05-04T00:00:00',
        'state_history': [],
        'prerequisites': [],
        'dependent_task_ids': [],
        'execution_mode': 'sequential',
        'locality': 'local',
        'sensitivity': 'public',
        'context': {},
    }


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    """Tmp agent_data dir + factory cache reset between tests."""
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    # platform_paths caches; reset so NUNBA_DATA_DIR is picked up fresh
    from core import platform_paths
    platform_paths.reset_cache()
    data_dir = tmp_path / 'data' / 'agent_data'
    data_dir.mkdir(parents=True, exist_ok=True)
    # Reset agent_ledger.factory cache so each test sees fresh ledgers
    from agent_ledger.factory import clear_ledger_cache
    clear_ledger_cache()
    yield data_dir
    clear_ledger_cache()
    platform_paths.reset_cache()


@pytest.fixture
def app(storage_dir):
    """Flask app with agent_engine_bp + bypassed @require_auth.

    @require_auth is applied at module-import time, so patching
    ``integrations.agent_engine.api.require_auth`` AFTER the module is
    loaded has no effect on already-decorated route handlers.  We patch
    the SOURCE — ``integrations.social.auth.require_auth`` — before
    first import, then reload api so the route body re-runs the
    ``from integrations.social.auth import require_auth`` line and
    picks up our identity-decorator.
    """
    import integrations.social.auth as auth_mod
    auth_mod.require_auth = lambda f: f
    auth_mod.require_admin = lambda f: f

    # 2026-05-04: ledger routes now use @require_local_or_token (so a
    # localhost guest with no JWT can still see the admin Task Ledger
    # in Nunba bundled installs).  Patch the source to identity here
    # too — same pattern as require_auth above.
    import core.auth_local as auth_local_mod
    auth_local_mod.require_local_or_token = lambda f: f

    import importlib
    import integrations.agent_engine.api as api_mod
    importlib.reload(api_mod)

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(api_mod.agent_engine_bp)

    @app.before_request
    def _stub_user():
        class _StubUser:
            id = 'test-user-id'
            is_admin = True
        g.user = _StubUser()
        g.db = None
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ─── _iter_ledgers (aggregator helper) ────────────────────────────

class TestIterLedgers:
    """The aggregator is load-bearing — every handler depends on it."""

    def test_empty_dir_yields_nothing(self, storage_dir):
        from integrations.agent_engine.api import _iter_ledgers
        assert list(_iter_ledgers()) == []

    def test_walks_all_ledger_files(self, storage_dir):
        agent = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
        _write_ledger_file(storage_dir, agent, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                           [_make_task('t1', 'first')])
        _write_ledger_file(storage_dir, agent, '3528295409',  # numeric session
                           [_make_task('t2', 'second')])
        _write_ledger_file(storage_dir, agent, 'ffffffff-1111-2222-3333-444444444444',
                           [_make_task('t3', 'third')])

        from integrations.agent_engine.api import _iter_ledgers
        results = list(_iter_ledgers())
        assert len(results) == 3
        agents = {r[0] for r in results}
        assert agents == {agent}
        # Each yielded ledger has the expected single task
        all_descriptions = []
        for _aid, _sid, ledger in results:
            for task in ledger.tasks.values():
                all_descriptions.append(task.description)
        assert sorted(all_descriptions) == ['first', 'second', 'third']

    def test_rejects_path_traversal_filenames(self, storage_dir):
        # Write a file with a malicious-looking name; the regex must reject it.
        bad = storage_dir / 'ledger_..%2Fpasswd_x.json'
        bad.write_text('{"tasks": {}}', encoding='utf-8')

        from integrations.agent_engine.api import _iter_ledgers
        assert list(_iter_ledgers()) == []

    def test_rejects_sibling_benchmark_files(self, storage_dir):
        (storage_dir / 'benchmark_ledger.json').write_text('{}', encoding='utf-8')
        (storage_dir / 'benchmark_leaderboard.json').write_text('{}', encoding='utf-8')

        from integrations.agent_engine.api import _iter_ledgers
        assert list(_iter_ledgers()) == []

    def test_corrupt_file_is_skipped(self, storage_dir):
        # Write a non-JSON body to a regex-matching filename.
        bad = (storage_dir
               / 'ledger_6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_session1.json')
        bad.write_text('not valid json {{{', encoding='utf-8')

        from integrations.agent_engine.api import _iter_ledgers
        # SmartLedger.load() catches Exception and resets self.tasks={};
        # the iterator should still yield, with empty tasks.
        results = list(_iter_ledgers())
        # Either yields the ledger with empty tasks, or skips it — both are
        # acceptable graceful-degradation outcomes.  What's NOT acceptable
        # is propagating the JSON parse error to the caller.
        assert all(len(r[2].tasks) == 0 for r in results)

    def test_agent_filter_narrows(self, storage_dir):
        agent_a = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
        agent_b = 'c23d388c-07a0-4a79-816d-5b95642683c0'
        _write_ledger_file(storage_dir, agent_a, '1111',
                           [_make_task('a1', 'A task')])
        _write_ledger_file(storage_dir, agent_b, '2222',
                           [_make_task('b1', 'B task')])

        from integrations.agent_engine.api import _iter_ledgers
        a_results = list(_iter_ledgers(agent_filter=agent_a))
        assert len(a_results) == 1
        assert a_results[0][0] == agent_a


# ─── /api/agent-engine/ledger/tasks ───────────────────────────────

class TestListLedgerTasks:

    def test_empty_returns_success_empty_list(self, client, storage_dir):
        resp = client.get('/api/agent-engine/ledger/tasks')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['tasks'] == []
        assert data['total'] == 0

    def test_aggregates_across_ledgers(self, client, storage_dir):
        agent = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
        _write_ledger_file(storage_dir, agent, 's1',
                           [_make_task('t1', 'one')])
        _write_ledger_file(storage_dir, agent, 's2',
                           [_make_task('t2', 'two'), _make_task('t3', 'three')])

        resp = client.get('/api/agent-engine/ledger/tasks')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] == 3
        ids = {t['task_id'] for t in data['tasks']}
        assert ids == {'t1', 't2', 't3'}
        # Every returned task carries (agent_id, session_id) for UI grouping
        assert all('agent_id' in t and 'session_id' in t for t in data['tasks'])

    def test_status_filter_matches_enum_value(self, client, storage_dir):
        agent = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
        _write_ledger_file(storage_dir, agent, 's1', [
            _make_task('done', 'd', status='completed'),
            _make_task('pend', 'p', status='pending'),
        ])

        resp = client.get('/api/agent-engine/ledger/tasks?status=completed')
        assert resp.status_code == 200
        data = resp.get_json()
        assert {t['task_id'] for t in data['tasks']} == {'done'}

    def test_unknown_status_returns_400(self, client, storage_dir):
        resp = client.get('/api/agent-engine/ledger/tasks?status=garbage')
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False

    def test_limit_caps_response(self, client, storage_dir):
        agent = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
        # Spread 10 tasks across 5 ledgers
        for i in range(5):
            _write_ledger_file(storage_dir, agent, f'sess{i:08d}', [
                _make_task(f't{i}a', 'a'), _make_task(f't{i}b', 'b'),
            ])

        resp = client.get('/api/agent-engine/ledger/tasks?limit=4')
        assert resp.status_code == 200
        assert resp.get_json()['total'] == 4

    def test_garbage_limit_falls_back_to_default(self, client, storage_dir):
        resp = client.get('/api/agent-engine/ledger/tasks?limit=notanumber')
        # Falls back to default (50), still 200
        assert resp.status_code == 200


# ─── /api/agent-engine/ledger/tasks/<id> ──────────────────────────

class TestGetLedgerTask:

    def test_found_in_one_of_many_ledgers(self, client, storage_dir):
        agent = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
        _write_ledger_file(storage_dir, agent, 's1',
                           [_make_task('alpha', 'A')])
        _write_ledger_file(storage_dir, agent, 's2',
                           [_make_task('beta', 'B')])

        resp = client.get('/api/agent-engine/ledger/tasks/beta')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['task']['task_id'] == 'beta'
        assert data['task']['session_id'] == 's2'

    def test_missing_returns_404(self, client, storage_dir):
        resp = client.get('/api/agent-engine/ledger/tasks/does-not-exist')
        assert resp.status_code == 404


# ─── /api/agent-engine/ledger/stats ───────────────────────────────

class TestGetLedgerStats:

    def test_empty_returns_zeros(self, client, storage_dir):
        resp = client.get('/api/agent-engine/ledger/stats')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['stats']['total'] == 0
        assert data['stats']['sessions'] == 0
        assert data['stats']['by_status'] == {}

    def test_aggregates_by_status_across_sessions(self, client, storage_dir):
        agent = '6c2dc0fc-7c93-4fe0-973e-f7466ff63f29'
        _write_ledger_file(storage_dir, agent, 's1', [
            _make_task('a', 'a', status='pending'),
            _make_task('b', 'b', status='completed'),
        ])
        _write_ledger_file(storage_dir, agent, 's2', [
            _make_task('c', 'c', status='pending'),
        ])

        resp = client.get('/api/agent-engine/ledger/stats')
        assert resp.status_code == 200
        stats = resp.get_json()['stats']
        assert stats['total'] == 3
        assert stats['sessions'] == 2
        assert stats['by_status'] == {'pending': 2, 'completed': 1}
