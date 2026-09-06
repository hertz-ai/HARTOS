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

    def test_sync_picks_richest_conversation(self):
        self.assertIn('max(_mgr_msgs.values(), key=len', self.src,
                      "the sync must pick the longest pairwise buffer (the full "
                      "conversation as the manager saw it)")

    def test_sync_fires_on_STALE_not_only_on_EMPTY(self):
        """The empty-only guard was vacuous after its first fire.

        Measured live 2026-09-06 (agent 89555447799, 13:06-13:19): [725-SYNC]
        fired ONCE at 13:08:02 with 10 msgs.  From then on group_chat.messages
        was non-empty — so `if not group_chat.messages` could never fire again —
        but it still received no appends, so it FROZE.  state_transition's own
        messages[-1] log proves it: of 193 calls over ~12 minutes, 191 saw the
        same ChatInstructor nudge ("You should "), and a StatusVerifier verdict
        never once reached [-1].

        Every advance path reads group_chat.messages[-1] (state_transition:2612,
        the w1 loop's completed/breakdown/under-report branches), so a frozen
        list makes all of them unreachable: GOT COMPLETED 0, FAB-GUARD 0,
        advancing 0, 101 loop iterations, current_action_id stuck at 1.

        The guard cured EMPTINESS but the defect is STALENESS — a guard that
        cannot fire for its own defect after the first time (feedback_vacuous_
        guards).  Gate on "shorter than the manager's conversation" instead.
        """
        self.assertIn('len(_conv) > len(group_chat.messages)', self.src,
                      "the sync must fire whenever the group log is SHORTER "
                      "than the manager's conversation, not only when it is "
                      "empty — an empty-only guard is vacuous after its first "
                      "fire and the list then freezes")

    def test_sync_replaces_in_place_never_appends_a_duplicate_prefix(self):
        """Blind extend() on a non-empty list would duplicate the prefix.

        That is the trap in the obvious version of this fix.  Slice-assignment
        replaces the contents while keeping the SAME list object, which matters
        because autogen/graph wrappers hold a reference to it — rebinding
        `group_chat.messages = [...]` would detach them.
        """
        self.assertIn('group_chat.messages[:] = list(_conv)', self.src,
                      "resync must replace IN PLACE (slice-assign), so it "
                      "neither duplicates the already-synced prefix nor "
                      "rebinds the list object autogen holds")
        self.assertNotIn('group_chat.messages.extend(_conv)', self.src,
                         "blind extend() on a now-non-empty list appends a "
                         "second copy of the whole conversation")

    def test_sync_marker_kept(self):
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
