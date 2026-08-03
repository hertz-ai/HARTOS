"""
Shell System APIs — System management for HART OS.

Covers: task/process manager, storage manager, startup apps,
bluetooth management, print manager, media indexer.

All routes registered via register_shell_system_routes(app).
"""

import configparser
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time

logger = logging.getLogger('hevolve.shell.system')

# ─── Helpers ────────────────────────────────────────────────────

# The bounded probe lives in core.subprocess_safe — ONE implementation for
# both shell API modules. First attempt (6e0a101d) was reverted because
# TestShellWiFi/TestShellVPN mocked `<module>.subprocess` wholesale, so the
# mock stopped intercepting once the syscall moved into another namespace.
# Those tests now patch the `_run` SEAM instead of the implementation detail
# beneath it, which is what they always should have done — so the duplicate
# can finally go. Any test that patches `_run` is unaffected by this alias.
from core.subprocess_safe import run_probe as _run


def _first_int(r, default=0):
    """First stdout token of a _run result as int, else default. The bare
    `int(r.stdout.split()[0])` idiom crashed 500 (IndexError) whenever a tool
    exited 0 with EMPTY stdout (du on a vanished path, minimal builds) -- found
    by the deployed-surface suite. Empty/malformed output is an expected
    degrade, so default quietly; the caller's rc-check already gates rc!=0."""
    try:
        return int(r.stdout.split()[0])
    except (AttributeError, IndexError, ValueError, TypeError):
        return default


def _run_async_bounded(cmd, run_timeout=20, wait=6, name='hart-shell-op', **kw):
    """Run a blocking subprocess on a daemon worker, waiting at most ``wait``
    seconds for it on the CALLER.

    Returns ``(finished, result)``:
      * ``(True, CompletedProcess | None)`` — the command settled within ``wait``
        (``result`` is ``None`` only if the tool was missing or it self-timed-out
        at ``run_timeout``, mirroring :func:`_run`).
      * ``(False, None)`` — still running: the worker keeps going to completion so
        the side-effect still lands, but the request thread is freed instead of
        being pinned for the whole ``run_timeout``.

    Why this exists: the shell server runs on a 1-2 thread pool on a lite/embedded
    box. A 30s synchronous connect would pin a pool thread and queue every other
    shell fetch behind it (the click-to-freeze). Bounding the caller's wait keeps
    the pool responsive while a slow op finishes out-of-band.
    """
    holder = {}
    done = threading.Event()

    def _worker():
        try:
            holder['result'] = _run(cmd, timeout=run_timeout, **kw)
        finally:
            done.set()

    threading.Thread(target=_worker, name=name, daemon=True).start()
    finished = done.wait(wait)
    return finished, holder.get('result')


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


# ═══════════════════════════════════════════════════════════════
# Disk Utility (#157) — module-level pure helpers
#
# These build/parse the tool surface for format / fsck / defrag / resize / trim /
# health, kept at module level (not nested in the route fn) so the unit test can
# import + exercise them directly with a mocked subprocess boundary (Gate 5: no
# grep tests). Every helper is degrade-safe: a missing tool / unparseable output
# returns an HONEST empty/false, never raises. The matching NixOS tooling is
# shipped by hart-storage.nix (#157); these only INVOKE it, and the destructive
# ops (format/resize) are confirm + auth gated in the routes below.
# ═══════════════════════════════════════════════════════════════

# Where hart-disk-health.sh (the boot snapshot) writes its per-device SMART
# verdict. Env-overridable so the test points it at a fixture.
_DISK_HEALTH_FILE = os.environ.get('HART_DISK_HEALTH_FILE', '/run/hart/disk-health')

# mkfs argv per filesystem (the FORMAT op, DESTRUCTIVE). The 7-FS set matches
# hart.storage.filesystems. Device is appended at call time; label is optional.
_FS_FORMAT = {
    'ext4':  ['mkfs.ext4', '-F'],
    'btrfs': ['mkfs.btrfs', '-f'],
    'xfs':   ['mkfs.xfs', '-f'],
    'vfat':  ['mkfs.vfat', '-F', '32'],
    'exfat': ['mkfs.exfat'],
    'ntfs':  ['mkfs.ntfs', '-Q', '-F'],
    'f2fs':  ['mkfs.f2fs', '-f'],
}

# The label flag each mkfs uses (None where this simple path skips labelling).
_FS_LABEL_FLAG = {
    'ext4': '-L', 'btrfs': '-L', 'xfs': '-L', 'vfat': '-n',
    'exfat': '-n', 'ntfs': '-L', 'f2fs': '-l',
}

# The PRIMARY tool name per FS for each op (used by the capabilities catalog to
# report which ops this closure can actually perform via shutil.which).
_FS_FSCK_TOOL = {
    'ext4': 'e2fsck', 'btrfs': 'btrfs', 'xfs': 'xfs_repair',
    'vfat': 'fsck.fat', 'exfat': 'fsck.exfat', 'ntfs': 'ntfsfix', 'f2fs': 'fsck.f2fs',
}
_FS_DEFRAG_TOOL = {'ext4': 'e4defrag', 'btrfs': 'btrfs', 'xfs': 'xfs_fsr'}
_FS_RESIZE_TOOL = {
    'ext4': 'resize2fs', 'btrfs': 'btrfs', 'xfs': 'xfs_growfs',
    'ntfs': 'ntfsresize', 'f2fs': 'resize.f2fs',
}

# A /dev path with only safe characters. argv (not shell) already blocks shell
# injection; this rejects an obviously-bogus target before any op.
_DEV_RE = re.compile(r'^/dev/[A-Za-z0-9/_-]+$')


def _valid_device(device):
    """True iff ``device`` is a well-formed /dev path (regex only, no I/O)."""
    return bool(device) and bool(_DEV_RE.match(device))


def _parent_disk(device):
    """Resolve a partition's whole-disk parent (/dev/sda3 -> /dev/sda,
    /dev/nvme0n1p2 -> /dev/nvme0n1, /dev/mmcblk0p1 -> /dev/mmcblk0)."""
    m = re.match(r'^(/dev/(?:nvme\d+n\d+|mmcblk\d+))p\d+$', device or '')
    if m:
        return m.group(1)
    m = re.match(r'^(/dev/[a-zA-Z]+)\d+$', device or '')
    if m:
        return m.group(1)
    return device


def _is_mounted(device):
    """True iff ``device`` is currently mounted anywhere (via findmnt -S)."""
    r = _run(['findmnt', '-nro', 'TARGET', '-S', device], timeout=5)
    return bool(r and r.returncode == 0 and r.stdout.strip())


def _protected_devices():
    """The devices that back /, /boot, /nix (and their whole-disk parents) — the
    set the destructive ops refuse to touch so the running OS can never be wiped."""
    devs = set()
    for mp in ('/', '/boot', '/boot/efi', '/nix', '/nix/store'):
        r = _run(['findmnt', '-nro', 'SOURCE', mp], timeout=5)
        if r and r.returncode == 0 and r.stdout.strip():
            src = r.stdout.strip().split('\n')[0].strip()
            # findmnt may suffix a btrfs subvol: /dev/sda2[/@] -> /dev/sda2
            src = src.split('[')[0]
            if src.startswith('/dev/'):
                devs.add(src)
                devs.add(_parent_disk(src))
    return devs


def _is_protected_device(device):
    """True iff ``device`` (or its whole-disk parent) backs the running OS."""
    if not device:
        return False
    prot = _protected_devices()
    return device in prot or _parent_disk(device) in prot


def _device_fstype(device):
    """The on-disk filesystem of ``device`` (lsblk FSTYPE), '' if unknown."""
    r = _run(['lsblk', '-nro', 'FSTYPE', device], timeout=5)
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split('\n')[0].strip()
    return ''


def _path_fstype(path):
    """The filesystem mounted at (or containing) ``path`` (findmnt -T), '' if unknown."""
    if not path:
        return ''
    r = _run(['findmnt', '-nro', 'FSTYPE', '-T', path], timeout=5)
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split('\n')[0].strip()
    return ''


def _fsck_cmd(fstype, device, repair):
    """argv for a check (read-only) or repair fsck, or None if unsupported."""
    f = (fstype or '').lower()
    if f in ('ext4', 'ext3', 'ext2'):
        return ['e2fsck', '-f', ('-y' if repair else '-n'), device]
    if f == 'btrfs':
        return ['btrfs', 'check'] + (['--repair'] if repair else ['--readonly']) + [device]
    if f == 'xfs':
        # xfs_repair -n = no-modify check; without -n = repair (needs unmounted).
        return ['xfs_repair'] + ([] if repair else ['-n']) + [device]
    if f in ('vfat', 'fat', 'fat32'):
        return ['fsck.fat', ('-a' if repair else '-n'), device]
    if f == 'exfat':
        return ['fsck.exfat'] + (['-y'] if repair else ['-n']) + [device]
    if f == 'ntfs':
        # ntfsfix clears common errors; -n = no-action (check only).
        return ['ntfsfix'] + ([] if repair else ['-n']) + [device]
    if f == 'f2fs':
        return ['fsck.f2fs'] + (['-f'] if repair else ['--dry-run']) + [device]
    return None


def _defrag_cmd(fstype, target):
    """argv to defrag the filesystem at mount ``target``, or None if the FS has no
    online defrag (f2fs/ntfs/vfat/exfat are log-structured or flash-native)."""
    f = (fstype or '').lower()
    if f in ('ext4', 'ext3', 'ext2'):
        return ['e4defrag', target]
    if f == 'btrfs':
        return ['btrfs', 'filesystem', 'defragment', '-r', target]
    if f == 'xfs':
        return ['xfs_fsr', target]
    return None


def _resize_cmd(fstype, device, mount, size, grow):
    """argv to shrink/grow a filesystem, or None if unsupported. ``size`` is a
    target like '10G' (empty -> grow to the whole device). xfs can only grow."""
    f = (fstype or '').lower()
    if f in ('ext4', 'ext3', 'ext2'):
        return ['resize2fs', device] + ([size] if size else [])
    if f == 'btrfs':
        return ['btrfs', 'filesystem', 'resize', (size or 'max'), mount or device]
    if f == 'xfs':
        if not grow:
            return None  # xfs cannot shrink, only grow
        return ['xfs_growfs', mount or device]
    if f == 'ntfs':
        return (['ntfsresize', '-s', size, device] if size else ['ntfsresize', device])
    if f == 'f2fs':
        return ['resize.f2fs', device] + ([size] if size else [])
    return None


def _fs_capabilities():
    """Per-filesystem map of which ops this closure can perform (tool present)."""
    caps = {}
    for fs in _FS_FORMAT:
        fmt_tool = _FS_FORMAT[fs][0]
        fsck_tool = _FS_FSCK_TOOL.get(fs)
        defrag_tool = _FS_DEFRAG_TOOL.get(fs)
        resize_tool = _FS_RESIZE_TOOL.get(fs)
        caps[fs] = {
            'format': bool(fmt_tool and shutil.which(fmt_tool)),
            'fsck': bool(fsck_tool and shutil.which(fsck_tool)),
            'defrag': bool(defrag_tool and shutil.which(defrag_tool)),
            'resize': bool(resize_tool and shutil.which(resize_tool)),
        }
    return caps


def _lsblk_devices():
    """Flat list of block devices (disks + partitions) via lsblk -J, [] on any
    failure. Each entry: name/path/type/size/rota/model/mountpoint/fstype."""
    r = _run(['lsblk', '-J', '-b', '-o',
              'NAME,PATH,TYPE,SIZE,ROTA,MODEL,MOUNTPOINT,FSTYPE'], timeout=8)
    if not r or r.returncode != 0 or not r.stdout:
        return []
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    out = []

    def _walk(nodes):
        for n in nodes:
            name = n.get('name')
            out.append({
                'name': name,
                'path': n.get('path') or (f'/dev/{name}' if name else ''),
                'type': n.get('type'),
                'size': n.get('size'),
                'rota': n.get('rota'),
                'model': (n.get('model') or '').strip(),
                'mountpoint': n.get('mountpoint'),
                'fstype': n.get('fstype'),
            })
            if n.get('children'):
                _walk(n['children'])

    _walk(data.get('blockdevices', []) or [])
    return out


def _read_disk_health_snapshot(path=None):
    """Parse the hart-disk-health.sh boot snapshot into {'ok', 'devices': [...]},
    or None if the file is absent/unreadable (degrade to the live probe)."""
    path = path or _DISK_HEALTH_FILE
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    ok = False
    devs = {}
    for line in lines:
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        if k == 'ok':
            ok = v.strip() == '1'
        elif k.startswith('dev') and '.' in k:
            idx, field = k[3:].split('.', 1)
            devs.setdefault(idx, {})[field] = v
    ordered = [devs[i] for i in sorted(devs, key=lambda x: int(x) if x.isdigit() else 0)]
    return {'ok': ok, 'devices': ordered}


