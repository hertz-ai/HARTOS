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


_DETAIL_USB_BOOT_DISK = """Disk 2 is now the selected disk.

SanDisk Extreme USB Device
Disk ID: A1B2C3D4
Type   : USB
Status : Online
Boot Disk  : Yes
Pagefile Disk  : Yes
"""


def test_usb_stick_that_is_the_live_boot_disk_stays_system_and_excluded(monkeypatch):
    """A USB-bus disk that is ITSELF the live boot/pagefile medium (the machine
    was booted FROM the stick) MUST classify system=True and be EXCLUDED from the
    default write offer. Regression guard: the old code force-cleared `system`
    for any USB-bus disk, discarding the `Boot Disk: Yes` signal — so the
    diskpart fallback would have offered the live boot disk as a writable target
    (the exact wrong-disk catastrophe the safety layer prevents). This must match
    the Get-Disk path, which honours IsSystem/IsBoot regardless of bus."""
    bus, model, system = flasher._parse_diskpart_detail(_DETAIL_USB_BOOT_DISK)
    assert bus == "USB"
    assert "SanDisk" in model
    # The USB stick is the live boot/pagefile medium -> system True, NOT cleared.
    assert system is True

    disk = {"number": 2, "removable": True, "system": system,
            "model": model, "size": 32 * 1024**3}
    # Safety filter: a system USB disk is NOT in the default offered pool.
    assert flasher.usb_disks([disk]) == []
    # And disk_warning surfaces the boot-disk danger even though it is USB.
    monkeypatch.setattr(flasher, "disk_contents_summary", lambda d: "0 partition(s)")
    assert "SYSTEM" in flasher.disk_warning(disk).upper() or \
           "BOOT DISK" in flasher.disk_warning(disk).upper()


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


def test_list_parts_excludes_raw_image_assets_from_iso_flow(monkeypatch):
    """Releases now ALSO carry the installed raw image (.raw.xz.part-*). The old
    filter (`variant in name and ".part-" in name`) would interleave those into
    the ISO part list -- mixed offsets, corrupted write. The iso flow must keep
    ONLY .iso.part-* assets; the raw flow (image='raw') only .raw.xz.part-*."""
    lines = "\n".join([
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.iso.part-00","id":1,"size":10,"state":"uploaded"}',
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.iso.part-01","id":2,"size":20,"state":"uploaded"}',
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.raw.xz.part-00","id":3,"size":30,"state":"uploaded"}',
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.raw.xz.part-01","id":4,"size":40,"state":"uploaded"}',
        '{"name":"hart-os-1.0.0-desktop-x86_64-linux.raw.sha256","id":5,"size":64,"state":"uploaded"}',
    ])
    monkeypatch.setattr(flasher, "_run", lambda cmd, **kw: _CP(lines))
    iso_parts = flasher.list_parts("gh", "nightly-x", "desktop")
    assert [p["id"] for p in iso_parts] == [1, 2], \
        "iso flow must EXCLUDE .raw.xz parts (mixed offsets would corrupt the write)"
    raw_parts = flasher.list_parts("gh", "nightly-x", "desktop", image="raw")
    assert [p["id"] for p in raw_parts] == [3, 4], \
        "raw flow must pick ONLY .raw.xz parts"


# ─────────── raw image (the INSTALLED system) ───────────
# The raw path streams ONE xz (CI's `xz -T0` emits a single multi-block stream;
# split(1) only slices bytes) through decompression straight onto the device.
# These drive the REAL reader/orchestration with real lzma and a fake device.

def _mk_raw_image(size=1024 * 1024):
    """Deterministic pseudo-random image with the raw boot contract baked in:
    protective-MBR 0x55AA at 0x1FE + the GPT 'EFI PART' magic at LBA 1."""
    import hashlib as _h
    blocks, seed = [], b"hart-raw-test-seed"
    while sum(len(b) for b in blocks) < size:
        seed = _h.sha512(seed).digest()
        blocks.append(seed)
    data = bytearray(b"".join(blocks)[:size])
    data[0x1FE:0x200] = b"\x55\xAA"
    data[512:520] = b"EFI PART"
    return bytes(data)


def _mk_xz_parts(tmp_path, data, n_parts=3, base="hart-os-1.0.0-desktop-x86_64-linux.raw"):
    import lzma as _lzma
    comp = _lzma.compress(data, format=_lzma.FORMAT_XZ)
    step = (len(comp) + n_parts - 1) // n_parts
    paths = []
    for i in range(n_parts):
        p = tmp_path / ("%s.xz.part-%02d" % (base, i))
        p.write_bytes(comp[i * step:(i + 1) * step])
        paths.append(str(p))
    return paths


def test_xz_parts_reader_reconstructs_image_exactly(tmp_path):
    """read(n) must be EXACT-FILL: both device writers pad a short buffer to the
    sector boundary, so a short mid-stream read would inject zeros INTO the
    image. Reconstruct through an odd read size and prove byte equality + the
    tracked hash/count."""
    import hashlib as _h
    data = _mk_raw_image()
    paths = _mk_xz_parts(tmp_path, data)
    r = flasher._XZPartsReader(paths, lambda m: None)
    out, reads = b"", []
    while True:
        b = r.read(777_777)                    # odd size -> exact-fill is visible
        out += b
        reads.append(len(b))
        if len(b) < 777_777:
            break
    r.close()
    assert out == data, "decompressed stream must equal the original image"
    assert all(n == 777_777 for n in reads[:-1]), \
        "every read but the last must be EXACT-FILL (short mid-stream read pads zeros into the image)"
    assert r.count == len(data)
    assert r.hexdigest() == _h.sha256(data).hexdigest()


def test_xz_parts_reader_rejects_trailing_garbage(tmp_path):
    """Non-zero bytes after the xz stream mean the part sequence is wrong (a
    foreign file slipped in) -- the reader must corrupt LOUDLY, never hand the
    garbage onward to the device."""
    data = _mk_raw_image(64 * 1024)
    paths = _mk_xz_parts(tmp_path, data, n_parts=2)
    with open(paths[-1], "ab") as f:
        f.write(b"GARBAGE-AFTER-STREAM")
    r = flasher._XZPartsReader(paths, lambda m: None)
    with pytest.raises(RuntimeError, match="trailing data"):
        while r.read(flasher.CHUNK):
            pass


def _wire_fake_device(monkeypatch):
    """Device side ONLY: the write lands in an in-memory buffer, and read_at /
    sha256_device_region answer from that same buffer -- so the REAL
    verify_raw_sigs and the REAL full read-back comparison both run for
    whatever actually got written. Shared by the release-sourced and the
    --from-dir tests so there is exactly one fake device."""
    import hashlib as _h
    dev = {}
    monkeypatch.setattr(flasher, "find_dd", lambda: None)
    monkeypatch.setattr(flasher, "IS_WIN", False)
    monkeypatch.setattr(flasher, "_run", lambda cmd, **kw: _CP(""))

    def fake_py_write(devpath, off, fobj, log):
        buf = bytearray()
        while True:
            b = fobj.read(flasher.CHUNK)
            if not b:
                break
            buf += b
        dev["bytes"] = bytes(buf)
        return len(buf)
    monkeypatch.setattr(flasher, "_py_write", fake_py_write)
    monkeypatch.setattr(flasher, "read_at",
                        lambda d, off, n, dd: dev.get("bytes", b"")[off:off + n])
    monkeypatch.setattr(flasher, "sha256_device_region",
                        lambda d, off, size, dd, chunk=0:
                        _h.sha256(dev.get("bytes", b"")[off:off + size]).hexdigest())
    return dev


def _fake_disk(size):
    return {"number": 9, "model": "FakeStick", "dev": None,
            "physdrive": "/dev/fake9", "size": size}


def _wire_fake_raw_release(monkeypatch, tmp_path, data, sha_line):
    """Wire flash_raw's collaborators to a fake release + fake device: real
    reader, real verify_raw_sigs logic, device = an in-memory buffer."""
    base = "hart-os-1.0.0-desktop-x86_64-linux.raw"
    paths = _mk_xz_parts(tmp_path, data, base=base)
    parts = [{"name": os.path.basename(p), "id": i + 1,
              "size": os.path.getsize(p), "state": "uploaded"}
             for i, p in enumerate(paths)]
    dev = _wire_fake_device(monkeypatch)
    monkeypatch.setattr(flasher, "find_gh", lambda: "gh")
    monkeypatch.setattr(flasher, "list_parts", lambda gh, tag, variant, image="iso": parts)
    monkeypatch.setattr(flasher, "download_part",
                        lambda gh, tag, name, tmp, size, log: str(tmp_path / name))
    monkeypatch.setattr(flasher, "fetch_release_asset_text",
                        lambda gh, tag, name, tmp, log: sha_line)
    return dev


