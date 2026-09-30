"""The reuse loop's group log is brought up to date from the manager's own
conversation buffer, whenever it falls behind.

Live root cause 2026-09-05 (Auto Research reuse 18088688973, installed build,
DIAG datum nappend=0): in this reuse flow autogen accumulates the exchange in
the agents' pairwise `manager._oai_messages`, NOT in `group_chat.messages`
(the factory wraps the latter as a _GraphHookedList that, in this path, never
receives an append).  Every `group_chat.messages` read in get_agent_response
therefore saw an empty list and the turn bailed "empty mid-loop".

The fix (#725, now reuse_recipe._reuse_sync_group_log, called from the w1 loop
and the post-loop synthesis steer) resyncs the group log from the manager's
richest _oai_messages buffer.  These tests call that function on a real list
held by a group-chat stand-in and a manager whose _oai_messages is a dict of
pairwise buffers, and check what the group log holds afterwards.  They
replaced source-text checks that broke when the inline block became a
function (the behaviour they named was kept).
"""
import ast
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from hartos.reuse_recipe import _reuse_sync_group_log

SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos', 'reuse_recipe.py')


def _msgs(n, who='User'):
    return [{'role': 'user', 'name': who, 'content': f'{who} {i}'}
            for i in range(n)]


class _Agent:
    """A dict key with a name, as autogen's agents are."""

    def __init__(self, name):
        self.name = name


def _manager(**buffers):
    """A manager whose _oai_messages maps each agent to its pairwise log."""
    return SimpleNamespace(_oai_messages={
        _Agent(name): log for name, log in buffers.items()})


class ReuseHistorySync(unittest.TestCase):

    def test_sync_reads_manager_oai_messages(self):
        group = SimpleNamespace(messages=[])
        conv = _msgs(4)
        self.assertTrue(_reuse_sync_group_log(group, _manager(User=conv)))
        self.assertEqual(group.messages, conv)

    def test_sync_picks_richest_conversation(self):
        group = SimpleNamespace(messages=[])
        long_log, short_log = _msgs(6), _msgs(2, who='Helper')
        _reuse_sync_group_log(group, _manager(User=long_log, Helper=short_log))
        self.assertEqual(group.messages, long_log)

    def test_sync_fires_on_STALE_not_only_on_EMPTY(self):
        """The empty-only guard was vacuous after its first fire.

        Measured live 2026-09-06 (agent 89555447799, 13:06-13:19): [725-SYNC]
        fired ONCE at 13:08:02 with 10 msgs.  From then on group_chat.messages
        was non-empty, so `if not group_chat.messages` could never fire again,
        but it still received no appends, so it FROZE: 191 of 193
        state_transition calls saw the same ChatInstructor nudge at [-1], and
        no action ever advanced.  The sync must fire again whenever the
        manager's conversation has grown past what was last synced.
        """
        group = SimpleNamespace(messages=[])
        conv = _msgs(3)
        manager = _manager(User=conv)
        self.assertTrue(_reuse_sync_group_log(group, manager))
        self.assertEqual(len(group.messages), 3)

        conv.extend(_msgs(2, who='StatusVerifier'))   # the conversation grows
        self.assertTrue(_reuse_sync_group_log(group, manager))
        self.assertEqual(group.messages, conv)
        self.assertEqual(group.messages[-1]['name'], 'StatusVerifier')

    def test_nothing_new_is_not_a_resync(self):
        group = SimpleNamespace(messages=[])
        manager = _manager(User=_msgs(3))
        _reuse_sync_group_log(group, manager)
        self.assertFalse(_reuse_sync_group_log(group, manager))
        self.assertEqual(len(group.messages), 3)

    def test_sync_replaces_in_place_never_appends_a_duplicate_prefix(self):
        """Blind extend() on a non-empty list would duplicate the prefix, and
        rebinding `group_chat.messages = [...]` would detach the list object
        autogen and the graph wrappers hold.  The resync replaces the contents
        of the SAME list."""
        held = _msgs(2)
        group = SimpleNamespace(messages=held)
        conv = _msgs(2) + _msgs(3, who='Assistant')
        _reuse_sync_group_log(group, _manager(User=conv))
        self.assertIs(group.messages, held)            # same object
        self.assertEqual(group.messages, conv)         # no second prefix copy
        self.assertEqual(len(group.messages), 5)

    def test_sync_marker_kept(self):
        group = SimpleNamespace(messages=[])
        with mock.patch('hartos.reuse_recipe._ctx_safe_log') as log:
            _reuse_sync_group_log(group, _manager(User=_msgs(2)))
        self.assertTrue(any('[725-SYNC]' in str(c.args[1])
                            for c in log.call_args_list),
                        "keep the 725-SYNC log marker so the fix is observable live")

    def test_source_guard_no_diag_instrumentation_left(self):
        # Removal can only be checked in the source: the temporary DIAG-725
        # probes (nappend counter, _diag_empty overrides, the id/oai-counts
        # empty-guard log) must not ship.
        src = open(SRC, encoding='utf-8').read()
        ast.parse(src)
        for needle in ('DIAG-725', 'DIAG #725', '_diag_empty', '_nappend'):
            self.assertNotIn(needle, src,
                             f"temporary diagnostic {needle!r} must be removed "
                             f"before shipping the sync fix")


if __name__ == '__main__':
    unittest.main()