def _disk_health_live():
    """Live per-disk SMART/NVMe health via lsblk + smartctl -j, [] if no disks /
    no smartctl. Coarse 'smart' verdict plus temperature_c + power_on_hours."""
    devices = []
    for d in _lsblk_devices():
        if d.get('type') != 'disk':
            continue
        entry = {
            'name': d.get('name'), 'path': d.get('path'),
            'size': d.get('size'), 'rota': d.get('rota'),
            'model': d.get('model'), 'smart': 'unknown',
        }
        path = d.get('path')
        if path:
            # smartctl -j returns a bitmask exit code; parse stdout regardless of it.
            r = _run(['smartctl', '-j', '-H', '-A', path], timeout=12)
            if r and r.stdout:
                try:
                    data = json.loads(r.stdout)
                    passed = data.get('smart_status', {}).get('passed')
                    if passed is True:
                        entry['smart'] = 'passed'
                    elif passed is False:
                        entry['smart'] = 'failed'
                    temp = (data.get('temperature') or {}).get('current')
                    if temp is not None:
                        entry['temperature_c'] = temp
                    poh = (data.get('power_on_time') or {}).get('hours')
                    if poh is not None:
                        entry['power_on_hours'] = poh
                except (json.JSONDecodeError, ValueError, AttributeError):
                    pass
        devices.append(entry)
    return devices


def _memory_status():
    """RAM + swap totals (psutil) plus zram device detail (zramctl) and
    systemd-oomd liveness. Every section degrades independently to empty."""
    info = {'ram': {}, 'swap': {}, 'zram': [], 'oomd': {'active': None}}
    try:
        import psutil
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        info['ram'] = {
            'total_mb': round(vm.total / 1048576),
            'available_mb': round(vm.available / 1048576),
            'used_mb': round(vm.used / 1048576),
            'percent': vm.percent,
        }
        info['swap'] = {
            'total_mb': round(sw.total / 1048576),
            'used_mb': round(sw.used / 1048576),
            'free_mb': round(sw.free / 1048576),
            'percent': sw.percent,
        }
    except Exception:
        pass
    r = _run(['zramctl', '--output', 'NAME,ALGORITHM,DISKSIZE,DATA',
              '--noheadings'], timeout=5)
    if r and r.returncode == 0:
        for line in r.stdout.strip().split('\n'):
            parts = line.split()
            if len(parts) >= 2:
                info['zram'].append({
                    'name': parts[0], 'algorithm': parts[1],
                    'disksize': parts[2] if len(parts) > 2 else '',
                    'data': parts[3] if len(parts) > 3 else '',
                })
    r2 = _run(['systemctl', 'is-active', 'systemd-oomd'], timeout=5)
    if r2 is not None:
        info['oomd']['active'] = (r2.stdout.strip() == 'active')
    return info


# ─── Bluetooth discovered devices (in-memory) ──────────────────

_bt_discovered = []
_bt_lock = threading.Lock()

# ─── Media index (in-memory cache) ─────────────────────────────

_media_index = {'photos': [], 'music': [], 'videos': [],
                'last_scan': 0, 'scan_dirs': []}
_media_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
# Route registration
# ═══════════════════════════════════════════════════════════════

