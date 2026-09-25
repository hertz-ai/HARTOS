"""The three evolution safety gates, exercised through their REAL composition.

Found inert by the audit against arXiv 2607.12227 ("Rethinking the Evaluation
of Harness Evolution for Agents"); evidence in hevolveai Master 11.433. The
older tests mocked the very calls that were broken (test_rsi_gates.py patches
both capture_snapshot and validate_against_baseline), so each gate here runs
its real write and read path. Only the slow metric COLLECTORS are stubbed,
which also lets each test choose the measured values.

  A. autoresearch regression gate: the snapshot it writes must be the one the
     gate reads, so a real regression is REJECTED (it passed on "no baseline
     to compare" every time).
  B. merge-deploy benchmark gate: a regressed benchmark must block the deploy
     (a wrong call signature made it a swallowed TypeError).
  C. snapshot order: the newest snapshot is v12, not v9 (string sort).
"""
import json
import os
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.agent_engine import agent_baseline_service as abs_mod  # noqa: E402
from integrations.agent_engine import benchmark_registry as reg_mod  # noqa: E402


@pytest.fixture
def baselines(tmp_path, monkeypatch):
    d = tmp_path / 'baselines'
    d.mkdir()
    monkeypatch.setattr(abs_mod, 'BASELINE_DIR', str(d))
    svc = abs_mod.AgentBaselineService
    # Collectors that reach out (DB, world-model bridge, git) are not the gate.
    monkeypatch.setattr(svc, '_collect_world_model_metrics', staticmethod(lambda: {}))
    monkeypatch.setattr(svc, '_collect_trust_evolution_metrics',
                        staticmethod(lambda user_id: {}))
    monkeypatch.setattr(svc, '_collect_lightning_metrics',
                        staticmethod(lambda prompt_id, user_prompt: {}))
    monkeypatch.setattr(svc, '_build_metadata', staticmethod(lambda trigger: {}))
    return d


def _bench(monkeypatch, pass_rate):
    monkeypatch.setattr(
        abs_mod.AgentBaselineService, '_collect_benchmark_metrics',
        staticmethod(lambda: {'regression': {'pass_rate': pass_rate}}))


# -- A ---------------------------------------------------------------------

def test_A_autoresearch_gate_compares_against_the_snapshot_it_wrote(
        baselines, monkeypatch):
    from integrations.coding_agent import autoevolve_code_tools as ae

    session = ae.AutoResearchSession(
        experiment_id='gateA', repo_path='/tmp/x', target_file='t.py',
        run_command='echo')
    engine = ae.AutoResearchEngine()

    _bench(monkeypatch, 1.0)                      # the pre-evolution state
    assert engine.capture_baseline_snapshot(session, 'autoresearch_baseline', 0)
    assert session.baseline_enforced
    latest = abs_mod.AgentBaselineService.get_latest_snapshot(
        'gateA', ae._BASELINE_FLOW_ID)
    assert latest is not None, "the write must land in the slot the gate reads"

    _bench(monkeypatch, 0.5)                      # a candidate that regressed
    passed, regressions, reason = engine._baseline_delta_gate(session)
    assert passed is False, (
        "a real regression must be REJECTED, got %r (%s)" % (passed, reason))
    assert any('regression_pass_rate' in r for r in regressions)

    _bench(monkeypatch, 1.0)                      # control: no regression
    passed, regressions, _ = engine._baseline_delta_gate(session)
    assert passed is True and regressions == []


def test_A_a_failed_capture_is_reported_not_assumed(baselines, monkeypatch):
    from integrations.coding_agent import autoevolve_code_tools as ae
    session = ae.AutoResearchSession(
        experiment_id='gateA2', repo_path='/tmp/x', target_file='t.py',
        run_command='echo')
    monkeypatch.setattr(abs_mod.AgentBaselineService, 'capture_snapshot',
                        staticmethod(lambda **kw: None))
    assert ae.AutoResearchEngine().capture_baseline_snapshot(
        session, 'autoresearch_improvement', 3) is False
    assert session.baseline_enforced is False


# -- B ---------------------------------------------------------------------

