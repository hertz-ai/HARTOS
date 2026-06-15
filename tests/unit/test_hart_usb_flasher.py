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
