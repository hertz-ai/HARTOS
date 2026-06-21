"""Pure-logic guards for scripts/hart_usb_flasher.py.

The flasher's *write* path needs real hardware (it was validated by actually
flashing a USB), but its byte-math and its destructive-write SAFETY decisions
are pure functions and MUST be correct — a wrong offset corrupts the image, a
missing warning risks the wrong disk. These tests call the real functions with
mocked boundaries (no device, no PowerShell) and assert behaviour.
"""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "hart_usb_flasher.py")
_spec = importlib.util.spec_from_file_location("hart_usb_flasher", _PATH)
flasher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flasher)


def test_offsets_are_cumulative_byte_positions():
    parts = [{"size": 1992294400}, {"size": 1992294400},
             {"size": 1992294400}, {"size": 1053118464}]
    offs, total = flasher.offsets(parts)
    assert offs == [0, 1992294400, 3984588800, 5976883200]
    assert total == 7030001664           # the exact desktop ISO size


def test_human_readable_sizes():
    assert flasher.human(0) == "0.0 B"
    assert flasher.human(1536).endswith("KB")
    assert flasher.human(7030001664).endswith("GB")


def test_usb_filter_excludes_system_and_nonremovable():
    disks = [
        {"number": 0, "removable": False, "system": True},   # internal/system
        {"number": 1, "removable": True, "system": False},   # the USB
        {"number": 2, "removable": False, "system": False},  # internal data disk
    ]
    keep = flasher.usb_disks(disks)
    assert [d["number"] for d in keep] == [1]                # only the USB offered


def test_warning_flags_system_disk(monkeypatch):
    monkeypatch.setattr(flasher, "disk_contents_summary", lambda d: "")
    w = flasher.disk_warning({"system": True, "removable": False})
    assert "SYSTEM" in w.upper()


def test_warning_flags_existing_data(monkeypatch):
    monkeypatch.setattr(flasher, "disk_contents_summary",
                        lambda d: "1 partition(s) | E: 'Backup' [NTFS]")
    w = flasher.disk_warning({"system": False, "removable": True})
    assert "contains data" in w and "Backup" in w


def test_no_warning_for_blank_removable_usb(monkeypatch):
    monkeypatch.setattr(flasher, "disk_contents_summary", lambda d: "0 partition(s)")
    assert flasher.disk_warning({"system": False, "removable": True}) == ""


def test_default_mode_is_stream():
    """Stream is ~40%% faster (overlaps download+write); it must be the default."""
    args = flasher.build_parser().parse_args(["--device", "1", "--yes"])
    assert args.mode == "stream"


def test_tee_logger_writes_file_and_console(tmp_path):
    seen = []
    path = str(tmp_path / "flash.log")
    log = flasher._tee_logger(path, seen.append)
    log("hello")
    assert seen == ["hello"]                                 # console got it
    assert "hello" in open(path, encoding="utf-8").read()    # file got it too


# ─────────── Windows USB-wedge hardening: diskpart fallback + pnputil self-heal ───────────
# Real captured output from the 2026-06-20 SanDisk-Cruzer-Blade incident (Disk 1
# = the 28 GB USB stick, Disk 0 = the 953 GB NVMe system disk). Get-Disk hung
# 15+ min; diskpart saw both natively. The flasher must (a) fall back to diskpart
# when Get-Disk hangs/empties and parse the USB stick as removable + the NVMe as
# system, and (b) run the pnputil controller-restart self-heal when no removable
# disk is found, then re-enumerate.

_LIST_DISK = """Microsoft DiskPart version 10.0.26100.1150

  Disk ###  Status         Size     Free     Dyn  Gpt
  --------  -------------  -------  -------  ---  ---
  Disk 0    Online          953 GB  2048 KB        *
  Disk 1    Online           28 GB    28 GB
"""

_DETAIL_DISK0 = """Disk 0 is now the selected disk.

SAMSUNG MZVL21T0HCLR-00B00
Disk ID: {9F6F73A4-8292-4512-AC0C-5D847293D6AB}
Type   : NVMe
Status : Online
Boot Disk  : Yes
Pagefile Disk  : Yes
"""

_DETAIL_DISK1 = """Disk 1 is now the selected disk.

SanDisk Cruzer Blade USB Device
Disk ID: 9FB6382F
Type   : USB
Status : Online
Boot Disk  : No
Pagefile Disk  : No
"""