def test_flash_raw_writes_verifies_and_passes(monkeypatch, tmp_path):
    """End to end on a fake device: the image lands byte-identical from offset 0,
    the boot contract (55AA + EFI PART) checks out via the REAL verify_raw_sigs,
    and the stream hash matches the published companion."""
    import hashlib as _h
    data = _mk_raw_image()
    sha = _h.sha256(data).hexdigest()
    dev = _wire_fake_raw_release(monkeypatch, tmp_path, data,
                                 "%s  hart-os-1.0.0-desktop-x86_64-linux.raw\n" % sha)
    disk = {"number": 9, "model": "FakeStick", "dev": None,
            "physdrive": "/dev/fake9", "size": len(data) * 4}
    ok = flasher.flash_raw("nightly-x", "desktop", disk, str(tmp_path),
                           log=lambda m: None)
    assert ok is True
    assert dev["bytes"] == data, "device must hold the exact decompressed image from byte 0"


def test_flash_raw_rejects_companion_sha_mismatch(monkeypatch, tmp_path):
    """A corrupt download must FAIL LOUDLY against the published .raw.sha256 --
    never report a bootable flash from bytes that don't match the source."""
    data = _mk_raw_image(256 * 1024)
    _wire_fake_raw_release(monkeypatch, tmp_path, data,
                           "0" * 64 + "  hart-os-1.0.0-desktop-x86_64-linux.raw\n")
    disk = {"number": 9, "model": "FakeStick", "dev": None,
            "physdrive": "/dev/fake9", "size": len(data) * 4}
    with pytest.raises(RuntimeError, match="does not match the published"):
        flasher.flash_raw("nightly-x", "desktop", disk, str(tmp_path),
                          log=lambda m: None)


# ─────────── --from-dir: flash a RUN ARTIFACT, with no release ───────────
# The nightly pruner regularly deletes the last release carrying raw parts, and
# the CI run artifact (`gh run download <run-id> -n raw-desktop`) becomes the
# only place the newest installed image exists. Before --from-dir, flash_raw
# enumerated parts EXCLUSIVELY from a release and raised "no desktop raw image
# parts in <tag>" before ever looking at --tmp -- so a fully downloaded 7 GB
# artifact could not be flashed by this script at all, and hand-rolled dd (no
# ESP-claim retry, no read-back verify) was the only remaining option.


def test_list_local_parts_enumerates_a_downloaded_artifact(tmp_path):
    """The parts must come back IN WRITE ORDER with their real sizes: the xz
    stream is reassembled by concatenation, so a mis-ordered list decompresses
    to garbage."""
    data = _mk_raw_image(128 * 1024)
    paths = _mk_xz_parts(tmp_path, data, n_parts=4)
    parts = flasher.list_local_parts(str(tmp_path), "desktop", image="raw")
    assert [p["name"] for p in parts] == sorted(os.path.basename(p) for p in paths)
    assert [p["size"] for p in parts] == [os.path.getsize(p) for p in paths], \
        "sizes must be the real on-disk sizes; a wrong size silently truncates"
    assert all(p["state"] == "uploaded" for p in parts), \
        "local bytes are already here, so the still-uploading guard must pass"


def test_list_local_parts_never_mixes_iso_parts_or_other_variants(tmp_path):
    """Same exact-suffix contract list_parts documents: a dir holding both image
    kinds must not interleave them, or two images get written onto one device."""
    (tmp_path / "hart-os-1.0.0-desktop-x86_64-linux.raw.xz.part-00").write_bytes(b"a")
    (tmp_path / "hart-os-1.0.0-desktop-x86_64-linux.iso.part-00").write_bytes(b"b")
    (tmp_path / "hart-os-1.0.0-server-x86_64-linux.raw.xz.part-00").write_bytes(b"c")
    (tmp_path / "hart-os-1.0.0-desktop-x86_64-linux.raw.sha256").write_bytes(b"d")
    names = [p["name"] for p in
             flasher.list_local_parts(str(tmp_path), "desktop", image="raw")]
    assert names == ["hart-os-1.0.0-desktop-x86_64-linux.raw.xz.part-00"]


def test_flash_raw_from_dir_writes_the_image_with_NO_release_and_NO_gh(
        monkeypatch, tmp_path):
    """The point of --from-dir: no release is consulted and gh need not exist.

    Every release-side collaborator is wired to EXPLODE, so if flash_raw touches
    the release path at all this test fails rather than quietly passing on a
    fallback. find_gh returns None because a local flash must work on a box with
    no GitHub CLI installed."""
    import hashlib as _h
    data = _mk_raw_image(256 * 1024)
    base = "hart-os-1.0.0-desktop-x86_64-linux.raw"
    _mk_xz_parts(tmp_path, data, base=base)
    (tmp_path / (base + ".sha256")).write_text(
        "%s  %s\n" % (_h.sha256(data).hexdigest(), base))

    dev = _wire_fake_device(monkeypatch)
    monkeypatch.setattr(flasher, "find_gh", lambda: None)

    def _boom(*a, **kw):
        raise AssertionError("--from-dir consulted the RELEASE path")
    monkeypatch.setattr(flasher, "list_parts", _boom)
    monkeypatch.setattr(flasher, "download_part", _boom)
    monkeypatch.setattr(flasher, "fetch_release_asset_text", _boom)

    ok = flasher.flash_raw(None, "desktop", _fake_disk(len(data) * 4),
                           str(tmp_path), log=lambda m: None,
                           src_dir=str(tmp_path))
    assert ok is True
    assert dev["bytes"] == data, \
        "the device must hold the exact decompressed image from byte 0"


def test_flash_raw_from_dir_still_enforces_the_companion_sha256(
        monkeypatch, tmp_path):
    """A corrupt artifact must fail just as loudly as a corrupt download -- the
    local source must not become the weak link that skips verification."""
    data = _mk_raw_image(128 * 1024)
    base = "hart-os-1.0.0-desktop-x86_64-linux.raw"
    _mk_xz_parts(tmp_path, data, base=base)
    (tmp_path / (base + ".sha256")).write_text("%s  %s\n" % ("0" * 64, base))
    _wire_fake_device(monkeypatch)
    monkeypatch.setattr(flasher, "find_gh", lambda: None)
    with pytest.raises(RuntimeError, match="does not match the published"):
        flasher.flash_raw(None, "desktop", _fake_disk(len(data) * 4),
                          str(tmp_path), log=lambda m: None,
                          src_dir=str(tmp_path))


def test_flash_raw_from_dir_does_not_delete_the_operators_parts(
        monkeypatch, tmp_path):
    """The release path deletes its own download cache once the flash verifies.
    A --from-dir set was placed there by the operator (a 7 GB download that may
    be flashed to several sticks) -- deleting it is not this script's call."""
    import hashlib as _h
    data = _mk_raw_image(128 * 1024)
    base = "hart-os-1.0.0-desktop-x86_64-linux.raw"
    paths = _mk_xz_parts(tmp_path, data, base=base)
    (tmp_path / (base + ".sha256")).write_text(
        "%s  %s\n" % (_h.sha256(data).hexdigest(), base))
    _wire_fake_device(monkeypatch)
    monkeypatch.setattr(flasher, "find_gh", lambda: None)
    assert flasher.flash_raw(None, "desktop", _fake_disk(len(data) * 4),
                             str(tmp_path), log=lambda m: None,
                             src_dir=str(tmp_path)) is True
    assert all(os.path.exists(p) for p in paths), \
        "the operator-supplied part set was deleted after a successful flash"


def test_from_dir_is_refused_for_the_iso_image(capsys):
    """--image iso computes per-part DEVICE offsets from release sizes; half-
    honoring --from-dir there would write at unproven offsets. Refuse loudly,
    the same way the other ISO-only/raw-only flags are refused."""
    args = flasher.build_parser().parse_args(
        ["--from-dir", "/some/dir", "--image", "iso", "--device", "9", "--yes"])
    assert flasher.cmd_flash(args) == 2
    assert "--from-dir" in capsys.readouterr().err


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


