"""When every tool result is empty, the pipeline reports it — it does not ask
the model to write an answer it has no data for.

THREE LIVE RUNS ESTABLISHED THAT AN INSTRUCTION IS NOT ENOUGH.  Same agent
(92583386981, "English Learning Session"), same prompt, driven as its owner
through the real /chat route:

  14:58:34  "you are currently at a B1 (Intermediate) CEFR level..."
            get_chat_history -> {"res": []}
            (confounded: the UUID read defect meant no store was queried)

  15:14:12  after a3905aabf -- read fixed, store really consulted:
            SimpleMem search took 0.002s, 0 results; res_in_filter":[] x14
            "you are currently at a B1 level and have a vocabulary of about
             1,500 words"                        <- MORE specific, not less

  15:23:56  after b8dd44a49 -- the steer's false premise removed and the
            EXPLICIT honesty instruction delivered (verified in-window:
            "If those results are empty" present 3x, old premise 0x):
            SimpleMem search took 0.002s, 0 results; res_in_filter":[] x14
            15:25:35 [SYNTHESIS] unrun=none -> 15:26:01 (14 -> 17 msgs,
            25.3s, a real model turn)
            "you are currently at a B1 (Intermediate) CEFR level. You have a
             strong vocabulary for daily topics but need to improve your
             skills in formal writing..."

Run 3 is the decisive one: the model was told, in the message it was
answering, to say so plainly and "not supply values, figures or facts of
your own" — and supplied them anyway.  So the remedy cannot be a sentence.

THE GATE.  When the action's tools ran and EVERY result was vacuous, the
honest answer is fully determined and needs no model: report the absence.
This mirrors #808/D42, where the pipeline already took over telling the user
a tool had not run rather than accepting the model's "successfully
completed" — the pipeline authoring an honest report is established here,
not new.  It is delivered by appending to group_chat.messages, exactly as
the _reuse_written_answer recovery in the same function already does, so
there is no second delivery path.  It is not '' either, so #797/D31 stands.

FAIL-OPEN EVERYWHERE.  Anything unparseable, anything substantive, or no
tool results at all -> the gate declines and the existing steer runs.  A
prose action (no tools, so no results) can never trigger it.

    python -m pytest \
      tests/unit/test_empty_tool_results_are_reported_not_invented.py \
      --noconftest -q
"""
import ast
import io
import json
import os

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')

_WANTED = ('_reuse_result_is_vacuous', '_reuse_tool_results_all_vacuous',
           '_reuse_call_id_to_tool_name',
           '_reuse_registered_and_referenced_tools',
           '_reuse_is_pipeline_text', '_reuse_is_written_answer',
           '_reuse_message_is_user_answer')


def _src():
    with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _fn_node(name):
    for node in ast.parse(_src()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('%s not found in reuse_recipe.py' % name)


def _ns():
    """Exec the real predicates out of the shipped file (importing the
    module pulls autogen -> torch)."""
    tree = ast.parse(_src())
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith('_REUSE_'):
                    body.append(node)
                    break
        elif isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            body.append(node)

    def retrieve_json(content):
        try:
            v = json.loads(str(content or ''))
        except Exception:
            return None
        return v if isinstance(v, dict) else None

    ns = {'retrieve_json': retrieve_json, 'json': json,
          '_reuse_evidence_msg_lists': lambda gc, ag: [gc]}
    mod = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), MODULE, 'exec'), ns)
    return ns


class TestVacuityOfOneResult:

    def test_the_two_shapes_measured_live_are_vacuous(self):
        f = _ns()['_reuse_result_is_vacuous']
        assert f('{"res": []}')            # run 1's body
        assert f('{"res_in_filter": []}')  # runs 2 and 3's body

    def test_blank_bodies_are_vacuous(self):
        f = _ns()['_reuse_result_is_vacuous']
        assert f('')
        assert f('   ')
        assert f(None)

    def test_real_data_is_not_vacuous(self):
        f = _ns()['_reuse_result_is_vacuous']
        assert not f('{"res": [{"message": {"content": "B1", "role": "user"}}]}')
        assert not f('{"res_in_filter": [{"message": {"content": "hi"}}]}')

    def test_a_zero_is_data_not_emptiness(self):
        """0 and False are answers; only absence is absence."""
        f = _ns()['_reuse_result_is_vacuous']
        assert not f('{"count": 0}')
        assert not f('{"ok": false}')

    def test_unparseable_bodies_fail_open(self):
        """Cannot judge -> treat as substantive, so the gate never fires on
        a body it does not understand."""
        f = _ns()['_reuse_result_is_vacuous']
        assert not f('Not able to perform this action now please try later')
        assert not f('<html>whatever</html>')


