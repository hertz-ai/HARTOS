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
import json
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
    line after the header. `Type : USB` => bus 'USB'. `Boot Disk : Yes` (or a
    USB-less disk that is the boot/pagefile disk) => system True."""
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
    # A USB-bus disk is removable and never a system disk.
    if bus == "USB":
        system = False
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
def list_parts(gh, tag, variant):
    """Return sorted [{name, id, size}] for <variant> ISO parts of <tag>.

    A missing/unpublished tag makes `gh api` 404 and emit an error object
    ({"message":"Not Found",...}) on stdout instead of asset lines. Guard the
    dict access so that case yields an empty list (=> a clean "no parts" error
    in flash()) rather than a KeyError, and so a malformed line never aborts."""
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
        if name and variant in name and ".part-" in name:
            parts.append(a)
    parts.sort(key=lambda a: a["name"])
    return parts


def latest_nightly_tag(gh):
    r = _run([gh, "api", "repos/%s/releases?per_page=10" % REPO,
              "--jq", ".[] | select(.tag_name|startswith(\"nightly-\")) | .tag_name"])
    tags = [t.strip() for t in (r.stdout or "").splitlines() if t.strip()]
    return tags[0] if tags else None


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
    r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    for letter in (r.stdout or "").split():
        letter = letter.strip()
        if letter:
            _run(["cmd", "/c", "mountvol", "%s:" % letter, "/D"])


def _win_automount(enable):
    """Toggle Windows auto-mounting of new volumes (mountvol /E or /N). With it
    disabled, Windows won't re-mount — and protect — the ISO9660 volume as it is
    written, which otherwise fails raw writes with 'Invalid request code'."""
    _run(["cmd", "/c", "mountvol", "/E" if enable else "/N"])


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
        r = subprocess.run(["diskpart", "/s", path], capture_output=True, text=True)
        ok = "succeeded in cleaning" in (r.stdout or "").lower()
        log("  diskpart clean disk %d: %s" %
            (disk_number, "OK" if ok else (r.stdout or r.stderr or "").strip()[-160:]))
        return ok
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _prepare_windows_device(disk, dd, log):
    """Release Windows' raw-write protection before flashing: disable automount
    (so the freshly-written ISO isn't re-mounted + re-protected mid-write), drop
    any drive-letter mounts, and wipe the partition table with diskpart `clean`
    so there is no protected partition/volume. Without this, raw writes fail with
    'Invalid request code' / 'Permission denied' at ~12 MB."""
    _win_automount(False)
    _dismount_windows(disk)
    _win_diskpart_clean(disk["number"], log)


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


def write_source_to_device(disk, src_path, byte_offset, dd, log, writer=None):
    """Write a local file to the device at byte_offset (download mode)."""
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
    cd = read_at(dev, 0x8001, 5, dd)
    boot = read_at(dev, 0x1FE, 2, dd)
    ok_cd = cd == b"CD001"
    ok_boot = boot == b"\x55\xAA"
    log("  ISO9660 @0x8001: %r %s" % (cd, "OK" if ok_cd else "FAIL"))
    log("  boot sig @0x1FE: %r %s" % (boot, "OK" if ok_boot else "FAIL"))
    return ok_cd and ok_boot


# ───────────────────────── HARTLOG diagnostic-log partition ─────────────────────────
# The desktop live ISO uses ~6.6 GB of a 28 GB stick — ~21 GB is free. After a
# successful flash we carve that free space into ONE FAT32 partition labelled
# HARTLOG. The HART OS boot-log module (nixos/modules/hart-boot-log.nix) detects
# that label at boot and writes the boot journal + tier-supervisor state + GTK4/
# GL diagnostics to it, so the Windows host can read the boot journal off the
# stick (no TTY hand-copy). The label HARTLOG is the ONE source of truth shared
# with that module (hart.bootLog.label).
LOG_PART_LABEL = "HARTLOG"


def create_log_partition(disk, log):
    """Create a FAT32 partition labelled HARTLOG in the stick's FREE SPACE.

    ROBUST BY CONTRACT: this runs AFTER a successful flash + boot-sig verify, and
    it must NEVER fail/abort the (already-successful) flash. Every failure path —
    no free space, diskpart missing/errored, non-Windows, any exception — is
    caught and logged, and the function returns False without raising. The flash
    is already done and bootable; the log partition is a debug convenience.

    Mirrors the existing diskpart-fallback style (the flasher already shells
    diskpart for enumeration + `clean`). Windows-only: only diskpart can carve a
    partition into the post-isohybrid free space and FAT32-format + label it so
    Windows mounts the drive natively. On Linux/macOS this is a logged no-op (the
    user flashing from those hosts can read the ISO's own journal via other
    means; the HARTLOG convenience targets the Windows-host debug loop).
    """
    if not IS_WIN:
        log("  HARTLOG partition: skipped (only created on Windows; flash is complete)")
        return False
    import tempfile
    # Carve ALL remaining free space into one primary partition, format FAT32,
    # label HARTLOG. `create partition primary` with no size= uses the largest
    # contiguous free region — exactly the post-ISO unallocated tail. `quick`
    # format so it's fast; `assign` lets Windows mount it now (a drive letter is
    # harmless and lets the user open it immediately after replug).
    script = "\n".join([
        "select disk %d" % disk["number"],
        "create partition primary",
        "format fs=fat32 label=%s quick" % LOG_PART_LABEL,
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
            log("  HARTLOG partition: diskpart unavailable/timed out (%s) — "
                "skipped (flash is complete + bootable)" % e)
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
            log("  HARTLOG partition: created + FAT32-formatted in free space "
                "(label=%s) — the host can now read hart-boot-latest.log off it"
                % LOG_PART_LABEL)
            return True
        if "free" in low and ("no usable" in low or "not enough" in low):
            log("  HARTLOG partition: no free space on the stick "
                "(ISO filled it) — skipped (flash is complete + bootable)")
            return False
        log("  HARTLOG partition: diskpart did not confirm format — skipped "
            "(flash is complete + bootable). diskpart said: %s"
            % out.strip()[-200:])
        return False
    except Exception as e:                       # belt-and-suspenders: never fail the flash
        log("  HARTLOG partition: unexpected error (%s) — skipped "
            "(flash is complete + bootable)" % e)
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ───────────────────────── orchestration ─────────────────────────
def flash(tag, variant, disk, mode, tmp, progress=None, log=None,
          make_log_partition=False):
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
    if IS_WIN:
        _prepare_windows_device(disk, dd, log)
        writer = _WinExclusiveWriter(disk["number"])   # held exclusive for all parts
    try:
        for p, off in zip(parts, offs):
            log("* %s @ %d (%s)" % (p["name"], off, human(p["size"])))
            if mode == "download":
                src = download_part(gh, tag, p["name"], tmp, p["size"], log)
                w = write_source_to_device(disk, src, off, dd, log, writer)
                if w == p["size"]:                 # keep the file if the write failed
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
    # HARTLOG partition creation now DEFAULTS OFF on Windows. The Live OS creates
    # it itself on first boot (nixos/modules/hart-hartlog-create.nix), Linux-side,
    # which is SAFE. The Windows diskpart carve was DOUBLY broken: it HUNG on a
    # wedged Windows VDS, and a half-completed `diskpart create partition` CORRUPTED
    # a freshly-flashed stick's EFI/GPT (boot failed with start_image returned
    # 0x8000000000000001 = EFI_LOAD_ERROR). So we no longer touch the stick after a
    # successful flash by default — never risk bricking the very stick we flashed.
    # The legacy carve stays opt-in via --windows-log-partition (make_log_partition)
    # for anyone who explicitly wants the old path; it is gated on the verify
    # passing and NEVER raises / never fails the flash.
    if ok and make_log_partition:
        log("Creating HARTLOG diagnostic-log partition in the free space "
            "(--windows-log-partition; the Live OS normally creates it itself)...")
        try:
            create_log_partition(disk, log)
        except Exception as e:                   # the carve can NEVER fail the flash
            log("  HARTLOG partition: ignored error (%s) — flash is complete + bootable" % e)
    else:
        log("HARTLOG partition: not created on Windows (default) — the Live OS "
            "creates it itself on first boot, which can't corrupt the stick's "
            "EFI/GPT. Pass --windows-log-partition to force the legacy Windows path.")
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
    log("=== flash %s (%s) -> disk #%s %s mode=%s [%s] ==="
        % (tag, args.variant, disk["number"], disk["model"], args.mode,
           warn or "removable/blank"))
    try:
        ok = flash(tag, args.variant, disk, args.mode, args.tmp, log=log,
                   make_log_partition=args.windows_log_partition)
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
    p.add_argument("--device", help="disk number/name from --list")
    p.add_argument("--mode", default="stream", choices=["download", "stream"],
                   help="stream (default, ~40%% faster: overlaps download+write) "
                        "or download (download each part fully, then write)")
    p.add_argument("--tmp", default=default_tmp(), help="scratch dir (download mode)")
    p.add_argument("--yes", action="store_true", help="confirm the destructive write")
    p.add_argument("--quiet", action="store_true", help="silent CLI (errors to stderr)")
    p.add_argument("--log", help="debug log file (default: <tmp>/hart_flash.log)")
    p.add_argument("--allow-system", action="store_true",
                   help="DANGEROUS: also offer non-removable/system disks")
    p.add_argument("--windows-log-partition", action="store_true",
                   help="OPT-IN to the LEGACY Windows diskpart carve of the HARTLOG "
                        "diagnostic-log partition in the stick's free space. OFF by "
                        "default: the Live OS now creates HARTLOG itself on first "
                        "boot (Linux-side, safe). The Windows diskpart path hung on "
                        "a wedged VDS AND corrupted a freshly-flashed stick's "
                        "EFI/GPT — only use this if you specifically need the old "
                        "path.")
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
    # partition itself on first boot (Linux-side, safe). The legacy Windows
    # diskpart carve hung on a wedged VDS AND corrupted a freshly-flashed stick's
    # EFI/GPT, so it is OPT-IN only now.
    logpart_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(row4,
                    text="Legacy: create HARTLOG via Windows diskpart (OFF — the Live OS now does this safely)",
                    variable=logpart_var).pack(side="left")

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
                      make_log_partition=logpart_var.get())
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
