"""The named attach must run for EVERY action the walk dispatches, not once.

THE LIVE FAILURE (measured 2026-09-11, agent 88719487304, sessions
6c2dc0fc-7c93-4fe0-973e-f7466ff63f29_88719487304, two drives):

    "Tier-1 named attach" lines for this agent, WHOLE log, no window:  2
        02:28:34  action 1 names ['google_search'] -> 1 tools
        03:15:53  action 1 names ['google_search'] -> 1 tools

One per drive, action 1 only -- while each drive walked 6-9 actions. The attach
sits at the top of get_agent_response, BEFORE user_proxy.initiate_chat; the
action pointer then advances through the whole recipe inside the `while True:`
loop AFTER it. So the attach is per-CALL and the walk is per-ACTION, and every
action past the first is dispatched with whatever tools action 1 happened to
need.

WHAT IT COSTS. Agent 88719487304's actions 3, 4 and 7 all name
execute_coding_task -- a real tool (core/agent_tools.py:2175, registered
:2203), simply never attached. The fabrication gate correctly demanded it and
the model could not call it, so:
    03:20:23  refusing to advance action 3 ... unrun=['execute_coding_task']
    03:21:22  refusing (2/3)
    03:22:01  [REUSE] Action 3 TERMINATED, advancing   <- budget spent, forced
Action 4 went the same way with executed=[] -- nothing ran at all. Under the
anti-overclaim contract a force-advance is not achievement, so those actions
are failures that LOOK like progress in the advance count.

WHY THE ONE-IMPLEMENTATION SHAPE MATTERS. The naive fix is to paste the six
attach lines into the loop, which is a second copy of a rule that already
exists -- exactly the parallel-path drift this module has been bitten by (see
_vlm_merged_actions' docstring: "Three verbatim copies of the merge replaced
the flow action wholesale"). So the attach is lifted into ONE helper and BOTH
sites call it: the entry hook and the loop, on pointer change.

WHAT THIS GUARD DOES NOT CLAIM: that the model then calls the tool, or that the
agent reaches its goal. It claims the tool is available for every action rather
than only the first.
"""

import io
import os
import re
import types
import unittest

_HARTOS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Overridable so the guard can be pointed at a DIFFERENT revision of the file
# and proven to fail there. A guard that has only ever been run against the
# fixed source has not been shown to be capable of failing -- this suite has
# already been bitten twice by exactly that (see feedback_vacuous_guards).
# Used as: HARTOS_REUSE_SRC=<git show HEAD:...> python -m pytest <this file>
_SRC = os.environ.get('HARTOS_REUSE_SRC') or os.path.join(
    _HARTOS, 'hartos', 'reuse_recipe.py')


def _src():
    return io.open(_SRC, encoding='utf-8', errors='replace').read()


def _func(name, src):
    """One top-level function's source, bounded by the next TOP-LEVEL statement.

    Not `(?=^def )` -- reuse_recipe has module-level code between functions, and
    the greedy form swallows it (that mistake cost a NameError chasing the wrong
    file earlier in this session). Match the def line plus only blank/indented
    lines.
    """
    m = re.search(r'^def %s\(.*\n(?:(?:[ \t].*)?\n)*' % re.escape(name), src, re.M)
    return m.group(0) if m else ''


_HELPER = '_attach_named_tools_for_action'