class TestVacuityAcrossTheAction:

    def _chat(self, *calls):
        """(tool_name, call_id, body) -> a proposal + its result, as autogen
        records them.  The result's own `name` is the EXECUTING AGENT, so the
        function name is only knowable through the proposing message."""
        msgs = []
        for name, cid, body in calls:
            msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': cid, 'function': {'name': name, 'arguments': '{}'}}]})
            msgs.append({'role': 'tool', 'tool_responses': [
                {'tool_call_id': cid, 'role': 'tool', 'content': body}]})
        return msgs

    def test_all_empty_is_reported(self):
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = self._chat(('get_chat_history', 'a', '{"res_in_filter": []}'),
                        ('recall_memory', 'b', '{"res": []}'))
        assert f(gc, [], set(), {'get_chat_history', 'recall_memory'})

    def test_one_substantive_result_declines_the_gate(self):
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = self._chat(('get_chat_history', 'a', '{"res_in_filter": []}'),
                        ('recall_memory', 'b',
                         '{"res": [{"message": {"content": "x"}}]}'))
        assert not f(gc, [], set(), {'get_chat_history', 'recall_memory'})

    def test_no_tool_results_at_all_declines_the_gate(self):
        """A PROSE action names no tool and produces no results; it must
        never be answered with 'the lookup came back empty'."""
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = [{'role': 'assistant', 'content': 'here is your summary'}]
        assert not f(gc, [], set(), set())

    def test_results_from_an_earlier_action_are_ignored(self):
        """Scoped by the same evidence watermark the fabrication gate uses,
        so one stale empty result cannot speak for this action."""
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = self._chat(('get_chat_history', 'old', '{"res": []}'))
        assert not f(gc, [], {'old'}, {'get_chat_history'})

    def test_an_unnamed_tools_receipt_cannot_veto_the_gate(self):
        """THE LIVE SHAPE, 2026-09-10 15:37 (agent 92583386981).

        FAB-GUARD: `action 1 names tool(s) ['get_chat_history']`.  Two results
        existed -- the named lookup came back {"res_in_filter": []}, and
        send_message_to_user (which the action never named) returned a plain
        success receipt.  The receipt is not JSON, so vacuity fails open and
        the gate returned False: the whole thing was silent because a delivery
        tool said "sent".
        """
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = self._chat(
            ('get_chat_history', 'c1', '{"res_in_filter": []}'),
            ('send_message_to_user', 'c2',
             'Message sent successfully to user with request_id: '
             'walk-92583386981-153418-intermediate'))
        assert f(gc, [], set(), {'get_chat_history'}), (
            'a success receipt from an UNNAMED tool still vetoes the '
            'gate -- the exact pair measured live 2026-09-10 15:37')

    def test_a_named_delivery_tool_still_counts(self):
        """No tool is special-cased: if the ACTION names it, its result is
        judged like any other.  The filter is provenance, not a blocklist."""
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = self._chat(('send_message_to_user', 'c2', 'Message sent'))
        assert not f(gc, [], set(), {'send_message_to_user'})

    def test_an_unresolvable_result_is_not_judged(self):
        """No proposing message -> no function name -> not attributable to a
        named tool, so it neither triggers nor vetoes."""
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = [{'role': 'tool', 'tool_call_id': 'orphan',
               'content': '{"res": []}'}]
        assert not f(gc, [], set(), {'get_chat_history'})


class TestReferencedToolsDerivation:
    """The extracted derivation must keep the fabrication gate's own rule."""

    class _Ag:
        def __init__(self, fns):
            self._function_map = {f: None for f in fns}
            self.llm_config = None

    def test_only_tools_the_text_names_are_referenced(self):
        f = _ns()['_reuse_registered_and_referenced_tools']
        ag = self._Ag(['get_chat_history', 'google_search'])
        names, ref = f([ag], 'First recall my progress via get_chat_history')
        assert 'get_chat_history' in names and 'google_search' in names
        assert ref == ['get_chat_history']

    def test_short_names_are_not_matched(self):
        """len(n) > 3 in the original -- keep it, or 'run'/'get' match prose."""
        f = _ns()['_reuse_registered_and_referenced_tools']
        names, ref = f([self._Ag(['run'])], 'run the thing')
        assert ref == []


class TestCallIdResolution:

    def test_name_comes_from_the_proposal_not_the_result(self):
        f = _ns()['_reuse_call_id_to_tool_name']
        msgs = [{'role': 'assistant', 'tool_calls': [
                    {'id': 'x', 'function': {'name': 'get_chat_history'}}]},
                {'role': 'tool', 'tool_call_id': 'x', 'name': 'Assistant',
                 'content': '{}'}]
        assert f([msgs]) == {'x': 'get_chat_history'}


