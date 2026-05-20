"""#224 — WorldModelBridge._init_in_process must NOT block the chat hot
path on a SHA-256 file scan when in-process mode can't apply anyway.

Live evidence (2026-05-20 RequestID 5390c08e):
  19:27:48  draft-telemetry logged (draft answer ready)
              │
              │  116-second silence — no log lines for this request_id
              │  (the scan runs synchronously inside __init__)
              │
  19:29:44  [WorldModelBridge] HTTP mode: http://localhost:8000
  19:29:58  LangChain local response surfaces to Nunba

Root cause: `SourceProtectionService.verify_hevolveai_integrity()`
SHA-256-hashes every file in the `hevolveai` package via
`pkg_root.rglob('*')`.  On this disk, 1000s of files = 116 seconds.
The scan ran even though `hart_intelligence` wasn't imported, so the
in-process path that the scan gates was structurally unreachable.

Fix: short-circuit BEFORE the scan when sys.modules['hart_intelligence']
is missing.  Pin that the scan is NOT invoked in that case.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch


_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class IntegrityScanSkipTests(unittest.TestCase):

    def setUp(self):
        # Wipe hart_intelligence from sys.modules so the short-circuit
        # path runs.  Save the original so other tests aren't affected.
        self._saved_hi = sys.modules.pop('hart_intelligence', None)

    def tearDown(self):
        if self._saved_hi is not None:
            sys.modules['hart_intelligence'] = self._saved_hi

    @patch('security.source_protection.SourceProtectionService.verify_hevolveai_integrity')
    def test_integrity_scan_skipped_when_hart_intelligence_not_loaded(
            self, mock_verify):
        """The 116s scan must not run when in-process mode is structurally
        impossible.  Verifies the #224 fast-path: if hart_intelligence
        isn't in sys.modules, return immediately to HTTP mode."""
        # Verify hart_intelligence is genuinely absent (setUp removed it).
        self.assertNotIn('hart_intelligence', sys.modules)

        from integrations.agent_engine.world_model_bridge import WorldModelBridge

        # Construct the bridge — __init__ calls _init_in_process which
        # SHOULD short-circuit before calling verify_hevolveai_integrity.
        bridge = WorldModelBridge()

        # The mock must NOT have been called.  Before #224 this fired
        # synchronously on every fresh boot's first /chat request.
        mock_verify.assert_not_called()
        # And the bridge correctly fell back to HTTP mode.
        self.assertFalse(bridge._in_process)

    @patch('security.source_protection.SourceProtectionService.verify_hevolveai_integrity',
           return_value={'verified': True})
    def test_integrity_scan_runs_when_hart_intelligence_loaded(
            self, mock_verify):
        """When hart_intelligence IS in sys.modules, the in-process
        upgrade path IS reachable, so the integrity scan should run
        (preserving the original security gate)."""
        # Inject a fake hart_intelligence so the short-circuit doesn't fire.
        fake_hi = MagicMock()
        fake_hi.get_learning_provider = MagicMock(return_value=None)
        fake_hi.get_hive_mind = MagicMock(return_value=None)
        sys.modules['hart_intelligence'] = fake_hi

        try:
            from integrations.agent_engine.world_model_bridge import WorldModelBridge
            WorldModelBridge()
            # Integrity scan should have run exactly once.
            mock_verify.assert_called_once()
        finally:
            sys.modules.pop('hart_intelligence', None)

    @patch('security.source_protection.SourceProtectionService.verify_hevolveai_integrity')
    def test_init_completes_under_one_second_when_skipped(self, mock_verify):
        """Bound the fast-path budget so a future regression that adds a
        synchronous probe to the unreachable-in-process path doesn't
        silently re-introduce the 116s stall.  1 second is generous —
        the real path is microseconds (one dict lookup + one log call).
        """
        import time
        self.assertNotIn('hart_intelligence', sys.modules)
        from integrations.agent_engine.world_model_bridge import WorldModelBridge

        t0 = time.monotonic()
        WorldModelBridge()
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 1.0,
                        f"WorldModelBridge.__init__ took {elapsed:.2f}s — "
                        f"the integrity-scan short-circuit may have regressed")
        mock_verify.assert_not_called()


class ModeGateTests(unittest.TestCase):
    """#224 — `_record_interaction_safely` must skip the
    WorldModelBridge entirely when the user is in local_only mode.

    This is the architectural fix that complements the bridge-side
    short-circuit: local-only users opt out of contributing to the
    hive's learning loop, so the bridge should never even be
    instantiated for them.  Mode switch (local→hive) is picked up
    on the next chat turn because user_pref is per-request.
    """

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_local_only_skips_bridge_entirely(self, mock_get_bridge):
        """user_pref='local_only' → get_world_model_bridge NOT called.
        No bridge instantiation, no integrity scan, no record."""
        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        dispatcher = SpeculativeDispatcher.__new__(SpeculativeDispatcher)
        dispatcher._record_interaction_safely(
            user_pref='local_only',
            user_id='u1', prompt_id='p1', prompt='hi',
            response='hello', model_id='qwen-draft',
            latency_ms=100, node_id=None, goal_id=None,
            escalation_reason=None,
        )
        mock_get_bridge.assert_not_called()

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_auto_mode_calls_bridge(self, mock_get_bridge):
        """user_pref='auto' → bridge called normally (the legacy path)."""
        mock_bridge = MagicMock()
        mock_get_bridge.return_value = mock_bridge

        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        dispatcher = SpeculativeDispatcher.__new__(SpeculativeDispatcher)
        dispatcher._record_interaction_safely(
            user_pref='auto',
            user_id='u1', prompt_id='p1', prompt='hi',
            response='hello', model_id='qwen-draft',
            latency_ms=100, node_id=None, goal_id=None,
            escalation_reason=None,
        )
        mock_get_bridge.assert_called_once()
        mock_bridge.record_interaction.assert_called_once()

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_hive_preferred_calls_bridge(self, mock_get_bridge):
        """user_pref='hive_preferred' → bridge called normally.  Hive
        users explicitly want their interactions to feed HevolveAI."""
        mock_bridge = MagicMock()
        mock_get_bridge.return_value = mock_bridge

        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        dispatcher = SpeculativeDispatcher.__new__(SpeculativeDispatcher)
        dispatcher._record_interaction_safely(
            user_pref='hive_preferred',
            user_id='u1', prompt_id='p1', prompt='hi',
            response='hello', model_id='qwen-draft',
            latency_ms=100, node_id=None, goal_id=None,
            escalation_reason=None,
        )
        mock_get_bridge.assert_called_once()

    @patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge')
    def test_default_user_pref_is_auto_calls_bridge(self, mock_get_bridge):
        """Backward compat: callers that don't pass user_pref default
        to 'auto' (the pre-#224 behavior) so the bridge still fires."""
        mock_bridge = MagicMock()
        mock_get_bridge.return_value = mock_bridge

        from integrations.agent_engine.speculative_dispatcher import (
            SpeculativeDispatcher,
        )
        dispatcher = SpeculativeDispatcher.__new__(SpeculativeDispatcher)
        # NO user_pref kwarg — must default to 'auto'.
        dispatcher._record_interaction_safely(
            user_id='u1', prompt_id='p1', prompt='hi',
            response='hello', model_id='qwen-draft',
            latency_ms=100, node_id=None, goal_id=None,
            escalation_reason=None,
        )
        mock_get_bridge.assert_called_once()


if __name__ == "__main__":
    unittest.main()