_PNPUTIL_ENUM = """Microsoft PnP Utility

Instance ID:                USB\\ROOT_HUB30\\4&333850d8&0&0
Device Description:         USB Root Hub (USB 3.0)
Class Name:                 USB

Instance ID:                PCI\\VEN_8086&DEV_9A17&SUBSYS_00000000&REV_05\\3&11583659&1&68
Device Description:         Intel(R) USB 3.10 eXtensible Host Controller - 1.20 (Microsoft)
Class Name:                 USB

Instance ID:                USB\\VID_0781&PID_5567\\03022103070524030432
Device Description:         USB Mass Storage Device
Class Name:                 USB

Instance ID:                PCI\\VEN_8086&DEV_43ED&SUBSYS_12FB1462&REV_11\\3&11583659&1&A0
Device Description:         Intel(R) USB 3.20 eXtensible Host Controller - 1.20 (Microsoft)
Class Name:                 USB
"""


class _CP:
    """Minimal CompletedProcess stand-in."""
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _fake_diskpart_runner():
    """Return a subprocess.run replacement that answers `list disk` /
    `detail disk` from a temp-file script the way the real diskpart does."""
    def fake_run(cmd, **kw):
        if cmd[:1] == ["diskpart"]:
            script = open(cmd[2], encoding="utf-8").read()          # diskpart /s <file>
            if "list disk" in script:
                return _CP(_LIST_DISK)
            if "select disk 0" in script:
                return _CP(_DETAIL_DISK0)
            if "select disk 1" in script:
                return _CP(_DETAIL_DISK1)
            return _CP("")
        raise AssertionError("unexpected subprocess: %r" % (cmd,))
    return fake_run


def test_diskpart_fallback_parses_usb_and_system(monkeypatch):
    """The native diskpart enumeration must mark Disk 1 (SanDisk USB) removable
    + NOT system, and Disk 0 (NVMe boot disk) system + NOT removable — so only
    the USB stick is ever offered as a write target."""
    monkeypatch.setattr(flasher.subprocess, "run", _fake_diskpart_runner())
    disks = flasher._list_disks_windows_diskpart()
    by_num = {d["number"]: d for d in disks}

    usb = by_num[1]
    assert usb["removable"] is True and usb["system"] is False
    assert usb["bus"] == "USB"
    assert "SanDisk" in usb["model"]
    assert usb["size"] == 28 * 1024**3
    assert usb["physdrive"] == r"\\.\PhysicalDrive1"

    nvme = by_num[0]
    assert nvme["removable"] is False and nvme["system"] is True
    assert nvme["size"] == 953 * 1024**3

    # Safety filter: only the USB stick survives the offered-pool filter.
    assert [d["number"] for d in flasher.usb_disks(disks)] == [1]


def test_get_disk_timeout_falls_back_to_diskpart(monkeypatch):
    """When the Get-Disk PowerShell call TIMES OUT (the wedged-WMI symptom),
    `_list_disks_windows` must transparently fall back to the diskpart path."""
    diskpart = _fake_diskpart_runner()

    def fake_run(cmd, **kw):
        if cmd[:1] == ["powershell"]:
            raise flasher.subprocess.TimeoutExpired(cmd, kw.get("timeout", 12))
        return diskpart(cmd, **kw)

    monkeypatch.setattr(flasher.subprocess, "run", fake_run)
    disks = flasher._list_disks_windows()
    assert [d["number"] for d in flasher.usb_disks(disks)] == [1]    # diskpart path won


def test_host_controllers_are_pci_only(monkeypatch):
    """Only the PCI xHCI host controllers are restarted — never the hubs /
    mass-storage devices hanging off them (restarting a hub hangs a wedged stack)."""
    monkeypatch.setattr(flasher.subprocess, "run",
                        lambda cmd, **kw: _CP(_PNPUTIL_ENUM))
    ctrls = flasher._windows_usb_host_controllers()
    assert len(ctrls) == 2
    assert all(c.startswith("PCI\\") for c in ctrls)
    assert not any("ROOT_HUB" in c or "VID_" in c for c in ctrls)


def test_self_heal_restarts_each_controller(monkeypatch):
    """The self-heal must call `pnputil /restart-device` once per PCI controller."""
    restarted = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["pnputil", "/enum-devices"]:
            return _CP(_PNPUTIL_ENUM)
        if cmd[:2] == ["pnputil", "/restart-device"]:
            restarted.append(cmd[2])
            return _CP("", returncode=0)
        raise AssertionError("unexpected: %r" % (cmd,))

    monkeypatch.setattr(flasher.subprocess, "run", fake_run)
    monkeypatch.setattr(flasher.time, "sleep", lambda *_: None)      # no real wait
    healed = flasher._windows_usb_self_heal(log=lambda m: None)
    assert healed is True
    assert len(restarted) == 2 and all(c.startswith("PCI\\") for c in restarted)


