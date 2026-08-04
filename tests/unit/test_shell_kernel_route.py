"""/api/shell/kernel — running kernel, loaded modules, taint (#25).

WHY THE ROUTE EXISTS
────────────────────
"Which modules are loaded, and is this kernel tainted" is the OS-level
counterpart of the unclaimed-device yellow bang already served by
/api/shell/drivers: it is what an agent needs to triage "this machine is
behaving strangely". `lsmod` was reachable only through the terminal's
read-only allowlist, which returns text an agent has to re-parse — the same
gap /api/shell/drivers closed for `lspci` by parsing it once, server-side.

WHAT THESE TESTS PIN
────────────────────
1. The parse, against REAL /proc/modules shapes — deps vs "-", varying column
   counts, and the malformed line that must be skipped rather than guessed.
2. The three answers, same contract as gpu_status():
       available=True             -> a module list
       available=False + 503      -> this kernel has no module list at all
   An empty 200 is specifically forbidden: "nothing loaded" and "no such
   concept" are different facts and an empty list asserts the first.
3. That a module list still returns when only the taint file is unreadable —
   a partial answer that says so beats failing the whole call.
"""
import builtins
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.agent_engine.shell_system_apis import (  # noqa: E402
    kernel_status, parse_proc_modules)

# Real shapes, copied from a running Linux box: a module with no dependents
# ("-"), one with two, and the six-column form with state + offset.
PROC_MODULES = """\
nvidia_uvm 1523712 0 - Live 0xffffffffc0b00000
snd_hda_intel 57344 5 snd_hda_codec_hdmi,snd_hda_codec Live 0xffffffffc0a00000
i915 3145728 12 - Live 0xffffffffc0000000
"""


def _client():
    from flask import Flask
    from integrations.agent_engine import shell_system_apis
    app = Flask(__name__)
    app.config['TESTING'] = True
    shell_system_apis.register_shell_system_routes(app)
    return app.test_client()


class TheParseHandlesRealProcModules(unittest.TestCase):

    def test_reads_name_size_refcount(self):
        mods = parse_proc_modules(PROC_MODULES)
        self.assertEqual(3, len(mods))
        self.assertEqual('nvidia_uvm', mods[0]['name'])
        self.assertEqual(1523712, mods[0]['size_bytes'])
        self.assertEqual(0, mods[0]['refcount'])

    def test_a_dash_means_nothing_depends_on_it(self):
        """'-' is the empty list, NOT a module literally named '-'."""
        mods = {m['name']: m for m in parse_proc_modules(PROC_MODULES)}
        self.assertEqual([], mods['nvidia_uvm']['used_by'])
        self.assertEqual(['snd_hda_codec_hdmi', 'snd_hda_codec'],
                         mods['snd_hda_intel']['used_by'])

    def test_state_is_kept_not_assumed_live(self):
        """A module stuck Loading/Unloading is a real fault worth seeing."""
        mods = parse_proc_modules(
            "wedged 4096 0 - Unloading 0xffffffffc0d00000\n")
        self.assertEqual('Unloading', mods[0]['state'])

    def test_a_malformed_line_is_skipped_not_invented(self):
        """A list with a fabricated entry is worse than a short one."""
        mods = parse_proc_modules(
            "good_mod 4096 0 - Live 0x1\n"
            "this is not a module line\n"
            "\n"
            "also_good 8192 1 good_mod Live 0x2\n")
        self.assertEqual(['good_mod', 'also_good'], [m['name'] for m in mods])

    def test_empty_input_is_empty_not_a_crash(self):
        self.assertEqual([], parse_proc_modules(''))
        self.assertEqual([], parse_proc_modules(None))


class TheRouteTellsTheTruth(unittest.TestCase):

    def test_reports_modules_when_proc_is_readable(self):
        import io
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == '/proc/modules':
                return io.StringIO(PROC_MODULES)
            if path == '/proc/sys/kernel/tainted':
                return io.StringIO('4096\n')
            return real_open(path, *a, **kw)

        with patch('builtins.open', fake_open):
            r = _client().get('/api/shell/kernel')
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertTrue(body['available'])
        self.assertEqual(3, body['module_count'])
        self.assertEqual(4096, body['tainted'])
        self.assertTrue(body['kernel_release'], "kernel release was not reported")

    def test_no_proc_modules_is_503_not_an_empty_module_list(self):
        """'This OS has no modules' must not read as 'nothing is loaded'."""
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path.startswith('/proc'):
                raise FileNotFoundError(2, 'No such file or directory', path)
            return real_open(path, *a, **kw)

        with patch('builtins.open', fake_open):
            r = _client().get('/api/shell/kernel')
        self.assertEqual(503, r.status_code)
        body = r.get_json()
        self.assertFalse(body['available'])
        self.assertIn('/proc/modules', body['error'],
                      "the degraded answer must name what was missing")

    def test_an_unreadable_taint_file_still_returns_the_modules(self):
        """A partial answer that says so beats failing the whole call."""
        import io
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == '/proc/modules':
                return io.StringIO(PROC_MODULES)
            if path == '/proc/sys/kernel/tainted':
                raise PermissionError(13, 'Permission denied', path)
            return real_open(path, *a, **kw)

        with patch('builtins.open', fake_open):
            body = kernel_status()
        self.assertTrue(body['available'])
        self.assertEqual(3, body['module_count'])
        self.assertIsNone(body['tainted'],
                          "an unreadable taint flag must be None, never 0 — "
                          "0 means 'this kernel is clean', which is a claim")

    def test_the_taint_failure_is_logged(self):
        import io
        from integrations.agent_engine import shell_system_apis
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == '/proc/modules':
                return io.StringIO(PROC_MODULES)
            if path == '/proc/sys/kernel/tainted':
                raise PermissionError(13, 'boom-taint', path)
            return real_open(path, *a, **kw)

        with patch('builtins.open', fake_open), \
                patch.object(shell_system_apis.logger, 'warning') as warn:
            kernel_status()
        self.assertTrue(warn.called, "the degrade was swallowed silently")


if __name__ == '__main__':
    unittest.main()