class TestHelperExistsAndAttaches(unittest.TestCase):
    """The extracted attach must still do what the inline block did."""

    def _load(self, names, attached_calls, current_action=3):
        """exec the REAL helper against stubs for the globals it resolves.

        It takes only the session key and looks up its own agents and action
        id -- deliberately, and for the same reason
        ``_narrow_assistant_to_current_action`` does: an id or an agent handed
        in by a caller can be that caller's stale copy, and the tools would
        then be bound to an action the ledger has already left. So the stubs
        here have to supply ``user_agents`` (the 12-tuple the factory stores)
        and ``user_tasks``, not just arguments.
        """
        src = _src()
        body = _func(_HELPER, src)
        self.assertTrue(
            body,
            '%s is missing -- the attach is still inline, so it cannot be '
            'called a second time from the advance door without copying it'
            % _HELPER)

        logged = []

        def _log(level, msg):
            logged.append('%s|%s' % (level, msg))

        def _attach_for_names(names_, helper_, assistant_, registry_,
                              attached_, core_tools=None):
            attached_calls.append(list(names_))
            return len(names_)

        assistant = types.SimpleNamespace(_hart_attached_tools=set(),
                                          _hart_core_tools=None)
        # The real store holds a 12-tuple: assistant is [0], helper is [4].
        agents = tuple([assistant] + [object()] * 3 + [object()] * 8)

        ns = {
            'user_agents': {'u_1': agents},
            'user_tasks': {'u_1': types.SimpleNamespace(
                current_action=current_action)},
            'recipes': {'u_1': {'actions': [{}, {}, {}]}},
            '_reuse_action_tool_names': lambda up, aid: list(names),
            '_ctx_safe_log': _log,
            'attach_for_names': _attach_for_names,
        }
        exec(compile(body, _SRC, 'exec'), ns)
        # The helper imports attach_for_names from core.agent_tools by name at
        # call time; patch the real module so the stub is what it reaches.
        import core.agent_tools as _ct
        self._patched = (_ct, _ct.attach_for_names)
        _ct.attach_for_names = _attach_for_names
        return ns[_HELPER], logged, assistant

    def tearDown(self):
        patched = getattr(self, '_patched', None)
        if patched:
            patched[0].attach_for_names = patched[1]

    def test_it_attaches_the_tools_the_action_names(self):
        calls = []
        fn, logged, _ = self._load(['execute_coding_task'], calls)
        fn('u_1')
        self.assertEqual(
            calls, [['execute_coding_task']],
            'the helper did not pass the action\'s named tools to '
            'attach_for_names; action 3 of agent 88719487304 names exactly '
            'this tool and it was never attached across two live drives')
        self.assertTrue(any('action 3' in m for m in logged),
                        'the attach did not name the action it ran for')

    def test_it_reads_the_action_id_from_the_ledger_not_a_stale_copy(self):
        """Move the pointer, and the NEXT call must attach for the new action.

        This is the whole defect in one assertion: the old inline block read
        its id once per get_agent_response call, so it kept attaching for
        action 1 while the ledger walked on.
        """
        calls = []
        fn, logged, _ = self._load(['execute_coding_task'], calls,
                                   current_action=7)
        fn('u_1')
        self.assertTrue(
            any('action 7' in m for m in logged),
            'the helper did not follow the ledger to action 7 -- it must read '
            'user_tasks[...].current_action at call time, which is the single '
            'field _advance_reuse_action writes')

    def test_resolved_nothing_still_says_so(self):
        """The both-outcomes logging must survive the extraction."""
        calls = []
        fn, logged, _ = self._load([], calls)
        fn('u_1')
        self.assertEqual(calls, [], 'nothing to attach, so nothing should be')
        self.assertTrue(
            any('names no tool' in m for m in logged),
            'a resolved-nothing round went silent again -- that is the exact '
            'gap the both-outcomes logging was added to close')
        self.assertTrue(any('holds 3 action(s)' in m for m in logged),
                        'the store count that separates []\'s causes is gone')

    def test_it_logs_at_info_not_debug(self):
        """0 of 45,599 lines on this build were captured at DEBUG."""
        calls = []
        fn, logged, _ = self._load(['execute_coding_task'], calls)
        fn('u_1')
        self.assertTrue(logged, 'the attach logged nothing at all')
        self.assertFalse(
            [m for m in logged if m.startswith('debug|')],
            'the attach line is at DEBUG, where this build captures nothing '
            '-- the same invisibility that hid this defect for two drives')


