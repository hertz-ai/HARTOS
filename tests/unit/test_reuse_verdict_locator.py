"""Guard: the reuse loop finds the StatusVerifier verdict in the tail of the
group chat, not only at messages[-2] under a literal ChatInstructor
"TERMINATE".

Measured 2026-09-04 02:54:30 on the installed build (Auto Research agent,
prompt_id 18088688973): google_search executed with real results, the
StatusVerifier returned {"status": "completed", ...}, autogen appended the
ChatInstructor's default TERMINATE, and then the loop's own steer
`chat_instructor.initiate_chat("Hey @StatusVerifier ...")` appended a
ChatInstructor message on top.  Every re-check of the tail therefore saw a
ChatInstructor STEER at [-1] (same name as the sentinel, different content),
the `== 'TERMINATE'` gate never matched, the mention check saw
'@statusverifier' in the steer and `continue`d, and the loop injected another
steer — four spins in ~1s until count==4, current_action stayed 1, and the
turn's reply was the steer text itself.

`_reuse_locate_verdict(messages)` returns the LAST StatusVerifier message
that follows the last steer/sentinel boundary (or the last StatusVerifier
message at all), so the advance branch parses the verdict wherever autogen
and the loop's own injections left it.
"""
import unittest


def _m(name, content, role='assistant'):
    return {'role': role, 'name': name, 'content': content}


VERDICT = '{"status": "completed", "action": "Search the web", "action_id": 1, "message": "done"}'
STEER = ('Hey @StatusVerifier Agent, Please verify the status of the action 1: '
         'Search the web\n performed and Respond in the following format {"status": ...}')


class VerdictLocator(unittest.TestCase):
    def _loc(self):
        from hartos.reuse_recipe import _reuse_locate_verdict
        return _reuse_locate_verdict

    def test_classic_shape_verdict_then_terminate(self):
        msgs = [_m('User', 'go', 'user'), _m('Assistant', 'working'),
                _m('StatusVerifier', VERDICT), _m('ChatInstructor', 'TERMINATE', 'user')]
        self.assertEqual(self._loc()(msgs)['content'], VERDICT)

    def test_steer_appended_after_terminate_still_finds_verdict(self):
        # The live 02:54:30 shape: verdict, TERMINATE, then the loop's steer.
        msgs = [_m('User', 'go', 'user'), _m('Assistant', 'working'),
                _m('StatusVerifier', VERDICT), _m('ChatInstructor', 'TERMINATE', 'user'),
                _m('ChatInstructor', STEER, 'user')]
        self.assertEqual(self._loc()(msgs)['content'], VERDICT)

    def test_multiple_steers_stacked(self):
        msgs = [_m('StatusVerifier', VERDICT), _m('ChatInstructor', 'TERMINATE', 'user')]
        msgs += [_m('ChatInstructor', STEER, 'user')] * 4
        self.assertEqual(self._loc()(msgs)['content'], VERDICT)

    def test_latest_verdict_wins(self):
        old = '{"status": "pending", "action_id": 1, "message": "waiting"}'
        msgs = [_m('StatusVerifier', old), _m('ChatInstructor', STEER, 'user'),
                _m('Helper', 'ran the tool'), _m('StatusVerifier', VERDICT),
                _m('ChatInstructor', 'TERMINATE', 'user')]
        self.assertEqual(self._loc()(msgs)['content'], VERDICT)

    def test_no_verdict_returns_none(self):
        msgs = [_m('User', 'go', 'user'), _m('Assistant', 'working'),
                _m('ChatInstructor', STEER, 'user')]
        self.assertIsNone(self._loc()(msgs))
        self.assertIsNone(self._loc()([]))

    def test_loop_uses_the_locator_not_a_fixed_offset(self):
        import ast, os
        src_path = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')
        src = open(src_path, encoding='utf-8').read()
        tree = ast.parse(src)
        body = next(ast.get_source_segment(src, n) for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == 'get_agent_response')
        self.assertIn('_reuse_locate_verdict(', body)
        self.assertNotIn("_live_msgs[-2][\"content\"]", body,
                         'fixed [-2] verdict read — a steer on top strands the verdict')
        self.assertNotIn("_live_msgs[-1]['content'] == 'TERMINATE'", body,
                         'literal TERMINATE gate — a steer at [-1] has the same name')


if __name__ == '__main__':
    unittest.main()
