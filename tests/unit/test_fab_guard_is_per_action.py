"""The fabrication gate must not credit an action with an EARLIER action's tool run.

MEASURED LIVE 2026-09-08 21:52-21:56, agent 89555447799, drive 6 (rotation-proof
read across gui_app.log + .old):

    TOOL DISPATCHES (8 total, ALL before 21:53:51)
        21:52:44/46/51/56, 21:53:02 x2   google_search
        21:53:32, 21:53:51               execute_windows_or_android_command
    ACTIONS ADVANCED: 18
        action 1      names ['google_search']
        actions 2-18  names ['execute_windows_or_android_command']   (17)

    => execute_windows_or_android_command: DISPATCHED 2, NAMED BY 17 ADVANCES

Actions 4..18 — fifteen of them — advanced AFTER the last dispatch, ~10s apart,
every one logging `unrun=[]`.  Their tool never ran for them.

WHY: `_reuse_fabricated_tools` builds `executed` by scanning
`group_chat.messages` PLUS every agent's whole `_oai_messages` buffer, with no
time or action window.  `clear_history=False` means those accumulate for the
session, so one real role='tool' result puts the name in `executed`
permanently and every later action naming it passes for free.  The gate cannot
fail after a tool's first successful run.

The function's own docstring already records this once — "the set also
accumulates all session, so it saturated to the whole 25-name roster and
unrun=[] became unreachable; nine fabricated 'completed' verdicts advanced".
The remedy applied then ("fail-open is preserved by SCOPE") widened WHERE it
looks (group log + pairwise buffers, for the 2026-09-05 Trading miss); it never
windowed WHEN.  A fix on one axis reintroduced the documented defect on another.

Same family as feedback_vacuous_guards: a guard that cannot fail is not one.

    python -m pytest tests/unit/test_fab_guard_is_per_action.py --noconftest -q
"""
from types import SimpleNamespace

import pytest

from hartos.reuse_recipe import _reuse_fabricated_tools, _reuse_present_call_ids


TOOL = 'execute_windows_or_android_command'


def _agent(names):
    return SimpleNamespace(_function_map={n: (lambda: None) for n in names},
                           llm_config=None, _oai_messages={})


def _call(cid, fn):
    return {'role': 'assistant', 'tool_calls': [
        {'id': cid, 'function': {'name': fn}}]}


def _result(cid, body='ok, done'):
    return {'role': 'tool', 'tool_call_id': cid, 'content': body}


def _wire(monkeypatch, action_text, messages, seen=None):
    """A session whose current action names TOOL, with `messages` on the wire."""
    from hartos import reuse_recipe as rr
    task = SimpleNamespace(
        actions=[action_text],
        current_action=1,
        get_action=lambda i: action_text,
        evidence_seen_call_ids=set(seen or ()),
    )
    monkeypatch.setitem(rr.user_tasks, 'u1', task)
    gc = SimpleNamespace(messages=list(messages), agents=[])
    return gc, [_agent([TOOL, 'google_search'])]


class TestPresentCallIds:

    def test_collects_ids_across_every_list(self):
        a = [_call('c1', TOOL)]
        b = [_call('c2', 'google_search'), {'role': 'user', 'content': 'hi'}]
        assert _reuse_present_call_ids([a, b]) == {'c1', 'c2'}

    @pytest.mark.parametrize("lists", [[], [[]], [None], [[{'role': 'user'}]]])
    def test_no_calls_is_an_empty_set_not_a_raise(self, lists):
        assert _reuse_present_call_ids(lists) == set()


class TestTheGateIsScopedToTheCurrentAction:

    def test_a_run_during_THIS_action_still_passes(self, monkeypatch):
        """The gate must keep working — this is the non-regression half."""
        gc, agents = _wire(monkeypatch, 'run %s now' % TOOL,
                           [_call('new1', TOOL), _result('new1')], seen=())
        assert _reuse_fabricated_tools('u1', 1, gc, agents) == []

    def test_a_run_from_a_PREVIOUS_action_no_longer_counts(self, monkeypatch):
        """THE LIVE DEFECT: actions 4..18 rode action 2-3's single dispatch.

        The only evidence on the wire is a call/result pair whose id was
        already present when this action was dispatched.  That is someone
        else's work.
        """
        gc, agents = _wire(monkeypatch, 'run %s now' % TOOL,
                           [_call('old1', TOOL), _result('old1')],
                           seen={'old1'})
        assert _reuse_fabricated_tools('u1', 5, gc, agents) == [TOOL], (
            "a tool that ran only for an EARLIER action must read as unrun — "
            "crediting it is how 15 actions advanced on 0 dispatches")

    def test_old_evidence_plus_a_fresh_run_passes(self, monkeypatch):
        """Re-running the same tool for a later action is legitimate."""
        gc, agents = _wire(monkeypatch, 'run %s now' % TOOL,
                           [_call('old1', TOOL), _result('old1'),
                            _call('new1', TOOL), _result('new1')],
                           seen={'old1'})
        assert _reuse_fabricated_tools('u1', 5, gc, agents) == []

    def test_the_live_shape_15_free_passes_become_unrun(self, monkeypatch):
        """One dispatch, then fourteen later actions naming the same tool."""
        wire = [_call('d1', TOOL), _result('d1')]
        gc, agents = _wire(monkeypatch, 'run %s now' % TOOL, wire, seen={'d1'})
        caught = [a for a in range(4, 19)
                  if _reuse_fabricated_tools('u1', a, gc, agents) == [TOOL]]
        assert len(caught) == 15, (
            "every one of actions 4..18 must be flagged unrun; got %d" % len(caught))

    def test_no_watermark_behaves_as_before(self, monkeypatch):
        """A session predating the stamp (or action 1) must not regress."""
        gc, agents = _wire(monkeypatch, 'run %s now' % TOOL,
                           [_call('c1', TOOL), _result('c1')], seen=None)
        assert _reuse_fabricated_tools('u1', 1, gc, agents) == []

    def test_an_action_naming_no_tool_is_still_untouched(self, monkeypatch):
        gc, agents = _wire(monkeypatch, 'write a short summary', [], seen={'x'})
        assert _reuse_fabricated_tools('u1', 3, gc, agents) == []

    def test_a_missing_task_never_raises(self, monkeypatch):
        from hartos import reuse_recipe as rr
        monkeypatch.setattr(rr, 'user_tasks', {}, raising=False)
        gc = SimpleNamespace(messages=[], agents=[])
        assert _reuse_fabricated_tools('nobody', 1, gc, [_agent([TOOL])]) == []
