"""HART OS — network-wifi hardware dimension: the wifi probe DEGRADES, never dies.

PRINCIPLE (degrade-not-die): every hardware-dependent path must degrade
gracefully on missing/faulting hardware — never brick, black, or hang. The wifi
probe is the connectivity cluster's hardware-facing path. The real-HW failures it
must survive WITHOUT crashing/hanging, and report HONESTLY:

  1. wifi CHIP not enumerated (missing firmware/driver) -> must read
     available=False so the UI says "hardware not detected" (NOT a false "on").
  2. rfkill SOFT-BLOCK -> available=True, blocked='soft' (DISTINCT from no-hw).
     rfkill HARD-BLOCK -> available=True, blocked='hard' (physical/BIOS switch).
  3. NetworkManager not up (nmcli rc!=0 / binary missing) -> never crashes; a box
     with a wifi chip still reports available=True (NM-down != "no hardware").
  4. the probe never hangs/raises: rfkill is a pure sysfs read; every nmcli call
     is timeout-bounded and FileNotFoundError-guarded by the cache's _run.

These are BEHAVIOURAL tests: they drive the REAL _ConnectivityCache._probe_wifi /
_probe_rfkill_wifi with a fake sysfs tree on disk and a mocked nmcli, then assert
the parsed verdict + that no exception escapes. (No grep; no source-shape checks.)

The rfkill subsystem + a real wifi radio cannot be created on the Windows dev box
or in a plain CI container, so the SYSFS LAYOUT is faked in a tmp dir (the parser
takes the root dir as a parameter precisely so this is testable off real HW); the
on-a-real-radio read still needs real HW, which the boot-log lspci/rfkill summary
(hart-boot-log.nix) captures.

Usage:
    pytest tests/unit/test_wifi_probe_degrade.py -v
"""

import os
from types import SimpleNamespace

import pytest

from integrations.agent_engine import liquid_ui_service as lus


def _cp(returncode, stdout=''):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _mk_rfkill(tmp_path, entries):
    """Build a fake /sys/class/rfkill tree. `entries` is a list of
    (name, type, soft, hard) tuples. Returns the root dir path."""
    root = tmp_path / 'rfkill'
    root.mkdir()
    for name, typ, soft, hard in entries:
        d = root / name
        d.mkdir()
        (d / 'type').write_text(typ + '\n')
        (d / 'soft').write_text(str(soft) + '\n')
        (d / 'hard').write_text(str(hard) + '\n')
    return str(root)


# ═══════════════════════════════════════════════════════════════
# Section 1: _probe_rfkill_wifi parser — soft vs hard vs none vs absent vs unknown
# ═══════════════════════════════════════════════════════════════

