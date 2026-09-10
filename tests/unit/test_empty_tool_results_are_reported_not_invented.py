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

    def _tool_msg(self, cid, body):
        return {'role': 'tool', 'tool_responses': [
            {'tool_call_id': cid, 'role': 'tool', 'content': body}]}

    def test_all_empty_is_reported(self):
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = [self._tool_msg('a', '{"res_in_filter": []}'),
              self._tool_msg('b', '{"res": []}')]
        assert f(gc, [], set())

    def test_one_substantive_result_declines_the_gate(self):
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = [self._tool_msg('a', '{"res_in_filter": []}'),
              self._tool_msg('b', '{"res": [{"message": {"content": "x"}}]}')]
        assert not f(gc, [], set())

    def test_no_tool_results_at_all_declines_the_gate(self):
        """A PROSE action names no tool and produces no results; it must
        never be answered with 'the lookup came back empty'."""
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = [{'role': 'assistant', 'content': 'here is your summary'}]
        assert not f(gc, [], set())

    def test_results_from_an_earlier_action_are_ignored(self):
        """Scoped by the same evidence watermark the fabrication gate uses,
        so one stale empty result cannot speak for this action."""
        f = _ns()['_reuse_tool_results_all_vacuous']
        gc = [self._tool_msg('old', '{"res": []}')]
        assert not f(gc, [], {'old'})


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

    def test_gate_is_wired_into_the_synthesis_turn(self):
        fn = _fn_node('_reuse_synthesis_turn')
        called = {getattr(n.func, 'id', '') for n in ast.walk(fn)
                  if isinstance(n, ast.Call)}
        assert '_reuse_tool_results_all_vacuous' in called, (
            '_reuse_synthesis_turn still asks the model to synthesise an '
            'answer even when every tool result was empty. Measured live 3x '
            'on agent 92583386981 (14:58, 15:14, 15:23) — the last of those '
            'WITH an explicit honesty instruction delivered — and it '
            'invented a CEFR level every time.')
