"""#729 — `_chat_reply`'s persist must index the turn into the MemoryGraph.

Measured live 2026-08-30 17:44 (val-mem1): "Remember this: my favorite
color is teal" landed in simplemem buffer.json (the _chat_reply persist
block saved it) but the recall turn's `recall_memory("favorite_color")`
search returned only the QUESTION's own items — the fact was never in the
searchable store.  Root cause, double-verified by full reads:

  * The store indexer is `CustomAgentExecutor.prep_outputs`
    (hart_intelligence_entry.py:6173-6187): `_get_or_create_graph(...)`
    then `register_conversation('user'/'langchain', ...)`.
  * `_chat_reply`'s persist block exists precisely because "prep_outputs
    (the normal LangChain path) never fires for casual_conv or draft-first
    early returns" — but it ported only the SimpleMem half of
    prep_outputs' job and forgot the MemoryGraph half.  Early-return turns
    are therefore buffered but unrecallable.

The fix calls the SAME canonical primitives (`_get_or_create_graph` +
`register_conversation`) from the same gap-filler, and dedups against
executor turns (where prep_outputs already indexed) via a request-scoped
flask.g flag — prep_outputs stamps it, _chat_reply honors it.  No second
implementation of indexing exists.

    python -m pytest tests/unit/test_chat_reply_graph_indexing.py --noconftest -q
"""
import os
import tempfile
import threading
import unittest
from unittest.mock import patch


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class _RecorderGraph:
    def __init__(self):
        self.calls = []

    def register_conversation(self, role, text, session_key):
        self.calls.append((role, text, session_key))


class ChatReplyIndexesTheGraph(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._prev = os.environ.get('HEVOLVE_CACHE_DIR')
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('HEVOLVE_CACHE_DIR', None)
        else:
            os.environ['HEVOLVE_CACHE_DIR'] = self._prev

    def _reply(self, body, payload_extra=None, pre_stamp_indexed=False):
        import hart_intelligence_entry as hie

        graph = _RecorderGraph()
        payload = {'user_prompt': 'Remember this: my favorite color is teal.'}
        payload.update(payload_extra or {})
        with patch.object(hie, '_get_or_create_graph', lambda *a, **k: graph), \
                patch.object(hie.threading, 'Thread', _InlineThread), \
                patch.object(hie, '_tts_synthesize_and_publish',
                             lambda *a, **k: None):
            with hie.app.test_request_context('/chat', json=body):
                if pre_stamp_indexed:
                    from flask import g
                    g._hie_graph_indexed = True
                hie._chat_reply('t-gi-user', 't-gi-req', 'Noted - teal it is.',
                                **payload)
        return graph.calls

    def test_early_return_turn_is_indexed_into_the_graph(self):
        calls = self._reply({'media_mode': 'text'})
        roles = [c[0] for c in calls]
        self.assertIn('user', roles,
                      'the user prompt never reached register_conversation '
                      '- the fact stays unrecallable (#729)')
        self.assertIn('langchain', roles,
                      'the assistant reply never reached '
                      'register_conversation')
        user_call = next(c for c in calls if c[0] == 'user')
        self.assertIn('teal', user_call[1])

    def test_executor_turns_are_not_double_indexed(self):
        """prep_outputs already registered this request's turn - the
        request-scoped flag must make _chat_reply skip its own pass."""
        calls = self._reply({'media_mode': 'text'}, pre_stamp_indexed=True)
        self.assertEqual(calls, [],
                         'turn was indexed twice - prep_outputs and '
                         '_chat_reply both registered it')

    def test_no_user_prompt_means_no_indexing(self):
        """Callers without user_prompt (nothing to pair) stay untouched."""
        import hart_intelligence_entry as hie
        graph = _RecorderGraph()
        with patch.object(hie, '_get_or_create_graph', lambda *a, **k: graph), \
                patch.object(hie.threading, 'Thread', _InlineThread), \
                patch.object(hie, '_tts_synthesize_and_publish',
                             lambda *a, **k: None):
            with hie.app.test_request_context('/chat', json={}):
                hie._chat_reply('t-gi-user', 't-gi-req', 'hello')
        self.assertEqual(graph.calls, [])


if __name__ == '__main__':
    unittest.main()
