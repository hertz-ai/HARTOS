"""Canonical env-flag accessors — ONE bool semantics, ONE int fallback.

Guards the 2026-09-01 sweep findings: 623 distinct flags, 91 with
per-site default drift, bool parsing split across 3 idioms, and int
flags whose defaults were sometimes str ('8080') and sometimes int
(8080).  Every future flag read goes through these two."""
import os
import unittest
from unittest.mock import patch

from core.config_cache import env_flag, env_int


class EnvFlagSemantics(unittest.TestCase):

    def _with(self, value, default):
        env = {k: v for k, v in os.environ.items() if k != 'X_TEST_FLAG'}
        if value is not None:
            env['X_TEST_FLAG'] = value
        with patch.dict(os.environ, env, clear=True):
            return env_flag('X_TEST_FLAG', default)

    def test_unset_returns_declared_default_both_ways(self):
        self.assertTrue(self._with(None, True))
        self.assertFalse(self._with(None, False))

    def test_explicit_truthy_and_falsy_win_over_default(self):
        for v in ('1', 'true', 'YES', 'On '):
            self.assertTrue(self._with(v, False), v)
        for v in ('0', 'false', 'No', ' off'):
            self.assertFalse(self._with(v, True), v)

    def test_junk_keeps_default_never_flips(self):
        """The HEVOLVE_VLM_UNIFIED='yes'-reads-as-OFF class of bug:
        unrecognized values must keep the default, not disable."""
        for v in ('nah', 'enable', '2', 'tru'):
            self.assertTrue(self._with(v, True), v)
            self.assertFalse(self._with(v, False), v)


class EnvIntSemantics(unittest.TestCase):

    def _with(self, value, default):
        env = {k: v for k, v in os.environ.items() if k != 'X_TEST_INT'}
        if value is not None:
            env['X_TEST_INT'] = value
        with patch.dict(os.environ, env, clear=True):
            return env_int('X_TEST_INT', default)

    def test_unset_and_empty_return_typed_default(self):
        self.assertEqual(self._with(None, 8080), 8080)
        # int('') crashed the HEVOLVE_VLM_CAPTION_PORT sites — '' falls back
        self.assertEqual(self._with('', 8080), 8080)

    def test_numeric_string_parses(self):
        self.assertEqual(self._with(' 9891 ', 8080), 9891)

    def test_junk_falls_back_instead_of_raising(self):
        self.assertEqual(self._with('eight', 8080), 8080)


if __name__ == '__main__':
    unittest.main()
