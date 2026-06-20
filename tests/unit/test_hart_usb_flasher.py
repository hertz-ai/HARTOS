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
