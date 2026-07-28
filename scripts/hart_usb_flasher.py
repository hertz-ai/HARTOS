#!/usr/bin/env python3
"""HART OS USB Flasher — write a multi-part HART OS ISO release to a USB stick.

Two modes:
  download : fetch each part to a temp dir (resumable, size-verified), write it
             to the device at its byte offset, delete it. Robust; needs ~2 GB
             scratch at a time. THE DEFAULT.
  stream   : pipe each part straight to the device (no scratch space), with a
             byte-count verify + whole-part retry. Use when disk is tight.

Two front-ends:
  CLI  (silent / scriptable):  hart_usb_flasher.py --tag <tag> --device <id> \
                                   --mode download --yes
  GUI  (interactive):          hart_usb_flasher.py --gui   (default if no args)

Safety:
  * Only USB / removable disks are offered by default (``--list`` to see them).
    Writing a non-removable disk requires the explicit ``--allow-system`` flag
    AND ``--yes`` — the GUI never offers system disks.
  * The GUI shows a destructive-write warning naming the disk model + size and
    requires confirmation. The CLI requires ``--yes`` (otherwise it refuses).
  * After writing, the ISO9660 ``CD001`` magic and the ``0x55AA`` boot signature
    are read back off the device and asserted.

This file is self-contained (std-lib + the GitHub CLI ``gh`` for asset access)
and is published as a per-release asset by .github/workflows/release.yml so any
contributor can re-flash without bespoke commands. It is cross-platform-aware
(Windows / Linux / macOS) but is exercised on Windows first.

Layout of a release's desktop ISO: parts 00..02 are 1900 MiB each, part 03 is
the remainder; concatenated they are the 7,030,001,664-byte ISO. Each part is
written at the cumulative byte offset of the parts before it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import platform
import shutil
import subprocess
import sys
import time

REPO = os.environ.get("HART_REPO", "hertz-ai/HARTOS")
IS_WIN = platform.system() == "Windows"
SECTOR = 512
CHUNK = 4 * 1024 * 1024  # 4 MiB write chunk (sector-aligned)


# ───────────────────────── small helpers ─────────────────────────
def _run(cmd, **kw):
    """Run a command, return CompletedProcess (text). Never raises on non-zero."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def find_gh():
    # Prefer the known-good install path on Windows: a stale `gh` (e.g. an old
    # miniconda shim) frequently shadows the real CLI on PATH.
    if IS_WIN and os.path.exists(r"C:\Program Files\GitHub CLI\gh.exe"):
        return r"C:\Program Files\GitHub CLI\gh.exe"
    return shutil.which("gh")


def find_dd():
    for c in ("dd", r"C:\Program Files\Git\usr\bin\dd.exe"):
        p = shutil.which(c) if os.sep not in c else (c if os.path.exists(c) else None)
        if p:
            return p
    return None


def default_tmp():
    """Scratch dir. Prefer D:\\ on Windows (more headroom), else system temp."""
    if IS_WIN and os.path.isdir("D:\\"):
        return r"D:\hart_flash_tmp"
    return os.path.join(os.environ.get("TMPDIR", "/var/tmp"), "hart_flash_tmp")


# ───────────────────────── disk enumeration ─────────────────────────
def list_disks():
    """Return [{number, model, size, bus, removable, system, dev}] cross-platform."""
    if IS_WIN:
        return _list_disks_windows()
    if platform.system() == "Linux":
        return _list_disks_linux()
    if platform.system() == "Darwin":
        return _list_disks_macos()
    return []


def _list_disks_windows():
    """Enumerate disks via Get-Disk, but with a hard subprocess TIMEOUT and a
    diskpart fallback. The Windows Storage/PnP/WMI stack can WEDGE under heavy
    WSL2/Hyper-V/QEMU load — `Get-Disk` then hangs 15+ minutes. diskpart is a
    native tool that does NOT touch the wedged WMI path, so it still sees the
    disks. See memory/reference_windows_usb_wedge_pnputil_reset.md."""
    ps = (
        "Get-Disk | ForEach-Object { [pscustomobject]@{ "
        "Number=$_.Number; Model=$_.FriendlyName; Size=[int64]$_.Size; "
        "Serial=[string]$_.SerialNumber; "
        "Bus=[string]$_.BusType; Boot=[bool]$_.IsBoot; System=[bool]$_.IsSystem } } "
        "| ConvertTo-Json -Compress"
    )
    try:
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                 timeout=12)
        out = (r.stdout or "").strip()
        if not out:
            return _list_disks_windows_diskpart()
        data = json.loads(out)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        # WMI/Storage wedged or returned garbage — diskpart is native + fast.
        return _list_disks_windows_diskpart()
    if isinstance(data, dict):
        data = [data]
    disks = []
    for d in data:
        n = d["Number"]
        bus = (d.get("Bus") or "").upper()
        disks.append({
            "number": n,
            "model": (d.get("Model") or "?").strip(),
            "size": int(d.get("Size") or 0),
            # Stable identity. "number"/"dev"/"physdrive" are POSITIONAL: unplug
            # and replug a stick, or swap its port, and PhysicalDriveN can name a
            # different disk. assert_device_identity() re-checks this before any
            # write so a re-enumeration between choosing and writing cannot send
            # an image to the wrong device.
            "serial": (d.get("Serial") or "").strip(),
            "bus": bus,
            "removable": bus == "USB",
            "system": bool(d.get("System") or d.get("Boot")),
            # Cygwin/MSYS maps PhysicalDriveN -> /dev/sd{a+N}
            "dev": "/dev/sd%c" % chr(ord("a") + int(n)),
            "physdrive": r"\\.\PhysicalDrive%d" % int(n),
        })
    return disks


def _diskpart_script(lines):
    """Run a diskpart script (list of commands) with a timeout, return stdout.
    diskpart bypasses the wedge-prone WMI/Storage enumeration path."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        r = subprocess.run(["diskpart", "/s", path],
                           capture_output=True, text=True, timeout=30)
        return r.stdout or ""
    except (subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _parse_diskpart_size(size_str):
    """'28 GB' / '953 GB' / '3072 KB' -> bytes (int)."""
    parts = size_str.split()
    if len(parts) < 2:
        return 0
    try:
        val = float(parts[0])
    except ValueError:
        return 0
    unit = parts[1].upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 0)
    return int(val * mult)


def _list_disks_windows_diskpart():
    """Diskpart-based disk enumeration — the native fallback when Get-Disk hangs.

    Parses `list disk` for the disk numbers + sizes, then `select disk N` +
    `detail disk` per disk for the bus `Type :` (USB => removable) and the
    `Boot Disk :` line (Yes => system). Returns the SAME dict shape as
    `_list_disks_windows`."""
    import re
    out = _diskpart_script(["list disk"])
    disks = []
    # Lines look like:  "  Disk 0    Online          953 GB  2048 KB        *"
    for line in out.splitlines():
        m = re.match(r"\s*Disk\s+(\d+)\s+\S+\s+(\d+\s*[KMGT]?B)", line)
        if not m:
            continue
        n = int(m.group(1))
        size = _parse_diskpart_size(m.group(2))
        detail = _diskpart_script(["select disk %d" % n, "detail disk"])
        bus, model, system = _parse_diskpart_detail(detail)
        disks.append({
            "number": n,
            "model": model or "?",
            "size": size,
            "bus": bus,
            "removable": bus == "USB",
            "system": system,
            "dev": "/dev/sd%c" % chr(ord("a") + n),
            "physdrive": r"\\.\PhysicalDrive%d" % n,
        })
    return disks


def _parse_diskpart_detail(detail):
    """From `detail disk` output extract (bus, model, system).

    The model name is the first non-empty, non-`Key : value`, non-`Disk ID:`
    line after the header. `Type : USB` => bus 'USB'. `Boot Disk : Yes`
    (or `Pagefile Disk : Yes`) => system True — INCLUDING USB-bus disks: on a
    machine booted FROM a USB stick that stick IS the live boot/pagefile medium
    and MUST stay flagged + excluded from the default write offer (else the
    diskpart fallback would offer the live boot disk as a writable target — the
    exact wrong-disk catastrophe the safety layer exists to prevent). This
    matches the Get-Disk path, which honours IsSystem/IsBoot regardless of bus."""
    bus, model, system = "", "", False
    for raw in detail.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("type") and ":" in line:
            bus = line.split(":", 1)[1].strip().upper()
        elif low.startswith("boot disk") and ":" in line:
            system = system or line.split(":", 1)[1].strip().lower().startswith("yes")
        elif low.startswith("pagefile disk") and ":" in line:
            system = system or line.split(":", 1)[1].strip().lower().startswith("yes")
        elif (not model and ":" not in line
              and not low.startswith("microsoft diskpart")
              and not low.startswith("copyright")
              and not low.startswith("on computer")
              and not low.startswith("disk ")
              and not low.startswith("volume ")
              and not low.startswith("-")):
            model = line
    return bus, model, system


def _windows_usb_host_controllers():
    """Return the USB host-controller PCI Instance IDs from pnputil. These are
    the `PCI\\VEN_...` xHCI eXtensible-Host-Controller entries — restarting them
    re-enumerates the whole USB bus without a reboot. pnputil is native and does
    NOT use the wedge-prone WMI path."""
    try:
        r = subprocess.run(
            ["pnputil", "/enum-devices", "/connected", "/class", "USB"],
            capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return []
    ids = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("Instance ID:"):
            inst = line.split(":", 1)[1].strip()
            # Only the PCI host controllers — NOT the hubs/devices hanging off
            # them. Restarting a hub/Root-Hub hangs on a wedged stack; the PCI
            # xHCI controller restart is the safe reboot-equivalent reset.
            if inst.upper().startswith("PCI\\"):
                ids.append(inst)
    return ids


def _windows_usb_self_heal(log=None):
    """Un-wedge a hung Windows USB stack WITHOUT a reboot: restart each USB PCI
    host controller via pnputil (re-enumerates the bus), then wait for the
    devices to settle. Native path — works where Get-Disk/WMI hangs. See
    memory/reference_windows_usb_wedge_pnputil_reset.md."""
    log = log or (lambda m: print(m, flush=True))
    log("USB stack looked wedged — resetting the controllers, re-scanning…")
    controllers = _windows_usb_host_controllers()
    if not controllers:
        log("  no USB host controllers found to restart (pnputil empty/timed out)")
        return False
    restarted = 0
    for inst in controllers:
        try:
            r = subprocess.run(["pnputil", "/restart-device", inst],
                               capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0 or "restart" in (r.stdout or "").lower()
            log("  restart %s: %s" % (inst, "OK" if ok else "(no change)"))
            restarted += 1 if ok else 0
        except (subprocess.TimeoutExpired, OSError) as e:
            log("  restart %s: skipped (%s)" % (inst, e))
    time.sleep(4)  # let the bus re-enumerate before we re-scan
    return restarted > 0


def _list_disks_linux():
    r = _run(["lsblk", "-dbJ", "-o", "NAME,MODEL,SIZE,RM,TYPE,RO"])
    try:
        data = json.loads(r.stdout)
    except Exception:
        return []
    disks = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        disks.append({
            "number": d["name"], "model": (d.get("model") or "?").strip(),
            "size": int(d.get("size") or 0), "bus": "",
            "removable": str(d.get("rm")) in ("1", "True", "true"),
            "system": False, "dev": "/dev/" + d["name"], "physdrive": "/dev/" + d["name"],
        })
    return disks


def _list_disks_macos():
    r = _run(["diskutil", "list", "-plist", "physical"])
    # Minimal: parse `diskutil info` per disk would be heavier; keep simple.
    disks = []
    for line in _run(["diskutil", "list"]).stdout.splitlines():
        line = line.strip()
        if line.startswith("/dev/disk") and "external" in line:
            name = line.split()[0]
            disks.append({"number": name, "model": "external", "size": 0,
                          "bus": "USB", "removable": True, "system": False,
                          "dev": name, "physdrive": name})
    return disks


def usb_disks(disks):
    return [d for d in disks if d["removable"] and not d["system"]]


def list_disks_with_self_heal(allow_system=False, log=None):
    """Enumerate disks; if the candidate set is empty on Windows, the USB stack
    may be wedged (Get-Disk hung / nothing enumerated). Run the pnputil
    controller-restart self-heal ONCE, re-enumerate (the native diskpart path),
    and return the disks. So a user who says "it's plugged in" auto-recovers
    instead of needing a reboot.

    Returns (all_disks, candidate_disks)."""
    log = log or (lambda m: print(m, flush=True))
    disks = list_disks()
    candidates = disks if allow_system else usb_disks(disks)
    if not candidates and IS_WIN:
        if _windows_usb_self_heal(log):
            disks = list_disks()
            candidates = disks if allow_system else usb_disks(disks)
    return disks, candidates


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f PB" % n


# ───────────────────────── release parts ─────────────────────────
def list_parts(gh, tag, variant, image="iso"):
    """Return sorted [{name, id, size}] for <variant> parts of <tag>.

    image='iso' (default) selects the live ISO parts (.iso.part-NN); image='raw'
    selects the INSTALLED raw disk image's compressed parts (.raw.xz.part-NN).
    The suffix match must be EXACT: releases carry both image kinds side by
    side, and the old `".part-" in name` filter would interleave raw parts into
    the ISO list -- mixed offsets writing both images onto one device.

    A missing/unpublished tag makes `gh api` 404 and emit an error object
    ({"message":"Not Found",...}) on stdout instead of asset lines. Guard the
    dict access so that case yields an empty list (=> a clean "no parts" error
    in flash()) rather than a KeyError, and so a malformed line never aborts."""
    part_token = {"iso": ".iso.part-", "raw": ".raw.xz.part-"}[image]
    r = _run([gh, "api", "repos/%s/releases/tags/%s" % (REPO, tag),
              "--jq", ".assets[] | {name:.name, id:.id, size:.size, state:.state}"])
    parts = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            a = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = a.get("name") if isinstance(a, dict) else None
        if name and variant in name and part_token in name:
            parts.append(a)
    parts.sort(key=lambda a: a["name"])
    return parts


def latest_nightly_tag(gh):
    """Newest PUBLISHED nightly tag, or None. GitHub's /releases lists DRAFT
    releases FIRST, so a half-uploaded draft (whose ISO parts may not be
    downloadable yet) would otherwise be picked — drafts are excluded here and
    the remaining published nightlies are sorted newest-first by created_at so
    the result never depends on GitHub's draft-ordering quirk."""
    r = _run([gh, "api", "repos/%s/releases?per_page=20" % REPO])
    try:
        rels = json.loads(r.stdout or "[]")
    except (ValueError, TypeError):
        return None
    nightlies = [x for x in rels
                 if str(x.get("tag_name", "")).startswith("nightly-")
                 and not x.get("draft", False)]
    nightlies.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return nightlies[0].get("tag_name") if nightlies else None


