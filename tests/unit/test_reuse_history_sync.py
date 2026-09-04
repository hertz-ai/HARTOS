"""Guard: the reuse w1 loop syncs group_chat.messages from the manager's own
conversation buffer when the group log is empty.

Live root cause 2026-09-05 (Auto Research reuse 18088688973, installed build,
DIAG datum nappend=0): in this reuse flow autogen accumulates the exchange in
the agents' pairwise `manager._oai_messages`, NOT in `group_chat.messages`
(the factory wraps the latter as a _GraphHookedList that, in this path, never
receives an append).  Every `group_chat.messages` read in get_agent_response
therefore saw an empty list and the turn bailed "empty mid-loop" — this was the
GENERAL reuse blocker: it collapsed every reuse turn before any action could
advance (measured on both Auto Research and Trading).

Fix: at the TOP of the w1 loop, when group_chat.messages is empty, extend it
from the manager's richest _oai_messages buffer (autogen's own store — no
parallel path, no new state).  It only runs when empty, so it neither
double-populates nor perturbs a healthy flow.  Live proof 2026-09-05 04:39:
`725-SYNC` fired, `empty mid-loop` count 0, google_search executed 8x.

AST/text guard (no live llama needed).
"""
import ast
import os
import unittest


SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


class ReuseHistorySync(unittest.TestCase):
    def setUp(self):
        self.src = open(SRC, encoding='utf-8').read()
        self.tree = ast.parse(self.src)  # also proves the module still parses

    def test_sync_reads_manager_oai_messages(self):
        self.assertIn("getattr(manager, '_oai_messages', None)", self.src,
                      "the empty-history sync must read the manager's own "
                      "conversation buffer (autogen's store), not a new one")

    def test_sync_extends_group_messages(self):
        self.assertIn('group_chat.messages.extend(_conv)', self.src,
                      "the sync must populate the SAME group_chat.messages the "
                      "loop reads — not a parallel list")

    def test_sync_picks_richest_conversation(self):
        self.assertIn('max(_mgr_msgs.values(), key=len', self.src,
                      "the sync must pick the longest pairwise buffer (the full "
                      "conversation as the manager saw it)")

    def test_sync_guarded_on_empty_only(self):
        # The sync must be gated so it cannot double-populate a healthy loop.
        self.assertIn('if not group_chat.messages:', self.src)
        self.assertIn('[725-SYNC]', self.src,
                      "keep the 725-SYNC log marker so the fix is observable live")

    def test_no_diag_instrumentation_left(self):
        # The temporary DIAG-725 probes (nappend counter, _diag_empty overrides,
        # the id/oai-counts empty-guard log) must not ship.
        for needle in ('DIAG-725', 'DIAG #725', '_diag_empty', '_nappend'):
            self.assertNotIn(needle, self.src,
                             f"temporary diagnostic {needle!r} must be removed "
                             f"before shipping the sync fix")


if __name__ == '__main__':
    unittest.main()