def _require_system_auth(f):
    """Decorator: require local shell auth for destructive system ops."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import request, jsonify
        remote = request.remote_addr or ''
        if remote not in ('127.0.0.1', '::1', 'localhost'):
            token = request.headers.get('X-Shell-Token', '')
            expected = os.environ.get('HART_SHELL_TOKEN', '')
            if not expected or token != expected:
                return jsonify({'error': 'Unauthorized'}), 403
        return f(*args, **kwargs)
    return decorated


def _audit_system_op(action, detail=None):
    """Log a system operation to the immutable audit log (best-effort)."""
    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            'shell_ops', 'shell_system_api', action,
            detail=detail or {})
    except Exception:
        pass


def register_shell_system_routes(app):
    """Register all system management API routes."""
    from flask import jsonify, request

    # ─── 0. Firewall status ────────────────────────────────
    # PARITY GAP the matrix named (docs/architecture/OS_PARITY_MATRIX.md):
    # HART has a real firewall (networking.firewall / hart.firewall, nftables)
    # but NO way to see it. Worse, shell_manifest.py declares a
    # "Firewall & Firmware" PANEL whose api list points at
    # /api/shell/power/profiles — a POWER endpoint, borrowed "for system
    # status". A panel that cannot show firewall state is a false surface: it
    # looks like the OS has a firewall control and it does not.
    #
    # READ-ONLY on purpose. Opening or closing a port from an unauthenticated
    # local HTTP API is a security decision, not a convenience, and this file
    # already treats destructive process actions with a protected-name list.
    # The declarative half stays the source of truth (networking.firewall in
    # the profile) and the OTA/rebuild path applies changes; this route makes
    # the state VISIBLE and agent-readable, which is what the panel needs and
    # what an agent needs to reason about connectivity.
    #
    # Packaging only: nft/iptables/systemctl are the existing mechanisms.
    @app.route('/api/shell/firewall', methods=['GET'])
    def shell_firewall_status():
        info = {'available': False, 'active': False, 'backend': None,
                'tcp_ports': [], 'udp_ports': [], 'source': None}
        # Which unit is actually running — nftables and iptables are both
        # possible NixOS backends; report the one that is live rather than
        # assuming the configured one took effect.
        for unit, backend in (('nftables.service', 'nftables'),
                              ('firewall.service', 'iptables')):
            r = _run(['systemctl', 'is-active', unit])
            if r and (r.stdout or '').strip() == 'active':
                info.update(available=True, active=True, backend=backend)
                break
        else:
            r = _run(['systemctl', 'is-enabled', 'firewall.service'])
            if r and r.returncode == 0:
                info.update(available=True, backend='iptables')

        # Parse the LIVE ruleset, not the config: what is enforced can differ
        # from what was declared (a failed reload leaves the old ruleset up).
        rules = _run(['nft', 'list', 'ruleset'], timeout=8)
        if rules and rules.returncode == 0 and (rules.stdout or '').strip():
            info['source'] = 'nft'
            # NixOS renders allowed ports as a brace SET on one accept rule
            # ("tcp dport { 22, 6777 } accept"); a single port has no braces.
            # Both shapes, or the ports silently read as empty — the exact
            # mistake that made tests/security.nix red against a CORRECT
            # firewall for weeks.
            for proto, key in (('tcp', 'tcp_ports'), ('udp', 'udp_ports')):
                found = set()
                for line in rules.stdout.splitlines():
                    if f'{proto} dport' not in line or 'accept' not in line:
                        continue
                    seg = line.split(f'{proto} dport', 1)[1]
                    for num in re.findall(r'\d+', seg.split('accept')[0]):
                        found.add(int(num))
                info[key] = sorted(found)
        return jsonify(info)

    # ─── 10. Task / Process Manager ────────────────────────

    _PROTECTED_NAMES = {'init', 'systemd', 'hart-backend', 'hart-agent',
                        'hart-liquid', 'sshd', 'dbus-daemon',
                        'dockerd', 'containerd', 'kubelet', 'kube-apiserver',
                        'kube-controller', 'kube-scheduler', 'etcd',
                        'podman', 'crio', 'runc'}

    @app.route('/api/shell/tasks/processes', methods=['GET'])
    def shell_tasks_processes():
        search = request.args.get('search', '').lower()
        sort_by = request.args.get('sort', 'cpu')
        limit = int(request.args.get('limit', 100))
        try:
            import psutil
        except ImportError:
            return jsonify({'processes': [], 'total': 0, 'error': 'psutil not available'})
        from core.compute_optimizer import iter_processes
        procs = []
        # GIL-safe walker (yields mid-walk) — this task-manager view fetches
        # memory_info + cmdline per PID, the heaviest walk; without yielding, a
        # poll of it would stall the event loop (the #151 class).
        for p in iter_processes(['pid', 'name', 'username', 'cpu_percent',
                                 'memory_percent', 'memory_info', 'status',
                                 'nice', 'num_threads', 'create_time', 'cmdline']):
            try:
                info = p.info
                if search and search not in (info.get('name') or '').lower() and \
                   search not in ' '.join(info.get('cmdline') or []).lower():
                    continue
                mem = info.get('memory_info')
                procs.append({
                    'pid': info['pid'],
                    'name': info.get('name', ''),
                    'username': info.get('username', ''),
                    'cpu_percent': round(info.get('cpu_percent', 0), 1),
                    'memory_percent': round(info.get('memory_percent', 0), 1),
                    'memory_mb': round(mem.rss / 1048576, 1) if mem else 0,
                    'status': info.get('status', ''),
                    'nice': info.get('nice', 0),
                    'threads': info.get('num_threads', 0),
                    'create_time': info.get('create_time', 0),
                    'cmdline': ' '.join(info.get('cmdline') or [])[:200],
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        key = 'cpu_percent' if sort_by == 'cpu' else \
              'memory_percent' if sort_by == 'memory' else \
              'pid' if sort_by == 'pid' else 'cpu_percent'
        procs.sort(key=lambda p: p.get(key, 0), reverse=(sort_by != 'pid'))
        total = len(procs)
        return jsonify({'processes': procs[:limit], 'total': total, 'showing': min(limit, total)})

    @app.route('/api/shell/tasks/kill', methods=['POST'])
    def shell_tasks_kill():
        data = request.get_json(force=True)
        pid = data.get('pid', 0)
        sig_name = data.get('signal', 'SIGTERM')
        if not pid or pid <= 0:
            return jsonify({'error': 'Valid pid required'}), 400
        if pid == 1:
            return jsonify({'error': 'Cannot kill PID 1 (init)'}), 403
        # FAIL CLOSED. This was `except Exception: pass`, which fell through to
        # os.kill() below — so whenever psutil was ABSENT, or Process(pid)
        # raised (AccessDenied on a root-owned process being the ordinary
        # case, i.e. exactly when the target is most likely protected), the
        # protected-name check was skipped ENTIRELY and the kill proceeded.
        # The system reported itself protected and was not.
        #
        # An unverifiable guard is not a passed guard. A caller that cannot be
        # told "no" for the right reason must be told "no" anyway: 503 says
        # the check could not run, which is honest and retryable, rather than
        # 200 "killed" on a process we were never allowed to touch.
        #
        # NoSuchProcess is the ONE benign case — there is nothing to protect —
        # so it falls through to os.kill(), whose ProcessLookupError handler
        # returns the accurate 404.
        try:
            import psutil
        except ImportError:
            logger.warning("tasks/kill REFUSED: psutil unavailable, cannot "
                           "verify the protected-process guard (pid=%s)", pid)
            return jsonify({
                'error': 'Cannot verify protected-process guard (psutil '
                         'unavailable) — refusing to kill',
                'killed': False,
            }), 503
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except psutil.NoSuchProcess:
            name = None          # nothing to protect; os.kill will 404 below
        except Exception as e:
            logger.warning("tasks/kill REFUSED: protected-process guard could "
                           "not run for pid=%s: %s", pid, e)
            return jsonify({
                'error': f'Cannot verify protected-process guard ({type(e).__name__}) '
                         f'— refusing to kill',
                'killed': False,
            }), 503
        if name is not None and name in _PROTECTED_NAMES:
            return jsonify({'error': f'Cannot kill protected process: {name}'}), 403
        sig = getattr(signal, sig_name, signal.SIGTERM)
        try:
            os.kill(pid, sig)
            return jsonify({'killed': True, 'pid': pid, 'signal': sig_name})
        except ProcessLookupError:
            return jsonify({'error': 'Process not found'}), 404
        except PermissionError:
            return jsonify({'error': 'Permission denied'}), 403

    @app.route('/api/shell/tasks/priority', methods=['POST'])
    def shell_tasks_priority():
        data = request.get_json(force=True)
        pid = data.get('pid', 0)
        nice = data.get('nice', 0)
        if not pid:
            return jsonify({'error': 'pid required'}), 400
        try:
            import psutil
            p = psutil.Process(pid)
            p.nice(nice)
            return jsonify({'set': True, 'pid': pid, 'nice': nice})
        except ImportError:
            return jsonify({'error': 'psutil not available'}), 500
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            return jsonify({'error': str(e)}), 400

    # ── Disk encryption (LUKS) ──────────────────────────────────────────
    # hart-luks.nix sets encryption up at INSTALL time; nothing could report
    # it afterwards. Last of the declarative-only rows (tasks #25/#26).
    #
    # READ ONLY, and that is the complete answer rather than half of one:
    # you cannot turn disk encryption on at runtime. It is decided when the
    # volume is created — an "enable" route could only ever lie, or start a
    # destructive re-encrypt behind a GET. Windows shows BitLocker status
    # the same way and sends you to setup for the rest.
    @app.route('/api/shell/encryption/status', methods=['GET'])
    def shell_encryption_status():
        """Which block devices are LUKS-backed, and is root among them?

        `root_encrypted` is the field that matters: full-disk encryption
        that covers a data mount but not root is a common half-configured
        state that reads as "encrypted" to a user and protects far less
        than they think.
        """
        # lsblk is the enumerator udisks2/`/storage/devices` already use, so
        # this reuses the tree rather than inventing a second view of disks.
        r = _run(['lsblk', '-o', 'NAME,TYPE,FSTYPE,MOUNTPOINT', '-P'], timeout=6)
        if r is None:
            return jsonify({'available': False,
                            'error': 'lsblk not available'}), 503

        devices, root_encrypted = [], False
        for line in (r.stdout or '').splitlines():
            f = dict(re.findall(r'(\w+)="([^"]*)"', line))
            if not f:
                continue
            is_luks = (f.get('FSTYPE') == 'crypto_LUKS')
            is_mapper = (f.get('TYPE') == 'crypt')
            if is_luks or is_mapper:
                devices.append({'name': f.get('NAME', ''),
                                'type': f.get('TYPE', ''),
                                'fstype': f.get('FSTYPE', ''),
                                'mountpoint': f.get('MOUNTPOINT') or None,
                                'luks_container': is_luks})
            # An unlocked LUKS volume presents as TYPE=crypt; if THAT is
            # what carries /, root is genuinely on encrypted storage.
            if is_mapper and f.get('MOUNTPOINT') == '/':
                root_encrypted = True

        return jsonify({
            'available': True,
            'root_encrypted': root_encrypted,
            'encrypted_device_count': len(devices),
            'devices': devices,
            # Encryption is an install-time decision — say so, so no caller
            # goes looking for a toggle that cannot exist.
            'runtime_toggle_supported': False,
        })

    # ── Antivirus (ClamAV) ──────────────────────────────────────────────
    # hart-security.nix has run clamd + freshclam since it landed, but there
    # was NO agent-visible surface: the OS scanned, and nothing could ask it
    # what it found. That is the declarative-only gap the parity matrix
    # tracked as ❌ (task #25/#26) — a capability the node HAS but no agent
    # can see or drive. Read + scan only; enable/disable stays declarative,
    # because turning the scanner OFF from an unauthenticated local HTTP API
    # is a security decision, not a convenience (same line the firewall row
    # draws).
    @app.route('/api/shell/antivirus/status', methods=['GET'])
    def shell_antivirus_status():
        """Is the scanner live, and are its signatures current?

        Signature AGE is the load-bearing field. A running clamd with a
        6-month-old database looks healthy and catches nothing, which is the
        failure mode worth surfacing — same shape as the `unclaimed` flag on
        the device tree.
        """
        r = _run(['systemctl', 'is-active', 'clamav-daemon'], timeout=5)
        running = bool(r and (r.stdout or '').strip() == 'active')

        # freshclam writes the signature DB here; mtime is its freshness.
        db_age_days, db_present = None, False
        for db_dir in ('/var/lib/clamav',):
            try:
                newest = max(
                    (os.path.getmtime(os.path.join(db_dir, f))
                     for f in os.listdir(db_dir)
                     if f.endswith(('.cvd', '.cld'))),
                    default=None)
            except OSError:
                # Absent on a non-NixOS dev box / before first freshclam run.
                newest = None
            if newest:
                db_present = True
                db_age_days = round((time.time() - newest) / 86400, 1)
        return jsonify({
            'running': running,
            'signatures_present': db_present,
            'signature_age_days': db_age_days,
            # Explicitly stale rather than making every caller re-derive it.
            'signatures_stale': (db_age_days is not None and db_age_days > 7),
        })

    @app.route('/api/shell/antivirus/scan', methods=['POST'])
    def shell_antivirus_scan():
        """Scan a path with clamdscan (the DAEMON client, not clamscan).

        clamdscan hands the work to the already-running clamd, so the
        signature DB is not re-loaded per invocation — clamscan would spend
        ~30s and hundreds of MB doing exactly that every call.

        Bounded via _run_async_bounded: a scan of a large tree runs for
        minutes, and a synchronous call would pin a pool thread and queue
        every other shell fetch behind it (the click-to-freeze class this
        module already documents). The caller gets `finished: false` and the
        scan continues out-of-band rather than the request hanging.
        """
        data = request.get_json(silent=True) or {}
        target = (data.get('path') or '').strip()
        if not target:
            return jsonify({'error': 'path required'}), 400
        # Reject traversal + relative paths outright: this endpoint chooses
        # what clamd reads, so it must never accept a caller-composed path
        # it has not resolved.
        target = os.path.abspath(target)
        if not os.path.exists(target):
            return jsonify({'error': 'path not found', 'path': target}), 404

        finished, r = _run_async_bounded(
            ['clamdscan', '--fdpass', '--no-summary', target],
            run_timeout=900, wait=8, name='hart-av-scan')
        if not finished:
            return jsonify({'scanning': True, 'finished': False, 'path': target,
                            'note': 'scan continues in background'}), 202
        if r is None:
            return jsonify({'ok': False, 'error': 'clamdscan not available'}), 503
        # clamdscan exit codes: 0 = clean, 1 = infected, 2 = error.
        infected = [ln for ln in (r.stdout or '').splitlines()
                    if ln.strip().endswith('FOUND')]
        return jsonify({
            'ok': r.returncode in (0, 1),
            'finished': True,
            'path': target,
            'clean': r.returncode == 0,
            'infected_count': len(infected),
            'infected': infected[:100],
            'error': (r.stderr or '').strip() if r.returncode == 2 else None,
        }), (200 if r.returncode in (0, 1) else 503)

    @app.route('/api/shell/tasks/resources', methods=['GET'])
    def shell_tasks_resources():
        res = {'cpu': {}, 'ram': {}, 'gpu': None, 'disk_io': {}, 'network_io': {}}
        try:
            import psutil
            cpu_freq = psutil.cpu_freq()
            res['cpu'] = {
                'percent': psutil.cpu_percent(interval=0.1),
                'count': psutil.cpu_count(),
                'freq_mhz': round(cpu_freq.current) if cpu_freq else 0,
                'per_cpu': psutil.cpu_percent(percpu=True),
            }
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            res['ram'] = {
                'total_gb': round(mem.total / 1073741824, 1),
                'used_gb': round(mem.used / 1073741824, 1),
                'percent': mem.percent,
                'swap_total_gb': round(swap.total / 1073741824, 1),
                'swap_used_gb': round(swap.used / 1073741824, 1),
            }
            dio = psutil.disk_io_counters()
            if dio:
                res['disk_io'] = {
                    'read_bytes': dio.read_bytes,
                    'write_bytes': dio.write_bytes,
                }
            nio = psutil.net_io_counters()
            if nio:
                res['network_io'] = {
                    'bytes_sent': nio.bytes_sent,
                    'bytes_recv': nio.bytes_recv,
                }
        except ImportError:
            pass
        try:
            from integrations.service_tools.vram_manager import detect_gpu
            gpu = detect_gpu()
            if gpu:
                # detect_gpu contract: {name, total_gb, free_gb, cuda_available}.
                # memory_gb must read total_gb — the prior 'vram_mb'/'utilization'/
                # 'temperature' keys never existed in that dict, so the Task-Manager
                # GPU panel showed 0 GB forever (#152 wrong-key class). util/temp
                # aren't exposed by detect_gpu yet → honest 0 until a smi probe adds them.
                res['gpu'] = {
                    'name': gpu.get('name') or '',
                    'memory_gb': round(gpu.get('total_gb', 0), 1),
                    'memory_free_gb': round(gpu.get('free_gb', 0), 1),
                    'utilization': 0,
                    'temperature': 0,
                }
        except (ImportError, Exception):
            pass
        return jsonify(res)

    # ─── 11. Storage Manager ───────────────────────────────

    @app.route('/api/shell/storage', methods=['GET'])
    def shell_storage():
        try:
            import psutil
        except ImportError:
            return jsonify({'partitions': [], 'error': 'psutil not available'})
        # Exclude read-only image mounts (squashfs / iso9660 on the live ISO, any ro
        # loop image) and overlay/pseudo filesystems: the squashfs Nix store is ALWAYS
        # 100% full by nature, so counting it made the Storage panel alarm at ~100% for
        # no real reason (audit #0.4 -- the "Disk 100%" seen on the live ISO). psutil's
        # all=False does NOT drop these (a squashfs has a real loop device + fstype), so
        # filter explicitly; only writable, real storage is aggregated.
        _SKIP_FSTYPES = {'squashfs', 'iso9660', 'overlay', 'ramfs'}
        partitions = []
        for part in psutil.disk_partitions(all=False):
            if (part.fstype or '').lower() in _SKIP_FSTYPES:
                continue
            if 'ro' in (part.opts or '').split(','):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                partitions.append({
                    'device': part.device,
                    'mount': part.mountpoint,
                    'fstype': part.fstype,
                    'total_gb': round(usage.total / 1073741824, 1),
                    'used_gb': round(usage.used / 1073741824, 1),
                    'free_gb': round(usage.free / 1073741824, 1),
                    'percent': usage.percent,
                })
            except (PermissionError, OSError):
                pass
        total = sum(p['total_gb'] for p in partitions)
        used = sum(p['used_gb'] for p in partitions)
        return jsonify({
            'partitions': partitions,
            'total_gb': round(total, 1),
            'used_gb': round(used, 1),
            'overall_percent': round(used / total * 100, 1) if total > 0 else 0,
        })

    @app.route('/api/shell/storage/usage', methods=['GET'])
    def shell_storage_usage():
        path = request.args.get('path', os.path.expanduser('~'))
        if not os.path.isdir(path):
            return jsonify({'error': 'Valid directory path required'}), 400
        children = []
        try:
            for entry in os.scandir(path):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        r = _run(['du', '-sm', entry.path], timeout=5)
                        size_mb = _first_int(r) if r and r.returncode == 0 else 0
                    else:
                        size_mb = round(entry.stat().st_size / 1048576, 1)
                    children.append({
                        'name': entry.name, 'path': entry.path,
                        'size_mb': size_mb, 'is_dir': entry.is_dir(),
                    })
                except (OSError, ValueError):
                    pass
        except PermissionError:
            return jsonify({'error': 'Permission denied'}), 403
        children.sort(key=lambda c: c['size_mb'], reverse=True)
        total = sum(c['size_mb'] for c in children)
        return jsonify({'path': path, 'total_size_mb': round(total, 1),
                        'children': children[:100]})

    @app.route('/api/shell/storage/cleanup', methods=['GET'])
    def shell_storage_cleanup():
        home = os.path.expanduser('~')
        reclaimable = []
        for cat, path, desc in [
            ('cache', os.path.join(home, '.cache'), 'Application caches'),
            ('temp', '/tmp', 'Temporary files'),
            ('trash', os.path.join(home, '.local/share/Trash'), 'Trash bin'),
            ('journal', '/var/log/journal', 'System journal logs'),
        ]:
            if os.path.isdir(path):
                r = _run(['du', '-sm', path], timeout=10)
                size = _first_int(r) if r and r.returncode == 0 else 0
                reclaimable.append({
                    'category': cat, 'path': path,
                    'size_mb': size, 'description': desc,
                })
        r = _run(['nix-store', '--gc', '--print-dead'], timeout=15)
        if r and r.returncode == 0:
            dead_lines = r.stdout.strip().split('\n')
            reclaimable.append({
                'category': 'nix_old', 'path': '/nix/store',
                'size_mb': len(dead_lines) * 10,
                'description': f'Old Nix generations (~{len(dead_lines)} store paths)',
            })
        total = sum(r['size_mb'] for r in reclaimable)
        return jsonify({'reclaimable': reclaimable, 'total_reclaimable_mb': total})

    @app.route('/api/shell/storage/clean', methods=['POST'])
    def shell_storage_clean():
        data = request.get_json(force=True)
        categories = data.get('categories', [])
        if not categories:
            return jsonify({'error': 'categories required'}), 400
        home = os.path.expanduser('~')
        freed = {}
        for cat in categories:
            if cat == 'cache':
                cache_dir = os.path.join(home, '.cache')
                r = _run(['du', '-sm', cache_dir], timeout=5)
                size = _first_int(r) if r and r.returncode == 0 else 0
                _run(['find', cache_dir, '-type', 'f', '-atime', '+7', '-delete'], timeout=30)
                freed['cache'] = size
            elif cat == 'temp':
                r = _run(['du', '-sm', '/tmp'], timeout=5)
                size = _first_int(r) if r and r.returncode == 0 else 0
                _run(['find', '/tmp', '-user', os.environ.get('USER', 'hart'),
                      '-type', 'f', '-mtime', '+1', '-delete'], timeout=30)
                freed['temp'] = size
            elif cat == 'trash':
                trash = os.path.join(home, '.local/share/Trash')
                r = _run(['du', '-sm', trash], timeout=5)
                size = _first_int(r) if r and r.returncode == 0 else 0
                _run(['gio', 'trash', '--empty'], timeout=15)
                freed['trash'] = size
            elif cat == 'nix_old':
                _run(['nix-collect-garbage', '-d'], timeout=120)
                freed['nix_old'] = 0
            elif cat == 'journal':
                _run(['journalctl', '--vacuum-time=7d'], timeout=30)
                freed['journal'] = 0
        total = sum(freed.values())
        return jsonify({'cleaned': True, 'freed_mb': total, 'details': freed})

    @app.route('/api/shell/storage/smart', methods=['GET'])
    def shell_storage_smart():
        device = request.args.get('device', '')
        if not device:
            return jsonify({'error': 'device required (e.g. /dev/nvme0n1)'}), 400
        r = _run(['smartctl', '-j', '-a', device], timeout=15)
        if not r or r.returncode not in (0, 4):  # 4 = some attributes failed
            return jsonify({'error': 'smartctl not available or device not found'}), 500
        try:
            data = json.loads(r.stdout)
            health = data.get('smart_status', {}).get('passed', True)
            temp = data.get('temperature', {}).get('current', 0)
            poh = data.get('power_on_time', {}).get('hours', 0)
            return jsonify({
                'device': device, 'healthy': health,
                'temperature_c': temp, 'power_on_hours': poh,
                'model': data.get('model_name', ''),
                'serial': data.get('serial_number', ''),
                'firmware': data.get('firmware_version', ''),
            })
        except (json.JSONDecodeError, KeyError):
            return jsonify({'error': 'Failed to parse smartctl output'}), 500

    # ─── 11b. Disk Utility (#157): devices / health / capabilities / ops ──────
    # The defrag / shrink-grow / format-ALL-FS / fsck-repair / trim surface. The
    # tooling is shipped by hart-storage.nix; these routes INVOKE it. Read-only
    # ops are open; mutating ops are auth-gated (@_require_system_auth) and the two
    # DESTRUCTIVE ops (format, resize) ALSO require an explicit confirm=true AND
    # refuse a mounted or system (root/boot/nix) disk, so the running OS can never
    # be wiped by a stray click. A slow op returns 202 'running' (out-of-band) so
    # the small shell thread pool is never pinned (the same _run_async_bounded
    # discipline as wifi-connect).

    @app.route('/api/shell/storage/devices', methods=['GET'])
    def shell_storage_devices():
        """List block devices (disks + partitions) with size/rota/model/mount/fs."""
        return jsonify({'devices': _lsblk_devices()})

    @app.route('/api/shell/storage/health', methods=['GET'])
    def shell_storage_health():
        """Per-disk SMART/NVMe health. Prefers the live smartctl readout; falls
        back to the hart-disk-health boot snapshot (/run/hart/disk-health)."""
        live = _disk_health_live()
        if live:
            return jsonify({'source': 'live', 'devices': live})
        snap = _read_disk_health_snapshot()
        if snap and snap.get('devices'):
            return jsonify({'source': 'snapshot', 'devices': snap['devices']})
        return jsonify({'source': 'none', 'devices': []})

    @app.route('/api/shell/storage/capabilities', methods=['GET'])
    def shell_storage_capabilities():
        """Which disk ops this build can actually perform, per filesystem."""
        return jsonify({
            'supported_filesystems': list(_FS_FORMAT.keys()),
            'filesystems': _fs_capabilities(),
        })

    @app.route('/api/shell/storage/fsck', methods=['POST'])
    @_require_system_auth
    def shell_storage_fsck():
        """Check (read-only, default) or repair a filesystem. Repair is refused on
        a mounted or system disk (must be unmounted first)."""
        data = request.get_json(force=True, silent=True) or {}
        device = data.get('device', '')
        fstype = data.get('fstype', '')
        repair = bool(data.get('repair', False))
        if not _valid_device(device):
            return jsonify({'error': 'valid /dev device required'}), 400
        if repair and _is_mounted(device):
            return jsonify({'error': 'cannot repair a mounted filesystem, unmount it first'}), 409
        if repair and _is_protected_device(device):
            return jsonify({'error': 'refusing to repair a system disk (root/boot/nix)'}), 403
        if not fstype:
            fstype = _device_fstype(device)
        cmd = _fsck_cmd(fstype, device, repair)
        if not cmd:
            return jsonify({'error': f'fsck not supported for fstype: {fstype or "unknown"}'}), 400
        if not shutil.which(cmd[0]):
            return jsonify({'error': f'{cmd[0]} not available'}), 500
        finished, r = _run_async_bounded(cmd, run_timeout=120, wait=8, name='hart-fsck')
        if not finished:
            return jsonify({'running': True, 'device': device,
                            'mode': 'repair' if repair else 'check'}), 202
        _audit_system_op('storage_fsck', {'device': device, 'repair': repair})
        # 0 = clean; 1 = errors corrected (fsck convention) -> both are a success.
        ok = bool(r and r.returncode in (0, 1))
        return jsonify({
            'ok': ok, 'device': device, 'mode': 'repair' if repair else 'check',
            'returncode': r.returncode if r else None,
            'output': (r.stdout or r.stderr or '')[-4000:] if r else '',
        })

    @app.route('/api/shell/storage/defrag', methods=['POST'])
    @_require_system_auth
    def shell_storage_defrag():
        """Defragment a mounted filesystem (ext4 / btrfs / xfs). Non-destructive."""
        data = request.get_json(force=True, silent=True) or {}
        target = data.get('mount', '') or data.get('path', '')
        fstype = data.get('fstype', '')
        if not target or not os.path.isdir(target):
            return jsonify({'error': 'valid mount path required'}), 400
        if not fstype:
            fstype = _path_fstype(target)
        cmd = _defrag_cmd(fstype, target)
        if not cmd:
            return jsonify({'error': f'defrag not supported for fstype: {fstype or "unknown"} '
                                     '(log-structured / flash filesystems do not defragment)'}), 400
        if not shutil.which(cmd[0]):
            return jsonify({'error': f'{cmd[0]} not available'}), 500
        finished, r = _run_async_bounded(cmd, run_timeout=300, wait=8, name='hart-defrag')
        if not finished:
            return jsonify({'running': True, 'mount': target, 'fstype': fstype}), 202
        _audit_system_op('storage_defrag', {'mount': target, 'fstype': fstype})
        return jsonify({
            'ok': bool(r and r.returncode == 0), 'mount': target, 'fstype': fstype,
            'output': (r.stdout or r.stderr or '')[-4000:] if r else '',
        })

    @app.route('/api/shell/storage/trim', methods=['POST'])
    @_require_system_auth
    def shell_storage_trim():
        """Discard unused blocks on an SSD mount (fstrim). Safe, non-destructive."""
        data = request.get_json(force=True, silent=True) or {}
        target = data.get('mount', '') or '/'
        if not os.path.isdir(target):
            return jsonify({'error': 'valid mount path required'}), 400
        r = _run(['fstrim', '-v', target], timeout=60)
        if r is None:
            return jsonify({'error': 'fstrim not available'}), 500
        _audit_system_op('storage_trim', {'mount': target})
        return jsonify({'ok': r.returncode == 0, 'mount': target,
                        'output': (r.stdout or r.stderr or '').strip()})

    @app.route('/api/shell/storage/format', methods=['POST'])
    @_require_system_auth
    def shell_storage_format():
        """Format a device to any of the 7 supported filesystems. DESTRUCTIVE:
        requires confirm=true AND refuses a mounted or system (root/boot/nix) disk."""
        data = request.get_json(force=True, silent=True) or {}
        device = data.get('device', '')
        fstype = (data.get('fstype', '') or '').lower()
        label = data.get('label', '')
        confirm = bool(data.get('confirm', False))
        if not _valid_device(device):
            return jsonify({'error': 'valid /dev device required'}), 400
        if fstype not in _FS_FORMAT:
            return jsonify({'error': f'unsupported fstype: {fstype or "(none)"}. '
                                     f'Supported: {list(_FS_FORMAT.keys())}'}), 400
        if not confirm:
            return jsonify({'error': 'format is destructive: pass confirm=true to proceed',
                            'requires_confirm': True}), 400
        if _is_protected_device(device):
            return jsonify({'error': 'refusing to format a system disk (root/boot/nix)'}), 403
        if _is_mounted(device):
            return jsonify({'error': 'device is mounted, unmount it before formatting'}), 409
        cmd = list(_FS_FORMAT[fstype])
        if label and _FS_LABEL_FLAG.get(fstype):
            cmd += [_FS_LABEL_FLAG[fstype], str(label)]
        cmd.append(device)
        if not shutil.which(cmd[0]):
            return jsonify({'error': f'{cmd[0]} not available'}), 500
        finished, r = _run_async_bounded(cmd, run_timeout=120, wait=8, name='hart-format')
        if not finished:
            return jsonify({'running': True, 'device': device, 'fstype': fstype}), 202
        _audit_system_op('storage_format', {'device': device, 'fstype': fstype, 'label': label})
        return jsonify({
            'ok': bool(r and r.returncode == 0), 'device': device, 'fstype': fstype,
            'output': (r.stdout or r.stderr or '')[-4000:] if r else '',
        })

    @app.route('/api/shell/storage/resize', methods=['POST'])
    @_require_system_auth
    def shell_storage_resize():
        """Shrink or grow a filesystem. DESTRUCTIVE (shrink risks data): requires
        confirm=true, refuses a system disk, and refuses shrinking a mounted FS."""
        data = request.get_json(force=True, silent=True) or {}
        device = data.get('device', '')
        mount = data.get('mount', '')
        fstype = (data.get('fstype', '') or '').lower()
        size = data.get('size', '')  # e.g. '10G'; empty = grow to the whole device
        grow = bool(data.get('grow', True))
        confirm = bool(data.get('confirm', False))
        if not mount and not _valid_device(device):
            return jsonify({'error': 'device or mount required'}), 400
        if not confirm:
            return jsonify({'error': 'resize is destructive: pass confirm=true to proceed',
                            'requires_confirm': True}), 400
        if device and _is_protected_device(device):
            return jsonify({'error': 'refusing to resize a system disk (root/boot/nix)'}), 403
        if not fstype:
            fstype = _device_fstype(device) or _path_fstype(mount)
        cmd = _resize_cmd(fstype, device, mount, size, grow)
        if not cmd:
            return jsonify({'error': f'{"grow" if grow else "shrink"} not supported '
                                     f'for fstype: {fstype or "unknown"}'}), 400
        if not grow and device and _is_mounted(device):
            return jsonify({'error': 'shrink requires the filesystem unmounted'}), 409
        if not shutil.which(cmd[0]):
            return jsonify({'error': f'{cmd[0]} not available'}), 500
        finished, r = _run_async_bounded(cmd, run_timeout=180, wait=8, name='hart-resize')
        if not finished:
            return jsonify({'running': True, 'fstype': fstype}), 202
        _audit_system_op('storage_resize',
                         {'device': device, 'mount': mount, 'fstype': fstype,
                          'size': size, 'grow': grow})
        return jsonify({
            'ok': bool(r and r.returncode == 0), 'fstype': fstype,
            'output': (r.stdout or r.stderr or '')[-4000:] if r else '',
        })

    # ─── 11c. Memory (#157): zram/swap status + cache reclaim ─────────────────

    @app.route('/api/shell/memory', methods=['GET'])
    def shell_memory():
        """RAM + swap totals, zram device detail, and systemd-oomd liveness."""
        return jsonify(_memory_status())

    @app.route('/api/shell/memory/drop-caches', methods=['POST'])
    @_require_system_auth
    def shell_memory_drop_caches():
        """Reclaim clean pagecache/dentries/inodes (sync first, then drop). Never
        loses dirty data; the kernel re-reads on demand. Linux-only."""
        try:
            _run(['sync'], timeout=15)
            with open('/proc/sys/vm/drop_caches', 'w') as f:
                f.write('3\n')
            _audit_system_op('memory_drop_caches', {})
            return jsonify({'ok': True})
        except (FileNotFoundError, PermissionError, OSError) as e:
            # 503: /proc/sys/vm is a Linux facility; absent/denied = unavailable,
            # a controlled degrade -- not a crash.
            return jsonify({'ok': False, 'error': str(e)}), 503

    # ─── 12. Startup Apps Manager ──────────────────────────

    def _parse_desktop_file(path):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(path, encoding='utf-8')
        if not cp.has_section('Desktop Entry'):
            return None
        entry = cp['Desktop Entry']
        hidden = entry.get('Hidden', 'false').lower() == 'true'
        enabled_key = entry.get('X-GNOME-Autostart-enabled', 'true')
        enabled = enabled_key.lower() != 'false' and not hidden
        return {
            'name': entry.get('Name', os.path.basename(path)),
            'exec': entry.get('Exec', ''),
            'icon': entry.get('Icon', ''),
            'comment': entry.get('Comment', ''),
            'enabled': enabled,
            'file': path,
            'system': path.startswith('/etc/') or path.startswith('/run/'),
        }

    @app.route('/api/shell/startup', methods=['GET'])
    def shell_startup():
        entries = []
        dirs = ['/etc/xdg/autostart', os.path.expanduser('~/.config/autostart')]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if not f.endswith('.desktop'):
                    continue
                info = _parse_desktop_file(os.path.join(d, f))
                if info:
                    entries.append(info)
        return jsonify({'entries': entries, 'count': len(entries)})

    @app.route('/api/shell/startup/toggle', methods=['POST'])
    def shell_startup_toggle():
        data = request.get_json(force=True)
        filepath = data.get('file', '')
        enabled = data.get('enabled', True)
        if not filepath:
            return jsonify({'error': 'file required'}), 400
        filepath = os.path.expanduser(filepath)
        if not os.path.isfile(filepath):
            return jsonify({'error': 'File not found'}), 404
        if filepath.startswith('/etc/') or filepath.startswith('/run/'):
            user_dir = os.path.expanduser('~/.config/autostart')
            os.makedirs(user_dir, exist_ok=True)
            user_copy = os.path.join(user_dir, os.path.basename(filepath))
            if not os.path.exists(user_copy):
                import shutil
                shutil.copy2(filepath, user_copy)
            filepath = user_copy
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(filepath, encoding='utf-8')
        if not cp.has_section('Desktop Entry'):
            cp.add_section('Desktop Entry')
        cp.set('Desktop Entry', 'Hidden', 'false' if enabled else 'true')
        cp.set('Desktop Entry', 'X-GNOME-Autostart-enabled', str(enabled).lower())
        with open(filepath, 'w') as f:
            cp.write(f)
        return jsonify({'toggled': True, 'file': filepath, 'enabled': enabled})

    @app.route('/api/shell/startup/add', methods=['POST'])
    def shell_startup_add():
        data = request.get_json(force=True)
        name = data.get('name', '')
        exec_cmd = data.get('exec', '')
        if not name or not exec_cmd:
            return jsonify({'error': 'name and exec required'}), 400
        user_dir = os.path.expanduser('~/.config/autostart')
        os.makedirs(user_dir, exist_ok=True)
        safe_name = name.lower().replace(' ', '-')
        filepath = os.path.join(user_dir, f'{safe_name}.desktop')
        content = f"""[Desktop Entry]