def _write_bench(d, version, pass_rate, mtime):
    p = os.path.join(d, f'{version}.json')
    with open(p, 'w') as f:
        json.dump({'version': version, 'benchmarks': {'regression': {'metrics': {
            'pass_rate': {'value': pass_rate, 'direction': 'higher'}}}}}, f)
    os.utime(p, (mtime, mtime))


@pytest.fixture
def bench_dir(tmp_path, monkeypatch):
    d = tmp_path / 'benchmarks'
    d.mkdir()
    monkeypatch.setattr(reg_mod, 'BENCHMARK_DIR', str(d))
    return str(d)


def test_B_previous_version_is_the_newest_other_snapshot(bench_dir):
    now = time.time()
    _write_bench(bench_dir, 'old1', 1.0, now - 300)
    _write_bench(bench_dir, 'old2', 1.0, now - 100)
    _write_bench(bench_dir, 'newsha12', 1.0, now)
    reg = reg_mod.BenchmarkRegistry()
    assert reg.previous_version('newsha12') == 'old2'
    assert reg.previous_version('nothing-else') == 'newsha12'


def _deploy(bench_dir, new_pass_rate):
    from integrations.agent_engine.auto_deploy_service import AutoDeployService
    now = time.time()
    _write_bench(bench_dir, 'prevver1', 1.0, now - 60)
    reg = reg_mod.BenchmarkRegistry()

    def fake_capture(version, git_sha='', tier='fast'):
        _write_bench(bench_dir, version, new_pass_rate, time.time())
        return {'version': version}

    ok = type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
    with patch('subprocess.run', return_value=ok), \
            patch('integrations.agent_engine.pr_review_service.PRReviewService.'
                  'run_test_suite', return_value={'pass_rate': 1.0, 'failed': 0}), \
            patch.object(reg_mod, 'get_benchmark_registry', return_value=reg), \
            patch.object(reg, 'capture_snapshot', side_effect=fake_capture), \
            patch.object(AutoDeployService, '_sign_release',
                         return_value=None) as sign:
        result = AutoDeployService.on_pr_merged('repo', 'abcdef1234567890')
    return result, sign


def test_B_a_regressed_benchmark_blocks_the_merge_deploy(bench_dir):
    result, sign = _deploy(bench_dir, new_pass_rate=0.5)
    assert result['deployed'] is False
    assert result.get('error', '').startswith('Upgrade not safe'), result
    assert result['steps']['upgrade_safe']['safe'] is False
    assert result['steps']['upgrade_safe']['baseline'] == 'prevver1'
    assert not sign.called, "an unsafe upgrade must stop before signing"


def test_B_control_a_clean_benchmark_passes_the_gate(bench_dir):
    result, sign = _deploy(bench_dir, new_pass_rate=1.0)
    assert result['steps']['upgrade_safe']['safe'] is True
    assert sign.called, "a safe upgrade proceeds to signing"


# -- C ---------------------------------------------------------------------

def test_C_the_newest_snapshot_is_numeric_not_lexicographic(baselines):
    agent = baselines / 'agentC_0'
    agent.mkdir()
    for v in range(1, 13):
        (agent / f'v{v}.json').write_text(json.dumps({
            'version': v, 'trigger': 't', 'timestamp': v,
            'lightning_metrics': {'avg_reward': float(v)},
            'recipe_metrics': {}}))
    svc = abs_mod.AgentBaselineService
    versions = [s['version'] for s in svc.list_snapshots('agentC', 0)]
    assert versions == list(range(1, 13))
    assert svc.compute_trend('agentC', 0)['reward_trend'] == 'improving'
    metrics = abs_mod.AgentBaselineAdapter().run()['metrics']
    assert metrics['agentC_0_reward_delta']['value'] == pytest.approx(1.0), (
        "the adapter must compare v11 -> v12, not v8 -> v9")


# -- D: the loop may change ONLY its target file -----------------------------

def _edit_block(name, search, replace):
    return f"`{name}`\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