def test_empty_enum_triggers_self_heal_then_reenumerates(monkeypatch):
    """End-to-end: if the FIRST enumeration finds no removable disk, the wedge
    self-heal runs ONCE, and the SECOND enumeration (post-reset) finds the USB
    stick. Proves a "plugged in but invisible" user auto-recovers without reboot."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    calls = {"enum": 0, "heal": 0}

    def fake_list_disks():
        calls["enum"] += 1
        if calls["enum"] == 1:
            return []                                   # wedged: nothing visible
        return [{"number": 1, "removable": True, "system": False,
                 "model": "SanDisk", "size": 28 * 1024**3}]

    def fake_heal(log=None):
        calls["heal"] += 1
        return True

    monkeypatch.setattr(flasher, "list_disks", fake_list_disks)
    monkeypatch.setattr(flasher, "_windows_usb_self_heal", fake_heal)

    disks, candidates = flasher.list_disks_with_self_heal(log=lambda m: None)
    assert calls["heal"] == 1                            # self-heal ran exactly once
    assert calls["enum"] == 2                            # re-enumerated after the reset
    assert [d["number"] for d in candidates] == [1]      # USB stick now visible


def test_self_heal_not_invoked_when_disk_already_present(monkeypatch):
    """The self-heal must NOT run (no needless USB-bus reset) when a removable
    disk is found on the first try."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    monkeypatch.setattr(flasher, "list_disks",
                        lambda: [{"number": 1, "removable": True, "system": False,
                                  "model": "SanDisk", "size": 1}])
    healed = {"n": 0}
    monkeypatch.setattr(flasher, "_windows_usb_self_heal",
                        lambda log=None: healed.__setitem__("n", healed["n"] + 1) or True)
    _, candidates = flasher.list_disks_with_self_heal(log=lambda m: None)
    assert healed["n"] == 0 and [d["number"] for d in candidates] == [1]


def test_list_parts_missing_tag_returns_empty_not_keyerror(monkeypatch):
    """A missing/unpublished tag makes `gh api` 404 and emit an error object on
    stdout. `list_parts` must return [] (=> a clean "no parts" error downstream)
    rather than crash with KeyError: 'name' on the error JSON."""
    err_obj = '{"message":"Not Found","documentation_url":"...","status":"404"}'
    monkeypatch.setattr(flasher, "_run", lambda cmd, **kw: _CP(err_obj))
    assert flasher.list_parts("gh", "nightly-does-not-exist", "desktop") == []


def test_list_parts_parses_real_assets(monkeypatch):
    """Well-formed asset lines for the desktop variant are kept + sorted by name;
    non-part assets (sha256/torrent) and other variants are dropped."""
    lines = "\n".join([
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.iso.part-01","id":2,"size":20,"state":"uploaded"}',
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.iso.part-00","id":1,"size":10,"state":"uploaded"}',
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.iso.sha256","id":9,"size":39,"state":"uploaded"}',
        '{"name":"hart-os-1.0.0-server-x86_64-linux.iso.part-00","id":7,"size":99,"state":"uploaded"}',
    ])
    monkeypatch.setattr(flasher, "_run", lambda cmd, **kw: _CP(lines))
    parts = flasher.list_parts("gh", "nightly-x", "desktop")
    assert [p["name"].split(".part-")[1] for p in parts] == ["00", "01"]   # sorted, parts only
    assert all("desktop" in p["name"] for p in parts)


# ─────────── HARTLOG diagnostic-log partition (Part B) ───────────
# The Windows diskpart carve of the HARTLOG partition is now OPT-IN (DEFAULT OFF):
# the Live OS creates it itself on first boot (Linux-side, safe). The Windows
# path hung on a wedged VDS AND corrupted a freshly-flashed stick's EFI/GPT, so it
# is gated behind --windows-log-partition. When explicitly enabled, the carve must:
#   (a) run the right diskpart script (create primary + format fat32 label),
#   (b) report success ONLY when diskpart confirms the format,
#   (c) NEVER fail/abort the (already-successful) flash on ANY error,
#   (d) be OFF unless --windows-log-partition is passed,
#   (e) run AFTER a successful verify (not before, not on a failed flash).

# Real diskpart output for a successful `format fs=fat32 label=HARTLOG quick`.
_DISKPART_FORMAT_OK = """Microsoft DiskPart version 10.0.26100.1150

DiskPart succeeded in creating the specified partition.

  100 percent completed

DiskPart successfully formatted the volume.

DiskPart successfully assigned the drive letter or mount point.
"""

