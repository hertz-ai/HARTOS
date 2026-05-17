"""Regression guard for the 2026-04-29 daemon-freeze incident.

WorldModelBridge._init_in_process used to do
    from hart_intelligence import get_learning_provider, get_hive_mind

When called from a worker thread (e.g. agent_daemon →
record_interaction → _init_in_process), this triggered Python's
per-module import lock for the entire hart_intelligence import chain
(langchain, transformers, autogen, multimodal stacks).  On first
load the chain takes 300+ seconds.  The watchdog declared the
worker FROZEN at 300s and "restarted" it — but Python can't kill
threads, so the original kept holding the lock.  Each new worker
also blocked on the same lock, until 9 zombie daemon threads were
stacked behind a never-released import lock.  Net: zero goals
dispatched, zero spark spent, dashboard full of "completed" goals
that did no work.

Two contracts pinned here:
  1. `_init_in_process` source code does NOT contain
     `from hart_intelligence import …` (the offending statement).
  2. Functionally: when `hart_intelligence` is NOT in sys.modules,
     `_init_in_process` returns without taking the import lock —
     specifically without invoking the `import` machinery for
     `hart_intelligence`.

If a future refactor brings the worker-thread import back, both
tests fail loudly at CI time, not silently in production.

Run: pytest tests/unit/test_world_model_bridge_no_worker_import.py -v --noconftest
"""
import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestNoWorkerThreadImport(unittest.TestCase):
    def test_init_in_process_source_no_hart_intelligence_import(self):
        """AST-level guard: no real `from hart_intelligence import …`
        statement may appear inside `_init_in_process`.  Comments
        describing the historical bug don't trigger the test — only
        actual import statements do."""
        import ast
        from integrations.agent_engine import world_model_bridge

        source = open(world_model_bridge.__file__, encoding='utf-8').read()
        tree = ast.parse(source)
        target = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == '_init_in_process'):
                target = node
                break
        self.assertIsNotNone(target, '_init_in_process method not found')

        offenders = []
        for sub in ast.walk(target):
            if isinstance(sub, ast.ImportFrom):
                mod = sub.module or ''
                if mod == 'hart_intelligence' or mod.startswith(
                        'hart_intelligence.'):
                    offenders.append((sub.lineno, mod, [a.name for a in sub.names]))

        self.assertEqual(
            offenders, [],
            "_init_in_process contains real ImportFrom statements for "
            f"hart_intelligence: {offenders}. Worker threads triggering "
            "that import deadlock the daemon via Python's per-module "
            "import lock (incident 2026-04-29). Use sys.modules.get("
            "'hart_intelligence') instead — only consume the module when "
            "the bootstrap pre-warm has already loaded it on the main thread."
        )

    def test_init_in_process_does_not_trigger_import(self):
        """Functional guard: when hart_intelligence is absent from
        sys.modules, _init_in_process must NOT trigger an import.

        We install a finder that raises if hart_intelligence is asked
        for, then call _init_in_process directly.  If the method tries
        to import, the finder fires and the test fails.
        """
        from integrations.agent_engine import world_model_bridge

        # Save and clear hart_intelligence from sys.modules so a probe
        # looks like "first time".
        saved = sys.modules.pop('hart_intelligence', None)

        # Sentinel finder: refuses to load hart_intelligence by raising.
        class _RefusingFinder:
            attempted = []

            def find_spec(self, name, path=None, target=None):
                if name == 'hart_intelligence' or name.startswith(
                        'hart_intelligence.'):
                    _RefusingFinder.attempted.append(name)
                    # Returning None lets the next finder try; raising
                    # surfaces the violation immediately.
                    raise AssertionError(
                        f"_init_in_process attempted to import {name!r} "
                        f"from a worker thread — the bug is back. See "
                        f"world_model_bridge._init_in_process docstring."
                    )
                return None

        finder = _RefusingFinder()
        sys.meta_path.insert(0, finder)
        try:
            # Construct a bridge — calls __init__ which calls _init_in_process.
            # Don't import other heavy modules; we just need the codepath.
            try:
                bridge = world_model_bridge.WorldModelBridge()
            except AssertionError:
                # The finder fired — the test should fail.
                raise
            # If construction succeeds without triggering the finder, the
            # contract is satisfied: in-process probe did not import.
            self.assertEqual(
                _RefusingFinder.attempted, [],
                'No hart_intelligence import attempts expected on a clean '
                f'sys.modules state; got {_RefusingFinder.attempted}'
            )
            # bridge should still be usable in HTTP mode.
            self.assertFalse(
                bridge._in_process,
                'bridge should fall through to HTTP mode when '
                'hart_intelligence is not pre-loaded'
            )
        finally:
            sys.meta_path.remove(finder)
            if saved is not None:
                sys.modules['hart_intelligence'] = saved


if __name__ == '__main__':
    unittest.main()
