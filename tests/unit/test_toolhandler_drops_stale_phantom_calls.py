"""A tool call from a FINISHED action must not haunt the next action.

MEASURED LIVE 2026-09-09 06:46:23-06:46:34, agent 33323830039, installed
build.  Action 1 had completed honestly (`[FAB-GUARD] ... unrun=[]`, full
lifecycle to `terminated`).  Action 2 then got its own full round allowance
and still could not finish.  Its 10.4-second window:

    4 LLM calls, EVERY one over budget and left-trimmed
        [TRIM] left-trimmed 13 msg(s) — est 6291 -> 5431, budget 5484
        [TRIM] left-trimmed  7 msg(s) — est 6273 -> 5272, budget 5484
        [TRIM] left-trimmed  7 msg(s) — est 6371 -> 5370, budget 5484
        [TRIM] left-trimmed 14 msg(s) — est 6519 -> 5468, budget 5484
    7 placeholder fills vs 3 real results
        [TOOL-ANSWER-FILL] <id> <- placeholder (no peer holds it)   x7
        [TOOL-ANSWER-FILL] <id> <- REAL result 157 chars            x3
    8 x "Found 7..10 historical tool calls with missing responses"
    final verdict: {'status': 'pending', 'action': 'cd C:\\Users\\sathi\\
                    Documents', 'action_id': 2, ...}

The model said `pending` because it could not see its own result: 70% of the
tool answers in its view were HISTORICAL_TOOL_PLACEHOLDER.

MECHANISM (apply_transform STEP 4).  `historical_pending_calls` is every
unanswered tool_call id anywhere in the accumulated conversation, and under
`clear_history=False` NOTHING ever ages one out.  A call the model announced
during action 1 but never executed is still unanswered during action 2, so a
placeholder is minted for it on every later request — forever.  Each one
costs budget AND tells the model "your tools produced nothing".

WHY MORE ROUNDS CANNOT FIX IT: measured across that window the body GREW,
6291 -> 6519 est tokens.  Every extra round adds history and trims harder.

THE WATERMARK ALREADY KNOWS.  `_stamp_action_evidence_watermark` records
`evidence_seen_call_ids` at each dispatch — "tool runs that already existed
when THIS action started", i.e. someone else's work (reuse_recipe.py, the
fabrication gate's own window).  An id that is BOTH in that watermark AND
still unanswered belongs to a finished action and produced nothing.  Drop it
rather than answering it with a manufactured string: no new mechanism, no
second notion of "whose call is this".

    python -m pytest tests/unit/test_toolhandler_drops_stale_phantom_calls.py \
        --noconftest -q
"""
import logging

import pytest


HANDLER = 'hartos.helper'


@pytest.fixture(autouse=True)
def _app_context():
    """apply_transform logs through flask.current_app on every call.

    It is an autogen TransformMessages capability and always runs inside a
    request, so a bare app context is the honest stand-in.  Logging is muted
    because the transform dumps the whole conversation at INFO.
    """
    flask = pytest.importorskip('flask')
    app = flask.Flask(__name__)
    app.logger.setLevel(logging.CRITICAL)
    with app.app_context():
        yield


class _Task:
    """Stands in for user_tasks[user_prompt] — only the two fields read."""

    def __init__(self, current_action=2, seen=()):
        self.current_action = current_action
        self.evidence_seen_call_ids = set(seen)


def _assistant(call_id, name='execute_windows_or_android_command'):
    return {'role': 'assistant', 'name': 'Assistant', 'content': '',
            'tool_calls': [{'id': call_id, 'type': 'function',
                            'function': {'name': name, 'arguments': '{}'}}]}


def _tool_answer(call_id, content='Directory of C:\\Users\\sathi\\Documents'):
    return {'role': 'tool', 'name': 'execute_windows_or_android_command',
            'tool_call_id': call_id, 'content': content}


def _handler(mod, seen, current_action=2):
    return mod.ToolMessageHandler(
        user_tasks={'u_1': _Task(current_action, seen)},
        user_prompt='u_1', peer_agents=[])


def _placeholder_count(mod, out):
    ph = mod.HISTORICAL_TOOL_PLACEHOLDER
    return sum(1 for m in out
               if isinstance(m, dict) and m.get('role') == 'tool'
               and ph in str(m.get('content') or ''))


def _announced_ids(out):
    ids = set()
    for m in out:
        if isinstance(m, dict):
            for tc in (m.get('tool_calls') or []):
                if tc.get('id'):
                    ids.add(tc['id'])
    return ids


