"""A coding run succeeds only with a receipt, and an old lie cannot elect itself.

Owner ruling 2026-10-04: a daemon agent completes for real -- a file written,
a diff applied, a check passed -- never a reply that promises or narrates.

Measured 2026-10-05 on the owner's desktop:
  * aider_native_backend._execute_task returned success True for ANY model
    reply.  gui_app logs 10-04 10:49 to 10-05 11:30: 442 runs routed to
    aider_native, 0 "Applied edit to"; replies such as "I cannot directly
    access or modify files".
  * coding_benchmarks.db held 1,797 aider_native rows, every one success=1.
    The router picks the tool with the best local success rate
    (tool_router._check_local_benchmarks -> get_best_tool), so the lie chose
    aider_native again on every run, and export_learning_delta sent it out
    as this node's routing knowledge.

    python -m pytest tests/unit/test_coding_success_needs_a_receipt.py -q
"""
import sqlite3
from unittest.mock import patch

import pytest


def _edit(name, search, replace):
    return f"`{name}`\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


# ── aider_native: success is an applied edit ────────────────────────────────

def _run_aider(tmp_path, reply):
    pytest.importorskip('diff_match_patch')
    from integrations.coding_agent.aider_native_backend import AiderNativeBackend
    from integrations.coding_agent.aider_core import hart_model_adapter as hma
    work = tmp_path / 'repo'
    work.mkdir(exist_ok=True)
    (work / 't.py').write_text('x = 1\n')
    # is_installed also needs the repo map's deps (grep_ast, tree_sitter),
    # which the installed product has and this venv does not; with files
    # named, a run never builds a repo map, so only the edit path is needed.
    with patch.object(AiderNativeBackend, 'is_installed', return_value=True), \
            patch.object(hma, 'send_completion', return_value=reply), \
            patch.object(hma.HartModelAdapter, 'from_hartos_config',
                         return_value=object()):
        result = AiderNativeBackend().execute(
            'fix x', {'working_dir': str(work), 'files': ['t.py']})
    return work, result


def test_a_reply_with_no_edit_is_not_a_success(tmp_path):
    work, result = _run_aider(
        tmp_path, 'I cannot directly access or modify files on your system.')
    assert result['success'] is False
    assert result['files_changed'] == []
    assert 'no edit' in result['error'].lower()
    assert (work / 't.py').read_text() == 'x = 1\n'


def test_an_edit_that_does_not_apply_is_not_a_success(tmp_path):
    work, result = _run_aider(tmp_path, _edit('t.py', 'y = 9', 'y = 10'))
    assert result['success'] is False
    assert result['files_changed'] == []
    assert 't.py' in result['error']
    assert (work / 't.py').read_text() == 'x = 1\n'


def test_an_applied_edit_is_the_receipt(tmp_path):
    work, result = _run_aider(tmp_path, _edit('t.py', 'x = 1', 'x = 2'))
    assert result['success'] is True
    assert result['files_changed'] == ['t.py']
    assert (work / 't.py').read_text() == 'x = 2\n'


# ── the benchmark: rows from before the receipt rule elect nothing ──────────

