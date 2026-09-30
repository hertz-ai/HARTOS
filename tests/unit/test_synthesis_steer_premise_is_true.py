"""The synthesis steer must not presuppose that tool results exist.

MEASURED LIVE TWICE on agent 92583386981, driven as its owner through the
real /chat route.

RUN 2 (2026-09-10 15:14:12, HTTP 200, 68.7s) is the clean one, because the
retrieval defect had already been fixed (a3905aabf) so the store was really
consulted:

    15:15:16  [SYNTHESIS] reply would be raw control JSON — asking for the
              user-facing answer (... 14 msgs, unrun=none)
    15:15:21  [SYNTHESIS] round returned (14 -> 17 msgs)   <- +3, a REAL turn
    complete-steer posted 3x, INCOMPLETE steer 0x
    tool result: res_in_filter": []  x14, ZERO non-empty payloads
    SimpleMem search took 0.002s, 0 results

and the user was told:

    "Based on your English progress history, you are currently at a B1 level
     and have a vocabulary of about 1,500 words..."

"1500 words" and "B1" appear NOWHERE in any tool output in that window —
only in the model's own text. The store is genuinely empty and the answer
is invented.

WHY THE STEER IS THE CULPRIT, not the model being unruly: the branch that
fired tells the model to write the answer "using the real tool results from
this conversation". There were none. That is a FALSE PREMISE, and this file
already records what a false premise does here — see the comment above
_REUSE_SYNTHESIS_STEER_INCOMPLETE, measured 2026-09-09 08:14:20 on agent
33323830039, whose closing sentence is:

    "The model was obeying an instruction, not hallucinating."

That earlier fix corrected the premise for "the tools did not run". This is
the complementary case: the tools RAN and returned NOTHING. Same false
premise, same remedy — tell the truth about what is there.

SCOPE: this is a steer, i.e. an instruction, not a hard gate. It cannot
GUARANTEE the model declines to invent; it removes the instruction that
currently invites it. A hard groundedness gate is separate, larger, and
still open as #817. Do not read a green run here as "fabrication is
impossible now".

    python -m pytest tests/unit/test_synthesis_steer_premise_is_true.py \
        --noconftest -q
"""
import ast
import io
import os

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')


def _src():
    with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _module_strings():
    """NAME -> str for module-scope constants, resolving `"a" + _B`."""
    out = {}

    def _val(node):
        try:
            v = ast.literal_eval(node)
            return v if isinstance(v, str) else None
        except Exception:
            pass
        if isinstance(node, ast.Name):
            return out.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            lo, ro = _val(node.left), _val(node.right)
            if lo is not None and ro is not None:
                return lo + ro
        return None

    for node in ast.parse(_src()).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    v = _val(node.value)
                    if isinstance(v, str):
                        out[t.id] = v
    return out


class TestThePremiseIsNotAsserted:

    def test_steer_does_not_claim_results_exist(self):
        """The exact phrase that lied to the model on 15:15:16."""
        steer = _module_strings()['_REUSE_SYNTHESIS_STEER']
        assert 'using the real tool results from this conversation' not in steer, (
            'the complete-synthesis steer still tells the model to write the '
            'answer "using the real tool results from this conversation". '
            'When the tools ran and returned NOTHING that premise is false, '
            'and the model fills the gap: measured live 2026-09-10 15:14:12, '
            'res_in_filter=[] x14 and the user was told they are "B1" with '
            '"about 1,500 words".')

    def test_steer_says_what_to_do_when_results_are_empty(self):
        """A truthful premise is not enough; the empty case needs naming.

        Kept as a MEANING check over a small set of phrasings rather than one
        exact sentence, so a reword does not fail spuriously while an actual
        removal does.
        """
        steer = _module_strings()['_REUSE_SYNTHESIS_STEER'].lower()
        assert 'empty' in steer, (
            'the steer never mentions the empty-result case, so the model has '
            'no instruction for the situation that produced the fabrication')
        assert any(p in steer for p in ('say so', 'say that', 'do not supply',
                                        'do not invent', 'not invent')), (
            'the steer does not tell the model to report the absence instead '
            'of supplying values of its own')


class TestNoRegressionOnWhatAlreadyWorks:

    def test_answer_shape_marker_is_still_appended(self):
        """a5855f996 keys the pipeline-text predicate on this marker.

        If the steer stopped carrying it, the steer would once again be
        deliverable to the user as the agent's answer.
        """
        m = _module_strings()
        assert m['_REUSE_SYNTHESIS_ANSWER_SHAPE'] in m['_REUSE_SYNTHESIS_STEER']
        assert m['_REUSE_SYNTHESIS_ANSWER_SHAPE'] in m['_REUSE_SYNTHESIS_STEER_INCOMPLETE']

    def test_both_steers_still_forbid_re_running_tools(self):
        """The original job of this steer must survive the rewording."""
        m = _module_strings()
        for name in ('_REUSE_SYNTHESIS_STEER', '_REUSE_SYNTHESIS_STEER_INCOMPLETE'):
            s = m[name].lower()
            assert 'do not run' in s and 'tool' in s, name
            assert 'status object' in s, name

    def test_incomplete_steer_still_names_the_unrun_slot(self):
        """It is .format(unrun=...)ed at the call site; losing the slot would
        raise or silently drop the tool names the user deserves."""
        m = _module_strings()
        assert '{unrun}' in m['_REUSE_SYNTHESIS_STEER_INCOMPLETE']

    def test_answer_shape_carries_no_format_braces(self):
        """INCOMPLETE is .format()ed and the shape is appended to it, so a
        brace in the shape would blow up or eat text at post time."""
        shape = _module_strings()['_REUSE_SYNTHESIS_ANSWER_SHAPE']
        assert '{' not in shape and '}' not in shape
