"""Guard: the reuse turn extracts its reply from the same messages the loop
resolved — live group_chat.messages, else the state_transition snapshot
(#725: autogen empties the live list after a nested initiate_chat).

Measured 2026-09-03 23:37 on the installed build (Auto Research agent):
google_search executed with real results, the StatusVerifier returned
'completed', the loop resolved every step through the snapshot fallback,
then the final extraction checked bare `group_chat.messages`, found it
empty, logged 'no messages to extract a reply from' and returned '' —
Nunba answered from a direct knowledge-cutoff fallback with the real
results unused.  One resolver, used at both sites.
"""
import ast
import os
import unittest

_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _get_agent_response_src():
    src = open(_SRC, encoding='utf-8').read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == 'get_agent_response':
            return ast.get_source_segment(src, node) or ''
    raise AssertionError('get_agent_response not found')


class _GroupChat:
    def __init__(self, messages=None):
        self.messages = messages or []


class ReplyExtractionFallback(unittest.TestCase):
    def test_resolver_prefers_live_then_snapshot_then_empty(self):
        import hartos.reuse_recipe as rr
        snap = rr._reuse_msg_snapshot
        key = 'test-prompt-extraction'
        try:
            snap[key] = [{'role': 'assistant', 'content': 'from snapshot'}]
            self.assertEqual(rr._reuse_live_messages(_GroupChat(), key)[-1]['content'],
                             'from snapshot')
            live = _GroupChat([{'role': 'assistant', 'content': 'live'}])
            self.assertEqual(rr._reuse_live_messages(live, key)[-1]['content'], 'live')
            snap.pop(key, None)
            self.assertEqual(rr._reuse_live_messages(_GroupChat(), key), [])
        finally:
            snap.pop(key, None)

    def test_loop_and_final_extraction_use_the_one_resolver(self):
        src = _get_agent_response_src()
        self.assertNotIn('if not group_chat.messages:', src,
                         'bare live-list check — the snapshot fallback is bypassed')
        self.assertGreaterEqual(
            src.count('_reuse_live_messages(group_chat, user_prompt)'), 2)


if __name__ == '__main__':
    unittest.main()