def _old_db(path, rows):
    """A coding_benchmarks.db as the code before the receipt rule wrote it."""
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE benchmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_type TEXT NOT NULL,
        tool_name TEXT NOT NULL, model_name TEXT DEFAULT '',
        user_id TEXT DEFAULT '', completion_time_s REAL NOT NULL,
        success INTEGER NOT NULL DEFAULT 0, offloaded INTEGER NOT NULL DEFAULT 0,
        timestamp REAL NOT NULL)''')
    conn.executemany(
        'INSERT INTO benchmarks (task_type, tool_name, completion_time_s, '
        'success, timestamp) VALUES (?, ?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()


def test_rows_recorded_before_the_receipt_rule_elect_no_tool(tmp_path):
    from integrations.coding_agent.benchmark_tracker import BenchmarkTracker
    db = str(tmp_path / 'coding_benchmarks.db')
    _old_db(db, [('bug_fix', 'aider_native', 30.0, 1, 1.0)] * 40)
    tracker = BenchmarkTracker(db_path=db)
    assert tracker.get_best_tool('bug_fix') is None
    assert tracker.export_learning_delta() is None
    assert tracker.get_summary()['total_benchmarks'] == 0
    # the old rows stay on disk: local records are never rewritten
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT COUNT(*) FROM benchmarks').fetchone()[0] == 40
    conn.close()


def test_rows_recorded_under_the_rule_route_and_export(tmp_path):
    from integrations.coding_agent.benchmark_tracker import BenchmarkTracker
    db = str(tmp_path / 'coding_benchmarks.db')
    _old_db(db, [('bug_fix', 'aider_native', 30.0, 1, 1.0)] * 40)
    tracker = BenchmarkTracker(db_path=db)
    for _ in range(6):
        tracker.record('bug_fix', 'aider_native', 30.0, False)
        tracker.record('bug_fix', 'claude_code', 90.0, True)
    best = tracker.get_best_tool('bug_fix')
    assert best is not None and best[0] == 'claude_code'
    delta = tracker.export_learning_delta()['coding_benchmarks']['bug_fix']
    assert delta['aider_native']['success_rate'] == 0.0
    assert delta['aider_native']['sample_count'] == 6
    assert tracker.get_summary()['total_benchmarks'] == 12


def test_a_fresh_db_records_under_the_rule(tmp_path):
    from integrations.coding_agent.benchmark_tracker import BenchmarkTracker
    tracker = BenchmarkTracker(db_path=str(tmp_path / 'fresh.db'))
    for _ in range(5):
        tracker.record('feature', 'kilocode', 10.0, True)
    assert tracker.get_best_tool('feature')[0] == 'kilocode'


def test_the_router_no_longer_follows_the_old_lie(tmp_path, monkeypatch):
    from integrations.coding_agent import benchmark_tracker as bt
    from integrations.coding_agent.tool_router import CodingToolRouter

    class _B:
        def __init__(self, name):
            self.name = name

    db = str(tmp_path / 'coding_benchmarks.db')
    _old_db(db, [('code_review', 'aider_native', 30.0, 1, 1.0)] * 40)
    tracker = bt.BenchmarkTracker(db_path=db)
    monkeypatch.setattr(bt, 'get_benchmark_tracker', lambda: tracker)
    with patch('integrations.coding_agent.tool_router.get_available_backends',
               return_value={'aider_native': _B('aider_native'),
                             'kilocode': _B('kilocode')}):
        picked = CodingToolRouter().route('review utils.py', 'code_review')
    # heuristic for code_review is claude_code (not offered here), so the
    # first available wins -- the point is that no benchmark row decided it
    assert CodingToolRouter()._check_local_benchmarks(
        'code_review', {'aider_native': _B('aider_native')}) is None
    assert picked is not None


def test_the_benchmarks_tool_answers_before_any_row_meets_the_rule(tmp_path, monkeypatch):
    """get_coding_benchmarks('all') read export_learning_delta().get(...);
    the export is None until a tool has MIN_SAMPLES rows under the rule,
    which after this change is every node's starting state."""
    import asyncio
    import json as _json
    from unittest.mock import MagicMock
    from core.agent_tools import build_core_tool_closures
    from integrations.coding_agent import benchmark_tracker as bt
    db = str(tmp_path / 'coding_benchmarks.db')
    _old_db(db, [('bug_fix', 'aider_native', 30.0, 1, 1.0)] * 40)
    tracker = bt.BenchmarkTracker(db_path=db)
    monkeypatch.setattr(bt, 'get_benchmark_tracker', lambda: tracker)
    ctx = {
        'user_id': '999', 'prompt_id': '8888', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': '999_8888',
        'request_id_list': {'999_8888': 'req1'}, 'recent_file_id': {},
        'scheduler': MagicMock(), 'send_message_to_user1': MagicMock(),
        'retrieve_json': MagicMock(return_value={}),
        'strip_json_values': MagicMock(return_value=''),
        'save_conversation_db': MagicMock(return_value='1'),
    }
    tools = {name: fn for name, _d, fn in build_core_tool_closures(ctx)}
    out = _json.loads(asyncio.run(tools['get_coding_benchmarks']('all')))
    assert out['local'] == {}