Type=Application
Name={name}
Exec={exec_cmd}
Comment={data.get('comment', '')}
X-GNOME-Autostart-enabled=true
Hidden=false
"""
        with open(filepath, 'w') as f:
            f.write(content)
        return jsonify({'added': True, 'file': filepath, 'name': name})

    @app.route('/api/shell/startup/remove', methods=['POST'])
    def shell_startup_remove():
        data = request.get_json(force=True)
        filepath = data.get('file', '')
        if not filepath:
            return jsonify({'error': 'file required'}), 400
        filepath = os.path.expanduser(filepath)
        if filepath.startswith('/etc/') or filepath.startswith('/run/'):
            return jsonify({'error': 'Cannot remove system startup entries'}), 403
        if os.path.isfile(filepath):
            os.remove(filepath)
            return jsonify({'removed': True, 'file': filepath})
        return jsonify({'error': 'File not found'}), 404

    # ─── 13. Bluetooth Full Management ─────────────────────

    def _bt_run(cmd_str, timeout=5):
        return _run(['bluetoothctl'] + cmd_str.split(), timeout=timeout)

    @app.route('/api/shell/bluetooth/status', methods=['GET'])
    def shell_bt_status():
        info = {'powered': False, 'discoverable': False, 'pairable': False,
                'controller': {}, 'devices': []}
        r = _bt_run('show')
        if r and r.returncode == 0:
            for line in r.stdout.split('\n'):
                line = line.strip()
                if line.startswith('Controller'):
                    parts = line.split()
                    info['controller'] = {'address': parts[1] if len(parts) > 1 else ''}
                elif 'Powered:' in line:
                    info['powered'] = 'yes' in line.lower()
                elif 'Discoverable:' in line:
                    info['discoverable'] = 'yes' in line.lower()
                elif 'Pairable:' in line:
                    info['pairable'] = 'yes' in line.lower()
                elif 'Name:' in line and not info['controller'].get('name'):
                    info['controller']['name'] = line.split(':', 1)[1].strip()
        r2 = _bt_run('devices')
        if r2 and r2.returncode == 0:
            for line in r2.stdout.strip().split('\n'):
                parts = line.strip().split()
                if len(parts) >= 3 and parts[0] == 'Device':
                    mac = parts[1]
                    name = ' '.join(parts[2:])
                    dev = {'mac': mac, 'name': name, 'paired': True}
                    r3 = _bt_run(f'info {mac}')
                    if r3 and r3.returncode == 0:
                        for dline in r3.stdout.split('\n'):
                            dline = dline.strip()
                            if 'Connected:' in dline:
                                dev['connected'] = 'yes' in dline.lower()
                            elif 'Trusted:' in dline:
                                dev['trusted'] = 'yes' in dline.lower()
                            elif 'Icon:' in dline:
                                dev['icon'] = dline.split(':', 1)[1].strip()
                    info['devices'].append(dev)
        return jsonify(info)

    @app.route('/api/shell/bluetooth/scan', methods=['POST'])
    def shell_bt_scan():
        data = request.get_json(force=True)
        duration = data.get('duration', 10)
        with _bt_lock:
            _bt_discovered.clear()

        def _do_scan():
            r = _run(['bluetoothctl', '--timeout', str(duration), 'scan', 'on'],
                      timeout=duration + 5)
            if r and r.returncode == 0:
                with _bt_lock:
                    for line in r.stdout.split('\n'):
                        if 'NEW' in line and 'Device' in line:
                            parts = line.strip().split()
                            for i, p in enumerate(parts):
                                if ':' in p and len(p) == 17:
                                    mac = p
                                    name = ' '.join(parts[i + 1:])
                                    _bt_discovered.append({'mac': mac, 'name': name})
                                    break

        threading.Thread(target=_do_scan, daemon=True).start()
        return jsonify({'scanning': True, 'duration': duration})

    @app.route('/api/shell/bluetooth/discovered', methods=['GET'])
    def shell_bt_discovered():
        with _bt_lock:
            devices = list(_bt_discovered)
        return jsonify({'devices': devices, 'count': len(devices)})

    @app.route('/api/shell/bluetooth/pair', methods=['POST'])
    def shell_bt_pair():
        data = request.get_json(force=True)
        mac = data.get('mac', '')
        if not mac:
            return jsonify({'error': 'mac required'}), 400
        r = _bt_run(f'pair {mac}', timeout=15)
        ok = r and r.returncode == 0
        return jsonify({'paired': ok, 'mac': mac,
                        'error': '' if ok else (r.stderr.strip() if r else 'bluetoothctl not available')})

    @app.route('/api/shell/bluetooth/connect', methods=['POST'])
    def shell_bt_connect():
        data = request.get_json(force=True)
        mac = data.get('mac', '')
        if not mac:
            return jsonify({'error': 'mac required'}), 400
        r = _bt_run(f'connect {mac}', timeout=15)
        ok = r and r.returncode == 0
        return jsonify({'connected': ok, 'mac': mac})

    @app.route('/api/shell/bluetooth/disconnect', methods=['POST'])
    def shell_bt_disconnect():
        data = request.get_json(force=True)
        mac = data.get('mac', '')
        if not mac:
            return jsonify({'error': 'mac required'}), 400
        r = _bt_run(f'disconnect {mac}')
        ok = r and r.returncode == 0
        return jsonify({'disconnected': ok, 'mac': mac})

    @app.route('/api/shell/bluetooth/trust', methods=['POST'])
    def shell_bt_trust():
        data = request.get_json(force=True)
        mac = data.get('mac', '')
        trusted = data.get('trusted', True)
        if not mac:
            return jsonify({'error': 'mac required'}), 400
        cmd = 'trust' if trusted else 'untrust'
        r = _bt_run(f'{cmd} {mac}')
        ok = r and r.returncode == 0
        return jsonify({'trusted': trusted if ok else not trusted, 'mac': mac})

    @app.route('/api/shell/bluetooth/remove', methods=['POST'])
    def shell_bt_remove():
        data = request.get_json(force=True)
        mac = data.get('mac', '')
        if not mac:
            return jsonify({'error': 'mac required'}), 400
        r = _bt_run(f'remove {mac}')
        ok = r and r.returncode == 0
        return jsonify({'removed': ok, 'mac': mac})

    @app.route('/api/shell/bluetooth/power', methods=['POST'])
    def shell_bt_power():
        data = request.get_json(force=True)
        powered = data.get('powered', True)
        val = 'on' if powered else 'off'
        r = _bt_run(f'power {val}')
        ok = r and r.returncode == 0
        return jsonify({'powered': powered if ok else not powered})

    # ─── 14. Print Manager (CUPS) ──────────────────────────

    @app.route('/api/shell/printers', methods=['GET'])
    def shell_printers():
        printers = []
        cups_running = False
        r = _run(['lpstat', '-p', '-d'])
        if r and r.returncode == 0:
            cups_running = True
            default_printer = ''
            for line in r.stdout.strip().split('\n'):
                if line.startswith('printer'):
                    parts = line.split()
                    if len(parts) >= 2:
                        name = parts[1]
                        state = 'idle' if 'idle' in line.lower() else \
                                'printing' if 'printing' in line.lower() else 'disabled'
                        printers.append({
                            'name': name, 'state': state,
                            'accepting': 'disabled' not in line.lower(),
                            'default': False,
                        })
                elif 'system default destination' in line.lower():
                    default_printer = line.split(':')[-1].strip()
            for p in printers:
                if p['name'] == default_printer:
                    p['default'] = True
            r2 = _run(['lpstat', '-v'])
            if r2 and r2.returncode == 0:
                for line in r2.stdout.strip().split('\n'):
                    if 'device for' in line.lower():
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            pname = parts[0].split()[-1]
                            uri = parts[1].strip()
                            for p in printers:
                                if p['name'] == pname:
                                    p['uri'] = uri
        return jsonify({
            'printers': printers,
            'default': next((p['name'] for p in printers if p.get('default')), ''),
            'cups_running': cups_running,
        })

    @app.route('/api/shell/printers/jobs', methods=['GET'])
    def shell_printer_jobs():
        printer = request.args.get('printer', '')
        cmd = ['lpstat', '-W', 'all']
        if printer:
            cmd.extend(['-p', printer])
        r = _run(cmd)
        jobs = []
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 4:
                    jobs.append({
                        'id': parts[0],
                        'user': parts[1] if len(parts) > 1 else '',
                        'size': parts[2] if len(parts) > 2 else '',
                        'state': 'pending',
                    })
        return jsonify({'jobs': jobs, 'count': len(jobs)})

    @app.route('/api/shell/printers/add', methods=['POST'])
    def shell_printer_add():
        data = request.get_json(force=True)
        uri = data.get('uri', '')
        name = data.get('name', '')
        driver = data.get('driver', 'everywhere')
        if not uri or not name:
            return jsonify({'error': 'uri and name required'}), 400
        r = _run(['lpadmin', '-p', name, '-E', '-v', uri, '-m', driver], timeout=30)
        ok = r and r.returncode == 0
        return jsonify({'added': ok, 'name': name,
                        'error': r.stderr.strip() if r and not ok else ''})

    @app.route('/api/shell/printers/remove', methods=['POST'])
    def shell_printer_remove():
        data = request.get_json(force=True)
        name = data.get('name', '')
        if not name:
            return jsonify({'error': 'name required'}), 400
        r = _run(['lpadmin', '-x', name])
        ok = r and r.returncode == 0
        return jsonify({'removed': ok, 'name': name})

    @app.route('/api/shell/printers/set-default', methods=['POST'])
    def shell_printer_set_default():
        data = request.get_json(force=True)
        name = data.get('name', '')
        if not name:
            return jsonify({'error': 'name required'}), 400
        r = _run(['lpoptions', '-d', name])
        ok = r and r.returncode == 0
        return jsonify({'set': ok, 'default': name})

    @app.route('/api/shell/printers/test', methods=['POST'])
    def shell_printer_test():
        data = request.get_json(force=True)
        name = data.get('name', '')
        if not name:
            return jsonify({'error': 'name required'}), 400
        test_file = '/usr/share/cups/data/testprint.ps'
        if not os.path.isfile(test_file):
            test_file = '/dev/null'
        r = _run(['lp', '-d', name, test_file])
        ok = r and r.returncode == 0
        return jsonify({'printed': ok, 'printer': name})

    @app.route('/api/shell/printers/cancel', methods=['POST'])
    def shell_printer_cancel():
        data = request.get_json(force=True)
        job_id = data.get('job_id', '')
        if not job_id:
            return jsonify({'error': 'job_id required'}), 400
        r = _run(['cancel', str(job_id)])
        ok = r and r.returncode == 0
        return jsonify({'cancelled': ok, 'job_id': job_id})

    # ─── 15. Media Indexer ─────────────────────────────────

    @app.route('/api/shell/media/status', methods=['GET'])
    def shell_media_status():
        with _media_lock:
            return jsonify({
                'indexed': _media_index['last_scan'] > 0,
                'last_scan': _media_index['last_scan'],
                'counts': {
                    'photos': len(_media_index['photos']),
                    'music': len(_media_index['music']),
                    'videos': len(_media_index['videos']),
                },
                'scan_directories': _media_index['scan_dirs'],
            })

    @app.route('/api/shell/media/scan', methods=['POST'])
    def shell_media_scan():
        data = request.get_json(force=True)
        directories = data.get('directories', [])
        if not directories:
            home = os.path.expanduser('~')
            directories = [
                os.path.join(home, 'Pictures'),
                os.path.join(home, 'Videos'),
                os.path.join(home, 'Music'),
            ]

        def _do_scan():
            photos, music, videos = [], [], []
            photo_exts = {'.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif',
                          '.raw', '.cr2', '.nef', '.webp', '.bmp', '.tiff'}
            music_exts = {'.mp3', '.flac', '.ogg', '.opus', '.m4a', '.wav',
                          '.aac', '.wma', '.alac'}
            video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm',
                          '.flv', '.m4v', '.ts'}
            for directory in directories:
                if not os.path.isdir(directory):
                    continue
                for root, dirs, files in os.walk(directory):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for fname in files:
                        ext = os.path.splitext(fname)[1].lower()
                        fpath = os.path.join(root, fname)
                        try:
                            stat = os.stat(fpath)
                            entry = {
                                'path': fpath, 'name': fname,
                                'size': stat.st_size,
                                'modified': stat.st_mtime,
                            }
                        except OSError:
                            continue
                        if ext in photo_exts:
                            r = _run(['exiftool', '-json', '-DateTimeOriginal',
                                       '-ImageWidth', '-ImageHeight', '-Model', fpath],
                                      timeout=5)
                            if r and r.returncode == 0:
                                try:
                                    meta = json.loads(r.stdout)
                                    if meta:
                                        entry.update({
                                            'date_taken': meta[0].get('DateTimeOriginal', ''),
                                            'width': meta[0].get('ImageWidth', 0),
                                            'height': meta[0].get('ImageHeight', 0),
                                            'camera': meta[0].get('Model', ''),
                                        })
                                except (json.JSONDecodeError, IndexError):
                                    pass
                            photos.append(entry)
                        elif ext in music_exts:
                            r = _run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
                                       '-show_format', fpath], timeout=5)
                            if r and r.returncode == 0:
                                try:
                                    meta = json.loads(r.stdout)
                                    fmt = meta.get('format', {})
                                    tags = fmt.get('tags', {})
                                    entry.update({
                                        'title': tags.get('title', fname),
                                        'artist': tags.get('artist', ''),
                                        'album': tags.get('album', ''),
                                        'duration': float(fmt.get('duration', 0)),
                                        'year': tags.get('date', '')[:4],
                                    })
                                except (json.JSONDecodeError, ValueError):
                                    pass
                            music.append(entry)
                        elif ext in video_exts:
                            r = _run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
                                       '-show_format', '-show_streams', fpath], timeout=5)
                            if r and r.returncode == 0:
                                try:
                                    meta = json.loads(r.stdout)
                                    fmt = meta.get('format', {})
                                    vid_stream = next(
                                        (s for s in meta.get('streams', [])
                                         if s.get('codec_type') == 'video'), {})
                                    entry.update({
                                        'duration': float(fmt.get('duration', 0)),
                                        'resolution': f"{vid_stream.get('width', 0)}x{vid_stream.get('height', 0)}",
                                        'codec': vid_stream.get('codec_name', ''),
                                    })
                                except (json.JSONDecodeError, ValueError):
                                    pass
                            videos.append(entry)

            with _media_lock:
                _media_index['photos'] = photos
                _media_index['music'] = music
                _media_index['videos'] = videos
                _media_index['last_scan'] = time.time()
                _media_index['scan_dirs'] = directories

        threading.Thread(target=_do_scan, daemon=True).start()
        return jsonify({'scanning': True, 'directories': directories})

    @app.route('/api/shell/media/photos', methods=['GET'])
    def shell_media_photos():
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        sort = request.args.get('sort', 'date')
        with _media_lock:
            photos = list(_media_index['photos'])
        if sort == 'date':
            photos.sort(key=lambda p: p.get('modified', 0), reverse=True)
        elif sort == 'name':
            photos.sort(key=lambda p: p.get('name', ''))
        elif sort == 'size':
            photos.sort(key=lambda p: p.get('size', 0), reverse=True)
        start = (page - 1) * per_page
        return jsonify({
            'photos': photos[start:start + per_page],
            'total': len(photos), 'page': page,
        })

    @app.route('/api/shell/media/music', methods=['GET'])
    def shell_media_music():
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        artist = request.args.get('artist', '').lower()
        album = request.args.get('album', '').lower()
        with _media_lock:
            tracks = list(_media_index['music'])
        if artist:
            tracks = [t for t in tracks if artist in t.get('artist', '').lower()]
        if album:
            tracks = [t for t in tracks if album in t.get('album', '').lower()]
        start = (page - 1) * per_page
        return jsonify({
            'tracks': tracks[start:start + per_page],
            'total': len(tracks), 'page': page,
        })

    @app.route('/api/shell/media/videos', methods=['GET'])
    def shell_media_videos():
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        with _media_lock:
            videos = list(_media_index['videos'])
        videos.sort(key=lambda v: v.get('modified', 0), reverse=True)
        start = (page - 1) * per_page
        return jsonify({
            'videos': videos[start:start + per_page],
            'total': len(videos), 'page': page,
        })

    # ─── 15b. Media Player Controls ─────────────────────────────

    _player_proc = {'pid': None, 'path': None, 'engine': None}
    _player_lock = threading.Lock()

    @app.route('/api/shell/media/play', methods=['POST'])
    @_require_system_auth
    def shell_media_play():
        """Play a media file using mpv (background process)."""
        body = request.get_json(silent=True) or {}
        path = body.get('path')
        if not path:
            return jsonify({'error': 'path required'}), 400
        if not os.path.isfile(path):
            return jsonify({'error': 'File not found'}), 404

        # Path safety: only allow files under user home or /tmp
        home = os.path.expanduser('~')
        real = os.path.realpath(path)
        import tempfile
        allowed = [os.path.realpath(home), os.path.realpath(tempfile.gettempdir())]
        if not any(real.startswith(a) for a in allowed):
            return jsonify({'error': 'Path outside allowed roots'}), 403

        # Stop any existing playback
        with _player_lock:
            if _player_proc['pid']:
                try:
                    os.kill(_player_proc['pid'], signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass

        # Try mpv first, then xdg-open fallback
        for engine in ['mpv', 'vlc', 'xdg-open']:
            r = _run(['which', engine], timeout=2)
            if r and r.returncode == 0:
                try:
                    # Hide Windows console window on cross-platform engines
                    # (mpv/vlc on Windows pop a cmd window for stdout
                    # otherwise).  Routes through canonical helper for
                    # consistency with livekit_supervisor / vlm probes.
                    from core.subprocess_safe import hidden_popen_kwargs
                    proc = subprocess.Popen(
                        [engine, '--', path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **hidden_popen_kwargs(),
                    )
                    with _player_lock:
                        _player_proc['pid'] = proc.pid
                        _player_proc['path'] = path
                        _player_proc['engine'] = engine
                    _audit_system_op('media_play', {'path': path, 'engine': engine})
                    return jsonify({
                        'playing': True, 'path': path,
                        'engine': engine, 'pid': proc.pid,
                    })
                except Exception as e:
                    continue

        return jsonify({'error': 'No media player found (install mpv)'}), 500

    @app.route('/api/shell/media/stop', methods=['POST'])
    @_require_system_auth
    def shell_media_stop():
        """Stop current media playback."""
        with _player_lock:
            pid = _player_proc.get('pid')
            if not pid:
                return jsonify({'stopped': False, 'error': 'Nothing playing'})
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            _player_proc['pid'] = None
            _player_proc['path'] = None
            _player_proc['engine'] = None
        return jsonify({'stopped': True})

    @app.route('/api/shell/media/player-status', methods=['GET'])
    def shell_media_player_status():
        """Get current media player status."""
        with _player_lock:
            pid = _player_proc.get('pid')
            running = False
            if pid:
                try:
                    os.kill(pid, 0)  # Signal 0 = check existence
                    running = True
                except (OSError, ProcessLookupError):
                    _player_proc['pid'] = None
                    _player_proc['path'] = None
        return jsonify({
            'playing': running,
            'path': _player_proc.get('path'),
            'engine': _player_proc.get('engine'),
        })

    # ─── 16. Webcam / Camera ───────────────────────────────────

    @app.route('/api/shell/webcam/list', methods=['GET'])
    def shell_webcam_list():
        """List available webcam/camera devices."""
        devices = []
        try:
            import glob as _glob
            for dev in sorted(_glob.glob('/dev/video*')):
                info = {'device': dev}
                r = _run(['v4l2-ctl', '--device', dev, '--info'], timeout=5)
                if r and r.returncode == 0:
                    for line in r.stdout.split('\n'):
                        if 'Card type' in line:
                            info['name'] = line.split(':', 1)[1].strip()
                        elif 'Driver name' in line:
                            info['driver'] = line.split(':', 1)[1].strip()
                devices.append(info)
        except Exception:
            pass
        return jsonify({'devices': devices})

    @app.route('/api/shell/webcam/capture', methods=['POST'])
    def shell_webcam_capture():
        """Capture a single frame from webcam."""
        body = request.get_json(silent=True) or {}
        device = body.get('device', '/dev/video0')
        import tempfile
        out_path = os.path.join(tempfile.gettempdir(), f'hart_webcam_{int(time.time())}.jpg')
        r = _run(['ffmpeg', '-f', 'v4l2', '-i', device, '-frames:v', '1',
                   '-y', out_path], timeout=10)
        if r and r.returncode == 0 and os.path.isfile(out_path):
            return jsonify({'status': 'ok', 'path': out_path})
        # 503, not 500: a missing/failed capture tool is an UNAVAILABLE peripheral
        # (a controlled degrade the shell shows), never a server crash. Found by
        # the deployed-surface suite (500 = unhandled-crash class there).
        return jsonify({'status': 'error',
                       'error': r.stderr if r else 'ffmpeg not available'}), 503

    # ─── 17. Scanner ──────────────────────────────────────────

    @app.route('/api/shell/scanner/list', methods=['GET'])
    def shell_scanner_list():
        """List available scanners via SANE."""
        r = _run(['scanimage', '-L'], timeout=15)
        scanners = []
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if 'device' in line.lower():
                    scanners.append({'raw': line.strip()})
        return jsonify({'scanners': scanners})

    @app.route('/api/shell/scanner/scan', methods=['POST'])
    def shell_scanner_scan():
        """Scan a document/image."""
        body = request.get_json(silent=True) or {}
        fmt = body.get('format', 'png')
        import tempfile
        out_path = os.path.join(tempfile.gettempdir(), f'hart_scan_{int(time.time())}.{fmt}')
        r = _run(['scanimage', f'--format={fmt}', f'--output-file={out_path}'],
                  timeout=60)
        if r and r.returncode == 0 and os.path.isfile(out_path):
            return jsonify({'status': 'ok', 'path': out_path})
        return jsonify({'status': 'error',
                       'error': r.stderr if r else 'scanimage not available'}), 503

    # ─── 18. Battery / Power Monitoring ──────────────────────

    def _read_sysfs(path, default=''):
        """Read a single sysfs file, return stripped string or default."""
        try:
            with open(path) as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return default

    def _battery_info():
        """Gather battery information from psutil + Linux sysfs."""
        info = {
            'present': False, 'status': 'unknown', 'capacity': None,
            'voltage_v': None, 'power_w': None, 'temperature_c': None,
            'technology': None, 'health': 'unknown',
            'remaining_minutes': None, 'plugged_in': False,
        }

        # Try psutil first (cross-platform)
        try:
            import psutil
            bat = psutil.sensors_battery()
            if bat:
                info['present'] = True
                info['capacity'] = round(bat.percent, 1)
                info['plugged_in'] = bat.power_plugged
                if bat.power_plugged:
                    info['status'] = 'charging' if bat.percent < 100 else 'full'
                else:
                    info['status'] = 'discharging'
                if bat.secsleft and bat.secsleft > 0:
                    info['remaining_minutes'] = round(bat.secsleft / 60, 0)
        except (ImportError, RuntimeError):
            pass

        # Enrich with Linux sysfs (more detail)
        import glob as _glob
        bat_dirs = sorted(_glob.glob('/sys/class/power_supply/BAT*'))
        if bat_dirs:
            d = bat_dirs[0]
            info['present'] = True
            sysfs_status = _read_sysfs(f'{d}/status')
            if sysfs_status:
                info['status'] = sysfs_status.lower()

            cap = _read_sysfs(f'{d}/capacity')
            if cap.isdigit():
                info['capacity'] = int(cap)

            voltage = _read_sysfs(f'{d}/voltage_now')
            if voltage.isdigit():
                info['voltage_v'] = round(int(voltage) / 1_000_000, 2)

            power = _read_sysfs(f'{d}/power_now')
            if power.isdigit():
                info['power_w'] = round(int(power) / 1_000_000, 2)

            temp = _read_sysfs(f'{d}/temp')
            if temp.isdigit():
                info['temperature_c'] = round(int(temp) / 10, 1)

            info['technology'] = _read_sysfs(f'{d}/technology') or None

        # Health classification
        if info['capacity'] is not None:
            if info['capacity'] > 20:
                info['health'] = 'good'
            elif info['capacity'] > 5:
                info['health'] = 'low'
            else:
                info['health'] = 'critical'

        # AC adapter
        ac_dirs = sorted(_glob.glob('/sys/class/power_supply/AC*') +
                         _glob.glob('/sys/class/power_supply/ADP*'))
        if ac_dirs:
            online = _read_sysfs(f'{ac_dirs[0]}/online')
            if online == '1':
                info['plugged_in'] = True

        return info

    @app.route('/api/shell/battery', methods=['GET'])
    def shell_battery_status():
        """Get current battery status."""
        return jsonify(_battery_info())

    @app.route('/api/shell/battery/profile', methods=['GET'])
    def shell_battery_profile():
        """Get current power profile."""
        profiles = []
        r = _run(['powerprofilesctl', 'list'], timeout=5)
        current = None
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                line = line.strip()
                if line.endswith(':'):
                    name = line.rstrip(':').lstrip('* ')
                    profiles.append(name)
                    if line.startswith('*'):
                        current = name
        if not profiles:
            # Fallback: check TLP or cpufreq
            r2 = _run(['cat', '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'],
                       timeout=5)
            if r2 and r2.returncode == 0:
                current = r2.stdout.strip()
                profiles = ['performance', 'powersave', 'schedutil']
        return jsonify({
            'current_profile': current,
            'available': profiles,
        })

    @app.route('/api/shell/battery/profile', methods=['POST'])
    @_require_system_auth
    def shell_battery_set_profile():
        """Set power profile."""
        body = request.get_json(silent=True) or {}
        profile = body.get('profile')
        if not profile:
            return jsonify({'error': 'profile required'}), 400
        r = _run(['powerprofilesctl', 'set', profile], timeout=5)
        if r and r.returncode == 0:
            _audit_system_op('battery_profile', {'profile': profile})
            return jsonify({'success': True, 'profile': profile})
        return jsonify({'error': 'Failed to set profile',
                       'detail': r.stderr if r else 'powerprofilesctl not available'}), 500

    # ─── 19. WiFi Management ──────────────────────────────────

    @app.route('/api/shell/wifi/status', methods=['GET'])
    def shell_wifi_status():
        """Get current WiFi connection status."""
        info = {'enabled': False, 'connected': False, 'ssid': None,
                'signal': None, 'frequency': None, 'ip': None}
        # Check if WiFi is enabled
        r = _run(['nmcli', 'radio', 'wifi'], timeout=5)
        if r and r.returncode == 0:
            info['enabled'] = r.stdout.strip().lower() == 'enabled'

        # Current connection
        r = _run(['nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL,FREQ',
                  'device', 'wifi'], timeout=5)
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and parts[0] == 'yes':
                    info['connected'] = True
                    info['ssid'] = parts[1]
                    if len(parts) >= 3:
                        info['signal'] = int(parts[2]) if parts[2].isdigit() else None
                    if len(parts) >= 4:
                        info['frequency'] = parts[3]
                    break

        # IP address
        if info['connected']:
            r = _run(['nmcli', '-t', '-f', 'IP4.ADDRESS', 'device', 'show',
                      'type', 'wifi'], timeout=5)
            if r and r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    if 'IP4.ADDRESS' in line:
                        info['ip'] = line.split(':', 1)[1].strip() if ':' in line else None
                        break
        return jsonify(info)

    @app.route('/api/shell/wifi/networks', methods=['GET'])
    def shell_wifi_networks():
        """Scan and list available WiFi networks.

        Never sleeps on the request thread. A rescan only *triggers* a background
        scan in NetworkManager; we immediately return whatever the scan cache holds
        (NM keeps the last results), so the shell pool is never blocked. The list
        self-freshens on the next poll once the scan lands. The old time.sleep(2)
        here pinned the 1-2 thread pool and was a direct cause of the Wi-Fi click
        freezing the UI.
        """
        rescan = request.args.get('rescan', 'false').lower() == 'true'
        if rescan:
            # Fire the rescan trigger but do NOT block waiting for it to populate
            # (no time.sleep on the request path). NM scans asynchronously.
            _run(['nmcli', 'device', 'wifi', 'rescan'], timeout=4)

        r = _run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,FREQ,BSSID',
                  'device', 'wifi', 'list'], timeout=6)
        networks = []
        seen = set()
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 3 and parts[0] and parts[0] not in seen:
                    seen.add(parts[0])
                    networks.append({
                        'ssid': parts[0],
                        'signal': int(parts[1]) if parts[1].isdigit() else 0,
                        'security': parts[2] if len(parts) > 2 else '',
                        'frequency': parts[3] if len(parts) > 3 else '',
                    })
        networks.sort(key=lambda n: n['signal'], reverse=True)
        return jsonify({'networks': networks, 'count': len(networks)})

    @app.route('/api/shell/wifi/connect', methods=['POST'])
    @_require_system_auth
    def shell_wifi_connect():
        """Connect to a WiFi network.

        The association + DHCP handshake can take many seconds, so the nmcli
        connect runs on a bounded background worker and never pins the request
        thread for the full timeout. A fast join returns the REAL result; a slow
        one returns a structured 'connecting' (202) — NOT a faked success — and the
        next connectivity poll reports the true joined state. This keeps the small
        shell pool free instead of queueing every other fetch behind a 30s connect.
        """
        body = request.get_json(silent=True) or {}
        ssid = body.get('ssid')
        if not ssid:
            return jsonify({'error': 'ssid required'}), 400
        password = body.get('password')
        hidden = body.get('hidden', False)

        cmd = ['nmcli', 'device', 'wifi', 'connect', ssid]
        if password:
            cmd += ['password', password]
        if hidden:
            cmd += ['hidden', 'yes']

        finished, r = _run_async_bounded(cmd, run_timeout=20, wait=6,
                                         name='hart-wifi-connect')
        if not finished:
            # Still associating in the background; the request thread is freed.
            # Honest 'connecting' rather than a masked success.
            return jsonify({'connected': False, 'connecting': True, 'ssid': ssid}), 202
        if r and r.returncode == 0:
            _audit_system_op('wifi_connect', {'ssid': ssid})
            return jsonify({'connected': True, 'ssid': ssid})
        return jsonify({'connected': False,
                       'error': r.stderr.strip() if r else 'nmcli not available'}), 400

    @app.route('/api/shell/wifi/disconnect', methods=['POST'])
    @_require_system_auth
    def shell_wifi_disconnect():
        """Disconnect from current WiFi network."""
        r = _run(['nmcli', 'device', 'disconnect', 'type', 'wifi'], timeout=10)
        if r and r.returncode == 0:
            return jsonify({'disconnected': True})
        # Try finding the wifi device name
        r2 = _run(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device'], timeout=5)
        if r2 and r2.returncode == 0:
            for line in r2.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and parts[1] == 'wifi':
                    r3 = _run(['nmcli', 'device', 'disconnect', parts[0]], timeout=10)
                    if r3 and r3.returncode == 0:
                        return jsonify({'disconnected': True})
        return jsonify({'disconnected': False,
                       'error': 'Failed to disconnect'}), 400

    @app.route('/api/shell/wifi/saved', methods=['GET'])
    def shell_wifi_saved():
        """List saved WiFi connections."""
        r = _run(['nmcli', '-t', '-f', 'NAME,TYPE,AUTOCONNECT',
                  'connection', 'show'], timeout=5)
        connections = []
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and '802-11-wireless' in parts[1]:
                    connections.append({
                        'ssid': parts[0],
                        'autoconnect': parts[2].lower() == 'yes' if len(parts) > 2 else True,
                    })
        return jsonify({'connections': connections})

    @app.route('/api/shell/wifi/forget', methods=['POST'])
    @_require_system_auth
    def shell_wifi_forget():
        """Forget a saved WiFi connection."""
        body = request.get_json(silent=True) or {}
        ssid = body.get('ssid')
        if not ssid:
            return jsonify({'error': 'ssid required'}), 400
        r = _run(['nmcli', 'connection', 'delete', ssid], timeout=10)
        if r and r.returncode == 0:
            _audit_system_op('wifi_forget', {'ssid': ssid})
            return jsonify({'forgotten': True, 'ssid': ssid})
        return jsonify({'forgotten': False,
                       'error': r.stderr.strip() if r else 'nmcli not available'}), 400

    @app.route('/api/shell/wifi/toggle', methods=['POST'])
    @_require_system_auth
    def shell_wifi_toggle():
        """Enable or disable WiFi radio."""
        body = request.get_json(silent=True) or {}
        enable = body.get('enable', True)
        state = 'on' if enable else 'off'
        r = _run(['nmcli', 'radio', 'wifi', state], timeout=5)
        if r and r.returncode == 0:
            return jsonify({'enabled': enable})
        return jsonify({'error': 'Failed to toggle WiFi'}), 500

    # ─── 20. VPN Client ───────────────────────────────────────

    @app.route('/api/shell/vpn/list', methods=['GET'])
    def shell_vpn_list():
        """List configured VPN connections."""
        r = _run(['nmcli', '-t', '-f', 'NAME,TYPE,ACTIVE',
                  'connection', 'show'], timeout=5)
        vpns = []
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and 'vpn' in parts[1].lower():
                    vpns.append({
                        'name': parts[0],
                        'type': parts[1],
                        'active': parts[2].lower() == 'yes' if len(parts) > 2 else False,
                    })
        return jsonify({'connections': vpns})

    @app.route('/api/shell/vpn/status', methods=['GET'])
    def shell_vpn_status():
        """Get VPN connection status."""
        r = _run(['nmcli', '-t', '-f', 'NAME,TYPE,IP4.ADDRESS',
                  'connection', 'show', '--active'], timeout=5)
        vpn_active = None
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and 'vpn' in parts[1].lower():
                    vpn_active = {
                        'name': parts[0],
                        'type': parts[1],
                        'ip': parts[2] if len(parts) > 2 else None,
                    }
                    break
        return jsonify({
            'connected': vpn_active is not None,
            'vpn': vpn_active,
        })

    @app.route('/api/shell/vpn/connect', methods=['POST'])
    @_require_system_auth
    def shell_vpn_connect():
        """Activate a VPN connection."""
        body = request.get_json(silent=True) or {}
        name = body.get('name')
        if not name:
            return jsonify({'error': 'name required'}), 400
        r = _run(['nmcli', 'connection', 'up', name], timeout=30)
        if r and r.returncode == 0:
            _audit_system_op('vpn_connect', {'name': name})
            return jsonify({'connected': True, 'name': name})
        return jsonify({'connected': False,
                       'error': r.stderr.strip() if r else 'nmcli not available'}), 400

    @app.route('/api/shell/vpn/disconnect', methods=['POST'])
    @_require_system_auth
    def shell_vpn_disconnect():
        """Deactivate VPN connection."""
        body = request.get_json(silent=True) or {}
        name = body.get('name')
        if not name:
            return jsonify({'error': 'name required'}), 400
        r = _run(['nmcli', 'connection', 'down', name], timeout=10)
        if r and r.returncode == 0:
            _audit_system_op('vpn_disconnect', {'name': name})
            return jsonify({'disconnected': True})
        return jsonify({'disconnected': False,
                       'error': r.stderr.strip() if r else 'nmcli not available'}), 400

    @app.route('/api/shell/vpn/import', methods=['POST'])
    @_require_system_auth
    def shell_vpn_import():
        """Import a VPN configuration file."""
        body = request.get_json(silent=True) or {}
        config_path = body.get('config_path')
        vpn_type = body.get('type', 'openvpn')
        if not config_path:
            return jsonify({'error': 'config_path required'}), 400
        if not os.path.isfile(config_path):
            return jsonify({'error': 'Config file not found'}), 404

        r = _run(['nmcli', 'connection', 'import', 'type', vpn_type,
                  'file', config_path], timeout=10)
        if r and r.returncode == 0:
            # Extract connection name from output
            name = r.stdout.strip().split("'")[1] if "'" in r.stdout else os.path.basename(config_path)
            return jsonify({'imported': True, 'name': name})
        return jsonify({'imported': False,
                       'error': r.stderr.strip() if r else 'nmcli not available'}), 400

    @app.route('/api/shell/vpn/<name>', methods=['DELETE'])
    @_require_system_auth
    def shell_vpn_delete(name):
        """Delete a VPN connection."""
        r = _run(['nmcli', 'connection', 'delete', name], timeout=10)
        if r and r.returncode == 0:
            _audit_system_op('vpn_delete', {'name': name})
            return jsonify({'deleted': True})
        return jsonify({'deleted': False,
                       'error': r.stderr.strip() if r else 'not found'}), 400

    # ─── 21. Trash / Recycle Bin ──────────────────────────────

    def _trash_dir():
        """Get XDG trash directory."""
        return os.path.join(os.path.expanduser('~'), '.local', 'share', 'Trash')

    def _trash_list():
        """List items in trash with metadata."""
        trash = _trash_dir()
        info_dir = os.path.join(trash, 'info')
        files_dir = os.path.join(trash, 'files')
        items = []
        if not os.path.isdir(info_dir):
            return items

        for fname in os.listdir(info_dir):
            if not fname.endswith('.trashinfo'):
                continue
            item_name = fname[:-len('.trashinfo')]
            item_path = os.path.join(files_dir, item_name)
            info_path = os.path.join(info_dir, fname)

            entry = {'id': item_name, 'name': item_name}
            try:
                cp = configparser.ConfigParser()
                cp.read(info_path)
                if cp.has_section('Trash Info'):
                    entry['original_path'] = cp.get('Trash Info', 'Path', fallback='')
                    entry['deleted_time'] = cp.get('Trash Info', 'DeletionDate', fallback='')
            except Exception:
                pass

            if os.path.exists(item_path):
                try:
                    st = os.stat(item_path)
                    entry['size_bytes'] = st.st_size
                    entry['is_dir'] = os.path.isdir(item_path)
                except OSError:
                    entry['size_bytes'] = 0
            items.append(entry)

        items.sort(key=lambda x: x.get('deleted_time', ''), reverse=True)
        return items

    @app.route('/api/shell/trash', methods=['GET'])
    def shell_trash_list():
        """List items in trash."""
        items = _trash_list()
        total_size = sum(i.get('size_bytes', 0) for i in items)
        return jsonify({
            'items': items,
            'total_items': len(items),
            'total_size_mb': round(total_size / 1048576, 2),
        })

    @app.route('/api/shell/trash/move', methods=['POST'])
    @_require_system_auth
    def shell_trash_move_to():
        """Move a file to trash (instead of permanent delete)."""
        body = request.get_json(silent=True) or {}
        path = body.get('path')
        if not path:
            return jsonify({'error': 'path required'}), 400
        if not os.path.exists(path):
            return jsonify({'error': 'File not found'}), 404

        r = _run(['gio', 'trash', path], timeout=10)
        if r and r.returncode == 0:
            _audit_system_op('trash_move', {'path': path})
            return jsonify({'trashed': True, 'path': path})
        # Fallback: manual move to ~/.local/share/Trash
        try:
            trash = _trash_dir()
            files_dir = os.path.join(trash, 'files')
            info_dir = os.path.join(trash, 'info')
            os.makedirs(files_dir, exist_ok=True)
            os.makedirs(info_dir, exist_ok=True)

            name = os.path.basename(path)
            dst = os.path.join(files_dir, name)
            # Handle name collision
            counter = 1
            while os.path.exists(dst):
                base, ext = os.path.splitext(name)
                dst = os.path.join(files_dir, f'{base}.{counter}{ext}')
                name = f'{base}.{counter}{ext}'
                counter += 1

            import shutil
            shutil.move(path, dst)

            # Write .trashinfo
            from datetime import datetime, timezone
            info_path = os.path.join(info_dir, f'{name}.trashinfo')
            with open(info_path, 'w') as f:
                f.write('[Trash Info]\n')
                f.write(f'Path={os.path.abspath(path)}\n')
                f.write(f'DeletionDate={datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}\n')
            return jsonify({'trashed': True, 'path': path})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/trash/restore', methods=['POST'])
    @_require_system_auth
    def shell_trash_restore():
        """Restore item(s) from trash to original location."""
        body = request.get_json(silent=True) or {}
        item_id = body.get('id')
        restore_all = body.get('all', False)

        trash = _trash_dir()
        files_dir = os.path.join(trash, 'files')
        info_dir = os.path.join(trash, 'info')
        restored = []

        items_to_restore = _trash_list() if restore_all else []
        if item_id and not restore_all:
            items_to_restore = [i for i in _trash_list() if i['id'] == item_id]

        for item in items_to_restore:
            try:
                src = os.path.join(files_dir, item['id'])
                dst = item.get('original_path', '')
                if not dst or not src:
                    continue
                dst_dir = os.path.dirname(dst)
                if dst_dir:
                    os.makedirs(dst_dir, exist_ok=True)
                import shutil
                shutil.move(src, dst)
                # Remove .trashinfo
                info_path = os.path.join(info_dir, f"{item['id']}.trashinfo")
                if os.path.isfile(info_path):
                    os.remove(info_path)
                restored.append(dst)
            except Exception as e:
                logger.debug(f"Trash restore failed for {item['id']}: {e}")

        return jsonify({
            'restored_count': len(restored),
            'restored_paths': restored,
        })

    @app.route('/api/shell/trash/empty', methods=['DELETE'])
    @_require_system_auth
    def shell_trash_empty():
        """Empty the trash (permanent delete)."""
        body = request.get_json(silent=True) or {}
        older_than_days = body.get('older_than_days')

        r = _run(['gio', 'trash', '--empty'], timeout=30)
        if r and r.returncode == 0 and not older_than_days:
            return jsonify({'emptied': True})

        # Fallback or age-filtered empty
        trash = _trash_dir()
        files_dir = os.path.join(trash, 'files')
        info_dir = os.path.join(trash, 'info')
        freed = 0

        if older_than_days:
            from datetime import datetime, timezone, timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            items = _trash_list()
            for item in items:
                try:
                    dt_str = item.get('deleted_time', '')
                    if dt_str:
                        dt = datetime.fromisoformat(dt_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt > cutoff:
                            continue
                except (ValueError, TypeError):
                    pass

                item_path = os.path.join(files_dir, item['id'])
                info_path = os.path.join(info_dir, f"{item['id']}.trashinfo")
                try:
                    freed += item.get('size_bytes', 0)
                    import shutil
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    elif os.path.isfile(item_path):
                        os.remove(item_path)
                    if os.path.isfile(info_path):
                        os.remove(info_path)
                except Exception as e:
                    logger.debug(f"Trash empty failed for {item['id']}: {e}")
        else:
            # Full empty fallback
            import shutil
            for d in [files_dir, info_dir]:
                if os.path.isdir(d):
                    for item in os.listdir(d):
                        p = os.path.join(d, item)
                        try:
                            if os.path.isdir(p):
                                shutil.rmtree(p)
                            else:
                                os.remove(p)
                        except Exception:
                            pass

        _audit_system_op('trash_empty', {'freed_mb': round(freed / 1048576, 2)})
        return jsonify({'emptied': True, 'freed_mb': round(freed / 1048576, 2)})

    # ─── 22. Screen Rotation ──────────────────────────────────

    @app.route('/api/shell/display/rotation', methods=['GET'])
    def shell_display_rotation():
        """Get current display rotation/orientation."""
        outputs = []
        r = _run(['swaymsg', '-t', 'get_outputs', '-r'], timeout=5)
        if r and r.returncode == 0:
            try:
                for out in json.loads(r.stdout):
                    outputs.append({
                        'name': out.get('name', ''),
                        'transform': out.get('transform', 'normal'),
                        'active': out.get('active', False),
                    })
            except (json.JSONDecodeError, TypeError):
                pass
        if not outputs:
            # xrandr fallback
            r2 = _run(['xrandr', '--query'], timeout=5)
            if r2 and r2.returncode == 0:
                for line in r2.stdout.split('\n'):
                    if ' connected' in line:
                        parts = line.split()
                        name = parts[0] if parts else 'unknown'
                        rotation = 'normal'
                        for kw in ('left', 'right', 'inverted'):
                            if kw in line:
                                rotation = kw
                                break
                        outputs.append({'name': name, 'transform': rotation,
                                        'active': 'primary' in line or '+' in line})
        return jsonify({'outputs': outputs})

    @app.route('/api/shell/display/rotation', methods=['POST'])
    @_require_system_auth
    def shell_display_set_rotation():
        """Set display rotation. transform: normal|90|180|270|flipped."""
        body = request.get_json(silent=True) or {}
        output = body.get('output', '')
        transform = body.get('transform', 'normal')
        if not output:
            return jsonify({'error': 'output name required'}), 400

        valid = {'normal', '90', '180', '270', 'flipped',
                 'flipped-90', 'flipped-180', 'flipped-270'}
        if transform not in valid:
            return jsonify({'error': f'transform must be one of: {sorted(valid)}'}), 400

        # Try swaymsg (Wayland)
        r = _run(['swaymsg', 'output', output, 'transform', transform], timeout=5)
        if r and r.returncode == 0:
            _audit_system_op('display_rotate', {'output': output, 'transform': transform})
            return jsonify({'rotated': True, 'output': output, 'transform': transform})

        # xrandr fallback (X11)
        xrandr_map = {'normal': 'normal', '90': 'left', '180': 'inverted',
                      '270': 'right', 'flipped': 'normal'}
        xr = xrandr_map.get(transform, 'normal')
        r2 = _run(['xrandr', '--output', output, '--rotate', xr], timeout=5)
        if r2 and r2.returncode == 0:
            _audit_system_op('display_rotate', {'output': output, 'transform': transform})
            return jsonify({'rotated': True, 'output': output, 'transform': transform})

        return jsonify({'error': 'rotation failed (no swaymsg or xrandr)'}), 500

    @app.route('/api/shell/display/auto-rotate', methods=['GET'])
    def shell_display_auto_rotate_status():
        """Check if auto-rotate is available (iio-sensor-proxy)."""
        r = _run(['monitor-sensor', '--help'], timeout=3)
        available = r is not None
        return jsonify({'available': available,
                        'sensor': 'iio-sensor-proxy' if available else None})

    logger.info("Registered shell system routes (10 features)")