class TestRfkillParser:

    def setup_method(self):
        self.cache = lus._ConnectivityCache()

    def test_unblocked_wlan_entry_is_none(self, tmp_path):
        d = _mk_rfkill(tmp_path, [('rfkill0', 'wlan', 0, 0)])
        assert self.cache._probe_rfkill_wifi(d) == 'none'

    def test_soft_blocked_wlan_entry_is_soft(self, tmp_path):
        d = _mk_rfkill(tmp_path, [('rfkill0', 'wlan', 1, 0)])
        assert self.cache._probe_rfkill_wifi(d) == 'soft'

    def test_hard_blocked_wlan_entry_is_hard(self, tmp_path):
        d = _mk_rfkill(tmp_path, [('rfkill0', 'wlan', 0, 1)])
        assert self.cache._probe_rfkill_wifi(d) == 'hard'

    def test_hard_wins_over_soft(self, tmp_path):
        # Both flags set (airplane mode + physical switch): the un-recoverable
        # hard block must be reported so the UI doesn't promise a software toggle.
        d = _mk_rfkill(tmp_path, [('rfkill0', 'wlan', 1, 1)])
        assert self.cache._probe_rfkill_wifi(d) == 'hard'

    def test_only_non_wlan_radios_is_absent(self, tmp_path):
        # A box with bluetooth/wwan rfkill but NO wlan entry == no wifi chip.
        d = _mk_rfkill(tmp_path, [('rfkill0', 'bluetooth', 0, 0),
                                  ('rfkill1', 'wwan', 0, 0)])
        assert self.cache._probe_rfkill_wifi(d) == 'absent'

    def test_empty_rfkill_dir_is_absent(self, tmp_path):
        d = str(tmp_path / 'rfkill')
        os.mkdir(d)
        assert self.cache._probe_rfkill_wifi(d) == 'absent'

    def test_missing_rfkill_subsystem_is_unknown(self, tmp_path):
        # No /sys/class/rfkill at all (a container / the Windows dev box).
        assert self.cache._probe_rfkill_wifi(str(tmp_path / 'nope')) == 'unknown'

    def test_picks_wlan_among_mixed_radios(self, tmp_path):
        d = _mk_rfkill(tmp_path, [('rfkill0', 'bluetooth', 0, 0),
                                  ('rfkill1', 'wlan', 1, 0)])
        assert self.cache._probe_rfkill_wifi(d) == 'soft'

    def test_unreadable_flag_files_degrade_not_crash(self, tmp_path):
        # type=wlan present but soft/hard files removed: must not raise; the entry
        # still proves presence, so 'none' (unblocked) is the safe read.
        root = tmp_path / 'rfkill'
        root.mkdir()
        d0 = root / 'rfkill0'
        d0.mkdir()
        (d0 / 'type').write_text('wlan\n')
        # no soft/hard files written
        assert self.cache._probe_rfkill_wifi(str(root)) == 'none'


# ═══════════════════════════════════════════════════════════════
# Section 2: _probe_wifi integration — the degrade matrix (never crash/hang)
# ═══════════════════════════════════════════════════════════════