# diskpart when the ISO consumed the whole stick — no usable free extent.
_DISKPART_NO_FREE = """Microsoft DiskPart version 10.0.26100.1150

There is not enough usable free space on specified disk(s).
"""


def _fake_log_partition_diskpart(captured, output=_DISKPART_FORMAT_OK, returncode=0):
    """A subprocess.run replacement that captures the diskpart script the
    HARTLOG carve writes + returns a canned diskpart result."""
    def fake_run(cmd, **kw):
        if cmd[:1] == ["diskpart"]:
            captured["script"] = open(cmd[2], encoding="utf-8").read()
            return _CP(output, returncode=returncode)
        raise AssertionError("unexpected subprocess: %r" % (cmd,))
    return fake_run


def test_create_log_partition_runs_correct_diskpart_script(monkeypatch):
    """The carve must select the target disk, create a primary partition, and
    FAT32-format it with the HARTLOG label — the contract the boot-log module
    detects. On a confirmed format it returns True."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    captured = {}
    monkeypatch.setattr(flasher.subprocess, "run", _fake_log_partition_diskpart(captured))
    logs = []
    ok = flasher.create_log_partition({"number": 1}, logs.append)
    assert ok is True
    s = captured["script"]
    assert "select disk 1" in s
    assert "create partition primary" in s
    assert "format fs=fat32 label=HARTLOG quick" in s
    # The label the carve writes MUST match the module's contract constant.
    assert flasher.LOG_PART_LABEL == "HARTLOG"
    assert any("HARTLOG partition: created" in m for m in logs)


def test_create_log_partition_no_free_space_is_clean_skip(monkeypatch):
    """If the ISO filled the stick (no usable free extent), the carve is a clean
    skip — returns False, logs the reason, does NOT raise."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    captured = {}
    monkeypatch.setattr(flasher.subprocess, "run",
                        _fake_log_partition_diskpart(captured, output=_DISKPART_NO_FREE))
    logs = []
    ok = flasher.create_log_partition({"number": 1}, logs.append)
    assert ok is False
    assert any("no free space" in m for m in logs)


def test_create_log_partition_diskpart_timeout_never_raises(monkeypatch):
    """A diskpart timeout/OSError must be swallowed — the flash is already done;
    the log partition is a debug convenience that can never fail it."""
    monkeypatch.setattr(flasher, "IS_WIN", True)

    def boom(cmd, **kw):
        raise flasher.subprocess.TimeoutExpired(cmd, kw.get("timeout", 120))

    monkeypatch.setattr(flasher.subprocess, "run", boom)
    logs = []
    ok = flasher.create_log_partition({"number": 1}, logs.append)   # must NOT raise
    assert ok is False
    assert any("diskpart unavailable" in m or "timed out" in m for m in logs)


