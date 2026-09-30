"""FAB-GUARD must say WHICH session it is talking about.

MEASURED LIVE 2026-09-09 20:47:49. The marker was emitted as

    [FAB-GUARD] action 5 names tool(s) ['execute_windows_or_android_command'];
    executed=['execute_windows_or_android_command']; unrun=[]

with no session anywhere on the line, while every agent AND every background
daemon on this box writes into the same server.log. A serial REUSE walk then
scored 88764372848 as "PARTIAL reached=[1] tools-ran=[4] of 5" -- action 4's
tool cannot run without action 4 being reached; the tools-ran count had swept
up a different agent's line. The established idiom is the
"for session: <user_prompt>" suffix that "Retrieved current_action_id" uses.

These tests call the REAL gate and the REAL watermark stamp and assert on the
record they log.  The first version regexed reuse_recipe.py for the first
`f"[FAB-GUARD] action {` literal; a later, unrelated emit
("has no registered group chat") matched first and the test went red without
any defect (2026-09-24).  Behaviour does not move when lines do.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

SESSION = 'fabguard_user_77'
TOOL = 'execute_windows_or_android_command'
DISPATCH = {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #1: Use {TOOL} to open settings.'}
PROPOSAL = {'role': 'assistant', 'name': 'Helper', 'content': None,
            'tool_calls': [{'id': 'call_fg_1', 'type': 'function',
                            'function': {'name': TOOL, 'arguments': '{}'}}]}
RESULT = {'role': 'tool', 'name': 'Assistant', 'content': 'Settings opened.',
          'tool_responses': [{'tool_call_id': 'call_fg_1', 'role': 'tool',
                              'content': 'Settings opened.'}]}


class _Task:
    current_action = 1

    def __init__(self):
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return f'Action #1: Use {TOOL} to open settings.'


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


class _Chat:
    def __init__(self, messages):
        self.messages = list(messages)
        self.agents = [_Agent('Helper', [TOOL]), _Agent('Assistant', [])]


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def rr():
    return pytest.importorskip('hartos.reuse_recipe')


@pytest.fixture
def captured(rr, monkeypatch):
    """A real Flask app context with a handler on BOTH loggers the module
    uses (current_app.logger and the 'reuse_recipe' fallback)."""
    flask = pytest.importorskip('flask')
    app = flask.Flask(__name__)
    cap = _Capture()
    app.logger.addHandler(cap)
    app.logger.setLevel(logging.DEBUG)
    fallback = logging.getLogger('reuse_recipe')
    fallback.addHandler(cap)
    monkeypatch.setitem(rr.user_tasks, SESSION, _Task())
    try:
        with app.app_context():
            yield cap
    finally:
        fallback.removeHandler(cap)


def _fab_lines(cap, kind):
    return [ln for ln in cap.lines if ln.startswith(f'[FAB-GUARD] {kind}')]


class TestTheActionVerdictLine:
    """`[FAB-GUARD] action N names tool(s) ...` -- the line the walk reads."""

    def _verdict(self, rr, captured):
        chat = _Chat([DISPATCH, PROPOSAL, RESULT])
        unrun = rr._reuse_fabricated_tools(SESSION, 1, chat, chat.agents)
        lines = [ln for ln in _fab_lines(captured, 'action') if 'names tool(s)' in ln]
        assert len(lines) == 1, f'expected one verdict line, got {captured.lines}'
        return unrun, lines[0]

    def test_it_names_the_session(self, rr, captured):
        _unrun, line = self._verdict(rr, captured)
        assert line.endswith(f'for session: {SESSION}'), (
            'the verdict must carry the "for session: <user_prompt>" suffix; '
            'unqualified, every agent\'s action-N lines are indistinguishable '
            f'in a shared log. Got: {line!r}')

    def test_it_still_reports_executed_and_unrun(self, rr, captured):
        """Attribution is ADDED, not swapped in for the evidence."""
        unrun, line = self._verdict(rr, captured)
        assert unrun == []
        assert f"executed=['{TOOL}']" in line and 'unrun=[]' in line, line


class TestTheWatermarkLine:
    """The sibling marker has the same defect and the same fix."""

    def test_watermark_names_the_session(self, rr, captured, monkeypatch):
        import hartos.lifecycle_hooks as lh
        chat = _Chat([DISPATCH, PROPOSAL, RESULT])
        monkeypatch.setattr(lh, 'get_registered_groupchat', lambda _s: chat)
        rr._stamp_action_evidence_watermark(SESSION)
        lines = _fab_lines(captured, 'watermark')
        assert len(lines) == 1, captured.lines
        assert lines[0].endswith(f'for session: {SESSION}'), lines[0]
        assert '1 pre-existing tool call(s)' in lines[0], lines[0]
