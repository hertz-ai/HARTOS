"""
Behavioural degrade tests for the CANONICAL default-sink volume probe
(integrations/agent_engine/liquid_ui_service.py: _volume_get / _vol_run).

WHY a separate file from test_shell_audio_api.py: that file reimplements the
pactl-JSON `shell_audio` route inside the test (`_make_audio_app`) and asserts a
COPY, so a regression in the SHIPPED helper would not fail it. These tests import
and call the REAL module-level helpers that BOTH the background connectivity
prober (_ConnectivityCache) and the /api/shell/volume write routes depend on,
mocking ONLY the subprocess boundary (wpctl/pactl). This is the Gate-4/Gate-5
behavioural test of the canonical audio read path - the one whose try/except is
the whole "degrade, never crash" contract for the audio dimension.

Coverage = the audio "no audio out" / "cannot read volume" DEGRADE contract:
  * neither wpctl nor pactl present       -> available:False, never a crash/hang
                                             ("wpctl AND pactl both absent" mode)
  * a tool times out / raises OSError     -> _vol_run returns None (best-effort)
  * wpctl reports MUTED at level 0        -> available:True, volume 0, muted True
                                             (the steward's muted/zero-on-boot read)
  * wpctl reports a normal level          -> parsed percent, muted False
  * wpctl absent, pactl fallback          -> the pactl mute + volume are read
  * an out-of-range pactl percent         -> clamped into 0..150 (no bad value)

Importing liquid_ui_service is ~1.6s cold; these are pure unit tests (no Flask
app, no service instance, no threads started) - just the two module functions.
"""

import subprocess
import unittest
from unittest.mock import patch, MagicMock

from integrations.agent_engine import liquid_ui_service as L


def _cp(returncode=0, stdout=""):
    """A stand-in for subprocess.CompletedProcess exposing just .returncode/.stdout
    (the only two attributes _volume_get reads)."""
    return MagicMock(returncode=returncode, stdout=stdout)


class VolRunDegradeTest(unittest.TestCase):
    """_vol_run is the single guarded subprocess wrapper. It must swallow the
    three boundary faults (missing tool, timeout, OS error) and return None, so no
    caller ever sees an exception from a missing or faulting audio tool."""

    @patch.object(L.subprocess, 'run', side_effect=FileNotFoundError)
    def test_missing_tool_returns_none(self, _m):
        self.assertIsNone(
            L._vol_run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@']))

    @patch.object(L.subprocess, 'run',
                  side_effect=subprocess.TimeoutExpired(cmd='wpctl', timeout=4))
    def test_timeout_returns_none(self, _m):
        self.assertIsNone(
            L._vol_run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@']))

    @patch.object(L.subprocess, 'run', side_effect=OSError("boom"))
    def test_oserror_returns_none(self, _m):
        self.assertIsNone(L._vol_run(['pactl', 'get-default-sink']))

    @patch.object(L.subprocess, 'run', return_value=_cp(0, "ok"))
    def test_success_returns_completed(self, _m):
        r = L._vol_run(['wpctl', 'status'])
        self.assertIsNotNone(r)
        self.assertEqual(r.returncode, 0)


class VolumeGetDegradeTest(unittest.TestCase):
    """_volume_get is what the read routes + the connectivity snapshot call. Its
    contract: ALWAYS return a dict, never raise; available:False when nothing can
    answer; correctly parsed mute + level when a tool can."""

    @patch.object(L.subprocess, 'run', side_effect=FileNotFoundError)
    def test_no_tools_degrades_to_unavailable(self, _m):
        """The 'wpctl AND pactl both absent' (minimal live USB) failure mode:
        _volume_get returns available:False with null fields, and does NOT raise."""
        out = L._volume_get()
        self.assertEqual(
            out, {'available': False, 'volume': None, 'muted': None})

    def _dispatch(self, mapping):
        """Build a subprocess.run side_effect returning a mocked CompletedProcess
        from a (tool, subcommand) -> (rc, stdout) mapping. Anything unmapped is a
        rc1 empty result (== the tool ran but had no answer, e.g. no default sink)."""
        def se(cmd, **kw):
            key = (cmd[0], cmd[1] if len(cmd) > 1 else '')
            rc, out = mapping.get(key, (1, ''))
            return _cp(rc, out)
        return se

    def test_wpctl_muted_zero(self):
        """wpctl get-volume reports a MUTED sink at level 0 -> available, volume 0,
        muted True (the exact reading behind the steward's 'no audio out')."""
        se = self._dispatch({
            ('wpctl', 'get-volume'): (0, 'Volume: 0.00 [MUTED]'),
        })
        with patch.object(L.subprocess, 'run', side_effect=se):
            out = L._volume_get()
        self.assertTrue(out['available'])
        self.assertEqual(out['tool'], 'wpctl')
        self.assertEqual(out['volume'], 0)
        self.assertTrue(out['muted'])

    def test_wpctl_normal_level(self):
        se = self._dispatch({
            ('wpctl', 'get-volume'): (0, 'Volume: 0.55'),
        })
        with patch.object(L.subprocess, 'run', side_effect=se):
            out = L._volume_get()
        self.assertTrue(out['available'])
        self.assertEqual(out['tool'], 'wpctl')
        self.assertEqual(out['volume'], 55)
        self.assertFalse(out['muted'])

    def test_pactl_fallback_reads_mute_and_volume(self):
        """wpctl answers no usable volume (rc1, no 'Volume:') -> the pactl fallback
        path parses both the mute flag and the percent."""
        se = self._dispatch({
            ('wpctl', 'get-volume'): (1, ''),
            ('pactl', 'get-sink-mute'): (0, 'Mute: yes'),
            ('pactl', 'get-sink-volume'):
                (0, 'Volume: front-left: 26214 /  40% / -23.83 dB'),
        })
        with patch.object(L.subprocess, 'run', side_effect=se):
            out = L._volume_get()
        self.assertTrue(out['available'])
        self.assertEqual(out['tool'], 'pactl')
        self.assertEqual(out['volume'], 40)
        self.assertTrue(out['muted'])

    def test_pactl_percent_clamped_into_range(self):
        """A pathological pactl percent (200%) is clamped into 0..150 so the UI
        never receives an out-of-range volume."""
        se = self._dispatch({
            ('wpctl', 'get-volume'): (1, ''),
            ('pactl', 'get-sink-mute'): (0, 'Mute: no'),
            ('pactl', 'get-sink-volume'):
                (0, 'Volume: front-left: 131072 /  200% / 0.00 dB'),
        })
        with patch.object(L.subprocess, 'run', side_effect=se):
            out = L._volume_get()
        self.assertTrue(out['available'])
        self.assertEqual(out['volume'], 150)
        self.assertFalse(out['muted'])

    def test_pactl_unparseable_degrades_to_unavailable(self):
        """wpctl unusable AND pactl returns a volume line with no parseable percent
        -> _volume_get falls through to available:False (never raises on bad text)."""
        se = self._dispatch({
            ('wpctl', 'get-volume'): (1, ''),
            ('pactl', 'get-sink-mute'): (0, 'Mute: no'),
            ('pactl', 'get-sink-volume'): (0, 'Volume: (unknown)'),
        })
        with patch.object(L.subprocess, 'run', side_effect=se):
            out = L._volume_get()
        self.assertEqual(
            out, {'available': False, 'volume': None, 'muted': None})


if __name__ == '__main__':
    unittest.main()
