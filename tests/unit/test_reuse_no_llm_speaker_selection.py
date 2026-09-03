"""Guard: no reuse group ever asks the model who speaks next.

Speaker order in every reuse group (main, persona, timer, visual) is
pipeline state — who just spoke, whether it was a tool call, what the
StatusVerifier said — and each state_transition decides the next speaker
from that.  Returning the string "auto" from a custom
speaker_selection_method hands the choice to autogen's
_auto_select_speaker, which spends an extra model call per hop, outside
the AgentLightningWrapper, to guess what the state already knows; any
engine failure inside that call ends the whole turn (measured 2026-09-03
23:26 on the installed build).  This walks every `state_transition*`
function in reuse_recipe.py and fails on any `return "auto"`, and checks
the main group's derived fallback is present.
"""
import ast
import os
import unittest

_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _state_transitions():
    src = open(_SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    return [(node, ast.get_source_segment(src, node) or '')
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name.startswith('state_transition')]


class NoLlmSpeakerSelection(unittest.TestCase):
    def test_every_reuse_group_is_state_driven(self):
        groups = _state_transitions()
        # main, persona, timer (state_transition1), visual (state_transition2)
        self.assertGreaterEqual(len(groups), 4, [n.name for n, _ in groups])
        offenders = []
        for node, _src in groups:
            for n in ast.walk(node):
                if (isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                        and n.value.value == 'auto'):
                    offenders.append(f'{node.name}:{n.lineno}')
        self.assertEqual(
            offenders, [],
            'state_transition returns "auto" — that hands speaker selection to '
            'a model call the pipeline state already answers')

    def test_main_group_derived_fallback_present(self):
        # The agent-mapping dict entry is unique to the main group; the timer
        # and visual groups carry r"@statusverifier" as a regex literal.
        main = [src for _node, src in _state_transitions()
                if '"@statusverifier": verify' in src]
        self.assertEqual(len(main), 1)
        self.assertIn('return verify if last_speaker is assistant else assistant', main[0])


if __name__ == '__main__':
    unittest.main()
