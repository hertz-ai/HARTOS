"""The coding agent's claude_code backend runs Claude Code only through invoke_claude.

invoke_claude (integrations/coding_agent/claude_code_backend.py) is where the
owner's copilot switch, the DLP scrub of what leaves this device, the
llm_outbound.jsonl record and the provider breaker live.  The coding agent's
backend (tool_backends.ClaudeCodeBackend) spawned `claude -p` itself, with
none of them.  Measured 2026-10-05 on the owner's desktop: 23 "Executing
claude_code: ...claude.EXE" runs in 25 hours while the switch marker
(~/.claude/hartos-copilot.off, set 2026-09-16) said OFF, and no claude-code
record in llm_outbound.jsonl.

Nothing here reaches Anthropic: both spawn seams are replaced.

    python -m pytest tests/unit/test_coding_claude_runs_through_the_one_launcher.py -q
"""
import json
import os
import tempfile
from unittest.mock import patch

os.environ['CLAUDE_CONFIG_DIR'] = tempfile.mkdtemp(prefix='coding_claude_')

import pytest  # noqa: E402

import core.llm_outbound_logger as outbound  # noqa: E402
import integrations.coding_agent.claude_code_backend as cc  # noqa: E402
import integrations.coding_agent.tool_backends as tb  # noqa: E402
from core.subprocess_safe import BoundedResult  # noqa: E402


@pytest.fixture(autouse=True)
def _node(monkeypatch, tmp_path):
    """A node that CAN run Claude Code (binary resolves, a login exists), so
    whether it MAY is the owner's switch alone.  The outbound record goes to
    a temp file."""
    monkeypatch.delenv('HARTOS_COPILOT_ENABLED', raising=False)
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'test-token')
    monkeypatch.setattr(cc, '_resolve_claude_bin', lambda: '/usr/bin/claude')
    monkeypatch.setattr(tb.shutil, 'which',
                        lambda name, *a, **k: '/usr/bin/claude' if name == 'claude' else None)
    log_path = str(tmp_path / 'llm_outbound.jsonl')
    outbound._close_handle()
    monkeypatch.setattr(outbound, '_get_log_path', lambda: log_path)
    # The router reads the benchmark DB; never the user's own one.
    from integrations.coding_agent import benchmark_tracker as bt
    bench = bt.BenchmarkTracker(db_path=str(tmp_path / 'bench.db'))
    monkeypatch.setattr(bt, 'get_benchmark_tracker', lambda: bench)
    cc.set_copilot_enabled(True)
    yield log_path
    outbound._close_handle()
    cc.set_copilot_enabled(True)


def _records(log_path):
    outbound._close_handle()
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding='utf-8') as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_switched_off_the_coding_agent_neither_offers_nor_starts_claude():
    cc.set_copilot_enabled(False)
    from integrations.coding_agent.tool_router import CodingToolRouter
    with patch.object(tb, 'run_bounded') as backend_spawn, \
            patch.object(cc, 'run_bounded') as launcher_spawn:
        assert 'claude_code' not in tb.get_available_backends()
        routed = CodingToolRouter().route('fix it', 'code_review',
                                          user_override='claude_code')
        result = tb.ClaudeCodeBackend().execute('fix it', {'working_dir': '/repo'})
    backend_spawn.assert_not_called()
    launcher_spawn.assert_not_called()
    assert routed is None or routed.name != 'claude_code'
    assert result['success'] is False
    assert result['category'] == 'off'


def test_switched_on_a_coding_run_is_the_launchers_run(_node):
    reply = json.dumps({'result': 'patched utils.py'})
    with patch.object(tb, 'run_bounded') as backend_spawn, \
            patch.object(cc, 'run_bounded',
                         return_value=BoundedResult(0, reply, '')) as launcher_spawn:
        result = tb.ClaudeCodeBackend().execute(
            'fix the bug in utils.py for a@b.io',
            {'working_dir': '/repo', 'model': 'sonnet'}, timeout=77)
    backend_spawn.assert_not_called()
    launcher_spawn.assert_called_once()
    argv = launcher_spawn.call_args[0][0]
    assert argv[0] == '/usr/bin/claude' and argv[1] == '-p'
    assert 'a@b.io' not in argv[2], 'the task left the device unscrubbed'
    assert argv[argv.index('--output-format') + 1] == 'json'
    assert argv[argv.index('--model') + 1] == 'sonnet'
    assert launcher_spawn.call_args[1]['cwd'] == '/repo'
    assert launcher_spawn.call_args[1]['timeout'] == 77
    assert result['success'] is True
    assert result['output'] == 'patched utils.py'
    assert result['tool'] == 'claude_code'
    rec = _records(_node)[-1]
    assert rec['source'] == 'claude-code'
    assert rec['body']['mode'] == 'agentic'
    assert rec['body']['egress'] == 'dlp-scrubbed'


def test_a_failed_launch_is_a_failed_coding_run():
    with patch.object(cc, 'run_bounded',
                      return_value=BoundedResult(-1, '', '', timed_out=True)):
        result = tb.ClaudeCodeBackend().execute('fix it', {}, timeout=5)
    assert result['success'] is False
    assert result['category'] == 'timeout'
    assert result['tool'] == 'claude_code'


def test_availability_is_the_launchers_own_answer(monkeypatch):
    """One predicate: what the router offers is what invoke_claude would run."""
    assert tb.ClaudeCodeBackend().is_installed() is cc.claude_code_available()
    cc.set_copilot_enabled(False)
    assert tb.ClaudeCodeBackend().is_installed() is False
    monkeypatch.delenv('CLAUDE_CODE_OAUTH_TOKEN')
    cc.set_copilot_enabled(True)
    assert tb.ClaudeCodeBackend().is_installed() is cc.claude_code_available()