def offsets(parts):
    off, acc = [], 0
    for p in parts:
        off.append(acc)
        acc += p["size"]
    return off, acc


# ───────────────────────── device writing ─────────────────────────
def _dismount_windows(disk):
    """Remove mount points for every volume on the disk so a raw write isn't
    blocked by a held lock (the proven `mountvol <letter> /D` path)."""
    ps = ("Get-Partition -DiskNumber %d -ErrorAction SilentlyContinue | "
          "Get-Volume -ErrorAction SilentlyContinue | "
          "Where-Object DriveLetter | ForEach-Object { $_.DriveLetter }" % disk["number"])
    r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)
    for letter in (r.stdout or "").split():
        letter = letter.strip()
        if letter:
            _run(["cmd", "/c", "mountvol", "%s:" % letter, "/D"], timeout=20)


def _win_automount(enable):
    """Toggle Windows auto-mounting of new volumes (mountvol /E or /N). With it
    disabled, Windows won't re-mount — and protect — the ISO9660 volume as it is
    written, which otherwise fails raw writes with 'Invalid request code'."""
    _run(["cmd", "/c", "mountvol", "/E" if enable else "/N"], timeout=20)


def _win_diskpart_clean(disk_number, log):
    """Wipe the disk's partition table via diskpart `clean`, removing the
    protected partition/volume that makes raw writes fail with 'Invalid request
    code' / 'Permission denied' at ~12 MB (the isohybrid ISO leaves an MBR +
    a tiny unusable EFI volume that Windows protects). The caller already
    confirmed the disk is removable/USB before this runs."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("select disk %d\nclean\n" % disk_number)
        r = subprocess.run(["diskpart", "/s", path], capture_output=True, text=True,
                            timeout=45)
        ok = "succeeded in cleaning" in (r.stdout or "").lower()
        log("  diskpart clean disk %d: %s" %
            (disk_number, "OK" if ok else (r.stdout or r.stderr or "").strip()[-160:]))
        return ok
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _prepare_windows_device(disk, dd, log, clean=True):
    """Release Windows' raw-write protection before flashing: disable automount
    (so the freshly-written ISO isn't re-mounted + re-protected mid-write), drop
    any drive-letter mounts, and wipe the partition table with diskpart `clean`
    so there is no protected partition/volume. Without this, raw writes fail with
    'Invalid request code' / 'Permission denied' at ~12 MB.

    ``clean=False`` (a --start-part RESUME) keeps automount-off + dismount but
    SKIPS diskpart `clean` — the disk already holds the earlier parts, and a
    successful clean would wipe them. The exclusive-handle write still proceeds
    (the earlier parts already broke the ~12 MB wall)."""
    def _do_prepare():
        _win_automount(False)
        _dismount_windows(disk)
        if clean:
            _win_diskpart_clean(disk["number"], log)

    try:
        _do_prepare()
    except subprocess.TimeoutExpired:
        # A wedged Windows VDS (heavy WSL2 / Hyper-V / QEMU / nested-virt host)
        # can hang a diskpart `clean` / Get-Volume step INDEFINITELY. Reset the
        # USB host controllers via pnputil — the SAME reboot-free un-wedge the
        # enumeration path uses — then retry the prepare ONCE.
        log("  device-prepare HUNG (VDS wedged) — pnputil USB self-heal + retry")
        _windows_usb_self_heal(log)
        try:
            _do_prepare()
        except subprocess.TimeoutExpired:
            log("  device-prepare STILL hung after the USB reset — continuing to the "
                "raw write anyway (prepare is best-effort: the exclusive-handle retry "
                "loop + the ISO image overwrite the partition table)")


class _WinExclusiveWriter:
    """Holds an EXCLUSIVE handle (FILE_SHARE_NONE) to the physical drive for the
    WHOLE flash. After diskpart `clean` there are no mounted volumes, so Windows
    grants exclusive access; holding it stops Windows from re-scanning and
    re-protecting the isohybrid partition table written mid-flash — which is what
    walls a *shared* dd write at ~12 MB. Writes are sector-aligned: the part
    offsets are MiB-aligned and the part sizes are 512 B multiples."""

    def __init__(self, disk_number):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.k = k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
        k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                       ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.invalid = wintypes.HANDLE(-1).value
        # GENERIC_READ|WRITE, FILE_SHARE_NONE (exclusive), OPEN_EXISTING.
        # Right after diskpart `clean` (or when a clean FAILED and Windows is
        # mid-rescan of the partition table) the volume manager briefly holds
        # the drive, so the first exclusive open loses with ERROR_SHARING_
        # VIOLATION (32) / ERROR_NOT_READY (21). The grip clears within a few
        # seconds — retry with backoff instead of aborting the whole flash.
        # (Verified: a bare exclusive open succeeds seconds after the flasher's
        # first attempt failed err 32 on a Cruzer Blade whose `clean` was
        # refused with "Incorrect function".)
        _TRANSIENT = (32, 21, 5)        # SHARING_VIOLATION, NOT_READY, ACCESS_DENIED
        last_err = 0
        for attempt in range(6):
            self.h = k.CreateFileW(r"\\.\PhysicalDrive%d" % disk_number,
                                   0xC0000000, 0, None, 3, 0, None)
            if self.h != self.invalid:
                break
            last_err = ctypes.get_last_error()
            if last_err not in _TRANSIENT:
                break
            time.sleep(2 + attempt)     # 2,3,4,5,6s — let the volume manager release
        if self.h == self.invalid:
            raise RuntimeError("exclusive open of PhysicalDrive%d failed (err %d) — "
                               "is the disk still mounted? (a re-plug or reboot "
                               "resets a stick whose controller refuses writes)"
                               % (disk_number, last_err))

    def write_at(self, byte_offset, fobj):
        ctypes, wintypes, k = self.ctypes, self.wintypes, self.k
        if not k.SetFilePointerEx(self.h, byte_offset, None, 0):       # FILE_BEGIN
            raise RuntimeError("seek to %d failed (err %d)"
                               % (byte_offset, ctypes.get_last_error()))
        total, written = 0, wintypes.DWORD()
        while True:
            buf = fobj.read(CHUNK)
            if not buf:
                break
            if len(buf) % SECTOR:                          # pad a final partial sector
                buf += b"\x00" * (SECTOR - len(buf) % SECTOR)
            if not k.WriteFile(self.h, buf, len(buf), ctypes.byref(written), None):
                raise RuntimeError("write at %d failed (err %d)"
                                   % (byte_offset + total, ctypes.get_last_error()))
            total += written.value
        return total

    def close(self):
        try:
            self.k.CloseHandle(self.h)
        except Exception:
            pass


class DeviceIdentityChanged(RuntimeError):
    """The disk at the chosen index is no longer the disk that was chosen."""


def assert_device_identity(disk, log=None):
    """Re-enumerate and refuse to write if the target is no longer the same disk.

    The device handles this script writes to -- PhysicalDriveN on Windows,
    /dev/sdX on Linux -- are POSITIONAL. They name a slot in an enumeration
    order, not a physical stick. Unplug and replug a device, or move it to
    another port, and the same handle can resolve to a completely different
    disk, including one holding data.

    Selection and writing are separated by download time, a confirmation
    prompt, and sometimes minutes of a human walking to the machine, so the
    binding made at selection is exactly the kind of stale reference that gets
    acted on. An agent driving this is worse off than a person: it cannot see
    the user swap the stick, so nothing tells it the handle went stale.

    So the identity is re-checked at the last moment, against fields that
    survive re-enumeration (serial where the platform gives one, otherwise
    size + model). A mismatch raises rather than writes, because the failure
    mode being prevented is destroying the wrong disk.

    Best-effort by design: if re-enumeration itself fails, that is logged and
    the write proceeds, since refusing every write because an enumeration
    command was slow would be its own outage.
    """
    want_n = disk.get("number")
    if want_n is None:
        return
    try:
        current = {d.get("number"): d for d in list_disks()}
    except Exception as e:                      # enumeration unavailable
        if log:
            log(f"  identity re-check SKIPPED (enumeration failed: {e})")
        return
    now = current.get(want_n)
    if now is None:
        raise DeviceIdentityChanged(
            f"disk {want_n} ({disk.get('model')}, {disk.get('size')} bytes) is "
            f"GONE from the device list. It was unplugged or re-enumerated. "
            f"Refusing to write; re-select the target."
        )
    # Serial is the real identity. Fall back to size+model only where the
    # platform reports no serial, which is weaker but still catches the common
    # swap (two different sticks are rarely byte-identical in capacity).
    was_serial, now_serial = (disk.get("serial") or ""), (now.get("serial") or "")
    if was_serial and now_serial:
        if was_serial != now_serial:
            raise DeviceIdentityChanged(
                f"disk {want_n} is now serial {now_serial!r}, not {was_serial!r}. "
                f"A different device is in that slot. Refusing to write."
            )
    elif (now.get("size"), now.get("model")) != (disk.get("size"), disk.get("model")):
        raise DeviceIdentityChanged(
            f"disk {want_n} is now {now.get('model')!r} at {now.get('size')} bytes, "
            f"was {disk.get('model')!r} at {disk.get('size')} bytes. "
            f"Refusing to write."
        )
    if now.get("system") and not disk.get("system"):
        raise DeviceIdentityChanged(
            f"disk {want_n} now reports as a SYSTEM disk. Refusing to write."
        )
    if log:
        log(f"  identity re-checked: disk {want_n} is still "
            f"{now.get('model')} ({now_serial or 'no serial'})")


def write_source_to_device(disk, src_path, byte_offset, dd, log, writer=None):
    """Write a local file to the device at byte_offset (download mode)."""
    assert_device_identity(disk, log)
    if writer is not None:                          # Windows exclusive-handle path
        with open(src_path, "rb") as fobj:
            return writer.write_at(byte_offset, fobj)
    if dd:
        cmd = [dd, "if=%s" % _posix(src_path), "of=%s" % disk["dev"],
               "bs=4M", "oflag=seek_bytes", "seek=%d" % byte_offset, "conv=notrunc"]
        r = _run(cmd)
        written = _dd_bytes(r.stderr)
        return written
    return _py_write(disk["physdrive"], byte_offset, open(src_path, "rb"), log)


def stream_to_device(disk, byte_offset, producer_cmd, dd, log, writer=None):
    """Stream a producer (curl/gh) straight to the device (stream mode)."""
    assert_device_identity(disk, log)
    if writer is not None:                          # Windows exclusive-handle path
        p = subprocess.Popen(producer_cmd, stdout=subprocess.PIPE)
        try:
            return writer.write_at(byte_offset, p.stdout)
        finally:
            p.stdout.close()
            p.wait()
    if not dd:
        raise RuntimeError("stream mode needs `dd`; use --mode download instead")
    p = subprocess.Popen(producer_cmd, stdout=subprocess.PIPE)
    ddp = subprocess.Popen(
        [dd, "of=%s" % disk["dev"], "bs=4M", "oflag=seek_bytes",
         "seek=%d" % byte_offset, "conv=notrunc"],
        stdin=p.stdout, stderr=subprocess.PIPE, text=True)
    p.stdout.close()
    _, err = ddp.communicate()
    p.wait()
    return _dd_bytes(err)


def _py_write(physdrive, byte_offset, fobj, log):
    """Pure-Python sector-aligned raw write fallback (no dd). Windows/Linux."""
    written = 0
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    fd = os.open(physdrive, flags)
    try:
        os.lseek(fd, byte_offset, os.SEEK_SET)
        while True:
            buf = fobj.read(CHUNK)
            if not buf:
                break
            if len(buf) % SECTOR:  # pad final partial sector
                buf += b"\x00" * (SECTOR - len(buf) % SECTOR)
            os.write(fd, buf)
            written += len(buf)
    finally:
        os.close(fd)
        fobj.close()
    return written


def _posix(path):
    """C:\\x -> /c/x for MSYS dd."""
    if IS_WIN and len(path) > 2 and path[1] == ":":
        return "/%s%s" % (path[0].lower(), path[2:].replace("\\", "/"))
    return path


def _dd_bytes(stderr):
    for line in (stderr or "").splitlines():
        if "bytes" in line and "copied" in line:
            try:
                return int(line.split()[0])
            except Exception:
                pass
    return -1


# ───────────────────────── download / stream a part ─────────────────────────
def download_part(gh, tag, name, tmp, want_size, log, tries=4):
    os.makedirs(tmp, exist_ok=True)
    dest = os.path.join(tmp, name)
    if os.path.exists(dest) and os.path.getsize(dest) == want_size:
        log("  %s already downloaded (%d bytes), reusing" % (name, want_size))
        return dest
    for t in range(1, tries + 1):
        r = _run([gh, "release", "download", tag, "--repo", REPO,
                  "--pattern", name, "--dir", tmp, "--clobber"])
        got = os.path.getsize(dest) if os.path.exists(dest) else 0
        if got == want_size:
            return dest
        log("  %s download try %d: got %s want %s%s" %
            (name, t, got, want_size, " — " + (r.stderr or "").strip()[:120] if r.stderr else ""))
        time.sleep(2)
    raise RuntimeError("could not download %s (%s/%s bytes)" % (name, got, want_size))


def stream_producer(gh, asset_id):
    """A curl command that streams an asset with the gh token (resilient --retry)."""
    tok = _run([gh, "auth", "token"]).stdout.strip()
    url = "https://api.github.com/repos/%s/releases/assets/%d" % (REPO, asset_id)
    curl = shutil.which("curl") or "curl"
    return [curl, "-sL", "--retry", "5", "--retry-delay", "2", "--fail",
            "-H", "Authorization: token %s" % tok,
            "-H", "Accept: application/octet-stream", url]


# ───────────────────────── verification ─────────────────────────
# The on-disk boot-image contract — ONE source of truth shared by the pre-write
# verify, the post-carve re-verify, and the GPT-relocate non-destruction proof.
# A correctly written HART OS isohybrid carries the ISO9660 ``CD001`` magic at
# 0x8001 (the primary volume descriptor, LBA 16 in 2048 B sectors) and the
# protective-MBR ``0x55AA`` boot signature at 0x1FE (the last two bytes of LBA 0).
# The #128 GPT relocate writes ONLY LBA 1 + the device tail, so BOTH of these
# bytes must survive the carve byte-for-byte; the re-verify proves it.
ISO9660_MAGIC_OFFSET = 0x8001
ISO9660_MAGIC = b"CD001"
BOOT_SIG_OFFSET = 0x1FE
BOOT_SIG = b"\x55\xAA"


def read_at(dev_or_phys, offset, n, dd):
    if dd:
        # Binary capture (no text decode) so the 0x55AA boot sig isn't mangled.
        r = subprocess.run([dd, "if=%s" % dev_or_phys, "bs=1", "skip=%d" % offset,
                            "count=%d" % n, "status=none"], capture_output=True)
        return r.stdout[:n]
    fd = os.open(dev_or_phys, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        base = (offset // SECTOR) * SECTOR
        os.lseek(fd, base, os.SEEK_SET)
        data = os.read(fd, ((n + (offset - base)) // SECTOR + 1) * SECTOR)
        return data[offset - base: offset - base + n]
    finally:
        os.close(fd)


def verify_iso(disk, dd, log):
    dev = disk["dev"] if dd else disk["physdrive"]
    cd = read_at(dev, ISO9660_MAGIC_OFFSET, len(ISO9660_MAGIC), dd)
    boot = read_at(dev, BOOT_SIG_OFFSET, len(BOOT_SIG), dd)
    ok_cd = cd == ISO9660_MAGIC
    ok_boot = boot == BOOT_SIG
    log("  ISO9660 @0x8001: %r %s" % (cd, "OK" if ok_cd else "FAIL"))
    log("  boot sig @0x1FE: %r %s" % (boot, "OK" if ok_boot else "FAIL"))
    return ok_cd and ok_boot


# ── Full sha256 read-back verify (BUILT-IN, resume-safe, no side scripts) ──
# verify_iso only checks the two boot-signature bytes. This proves EVERY byte on the
# device matches the source: each part's sha256 is recorded (from the file we write)
# to a sidecar in the work dir, then after the write each part's region is read back
# off the raw device and compared. The sidecar survives --start-part resume passes
# (which delete each part after a good write), so a multi-pass flash still verifies
# end-to-end with no bespoke script and no reference-dir edits.
def sha256_file(path, chunk=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def sha256_device_region(dev_or_phys, offset, size, dd, chunk=4 * 1024 * 1024):
    """sha256 of `size` bytes read back off the raw device at `offset`. Offsets are
    MiB-aligned and sizes are 512 B multiples, so raw sector-aligned reads are valid."""
    h = hashlib.sha256()
    if dd:
        p = subprocess.Popen(
            [dd, "if=%s" % dev_or_phys, "bs=%d" % chunk, "iflag=skip_bytes,count_bytes",
             "skip=%d" % offset, "count=%d" % size, "status=none"], stdout=subprocess.PIPE)
        for b in iter(lambda: p.stdout.read(chunk), b""):
            h.update(b)
        p.wait()
        return h.hexdigest()
    # Builtin open() (not os.open) -- it handles Windows \\.\PhysicalDriveN device
    # namespaces; reads are sector-aligned (MiB-aligned offset, 512 B-multiple sizes).
    with open(dev_or_phys, "rb") as fh:
        fh.seek(offset)
        remaining = size
        while remaining > 0:
            b = fh.read(min(chunk, remaining))
            if not b:
                break
            h.update(b)
            remaining -= len(b)
    return h.hexdigest()


def _verify_sidecar(tmp):
    return os.path.join(tmp, ".hart-flash-verify.json")


def record_part_hash(tmp, name, offset, size, sha, log=None):
    """Persist a written part's {offset,size,sha256} so the post-write full verify can
    read that region back and compare -- survives --start-part resume (parts are
    deleted after a good write). Best-effort: a sidecar hiccup never fails the flash."""
    path = _verify_sidecar(tmp)
    try:
        recs = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                recs = json.load(f)
        recs[name] = {"offset": offset, "size": size, "sha256": sha}
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(recs, f)
        os.replace(path + ".tmp", path)
    except (OSError, ValueError) as e:
        if log:
            log("  (verify record skipped: %s)" % e)


def full_verify(disk, parts, tmp, dd, log):
    """Read every written part's region back off the device and sha256-compare it to
    the hash recorded at write time. Returns True (all match), False (a mismatch), or
    None (incomplete record -> could not verify; e.g. stream mode or a partial flash)."""
    path = _verify_sidecar(tmp)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            recs = json.load(f)
    except (OSError, ValueError):
        return None
    names = [p["name"] for p in parts]
    if not all(n in recs for n in names):
        return None
    dev = disk["dev"] if dd else disk["physdrive"]
    allok = True
    for n in names:
        r = recs[n]
        got = sha256_device_region(dev, r["offset"], r["size"], dd)
        ok = (got == r["sha256"])
        log("  verify %-45s @ %d: %s" % (n, r["offset"], "OK" if ok else "MISMATCH"))
        allok = allok and ok
    if allok:
        try:
            os.remove(path)
        except OSError:
            pass
    return allok


def reverify_boot_sigs_after_carve(disk, dd, log, tries=5):
    """Re-read the boot signatures AFTER the HARTLOG carve to PROVE the carve did
    not clobber the boot image. The carve (GPT relocate + diskpart create/format)
    is non-destructive by construction — the relocate writes only LBA 1 + the
    device tail, and diskpart only touches the freed tail — so this is the belt-
    and-suspenders runtime proof of that invariant.

    Tri-state so a transient read never gets mistaken for corruption:
      True  — both signatures are present on a clean read (carve was safe).
      False — a signature is DEFINITIVELY changed/absent on a clean read (the
              carve corrupted the boot image; the stick is suspect → re-flash).
      None  — the device could not be read (busy / "device not ready" right after
              a diskpart format) → INDETERMINATE; the caller must NOT treat this
              as corruption (no false brick claim).

    Retries the post-format Windows transient (ERROR_SHARING_VIOLATION 32 /
    ERROR_NOT_READY 21 / ERROR_ACCESS_DENIED 5) and a short read with the same
    backoff the GPT-relocate open uses, so a slow stick settling after the format
    resolves to a real verdict rather than a spurious failure."""
    dev = disk["dev"] if dd else disk["physdrive"]
    last = None
    for attempt in range(tries):
        try:
            cd = read_at(dev, ISO9660_MAGIC_OFFSET, len(ISO9660_MAGIC), dd)
            boot = read_at(dev, BOOT_SIG_OFFSET, len(BOOT_SIG), dd)
        except OSError as e:
            last = e
            if getattr(e, "winerror", None) in (32, 21, 5) or not IS_WIN:
                time.sleep(1 + attempt)
                continue
            break
        if len(cd) < len(ISO9660_MAGIC) or len(boot) < len(BOOT_SIG):
            last = "short read (device not settled)"
            time.sleep(1 + attempt)
            continue
        ok_cd = cd == ISO9660_MAGIC
        ok_boot = boot == BOOT_SIG
        log("  post-carve ISO9660 @0x8001: %r %s" % (cd, "OK" if ok_cd else "CHANGED"))
        log("  post-carve boot sig @0x1FE: %r %s" % (boot, "OK" if ok_boot else "CHANGED"))
        return ok_cd and ok_boot
    log("  post-carve re-verify: could not read the device to re-check (%s) - "
        "indeterminate, NOT treating as corruption" % last)
    return None


# ───────────────────────── HARTLOG diagnostic-log partition ─────────────────────────
# The desktop live ISO uses ~6.6 GB of a 28 GB stick — ~21 GB is free. After a
# successful flash we carve that free space into ONE FAT32 partition labelled
# HARTLOG. The HART OS boot-log module (nixos/modules/hart-boot-log.nix) detects
# that label at boot and writes the boot journal + tier-supervisor state + GTK4/
# GL diagnostics to it, so the Windows host can read the boot journal off the
# stick (no TTY hand-copy). The label HARTLOG is the ONE source of truth shared
# with that module (hart.bootLog.label).
LOG_PART_LABEL = "HARTLOG"

# HARTSTATE is the OPT-IN persistence partition carved from the SAME free tail by
# the SAME mechanism (label + fs are the only knobs that differ). The live OS
# bind-persists wifi/settings/home onto whatever partition carries this label, so a
# live USB survives reboots. ext4 is preferred (Unix permissions + symlinks + no
# 4 GiB file cap that a home dir needs); the Windows diskpart path cannot create a
# Linux-native fs, so there it falls back to what the carve has always used (FAT32)
# and the live OS reformats HARTSTATE to ext4 on first boot before it binds onto it.
STATE_PART_LABEL = "HARTSTATE"


def create_log_partition(disk, log, iso_bytes=0, label=LOG_PART_LABEL, fs="fat32"):
    """Carve a labelled partition into the stick's FREE SPACE (host-side).

    Defaults reproduce the FAT32 HARTLOG diagnostic-log carve verbatim (every legacy
    2/3-arg caller is unchanged). Pass `label=STATE_PART_LABEL, fs="ext4"` to carve
    the HARTSTATE persistence partition through the EXACT same mechanism + safety
    (GPT relocate, no-free-space skip, never-raises) - one carve path, two labels.

    DISPATCHER: routes to the OS-appropriate backend. This is OFF by default in
    flash() (the Live OS creates HARTLOG itself on first boot, Linux-side, which
    can never corrupt the stick's EFI/GPT). It is the opt-in host-side pre-seed so
    a host can read the boot log off the stick even BEFORE the first boot.

    ROBUST BY CONTRACT: it runs AFTER a successful flash + boot-sig verify and must
    NEVER fail/abort the (already-successful) flash. Every failure path (no free
    space, tool missing/errored, any exception) is caught and logged, and the
    function returns False without raising. The flash is already bootable; the log
    partition is a debug convenience.

      Windows    -> _windows_grow_gpt_to_device_end (the sgdisk -e equivalent:
                    relocate the backup GPT to the device's TRUE last LBA so the
                    trailing free tail a dd-written isohybrid hid becomes visible)
                    THEN _create_log_partition_windows (diskpart carves that tail)
      Linux/macOS-> _create_log_partition_unix (sgdisk: relocate the backup GPT to
                    the true device end, then carve the trailing free tail)

    `iso_bytes` is the exact assembled ISO length (flash() passes `total`). The
    Windows path needs it to relocate the backup GPT correctly; absent it (legacy
    2-arg callers) the Windows relocate is skipped and diskpart runs as before.

    The canonical, OS-independent home for the carve is the Live-OS module
    (nixos/modules/hart-hartlog-create.nix); a Windows-flashed stick is therefore
    still covered on its first boot.
    """
    if IS_WIN:
        # THE #128 fix. A dd-written isohybrid GPT ISO leaves the BACKUP GPT header
        # at the ISO image's last LBA (mid-stick), so the PRIMARY header at LBA1
        # caps LastUsableLBA at the ISO boundary and diskpart sees NO free tail
        # ("not enough usable free space") -> the carve no-ops (the "partition 1 is
        # the WHOLE 29 GB stick" symptom). Relocate the backup GPT to the device's
        # TRUE last LBA first (std-lib, non-destructive: writes ONLY LBA1 + the
        # device tail) so the diskpart carve below sees the revealed free tail.
        if iso_bytes:
            try:
                _windows_grow_gpt_to_device_end(disk, iso_bytes, log)
            except Exception as e:                # never let the relocate fail the flash
                log("  HARTLOG grow: ignored error (%s) - continuing to the diskpart "
                    "carve (flash is complete + bootable)" % e)
        return _create_log_partition_windows(disk, log, label=label, fs=fs)
    return _create_log_partition_unix(disk, log, label=label, fs=fs)


# ── #128: the sgdisk -e equivalent on Windows (pure std-lib GPT relocate) ──
# A dd-written isohybrid GPT ISO writes its BACKUP (secondary) GPT header at the
# ISO image's last LBA (e.g. ~6.55 GB), NOT at the physical end of the stick (e.g.
# ~29 GB). The PRIMARY header at LBA1 therefore advertises LastUsableLBA = the ISO
# boundary, so diskpart sees no usable free space in the multi-GB trailing tail and
# its `create partition` no-ops. The fix mirrors what the Linux/macOS path already
# does with `sgdisk -e`: relocate the backup GPT to the device's TRUE last LBA
# (which rewrites the primary header's LastUsableLBA), revealing the tail, then let
# the existing diskpart carve run. This is a pure std-lib rewrite (struct + zlib)
# so the flasher stays self-contained, and it is split into a pure-logic core
# (_grow_gpt_to_device_end, driven by a seekable handle so a temp-file fixture can
# test it) + the Windows IO wrapper (_windows_grow_gpt_to_device_end).
GPT_SIGNATURE = b"EFI PART"


def _windows_device_size_bytes(disk_number, log):
    """Exact device byte length via IOCTL_DISK_GET_LENGTH_INFO (std-lib ctypes, the
    _WinExclusiveWriter WinDLL pattern). Sector-EXACT — unlike the diskpart
    `list disk` fallback which rounds to whole GB; a rounded size would place the
    relocated backup header off the true device end = an INVALID GPT, so we trust
    ONLY this IOCTL and skip the relocate if it fails. Returns int bytes, or 0 when
    the size could not be determined."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:
        log("  HARTLOG grow: ctypes unavailable (%s)" % e)
        return 0
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
        k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        invalid = wintypes.HANDLE(-1).value
        GENERIC_READ = 0x80000000
        FILE_SHARE_RW = 0x00000001 | 0x00000002        # READ | WRITE
        OPEN_EXISTING = 3
        IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
        h = k.CreateFileW(r"\\.\PhysicalDrive%d" % disk_number, GENERIC_READ,
                          FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h == invalid:
            log("  HARTLOG grow: could not open PhysicalDrive%d for the size query "
                "(err %d)" % (disk_number, ctypes.get_last_error()))
            return 0
        try:
            length = ctypes.c_longlong(0)
            returned = wintypes.DWORD(0)
            ok = k.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                                   ctypes.byref(length), ctypes.sizeof(length),
                                   ctypes.byref(returned), None)
            if not ok:
                log("  HARTLOG grow: IOCTL_DISK_GET_LENGTH_INFO failed (err %d)"
                    % ctypes.get_last_error())
                return 0
            return int(length.value)
        finally:
            k.CloseHandle(h)
    except Exception as e:
        log("  HARTLOG grow: device-size query errored (%s)" % e)
        return 0


def _open_seekable_raw(physdrive, log):
    """Open a raw block device as an UNBUFFERED seekable binary handle (rb+) for the
    GPT relocate. Retries the Windows post-flash transient (the volume manager
    briefly holds the drive: ERROR_SHARING_VIOLATION 32 / ERROR_NOT_READY 21 /
    ERROR_ACCESS_DENIED 5) with the same backoff _WinExclusiveWriter uses. Returns
    the handle, or None on failure (never raises)."""
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    last = None
    for attempt in range(6):
        try:
            fd = os.open(physdrive, flags)
            return os.fdopen(fd, "rb+", buffering=0)
        except OSError as e:
            last = e
            if getattr(e, "winerror", None) not in (32, 21, 5):
                break
            time.sleep(2 + attempt)                    # 2,3,4,5,6s — let the VM release
    log("  HARTLOG grow: could not open %s for the GPT relocate (%s) - skipped "
        "(flash is complete + bootable)" % (physdrive, last))
    return None


def _read_exact(handle, n):
    """Read exactly n bytes from a seekable handle (a raw-device read can return a
    sector-multiple short of the request). Returns what it got at EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = handle.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _pad_to_sector(b):
    """Zero-pad bytes up to the next 512 B boundary (raw block writes are sector-
    aligned). A whole-sector input is returned unchanged."""
    if len(b) % SECTOR:
        return bytes(b) + b"\x00" * (SECTOR - len(b) % SECTOR)
    return bytes(b)


def _set_gpt_header_crc(hdr, header_size):
    """Zero the HeaderCRC32 field, CRC32 the first header_size bytes, store it back
    (the GPT header-checksum protocol). hdr is a mutable bytearray of >= one sector."""
    import struct
    import zlib
    struct.pack_into("<I", hdr, 16, 0)
    crc = zlib.crc32(bytes(hdr[:header_size])) & 0xFFFFFFFF
    struct.pack_into("<I", hdr, 16, crc)


def _grow_gpt_to_device_end(handle, device_sectors, iso_bytes, log):
    """Relocate the backup GPT to the device's TRUE last LBA — the std-lib
    equivalent of `sgdisk -e`. After a dd-written isohybrid GPT ISO the backup GPT
    header sits at the ISO image's last LBA (mid-stick) and the primary header caps
    LastUsableLBA at the ISO boundary, so a diskpart `create partition` sees NO
    trailing free space. This rewrites the primary header's LastUsableLBA +
    AlternateLBA to the real device end and writes a fresh backup GPT (entry array +
    header) at the device tail, revealing the free tail.

    Operates ONLY on a seekable binary `handle` (.seek/.read/.write) so a temp-file
    fixture can drive it (the SRP/testability boundary). It writes ONLY LBA 1 (the
    primary header), the backup entry array, and the backup header at the device
    tail — NEVER LBA 0 (the protective-MBR 0x55AA boot sig) and NEVER the ISO9660
    data (the GPT lives in the iso9660 system area, which ISO9660 ignores; CD001 at
    0x8001 = LBA 64 is untouched). The stale mid-device backup GPT is left as
    harmless data (a reader follows the primary's AlternateLBA to the real one).

    Returns True on a clean relocate (or a no-op when the backup is already at the
    device end), False on a clean skip (no/invalid GPT = an MBR isohybrid, which we
    NEVER convert; the Live OS parted path then carves HARTLOG on first boot).
    Best-effort; the caller also wraps it so it can never fail the already-bootable
    flash.
    """
    import struct
    import zlib

    handle.seek(SECTOR)                              # LBA 1 = primary GPT header
    hdr = bytearray(_read_exact(handle, SECTOR))
    if len(hdr) < 92 or bytes(hdr[0:8]) != GPT_SIGNATURE:
        log("  HARTLOG grow: no GPT signature at LBA1 (MBR isohybrid?) - skipped; "
            "the Live OS parted path carves HARTLOG on first boot")
        return False
    header_size = struct.unpack_from("<I", hdr, 12)[0]
    if header_size < 92 or header_size > SECTOR:
        log("  HARTLOG grow: implausible GPT header size %d - skipped" % header_size)
        return False
    my_lba          = struct.unpack_from("<Q", hdr, 24)[0]
    first_usable    = struct.unpack_from("<Q", hdr, 40)[0]
    last_usable_old = struct.unpack_from("<Q", hdr, 48)[0]
    part_entry_lba  = struct.unpack_from("<Q", hdr, 72)[0]
    num_entries     = struct.unpack_from("<I", hdr, 80)[0]
    entry_size      = struct.unpack_from("<I", hdr, 84)[0]
    array_crc_old   = struct.unpack_from("<I", hdr, 88)[0]
    if my_lba != 1:
        log("  HARTLOG grow: primary MyLBA=%d (expected 1) - skipped" % my_lba)
        return False
    if num_entries == 0 or num_entries > 1024 or entry_size < 128 or entry_size % 8:
        log("  HARTLOG grow: implausible entry array (%d x %d) - skipped"
            % (num_entries, entry_size))
        return False

    array_bytes      = num_entries * entry_size
    array_sectors    = (array_bytes + SECTOR - 1) // SECTOR
    backup_hdr_lba   = device_sectors - 1
    backup_array_lba = backup_hdr_lba - array_sectors
    new_last_usable  = backup_array_lba - 1

    if new_last_usable == last_usable_old:
        log("  HARTLOG grow: backup GPT already at the device end "
            "(last_usable=%d) - no relocate needed" % last_usable_old)
        return True
    if new_last_usable < last_usable_old:
        log("  HARTLOG grow: device end (last_usable %d) is not beyond the current "
            "GPT (%d) - skipped (nothing to reveal)"
            % (new_last_usable, last_usable_old))
        return False
    if backup_array_lba <= first_usable:
        log("  HARTLOG grow: device too small for a backup GPT past the data - skipped")
        return False

    # Read the primary partition entry array.
    handle.seek(part_entry_lba * SECTOR)
    array = bytearray(_read_exact(handle, array_bytes))
    if len(array) < array_bytes:
        log("  HARTLOG grow: short read of the GPT entry array - skipped")
        return False

    # Defensive clamp: cap any USED entry whose EndingLBA runs past the ISO image
    # bound. The build-time GPT already sizes partition 1 to the ISO, so this is
    # normally a no-op — it only guards a malformed table from describing space past
    # the data we just wrote.
    array_changed = False
    iso_last_lba = (iso_bytes // SECTOR - 1) if iso_bytes else 0
    if iso_last_lba > 0:
        zero16 = b"\x00" * 16
        for i in range(num_entries):
            base = i * entry_size
            if bytes(array[base:base + 16]) == zero16:
                continue                              # unused slot
            ending = struct.unpack_from("<Q", array, base + 40)[0]
            if ending > iso_last_lba:
                struct.pack_into("<Q", array, base + 40, iso_last_lba)
                array_changed = True
    array_crc = (zlib.crc32(bytes(array)) & 0xFFFFFFFF) if array_changed else array_crc_old

    # New PRIMARY header (LBA 1): extend AlternateLBA + LastUsableLBA to the device end.
    new_primary = bytearray(hdr)
    struct.pack_into("<Q", new_primary, 32, backup_hdr_lba)     # AlternateLBA
    struct.pack_into("<Q", new_primary, 48, new_last_usable)    # LastUsableLBA
    struct.pack_into("<I", new_primary, 88, array_crc)
    _set_gpt_header_crc(new_primary, header_size)

    # New BACKUP header (LBA device_sectors-1): mirror, with MyLBA/AlternateLBA
    # swapped + PartitionEntryLBA pointing at the tail array.
    new_backup = bytearray(hdr)
    struct.pack_into("<Q", new_backup, 24, backup_hdr_lba)      # MyLBA
    struct.pack_into("<Q", new_backup, 32, 1)                   # AlternateLBA = primary
    struct.pack_into("<Q", new_backup, 48, new_last_usable)     # LastUsableLBA
    struct.pack_into("<Q", new_backup, 72, backup_array_lba)    # PartitionEntryLBA
    struct.pack_into("<I", new_backup, 88, array_crc)
    _set_gpt_header_crc(new_backup, header_size)

    # WRITE: primary header @LBA1, backup entry array + backup header at the tail.
    # If we clamped an entry, the primary array on disk is now stale vs the new CRC,
    # so rewrite it too. Whole-sector writes at sector-aligned offsets ONLY.
    handle.seek(1 * SECTOR)
    handle.write(_pad_to_sector(new_primary))
    handle.seek(backup_array_lba * SECTOR)
    handle.write(_pad_to_sector(array))
    if array_changed:
        handle.seek(part_entry_lba * SECTOR)
        handle.write(_pad_to_sector(array))
    handle.seek(backup_hdr_lba * SECTOR)
    handle.write(_pad_to_sector(new_backup))
    try:
        handle.flush()
    except Exception:
        pass
    log("  HARTLOG grow: relocated the backup GPT to LBA %d (device end); "
        "last_usable %d -> %d - the trailing free tail is now visible to the "
        "diskpart carve" % (backup_hdr_lba, last_usable_old, new_last_usable))
    return True


def _windows_grow_gpt_to_device_end(disk, iso_bytes, log):
    """Windows wrapper for the GPT relocate: get the sector-exact device size (IOCTL),
    open a seekable raw handle (with the post-flash transient backoff), run
    _grow_gpt_to_device_end, fsync + close. Best-effort; NEVER raises — any failure
    is a logged skip (the diskpart carve then no-ops and the Live OS still carves
    HARTLOG on first boot). Returns the relocate result (bool)."""
    number = disk["number"]
    physdrive = disk.get("physdrive") or (r"\\.\PhysicalDrive%d" % number)
    dev_bytes = _windows_device_size_bytes(number, log)
    if dev_bytes <= 0:
        log("  HARTLOG grow: no exact device size - skipped (the diskpart carve will "
            "no-op; the Live OS carves HARTLOG on first boot)")
        return False
    device_sectors = dev_bytes // SECTOR
    iso_sectors = (iso_bytes // SECTOR) if iso_bytes else 0
    if iso_sectors and device_sectors <= iso_sectors + 34:
        log("  HARTLOG grow: device (%d sectors) is not larger than the ISO "
            "(%d sectors) - skipped (no trailing tail to reveal)"
            % (device_sectors, iso_sectors))
        return False
    handle = _open_seekable_raw(physdrive, log)
    if handle is None:
        return False
    try:
        return _grow_gpt_to_device_end(handle, device_sectors, iso_bytes, log)
    finally:
        try:
            handle.flush()
        except Exception:
            pass
        try:
            os.fsync(handle.fileno())
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass


def _create_log_partition_windows(disk, log, label=LOG_PART_LABEL, fs="fat32"):
    """Legacy Windows carve via diskpart. Mirrors the existing diskpart-fallback
    style (the flasher already shells diskpart for enumeration + `clean`). Only
    diskpart can carve a partition into the post-isohybrid free space and format +
    label it so Windows mounts the drive natively. NEVER raises.

    diskpart cannot create a Linux-native fs (ext4), so a HARTSTATE carve
    (fs="ext4") is FAT32-formatted host-side - exactly what this path has always
    used - and the live OS reformats HARTSTATE to ext4 on first boot before it binds
    persistence onto it. FAT32/NTFS/exFAT pass through unchanged.

    NOTE: this path is doubly fragile (it hung on a wedged VDS, and a half-completed
    `create partition` corrupted a freshly-flashed stick's EFI/GPT), which is why it
    is opt-in only and the Live-OS Linux carve is canonical. Kept for operators who
    explicitly want the old behaviour via --windows-log-partition / --state-partition.
    """
    import tempfile
    dp_fs = fs if fs in ("fat32", "ntfs", "exfat") else "fat32"
    # Carve ALL remaining free space into one primary partition, format it, label it.
    # `create partition primary` with no size= uses the largest contiguous free
    # region — exactly the post-ISO unallocated tail. `quick` format so it's fast;
    # `assign` lets Windows mount it now (a drive letter is harmless and lets the
    # user open it immediately after replug).
    script = "\n".join([
        # rescan FIRST so diskpart re-reads the now-valid full-disk GPT the
        # _windows_grow_gpt_to_device_end relocate exposed (the trailing free tail);
        # without it diskpart may still hold the stale, ISO-capped view.
        "rescan",
        "select disk %d" % disk["number"],
        "create partition primary",
        "format fs=%s label=%s quick" % (dp_fs, label),
        "assign",
        "",
    ])
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(script)
        try:
            r = subprocess.run(["diskpart", "/s", path],
                               capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as e:
            log("  %s partition: diskpart unavailable/timed out (%s) - "
                "skipped (flash is complete + bootable)" % (label, e))
            return False
        out = (r.stdout or "") + (r.stderr or "")
        low = out.lower()
        # diskpart reports success per step; the format completion line is the
        # authoritative "it worked" marker. A "no usable free extent" / "not
        # enough usable space" means the ISO consumed (almost) the whole stick —
        # a clean skip, never an error.
        ok = ("successfully formatted the volume" in low
              or "diskpart successfully formatted" in low
              or ("format" in low and "100 percent completed" in low))
        if ok:
            log("  %s partition: created + %s-formatted in free space "
                "(label=%s) - the host can now mount it natively"
                % (label, dp_fs.upper(), label))
            return True
        if "free" in low and ("no usable" in low or "not enough" in low):
            log("  %s partition: no free space on the stick "
                "(ISO filled it) - skipped (flash is complete + bootable)" % label)
            return False
        log("  %s partition: diskpart did not confirm format - skipped "
            "(flash is complete + bootable). diskpart said: %s"
            % (label, out.strip()[-200:]))
        return False
    except Exception as e:                       # belt-and-suspenders: never fail the flash
        log("  %s partition: unexpected error (%s) - skipped "
            "(flash is complete + bootable)" % (label, e))
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _create_log_partition_unix(disk, log, label=LOG_PART_LABEL, fs="fat32"):
    """Linux/macOS carve (the SAFE host-side path; no diskpart, no GPT corruption).
    Two steps, both via sgdisk:

      1. `sgdisk -e` relocates the BACKUP GPT header to the device's TRUE last LBA.
         A dd-written isohybrid GPT ISO puts the backup header at the ISO image's
         last LBA (mid-stick), so the primary header advertises LastUsableLBA = the
         ISO boundary and the multi-GB trailing tail is invisible. -e rewrites it to
         the real device end (idempotent; writes only LBA 1 + the new last LBA, so
         the in-use ISO/EFI partitions are never touched). This is the same fix the
         Live-OS module applies.
      2. `--largest-new=0` carves the now-visible trailing free tail into one new
         partition, named + typed for `label`, then mkfs formats + labels it. fs
         picks the on-disk filesystem + GPT typecode: "ext4" -> mkfs.ext4 + Linux-fs
         typecode 8300 (the HARTSTATE persistence default: Unix permissions +
         symlinks a bind-persisted home needs); anything else -> mkfs.vfat FAT32 +
         basic-data typecode 0700 (the HARTLOG default).

    BEST-EFFORT + BOUNDED: every step has a subprocess timeout; any missing tool,
    no-free-space, or error is a logged skip that returns False WITHOUT raising. The
    flash is already complete + bootable, and the Live OS carves the partition itself
    on first boot, so this host-side carve is purely a pre-seed. GPT-only: an MBR/DOS
    isohybrid is left to the Live-OS parted path (running sgdisk on it would convert
    the table).
    """
    dev = disk.get("dev") or disk.get("physdrive")
    sgdisk = shutil.which("sgdisk")
    if fs == "ext4":
        mkfs = shutil.which("mkfs.ext4")
        mkfs_name, typecode = "mkfs.ext4", "8300"       # Linux filesystem
        mkfs_args = ["-F", "-L", label]
        fs_disp = "ext4"
    else:
        mkfs = shutil.which("mkfs.vfat") or shutil.which("mkfs.fat")
        mkfs_name, typecode = "mkfs.vfat", "0700"       # Microsoft basic data
        mkfs_args = ["-F", "32", "-n", label]
        fs_disp = "FAT32"
    if not dev or not os.path.exists(dev):
        log("  %s partition: no block-device path for the target - skipped "
            "(flash is complete; the Live OS creates it on first boot)" % label)
        return False
    if not sgdisk or not mkfs:
        log("  %s partition: sgdisk/%s not on this host - skipped "
            "(flash is complete; the Live OS creates it on first boot)"
            % (label, mkfs_name))
        return False

    def _digits(s):
        return "".join(c for c in (s or "") if c.isdigit())

    def _run_t(cmd, timeout=60):
        """Run a tool with a hard timeout; return CompletedProcess or None on
        timeout/OSError (never raises out of the carve)."""
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            log("  %s partition: %s failed (%s)" % (label, cmd[0] if cmd else "?", e))
            return None

    try:
        # GPT-only guard: never run sgdisk on an MBR/DOS stick (it would convert it).
        rpt = _run_t(["lsblk", "-ndo", "PTTYPE", dev], timeout=20)
        pttype = (rpt.stdout if rpt else "").strip()
        if pttype and pttype != "gpt":
            log("  %s partition: %s is %s, not GPT - skipped "
                "(the Live OS handles the MBR/DOS carve)" % (label, dev, pttype))
            return False

        # 1. Relocate the backup GPT header to the true device end.
        if _run_t([sgdisk, "-e", dev]) is None:
            log("  %s partition: backup-GPT relocate failed - skipped "
                "(flash is complete + bootable)" % label)
            return False

        # 2. Measure the now-visible trailing free space.
        rf = _run_t([sgdisk, "-f", dev])
        re_ = _run_t([sgdisk, "-E", dev])
        if rf is None or re_ is None:
            return False
        first_free = int(_digits(rf.stdout) or "0")
        last_usable = int(_digits(re_.stdout) or "0")
        if first_free <= 0 or last_usable <= 0 or first_free >= last_usable:
            log("  %s partition: no trailing free space after relocate "
                "(first_free=%s last_usable=%s) - skipped (ISO filled the stick)"
                % (label, first_free, last_usable))
            return False
        free_sectors = last_usable - first_free + 1
        if free_sectors < 32768:                      # < 16 MiB
            log("  %s partition: trailing free space too small (%d sectors) - "
                "skipped (flash is complete + bootable)" % (label, free_sectors))
            return False

        # 3. Carve the largest free region into a new partition. --largest-new
        #    cannot move/resize an existing partition; it only appends a GPT entry.
        if _run_t([sgdisk, "--largest-new=0",
                   "--change-name=0:%s" % label,
                   "--typecode=0:%s" % typecode, dev]) is None:
            log("  %s partition: sgdisk carve failed - skipped "
                "(flash is complete + bootable)" % label)
            return False

        # 4. Re-read the table, resolve the new node, format it.
        for tool, args in (("partprobe", [dev]), ("partx", ["-a", dev])):
            exe = shutil.which(tool)
            if exe:
                _run_t([exe] + args, timeout=30)
        newpart = ""
        for _ in range(10):
            rl = _run_t(["lsblk", "-lnpo", "NAME,TYPE", dev], timeout=20)
            if rl:
                parts = [ln.split()[0] for ln in (rl.stdout or "").splitlines()
                         if len(ln.split()) >= 2 and ln.split()[1] == "part"]
                if parts:
                    newpart = parts[-1]
            if newpart and os.path.exists(newpart):
                break
            time.sleep(1)
        if not newpart or not os.path.exists(newpart):
            log("  %s partition: created the partition but its node did not "
                "settle - the Live OS self-heal will format it on first boot" % label)
            return False
        rm = _run_t([mkfs] + mkfs_args + [newpart], timeout=90)
        if rm is None or rm.returncode != 0:
            log("  %s partition: %s did not complete - the Live OS will "
                "format it on first boot" % (label, mkfs_name))
            return False
        log("  %s partition: created + %s-formatted on %s (label=%s) - the "
            "host can now mount it natively" % (label, fs_disp, newpart, label))
        return True
    except Exception as e:                       # belt-and-suspenders: never fail the flash
        log("  %s partition: unexpected error (%s) - skipped "
            "(flash is complete + bootable)" % (label, e))
        return False


# ─────────────────── raw image (the INSTALLED system) ───────────────────
# The raw-desktop artifact (2026-07-16) is the INSTALLED writable-root disk
# image: GPT with an ESP + an ext4 root that first boot grows to fill the
# stick. State persists because the root IS a real disk -- so this path has
# NO HARTSTATE/HARTLOG carve at all (the whole carve lineage exists only for
# the read-only live ISO). CI ships it as ONE xz stream (xz -T0 = a single
# multi-block stream, so one LZMADecompressor handles it -- split(1) only
# slices bytes) in .raw.xz.part-NN chunks, plus a .raw.sha256 companion
# holding the sha256 of the UNCOMPRESSED image for device verification.
GPT_SIG_OFFSET = 512          # LBA 1: the GPT header magic
GPT_SIG = b"EFI PART"


class _XZPartsReader:
    """File-like over sequential .raw.xz.part-NN files: read(n) hands out the
    DECOMPRESSED image bytes and tracks their sha256 + count.

    EXACT-FILL is the load-bearing contract: read(n) returns exactly n bytes
    unless the stream is exhausted. Both device writers (_WinExclusiveWriter.
    write_at and _py_write) pad any short buffer to the sector boundary, so a
    short MID-STREAM read would inject zero bytes INTO the image and corrupt
    it silently. The buffer loop below only returns early at true EOF."""

    def __init__(self, paths, log, progress=None, total_compressed=0):
        self._paths = list(paths)
        self._log = log
        self._progress = progress or (lambda f: None)
        self._total_comp = max(1, total_compressed)
        self._read_comp = 0
        self._fh = None
        self._d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
        self._buf = bytearray()
        self._exhausted = False
        self._hash = hashlib.sha256()
        self.count = 0            # decompressed bytes handed out so far

    def _next_compressed(self):
        """The next compressed chunk across the part files, or None at the end."""
        while True:
            if self._fh is None:
                if not self._paths:
                    return None
                path = self._paths.pop(0)
                self._log("  decompressing %s" % os.path.basename(path))
                self._fh = open(path, "rb")
            chunk = self._fh.read(CHUNK)
            if chunk:
                self._read_comp += len(chunk)
                self._progress(self._read_comp / self._total_comp)
                return chunk
            self._fh.close()
            self._fh = None

    def read(self, n):
        while len(self._buf) < n and not self._exhausted:
            chunk = self._next_compressed()
            if chunk is None:
                self._exhausted = True
                break
            self._buf += self._d.decompress(chunk)
            if self._d.eof:
                # xz -T0 emits ONE stream; anything non-zero after it means the
                # parts were assembled wrong (e.g. a foreign file slipped into
                # the sequence) -- corrupt loudly, never write it to the device.
                tail = self._d.unused_data.lstrip(b"\x00")
                if tail:
                    raise RuntimeError("trailing data after the xz stream "
                                       "(%d bytes) - part sequence is wrong" % len(tail))
                self._exhausted = True
                break
        ret = bytes(self._buf[:n])
        del self._buf[:n]
        self._hash.update(ret)
        self.count += len(ret)
        return ret

    def hexdigest(self):
        return self._hash.hexdigest()

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError as e:
                self._log("  (part close failed: %s)" % e)
            self._fh = None


def fetch_release_asset_text(gh, tag, name, tmp, log):
    """Download a small text asset (the .raw.sha256 companion) and return its
    content, or None. A missing companion degrades verification (logged loudly
    upstream) -- it never crashes the flash."""
    try:
        _run([gh, "release", "download", tag, "--repo", REPO,
              "--pattern", name, "--dir", tmp, "--clobber"])
        p = os.path.join(tmp, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        log("  %s: not present on the release" % name)
    except (OSError, RuntimeError) as e:
        log("  %s: fetch failed (%s)" % (name, e))
    return None


def verify_raw_sigs(disk, dd, log):
    """The raw image's on-disk boot contract: the protective-MBR 0x55AA boot
    signature at 0x1FE plus the GPT header magic 'EFI PART' at LBA 1 (byte 512).
    A raw-efi image is NOT an ISO -- there is no CD001 to check."""
    dev = disk["dev"] if dd else disk["physdrive"]
    boot = read_at(dev, BOOT_SIG_OFFSET, len(BOOT_SIG), dd)
    gpt = read_at(dev, GPT_SIG_OFFSET, len(GPT_SIG), dd)
    ok_boot = boot == BOOT_SIG
    ok_gpt = gpt == GPT_SIG
    log("  boot sig @0x1FE: %r %s" % (boot, "OK" if ok_boot else "FAIL"))
    log("  GPT sig @LBA1  : %r %s" % (gpt, "OK" if ok_gpt else "FAIL"))
    return ok_boot and ok_gpt


def flash_raw(tag, variant, disk, tmp, progress=None, log=None, verify=True):
    """Flash the INSTALLED raw image: download the .raw.xz.part-NN set, stream
    the single xz through decompression straight onto the device from byte 0,
    then verify the decompressed stream sha256 against the published
    .raw.sha256 companion AND read the device back to prove what landed.

    No per-part offsets exist here (offsets live in compressed space), so
    there is no --start-part resume and no per-part verify -- the stream hash
    covers every byte end to end."""
    log = log or (lambda m: None)
    progress = progress or (lambda f: None)
    gh = find_gh()
    if not gh:
        raise RuntimeError("GitHub CLI `gh` not found")
    dd = find_dd()
    parts = list_parts(gh, tag, variant, image="raw")
    if not parts:
        raise RuntimeError("no %s raw image parts in %s (the raw artifact ships "
                           "from 2026-07-16 nightlies onward; older tags are ISO-only)"
                           % (variant, tag))
    bad = [p["name"] for p in parts if p.get("state") and p["state"] != "uploaded"]
    if bad:
        raise RuntimeError("assets still uploading: %s" % ", ".join(bad))
    total_comp = sum(p["size"] for p in parts)
    base = parts[0]["name"].rsplit(".xz.part-", 1)[0]      # hart-os-...-linux.raw
    log("Flashing RAW image %s (%s) -> %s [%s], %d xz parts, %s compressed"
        % (tag, variant, disk["dev"] if dd else disk["physdrive"],
           disk["model"], len(parts), human(total_comp)))

    expected = None
    txt = fetch_release_asset_text(gh, tag, base + ".sha256", tmp, log)
    if txt and txt.split():
        expected = txt.split()[0].lower()
        log("  companion %s.sha256: %s" % (base, expected))
    else:
        log("  WARNING: no %s.sha256 companion - the device read-back will verify "
            "against the streamed hash only (device==stream, stream==source unproven)"
            % base)

    srcs = [download_part(gh, tag, p["name"], tmp, p["size"], log) for p in parts]

    writer = None
    if IS_WIN:
        _prepare_windows_device(disk, dd, log, clean=True)
        writer = _WinExclusiveWriter(disk["number"])
    reader = _XZPartsReader(srcs, log, progress=progress, total_compressed=total_comp)
    try:
        if writer is not None:
            written = writer.write_at(0, reader)
        else:
            written = _py_write(disk.get("physdrive") or disk["dev"], 0, reader, log)
    finally:
        reader.close()
        if writer is not None:
            writer.close()
        if IS_WIN:
            _win_automount(True)
    stream_sha = reader.hexdigest()
    log("  wrote %s (%s decompressed image bytes; stream sha256=%s)"
        % (human(written), human(reader.count), stream_sha))
    if reader.count % SECTOR:
        # A GPT disk image is sector-addressed, so a non-sector-multiple length
        # means the artifact itself is malformed -- say so, loudly.
        log("  WARNING: image length %d is not a 512-byte multiple - artifact malformed?"
            % reader.count)
    if expected and stream_sha != expected:
        raise RuntimeError("decompressed stream sha256 %s does not match the "
                           "published %s.sha256 (%s) - the download is corrupt; "
                           "delete %s and re-run" % (stream_sha, base, expected, tmp))
    try:
        _run(["sync"])
    except Exception as e:
        log("  (sync skipped: %s)" % e)

    log("Verifying raw boot signatures...")
    ok = verify_raw_sigs(disk, dd, log)
    log("DONE - raw image bootable OK" if ok else "DONE but signature check FAILED")
    if verify and reader.count and reader.count % SECTOR == 0:
        log("Verifying full image (sha256 read-back of the device region)...")
        dev = disk["dev"] if dd else disk["physdrive"]
        back = sha256_device_region(dev, 0, reader.count, dd)
        if back == stream_sha:
            log("  FULL VERIFY: OK - every byte on the device matches the stream (sha256)")
            for s in srcs:                      # only drop the cache once proven
                try:
                    os.remove(s)
                except OSError as e:
                    log("  (part cleanup skipped: %s)" % e)
        else:
            log("  FULL VERIFY: MISMATCH - device %s vs stream %s; re-flash" % (back, stream_sha))
            ok = False
    log("No HARTSTATE/HARTLOG carve for the raw image: the root filesystem is "
        "WRITABLE, so wifi/theme/home persist natively and the journal is "
        "persistent on disk - the carve lineage exists only for the live ISO.")
    return ok


# ───────────────────────── orchestration ─────────────────────────
def flash(tag, variant, disk, mode, tmp, progress=None, log=None,
          make_log_partition=False, start_part=0, verify=True, jobs=1,
          make_state_partition=False):
    log = log or (lambda m: None)
    progress = progress or (lambda f: None)
    gh = find_gh()
    if not gh:
        raise RuntimeError("GitHub CLI `gh` not found")
    dd = find_dd()
    parts = list_parts(gh, tag, variant)
    if not parts:
        raise RuntimeError("no %s ISO parts in %s" % (variant, tag))
    bad = [p["name"] for p in parts if p.get("state") and p["state"] != "uploaded"]
    if bad:
        raise RuntimeError("assets still uploading: %s" % ", ".join(bad))
    offs, total = offsets(parts)
    log("Flashing %s (%s) -> %s [%s], %d parts, %s, mode=%s" %
        (tag, variant, disk["dev"] if dd else disk["physdrive"],
         disk["model"], len(parts), human(total), mode))
    done = 0
    writer = None
    # --jobs N (download mode): prefetch up to N parts CONCURRENTLY into `tmp` while
    # the write loop below still consumes them strictly in offset order. Only the
    # downloads overlap — writes to the raw device stay serial (concurrent raw writes
    # to one disk are unsafe). Reuses download_part (its size-check makes an
    # already-present part a no-op) so there is exactly one download path.
    jobs = max(1, int(jobs or 1))
    _pool, _prefetch = None, {}
    if mode == "download" and jobs > 1:
        from concurrent.futures import ThreadPoolExecutor
        _pool = ThreadPoolExecutor(max_workers=jobs)

        def _submit(i):
            pp = parts[i]
            _prefetch[i] = _pool.submit(
                download_part, gh, tag, pp["name"], tmp, pp["size"], log)
        for i in range(start_part, min(start_part + jobs, len(parts))):
            _submit(i)
        log("Parallel download: prefetching up to %d parts concurrently into %s "
            "(writes stay serial + in-order)" % (jobs, tmp))
    elif jobs > 1:
        log("Note: --jobs %d applies to download mode only; stream mode writes "
            "each part serially. Proceeding single-stream." % jobs)
    if IS_WIN:
        # RESUME (start_part>0): keep the disk's already-written parts — skip the
        # destructive diskpart clean, only automount-off + dismount.
        _prepare_windows_device(disk, dd, log, clean=(start_part <= 0))
        writer = _WinExclusiveWriter(disk["number"])   # held exclusive for all parts
    try:
        for idx, (p, off) in enumerate(zip(parts, offs)):
            if idx < start_part:
                log("* %s @ %d — SKIP (already written; --start-part %d resume)"
                    % (p["name"], off, start_part))
                done += p["size"]
                progress(done / total)
                continue
            log("* %s @ %d (%s)" % (p["name"], off, human(p["size"])))
            if mode == "download":
                if _pool is not None:
                    src = _prefetch.pop(idx).result()   # block on THIS part's prefetch
                    nxt = idx + jobs                     # keep the sliding window full
                    if nxt < len(parts):
                        _submit(nxt)
                else:
                    src = download_part(gh, tag, p["name"], tmp, p["size"], log)
                w = write_source_to_device(disk, src, off, dd, log, writer)
                if w == p["size"]:                 # keep the file if the write failed
                    if verify:                     # record the source hash for read-back
                        try:                       # best-effort: never fail a good flash
                            record_part_hash(tmp, p["name"], off, p["size"],
                                             sha256_file(src), log)
                        except OSError as e:
                            log("  (verify record skipped: %s)" % e)
                    try:
                        os.remove(src)
                    except OSError:
                        pass
            else:
                w = -1
                for t in range(1, 5):
                    w = stream_to_device(disk, off, stream_producer(gh, p["id"]), dd, log, writer)
                    if w == p["size"]:
                        break
                    log("  stream try %d: wrote %s want %s — retry" % (t, w, p["size"]))
                    time.sleep(2)
            if w != p["size"]:
                raise RuntimeError("%s wrote %s of %s bytes" % (p["name"], w, p["size"]))
            log("  [ok] %s (%s bytes)" % (p["name"], w))
            done += p["size"]
            progress(done / total)
    finally:
        if _pool is not None:
            _pool.shutdown(wait=False)
        if writer is not None:
            writer.close()
        if IS_WIN:
            _win_automount(True)
    try:
        _run(["sync"])
    except Exception:
        pass
    log("Verifying signatures...")
    ok = verify_iso(disk, dd, log)
    log("DONE - bootable OK" if ok else "DONE but signature check FAILED")
    # BUILT-IN full sha256 read-back verify (default on; --no-verify to skip). Proves
    # every byte on the device matches the source, so no bespoke verify script is needed.
    if verify:
        log("Verifying full image (sha256 read-back of every part)...")
        fv = full_verify(disk, parts, tmp, dd, log)
        if fv is True:
            log("  FULL VERIFY: OK - every byte on the device matches the source (sha256)")
        elif fv is False:
            log("  FULL VERIFY: MISMATCH - the device does NOT match the source; re-flash")
            ok = False
        else:
            log("  FULL VERIFY: skipped (incomplete per-part hash record; e.g. stream mode "
                "or a partial write) - the boot signatures above still passed")
    # HARTLOG partition creation DEFAULTS OFF. The Live OS creates it itself on
    # first boot (nixos/modules/hart-hartlog-create.nix), Linux-side, which is SAFE
    # (it relocates the backup GPT to the true device end, then carves only the
    # trailing free tail). The Windows diskpart carve was DOUBLY broken: it HUNG on
    # a wedged Windows VDS, and a half-completed `diskpart create partition`
    # CORRUPTED a freshly-flashed stick's EFI/GPT (boot failed with start_image
    # returned 0x8000000000000001 = EFI_LOAD_ERROR). So we no longer touch the stick
    # after a successful flash by default - never risk bricking the very stick we
    # flashed. The opt-in host-side carve (--windows-log-partition / make_log_partition)
    # is a pre-seed so the host can read the log even BEFORE the first boot: on
    # Linux/macOS it runs the SAFE sgdisk relocate-then-carve; on Windows it now
    # FIRST relocates the backup GPT to the device's true end (std-lib, non-
    # destructive: writes only LBA1 + the device tail — the #128 fix) so diskpart
    # sees the revealed free tail, THEN carves it. `total` is the exact ISO byte
    # length the relocate needs. Gated on the verify passing; NEVER raises.
    if ok and (make_log_partition or make_state_partition):
        # Both carves use the SAME mechanism + free tail (only label + fs differ);
        # each is best-effort and can NEVER fail the (already-bootable) flash, and a
        # SINGLE post-carve re-verify below proves the boot image survived whichever
        # carve(s) ran. HARTLOG is a FAT32 diagnostic-log pre-seed; HARTSTATE is the
        # ext4 persistence partition the live OS bind-persists wifi/settings/home onto.
        if make_log_partition:
            log("Creating HARTLOG diagnostic-log partition in the free space "
                "(opt-in host-side pre-seed; the Live OS normally creates it itself)...")
            try:
                create_log_partition(disk, log, total)
            except Exception as e:               # the carve can NEVER fail the flash
                log("  HARTLOG partition: ignored error (%s) - flash is complete + bootable" % e)
        if make_state_partition:
            log("Creating HARTSTATE persistence partition in the free space "
                "(opt-in host-side pre-seed; the Live OS bind-persists onto it)...")
            try:
                create_log_partition(disk, log, total,
                                     label=STATE_PART_LABEL, fs="ext4")
            except Exception as e:               # the carve can NEVER fail the flash
                log("  HARTSTATE partition: ignored error (%s) - flash is complete + bootable" % e)
        # POST-CARVE SAFETY RE-VERIFY (#128). The carve rewrote the GPT (relocate)
        # and ran diskpart (create/format) — the exact step that historically
        # corrupted a freshly-flashed stick's EFI/GPT. Re-read the boot signatures
        # to PROVE the boot image is intact: a DEFINITIVE change ABORTS (flips the
        # flash result to FAILED) so the user re-flashes rather than booting a
        # silently-bricked stick; an indeterminate read (device still settling after
        # the format) leaves the result untouched (never a false brick claim).
        log("Re-verifying boot signatures after the host-side carve...")
        verdict = reverify_boot_sigs_after_carve(disk, dd, log)
        if verdict is False:
            log("!! POST-CARVE CHECK FAILED: a boot signature CHANGED after the "
                "host-side carve - the stick may NOT boot. RE-FLASH it (the host-side "
                "carve is opt-in + best-effort; the Live OS creating the partitions "
                "on first boot is the safe default).")
            ok = False
        elif verdict is True:
            log("Post-carve re-verify OK - boot signatures intact; the host-side "
                "carve did not disturb the ISO image.")
    else:
        log("HARTLOG/HARTSTATE partition: not created host-side (default) - the Live "
            "OS creates them itself on first boot, which can't corrupt the stick's "
            "EFI/GPT. Pass --windows-log-partition / --state-partition for the opt-in "
            "host-side carve.")
    return ok


# ───────────────────────── warnings + logging ─────────────────────────
def disk_contents_summary(disk):
    """One-line summary of what's on the disk — partition count + any drive
    letters/labels — so the destructive-write warning can say WHAT will be lost."""
    if IS_WIN:
        ps = ("$p=@(Get-Partition -DiskNumber %d -ErrorAction SilentlyContinue); "
              "$v=$p|Get-Volume -ErrorAction SilentlyContinue|Where-Object DriveLetter; "
              "$l=($v|ForEach-Object{\"$($_.DriveLetter): '$($_.FileSystemLabel)' [$($_.FileSystem)]\"}) -join '; '; "
              "$o=\"$($p.Count) partition(s)\"; if($l){$o+=\" | $l\"}; $o" % disk["number"])
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        return (r.stdout or "").strip()
    r = _run(["lsblk", "-no", "NAME,FSTYPE,LABEL,MOUNTPOINT", str(disk.get("dev", ""))])
    return " | ".join(x.strip() for x in (r.stdout or "").splitlines()[1:] if x.strip())


def disk_warning(disk):
    """Strong warning text if the target looks RISKY: a system/boot disk, a
    non-removable internal disk, or one Windows currently has mounted (drive
    letters = likely not a blank stick). Empty for a plain removable USB."""
    bits = []
    if disk.get("system"):
        bits.append("!! SYSTEM / BOOT DISK !!")
    elif not disk.get("removable"):
        bits.append("!! NON-REMOVABLE (internal) DISK !!")
    contents = disk_contents_summary(disk)
    if contents and not contents.startswith("0 partition"):
        bits.append("contains data: " + contents)
    return "  ".join(bits)


def _tee_logger(log_path, console):
    """Return log(msg) that prints via `console` AND appends a timestamped line
    to log_path — a persistent record to pinpoint what a failed flash did."""
    import datetime
    def log(msg):
        console(msg)
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("%s  %s\n" %
                         (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass
    return log


# ───────────────────────── CLI ─────────────────────────
def cmd_list(args):
    _, shown = list_disks_with_self_heal(args.allow_system)
    if not shown:
        print("No removable/USB disks found." if not args.allow_system else "No disks.")
        return 1
    for d in shown:
        tag = "USB" if d["removable"] else d["bus"] or "disk"
        sysflag = " [SYSTEM]" if d["system"] else ""
        print("  #%s  %-28s %9s  %s%s  (%s)" %
              (d["number"], d["model"][:28], human(d["size"]), tag, sysflag,
               d["dev"] if not IS_WIN else d["physdrive"]))
    return 0


def pick_disk(args):
    _, pool = list_disks_with_self_heal(args.allow_system,
                                        log=lambda m: sys.stderr.write(m + "\n"))
    sel = [d for d in pool if str(d["number"]) == str(args.device)]
    if not sel:
        sys.stderr.write("Device %r is not an offered %s disk. Run --list.\n" %
                         (args.device, "" if args.allow_system else "USB/removable"))
        sys.exit(2)
    d = sel[0]
    if d["system"] and not args.allow_system:
        sys.stderr.write("Refusing to write a system disk.\n")
        sys.exit(2)
    return d


def cmd_flash(args):
    if not args.yes:
        sys.stderr.write("Refusing to write without --yes (this ERASES the disk).\n")
        return 2
    gh = find_gh()
    tag = args.tag or latest_nightly_tag(gh)
    if not tag:
        sys.stderr.write("No --tag and no nightly release found.\n")
        return 2
    disk = pick_disk(args)
    warn = disk_warning(disk)
    if warn:
        sys.stderr.write("WARNING — target #%s %s (%s):\n  %s\n"
                         % (disk["number"], disk["model"], human(disk["size"]), warn))
    os.makedirs(args.tmp, exist_ok=True)
    log_path = args.log or os.path.join(args.tmp, "hart_flash.log")
    console = (lambda m: None) if args.quiet else (lambda m: print(m, flush=True))
    log = _tee_logger(log_path, console)
    log("=== flash %s (%s/%s) -> disk #%s %s mode=%s [%s] ==="
        % (tag, args.variant, args.image, disk["number"], disk["model"], args.mode,
           warn or "removable/blank"))
    # The raw image has no per-part device offsets (they live in compressed
    # space) and no carve story (its root is WRITABLE -- state persists
    # natively). Reject the ISO-only flags loudly instead of half-honoring them.
    if args.image == "raw":
        blocked = [f for f, on in (("--start-part", args.start_part > 0),
                                   ("--state-partition", args.state_partition),
                                   ("--windows-log-partition", args.windows_log_partition)) if on]
        if blocked:
            sys.stderr.write("%s do(es) not apply to --image raw: the raw image "
                             "streams sequentially (no part offsets to resume) and "
                             "its root is WRITABLE (state persists natively - no "
                             "HARTSTATE/HARTLOG carve exists or is needed).\n"
                             % ", ".join(blocked))
            return 2
        if args.mode == "stream" or args.jobs > 1:
            log("note: --image raw always downloads then streams the single xz "
                "sequentially; --mode/--jobs are ignored")
    try:
        if args.image == "raw":
            ok = flash_raw(tag, args.variant, disk, args.tmp, log=log,
                           verify=args.verify)
        else:
            ok = flash(tag, args.variant, disk, args.mode, args.tmp, log=log,
                       make_log_partition=args.windows_log_partition,
                       make_state_partition=args.state_partition,
                       start_part=args.start_part, verify=args.verify, jobs=args.jobs)
    except Exception as e:
        log("FLASH FAILED: %s" % e)
        sys.stderr.write("FLASH FAILED: %s\n  (debug log: %s)\n" % (e, log_path))
        return 1
    if not args.quiet:
        print("debug log: %s" % log_path)
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(description="HART OS USB Flasher (CLI + GUI).")
    p.add_argument("--gui", action="store_true", help="launch the GUI")
    p.add_argument("--list", action="store_true", help="list candidate disks and exit")
    p.add_argument("--tag", help="release tag (default: latest nightly-*)")
    p.add_argument("--variant", default="desktop", choices=["desktop", "server", "edge"])
    p.add_argument("--image", default="iso", choices=["iso", "raw"],
                   help="iso (default): the live/rescue medium (read-only root, "
                        "stateless). raw: the INSTALLED system image (writable "
                        "root; wifi/theme/home persist natively, first boot grows "
                        "the root to fill the stick; UEFI boot). raw ships from "
                        "2026-07-16 nightlies onward.")
    p.add_argument("--device", help="disk number/name from --list")
    p.add_argument("--mode", default="stream", choices=["download", "stream"],
                   help="stream (default, ~40%% faster: overlaps download+write) "
                        "or download (download each part fully, then write)")
    p.add_argument("--tmp", default=default_tmp(), help="scratch dir (download mode)")
    p.add_argument("--jobs", type=int, default=1, metavar="N",
                   help="download mode only: prefetch up to N parts CONCURRENTLY "
                        "(default 1 = serial). Only the downloads overlap; writes to "
                        "the device stay serial + in-order (concurrent raw writes to a "
                        "single disk are unsafe). Trades scratch for speed: needs about "
                        "N x part-size free in --tmp (each desktop part is ~1.9 GiB, so "
                        "--jobs 4 wants ~7.6 GiB). Ignored in stream mode.")
    p.add_argument("--yes", action="store_true", help="confirm the destructive write")
    p.add_argument("--start-part", type=int, default=0,
                   help="RESUME: skip writing parts before this index (already on "
                        "the device) and skip the destructive diskpart clean. Use "
                        "when a prior run wrote the first N parts (e.g. --start-part 2 "
                        "after part-00/01 completed). Offsets stay absolute.")
    p.add_argument("--no-verify", dest="verify", action="store_false",
                   help="skip the built-in full sha256 read-back verify (on by default; "
                        "the verify reads every part back off the device and compares it "
                        "to the source hash, resume-safe -- no bespoke script needed).")
    p.set_defaults(verify=True)
    p.add_argument("--quiet", action="store_true", help="silent CLI (errors to stderr)")
    p.add_argument("--log", help="debug log file (default: <tmp>/hart_flash.log)")
    p.add_argument("--allow-system", action="store_true",
                   help="DANGEROUS: also offer non-removable/system disks")
    p.add_argument("--windows-log-partition", action="store_true",
                   help="OPT-IN to the host-side carve of the HARTLOG diagnostic-log "
                        "partition in the stick's free space, right after the flash. "
                        "OFF by default: the Live OS creates HARTLOG itself on first "
                        "boot (Linux-side, safe). On Linux/macOS this opt-in runs the "
                        "SAFE sgdisk relocate-then-carve; on Windows it FIRST relocates "
                        "the backup GPT to the device's true end (std-lib, non-"
                        "destructive: writes only LBA1 + the device tail) so the "
                        "trailing free tail a dd-written isohybrid hid becomes visible, "
                        "THEN diskpart carves it. Best-effort + real-HW-gated: it can "
                        "never fail the (already-bootable) flash.")
    p.add_argument("--state-partition", action="store_true",
                   help="OPT-IN to carve a HARTSTATE persistence partition from the "
                        "stick's free space, right after the flash, so the live USB is "
                        "STATEFUL across reboots (the live OS bind-persists wifi/"
                        "settings/home onto it). Uses the SAME safe carve as "
                        "--windows-log-partition (GPT relocate-then-carve, post-carve "
                        "boot re-verify, never fails the already-bootable flash): ext4 "
                        "on Linux/macOS, FAT32 host-side on Windows (the live OS "
                        "reformats it to ext4 on first boot). OFF by default.")
    return p


def main(argv=None):
    # Windows consoles default to cp1252; force UTF-8 (replace on failure) so a
    # status glyph can never crash the run with a UnicodeEncodeError.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if args.gui or (len(sys.argv) == 1):
        return launch_gui()
    if args.list:
        return cmd_list(args)
    if not args.device:
        sys.stderr.write("Need --device (see --list) or --gui.\n")
        return 2
    return cmd_flash(args)


# ───────────────────────── GUI (Tkinter) ─────────────────────────
def launch_gui():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception as e:
        sys.stderr.write("GUI unavailable (%s). Use the CLI.\n" % e)
        return 1
    import threading

    gh = find_gh()
    root = tk.Tk()
    root.title("HART OS — USB Flasher")
    root.geometry("640x520")
    disks_state = {"list": []}

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="HART OS USB Flasher", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Writes a HART OS release to a USB stick. The target is ERASED.",
              foreground="#b00").pack(anchor="w", pady=(0, 8))

    row = ttk.Frame(frm); row.pack(fill="x", pady=3)
    ttk.Label(row, text="USB disk:", width=12).pack(side="left")
    disk_cb = ttk.Combobox(row, state="readonly", width=46); disk_cb.pack(side="left")

    def refresh():
        _, ds = list_disks_with_self_heal(allow_system=False, log=log)
        disks_state["list"] = ds
        disk_cb["values"] = ["#%s  %s  (%s)" % (d["number"], d["model"], human(d["size"])) for d in ds]
        if ds:
            disk_cb.current(0)
    ttk.Button(row, text="↻", width=3, command=refresh).pack(side="left", padx=4)

    row2 = ttk.Frame(frm); row2.pack(fill="x", pady=3)
    ttk.Label(row2, text="Release tag:", width=12).pack(side="left")
    tag_var = tk.StringVar(value=(latest_nightly_tag(gh) or "") if gh else "")
    ttk.Entry(row2, textvariable=tag_var, width=46).pack(side="left")

    row3 = ttk.Frame(frm); row3.pack(fill="x", pady=3)
    ttk.Label(row3, text="Variant:", width=12).pack(side="left")
    var_var = tk.StringVar(value="desktop")
    ttk.Combobox(row3, textvariable=var_var, values=["desktop", "server", "edge"],
                 state="readonly", width=12).pack(side="left")
    ttk.Label(row3, text="   Mode:").pack(side="left")
    mode_var = tk.StringVar(value="stream")
    ttk.Radiobutton(row3, text="Streaming (faster)", variable=mode_var, value="stream").pack(side="left")
    ttk.Radiobutton(row3, text="Download+Flash", variable=mode_var, value="download").pack(side="left")

    row4 = ttk.Frame(frm); row4.pack(fill="x", pady=3)
    # Default OFF (matches the CLI default): the Live OS creates the HARTLOG
    # partition itself on first boot (Linux-side, safe). The opt-in host-side carve
    # is a pre-seed so the host can read the log before first boot; on Linux/macOS
    # it is the safe sgdisk path, on Windows the legacy diskpart path.
    logpart_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(row4,
                    text="Also create HARTLOG host-side now (OFF - the Live OS does this safely on first boot)",
                    variable=logpart_var).pack(side="left")

    row4b = ttk.Frame(frm); row4b.pack(fill="x", pady=3)
    # Opt-in HARTSTATE persistence partition: makes the live USB stateful across
    # reboots (the live OS bind-persists wifi/settings/home onto it). Same safe carve.
    statepart_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(row4b,
                    text="Make this USB STATEFUL (carve a HARTSTATE persistence partition - keeps wifi/settings/home across reboots)",
                    variable=statepart_var).pack(side="left")

    pbar = ttk.Progressbar(frm, mode="determinate", maximum=1.0)
    pbar.pack(fill="x", pady=(10, 4))
    logbox = tk.Text(frm, height=15, wrap="word", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True, pady=4)

    def log(m):
        logbox.insert("end", m + "\n"); logbox.see("end"); root.update_idletasks()

    def do_flash():
        ds = disks_state["list"]
        if not ds or disk_cb.current() < 0:
            messagebox.showerror("No disk", "Insert a USB stick and press ↻.")
            return
        d = ds[disk_cb.current()]
        warn = disk_warning(d)
        msg = ("This will PERMANENTLY ERASE:\n\n  #%s  %s  (%s)\n\n"
               % (d["number"], d["model"], human(d["size"])))
        if warn:
            msg += "⚠ " + warn + "\n\n"
        msg += "Make sure this is your USB stick, not another drive.\nContinue?"
        if not messagebox.askyesno("ERASE this disk?", msg):
            return
        btn.config(state="disabled")

        def worker():
            try:
                tmp = default_tmp()
                os.makedirs(tmp, exist_ok=True)
                flog = _tee_logger(os.path.join(tmp, "hart_flash.log"), log)
                flog("=== GUI flash %s (%s) disk #%s mode=%s [%s] ==="
                     % (tag_var.get().strip(), var_var.get(), d["number"],
                        mode_var.get(), warn or "removable/blank"))
                flash(tag_var.get().strip(), var_var.get(), d, mode_var.get(),
                      tmp, progress=lambda f: pbar.config(value=f), log=flog,
                      make_log_partition=logpart_var.get(),
                      make_state_partition=statepart_var.get())
                messagebox.showinfo("Done", "Flash complete — the stick is bootable.")
            except Exception as e:
                log("FAILED: %s" % e)
                messagebox.showerror("Failed", str(e))
            finally:
                btn.config(state="normal")
        threading.Thread(target=worker, daemon=True).start()

    btn = ttk.Button(frm, text="⚡ Flash USB", command=do_flash)
    btn.pack(pady=6)
    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