class TestProbeWifiDegradeMatrix:

    def setup_method(self):
        self.cache = lus._ConnectivityCache()

    def _set_rf(self, monkeypatch, verdict):
        monkeypatch.setattr(self.cache, '_probe_rfkill_wifi',
                            lambda *a, **k: verdict)

    def _set_nmcli(self, monkeypatch, fn):
        monkeypatch.setattr(self.cache, '_run', fn)

    # ── Failure mode 1: chip not enumerated (missing firmware/driver) ──
    def test_no_chip_reports_unavailable_not_false_on(self, monkeypatch):
        """rfkill 'absent' (no wlan entry) => available=False even though NM's
        `radio wifi` answers 'enabled' (the software toggle is present with zero
        devices). This is the honest 'hardware not detected', not a fake 'on'."""
        self._set_rf(monkeypatch, 'absent')

        def nm(cmd, *a, **k):
            if cmd[:3] == ['nmcli', 'radio', 'wifi']:
                return _cp(0, 'enabled\n')       # software switch says on...
            if cmd[:2] == ['nmcli', '-t']:
                return _cp(1, '')                # ...but "No Wi-Fi device found"
            return _cp(1, '')
        self._set_nmcli(monkeypatch, nm)

        w = self.cache._probe_wifi()
        assert w['available'] is False, "no wifi chip must NOT report available"
        assert w['blocked'] is None

    # ── Failure mode 2: rfkill soft-block (mustn't read as "no hardware") ──
    def test_soft_block_is_available_and_distinguished(self, monkeypatch):
        self._set_rf(monkeypatch, 'soft')

        def nm(cmd, *a, **k):
            if cmd[:3] == ['nmcli', 'radio', 'wifi']:
                return _cp(0, 'disabled\n')      # soft-block reads disabled
            return _cp(0, '')                    # device wifi: present, no APs
        self._set_nmcli(monkeypatch, nm)

        w = self.cache._probe_wifi()
        assert w['available'] is True, "a soft-blocked chip IS present hardware"
        assert w['blocked'] == 'soft'
        assert w['enabled'] is False

    def test_hard_block_is_available_and_distinguished(self, monkeypatch):
        self._set_rf(monkeypatch, 'hard')
        self._set_nmcli(monkeypatch, lambda *a, **k: _cp(0, 'disabled\n'))
        w = self.cache._probe_wifi()
        assert w['available'] is True
        assert w['blocked'] == 'hard'

    # ── Failure mode 3: NetworkManager not up / nmcli missing ──
    def test_nm_down_with_chip_present_still_available(self, monkeypatch):
        """A wifi chip exists (rfkill 'none') but nmcli fails every call
        (NM not up, or the binary is absent -> _run returns None). The probe must
        NOT crash and must still report available=True (NM-down is not 'no hw')."""
        self._set_rf(monkeypatch, 'none')
        self._set_nmcli(monkeypatch, lambda *a, **k: None)   # mirrors _run on FNF
        w = self.cache._probe_wifi()
        assert w['available'] is True
        assert w['connected'] is False
        assert w['blocked'] is None

    def test_nmcli_missing_everywhere_does_not_raise(self, monkeypatch):
        # rfkill unknown (no subsystem) AND nmcli absent: the worst case must
        # still return a well-formed, everything-false dict — never an exception.
        self._set_rf(monkeypatch, 'unknown')
        self._set_nmcli(monkeypatch, lambda *a, **k: None)
        w = self.cache._probe_wifi()
        assert w['available'] is False
        assert set(w) == {'available', 'enabled', 'connected',
                          'ssid', 'signal', 'blocked'}

    # ── Failure mode 4: the probe must never hang/raise on a slow/erroring tool ──
    def test_run_timeout_is_swallowed_no_hang(self, monkeypatch):
        # The real cache._run converts TimeoutExpired into None (no raise). Prove
        # _probe_wifi tolerates that everywhere and yields a valid snapshot.
        self._set_rf(monkeypatch, 'none')

        def timeout_run(cmd, *a, **k):
            return None   # cache._run already maps TimeoutExpired/FNF -> None
        self._set_nmcli(monkeypatch, timeout_run)
        w = self.cache._probe_wifi()      # must return, not hang/raise
        assert w['available'] is True     # rfkill alone established presence

    def test_unknown_rfkill_falls_back_to_nm_radio(self, monkeypatch):
        """No rfkill subsystem (container/VM) but NM answers: fall back to the
        radio switch for presence so those environments don't regress to 'no hw'."""
        self._set_rf(monkeypatch, 'unknown')

        def nm(cmd, *a, **k):
            if cmd[:3] == ['nmcli', 'radio', 'wifi']:
                return _cp(0, 'enabled\n')
            if cmd[:2] == ['nmcli', '-t']:
                return _cp(0, 'yes:HomeNet:80\n')
            return _cp(1, '')
        self._set_nmcli(monkeypatch, nm)

        w = self.cache._probe_wifi()
        assert w['available'] is True
        assert w['enabled'] is True
        assert w['connected'] is True
        assert w['ssid'] == 'HomeNet'
        assert w['signal'] == 80


# ═══════════════════════════════════════════════════════════════
# Section 3: the real cache._run boundary — timeout/FNF never raise (anti-hang)
# ═══════════════════════════════════════════════════════════════

class TestRunNeverHangsOrRaises:

    def test_timeout_returns_none(self, monkeypatch):
        cache = lus._ConnectivityCache()

        def slow(cmd, *a, **k):
            raise lus.subprocess.TimeoutExpired(cmd, cache.PROBE_TIMEOUT_S)
        monkeypatch.setattr(lus.subprocess, 'run', slow)
        assert cache._run(['nmcli', 'radio', 'wifi']) is None

    def test_missing_binary_is_recorded_absent(self, monkeypatch):
        cache = lus._ConnectivityCache()

        def fnf(cmd, *a, **k):
            raise FileNotFoundError(cmd[0])
        monkeypatch.setattr(lus.subprocess, 'run', fnf)
        assert cache._run(['nmcli', 'radio', 'wifi']) is None
        assert 'nmcli' in cache._absent


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
