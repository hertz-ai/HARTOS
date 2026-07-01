"""
Behavioural tests for the boot-time audio rescue script
(nixos/modules/hart-audio-unmute.sh).

These run the REAL script - the exact bytes hart-audio.nix ships via
writeShellScript(readFile ...) - against STUB `wpctl` / `pactl` / `sleep`
executables placed first on PATH. The stubs record their argv to a log file, so
each test asserts the OBSERVABLE side effects (which control commands the script
issued), NOT the source text. This is a Gate-5 behavioural test with the audio
transport (wpctl/pactl) mocked at the process boundary.

Coverage (the audio failure modes + the degrade contract):
  * No default sink            -> clean no-op (NO set-mute / set-volume), exit 0
                                  == "no audio device" degrades gracefully.
  * wpctl sink muted + level 0 -> UNMUTE + rescue level to the floor (steward bug)
  * wpctl sink at level 0.45   -> UNMUTE only; deliberate level NOT clobbered
  * pactl fallback (no wpctl)  -> the pactl control vocabulary is used
  * a failing control tool     -> still exit 0 (best-effort, never bricks boot)
  * volume clamp               -> 999% clamps to 1.50 (150%)
  * FIRST boot (no stamp)      -> set the FULL floor UNCONDITIONALLY (even over a
                                  non-zero level), then a later boot never clobbers
  * hotplug reselection        -> no default sink but one EXISTS -> promote it,
                                  then rescue it (default-sink reselection)

First-boot isolation: the script writes a per-user stamp under $XDG_STATE_HOME.
Each test points XDG_STATE_HOME at a temp dir, and `_run(seed_stamp=...)` controls
whether the stamp pre-exists (True == a later/subsequent boot; False == first boot).

[Linux/POSIX] The artifact under test is a POSIX sh script; the test needs a real
`sh`. It SKIPS on a host without `sh` (e.g. the bare Windows dev box) rather than
assert nothing - it runs for real in CI and on any POSIX box.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

_SH = shutil.which("sh") or shutil.which("bash")

_SCRIPT = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..",
    "nixos", "modules", "hart-audio-unmute.sh"))


def _write_exec(path, body):
    with open(path, "w", newline="\n") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(_SH, "no POSIX sh on PATH (Windows dev box) - runs in CI")
@unittest.skipUnless(os.path.exists(_SCRIPT), "hart-audio-unmute.sh not found")
class HartAudioUnmuteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hart-audio-test-")
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.calllog = os.path.join(self.tmp, "calls.log")
        # Per-test first-boot stamp home so the script's once-per-user flag is
        # isolated from the real machine (and from other tests).
        self.state = os.path.join(self.tmp, "state")
        # A no-op `sleep` so the bounded sink-wait loop returns instantly.
        _write_exec(os.path.join(self.bindir, "sleep"), "#!/bin/sh\nexit 0\n")

    def _seed_stamp(self):
        """Pre-create the first-boot stamp so the script takes the SUBSEQUENT-boot
        path (rescue-when-0, never clobber) instead of the first-boot full set."""
        d = os.path.join(self.state, "hart")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "audio-firstboot"), "w").close()

    def _reset_calls(self):
        if os.path.exists(self.calllog):
            os.remove(self.calllog)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── stub builders ──────────────────────────────────────────────────────
    def _add_wpctl(self, getvol="Volume: 0.00 [MUTED]", has_sink=True):
        """Stub wpctl. `get-volume` prints `getvol` (rc 0) when has_sink, else
        rc 1 (no default sink). set-mute / set-volume / status log + rc 0."""
        body = (
            "#!/bin/sh\n"
            'echo "wpctl $*" >> "%s"\n'
            'case "$1" in\n'
            "  get-volume)\n"
            "    %s\n"
            "    ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        ) % (
            self.calllog,
            ('printf "%%s\\n" "%s"; exit 0' % getvol) if has_sink else "exit 1",
        )
        _write_exec(os.path.join(self.bindir, "wpctl"), body)

    def _add_pactl(self, default_sink="alsa_output.test", getvol="front-left: 0 / 0% / -inf"):
        body = (
            "#!/bin/sh\n"
            'echo "pactl $*" >> "%s"\n'
            'case "$1" in\n'
            "  get-default-sink) printf '%%s\\n' '%s'; exit 0 ;;\n"
            "  get-sink-volume)  printf '%%s\\n' '%s'; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        ) % (self.calllog, default_sink, getvol)
        _write_exec(os.path.join(self.bindir, "pactl"), body)

    def _add_pactl_hotplug(self, sink_name="alsa_output.hotplug", getvol="front-left: 0 / 0% / -inf"):
        """Stateful pactl stub for the hotplug reselection path: `get-default-sink`
        is EMPTY until `set-default-sink` is called (a marker file flips it), so
        promoting a sink makes it queryable - exactly like the real tool. `list
        short sinks` reports one available sink for the reselection to pick."""
        marker = os.path.join(self.tmp, "default_set")
        body = (
            "#!/bin/sh\n"
            'echo "pactl $*" >> "%s"\n'
            'case "$1" in\n'
            '  get-default-sink) if [ -e "%s" ]; then printf "%%s\\n" "%s"; else printf "\\n"; fi; exit 0 ;;\n'
            '  list)             printf "%%s\\t%%s\\tmodule\\n" "42" "%s"; exit 0 ;;\n'
            '  set-default-sink) : > "%s"; exit 0 ;;\n'
            '  get-sink-volume)  printf "%%s\\n" "%s"; exit 0 ;;\n'
            "  *) exit 0 ;;\n"
            "esac\n"
        ) % (self.calllog, marker, sink_name, sink_name, marker, getvol)
        _write_exec(os.path.join(self.bindir, "pactl"), body)

    # ── runner ─────────────────────────────────────────────────────────────
    def _run(self, vol="100", seed_stamp=True):
        env = dict(os.environ)
        # Stub dir FIRST so wpctl/pactl/sleep are shadowed; real coreutils still
        # resolve later on PATH (sed/grep/tr/printf/head/basename/command).
        env["PATH"] = self.bindir + os.pathsep + env.get("PATH", "")
        # Isolate the per-user first-boot stamp under a temp dir.
        env["XDG_STATE_HOME"] = self.state
        # seed_stamp=True == the stamp already exists == a SUBSEQUENT boot (the
        # default, so the existing rescue-when-0 / never-clobber tests keep their
        # meaning). seed_stamp=False == a genuine FIRST boot.
        if seed_stamp:
            self._seed_stamp()
        proc = subprocess.run(
            [_SH, _SCRIPT, str(vol)],
            env=env, capture_output=True, text=True, timeout=60)
        calls = ""
        if os.path.exists(self.calllog):
            with open(self.calllog) as f:
                calls = f.read()
        return proc.returncode, calls, proc.stderr

    # ── tests ──────────────────────────────────────────────────────────────
    def test_no_sink_degrades_to_clean_noop(self):
        """No wpctl sink AND no pactl default sink -> NO control commands, exit 0.
        This is the 'no audio device' degrade path."""
        self._add_wpctl(has_sink=False)
        self._add_pactl(default_sink="")  # empty => no default sink
        rc, calls, _ = self._run()
        self.assertEqual(rc, 0, "must exit 0 when there is no sink (never fail boot)")
        self.assertNotIn("set-mute", calls)
        self.assertNotIn("set-volume", calls)
        self.assertNotIn("set-sink-mute", calls)
        self.assertNotIn("set-sink-volume", calls)

    def test_no_tools_at_all_is_noop(self):
        """Neither wpctl nor pactl present -> clean no-op, exit 0."""
        rc, calls, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(calls, "")

    def test_wpctl_muted_zero_gets_unmuted_and_rescued(self):
        """Sink muted at level 0 -> UNMUTE + set level to the 60% floor."""
        self._add_wpctl(getvol="Volume: 0.00 [MUTED]")
        rc, calls, _ = self._run("60")
        self.assertEqual(rc, 0)
        self.assertIn("set-mute @DEFAULT_AUDIO_SINK@ 0", calls)
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 0.60", calls)

    def test_wpctl_unmuted_but_volume_zero_gets_rescued(self):
        """The SECOND distinct steward failure mode: a sink that is NOT muted but
        is pinned at VOLUME 0 on boot -> still silent. The rescue must raise it to
        the 60% floor (the unmute is unconditional + harmless on an already-unmuted
        sink). Without this the 'volume 0 on boot -> no audio out' mode stays silent."""
        self._add_wpctl(getvol="Volume: 0.00")  # no [MUTED] tag, level 0
        rc, calls, _ = self._run("60")
        self.assertEqual(rc, 0)
        # Unmute is unconditional (safe no-op when already unmuted).
        self.assertIn("set-mute @DEFAULT_AUDIO_SINK@ 0", calls)
        # The level-0 sink is rescued to the floor.
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 0.60", calls)

    def test_wpctl_unreadable_level_is_rescued(self):
        """DEGRADE: the sink exists (get-volume rc 0) but its level is UNPARSEABLE
        (a malformed `Volume:` line with no number). The rescue treats an unreadable
        level the same as 0 -> unmute + set the audible floor, so a sink we cannot
        read a level from is never left silent. It must never crash on the bad parse."""
        self._add_wpctl(getvol="Volume:")  # label present, no parseable number
        rc, calls, _ = self._run("60")
        self.assertEqual(rc, 0)
        self.assertIn("set-mute @DEFAULT_AUDIO_SINK@ 0", calls)
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 0.60", calls)

    def test_wpctl_nonzero_level_not_clobbered(self):
        """Sink at a deliberate 0.45 -> UNMUTE only; level left untouched."""
        self._add_wpctl(getvol="Volume: 0.45")
        rc, calls, _ = self._run("60")
        self.assertEqual(rc, 0)
        self.assertIn("set-mute @DEFAULT_AUDIO_SINK@ 0", calls)
        self.assertNotIn("set-volume", calls,
                         "a deliberate non-zero level must NOT be clobbered")

    def test_pactl_fallback_when_no_wpctl(self):
        """No wpctl, pactl default sink at 0% -> unmute + set 60% via pactl."""
        self._add_pactl(default_sink="alsa_output.test",
                        getvol="front-left: 0 / 0% / -inf")
        rc, calls, _ = self._run("60")
        self.assertEqual(rc, 0)
        self.assertIn("set-sink-mute alsa_output.test 0", calls)
        self.assertIn("set-sink-volume alsa_output.test 60%", calls)

    def test_pactl_fallback_nonzero_not_clobbered(self):
        """pactl default sink already at 55% -> unmute only, level untouched."""
        self._add_pactl(default_sink="alsa_output.test",
                        getvol="front-left: 36044 / 55% / -6.00 dB")
        rc, calls, _ = self._run("60")
        self.assertEqual(rc, 0)
        self.assertIn("set-sink-mute alsa_output.test 0", calls)
        self.assertNotIn("set-sink-volume", calls)

    def test_volume_clamped_to_150(self):
        """An out-of-range floor clamps to 1.50 (150%) on the wpctl path."""
        self._add_wpctl(getvol="Volume: 0.00 [MUTED]")
        rc, calls, _ = self._run("999")
        self.assertEqual(rc, 0)
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 1.50", calls)

    def test_nonnumeric_volume_defaults_to_100(self):
        """A non-numeric floor arg falls back to the 100% default (steward: a
        fresh OS should be audible - the default is now 100%, not 60%)."""
        self._add_wpctl(getvol="Volume: 0.00 [MUTED]")
        rc, calls, _ = self._run("garbage")
        self.assertEqual(rc, 0)
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 1.00", calls)

    # ── first-boot: set the FULL floor once, unconditionally ─────────────────
    def test_first_boot_sets_full_default_even_over_nonzero(self):
        """FIRST boot (no stamp): the default sink is set to the FULL floor
        UNCONDITIONALLY - even over a non-zero level - so a brand-new install is
        audible out of the box. This is the ONLY time a non-zero level is set."""
        self._add_wpctl(getvol="Volume: 0.40")   # a fresh sink at 40%
        rc, calls, _ = self._run("100", seed_stamp=False)   # no stamp = first boot
        self.assertEqual(rc, 0)
        self.assertIn("set-mute @DEFAULT_AUDIO_SINK@ 0", calls)
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 1.00", calls,
                      "first boot must set the full 100% floor even over 0.40")

    def test_first_boot_then_subsequent_never_clobbers(self):
        """Idempotence + never-clobber-after-first-boot: the FIRST boot writes the
        stamp and sets 100%; a LATER boot (stamp now present) leaves a deliberate
        level untouched. Both runs share ONE XDG_STATE_HOME (self.state)."""
        self._add_wpctl(getvol="Volume: 0.40")
        rc1, calls1, _ = self._run("100", seed_stamp=False)   # first boot -> 1.00 + writes stamp
        self.assertEqual(rc1, 0)
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 1.00", calls1)
        # Fresh call log; DO NOT re-seed - rely on the stamp the first run wrote.
        self._reset_calls()
        rc2, calls2, _ = self._run("100", seed_stamp=False)
        self.assertEqual(rc2, 0)
        self.assertIn("set-mute @DEFAULT_AUDIO_SINK@ 0", calls2, "still unmutes")
        self.assertNotIn("set-volume", calls2,
                         "after first boot a deliberate 0.40 must NOT be clobbered")

    # ── hotplug-safe default-sink reselection ────────────────────────────────
    def test_hotplug_reselects_default_sink_then_rescues(self):
        """No default sink assigned but one EXISTS (a hotplug edge) -> the script
        PROMOTES it to default (reselection), then rescues it. Proves the
        default-sink reselection / hotplug-safety hardening."""
        self._add_pactl_hotplug(sink_name="alsa_output.hotplug",
                                 getvol="front-left: 0 / 0% / -inf")
        rc, calls, _ = self._run("100")   # subsequent boot; the 0% sink is rescued
        self.assertEqual(rc, 0)
        self.assertIn("set-default-sink alsa_output.hotplug", calls,
                      "must promote the available sink to default (reselection)")
        self.assertIn("set-sink-mute alsa_output.hotplug 0", calls,
                      "must unmute the reselected sink")
        self.assertIn("set-sink-volume alsa_output.hotplug 100%", calls,
                      "must rescue the silent reselected sink to the floor")


if __name__ == "__main__":
    unittest.main()
