"""Canonical env-flag accessors — ONE bool semantics, ONE int fallback.

Guards the 2026-09-01 sweep findings: 623 distinct flags, 91 with
per-site default drift, bool parsing split across 3 idioms, and int
flags whose defaults were sometimes str ('8080') and sometimes int
(8080).  Every future flag read goes through these two."""
import os
import unittest
from unittest.mock import patch

from core.config_cache import env_flag, env_int, flag_value


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

    def test_junk_fallback_is_announced(self):
        with self.assertLogs('hevolve_core', level='WARNING') as logs:
            self.assertEqual(self._with('eight', 8080), 8080)
        self.assertIn('X_TEST_INT', logs.output[0])

    def test_below_minimum_falls_back_loudly(self):
        env = {k: v for k, v in os.environ.items() if k != 'X_TEST_INT'}
        env['X_TEST_INT'] = '0'
        with patch.dict(os.environ, env, clear=True), \
                self.assertLogs('hevolve_core', level='WARNING') as logs:
            self.assertEqual(env_int('X_TEST_INT', 4, minimum=1), 4)
        self.assertIn('minimum', logs.output[0])

    def test_a_value_at_the_minimum_is_used(self):
        env = {k: v for k, v in os.environ.items() if k != 'X_TEST_INT'}
        env['X_TEST_INT'] = '1'
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(env_int('X_TEST_INT', 4, minimum=1), 1)


class FlagValueSemantics(unittest.TestCase):
    """flag_value is the ONE reading behind env_flag and a request body's
    boolean field (/chat's agent_tools, task #120)."""

    def test_a_bool_is_itself(self):
        self.assertTrue(flag_value(True, False))
        self.assertFalse(flag_value(False, True))

    def test_a_number_is_its_truth(self):
        self.assertFalse(flag_value(0, True))
        self.assertTrue(flag_value(1, False))
        self.assertTrue(flag_value(2, False))

    def test_strings_are_read_stripped_and_case_folded(self):
        for v in ('1', 'true', 'YES', 'On ', ' TRUE'):
            self.assertTrue(flag_value(v, False), v)
        for v in ('0', 'false', 'No', ' off', 'FALSE', 'False'):
            self.assertFalse(flag_value(v, True), v)

    def test_none_empty_and_junk_keep_the_default(self):
        for v in (None, '', '  ', 'nah', 'enable', 'tru', [], {}):
            self.assertTrue(flag_value(v, True), repr(v))
            self.assertFalse(flag_value(v, False), repr(v))

    def test_env_flag_is_flag_value_on_the_env_string(self):
        """Old-vs-new equivalence: env_flag read the env value itself before
        it delegated; every spelling the table knows, and junk, agree."""
        for v in ('1', 'true', 'YES', 'On ', '0', 'false', 'No', ' off',
                  'nah', 'enable', '2', 'tru', ''):
            for default in (True, False):
                env = {k: val for k, val in os.environ.items() if k != 'X_TEST_FLAG'}
                env['X_TEST_FLAG'] = v
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(env_flag('X_TEST_FLAG', default),
                                     flag_value(v, default), (v, default))


if __name__ == '__main__':
    unittest.main()