def test_D_edit_target_containment_and_declared_set(tmp_path):
    """The decision itself, dependency-free (the full apply below needs the
    vendored aider deps, which neither this venv nor the shipped Nunba has)."""
    from pathlib import Path
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    work = (tmp_path / 'repo')
    work.mkdir()
    root = Path(work).resolve()
    outside = (tmp_path / 'outside.py').resolve()
    decide = AiderNativeBackend._resolve_edit_target
    allowed = {(root / 't.py').resolve()}
    assert decide(root, '../outside.py', None)[0] is None
    assert decide(root, str(outside), None)[0] is None, "absolute path replaces root"
    assert decide(root, 'harness.py', allowed) == (None, 'not one of the declared files')
    assert decide(root, 't.py', allowed)[0] == (root / 't.py').resolve()
    assert decide(root, 'sub/harness.py', None)[0] is not None, (
        "unrestricted callers may still edit inside their working dir")


def test_D_backend_never_edits_outside_its_working_dir(tmp_path):
    pytest.importorskip('diff_match_patch')
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    work = tmp_path / 'repo'
    work.mkdir()
    (work / 't.py').write_text('x = 1\n')
    (work / 'harness.py').write_text('SCORE = 0\n')
    outside = tmp_path / 'outside.py'
    outside.write_text('SECRET = 1\n')
    backend = AiderNativeBackend()
    reply = (_edit_block('../outside.py', 'SECRET = 1', 'SECRET = 2')
             + _edit_block(str(outside), 'SECRET = 1', 'SECRET = 3')
             + _edit_block('harness.py', 'SCORE = 0', 'SCORE = 100')
             + _edit_block('t.py', 'x = 1', 'x = 2'))

    res = {r['file']: r for r in backend._apply_edits(
        reply, str(work), ['t.py'], restrict_to_files=True)}
    assert outside.read_text() == 'SECRET = 1\n', "no edit may leave working_dir"
    assert res['../outside.py']['status'] == 'refused'
    assert res[str(outside)]['status'] == 'refused'
    assert res['harness.py']['status'] == 'refused'
    assert (work / 'harness.py').read_text() == 'SCORE = 0\n'
    assert res['t.py']['status'] == 'applied'

    # control: without the restriction other callers keep editing any file
    # INSIDE their working dir, but containment still holds
    (work / 't.py').write_text('x = 1\n')
    res = {r['file']: r for r in backend._apply_edits(reply, str(work), ['t.py'])}
    assert res['harness.py']['status'] == 'applied'
    assert res['../outside.py']['status'] == 'refused'
    assert outside.read_text() == 'SECRET = 1\n'


def test_D_autoresearch_rejects_and_reverts_an_edit_beyond_its_target(tmp_path):
    import subprocess as sp
    from integrations.coding_agent import autoevolve_code_tools as ae
    repo = tmp_path / 'exp'
    repo.mkdir()
    (repo / 't.py').write_text('x = 1\n')
    (repo / 'harness.py').write_text('SCORE = 0\n')
    for cmd in (['git', 'init', '-q'], ['git', 'add', '-A'],
                ['git', '-c', 'user.email=t@t', '-c', 'user.name=t',
                 'commit', '-q', '-m', 'init']):
        sp.run(cmd, cwd=repo, check=True, capture_output=True)

    session = ae.AutoResearchSession(repo_path=str(repo), target_file='t.py',
                                     run_command='echo')
    engine = ae.get_autoresearch_engine()
    engine.register_session(session)

    def cheating_edit(s):
        (repo / 't.py').write_text('x = 2\n')
        (repo / 'harness.py').write_text('SCORE = 100\n')   # games the metric
        return 'raise the score', [], ['t.py', 'harness.py']

    try:
        with patch.object(engine, 'generate_and_apply_edit', side_effect=cheating_edit):
            out = json.loads(ae.autoresearch_edit(session.session_id))
    finally:
        engine.unregister_session(session.session_id)
    assert out['success'] is False and 'harness.py' in out['reason']
    assert (repo / 'harness.py').read_text() == 'SCORE = 0\n', "harness restored"
    assert (repo / 't.py').read_text() == 'x = 1\n', "the whole iteration is void"
    assert session.scope_rejections == 1