class TestPhantomFromAFinishedActionIsDropped:

    def test_stale_unanswered_call_gets_no_placeholder(self):
        """The measured defect. RED before the fix.

        The shape matters.  STEP 4 calls a pending id "historical" only when
        it is absent from the LAST assistant message that carries tool_calls;
        with the phantom as the newest call it is treated as ACTIVE and no
        placeholder is minted.  An earlier draft of this test stopped there
        and passed vacuously.  Live, action 2 has announced its own call, so
        action 1's phantom is genuinely historical — that is reproduced here.
        """
        mod = pytest.importorskip(HANDLER)
        # 'old1' was announced during action 1 and never executed; the
        # watermark stamped at action 2's dispatch therefore contains it.
        # 'new1' is action 2's own in-flight call, so 'old1' is historical.
        msgs = [
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'Perform the action'},
            _assistant('old1'),
            {'role': 'assistant', 'name': 'Assistant', 'content': 'Working on it.'},
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'Perform the action'},
            _assistant('new1'),
        ]
        out = _handler(mod, seen={'old1'}).apply_transform(msgs)
        assert _placeholder_count(mod, out) == 0, (
            "a phantom call from a FINISHED action was answered with "
            "HISTORICAL_TOOL_PLACEHOLDER — that is the 7-of-10 the live "
            "drive showed the model, and it is why action 2 reported pending")
        assert 'old1' not in _announced_ids(out), (
            'the phantom announcement must go with its placeholder, or the '
            'next transform re-mints one for it')
        assert 'new1' in _announced_ids(out), (
            "action 2's own call must survive the same pass that drops "
            "action 1's phantom")

    def test_the_current_actions_own_call_is_untouched(self):
        """Anti-vacuity: this must not silence the action that is running.

        `new1` is NOT in the watermark — it was announced by the CURRENT
        action — so it is live work, and dropping it would destroy the very
        tool call the action is waiting on.
        """
        mod = pytest.importorskip(HANDLER)
        msgs = [
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'Perform the action'},
            _assistant('new1'),
            {'role': 'assistant', 'name': 'Assistant', 'content': 'Working on it.'},
        ]
        out = _handler(mod, seen={'old1'}).apply_transform(msgs)
        assert 'new1' in _announced_ids(out), (
            "the current action's own announced call was dropped — the "
            'action can never complete if its call disappears')

    def test_a_real_answer_is_never_dropped(self):
        """A stale call that DID produce a result keeps both call and answer.

        The window is about phantoms, never about discarding real work: the
        fabrication gate reads these same answers to decide whether an action
        really ran.
        """
        mod = pytest.importorskip(HANDLER)
        msgs = [
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'Perform the action'},
            _assistant('old2'),
            _tool_answer('old2'),
            {'role': 'assistant', 'name': 'Assistant', 'content': 'Done.'},
        ]
        out = _handler(mod, seen={'old2'}).apply_transform(msgs)
        assert 'old2' in _announced_ids(out)
        answers = [m for m in out if isinstance(m, dict)
                   and m.get('role') == 'tool' and m.get('tool_call_id') == 'old2']
        assert answers, 'the real tool answer was dropped'
        assert mod.HISTORICAL_TOOL_PLACEHOLDER not in str(answers[0].get('content'))

    def test_no_watermark_means_no_dropping(self):
        """Fail-safe: with nothing stamped, behaviour is exactly as before.

        A session whose watermark was never recorded must not have its
        history silently pruned — uncertainty keeps the old behaviour.
        """
        mod = pytest.importorskip(HANDLER)
        msgs = [
            {'role': 'user', 'name': 'ChatInstructor', 'content': 'Perform the action'},
            _assistant('old3'),
            {'role': 'assistant', 'name': 'Assistant', 'content': 'Working on it.'},
        ]
        out = _handler(mod, seen=()).apply_transform(msgs)
        assert 'old3' in _announced_ids(out), (
            'with an empty watermark nothing is stale, so nothing may be '
            'dropped')

    def test_missing_session_does_not_raise(self):
        """apply_transform runs on EVERY llm call; it must never throw."""
        mod = pytest.importorskip(HANDLER)
        h = mod.ToolMessageHandler(user_tasks={}, user_prompt='nope', peer_agents=[])
        msgs = [{'role': 'user', 'content': 'hi'}, _assistant('x1')]
        assert h.apply_transform(msgs) is not None