def test_create_log_partition_non_windows_is_noop(monkeypatch):
    """On Linux/macOS the carve is a logged no-op (diskpart is Windows-only)."""
    monkeypatch.setattr(flasher, "IS_WIN", False)
    called = {"n": 0}
    monkeypatch.setattr(flasher.subprocess, "run",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    logs = []
    ok = flasher.create_log_partition({"number": 1}, logs.append)
    assert ok is False
    assert called["n"] == 0                       # never shells diskpart off-Windows
    assert any("only created on Windows" in m for m in logs)


def _stub_flash_machinery(monkeypatch, verify_result=True):
    """Stub every heavy step of flash() so a test can exercise ONLY the
    post-verify HARTLOG step + its ordering. Returns the call-order list."""
    order = []
    monkeypatch.setattr(flasher, "IS_WIN", False)            # skip the Windows writer path
    monkeypatch.setattr(flasher, "find_gh", lambda: "gh")
    monkeypatch.setattr(flasher, "find_dd", lambda: None)
    monkeypatch.setattr(flasher, "list_parts",
                        lambda gh, tag, variant: [{"name": "p0", "id": 1, "size": 10,
                                                   "state": "uploaded"}])
    monkeypatch.setattr(flasher, "download_part", lambda *a, **k: "/tmp/p0")
    monkeypatch.setattr(flasher, "stream_to_device", lambda *a, **k: 10)
    monkeypatch.setattr(flasher, "write_source_to_device", lambda *a, **k: 10)
    monkeypatch.setattr(flasher, "stream_producer", lambda *a, **k: ["curl"])
    monkeypatch.setattr(flasher.os, "remove", lambda *a, **k: None)
    monkeypatch.setattr(flasher, "_run", lambda *a, **k: _CP(""))

    def fake_verify(disk, dd, log):
        order.append("verify")
        return verify_result
    monkeypatch.setattr(flasher, "verify_iso", fake_verify)

    def fake_carve(disk, log):
        order.append("carve")
        return True
    monkeypatch.setattr(flasher, "create_log_partition", fake_carve)
    return order


def test_flash_creates_log_partition_after_successful_verify(monkeypatch):
    """End-to-end ordering: when the legacy Windows carve is EXPLICITLY enabled
    (make_log_partition=True / --windows-log-partition), flash() must call
    create_log_partition AFTER a successful verify_iso (a debug partition on a
    verified-bootable stick)."""
    order = _stub_flash_machinery(monkeypatch, verify_result=True)
    ok = flasher.flash("tag", "desktop", {"number": 1, "model": "USB", "dev": "/dev/sdb",
                                          "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=lambda m: None,
                       make_log_partition=True)
    assert ok is True
    assert order == ["verify", "carve"], \
        "create_log_partition must run AFTER verify_iso, only on success"


def test_flash_default_skips_windows_carve(monkeypatch):
    """THE inversion: the Windows diskpart carve is now OFF BY DEFAULT (the Live
    OS creates HARTLOG itself, which can't corrupt the stick's EFI/GPT). A plain
    flash() with no make_log_partition arg must NOT carve."""
    order = _stub_flash_machinery(monkeypatch, verify_result=True)
    ok = flasher.flash("tag", "desktop", {"number": 1, "model": "USB", "dev": "/dev/sdb",
                                          "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=lambda m: None)
    assert ok is True
    assert order == ["verify"], \
        "the Windows carve must be OFF by default — the Live OS owns HARTLOG now"


def test_flash_skips_log_partition_when_verify_fails(monkeypatch):
    """A FAILED verify must NOT get a log partition — no point debug-partitioning
    a stick that didn't flash."""
    order = _stub_flash_machinery(monkeypatch, verify_result=False)
    ok = flasher.flash("tag", "desktop", {"number": 1, "model": "USB", "dev": "/dev/sdb",
                                          "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=lambda m: None)
    assert ok is False
    assert order == ["verify"], "no carve on a failed verify"


def test_flash_explicit_disable_skips_the_carve(monkeypatch):
    """An explicit make_log_partition=False skips the carve even on a successful
    verify (same as the default now)."""
    order = _stub_flash_machinery(monkeypatch, verify_result=True)
    ok = flasher.flash("tag", "desktop", {"number": 1, "model": "USB", "dev": "/dev/sdb",
                                          "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=lambda m: None,
                       make_log_partition=False)
    assert ok is True
    assert order == ["verify"], "make_log_partition=False must skip the carve"


def test_flash_carve_exception_does_not_fail_the_flash(monkeypatch):
    """If create_log_partition RAISES (it shouldn't, but belt-and-suspenders),
    the flash result is still the verify result — the carve can never turn a
    successful flash into a failure."""
    order = _stub_flash_machinery(monkeypatch, verify_result=True)

    def raising_carve(disk, log):
        order.append("carve")
        raise RuntimeError("diskpart exploded")
    monkeypatch.setattr(flasher, "create_log_partition", raising_carve)

    # create_log_partition is documented never to raise, but flash() must not
    # let a hypothetical raise corrupt the already-successful result. Guard it
    # the same way the function guards itself: the flash already returned ok=True
    # by the time the carve runs, so wrap the call.
    try:
        ok = flasher.flash("tag", "desktop",
                           {"number": 1, "model": "USB", "dev": "/dev/sdb",
                            "physdrive": "/dev/sdb"},
                           "download", "/tmp", log=lambda m: None,
                           make_log_partition=True)
    except RuntimeError:
        pytest.fail("a carve exception must not propagate out of flash()")
    assert ok is True
    assert order == ["verify", "carve"]


def test_windows_log_partition_flag_parses(monkeypatch):
    """The carve is OFF by default; --windows-log-partition is the explicit opt-in
    to the legacy Windows diskpart path (the Live OS owns HARTLOG creation now)."""
    args = flasher.build_parser().parse_args(["--device", "1", "--yes"])
    assert args.windows_log_partition is False           # default: Live OS owns it
    args2 = flasher.build_parser().parse_args(
        ["--device", "1", "--yes", "--windows-log-partition"])
    assert args2.windows_log_partition is True           # explicit legacy opt-in