class TestItRunsForEveryAction(unittest.TestCase):
    """RED until the attach re-runs wherever the action pointer moves.

    WHERE THAT IS, established by reading rather than assumed: the pointer
    field ``user_tasks[user_prompt].current_action`` is ASSIGNED at exactly
    one line in the module (inside ``_advance_reuse_action``), and that
    function has exactly one caller (inside ``_advance_or_steer``), which in
    turn has six call sites across BOTH walk loops -- get_agent_response's
    and chat_agent's. So ``_advance_or_steer`` is the single door, and one
    call there covers every advance in the module. The first draft of this
    guard asserted the attach appeared textually inside get_agent_response's
    ``while True:`` block; that would have passed a fix covering four of the
    six sites and none of chat_agent's, and would have forced a second copy
    of the attach into the loop body. Both assertions below are kept honest
    by the ordering check: an attach placed ABOVE the advance would re-bind
    the tools of the action just finished.
    """

    def test_the_entry_hook_still_attaches_for_the_first_action(self):
        body = _func('get_agent_response', _src())
        self.assertTrue(body, 'get_agent_response is gone; re-point this guard')
        loop_at = body.find('while True:')
        self.assertGreater(loop_at, 0, 'the reuse while-loop is gone')
        self.assertGreaterEqual(
            body[:loop_at].count(_HELPER), 1,
            'the entry attach is gone -- action 1 would lose its tools')

    def test_the_advance_door_reattaches_after_moving_the_pointer(self):
        body = _func('_advance_or_steer', _src())
        self.assertTrue(body, '_advance_or_steer is gone; re-point this guard')

        moved_at = body.find('_advance_reuse_action(')
        self.assertGreater(
            moved_at, 0,
            '_advance_or_steer no longer calls _advance_reuse_action; the '
            'pointer moves somewhere else now and this guard is pointed at '
            'the wrong door')

        attach_at = body.find(_HELPER)
        self.assertGreater(
            attach_at, 0,
            'the one door every advance goes through does not re-attach, so '
            'every action after the first is dispatched with the previous '
            'action\'s tools. Measured live 2026-09-11 on agent 88719487304: '
            '2 "Tier-1 named attach" lines across two drives, both action 1, '
            'while 6-9 actions were walked each time; actions 3 and 4 name '
            'execute_coding_task, never got it, and died on their round '
            'budget with unrun=[\'execute_coding_task\']')
        self.assertGreater(
            attach_at, moved_at,
            'the attach runs BEFORE the advance, so it binds the tools of the '
            'action that just finished -- the same off-by-one the prompt '
            'narrow already has a comment about ("placed after the advance, '
            'it is what moves current_action")')

    def test_the_narrow_and_the_attach_stay_together(self):
        """They answer one question: the pointer moved, what re-points with it?

        If a later change moves one and not the other, the prompt will name an
        action whose tools are not attached (or the reverse) -- which is the
        state this whole defect family lived in.
        """
        body = _func('_advance_or_steer', _src())
        narrow = body.find('_narrow_assistant_to_current_action(user_prompt)\n'
                           '    #')
        self.assertGreater(
            narrow, 0,
            'the post-advance narrow call moved; prompt and tools are now '
            're-pointed at different moments')

    def test_the_attach_failure_is_not_swallowed_at_debug(self):
        """Same class as d6495f499: DEBUG is invisible on this build."""
        body = _func('get_agent_response', _src())
        # Anchor on the MESSAGE and read backwards to the nearest logger call.
        # Anchoring on the statement shape instead (`except ...:\n<call>`) made
        # this guard fail the moment the handler gained a comment block and a
        # line break -- a green-to-red flip that says nothing about severity,
        # which is the only thing being asserted here.
        at = body.find('"turn attach skipped')
        self.assertGreater(
            at, 0, 'the turn-attach handler moved; re-point this guard')
        levels = re.findall(r'current_app\.logger\.(\w+)\(', body[:at])
        self.assertTrue(levels, 'no logger call precedes the message')
        self.assertNotEqual(
            levels[-1], 'debug',
            'the turn-attach failure logs at DEBUG, and this build captured 0 '
            'of 45,599 lines at DEBUG -- so if the attach raises for actions '
            '2..N nothing says so, which is why this defect survived two live '
            'drives before being found by absence')


if __name__ == '__main__':
    unittest.main()