def test_create_log_partition_unix_no_tools_is_clean_skip(monkeypatch):
    """On a host without sgdisk/mkfs.vfat (e.g. the Windows dev box, or a bare
    macOS) the Linux/macOS carve is a clean, NON-destructive skip: returns False,
    logs the reason, shells NOTHING, never raises. (The Live OS creates HARTLOG
    itself on first boot.)"""
    monkeypatch.setattr(flasher, "IS_WIN", False)
    monkeypatch.setattr(flasher.shutil, "which", lambda _name: None)   # no sgdisk/mkfs
    monkeypatch.setattr(flasher.os.path, "exists", lambda _p: True)    # pretend dev present
    called = {"n": 0}
    monkeypatch.setattr(flasher.subprocess, "run",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    logs = []
    ok = flasher.create_log_partition({"number": 1, "dev": "/dev/sdb"}, logs.append)
    assert ok is False
    assert called["n"] == 0                       # never shells a partition tool
    assert any("not on this host" in m for m in logs)


def test_create_log_partition_unix_relocates_backup_then_carves(monkeypatch):
    """THE #128 fix at flash time: the Linux/macOS carve must run `sgdisk -e`
    (relocate the backup GPT to the TRUE device end) BEFORE measuring/carving the
    free tail, then carve + FAT32-format HARTLOG. Without the relocate, the trailing
    tail of a dd-written isohybrid is invisible and the carve no-ops. Proves the
    ORDER (relocate precedes carve) + that HARTLOG is named, typed, and formatted."""
    monkeypatch.setattr(flasher, "IS_WIN", False)
    tools = {"sgdisk": "/usr/bin/sgdisk", "mkfs.vfat": "/usr/sbin/mkfs.vfat",
             "partprobe": "/usr/sbin/partprobe", "partx": "/usr/sbin/partx"}
    monkeypatch.setattr(flasher.shutil, "which", lambda name: tools.get(name))
    monkeypatch.setattr(flasher.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(flasher.time, "sleep", lambda *_: None)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        exe = cmd[0]
        if exe.endswith("sgdisk"):
            if "-f" in cmd:
                return _CP("67584\n")                # first aligned free sector
            if "-E" in cmd:
                return _CP("60909534\n")             # last usable (tail now exposed)
            return _CP("")                           # -e, --largest-new, ...
        if exe == "lsblk":
            if "PTTYPE" in cmd:
                return _CP("gpt\n")
            return _CP("/dev/sdb1 part\n/dev/sdb2 part\n")   # the new node is sdb2
        return _CP("")                               # partprobe / partx / mkfs.vfat
    monkeypatch.setattr(flasher.subprocess, "run", fake_run)

    logs = []
    ok = flasher.create_log_partition({"number": 1, "dev": "/dev/sdb"}, logs.append)
    assert ok is True

    sg = [c for c in calls if c[0].endswith("sgdisk")]
    e_idx = next(i for i, c in enumerate(sg) if "-e" in c)
    carve_idx = next(i for i, c in enumerate(sg)
                     if any(a.startswith("--largest-new") for a in c))
    assert e_idx < carve_idx, "sgdisk -e (relocate backup GPT) must run BEFORE the carve"
    assert any("--change-name=0:HARTLOG" in c for c in sg)        # named HARTLOG
    assert any(c[0].endswith("mkfs.vfat") and "HARTLOG" in c for c in calls)  # FAT32+label
    assert any("created + FAT32-formatted" in m for m in logs)


def test_create_log_partition_unix_no_free_after_relocate_is_skip(monkeypatch):
    """If even after relocating the backup GPT there is no usable trailing tail (the
    ISO truly filled the stick), the carve is a clean skip: it must NOT carve or
    format, returns False, never raises."""
    monkeypatch.setattr(flasher, "IS_WIN", False)
    tools = {"sgdisk": "/usr/bin/sgdisk", "mkfs.vfat": "/usr/sbin/mkfs.vfat"}
    monkeypatch.setattr(flasher.shutil, "which", lambda name: tools.get(name))
    monkeypatch.setattr(flasher.os.path, "exists", lambda _p: True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0].endswith("sgdisk"):
            if "-f" in cmd:
                return _CP("4000000\n")
            if "-E" in cmd:
                return _CP("4000000\n")              # first_free == last_usable: no tail
            return _CP("")
        if cmd[0] == "lsblk":
            return _CP("gpt\n")
        return _CP("")
    monkeypatch.setattr(flasher.subprocess, "run", fake_run)
    logs = []
    ok = flasher.create_log_partition({"number": 1, "dev": "/dev/sdb"}, logs.append)
    assert ok is False
    assert not any(any(a.startswith("--largest-new") for a in c)
                   for c in calls if c[0].endswith("sgdisk")), "must NOT carve with no tail"
    assert not any(c[0].endswith("mkfs.vfat") for c in calls), "must NOT format with no tail"
    assert any("no trailing free space" in m for m in logs)


def test_create_log_partition_unix_mbr_is_skipped_not_converted(monkeypatch):
    """A DOS/MBR isohybrid must be LEFT to the Live-OS parted path: running sgdisk on
    it would convert the table + destroy the boot layout. The Unix carve detects
    PTTYPE != gpt and skips WITHOUT ever touching sgdisk."""
    monkeypatch.setattr(flasher, "IS_WIN", False)
    tools = {"sgdisk": "/usr/bin/sgdisk", "mkfs.vfat": "/usr/sbin/mkfs.vfat"}
    monkeypatch.setattr(flasher.shutil, "which", lambda name: tools.get(name))
    monkeypatch.setattr(flasher.os.path, "exists", lambda _p: True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "lsblk" and "PTTYPE" in cmd:
            return _CP("dos\n")
        return _CP("")
    monkeypatch.setattr(flasher.subprocess, "run", fake_run)
    logs = []
    ok = flasher.create_log_partition({"number": 1, "dev": "/dev/sdb"}, logs.append)
    assert ok is False
    assert not any(c[0].endswith("sgdisk") for c in calls), "must NEVER run sgdisk on MBR"
    assert any("not GPT" in m for m in logs)


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

    def fake_carve(disk, log, iso_bytes=0, **kw):
        # **kw so the HARTSTATE carve (create_log_partition(..., label=, fs=)) is
        # captured by the same stub — one carve path, two labels.
        order.append("carve")
        return True
    monkeypatch.setattr(flasher, "create_log_partition", fake_carve)

    # The post-carve re-verify reads the raw device; stub it CLEAN (intact sigs)
    # so the ordering tests don't touch hardware. Dedicated tests below drive the
    # real reverify_boot_sigs_after_carve + its abort-on-change behaviour.
    monkeypatch.setattr(flasher, "reverify_boot_sigs_after_carve",
                        lambda disk, dd, log, **k: True)
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

    def raising_carve(disk, log, iso_bytes=0):
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


# ─────────── HARTSTATE persistence partition (opt-in stateful live USB) ───────────
# HARTSTATE reuses the EXACT HARTLOG carve mechanism + safety (GPT relocate, no-free
# skip, never-raises, post-carve boot re-verify) — the only knobs that differ are the
# label (HARTSTATE) and the fs (ext4, so a bind-persisted home has Unix permissions +
# symlinks). Windows diskpart can't create ext4, so there it FAT32-formats a labelled
# placeholder the live OS reformats to ext4 on first boot. These mirror the HARTLOG
# create_log_partition tests + run on the Windows dev box (mocked runner, no device).


def test_create_state_partition_runs_correct_diskpart_script(monkeypatch):
    """The HARTSTATE carve drives the SAME diskpart script as HARTLOG but with the
    HARTSTATE label. diskpart can't make ext4, so the host-side pre-seed is FAT32
    (what the carve has always used) + the HARTSTATE label the live OS detects to
    bind persistence; on a confirmed format it returns True."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    captured = {}
    monkeypatch.setattr(flasher.subprocess, "run", _fake_log_partition_diskpart(captured))
    logs = []
    ok = flasher.create_log_partition({"number": 1}, logs.append,
                                      label=flasher.STATE_PART_LABEL, fs="ext4")
    assert ok is True
    s = captured["script"]
    assert "select disk 1" in s
    assert "create partition primary" in s
    assert "format fs=fat32 label=HARTSTATE quick" in s
    assert flasher.STATE_PART_LABEL == "HARTSTATE"
    assert any("HARTSTATE partition: created" in m for m in logs)


def test_create_state_partition_unix_carves_ext4_with_label(monkeypatch):
    """On Linux/macOS the HARTSTATE carve relocates the backup GPT (sgdisk -e) BEFORE
    carving, names the new partition HARTSTATE, types it Linux-fs (8300), and formats
    it EXT4 (mkfs.ext4) — the persistence-ready fs. Proves the relocate-before-carve
    order + the HARTSTATE label/typecode/ext4 argv."""
    monkeypatch.setattr(flasher, "IS_WIN", False)
    tools = {"sgdisk": "/usr/bin/sgdisk", "mkfs.ext4": "/usr/sbin/mkfs.ext4",
             "partprobe": "/usr/sbin/partprobe", "partx": "/usr/sbin/partx"}
    monkeypatch.setattr(flasher.shutil, "which", lambda name: tools.get(name))
    monkeypatch.setattr(flasher.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(flasher.time, "sleep", lambda *_: None)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        exe = cmd[0]
        if exe.endswith("sgdisk"):
            if "-f" in cmd:
                return _CP("67584\n")                # first aligned free sector
            if "-E" in cmd:
                return _CP("60909534\n")             # last usable (tail now exposed)
            return _CP("")                           # -e, --largest-new, ...
        if exe == "lsblk":
            if "PTTYPE" in cmd:
                return _CP("gpt\n")
            return _CP("/dev/sdb1 part\n/dev/sdb2 part\n")   # the new node is sdb2
        return _CP("")                               # partprobe / partx / mkfs.ext4
    monkeypatch.setattr(flasher.subprocess, "run", fake_run)

    logs = []
    ok = flasher.create_log_partition({"number": 1, "dev": "/dev/sdb"}, logs.append,
                                      label=flasher.STATE_PART_LABEL, fs="ext4")
    assert ok is True

    sg = [c for c in calls if c[0].endswith("sgdisk")]
    e_idx = next(i for i, c in enumerate(sg) if "-e" in c)
    carve_idx = next(i for i, c in enumerate(sg)
                     if any(a.startswith("--largest-new") for a in c))
    assert e_idx < carve_idx, "sgdisk -e (relocate backup GPT) must run BEFORE the carve"
    assert any("--change-name=0:HARTSTATE" in c for c in sg)      # named HARTSTATE
    assert any("--typecode=0:8300" in c for c in sg)             # Linux-filesystem typecode
    assert any(c[0].endswith("mkfs.ext4") and "HARTSTATE" in c for c in calls)  # ext4+label
    assert not any(c[0].endswith("mkfs.vfat") for c in calls), "HARTSTATE must not be FAT32 on Unix"
    assert any("created + ext4-formatted" in m for m in logs)


def test_create_state_partition_carve_failure_is_swallowed(monkeypatch):
    """A carve failure (diskpart timeout/OSError) is SWALLOWED — the HARTSTATE carve
    can never raise nor fail the already-successful flash (identical to HARTLOG)."""
    monkeypatch.setattr(flasher, "IS_WIN", True)

    def boom(cmd, **kw):
        raise flasher.subprocess.TimeoutExpired(cmd, kw.get("timeout", 120))

    monkeypatch.setattr(flasher.subprocess, "run", boom)
    logs = []
    ok = flasher.create_log_partition({"number": 1}, logs.append,      # must NOT raise
                                      label=flasher.STATE_PART_LABEL, fs="ext4")
    assert ok is False
    assert any("HARTSTATE partition" in m and ("unavailable" in m or "timed out" in m)
               for m in logs)


def test_state_partition_flag_parses():
    """--state-partition is OFF by default and the explicit opt-in to the stateful
    live-USB carve."""
    args = flasher.build_parser().parse_args(["--device", "1", "--yes"])
    assert args.state_partition is False
    args2 = flasher.build_parser().parse_args(
        ["--device", "1", "--yes", "--state-partition"])
    assert args2.state_partition is True


def test_flash_creates_state_partition_after_successful_verify(monkeypatch):
    """make_state_partition=True (--state-partition) must carve HARTSTATE AFTER a
    successful verify, through the same create_log_partition path as HARTLOG."""
    order = _stub_flash_machinery(monkeypatch, verify_result=True)
    ok = flasher.flash("tag", "desktop", {"number": 1, "model": "USB", "dev": "/dev/sdb",
                                          "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=lambda m: None,
                       make_state_partition=True)
    assert ok is True
    assert order == ["verify", "carve"], \
        "the HARTSTATE carve must run AFTER verify_iso, only on success"


def test_flash_state_carve_exception_does_not_fail_the_flash(monkeypatch):
    """If the HARTSTATE carve RAISES (it shouldn't), flash() swallows it — the carve
    can never turn a successful flash into a failure."""
    order = _stub_flash_machinery(monkeypatch, verify_result=True)

    def raising_carve(disk, log, iso_bytes=0, **kw):
        order.append("carve")
        raise RuntimeError("carve exploded")
    monkeypatch.setattr(flasher, "create_log_partition", raising_carve)

    try:
        ok = flasher.flash("tag", "desktop",
                           {"number": 1, "model": "USB", "dev": "/dev/sdb",
                            "physdrive": "/dev/sdb"},
                           "download", "/tmp", log=lambda m: None,
                           make_state_partition=True)
    except RuntimeError:
        pytest.fail("a state-carve exception must not propagate out of flash()")
    assert ok is True
    assert order == ["verify", "carve"]


# ─────────── #128: the Windows GPT relocate (sgdisk -e equivalent) ───────────
# ROOT CAUSE of the failed Windows carve: a dd-written isohybrid GPT ISO leaves the
# BACKUP GPT header at the ISO image's last LBA (mid-stick), so the PRIMARY header
# caps LastUsableLBA at the ISO boundary and diskpart sees NO free tail ("not enough
# usable free space") -> the carve no-ops ("partition 1 is the WHOLE 29 GB stick").
# THE FIX: _grow_gpt_to_device_end relocates the backup GPT to the device's TRUE last
# LBA (the std-lib equivalent of `sgdisk -e`) so diskpart then sees the revealed free
# tail. It is pure-logic over a SEEKABLE handle, so these tests drive it against a
# synthetic temp-file "device" with a valid mini-GPT — no real hardware, no ctypes.
import struct as _struct
import zlib as _zlib

_GPT_NUM = 128
_GPT_ESIZE = 128


def _build_synthetic_gpt_device(path, device_sectors, iso_sectors, entry_ending_lba,
                                valid_gpt=True):
    """Write a synthetic 'device' image with a valid mini-GPT whose backup header is
    MID-FILE (at iso_sectors-1) — the exact dd-written-isohybrid symptom. Also lays
    down the protective-MBR 0x55AA boot sig @0x1FE and the ISO9660 'CD001' magic
    @0x8001, so a test can prove _grow_gpt_to_device_end never touches them.

    Returns the meta dict {array, array_crc, primary_hdr} the assertions compare
    against. valid_gpt=False writes garbage at LBA1 (an MBR isohybrid stand-in)."""
    SEC = flasher.SECTOR
    first_usable = 34
    part_entry_lba = 2
    last_usable_old = iso_sectors - 34
    alt_lba_old = iso_sectors - 1

    # One USED partition entry (nonzero type GUID) + 127 empty slots.
    array = bytearray(_GPT_NUM * _GPT_ESIZE)
    _struct.pack_into("<16s", array, 0, bytes(range(1, 17)))     # type GUID (used)
    _struct.pack_into("<16s", array, 16, bytes(range(17, 33)))   # unique GUID
    _struct.pack_into("<Q", array, 32, 2048)                     # StartingLBA
    _struct.pack_into("<Q", array, 40, entry_ending_lba)         # EndingLBA
    nm = "HART_OS".encode("utf-16-le")
    array[56:56 + len(nm)] = nm
    array_crc = _zlib.crc32(bytes(array)) & 0xFFFFFFFF

    def _mk_header(my_lba, alt_lba, part_lba):
        h = bytearray(SEC)
        h[0:8] = b"EFI PART"
        _struct.pack_into("<I", h, 8, 0x00010000)                # revision 1.0
        _struct.pack_into("<I", h, 12, 92)                       # header size
        _struct.pack_into("<Q", h, 24, my_lba)                   # MyLBA
        _struct.pack_into("<Q", h, 32, alt_lba)                  # AlternateLBA
        _struct.pack_into("<Q", h, 40, first_usable)             # FirstUsableLBA
        _struct.pack_into("<Q", h, 48, last_usable_old)          # LastUsableLBA
        _struct.pack_into("<16s", h, 56, bytes(range(33, 49)))   # DiskGUID
        _struct.pack_into("<Q", h, 72, part_lba)                 # PartitionEntryLBA
        _struct.pack_into("<I", h, 80, _GPT_NUM)                 # NumberOfPartitionEntries
        _struct.pack_into("<I", h, 84, _GPT_ESIZE)              # SizeOfPartitionEntry
        _struct.pack_into("<I", h, 88, array_crc)               # PartitionEntryArrayCRC32
        crc = _zlib.crc32(bytes(h[:92])) & 0xFFFFFFFF
        _struct.pack_into("<I", h, 16, crc)                      # HeaderCRC32
        return bytes(h)

    primary = _mk_header(1, alt_lba_old, part_entry_lba)
    mid_backup_array_lba = alt_lba_old - 32
    mid_backup = _mk_header(alt_lba_old, 1, mid_backup_array_lba)

    with open(path, "wb") as f:
        f.truncate(device_sectors * SEC)
        f.seek(0x1FE); f.write(b"\x55\xAA")                      # protective-MBR boot sig
        if valid_gpt:
            f.seek(1 * SEC); f.write(primary)                   # primary GPT header
            f.seek(part_entry_lba * SEC); f.write(bytes(array))  # primary entry array
            # the STALE mid-file backup (array + header) the dd write left behind
            f.seek(mid_backup_array_lba * SEC); f.write(bytes(array))
            f.seek(alt_lba_old * SEC); f.write(mid_backup)
        else:
            f.seek(1 * SEC); f.write(b"\xE9" + b"\x90" * 445)   # MBR-ish garbage, no sig
        f.seek(0x8001); f.write(b"CD001")                       # ISO9660 magic @ LBA64+1
    return {"array": bytes(array), "array_crc": array_crc, "primary_hdr": primary}


def _validate_gpt_header(sector_bytes):
    """Return (sig_ok, header_crc_ok) for a 512 B GPT-header sector."""
    sig_ok = sector_bytes[0:8] == b"EFI PART"
    stored = _struct.unpack_from("<I", sector_bytes, 16)[0]
    chk = bytearray(sector_bytes[:92])
    _struct.pack_into("<I", chk, 16, 0)
    return sig_ok, (_zlib.crc32(bytes(chk)) & 0xFFFFFFFF) == stored


def test_grow_gpt_relocates_backup_to_device_end_nondestructive(tmp_path):
    """THE #128 fix: _grow_gpt_to_device_end moves the backup GPT from its mid-file
    (ISO-boundary) location to the device's TRUE last LBA, rewriting LastUsableLBA so
    diskpart later sees the trailing free tail — WITHOUT touching the boot sig, the
    ISO9660 magic, or the existing partition entry (which is within the ISO bound)."""
    SEC = flasher.SECTOR
    device_sectors = 131072          # 64 MiB synthetic device
    iso_sectors = 32768              # 16 MiB synthetic ISO
    path = str(tmp_path / "dev.img")
    meta = _build_synthetic_gpt_device(path, device_sectors, iso_sectors,
                                       entry_ending_lba=32000)   # within the ISO bound
    before = open(path, "rb").read()

    with open(path, "rb+") as h:
        ok = flasher._grow_gpt_to_device_end(h, device_sectors,
                                             iso_sectors * SEC, log=lambda m: None)
    assert ok is True
    after = open(path, "rb").read()

    # ── primary header relocated to the device end + CRC re-validates ──
    prim = after[SEC:SEC + 512]
    sig_ok, crc_ok = _validate_gpt_header(prim)
    assert sig_ok and crc_ok
    assert _struct.unpack_from("<Q", prim, 32)[0] == device_sectors - 1    # AlternateLBA
    assert _struct.unpack_from("<Q", prim, 48)[0] == device_sectors - 34   # LastUsableLBA
    assert _struct.unpack_from("<I", prim, 88)[0] == meta["array_crc"]     # array CRC unchanged

    # ── the existing partition entry bytes are byte-for-byte unchanged ──
    arr_after = after[2 * SEC: 2 * SEC + _GPT_NUM * _GPT_ESIZE]
    assert arr_after == meta["array"]

    # ── a VALID backup GPT now sits at device_sectors-1 ──
    bh = after[(device_sectors - 1) * SEC:(device_sectors - 1) * SEC + 512]
    sig_ok_b, crc_ok_b = _validate_gpt_header(bh)
    assert sig_ok_b and crc_ok_b
    assert _struct.unpack_from("<Q", bh, 24)[0] == device_sectors - 1      # MyLBA
    assert _struct.unpack_from("<Q", bh, 32)[0] == 1                       # AlternateLBA -> primary
    assert _struct.unpack_from("<Q", bh, 72)[0] == device_sectors - 33     # PartitionEntryLBA
    barr = after[(device_sectors - 33) * SEC:(device_sectors - 33) * SEC + _GPT_NUM * _GPT_ESIZE]
    assert barr == meta["array"]                                          # backup array == primary

    # ── NON-DESTRUCTION proof: LBA0 (incl. 0x55AA) + the ISO9660 magic are identical ──
    assert after[:SEC] == before[:SEC]
    assert after[0x1FE:0x200] == before[0x1FE:0x200] == b"\x55\xAA"
    assert after[0x8001:0x8006] == before[0x8001:0x8006] == b"CD001"


def test_grow_gpt_mbr_isohybrid_is_skipped_not_converted(tmp_path):
    """An MBR isohybrid (no 'EFI PART' at LBA1) must be LEFT to the Live-OS parted
    path: the relocate detects the missing GPT signature and returns False WITHOUT
    writing a single byte (it must never convert the table)."""
    device_sectors = 131072
    path = str(tmp_path / "mbr.img")
    _build_synthetic_gpt_device(path, device_sectors, 32768, 32000, valid_gpt=False)
    before = open(path, "rb").read()
    with open(path, "rb+") as h:
        ok = flasher._grow_gpt_to_device_end(h, device_sectors,
                                             32768 * flasher.SECTOR, log=lambda m: None)
    assert ok is False
    assert open(path, "rb").read() == before                 # not one byte written


def test_grow_gpt_idempotent_when_backup_already_at_device_end(tmp_path):
    """If the backup GPT is ALREADY at the device end (a second flash, or a stick
    written directly to the full disk), the relocate is a clean no-op that returns
    True and changes nothing."""
    SEC = flasher.SECTOR
    device_sectors = 131072
    path = str(tmp_path / "full.img")
    # iso_sectors == device_sectors -> last_usable_old already == device_sectors-34.
    _build_synthetic_gpt_device(path, device_sectors, device_sectors, 130000)
    before = open(path, "rb").read()
    with open(path, "rb+") as h:
        ok = flasher._grow_gpt_to_device_end(h, device_sectors,
                                             device_sectors * SEC, log=lambda m: None)
    assert ok is True
    assert open(path, "rb").read() == before                 # idempotent: nothing rewritten


def test_grow_gpt_clamps_entry_past_iso_bound_and_fixes_array_crc(tmp_path):
    """Defensive clamp: a USED entry whose EndingLBA runs PAST the ISO image bound is
    capped to the ISO's last LBA, the array CRC is recomputed, and BOTH the primary
    and the backup entry arrays on disk are rewritten so their CRCs stay consistent."""
    SEC = flasher.SECTOR
    device_sectors = 131072
    iso_sectors = 32768
    past = iso_sectors + 500                                  # EndingLBA beyond the ISO bound
    path = str(tmp_path / "clamp.img")
    _build_synthetic_gpt_device(path, device_sectors, iso_sectors, entry_ending_lba=past)
    with open(path, "rb+") as h:
        ok = flasher._grow_gpt_to_device_end(h, device_sectors,
                                             iso_sectors * SEC, log=lambda m: None)
    assert ok is True
    after = open(path, "rb").read()

    iso_last_lba = iso_sectors - 1
    # primary entry EndingLBA clamped on disk
    prim_arr = after[2 * SEC: 2 * SEC + _GPT_NUM * _GPT_ESIZE]
    assert _struct.unpack_from("<Q", prim_arr, 40)[0] == iso_last_lba
    # backup entry array (tail) clamped identically
    barr = after[(device_sectors - 33) * SEC:(device_sectors - 33) * SEC + _GPT_NUM * _GPT_ESIZE]
    assert _struct.unpack_from("<Q", barr, 40)[0] == iso_last_lba
    assert barr == prim_arr
    # the recomputed array CRC in the primary header matches the on-disk array
    expect_crc = _zlib.crc32(prim_arr) & 0xFFFFFFFF
    prim_hdr = after[SEC:SEC + 512]
    assert _struct.unpack_from("<I", prim_hdr, 88)[0] == expect_crc
    sig_ok, crc_ok = _validate_gpt_header(prim_hdr)
    assert sig_ok and crc_ok                                 # header CRC still valid after the edit


def test_windows_carve_relocates_gpt_before_diskpart(monkeypatch):
    """The dispatcher must run the GPT relocate (the sgdisk -e equivalent) BEFORE the
    diskpart carve when an exact ISO size is known — that ORDER is the whole fix: the
    relocate reveals the tail, then diskpart carves it."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    order = []
    monkeypatch.setattr(flasher, "_windows_grow_gpt_to_device_end",
                        lambda disk, iso_bytes, log: order.append(("grow", iso_bytes)) or True)
    monkeypatch.setattr(flasher, "_create_log_partition_windows",
                        lambda disk, log, **kw: order.append(("diskpart",)) or True)
    flasher.create_log_partition({"number": 1}, lambda m: None, iso_bytes=7030001664)
    assert order == [("grow", 7030001664), ("diskpart",)], \
        "the GPT relocate must run BEFORE the diskpart carve"


def test_windows_carve_skips_grow_without_iso_size(monkeypatch):
    """Legacy 2-arg callers (no iso_bytes) must NOT trigger the relocate — they fall
    straight through to the diskpart carve, preserving the old behaviour."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    called = {"grow": 0}
    monkeypatch.setattr(flasher, "_windows_grow_gpt_to_device_end",
                        lambda *a, **k: called.__setitem__("grow", called["grow"] + 1) or True)
    monkeypatch.setattr(flasher, "_create_log_partition_windows", lambda disk, log, **kw: True)
    flasher.create_log_partition({"number": 1}, lambda m: None)   # no iso_bytes
    assert called["grow"] == 0


def test_windows_grow_skips_when_device_size_unknown(monkeypatch):
    """If the exact device size can't be read (IOCTL failed), the relocate is a clean
    skip — it must NOT open the raw device or rewrite any GPT (a rounded size would
    place the backup header off the true end = an invalid GPT)."""
    monkeypatch.setattr(flasher, "_windows_device_size_bytes", lambda num, log: 0)
    opened = {"n": 0}
    monkeypatch.setattr(flasher, "_open_seekable_raw",
                        lambda *a, **k: opened.__setitem__("n", opened["n"] + 1))
    logs = []
    ok = flasher._windows_grow_gpt_to_device_end({"number": 1, "physdrive": r"\\.\PhysicalDrive1"},
                                                 7030001664, logs.append)
    assert ok is False
    assert opened["n"] == 0                                   # never touched the raw device
    assert any("no exact device size" in m for m in logs)


# ─────────── #128: post-carve boot-signature re-verify (abort-on-change) ───────────
# The carve rewrites the GPT (relocate) + runs diskpart (create/format) — the step
# that historically corrupted a freshly-flashed stick's EFI/GPT. reverify_boot_sigs_
# after_carve re-reads the ISO9660 'CD001' magic (@0x8001) + the 0x55AA boot sig
# (@0x1FE) AFTER the carve and is TRI-STATE: True=intact, False=definitively changed
# (abort), None=could-not-read (indeterminate, never a false brick). flash() flips
# its result to FAILED only on a definitive False.


def _reads(mapping):
    """A read_at stand-in that answers by offset from a {offset: bytes} map."""
    def fake_read_at(dev, offset, n, dd):
        return mapping[offset][:n]
    return fake_read_at


def test_reverify_true_when_both_sigs_intact(monkeypatch):
    """A clean read showing CD001 @0x8001 + 0x55AA @0x1FE returns True — the carve
    left the boot image untouched."""
    monkeypatch.setattr(flasher, "read_at",
                        _reads({0x8001: b"CD001", 0x1FE: b"\x55\xAA"}))
    v = flasher.reverify_boot_sigs_after_carve(
        {"physdrive": r"\\.\PhysicalDrive1"}, None, lambda m: None)
    assert v is True


def test_reverify_false_when_boot_sig_changed(monkeypatch):
    """A clean read where the 0x55AA boot sig was overwritten returns False — the
    DEFINITIVE 'carve corrupted the boot image' verdict that must abort the flash."""
    monkeypatch.setattr(flasher, "read_at",
                        _reads({0x8001: b"CD001", 0x1FE: b"\x00\x00"}))
    logs = []
    v = flasher.reverify_boot_sigs_after_carve(
        {"physdrive": r"\\.\PhysicalDrive1"}, None, logs.append)
    assert v is False
    assert any("CHANGED" in m for m in logs)


def test_reverify_false_when_iso9660_magic_changed(monkeypatch):
    """The ISO9660 'CD001' magic being clobbered is equally a definitive False."""
    monkeypatch.setattr(flasher, "read_at",
                        _reads({0x8001: b"XXXXX", 0x1FE: b"\x55\xAA"}))
    v = flasher.reverify_boot_sigs_after_carve(
        {"physdrive": r"\\.\PhysicalDrive1"}, None, lambda m: None)
    assert v is False


def test_reverify_indeterminate_on_busy_device_no_false_brick(monkeypatch):
    """If the device can NEVER be read (a persistent post-format 'device not ready'
    transient), the verdict is None — INDETERMINATE — so the caller does NOT claim a
    brick on a read failure. Retries the winerror-32/21/5 transient with backoff."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    monkeypatch.setattr(flasher.time, "sleep", lambda *_: None)   # no real wait
    busy = OSError("sharing violation")
    busy.winerror = 32

    def always_busy(dev, offset, n, dd):
        raise busy
    monkeypatch.setattr(flasher, "read_at", always_busy)
    logs = []
    v = flasher.reverify_boot_sigs_after_carve(
        {"physdrive": r"\\.\PhysicalDrive1"}, None, logs.append, tries=3)
    assert v is None                                             # never False on a read error
    assert any("indeterminate" in m for m in logs)


def test_reverify_retries_then_succeeds_after_transient(monkeypatch):
    """A transient 'not ready' (winerror 21) on the first read, then a clean read,
    must resolve to a real verdict (True) — proving the transient is handled, not
    fatal."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    monkeypatch.setattr(flasher.time, "sleep", lambda *_: None)
    state = {"n": 0}

    def flaky(dev, offset, n, dd):
        # First CALL of the first attempt raises NOT_READY; subsequent reads are clean.
        state["n"] += 1
        if state["n"] == 1:
            e = OSError("not ready")
            e.winerror = 21
            raise e
        return {0x8001: b"CD001", 0x1FE: b"\x55\xAA"}[offset][:n]
    monkeypatch.setattr(flasher, "read_at", flaky)
    v = flasher.reverify_boot_sigs_after_carve(
        {"physdrive": r"\\.\PhysicalDrive1"}, None, lambda m: None)
    assert v is True


def test_reverify_short_read_is_indeterminate(monkeypatch):
    """A short read (device not settled — fewer bytes than the signature) is treated
    as indeterminate (None), never as a corrupted signature."""
    monkeypatch.setattr(flasher, "IS_WIN", True)
    monkeypatch.setattr(flasher.time, "sleep", lambda *_: None)
    monkeypatch.setattr(flasher, "read_at",
                        _reads({0x8001: b"CD", 0x1FE: b""}))       # truncated
    v = flasher.reverify_boot_sigs_after_carve(
        {"physdrive": r"\\.\PhysicalDrive1"}, None, lambda m: None, tries=2)
    assert v is None


def test_flash_aborts_when_post_carve_reverify_fails(monkeypatch):
    """END-TO-END abort: a successful verify + carve, but the POST-carve re-verify
    finds a changed boot sig -> flash() must flip its result to FAILED so the user
    re-flashes rather than booting a silently-corrupted stick."""
    order = _stub_flash_machinery(monkeypatch, verify_result=True)
    monkeypatch.setattr(flasher, "reverify_boot_sigs_after_carve",
                        lambda disk, dd, log, **k: False)
    logs = []
    ok = flasher.flash("tag", "desktop",
                       {"number": 1, "model": "USB", "dev": "/dev/sdb",
                        "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=logs.append,
                       make_log_partition=True)
    assert ok is False, "a definitive post-carve boot-sig change must fail the flash"
    assert order == ["verify", "carve"]
    assert any("POST-CARVE CHECK FAILED" in m for m in logs)


def test_flash_indeterminate_reverify_keeps_success(monkeypatch):
    """An INDETERMINATE post-carve re-verify (None — device busy) must NOT turn a
    successful flash into a failure (no false brick claim)."""
    _stub_flash_machinery(monkeypatch, verify_result=True)
    monkeypatch.setattr(flasher, "reverify_boot_sigs_after_carve",
                        lambda disk, dd, log, **k: None)
    ok = flasher.flash("tag", "desktop",
                       {"number": 1, "model": "USB", "dev": "/dev/sdb",
                        "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=lambda m: None,
                       make_log_partition=True)
    assert ok is True


def test_flash_no_reverify_on_default_path(monkeypatch):
    """The post-carve re-verify only runs on the opt-in carve path. A default flash
    (no make_log_partition) must NOT call reverify_boot_sigs_after_carve at all."""
    _stub_flash_machinery(monkeypatch, verify_result=True)
    called = {"n": 0}
    monkeypatch.setattr(flasher, "reverify_boot_sigs_after_carve",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True)
    ok = flasher.flash("tag", "desktop",
                       {"number": 1, "model": "USB", "dev": "/dev/sdb",
                        "physdrive": "/dev/sdb"},
                       "download", "/tmp", log=lambda m: None)
    assert ok is True
    assert called["n"] == 0, "no post-carve re-verify when the carve never ran"


# ─────────── _WinExclusiveWriter.write_at byte-placement ───────────
# The actual byte-write path (seek + write at offset) had ZERO non-hardware
# coverage, yet the file's own docstring warns "a wrong offset corrupts the
# image". These tests drive write_at against an in-memory BytesIO-backed fake
# kernel32 handle (no ctypes WinDLL, no PhysicalDrive) and assert bytes land at
# the right offsets + the final partial sector is zero-padded to SECTOR.

import io


class _FakeDWORD:
    """Stand-in for wintypes.DWORD() — write_at sets .value via byref()."""
    def __init__(self):
        self.value = 0


class _FakeKernel32:
    """A fake kernel32 backed by a BytesIO. SetFilePointerEx moves the cursor,
    WriteFile writes the buffer at the cursor + reports the byte count, exactly
    as the real WriteFile/SetFilePointerEx pair the writer drives."""
    def __init__(self):
        self.buf = io.BytesIO()

    def SetFilePointerEx(self, h, offset, _new_ptr, _whence):
        # _whence == 0 (FILE_BEGIN); a negative seek would fail in the real API.
        if offset < 0:
            return 0
        self.buf.seek(offset)
        return 1                                   # non-zero == success

    def WriteFile(self, _h, data, length, written_ref, _overlapped):
        # ctypes passes the buffer; bytes() handles both bytes and ctypes views.
        self.buf.write(bytes(bytes(data)[:length]))
        written_ref._obj.value = length            # byref() target
        return 1                                   # non-zero == success


class _FakeByref:
    """Minimal ctypes.byref stand-in: wraps the DWORD so WriteFile can set it."""
    def __init__(self, obj):
        self._obj = obj


def _fake_writer():
    """A _WinExclusiveWriter wired to a fake kernel32 + BytesIO — bypasses the
    real exclusive-handle open (no hardware, no ctypes WinDLL)."""
    w = flasher._WinExclusiveWriter.__new__(flasher._WinExclusiveWriter)
    fake_k = _FakeKernel32()
    w.k = fake_k
    w.h = object()                                  # opaque handle; the fake k ignores it

    class _FakeCtypes:
        @staticmethod
        def byref(obj):
            return _FakeByref(obj)

        @staticmethod
        def get_last_error():
            return 0
    w.ctypes = _FakeCtypes()

    class _FakeWintypes:
        DWORD = _FakeDWORD
    w.wintypes = _FakeWintypes()
    return w, fake_k


def test_write_at_places_bytes_at_the_seeked_offset():
    """A single write lands exactly at byte_offset — never at 0 + never shifted."""
    w, k = _fake_writer()
    offset = 3 * 1024 * 1024                         # 3 MiB, MiB-aligned like a part
    payload = b"HART" * (flasher.SECTOR // 4)        # exactly one 512B sector
    n = w.write_at(offset, io.BytesIO(payload))
    assert n == len(payload)
    raw = k.buf.getvalue()
    assert len(raw) == offset + len(payload)
    assert raw[:offset] == b"\x00" * offset          # nothing written before offset
    assert raw[offset:offset + len(payload)] == payload


def test_write_at_multipart_offsets_are_correct():
    """Multiple parts written at cumulative offsets (the real multi-part flow)
    land contiguously, each at its own offset — no overlap, no gap, no shift."""
    w, k = _fake_writer()
    p0 = b"A" * flasher.SECTOR
    p1 = b"B" * flasher.SECTOR
    p2 = b"C" * flasher.SECTOR
    offs = [0, flasher.SECTOR, 2 * flasher.SECTOR]
    w.write_at(offs[0], io.BytesIO(p0))
    w.write_at(offs[1], io.BytesIO(p1))
    w.write_at(offs[2], io.BytesIO(p2))
    raw = k.buf.getvalue()
    assert raw[offs[0]:offs[0] + flasher.SECTOR] == p0
    assert raw[offs[1]:offs[1] + flasher.SECTOR] == p1
    assert raw[offs[2]:offs[2] + flasher.SECTOR] == p2
    assert len(raw) == 3 * flasher.SECTOR


def test_write_at_pads_final_partial_sector_with_zeros():
    """A payload that is NOT a whole-sector multiple is zero-padded UP to the next
    SECTOR boundary (raw block writes must be sector-aligned), and the reported
    byte count reflects the padded length."""
    w, k = _fake_writer()
    payload = b"\xab" * (flasher.SECTOR + 100)       # 1 full sector + 100 bytes
    n = w.write_at(0, io.BytesIO(payload))
    expected_len = 2 * flasher.SECTOR                # padded up to 2 full sectors
    assert n == expected_len
    raw = k.buf.getvalue()
    assert len(raw) == expected_len
    assert raw[:len(payload)] == payload             # real bytes intact
    assert raw[len(payload):] == b"\x00" * (expected_len - len(payload))  # zero pad


def test_write_at_chunks_larger_than_chunk_size_are_all_written():
    """A source bigger than CHUNK is read + written across multiple WriteFile
    calls; every byte still lands, in order, with no truncation."""
    w, k = _fake_writer()
    payload = bytes((i % 256) for i in range(flasher.CHUNK + flasher.SECTOR))
    n = w.write_at(0, io.BytesIO(payload))
    assert n == len(payload)                         # already sector-aligned
    assert k.buf.getvalue() == payload


# ── --start-part RESUME (salvages a reaped / timed-out flash) ────────────────
# The write is slow (~6.6 MB/s on a Cruzer Blade => ~17 min for the 6.7 GB
# desktop ISO), longer than a single uninterrupted window on some hosts. When a
# prior run already wrote the first N parts, `--start-part N` resumes: it writes
# ONLY parts >= N at their ABSOLUTE offsets and SKIPS the destructive diskpart
# clean (which would wipe the already-written parts). Validated end-to-end by a
# real flash whose sha256 read-back matched byte-for-byte.

def _resume_env(monkeypatch, parts):
    sizes = {p["name"]: p["size"] for p in parts}
    dl, writes = [], []
    monkeypatch.setattr(flasher, "IS_WIN", False)   # exercise the portable write path
    monkeypatch.setattr(flasher, "find_gh", lambda: "gh")
    monkeypatch.setattr(flasher, "find_dd", lambda: "dd")
    monkeypatch.setattr(flasher, "list_parts", lambda gh, tag, variant: [dict(p) for p in parts])
    monkeypatch.setattr(flasher, "verify_iso", lambda disk, dd, log: True)
    monkeypatch.setattr(flasher, "_run", lambda *a, **k: None)
    monkeypatch.setattr(flasher, "download_part",
                        lambda gh, tag, name, tmp, want, log, tries=4: dl.append(name) or ("src:" + name))

    def _fake_write(disk, src, off, dd, log, writer):
        name = src.split(":", 1)[1]
        writes.append((name, off))
        return sizes[name]
    monkeypatch.setattr(flasher, "write_source_to_device", _fake_write)
    return dl, writes


def test_start_part_resume_writes_only_remaining_parts_at_absolute_offsets(monkeypatch, tmp_path):
    parts = [{"name": "p0", "size": 100, "id": 0, "state": "uploaded"},
             {"name": "p1", "size": 200, "id": 1, "state": "uploaded"},
             {"name": "p2", "size": 300, "id": 2, "state": "uploaded"},
             {"name": "p3", "size": 400, "id": 3, "state": "uploaded"}]
    dl, writes = _resume_env(monkeypatch, parts)
    disk = {"number": 2, "model": "T", "physdrive": r"\.\PhysicalDrive2", "dev": "/dev/sdx"}
    ok = flasher.flash("tag", "desktop", disk, "download", str(tmp_path), start_part=2)
    assert ok is True
    # only parts >= 2 are downloaded + written; 0 and 1 are already on the device
    assert dl == ["p2", "p3"]
    assert [n for n, _ in writes] == ["p2", "p3"]
    # ABSOLUTE cumulative offsets survive the skip: p2 @ 100+200, p3 @ 100+200+300
    assert dict(writes) == {"p2": 300, "p3": 600}


def test_start_part_zero_is_a_full_flash_no_skip(monkeypatch, tmp_path):
    parts = [{"name": "p%d" % i, "size": (i + 1) * 10, "id": i, "state": "uploaded"}
             for i in range(3)]
    _dl, writes = _resume_env(monkeypatch, parts)
    disk = {"number": 2, "model": "T", "physdrive": r"\.\x", "dev": "/dev/sdx"}
    ok = flasher.flash("tag", "desktop", disk, "download", str(tmp_path))  # default start_part=0
    assert ok is True
    assert [n for n, _ in writes] == ["p0", "p1", "p2"]      # every part written
    assert dict(writes) == {"p0": 0, "p1": 10, "p2": 30}     # unchanged normal path


# ── Built-in full sha256 read-back verify (replaces the bespoke verify script) ──
# The flasher records each part's source sha256 at write time, then reads those regions
# back off the device and compares. A regular file stands in for the raw device
# (sha256_device_region uses builtin open(), so a file path works). Validated end-to-end
# against a real USB stick; these pin the logic + the resume-safe sidecar.
import hashlib as _hl


def test_verify_reads_back_and_matches(tmp_path):
    p0 = b'A' * 4096
    p1 = b'B' * 8192
    parts = [{'name': 'part-00', 'size': len(p0)}, {'name': 'part-01', 'size': len(p1)}]
    dev = tmp_path / 'device.img'
    dev.write_bytes(p0 + p1)                       # the "written" device
    work = str(tmp_path / 'work')
    os.makedirs(work)
    # what the flasher records at write time (per part), surviving --start-part passes
    flasher.record_part_hash(work, 'part-00', 0, len(p0), _hl.sha256(p0).hexdigest())
    flasher.record_part_hash(work, 'part-01', len(p0), len(p1), _hl.sha256(p1).hexdigest())
    disk = {'physdrive': str(dev), 'dev': None}
    assert flasher.full_verify(disk, parts, work, None, lambda m: None) is True


def test_verify_detects_a_corrupted_device(tmp_path):
    p0 = b'A' * 4096
    parts = [{'name': 'part-00', 'size': 4096}]
    dev = tmp_path / 'device.img'
    dev.write_bytes(b'X' + p0[1:])                 # one byte flipped
    work = str(tmp_path / 'w')
    os.makedirs(work)
    flasher.record_part_hash(work, 'part-00', 0, 4096, _hl.sha256(p0).hexdigest())
    disk = {'physdrive': str(dev), 'dev': None}
    assert flasher.full_verify(disk, parts, work, None, lambda m: None) is False


def test_verify_returns_none_when_record_incomplete(tmp_path):
    # A partial flash (not every part hashed yet) -> cannot verify -> None (not a fail).
    parts = [{'name': 'part-00', 'size': 10}, {'name': 'part-01', 'size': 10}]
    work = str(tmp_path / 'w')
    os.makedirs(work)
    flasher.record_part_hash(work, 'part-00', 0, 10, 'x' * 64)   # only 1 of 2 recorded
    disk = {'physdrive': str(tmp_path / 'nope'), 'dev': None}
    assert flasher.full_verify(disk, parts, work, None, lambda m: None) is None


def test_sha256_device_region_reads_the_right_slice(tmp_path):
    blob = bytes(range(256)) * 40
    dev = tmp_path / 'd.img'
    dev.write_bytes(blob)
    got = flasher.sha256_device_region(str(dev), 512, 1024, None)
    assert got == _hl.sha256(blob[512:512 + 1024]).hexdigest()


def test_latest_nightly_tag_skips_drafts_and_picks_newest_published(monkeypatch):
    """GitHub's /releases lists DRAFT releases first; latest_nightly_tag must skip
    drafts and return the newest PUBLISHED nightly (by created_at), not the draft."""
    import json
    releases = [
        {"tag_name": "nightly-draft-1", "draft": True,  "created_at": "2026-07-13T08:56:15Z"},
        {"tag_name": "nightly-newer-2", "draft": False, "created_at": "2026-07-13T11:57:02Z"},
        {"tag_name": "nightly-older-3", "draft": False, "created_at": "2026-07-13T07:49:50Z"},
        {"tag_name": "v1.0.0",          "draft": False, "created_at": "2026-04-13T18:24:16Z"},
    ]

    class _R:
        stdout = json.dumps(releases)
    monkeypatch.setattr(flasher, "_run", lambda *a, **k: _R())
    assert flasher.latest_nightly_tag("gh") == "nightly-newer-2"


def test_latest_nightly_tag_none_for_only_drafts_or_bad_json(monkeypatch):
    import json

    class _OnlyDraft:
        stdout = json.dumps([{"tag_name": "nightly-d", "draft": True, "created_at": "z"}])
    monkeypatch.setattr(flasher, "_run", lambda *a, **k: _OnlyDraft())
    assert flasher.latest_nightly_tag("gh") is None

    class _Bad:
        stdout = "not json at all"
    monkeypatch.setattr(flasher, "_run", lambda *a, **k: _Bad())
    assert flasher.latest_nightly_tag("gh") is None


def _mock_flash_boundaries(monkeypatch, parts):
    """Stub every side-effecting boundary flash() touches so only its
    download/write orchestration runs (no gh, no device, no PowerShell)."""
    monkeypatch.setattr(flasher, "IS_WIN", False)
    monkeypatch.setattr(flasher, "find_gh", lambda: "gh")
    monkeypatch.setattr(flasher, "find_dd", lambda: None)
    monkeypatch.setattr(flasher, "list_parts", lambda *a, **k: parts)
    monkeypatch.setattr(flasher, "verify_iso", lambda *a, **k: True)
    monkeypatch.setattr(flasher, "full_verify", lambda *a, **k: True)
    monkeypatch.setattr(flasher, "record_part_hash", lambda *a, **k: None)
    monkeypatch.setattr(flasher, "sha256_file", lambda *a, **k: "d" * 64)


def test_jobs_parallel_prefetch_is_concurrent_and_writes_in_order(monkeypatch, tmp_path):
    """--jobs N (download mode) fetches parts CONCURRENTLY, yet writes them to the
    device strictly in ascending offset order (concurrent raw writes are unsafe)."""
    import threading
    import time
    parts = [{"name": "iso.part-0%d" % i, "id": i, "size": 100} for i in range(4)]
    _mock_flash_boundaries(monkeypatch, parts)

    lock = threading.Lock()
    live = {"now": 0, "max": 0}
    downloaded = []

    def fake_download(gh, tag, name, tmp, size, log, tries=4):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.05)                       # widen the overlap window
        with lock:
            live["now"] -= 1
            downloaded.append(name)
        pth = os.path.join(str(tmp_path), name)
        with open(pth, "wb") as f:
            f.write(b"\0" * size)
        return pth
    monkeypatch.setattr(flasher, "download_part", fake_download)

    writes = []
    monkeypatch.setattr(flasher, "write_source_to_device",
                        lambda disk, src, off, dd, log, writer=None: (
                            writes.append(off) or os.path.getsize(src)))

    disk = {"number": 2, "dev": "/dev/sdx", "physdrive": r"\\.\PhysicalDrive2",
            "model": "SanDisk", "size": 1 << 30}
    ok = flasher.flash("tag", "desktop", disk, "download", str(tmp_path),
                       jobs=3, log=lambda m: None)
    assert ok is True
    assert sorted(downloaded) == sorted(p["name"] for p in parts)  # every part fetched
    assert writes == [0, 100, 200, 300]                            # serial + in order
    assert live["max"] >= 2                                        # downloads overlapped


def test_jobs_default_is_strictly_serial(monkeypatch, tmp_path):
    """--jobs 1 (the default) keeps the plain serial path: never more than one
    download in flight, no thread pool."""
    import threading
    import time
    parts = [{"name": "iso.part-0%d" % i, "id": i, "size": 100} for i in range(3)]
    _mock_flash_boundaries(monkeypatch, parts)

    lock = threading.Lock()
    live = {"now": 0, "max": 0}

    def fake_download(gh, tag, name, tmp, size, log, tries=4):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.02)
        with lock:
            live["now"] -= 1
        pth = os.path.join(str(tmp_path), name)
        with open(pth, "wb") as f:
            f.write(b"\0" * size)
        return pth
    monkeypatch.setattr(flasher, "download_part", fake_download)
    monkeypatch.setattr(flasher, "write_source_to_device",
                        lambda disk, src, off, dd, log, writer=None: os.path.getsize(src))

    disk = {"number": 2, "dev": "/dev/sdx", "physdrive": "x", "model": "X", "size": 1 << 30}
    ok = flasher.flash("tag", "desktop", disk, "download", str(tmp_path),
                       jobs=1, log=lambda m: None)
    assert ok is True
    assert live["max"] == 1                                        # strictly serial