class TestTheReportIsDeliverable:

    def test_report_is_not_refused_as_pipeline_text(self):
        """a5855f996 refuses module-written steers. The honest report is
        module-written too, so it must NOT collide with those markers or it
        would be walked past and never reach the user."""
        ns = _ns()
        rep = ns['_REUSE_NO_DATA_REPORT']
        assert not ns['_reuse_is_pipeline_text'](rep)

    def test_report_reads_as_a_user_answer(self):
        ns = _ns()
        msg = {'content': ns['_REUSE_NO_DATA_REPORT'],
               'name': 'Assistant', 'role': 'assistant'}
        assert ns['_reuse_message_is_user_answer'](msg)

    def test_report_states_the_absence_and_disclaims_invention(self):
        rep = _ns()['_REUSE_NO_DATA_REPORT'].lower()
        assert 'empty' in rep or 'nothing' in rep
        assert 'made' in rep or 'invent' in rep or 'not made' in rep


class TestTheSynthesisTurnConsultsIt:

    def test_the_no_data_report_is_reachable_from_the_synthesis_turn(self):
        """The pipeline must have a branch that reports the absence instead
        of asking the model for an answer it has no data for.

        Asserts the BRANCH, not who computes the predicate: the first cut
        computed it inline here and that was measurably too late (see
        TestTheAnswerIsTakenWhileTheActionIsStillCurrent).  What must not
        regress is that some branch of this function delivers
        _REUSE_NO_DATA_REPORT.

        Measured live 3x on agent 92583386981 (14:58, 15:14, 15:23) -- the
        last WITH an explicit honesty instruction delivered -- and it invented
        a CEFR level every time.  Then a 4th run (15:34) with the gate shipped
        but silent, which is what these tests now pin down.
        """
        fn = _fn_node('_reuse_synthesis_turn')
        assert any(isinstance(n, ast.Name)
                   and n.id == '_REUSE_NO_DATA_REPORT'
                   for n in ast.walk(fn)), (
            '_reuse_synthesis_turn has no branch that reports an empty '
            'lookup; it can only ever ask the model to write the answer')


class TestTheAnswerIsTakenWhileTheActionIsStillCurrent:
    """The 48 ms that made the first cut of this gate silent.

    _advance_reuse_action moves `current_action` and then re-stamps the
    evidence watermark.  After those two lines the finished action's tool
    calls are all inside the watermark and its text is out of range, so
    nothing downstream can scope evidence to it any more.  The vacuity answer
    must therefore be taken BEFORE both.
    """

    def _adv(self):
        return _fn_node('_advance_reuse_action')

    def _line_of_call(self, fn, name):
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == name:
                return n.lineno
        return None

    def test_vacuity_is_stamped_from_the_advance_site(self):
        assert self._line_of_call(self._adv(),
                                  '_stamp_action_result_vacuity') is not None, (
            'nothing records whether the finished action got data back, so '
            'the synthesis turn has only the post-advance state to read -- '
            'which is what made the gate fire 0x on 2026-09-10 15:37')

    def test_vacuity_is_stamped_before_the_watermark_moves(self):
        fn = self._adv()
        vac = self._line_of_call(fn, '_stamp_action_result_vacuity')
        mark = self._line_of_call(fn, '_stamp_action_evidence_watermark')
        assert vac is not None and mark is not None
        assert vac < mark, (
            'the watermark is re-stamped first, so by the time vacuity is '
            'computed the action-s own tool calls are already inside it and '
            'every result gets skipped as "an earlier action-s work"')

    def test_vacuity_is_stamped_before_the_pointer_moves(self):
        """current_action must still name the finished action."""
        fn = self._adv()
        vac = self._line_of_call(fn, '_stamp_action_result_vacuity')
        moves = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Attribute)
                         and t.attr == 'current_action' for t in n.targets)]
        assert vac is not None and moves
        assert vac < min(moves), (
            'the pointer advances first; with a 1-action recipe that leaves '
            'current_action past the end and the action text out of range')

    def test_synthesis_reads_the_stamp_and_does_not_recompute_it(self):
        fn = _fn_node('_reuse_synthesis_turn')
        called = {getattr(n.func, 'id', '') for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert '_reuse_tool_results_all_vacuous' not in called, (
            'the synthesis turn still computes vacuity for itself; at that '
            'point the watermark and the action pointer have both moved past '
            'the evidence, so the answer is always False')
        assert any(isinstance(n, ast.Constant)
                   and n.value == 'evidence_vacuous_action'
                   for n in ast.walk(fn)), (
            'the synthesis turn never reads the stamped answer')
