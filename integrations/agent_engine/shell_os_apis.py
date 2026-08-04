"""
Shell OS APIs — Extended system management endpoints for LiquidUI.

Provides Flask route registrations for:
  - Notifications (freedesktop.org D-Bus bridge)
  - File manager (browse, mkdir, delete, move, copy)
  - Terminal (PTY allocation, I/O, resize)
  - User account management (list, create, modify, delete)
  - First-time setup wizard (progress, steps)
  - Backup restore
  - Power management (profiles, suspend, hibernate, checkpoint)
  - i18n (locale listing, selection, translation lookup)
  - Accessibility (settings read/write)
  - Screenshot / screen recording
  - Multi-device pairing (mesh status, pair, unpair)

All routes prefixed with /api/shell/ to match existing conventions.
Registration: call register_shell_os_routes(app) from the server init.

Security:
  - Local-only auth: requests must come from 127.0.0.1/::1 OR carry a
    valid X-Shell-Token header (generated at desktop login).
  - Path sandbox: file operations confined to user home + /tmp.
  - Destructive ops classified via action_classifier + audit logged.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess

from core.subprocess_safe import run_probe
import tempfile
import time
from functools import wraps
from typing import Optional

logger = logging.getLogger('hevolve.shell')

# ─── Path Sandbox ─────────────────────────────────────────────────

# Allowed filesystem roots for file operations
_ALLOWED_ROOTS = None  # Lazily computed


def _get_allowed_roots():
    """Get allowed filesystem roots (user home + /tmp + configurable)."""
    global _ALLOWED_ROOTS
    if _ALLOWED_ROOTS is not None:
        return _ALLOWED_ROOTS
    roots = [
        os.path.realpath(os.path.expanduser('~')),
        os.path.realpath(tempfile.gettempdir()),
    ]
    extra = os.environ.get('HART_SHELL_ALLOWED_PATHS', '')
    if extra:
        for p in extra.split(':'):
            rp = os.path.realpath(p.strip())
            if os.path.isdir(rp):
                roots.append(rp)
    # This PC / partition browsing: admit every real disk mountpoint reported by
    # psutil (the SAME read-only source the Storage + This-PC panels use), so a
    # drive root like C:\ or /mnt/data opens in the file manager instead of 403.
    # Read paths only, and every shell route is _require_shell_auth (local-only)
    # gated — this widens what the local desktop session can browse, nothing more.
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            mp = os.path.realpath(part.mountpoint)
            if os.path.isdir(mp) and mp not in roots:
                roots.append(mp)
    except Exception:
        pass
    _ALLOWED_ROOTS = roots
    return roots


def _is_path_allowed(path):
    """Check if a resolved path is within allowed roots.

    realpath, not abspath: abspath only NORMALISES (`../../etc/shadow` becomes
    `/etc/shadow` and is then happily accepted) and does not follow symlinks, so
    a link pointing out of an allowed root would pass untouched.

    commonpath, not startswith: a prefix test lets `/home/hart` authorise
    `/home/hart-evil`, because the string genuinely starts with the root. Only a
    component-wise comparison answers "is this INSIDE the root". commonpath
    raises ValueError across drives/mixed absolute-relative, which is a "no".
    """
    real = os.path.realpath(path)
    for root in _get_allowed_roots():
        root_real = os.path.realpath(root)
        try:
            if os.path.commonpath([real, root_real]) == root_real:
                return True
        except ValueError:
            continue        # different drive / not comparable -> not inside
    return False


# ─── Shell Auth (local-only, no social DB dependency) ─────────────

def _shell_auth_check():
    """Verify request is from local desktop session.

    Returns (ok, error_response) — if ok is True, request is authorized.
    Accepts:
      1. Localhost origin (127.0.0.1, ::1, 0.0.0.0) — desktop is local
      2. Valid X-Shell-Token header (for remote LiquidUI sessions)
    """
    from flask import request, jsonify

    remote = request.remote_addr or ''
    local_addrs = ('127.0.0.1', '::1', '0.0.0.0', 'localhost')
    if remote in local_addrs:
        return True, None

    # Check shell token (set during desktop login)
    token = request.headers.get('X-Shell-Token', '')
    if token:
        expected = os.environ.get('HART_SHELL_TOKEN', '')
        if expected and token == expected:
            return True, None

    return False, jsonify({'error': 'Shell API: local access only'}), 403


def _require_shell_auth(f):
    """Decorator: require local shell authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        result = _shell_auth_check()
        if not result[0]:
            return result[1], result[2]
        return f(*args, **kwargs)
    return decorated


# ─── Audit helper ──────────────────────────────────────────────────

def _audit_shell_op(action, detail=None):
    """Log a shell operation to the immutable audit log (best-effort)."""
    try:
        from security.immutable_audit_log import get_audit_log
        get_audit_log().log_event(
            'shell_ops', 'shell_os_api', action,
            detail=detail or {})
    except Exception:
        pass


def _classify_destructive(action_desc):
    """Check if an action is destructive via action_classifier.

    Returns True if action is safe.
    Returns False if action is destructive OR classifier unavailable (fail-closed).
    """
    try:
        from security.action_classifier import classify_action
        result = classify_action(action_desc)
        # classify_action returns a string literal: 'safe', 'destructive', or 'unknown'
        return result == 'safe'
    except Exception:
        logger.warning("Action classifier unavailable — blocking action (fail-closed)")
        return False  # fail-closed: deny if classifier unavailable


# Read-only diagnostic binaries that NEVER need the (LLM-backed) destructive
# classifier: with shell=False (no pipes/redirects) they cannot mutate state, and
# gating them on the classifier made the Terminal hang whenever the local LLM was
# busy or down (every command returned "Fetch is aborted" on real hardware). The
# classifier still guards every other command. One canonical allowlist; do not duplicate.
_READONLY_SAFE_BINS = frozenset({
    'journalctl', 'dmesg', 'ls', 'cat', 'head', 'tail', 'grep', 'egrep', 'fgrep',
    'ps', 'free', 'df', 'du', 'lsblk', 'blkid', 'lsusb', 'lspci', 'lscpu', 'lsmod',
    'lsof', 'uname', 'hostname', 'uptime', 'whoami', 'id', 'who', 'w',
    'printenv', 'date', 'cal', 'pwd', 'echo', 'stat', 'file', 'wc', 'nproc',
})
# NOTE: 'env' is deliberately EXCLUDED - `env <program>` executes an arbitrary
# program, which would bypass the destructive classifier. 'printenv' covers the
# read-only environment-dump use case.


# ── Live accessibility state ──
# Module-level so the shell RENDER (liquid_ui_service.render_desktop_shell, same
# process) reads the SAME dict the /api/shell/accessibility routes mutate. Seeded
# from the NixOS declarative file at import; runtime PUTs override for the session.
_A11Y_SETTINGS = {
    'font_scale': 1.0,
    'high_contrast': False,
    'reduced_motion': False,
    'large_cursor': False,
    'screen_reader': False,
    'sticky_keys': False,
}
try:
    with open('/etc/hart/accessibility.json') as _a11y_f:
        _A11Y_SETTINGS.update({k: v for k, v in json.load(_a11y_f).items()
                               if k in _A11Y_SETTINGS})
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass


def get_a11y_settings():
    """Live accessibility state — read by both the API and the shell render."""
    return dict(_A11Y_SETTINGS)


# ── Firmware-setup (reboot-into-UEFI) capability ──
# CANONICAL home for "can this box reboot straight into the UEFI/BIOS setup?".
# ONE writer, imported by both the shell-OS power-action handler AND the
# liquid_ui_service session route + power-menu gate, so the answer never drifts
# (DRY: no second copy of this probe).
#
# `systemctl reboot --firmware-setup` works only on a UEFI system whose firmware
# advertises the "boot to firmware UI" capability via the EFI global variable
# OsIndicationsSupported (bit 0 = EFI_OS_INDICATIONS_BOOT_TO_FW_UI). On legacy
# BIOS there is no /sys/firmware/efi and the flag is meaningless, so we hide the
# action entirely. We read the efivar directly (no privileged call): its layout
# is a 4-byte attributes prefix followed by the 8-byte little-endian value.
_FW_BOOT_TO_FW_UI = 0x0000000000000001  # EFI_OS_INDICATIONS_BOOT_TO_FW_UI

# efivarfs path for OsIndicationsSupported (EFI global variable namespace GUID
# 8be4df61-93ca-11d2-aa0d-00e098032b8c).
_OS_INDICATIONS_SUPPORTED = (
    '/sys/firmware/efi/efivars/'
    'OsIndicationsSupported-8be4df61-93ca-11d2-aa0d-00e098032b8c')


def firmware_setup_supported():
    """True iff the system is UEFI-booted AND its firmware advertises the
    boot-to-firmware-UI capability (so `systemctl reboot --firmware-setup` will
    actually enter setup). False on legacy BIOS or when the capability is absent
    — the caller hides the action so the user never gets a plain reboot when they
    asked for firmware setup."""
    # 1. Must be UEFI-booted at all.
    if not os.path.isdir('/sys/firmware/efi'):
        return False
    # 2. Read OsIndicationsSupported and test the boot-to-fw-UI bit.
    try:
        with open(_OS_INDICATIONS_SUPPORTED, 'rb') as f:
            raw = f.read()
        # 4-byte attributes prefix + the variable data (8-byte LE value).
        if len(raw) < 12:
            return False
        value = int.from_bytes(raw[4:12], 'little')
        return bool(value & _FW_BOOT_TO_FW_UI)
    except (FileNotFoundError, PermissionError, OSError):
        # The efivar is absent/unreadable. Be conservative: if we are UEFI-booted
        # but cannot read the capability, do NOT claim support (hide the action)
        # — a wrong "supported" would give the user a plain reboot.
        return False


# ── Native logind (org.freedesktop.login1) power calls ──
# `_logind_call` is the SHARED entry point the power-action route AND the
# liquid_ui_service session routes use to "ask the OS to reboot / power off /
# suspend / hibernate / arm firmware setup / lock / terminate a session". The
# implementation now lives in the canonical TYPED + NATIVE OS bridge
# (integrations.agent_engine.os_bridge.logind) — #133 / W3: it tries a NATIVE
# D-Bus call (jeepney, no subprocess) FIRST and falls back to a bounded, result-
# checked `busctl call --system` only when the native transport is unavailable
# (e.g. the Windows dev box). Both transports CHECK the result, so a polkit denial
# surfaces as a REAL error instead of the old `subprocess.Popen(['systemctl',
# 'reboot'])` fire-and-forget that masked failures as `{'initiated': True}` while
# the box did nothing. The matching polkit grant lives in nixos/modules/
# hart-base.nix `security.polkit` (the `hart` user is authorized for these login1
# actions).
#
# This function keeps its `(method, *busctl_args)` signature UNCHANGED — it is
# imported by liquid_ui_service.py's session routes and mocked by the shell power
# tests + nixos/tests/power-actions.nix — and simply delegates to the ONE native
# implementation (no parallel path).
def _logind_call(method, *busctl_args, timeout=10):
    """Invoke an org.freedesktop.login1.Manager method, RESULT-CHECKED.

    `busctl_args` are the (signature, *string_values) the method takes, e.g.
    ('b', 'true') for the interactive-boolean methods (Reboot, PowerOff, Suspend,
    Hibernate, SetRebootToFirmwareSetup), ('s', sid) for TerminateSession, or
    nothing for a no-arg method (LockSessions).

    Returns (ok: bool, error: Optional[str]); `ok` is True ONLY when logind
    accepted the method AND polkit authorized it. Delegates to the canonical
    native client (os_bridge.logind), which tries native D-Bus first and falls
    back to bounded busctl — degrade-not-die, never raised to the request thread.
    """
    from integrations.agent_engine.os_bridge.logind import logind_call
    return logind_call(method, busctl_args, timeout=timeout)


def register_shell_os_routes(app):
    """Register all extended shell OS API routes on a Flask app."""

    from flask import jsonify, request, Response

    # ═══════════════════════════════════════════════════════════
    # Notifications — freedesktop.org D-Bus bridge
    # ═══════════════════════════════════════════════════════════

    _notification_queue = []  # In-memory for SSE; production uses DB

    @app.route('/api/shell/notifications', methods=['GET'])
    def shell_notifications_list():
        """List recent notifications."""
        limit = request.args.get('limit', 50, type=int)
        unread = request.args.get('unread', 'false').lower() == 'true'

        # Try DB-backed notifications first
        try:
            from integrations.social.services import NotificationService
            from integrations.social.models import db_session
            user_id = request.args.get('user_id', '1')
            with db_session() as db:
                notifs = NotificationService.get_for_user(
                    db, int(user_id), unread_only=unread, limit=limit)
                return jsonify({
                    'notifications': [n.to_dict() for n in notifs],
                    'source': 'database',
                })
        except Exception as e:
            # LEGITIMATE fallback (the in-memory queue below), but it must not
            # be SILENT: without this line a DB outage renders an empty
            # notification list that looks like "you have no notifications".
            # The response already distinguishes source=database vs the
            # fallback, so the caller can tell WHICH path ran — this says WHY.
            # `except (ImportError, Exception)` was redundant: ImportError IS
            # an Exception, so the tuple caught everything anyway.
            logger.warning("notifications: DB path unavailable (%s: %s) — "
                           "serving the in-memory queue", type(e).__name__, e)

        # Fallback: in-memory queue
        items = _notification_queue[-limit:]
        if unread:
            items = [n for n in items if not n.get('read')]
        return jsonify({
            'notifications': items,
            'source': 'memory',
        })

    @app.route('/api/shell/notifications/send', methods=['POST'])
    def shell_notification_send():
        """Send a desktop notification via D-Bus (freedesktop.org spec)."""
        data = request.get_json(force=True)
        title = data.get('title', 'HART OS')
        body = data.get('body', '')
        urgency = data.get('urgency', 'normal')  # low, normal, critical
        icon = data.get('icon', 'dialog-information')
        timeout = data.get('timeout', 5000)

        notif = {
            'id': len(_notification_queue) + 1,
            'title': title,
            'body': body,
            'urgency': urgency,
            'icon': icon,
            'timestamp': time.time(),
            'read': False,
        }
        _notification_queue.append(notif)

        # Try D-Bus delivery
        dbus_sent = False
        try:
            result = subprocess.run(
                ['notify-send', '-u', urgency, '-i', icon,
                 '-t', str(timeout), title, body],
                capture_output=True, timeout=5)
            dbus_sent = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return jsonify({
            'sent': True,
            'dbus_delivered': dbus_sent,
            'notification': notif,
        })

    @app.route('/api/shell/notifications/read', methods=['POST'])
    def shell_notification_mark_read():
        """Mark notifications as read."""
        data = request.get_json(force=True)
        ids = data.get('ids', [])
        mark_all = data.get('all', False)

        if mark_all:
            for n in _notification_queue:
                n['read'] = True
            return jsonify({'marked': len(_notification_queue)})

        count = 0
        for n in _notification_queue:
            if n.get('id') in ids:
                n['read'] = True
                count += 1
        return jsonify({'marked': count})

    # ═══════════════════════════════════════════════════════════
    # File Manager — browse, create, delete, move, copy
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/files/browse', methods=['GET'])
    @_require_shell_auth
    def shell_files_browse():
        """Browse directory contents."""
        # expanduser so '~' and '~/Documents' (the explorer's Places sidebar)
        # resolve to the real home; realpath('~') alone would yield a literal
        # './~'. Absolute and relative non-~ paths pass through unchanged.
        path = os.path.expanduser(request.args.get('path', '~'))
        show_hidden = request.args.get('hidden', 'false').lower() == 'true'

        # Security: prevent traversal outside allowed paths
        real_path = os.path.realpath(path)
        if not _is_path_allowed(real_path):
            return jsonify({'error': 'Path outside allowed roots'}), 403
        if not os.path.isdir(real_path):
            return jsonify({'error': 'Not a directory'}), 400

        entries = []
        try:
            for entry in os.scandir(real_path):
                if not show_hidden and entry.name.startswith('.'):
                    continue
                try:
                    stat = entry.stat()
                    entries.append({
                        'name': entry.name,
                        'path': entry.path,
                        'is_dir': entry.is_dir(),
                        'size': stat.st_size if not entry.is_dir() else 0,
                        'modified': stat.st_mtime,
                        'extension': os.path.splitext(entry.name)[1].lower()
                            if not entry.is_dir() else '',
                    })
                except (PermissionError, OSError):
                    pass
        except PermissionError:
            return jsonify({'error': 'Permission denied'}), 403

        # Sort: dirs first, then alphabetical
        entries.sort(key=lambda e: (not e['is_dir'], e['name'].lower()))

        return jsonify({
            'path': real_path,
            'parent': os.path.dirname(real_path),
            'entries': entries,
            'count': len(entries),
        })

    @app.route('/api/shell/files/mkdir', methods=['POST'])
    @_require_shell_auth
    def shell_files_mkdir():
        """Create a directory."""
        data = request.get_json(force=True)
        path = data.get('path', '')
        if not path:
            return jsonify({'error': 'path required'}), 400
        if not _is_path_allowed(path):
            return jsonify({'error': 'Path outside allowed roots'}), 403
        try:
            os.makedirs(path, exist_ok=True)
            _audit_shell_op('mkdir', {'path': path})
            return jsonify({'created': path})
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/shell/files/delete', methods=['POST'])
    @_require_shell_auth
    def shell_files_delete():
        """Delete a file or directory (moves to trash first if available)."""
        data = request.get_json(force=True)
        path = data.get('path', '')
        if not path or not os.path.exists(path):
            return jsonify({'error': 'path not found'}), 400
        if not _is_path_allowed(path):
            return jsonify({'error': 'Path outside allowed roots'}), 403

        if not _classify_destructive(f'delete file: {path}'):
            return jsonify({'error': 'Action classified as destructive — requires approval'}), 403

        _audit_shell_op('file_delete', {'path': path})

        # Try trash first (freedesktop.org spec)
        trashed = False
        try:
            result = subprocess.run(
                ['gio', 'trash', path],
                capture_output=True, timeout=10)
            trashed = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        if not trashed:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except (PermissionError, OSError) as e:
                return jsonify({'error': str(e)}), 400

        return jsonify({'deleted': path, 'trashed': trashed})

    @app.route('/api/shell/files/move', methods=['POST'])
    @_require_shell_auth
    def shell_files_move():
        """Move/rename a file or directory."""
        data = request.get_json(force=True)
        src = data.get('source', '')
        dst = data.get('destination', '')
        if not src or not dst:
            return jsonify({'error': 'source and destination required'}), 400
        if not _is_path_allowed(src) or not _is_path_allowed(dst):
            return jsonify({'error': 'Path outside allowed roots'}), 403
        try:
            _audit_shell_op('file_move', {'from': src, 'to': dst})
            shutil.move(src, dst)
            return jsonify({'moved': {'from': src, 'to': dst}})
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/shell/files/copy', methods=['POST'])
    @_require_shell_auth
    def shell_files_copy():
        """Copy a file or directory."""
        data = request.get_json(force=True)
        src = data.get('source', '')
        dst = data.get('destination', '')
        if not src or not dst:
            return jsonify({'error': 'source and destination required'}), 400
        if not _is_path_allowed(src) or not _is_path_allowed(dst):
            return jsonify({'error': 'Path outside allowed roots'}), 403
        try:
            _audit_shell_op('file_copy', {'from': src, 'to': dst})
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            return jsonify({'copied': {'from': src, 'to': dst}})
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/shell/files/info', methods=['GET'])
    @_require_shell_auth
    def shell_files_info():
        """Get detailed file/directory info."""
        path = request.args.get('path', '')
        if not path or not os.path.exists(path):
            return jsonify({'error': 'path not found'}), 404
        if not _is_path_allowed(path):
            return jsonify({'error': 'Path outside allowed roots'}), 403
        try:
            stat = os.stat(path)
            return jsonify({
                'path': path,
                'name': os.path.basename(path),
                'is_dir': os.path.isdir(path),
                'size': stat.st_size,
                'modified': stat.st_mtime,
                'created': stat.st_ctime,
                'permissions': oct(stat.st_mode)[-3:],
                'extension': os.path.splitext(path)[1].lower(),
            })
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

    # ═══════════════════════════════════════════════════════════
    # Terminal — PTY allocation and I/O
    # ═══════════════════════════════════════════════════════════

    _terminals = {}  # session_id -> {pid, fd, cols, rows}

    @app.route('/api/shell/terminal/create', methods=['POST'])
    @_require_shell_auth
    def shell_terminal_create():
        """Create a new PTY terminal session."""
        data = request.get_json(force=True) if request.data else {}
        cols = data.get('cols', 80)
        rows = data.get('rows', 24)
        shell = data.get('shell', os.environ.get('SHELL', '/bin/bash'))

        try:
            import pty
            import fcntl
            import termios
            import struct

            pid, fd = pty.openpty()
            if pid == 0:
                # Child: exec shell
                os.execlp(shell, shell)
            else:
                # Parent: set terminal size
                winsize = struct.pack('HHHH', rows, cols, 0, 0)
                fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

                session_id = f'term_{pid}'
                _terminals[session_id] = {
                    'pid': pid,
                    'fd': fd,
                    'cols': cols,
                    'rows': rows,
                    'created': time.time(),
                }
                return jsonify({
                    'session_id': session_id,
                    'pid': pid,
                    'cols': cols,
                    'rows': rows,
                })
        except ImportError:
            # Windows: no pty module
            return jsonify({
                'error': 'PTY not available on this platform',
                'fallback': 'Use /api/shell/terminal/exec for command execution',
            }), 501
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/terminal/exec', methods=['POST'])
    @_require_shell_auth
    def shell_terminal_exec():
        """Execute a single command (stateless, cross-platform)."""
        data = request.get_json(force=True)
        command = data.get('command', '')
        timeout = data.get('timeout', 30)
        cwd = data.get('cwd', os.path.expanduser('~'))

        if not command:
            return jsonify({'error': 'command required'}), 400

        # Security: block dangerous patterns
        blocked = ['rm -rf /', 'mkfs', 'dd if=/dev/zero', ':(){', 'fork bomb']
        cmd_lower = command.lower()
        for pattern in blocked:
            if pattern in cmd_lower:
                return jsonify({'error': 'Command blocked by safety filter'}), 403

        # shell=False prevents command injection; shlex.split tokenizes safely.
        try:
            cmd_list = shlex.split(command)
        except ValueError:
            cmd_list = command.split()
        base = os.path.basename(cmd_list[0]) if cmd_list else ''

        # Fast-path obviously read-only diagnostic commands PAST the (LLM-backed)
        # destructive classifier. Otherwise a busy/down local LLM hangs the classify
        # call and the Terminal fetch aborts on EVERY command (the real-HW bug). With
        # shell=False these binaries cannot pipe or redirect, so they stay read-only.
        if base not in _READONLY_SAFE_BINS:
            if not _classify_destructive(f'terminal exec: {command[:200]}'):
                return jsonify({'error': 'Action classified as destructive - requires approval'}), 403

        _audit_shell_op('terminal_exec', {'command': command[:200]})

        try:
            result = subprocess.run(
                cmd_list, shell=False, capture_output=True,
                text=True, timeout=timeout, cwd=cwd)
            return jsonify({
                'stdout': result.stdout[-10000:],  # Cap output
                'stderr': result.stderr[-5000:],
                'returncode': result.returncode,
                'command': command,
            })
        except subprocess.TimeoutExpired:
            return jsonify({
                'error': f'Command timed out after {timeout}s',
                'command': command,
            }), 408
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/terminal/resize', methods=['POST'])
    @_require_shell_auth
    def shell_terminal_resize():
        """Resize a terminal session."""
        data = request.get_json(force=True)
        session_id = data.get('session_id', '')
        cols = data.get('cols', 80)
        rows = data.get('rows', 24)

        if session_id not in _terminals:
            return jsonify({'error': 'Session not found'}), 404

        try:
            import fcntl
            import termios
            import struct
            fd = _terminals[session_id]['fd']
            winsize = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
            _terminals[session_id]['cols'] = cols
            _terminals[session_id]['rows'] = rows
            return jsonify({'resized': True, 'cols': cols, 'rows': rows})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/terminal/sessions', methods=['GET'])
    def shell_terminal_sessions():
        """List active terminal sessions."""
        sessions = []
        for sid, info in list(_terminals.items()):
            sessions.append({
                'session_id': sid,
                'pid': info['pid'],
                'cols': info['cols'],
                'rows': info['rows'],
                'created': info['created'],
            })
        return jsonify({'sessions': sessions})

    # ═══════════════════════════════════════════════════════════
    # User Account Management
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/users', methods=['GET'])
    def shell_users_list():
        """List system users."""
        users = []
        try:
            import pwd
            for pw in pwd.getpwall():
                if pw.pw_uid >= 1000 or pw.pw_name in ('root', 'hart'):
                    users.append({
                        'username': pw.pw_name,
                        'uid': pw.pw_uid,
                        'gid': pw.pw_gid,
                        'home': pw.pw_dir,
                        'shell': pw.pw_shell,
                        'gecos': pw.pw_gecos,
                    })
        except ImportError:
            # Windows fallback
            users.append({
                'username': os.environ.get('USERNAME', 'unknown'),
                'uid': 0,
                'gid': 0,
                'home': os.path.expanduser('~'),
                'shell': os.environ.get('SHELL', 'cmd.exe'),
                'gecos': '',
            })
        return jsonify({'users': users})

    @app.route('/api/shell/users/create', methods=['POST'])
    @_require_shell_auth
    def shell_users_create():
        """Create a new system user (requires root)."""
        data = request.get_json(force=True)
        username = data.get('username', '')
        password = data.get('password', '')
        groups = data.get('groups', ['hart'])

        if not username:
            return jsonify({'error': 'username required'}), 400
        if len(username) < 2 or not username.isalnum():
            return jsonify({'error': 'Invalid username (alphanumeric, 2+ chars)'}), 400

        # G7: Sanitize group names — only allow alphanumeric, hyphens, underscores
        import re as _re_users
        for grp in groups:
            if not _re_users.match(r'^[a-zA-Z0-9_-]+$', str(grp)):
                return jsonify({'error': f'Invalid group name: {grp}'}), 400

        try:
            group_str = ','.join(groups)
            result = subprocess.run(
                ['useradd', '-m', '-G', group_str, '-s', '/bin/bash', username],
                capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return jsonify({'error': result.stderr.strip()}), 400

            if password:
                proc = subprocess.run(
                    ['chpasswd'],
                    input=f'{username}:{password}',
                    capture_output=True, text=True, timeout=10)
                if proc.returncode != 0:
                    return jsonify({'error': 'User created but password set failed'}), 500

            return jsonify({'created': username, 'groups': groups})
        except FileNotFoundError:
            return jsonify({'error': 'useradd not available'}), 501
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/users/delete', methods=['POST'])
    @_require_shell_auth
    def shell_users_delete():
        """Delete a system user (requires root)."""
        data = request.get_json(force=True)
        username = data.get('username', '')
        remove_home = data.get('remove_home', False)

        if not username or username in ('root', 'hart', 'hart-admin'):
            return jsonify({'error': 'Cannot delete protected user'}), 403

        if not _classify_destructive(f'delete user: {username}'):
            return jsonify({'error': 'Action classified as destructive — requires approval'}), 403

        try:
            cmd = ['userdel']
            if remove_home:
                cmd.append('-r')
            cmd.append(username)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return jsonify({'error': result.stderr.strip()}), 400
            return jsonify({'deleted': username})
        except FileNotFoundError:
            return jsonify({'error': 'userdel not available'}), 501
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ═══════════════════════════════════════════════════════════
    # First-Time Setup Wizard
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/setup/status', methods=['GET'])
    def shell_setup_status():
        """Check first-time setup completion status."""
        data_dir = os.environ.get('HEVOLVE_DATA_DIR', '/var/lib/hart')
        marker = os.path.join(data_dir, '.first-boot-done')
        wizard_state_path = os.path.join(data_dir, 'wizard_state.json')

        wizard_state = {}
        if os.path.isfile(wizard_state_path):
            try:
                with open(wizard_state_path) as f:
                    wizard_state = json.load(f)
            except Exception:
                pass

        return jsonify({
            'first_boot_done': os.path.isfile(marker),
            'wizard_completed': wizard_state.get('completed', False),
            'current_step': wizard_state.get('current_step', 0),
            'steps': [
                {'id': 'welcome', 'title': 'Welcome', 'completed': wizard_state.get('welcome', False)},
                {'id': 'network', 'title': 'Network Setup', 'completed': wizard_state.get('network', False)},
                {'id': 'account', 'title': 'User Account', 'completed': wizard_state.get('account', False)},
                {'id': 'ai_models', 'title': 'AI Models', 'completed': wizard_state.get('ai_models', False)},
                {'id': 'privacy', 'title': 'Privacy & Security', 'completed': wizard_state.get('privacy', False)},
            ],
        })

    @app.route('/api/shell/setup/step', methods=['POST'])
    def shell_setup_step():
        """Complete a setup wizard step."""
        data = request.get_json(force=True)
        step_id = data.get('step', '')
        step_data = data.get('data', {})

        data_dir = os.environ.get('HEVOLVE_DATA_DIR', '/var/lib/hart')
        wizard_state_path = os.path.join(data_dir, 'wizard_state.json')

        # Load current state
        state = {}
        if os.path.isfile(wizard_state_path):
            try:
                with open(wizard_state_path) as f:
                    state = json.load(f)
            except Exception:
                pass

        # Mark step complete
        state[step_id] = True
        state.setdefault('step_data', {})[step_id] = step_data

        # Check if all steps done
        required = ['welcome', 'network', 'account', 'ai_models', 'privacy']
        all_done = all(state.get(s) for s in required)
        if all_done:
            state['completed'] = True

        state['current_step'] = state.get('current_step', 0) + 1

        # Save
        try:
            os.makedirs(data_dir, exist_ok=True)
            with open(wizard_state_path, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        return jsonify({
            'step': step_id,
            'completed': all_done,
            'current_step': state['current_step'],
        })

    # ═══════════════════════════════════════════════════════════
    # Backup Restore
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/backup/list', methods=['GET'])
    def shell_backup_list():
        """List available backups for a user."""
        user_id = request.args.get('user_id', '1')
        try:
            from integrations.social.backup_service import list_backups
            from integrations.social.models import db_session
            with db_session() as db:
                backups = list_backups(db, int(user_id))
                return jsonify({
                    'backups': [b.to_dict() if hasattr(b, 'to_dict')
                                else {'id': str(b)} for b in backups],
                    'count': len(backups),
                })
        except (ImportError, Exception) as e:
            return jsonify({'backups': [], 'error': str(e)})

    @app.route('/api/shell/backup/restore', methods=['POST'])
    def shell_backup_restore():
        """Restore from a backup."""
        data = request.get_json(force=True)
        user_id = data.get('user_id')
        passphrase = data.get('passphrase', '')
        backup_id = data.get('backup_id')

        if not user_id or not passphrase:
            return jsonify({'error': 'user_id and passphrase required'}), 400

        try:
            from integrations.social.backup_service import restore_backup
            from integrations.social.models import db_session
            with db_session() as db:
                result = restore_backup(db, int(user_id), passphrase, backup_id)
                return jsonify({
                    'restored': True,
                    'profile': bool(result.get('profile')),
                    'posts': len(result.get('posts', [])),
                    'comments': len(result.get('comments', [])),
                    'votes': len(result.get('votes', [])),
                })
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    # ═══════════════════════════════════════════════════════════
    # Power Management
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/power/profiles', methods=['GET'])
    def shell_power_profiles():
        """List available power profiles."""
        profiles = ['performance', 'balanced', 'powersave']
        active = 'balanced'
        try:
            result = subprocess.run(
                ['powerprofilesctl', 'get'],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                active = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Battery info
        battery = None
        for bat_path in ['/sys/class/power_supply/BAT0',
                         '/sys/class/power_supply/BAT1']:
            cap_file = os.path.join(bat_path, 'capacity')
            if os.path.isfile(cap_file):
                try:
                    with open(cap_file) as f:
                        battery = {
                            'percent': int(f.read().strip()),
                            'status': open(os.path.join(bat_path, 'status')).read().strip(),
                        }
                except Exception:
                    pass
                break

        return jsonify({
            'profiles': profiles,
            'active': active,
            'battery': battery,
        })

    @app.route('/api/shell/power/set', methods=['POST'])
    @_require_shell_auth
    def shell_power_set():
        """Set power profile."""
        data = request.get_json(force=True)
        profile = data.get('profile', '')
        if profile not in ('performance', 'balanced', 'powersave'):
            return jsonify({'error': 'Invalid profile'}), 400
        try:
            result = subprocess.run(
                ['powerprofilesctl', 'set', profile],
                capture_output=True, text=True, timeout=5)
            return jsonify({
                'set': profile,
                'success': result.returncode == 0,
            })
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return jsonify({'error': 'powerprofilesctl not available'}), 501

    @app.route('/api/shell/power/action', methods=['POST'])
    @_require_shell_auth
    def shell_power_action():
        """Execute power action (suspend, hibernate, reboot, shutdown, lock,
        firmware/uefi)."""
        data = request.get_json(force=True)
        action = data.get('action', '')

        # These are an ENUMERATED, intentional set of power verbs (the whitelist) —
        # already behind the local-shell auth gate (@_require_shell_auth) and (for
        # firmware/uefi) the firmware-capability gate below. They must NOT be
        # routed through the FREE-FORM destructive classifier: that classifier
        # exists for arbitrary command/file text (terminal exec, file delete),
        # and it (a) refuses 'reboot'/'shutdown' as destructive and (b) refuses
        # 'firmware'/'suspend'/'hibernate' as 'unknown' (fail-closed) — so on a
        # real box EVERY power action 403'd and the firmware feature was dead.
        # The canonical gate for a power verb is membership in this whitelist
        # (and the capability probe for firmware/uefi).
        #
        # Each verb maps to a NATIVE logind (org.freedesktop.login1.Manager) D-Bus
        # method, invoked + result-checked by `_logind_call` above. The interactive
        # boolean is `true` so logind may consult polkit; the hart-base.nix
        # security.polkit rule grants the `hart` shell user these login1 actions
        # outright, so the call is authorized without a prompt. This replaces the
        # old fire-and-forget `subprocess.Popen(['systemctl', ...])` that masked a
        # polkit denial as `{'initiated': True}` while the box never powered down.
        # DRY (#165): the verb -> login1-method map is the ONE canonical copy in
        # os_bridge.power._POWER_METHOD (reboot/shutdown/suspend/hibernate); reuse
        # it, do NOT redefine. `lock` is handled by its own no-arg branch below.
        # This route is the BACKWARD-COMPAT surface (keeps the {action,initiated}
        # shape + the _logind_call path existing callers/mocks depend on); the
        # typed forward path new callers should use is POST /api/os/invoke
        # (os_bridge.routes -> os_bridge.power.invoke_power, same logind_call).
        from integrations.agent_engine.os_bridge.power import _POWER_METHOD
        # firmware/uefi = "Restart into Firmware (UEFI)": arm the UEFI boot-to-
        # firmware-UI flag (SetRebootToFirmwareSetup true), THEN reboot — the next
        # boot enters the BIOS/UEFI setup. Two-step; if arming fails we do NOT
        # reboot (a plain reboot would be the wrong action for the user's intent).
        valid_actions = list(_POWER_METHOD.keys()) + ['lock', 'firmware', 'uefi']
        if action not in valid_actions:
            return jsonify({'error': f'Invalid action. Valid: {valid_actions}'}), 400

        # Gate firmware setup to UEFI boxes that advertise the capability — never
        # give the user a plain reboot when they asked to enter firmware setup.
        if action in ('firmware', 'uefi') and not firmware_setup_supported():
            return jsonify({'error': 'Reboot to firmware setup is not supported on '
                                     'this system (legacy BIOS or capability not '
                                     'advertised)'}), 400

        _audit_shell_op('power_action', {'action': action})

        if action in ('firmware', 'uefi'):
            ok, err = _logind_call('SetRebootToFirmwareSetup', 'b', 'true')
            if not ok:
                return jsonify({'action': action, 'initiated': False,
                                'error': f'Could not arm firmware setup: {err}'}), 500
            ok, err = _logind_call('Reboot', 'b', 'true')
        elif action == 'lock':
            ok, err = _logind_call('LockSessions')
        else:
            ok, err = _logind_call(_POWER_METHOD[action], 'b', 'true')

        if not ok:
            # Real failure (polkit denied, busctl missing, timeout) — surface it
            # as an error, never a masked {'initiated': True}.
            return jsonify({'action': action, 'initiated': False, 'error': err}), 500
        return jsonify({'action': action, 'initiated': True})

    @app.route('/api/shell/power/checkpoint', methods=['POST'])
    def shell_power_checkpoint():
        """Checkpoint agent state before suspend."""
        return jsonify({'checkpointed': True, 'timestamp': time.time()})

    @app.route('/api/shell/power/resume', methods=['POST'])
    def shell_power_resume():
        """Signal resume from suspend."""
        return jsonify({'resumed': True, 'timestamp': time.time()})

    # ═══════════════════════════════════════════════════════════
    # i18n — Internationalization
    # ═══════════════════════════════════════════════════════════

    _i18n_strings = {}  # locale -> {key: translation}
    _current_locale = 'en'

    @app.route('/api/shell/i18n/locales', methods=['GET'])
    def shell_i18n_locales():
        """List available locales."""
        locales = [
            {'code': 'en', 'name': 'English', 'native': 'English', 'rtl': False},
            {'code': 'es', 'name': 'Spanish', 'native': 'Español', 'rtl': False},
            {'code': 'fr', 'name': 'French', 'native': 'Français', 'rtl': False},
            {'code': 'de', 'name': 'German', 'native': 'Deutsch', 'rtl': False},
            {'code': 'ja', 'name': 'Japanese', 'native': '日本語', 'rtl': False},
            {'code': 'zh', 'name': 'Chinese', 'native': '中文', 'rtl': False},
            {'code': 'ko', 'name': 'Korean', 'native': '한국어', 'rtl': False},
            {'code': 'ar', 'name': 'Arabic', 'native': 'العربية', 'rtl': True},
            {'code': 'hi', 'name': 'Hindi', 'native': 'हिन्दी', 'rtl': False},
            {'code': 'pt', 'name': 'Portuguese', 'native': 'Português', 'rtl': False},
            {'code': 'ru', 'name': 'Russian', 'native': 'Русский', 'rtl': False},
        ]

        # Detect system locale
        system_locale = os.environ.get('LANG', 'en_US.UTF-8').split('.')[0].split('_')[0]

        return jsonify({
            'locales': locales,
            'current': _current_locale,
            'system': system_locale,
        })

    @app.route('/api/shell/i18n/set', methods=['POST'])
    def shell_i18n_set():
        """Set active locale."""
        nonlocal _current_locale
        data = request.get_json(force=True)
        locale = data.get('locale', 'en')
        _current_locale = locale
        return jsonify({'locale': locale, 'set': True})

    @app.route('/api/shell/i18n/strings', methods=['GET'])
    def shell_i18n_strings():
        """Get translation strings for current or specified locale."""
        locale = request.args.get('locale', _current_locale)

        # Load locale file if exists
        strings = _i18n_strings.get(locale, {})
        if not strings:
            locale_dir = os.environ.get('HART_LOCALE_DIR',
                os.path.join(os.path.dirname(__file__), '..', '..', 'locales'))
            locale_file = os.path.join(locale_dir, f'{locale}.json')
            if os.path.isfile(locale_file):
                try:
                    with open(locale_file) as f:
                        strings = json.load(f)
                    _i18n_strings[locale] = strings
                except Exception:
                    pass

        return jsonify({
            'locale': locale,
            'strings': strings,
            'count': len(strings),
        })

    # ═══════════════════════════════════════════════════════════
    # Accessibility
    # ═══════════════════════════════════════════════════════════

    _a11y_settings = _A11Y_SETTINGS  # module-level shared state (see top of file)

    @app.route('/api/shell/accessibility', methods=['GET'])
    def shell_accessibility_get():
        """Get current accessibility settings (live state — seeded from the NixOS
        declarative file at import, plus any runtime PUT overrides)."""
        return jsonify(_a11y_settings)

    @app.route('/api/shell/accessibility', methods=['PUT'])
    def shell_accessibility_set():
        """Update accessibility settings (runtime override)."""
        data = request.get_json(force=True)
        for key in _a11y_settings:
            if key in data:
                _a11y_settings[key] = data[key]
        return jsonify(_a11y_settings)

    # ═══════════════════════════════════════════════════════════
    # Screenshot / Screen Recording
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/screenshot', methods=['POST'])
    @_require_shell_auth
    def shell_screenshot():
        """Take a screenshot."""
        data = request.get_json(force=True) if request.data else {}
        region = data.get('region')  # {x, y, width, height} or None for full
        output_dir = data.get('output_dir',
            os.path.expanduser('~/Pictures/Screenshots'))
        os.makedirs(output_dir, exist_ok=True)

        filename = f'screenshot_{int(time.time())}.png'
        output_path = os.path.join(output_dir, filename)

        # Try multiple screenshot tools
        captured = False
        for tool_cmd in [
            ['grim', output_path],                          # Wayland
            ['scrot', output_path],                         # X11
            ['gnome-screenshot', '-f', output_path],        # GNOME
            ['import', '-window', 'root', output_path],     # ImageMagick
        ]:
            try:
                result = subprocess.run(
                    tool_cmd, capture_output=True, timeout=10)
                if result.returncode == 0 and os.path.isfile(output_path):
                    captured = True
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

        # Fallback: try mss (Python)
        if not captured:
            try:
                import mss
                with mss.mss() as sct:
                    sct.shot(output=output_path)
                    captured = True
            except ImportError:
                pass

        if captured:
            size = os.path.getsize(output_path)
            return jsonify({
                'captured': True,
                'path': output_path,
                'filename': filename,
                'size': size,
            })
        return jsonify({'captured': False, 'error': 'No screenshot tool available'}), 501

    @app.route('/api/shell/recording/start', methods=['POST'])
    @_require_shell_auth
    def shell_recording_start():
        """Start screen recording."""
        data = request.get_json(force=True) if request.data else {}
        output_dir = data.get('output_dir',
            os.path.expanduser('~/Videos/Recordings'))
        os.makedirs(output_dir, exist_ok=True)

        filename = f'recording_{int(time.time())}.mp4'
        output_path = os.path.join(output_dir, filename)

        # Try wf-recorder (Wayland) or ffmpeg (X11)
        for tool_cmd in [
            ['wf-recorder', '-f', output_path],
            ['ffmpeg', '-f', 'x11grab', '-i', ':0', '-y', output_path],
        ]:
            try:
                # ffmpeg is cross-platform — on Windows users with ffmpeg
                # on PATH this would pop a brief cmd console even though
                # the x11grab arg means the spawn fails immediately.
                # Hide via canonical helper (no-op on macOS/Linux).
                from core.subprocess_safe import hidden_popen_kwargs
                proc = subprocess.Popen(
                    tool_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **hidden_popen_kwargs(),
                )
                return jsonify({
                    'recording': True,
                    'pid': proc.pid,
                    'path': output_path,
                    'filename': filename,
                })
            except FileNotFoundError:
                continue

        return jsonify({'recording': False, 'error': 'No recording tool available'}), 501

    @app.route('/api/shell/recording/stop', methods=['POST'])
    @_require_shell_auth
    def shell_recording_stop():
        """Stop screen recording."""
        data = request.get_json(force=True) if request.data else {}
        pid = data.get('pid')
        if pid:
            try:
                os.kill(pid, 2)  # SIGINT
                return jsonify({'stopped': True, 'pid': pid})
            except (ProcessLookupError, PermissionError) as e:
                return jsonify({'error': str(e)}), 400
        return jsonify({'error': 'pid required'}), 400

    # ═══════════════════════════════════════════════════════════
    # Multi-Device Pairing (Compute Mesh UI bridge)
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/devices', methods=['GET'])
    def shell_devices_list():
        """List paired devices in the compute mesh."""
        try:
            import requests as req
            mesh_port = os.environ.get('MESH_TASK_RELAY_PORT', '6796')
            resp = req.get(f'http://localhost:{mesh_port}/mesh/peers', timeout=3)
            if resp.ok:
                return jsonify(resp.json())
        except Exception as e:
            # Same shape: a real fallback, but a silent one made "no paired
            # devices" indistinguishable from "the mesh relay is down".
            logger.warning("devices: mesh relay unreachable (%s: %s) — "
                           "falling back to the peer files on disk",
                           type(e).__name__, e)

        # Fallback: read peer files
        peer_dir = os.environ.get(
            'MESH_PEER_DIR', '/var/lib/hart/mesh/peers')
        peers = []
        if os.path.isdir(peer_dir):
            for fname in os.listdir(peer_dir):
                if fname.endswith('.json'):
                    try:
                        with open(os.path.join(peer_dir, fname)) as f:
                            peers.append(json.load(f))
                    except Exception:
                        pass
        return jsonify({'peers': peers, 'count': len(peers)})

    @app.route('/api/shell/devices/pair', methods=['POST'])
    @_require_shell_auth
    def shell_devices_pair():
        """Initiate device pairing."""
        data = request.get_json(force=True)
        address = data.get('address', '')
        if not address:
            return jsonify({'error': 'address required'}), 400

        try:
            import requests as req
            mesh_port = os.environ.get('MESH_TASK_RELAY_PORT', '6796')
            resp = req.post(
                f'http://localhost:{mesh_port}/mesh/pair',
                json={'peer_address': address}, timeout=10)
            return jsonify(resp.json())
        except Exception as e:
            return jsonify({'error': str(e), 'address': address}), 500

    @app.route('/api/shell/devices/unpair', methods=['POST'])
    @_require_shell_auth
    def shell_devices_unpair():
        """Remove a paired device."""
        data = request.get_json(force=True)
        device_id = data.get('device_id', '')
        if not device_id:
            return jsonify({'error': 'device_id required'}), 400

        peer_dir = os.environ.get(
            'MESH_PEER_DIR', '/var/lib/hart/mesh/peers')
        peer_file = os.path.join(peer_dir, f'{device_id}.json')
        if os.path.isfile(peer_file):
            os.remove(peer_file)
            return jsonify({'unpaired': device_id})
        return jsonify({'error': 'Device not found'}), 404

    # ═══════════════════════════════════════════════════════════
    # OTA Update API (bridge to upgrade_orchestrator)
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/upgrades/status', methods=['GET'])
    def upgrades_status():
        """Get current upgrade pipeline status."""
        try:
            from integrations.agent_engine.upgrade_orchestrator import get_upgrade_orchestrator
            orch = get_upgrade_orchestrator()
            return jsonify(orch.get_status())
        except (ImportError, Exception) as e:
            return jsonify({'stage': 'idle', 'error': str(e)})

    @app.route('/api/upgrades/start', methods=['POST'])
    @_require_shell_auth
    def upgrades_start():
        """Start upgrade pipeline."""
        data = request.get_json(force=True)
        version = data.get('version', '')
        sha = data.get('sha', '')
        if not version:
            return jsonify({'error': 'version required'}), 400
        try:
            from integrations.agent_engine.upgrade_orchestrator import get_upgrade_orchestrator
            orch = get_upgrade_orchestrator()
            result = orch.start_upgrade(version, sha)
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/upgrades/advance', methods=['POST'])
    def upgrades_advance():
        """Advance upgrade pipeline to next stage."""
        try:
            from integrations.agent_engine.upgrade_orchestrator import get_upgrade_orchestrator
            orch = get_upgrade_orchestrator()
            result = orch.advance_pipeline()
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/upgrades/rollback', methods=['POST'])
    @_require_shell_auth
    def upgrades_rollback():
        """Rollback current upgrade."""
        data = request.get_json(force=True) if request.data else {}
        reason = data.get('reason', 'manual_rollback')
        try:
            from integrations.agent_engine.upgrade_orchestrator import get_upgrade_orchestrator
            orch = get_upgrade_orchestrator()
            result = orch.rollback(reason)
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ─── Battery / WiFi / VPN ────────────────────────────────
    # Canonical hardware control (battery, WiFi, VPN) lives in
    # shell_system_apis.register_shell_system_routes — it owns the richer set
    # (wifi networks/saved/toggle, vpn status/delete, battery profile) and the
    # frontend panels call it.  Duplicating it here registered SECOND views
    # with the SAME Flask endpoint names (shell_battery_status,
    # shell_wifi_connect, shell_vpn_list, …) → AssertionError when
    # register_shell_system_routes ran after register_shell_os_routes → it
    # aborted AND register_app_install_routes never ran (app installer dead).
    # Removed the dups; only the lid-action endpoint (no shell_system
    # equivalent) stays here.  (2026-06-01)

    @app.route('/api/shell/power/lid', methods=['GET', 'PUT'])
    def shell_lid_action():
        """Get/set lid close action (logind.conf HandleLidSwitch)."""
        VALID_ACTIONS = {'suspend', 'hibernate', 'poweroff', 'lock', 'ignore'}
        if request.method == 'GET':
            action = 'suspend'  # default
            try:
                import configparser
                cp = configparser.ConfigParser()
                cp.read('/etc/systemd/logind.conf')
                action = cp.get('Login', 'HandleLidSwitch', fallback='suspend')
            except Exception:
                pass
            return jsonify({'action': action, 'valid_actions': sorted(VALID_ACTIONS)})
        body = request.get_json(silent=True) or {}
        action = body.get('action', '')
        if action not in VALID_ACTIONS:
            return jsonify({'error': f'Invalid action. Must be one of: {sorted(VALID_ACTIONS)}'}), 400
        return jsonify({'status': 'ok', 'action': action,
                        'note': 'Requires root to modify logind.conf'})

    # ─── Trash / Recycle Bin ────────────────────────────────
    # Canonical trash lives in shell_system_apis.register_shell_system_routes
    # (it owns /api/shell/trash + /move + /restore + DELETE /empty — the exact
    # shape the shell's recycle-bin panel calls, per shell_manifest SYSTEM_PANELS).
    # This module USED to also define '/api/shell/trash' GET as a view named
    # `shell_trash_list`; register_shell_os_routes runs first, so when
    # register_shell_system_routes ran second Flask raised
    # "View function mapping is overwriting an existing endpoint function:
    # shell_trash_list" — which aborted shell_system registration partway AND
    # meant register_app_install_routes (called right after, in
    # liquid_ui_service) never ran, silently killing the app installer.
    # Removed the duplicate; trash is shell_system's responsibility.  (2026-06-01)

    # ─── Notes App ──────────────────────────────────────────

    _NOTES_DIR = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'agent_data', 'notes')

    @app.route('/api/shell/notes', methods=['GET'])
    def shell_notes_list():
        """List all notes."""
        os.makedirs(_NOTES_DIR, exist_ok=True)
        notes = []
        for fname in sorted(os.listdir(_NOTES_DIR)):
            if fname.endswith('.json'):
                try:
                    with open(os.path.join(_NOTES_DIR, fname)) as f:
                        note = json.load(f)
                        note['id'] = fname.replace('.json', '')
                        notes.append(note)
                except Exception:
                    pass
        return jsonify({'notes': notes})

    @app.route('/api/shell/notes', methods=['POST'])
    def shell_notes_save():
        """Save a new note."""
        body = request.get_json(silent=True) or {}
        title = body.get('title', 'Untitled')
        content = body.get('content', '')
        if not content:
            return jsonify({'error': 'content is required'}), 400
        os.makedirs(_NOTES_DIR, exist_ok=True)
        from datetime import datetime
        note_id = f"note_{int(time.time() * 1000)}"
        note = {'title': title, 'content': content,
                'created': datetime.now().isoformat(),
                'modified': datetime.now().isoformat()}
        with open(os.path.join(_NOTES_DIR, f'{note_id}.json'), 'w') as f:
            json.dump(note, f, indent=2)
        return jsonify({'status': 'saved', 'id': note_id}), 201

    @app.route('/api/shell/notes/<note_id>', methods=['DELETE'])
    def shell_notes_delete(note_id):
        """Delete a note."""
        path = os.path.join(_NOTES_DIR, f'{note_id}.json')
        if not os.path.isfile(path):
            return jsonify({'error': 'Note not found'}), 404
        os.remove(path)
        return jsonify({'status': 'deleted', 'id': note_id})

    # ─── Media Player (open-with) ───────────────────────────

    @app.route('/api/shell/open-with', methods=['POST'])
    def shell_open_with():
        """Open a file with the system's default application."""
        body = request.get_json(silent=True) or {}
        path = body.get('path', '')
        if not path:
            return jsonify({'error': 'path is required'}), 400
        if not os.path.isfile(path):
            return jsonify({'error': 'File not found'}), 404
        # Sandbox check
        resolved = os.path.realpath(path)
        allowed_roots = [os.path.expanduser('~'), '/tmp', '/var/tmp']
        if not any(resolved.startswith(root) for root in allowed_roots):
            return jsonify({'error': 'Path outside allowed directories'}), 403
        try:
            subprocess.Popen(['xdg-open', resolved],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return jsonify({'status': 'opened', 'path': resolved})
        except FileNotFoundError:
            return jsonify({'error': 'xdg-open not available'}), 500

    # ── Self-Build API ──────────────────────────────────────────
    # Runtime OS rebuilding — the OS modifies and rebuilds itself

    @app.route('/api/system/self-build/status', methods=['GET'])
    def _self_build_status():
        """Current self-build status and generation info."""
        from flask import jsonify
        info = {'self_build_available': False}

        try:
            result = subprocess.run(
                ['nixos-version'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                info['nixos_version'] = result.stdout.strip()
                info['self_build_available'] = True
        except Exception:
            pass

        # Current generation
        gen_link = '/nix/var/nix/profiles/system'
        if os.path.islink(gen_link):
            info['current_generation'] = os.readlink(gen_link)

        # Runtime config exists?
        runtime_nix = '/etc/hart/runtime.nix'
        info['runtime_config_exists'] = os.path.isfile(runtime_nix)

        # Build history
        history_file = '/var/lib/hart/ota/history/builds.jsonl'
        if os.path.isfile(history_file):
            try:
                with open(history_file) as f:
                    lines = f.readlines()
                info['recent_builds'] = [
                    json.loads(l) for l in lines[-5:] if l.strip()]
            except Exception:
                pass

        return jsonify(info)

    @app.route('/api/system/self-build/packages', methods=['GET'])
    def _self_build_packages():
        """List runtime-installed packages."""
        from flask import jsonify
        runtime_nix = '/etc/hart/runtime.nix'
        packages = []
        if os.path.isfile(runtime_nix):
            try:
                with open(runtime_nix) as f:
                    in_packages = False
                    for line in f:
                        stripped = line.strip()
                        if 'systemPackages' in stripped:
                            in_packages = True
                            continue
                        if in_packages and stripped == '];':
                            break
                        if in_packages and stripped and not stripped.startswith('#'):
                            packages.append(stripped)
            except Exception:
                pass
        return jsonify({'packages': packages})

    @app.route('/api/system/self-build/install', methods=['POST'])
    def _self_build_install():
        """Add a package to runtime config (requires self-build to apply)."""
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        package = data.get('package', '').strip()
        if not package or not package.replace('-', '').replace('_', '').isalnum():
            return jsonify({'error': 'Invalid package name'}), 400

        runtime_nix = '/etc/hart/runtime.nix'
        if not os.path.isfile(runtime_nix):
            return jsonify({'error': 'Runtime config not found'}), 404

        try:
            with open(runtime_nix) as f:
                content = f.read()
            if package in content:
                return jsonify({'status': 'already_installed', 'package': package})
            content = content.replace(
                '# Packages added at runtime appear here',
                f'# Packages added at runtime appear here\n    {package}')
            with open(runtime_nix, 'w') as f:
                f.write(content)
            return jsonify({
                'status': 'staged',
                'package': package,
                'message': 'Run self-build to apply',
            })
        except PermissionError:
            return jsonify({'error': 'Permission denied'}), 403

    @app.route('/api/system/self-build/remove', methods=['POST'])
    def _self_build_remove():
        """Remove a package from runtime config."""
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        package = data.get('package', '').strip()
        if not package:
            return jsonify({'error': 'Package name required'}), 400

        runtime_nix = '/etc/hart/runtime.nix'
        if not os.path.isfile(runtime_nix):
            return jsonify({'error': 'Runtime config not found'}), 404

        try:
            with open(runtime_nix) as f:
                lines = f.readlines()
            new_lines = [l for l in lines if package not in l.strip()
                         or l.strip().startswith('#')]
            if len(new_lines) == len(lines):
                return jsonify({'status': 'not_found', 'package': package})
            with open(runtime_nix, 'w') as f:
                f.writelines(new_lines)
            return jsonify({
                'status': 'staged_removal',
                'package': package,
                'message': 'Run self-build to apply',
            })
        except PermissionError:
            return jsonify({'error': 'Permission denied'}), 403

    @app.route('/api/system/self-build/trigger', methods=['POST'])
    def _self_build_trigger():
        """Trigger a self-build (dry-run or switch)."""
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        mode = data.get('mode', 'dry-run')
        if mode not in ('dry-run', 'switch', 'diff'):
            return jsonify({'error': 'Mode must be dry-run, switch, or diff'}), 400

        try:
            result = subprocess.run(
                ['hart-self-build', mode],
                capture_output=True, text=True, timeout=600)
            return jsonify({
                'status': 'completed' if result.returncode == 0 else 'failed',
                'mode': mode,
                'returncode': result.returncode,
                'output': result.stdout[-2000:] if result.stdout else '',
                'errors': result.stderr[-1000:] if result.stderr else '',
            })
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Build timed out (10 min limit)'}), 504
        except FileNotFoundError:
            return jsonify({'error': 'hart-self-build not available (not on NixOS?)'}), 501

    @app.route('/api/system/generations', methods=['GET'])
    def _system_generations():
        """List NixOS generations (rollback targets)."""
        from flask import jsonify
        generations = []
        profile_dir = '/nix/var/nix/profiles'
        if os.path.isdir(profile_dir):
            try:
                for entry in sorted(os.listdir(profile_dir), reverse=True):
                    if entry.startswith('system-') and entry.endswith('-link'):
                        gen_num = entry.replace('system-', '').replace('-link', '')
                        target = os.readlink(os.path.join(profile_dir, entry))
                        generations.append({
                            'generation': gen_num,
                            'path': target,
                        })
            except Exception:
                pass
        current = ''
        if os.path.islink(os.path.join(profile_dir, 'system')):
            current = os.readlink(os.path.join(profile_dir, 'system'))
        return jsonify({
            'current': current,
            'generations': generations[:20],
        })

    @app.route('/api/system/rollback', methods=['POST'])
    def _system_rollback():
        """Rollback to previous NixOS generation."""
        from flask import jsonify
        try:
            result = subprocess.run(
                ['sudo', 'nixos-rebuild', 'switch', '--rollback'],
                capture_output=True, text=True, timeout=300)
            return jsonify({
                'status': 'rolled_back' if result.returncode == 0 else 'failed',
                'output': result.stdout[-2000:] if result.stdout else '',
            })
        except FileNotFoundError:
            return jsonify({'error': 'nixos-rebuild not available'}), 501

    # ─── Cloud File Sync (rclone wrapper) ────────────────────

    _SYNC_CONFIG = os.path.expanduser('~/.config/hart/cloud-sync.json')

    def _load_sync_config():
        try:
            with open(_SYNC_CONFIG) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {'remotes': [], 'sync_pairs': []}

    def _save_sync_config(data):
        os.makedirs(os.path.dirname(_SYNC_CONFIG), exist_ok=True)
        with open(_SYNC_CONFIG, 'w') as f:
            json.dump(data, f, indent=2)

    @app.route('/api/shell/cloud-sync/remotes', methods=['GET'])
    def shell_sync_remotes():
        """List configured rclone remotes."""
        r = run_probe(['rclone', 'listremotes'], timeout=10)
        remotes = []
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                name = line.strip().rstrip(':')
                if name:
                    remotes.append({'name': name})
        return jsonify({'remotes': remotes, 'rclone_available': r is not None})

    @app.route('/api/shell/cloud-sync/pairs', methods=['GET'])
    def shell_sync_pairs():
        """List configured sync pairs (local ↔ remote)."""
        cfg = _load_sync_config()
        return jsonify({'pairs': cfg.get('sync_pairs', [])})

    @app.route('/api/shell/cloud-sync/pairs', methods=['POST'])
    @_require_shell_auth
    def shell_sync_add_pair():
        """Add a sync pair (local dir ↔ remote path)."""
        body = request.get_json(silent=True) or {}
        local_path = body.get('local_path')
        remote_path = body.get('remote_path')
        direction = body.get('direction', 'bisync')  # sync, bisync, copy

        if not local_path or not remote_path:
            return jsonify({'error': 'local_path and remote_path required'}), 400

        # Path safety
        real = os.path.realpath(os.path.expanduser(local_path))
        home = os.path.realpath(os.path.expanduser('~'))
        if not real.startswith(home):
            return jsonify({'error': 'local_path must be under home directory'}), 403

        cfg = _load_sync_config()
        pair = {
            'id': f"pair_{int(time.time())}",
            'local_path': local_path,
            'remote_path': remote_path,
            'direction': direction,
            'created': time.time(),
            'last_sync': None,
        }
        cfg.setdefault('sync_pairs', []).append(pair)
        _save_sync_config(cfg)

        _audit_shell_op('cloud_sync_add', {
            'local': local_path, 'remote': remote_path})
        return jsonify({'added': True, 'pair': pair}), 201

    @app.route('/api/shell/cloud-sync/pairs/<pair_id>', methods=['DELETE'])
    @_require_shell_auth
    def shell_sync_remove_pair(pair_id):
        """Remove a sync pair."""
        cfg = _load_sync_config()
        pairs = cfg.get('sync_pairs', [])
        cfg['sync_pairs'] = [p for p in pairs if p.get('id') != pair_id]
        _save_sync_config(cfg)
        _audit_shell_op('cloud_sync_remove', {'pair_id': pair_id})
        return jsonify({'removed': True})

    @app.route('/api/shell/cloud-sync/run', methods=['POST'])
    @_require_shell_auth
    def shellrun_probe():
        """Trigger sync for a specific pair or all pairs."""
        body = request.get_json(silent=True) or {}
        pair_id = body.get('pair_id')

        cfg = _load_sync_config()
        pairs = cfg.get('sync_pairs', [])
        if pair_id:
            pairs = [p for p in pairs if p.get('id') == pair_id]

        if not pairs:
            return jsonify({'error': 'No sync pairs configured'}), 400

        results = []
        for pair in pairs:
            local = pair['local_path']
            remote = pair['remote_path']
            direction = pair.get('direction', 'sync')

            cmd = ['rclone', direction, local, remote]
            if direction == 'bisync':
                cmd = ['rclone', 'bisync', local, remote, '--resync']

            r = run_probe(cmd, timeout=300)
            success = r is not None and r.returncode == 0
            results.append({
                'pair_id': pair.get('id'),
                'success': success,
                'error': r.stderr[:500] if r and not success else None,
            })
            if success:
                pair['last_sync'] = time.time()

        _save_sync_config(cfg)
        _audit_shell_op('cloud_sync_run', {
            'pairs': len(results),
            'success': sum(1 for r in results if r['success'])})
        return jsonify({'results': results})

    @app.route('/api/shell/cloud-sync/status', methods=['GET'])
    def shell_sync_status():
        """Get sync status for all pairs."""
        cfg = _load_sync_config()
        # Check if rclone is available
        r = run_probe(['rclone', 'version'], timeout=5)
        return jsonify({
            'rclone_installed': r is not None and r.returncode == 0,
            'rclone_version': r.stdout.split('\n')[0] if r and r.returncode == 0 else None,
            'pairs': cfg.get('sync_pairs', []),
            'total_pairs': len(cfg.get('sync_pairs', [])),
        })

    # ─── App Store APIs (consolidated) ──────────────────────
    # Phase-8 route consolidation: search / installed / install / uninstall (and
    # detect / history / platforms) used to be DEFINED here on /api/apps/* AND
    # AGAIN in app_installer.register_app_install_routes on /api/shell/apps/* —
    # two implementations of the SAME AppInstaller calls that drifted (this copy
    # had the call-shape bug test_shell_app_routes.py was written to catch). They
    # are now ONE surface owned by register_app_install_routes, registered on BOTH
    # the /api/shell/apps/* and /api/apps/* prefixes with the install/uninstall
    # gate. We delegate here so a caller that only registers shell_os_routes
    # (tests, edge nodes) still gets the /api/apps/* store routes. Idempotent: the
    # latch inside register_app_install_routes makes the liquid_ui double-call
    # (shell_os_routes + its own direct call) a no-op.
    try:
        from integrations.agent_engine.app_installer import (
            register_app_install_routes)
        register_app_install_routes(app)
    except Exception as e:  # pragma: no cover - non-shell node
        logger.debug(f"app install route delegation skipped: {e}")

    # ─── App Permissions APIs ─────────────────────────────────
    # Part of the ONE consolidated app surface: register each permission verb on
    # BOTH the canonical /api/shell/apps/* prefix and the legacy /api/apps/*
    # (one view, two URL rules) — same dual-prefix scheme app_installer uses for
    # the store verbs. The impl + its file I/O stay HERE (heavily mocked by
    # test_shell_os_apis.py via shell_os_apis.open / _PERMISSIONS_FILE) because
    # they never touch AppInstaller, so they are not part of the store-route
    # duplication that was consolidated above.

    def _apps_route(suffix, **kwargs):
        """Bind one permission view to both /api/shell/apps and /api/apps."""
        def deco(fn):
            for i, pfx in enumerate(('/api/shell/apps', '/api/apps')):
                ep = fn.__name__ if i == 0 else f'{fn.__name__}__legacy'
                app.add_url_rule(pfx + suffix, ep, fn, **kwargs)
            return fn
        return deco

    _PERMISSIONS_FILE = os.path.expanduser('~/.config/hart/app-permissions.json')

    def _load_permissions():
        try:
            with open(_PERMISSIONS_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_permissions(data):
        os.makedirs(os.path.dirname(_PERMISSIONS_FILE), exist_ok=True)
        with open(_PERMISSIONS_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    @_apps_route('/<app_id>/permissions', methods=['GET'])
    def shell_app_permissions(app_id):
        """Get permissions for an installed app."""
        perms = _load_permissions()
        app_perms = perms.get(app_id, {})

        # Merge with manifest-declared permissions
        manifest_perms = []
        try:
            from core.platform.registry import get_registry
            _reg = get_registry()
            registry = _reg.get('apps') if _reg.has('apps') else None
            manifest = registry.get(app_id) if registry else None
            if manifest and hasattr(manifest, 'permissions'):
                manifest_perms = manifest.permissions or []
        except (ImportError, Exception):
            pass

        result = []
        all_types = set(app_perms.keys()) | {p.get('type', p) if isinstance(p, dict)
                                              else p for p in manifest_perms}
        for ptype in sorted(all_types):
            entry = app_perms.get(ptype, {})
            result.append({
                'type': ptype,
                'granted': entry.get('granted', True),  # Default: granted
                'requested': ptype in {p.get('type', p) if isinstance(p, dict)
                                       else p for p in manifest_perms},
            })

        return jsonify({'app_id': app_id, 'permissions': result})

    @_apps_route('/<app_id>/permission/<perm_type>', methods=['POST'])
    @_require_shell_auth
    def shell_app_set_permission(app_id, perm_type):
        """Grant or revoke a permission for an app."""
        body = request.get_json(silent=True) or {}
        granted = body.get('granted', True)

        perms = _load_permissions()
        if app_id not in perms:
            perms[app_id] = {}
        perms[app_id][perm_type] = {
            'granted': granted,
            'updated': time.time(),
        }
        _save_permissions(perms)

        _audit_shell_op('app_permission', {
            'app_id': app_id, 'type': perm_type, 'granted': granted,
        })

        return jsonify({
            'updated': True, 'app_id': app_id,
            'type': perm_type, 'granted': granted,
        })

    @_apps_route('/<app_id>/permissions/reset', methods=['POST'])
    @_require_shell_auth
    def shell_app_reset_permissions(app_id):
        """Reset all permissions for an app to defaults."""
        perms = _load_permissions()
        perms.pop(app_id, None)
        _save_permissions(perms)

        _audit_shell_op('app_permission_reset', {'app_id': app_id})
        return jsonify({'reset': True, 'app_id': app_id})

    # ─── File Tagging (xattr-based) ────────────────────────────

    _TAG_XATTR = 'user.hart.tags'

    @app.route('/api/shell/files/tags', methods=['GET'])
    def shell_file_tags():
        """Get tags for a file (via xattr)."""
        path = request.args.get('path', '')
        if not path or not os.path.exists(path):
            return jsonify({'error': 'path required and must exist'}), 400
        tags = []
        try:
            import xattr
            raw = xattr.getxattr(path, _TAG_XATTR)
            tags = json.loads(raw.decode('utf-8'))
        except Exception:
            pass
        return jsonify({'path': path, 'tags': tags})

    @app.route('/api/shell/files/tags', methods=['POST'])
    @_require_shell_auth
    def shell_file_set_tags():
        """Set tags on a file (via xattr)."""
        body = request.get_json(silent=True) or {}
        path = body.get('path', '')
        tags = body.get('tags', [])
        if not path or not os.path.exists(path):
            return jsonify({'error': 'path required and must exist'}), 400
        if not isinstance(tags, list):
            return jsonify({'error': 'tags must be a list'}), 400
        try:
            import xattr
            xattr.setxattr(path, _TAG_XATTR, json.dumps(tags).encode('utf-8'))
            _audit_shell_op('file_tag', {'path': path, 'tags': tags})
            return jsonify({'tagged': True, 'path': path, 'tags': tags})
        except ImportError:
            return jsonify({'error': 'xattr package not installed'}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/files/search-by-tag', methods=['GET'])
    def shell_file_search_by_tag():
        """Search files by tag in a directory."""
        tag = request.args.get('tag', '')
        directory = request.args.get('dir', os.path.expanduser('~'))
        if not tag:
            return jsonify({'error': 'tag parameter required'}), 400
        home = os.path.expanduser('~')
        real_dir = os.path.realpath(directory)
        if not real_dir.startswith(home) and not real_dir.startswith('/tmp'):
            return jsonify({'error': 'directory must be under home'}), 403
        matches = []
        try:
            import xattr
            for root, dirs, files in os.walk(real_dir):
                # Limit depth to prevent runaway scans
                depth = root[len(real_dir):].count(os.sep)
                if depth > 3:
                    dirs.clear()
                    continue
                for fname in files[:200]:
                    fp = os.path.join(root, fname)
                    try:
                        raw = xattr.getxattr(fp, _TAG_XATTR)
                        file_tags = json.loads(raw.decode('utf-8'))
                        if tag in file_tags:
                            matches.append({'path': fp, 'tags': file_tags})
                    except Exception:
                        continue
                if len(matches) >= 100:
                    break
        except ImportError:
            return jsonify({'error': 'xattr package not installed'}), 500
        return jsonify({'tag': tag, 'matches': matches, 'count': len(matches)})

    # ─── Hotspot / Tethering ─────────────────────────────────

    @app.route('/api/shell/hotspot/status', methods=['GET'])
    def shell_hotspot_status():
        """Check if a hotspot is active."""
        active = None
        try:
            r = subprocess.run(['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE',
                                'connection', 'show', '--active'],
                               capture_output=True, text=True, timeout=5)
            if r and r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(':')
                    if len(parts) >= 2 and 'wifi' in parts[1].lower():
                        r2 = subprocess.run(
                            ['nmcli', '-t', '-f', '802-11-wireless.mode',
                             'connection', 'show', parts[0]],
                            capture_output=True, text=True, timeout=5)
                        if r2 and 'ap' in r2.stdout.lower():
                            active = {'name': parts[0],
                                      'device': parts[2] if len(parts) > 2 else ''}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return jsonify({'active': active is not None, 'hotspot': active})

    @app.route('/api/shell/hotspot/start', methods=['POST'])
    @_require_shell_auth
    def shell_hotspot_start():
        """Create a WiFi hotspot."""
        body = request.get_json(silent=True) or {}
        ssid = body.get('ssid', 'HART-Hotspot')
        password = body.get('password', '')
        band = body.get('band', 'bg')  # bg or a
        cmd = ['nmcli', 'dev', 'wifi', 'hotspot', 'ssid', ssid]
        if password:
            cmd += ['password', password]
        if band == 'a':
            cmd += ['band', 'a']
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r and r.returncode == 0:
                _audit_shell_op('hotspot_start', {'ssid': ssid})
                return jsonify({'started': True, 'ssid': ssid})
            return jsonify({'error': r.stderr.strip() if r else 'nmcli not available'}), 500
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return jsonify({'error': 'nmcli not available'}), 500

    @app.route('/api/shell/hotspot/stop', methods=['POST'])
    @_require_shell_auth
    def shell_hotspot_stop():
        """Stop the active hotspot."""
        try:
            r = subprocess.run(['nmcli', 'connection', 'down', 'Hotspot'],
                               capture_output=True, text=True, timeout=10)
            if r and r.returncode == 0:
                _audit_shell_op('hotspot_stop', {})
                return jsonify({'stopped': True})
            return jsonify({'error': r.stderr.strip() if r else 'failed'}), 500
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return jsonify({'error': 'nmcli not available'}), 500

    # ─── Weather (wttr.in — no API key needed) ────────────────

    @app.route('/api/shell/weather', methods=['GET'])
    def shell_weather():
        """Get current weather. Wraps wttr.in (open, no API key)."""
        location = request.args.get('location', '')
        try:
            import urllib.request
            url = f'https://wttr.in/{location}?format=j1'
            req = urllib.request.Request(url, headers={'User-Agent': 'HARTOS/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            current = data.get('current_condition', [{}])[0]
            return jsonify({
                'location': location or 'auto-detected',
                'temp_c': current.get('temp_C'),
                'temp_f': current.get('temp_F'),
                'feels_like_c': current.get('FeelsLikeC'),
                'humidity': current.get('humidity'),
                'description': current.get('weatherDesc', [{}])[0].get('value', ''),
                'wind_kmph': current.get('windspeedKmph'),
                'wind_dir': current.get('winddir16Point'),
                'uv_index': current.get('uvIndex'),
                'visibility_km': current.get('visibility'),
                'raw': current,
            })
        except Exception as e:
            return jsonify({'error': f'Weather unavailable: {e}'}), 503

    # ─── App Auto-Update ──────────────────────────────────────

    @app.route('/api/shell/auto-update/status', methods=['GET'])
    def shell_auto_update_status():
        """Check auto-update configuration."""
        # Check if flatpak auto-update timer exists
        flatpak_timer = False
        try:
            r = subprocess.run(['systemctl', '--user', 'is-active',
                                'flatpak-auto-update.timer'],
                               capture_output=True, text=True, timeout=5)
            if r and r.returncode == 0:
                flatpak_timer = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Check NixOS auto-upgrade
        nix_auto = False
        try:
            r2 = subprocess.run(['systemctl', 'is-active', 'nixos-upgrade.timer'],
                                capture_output=True, text=True, timeout=5)
            if r2 and r2.returncode == 0:
                nix_auto = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return jsonify({
            'flatpak_auto_update': flatpak_timer,
            'nixos_auto_upgrade': nix_auto,
        })

    @app.route('/api/shell/auto-update/run', methods=['POST'])
    @_require_shell_auth
    def shell_auto_update_run():
        """Trigger manual update check for installed apps."""
        body = request.get_json(silent=True) or {}
        target = body.get('target', 'all')  # flatpak, nix, all
        results = {}
        if target in ('flatpak', 'all'):
            r = subprocess.run(['flatpak', 'update', '-y', '--noninteractive'],
                               capture_output=True, text=True, timeout=300)
            results['flatpak'] = {
                'success': r is not None and r.returncode == 0,
                'output': (r.stdout[-500:] if r else '')
            }
        if target in ('nix', 'all'):
            r = subprocess.run(['nix-channel', '--update'],
                               capture_output=True, text=True, timeout=120)
            results['nix_channel'] = {
                'success': r is not None and r.returncode == 0,
            }
        _audit_shell_op('auto_update_run', {'target': target})
        return jsonify({'results': results})

    # ─── Secure DNS (systemd-resolved DoT/DoH) ───────────────

    @app.route('/api/shell/dns/status', methods=['GET'])
    def shell_dns_status():
        """Get current DNS configuration."""
        dns_info = {'servers': [], 'dnssec': False, 'dot': False}
        try:
            r = subprocess.run(['resolvectl', 'status'],
                               capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            r = None
        if r and r.returncode == 0:
            for line in r.stdout.split('\n'):
                line = line.strip()
                if 'DNS Servers' in line:
                    dns_info['servers'] = line.split(':', 1)[-1].strip().split()
                if 'DNSSEC' in line and 'yes' in line.lower():
                    dns_info['dnssec'] = True
                if 'DNS over TLS' in line and 'yes' in line.lower():
                    dns_info['dot'] = True
        return jsonify(dns_info)

    @app.route('/api/shell/dns/set', methods=['POST'])
    @_require_shell_auth
    def shell_dns_set():
        """Set DNS provider with optional DoT. Providers: cloudflare, google, quad9, custom."""
        body = request.get_json(silent=True) or {}
        provider = body.get('provider', 'cloudflare')
        dot = body.get('dot', True)
        providers = {
            'cloudflare': ['1.1.1.1', '1.0.0.1'],
            'google': ['8.8.8.8', '8.8.4.4'],
            'quad9': ['9.9.9.9', '149.112.112.112'],
        }
        servers = body.get('servers') if provider == 'custom' else providers.get(provider)
        if not servers:
            return jsonify({'error': f'Unknown provider: {provider}'}), 400

        # Set DNS via resolvectl
        for s in servers:
            subprocess.run(['resolvectl', 'dns', 'dns0', s],
                           capture_output=True, text=True, timeout=5)
        if dot:
            subprocess.run(['resolvectl', 'dnsovertls', 'dns0', 'yes'],
                           capture_output=True, text=True, timeout=5)
        _audit_shell_op('dns_set', {'provider': provider, 'dot': dot})
        return jsonify({'set': True, 'provider': provider, 'servers': servers, 'dot': dot})

    # ─── SSO / LDAP (sssd + PAM) ─────────────────────────────

    @app.route('/api/shell/sso/status', methods=['GET'])
    def shell_sso_status():
        """Check SSO/LDAP integration status."""
        sssd_active = False
        try:
            r = subprocess.run(['systemctl', 'is-active', 'sssd'],
                               capture_output=True, text=True, timeout=5)
            if r and r.returncode == 0 and 'active' in r.stdout:
                sssd_active = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        domain = None
        if os.path.isfile('/etc/sssd/sssd.conf'):
            try:
                with open('/etc/sssd/sssd.conf') as f:
                    for line in f:
                        if line.strip().startswith('domains'):
                            domain = line.split('=', 1)[-1].strip()
                            break
            except PermissionError:
                pass
        try:
            r_which = subprocess.run(['which', 'sssd'], capture_output=True, timeout=3)
            sssd_installed = r_which.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            sssd_installed = False
        return jsonify({
            'sssd_active': sssd_active,
            'domain': domain,
            'sssd_installed': sssd_installed,
        })

    @app.route('/api/shell/sso/join', methods=['POST'])
    @_require_shell_auth
    def shell_sso_join():
        """Join an Active Directory / LDAP domain via realm."""
        body = request.get_json(silent=True) or {}
        domain = body.get('domain', '')
        username = body.get('username', '')
        password = body.get('password', '')
        if not domain or not username:
            return jsonify({'error': 'domain and username required'}), 400
        # realm join is the standard way to join AD/LDAP domains
        cmd = ['realm', 'join', '--user', username, domain]
        try:
            r = subprocess.run(cmd, input=password + '\n', capture_output=True,
                               text=True, timeout=60)
            if r.returncode == 0:
                _audit_shell_op('sso_join', {'domain': domain})
                return jsonify({'joined': True, 'domain': domain})
            return jsonify({'error': r.stderr.strip()}), 500
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return jsonify({'error': f'realm not available: {e}'}), 500

    @app.route('/api/shell/sso/leave', methods=['POST'])
    @_require_shell_auth
    def shell_sso_leave():
        """Leave an Active Directory / LDAP domain."""
        body = request.get_json(silent=True) or {}
        domain = body.get('domain', '')
        if not domain:
            return jsonify({'error': 'domain required'}), 400
        try:
            r = subprocess.run(['realm', 'leave', domain],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                _audit_shell_op('sso_leave', {'domain': domain})
                return jsonify({'left': True, 'domain': domain})
            return jsonify({'error': r.stderr.strip()}), 500
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return jsonify({'error': f'realm not available: {e}'}), 500

    @app.route('/api/shell/sso/test', methods=['POST'])
    @_require_shell_auth
    def shell_sso_test():
        """Test LDAP connection."""
        body = request.get_json(silent=True) or {}
        uri = body.get('uri', '')
        base_dn = body.get('base_dn', '')
        if not uri:
            return jsonify({'error': 'uri required'}), 400
        try:
            r = subprocess.run(
                ['ldapsearch', '-x', '-H', uri, '-b', base_dn,
                 '-s', 'base', '(objectclass=*)'],
                capture_output=True, text=True, timeout=10)
            return jsonify({
                'reachable': r.returncode == 0,
                'output': r.stdout[:500] if r.returncode == 0 else r.stderr[:500],
            })
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return jsonify({'error': 'ldapsearch not available'}), 500

    # ─── Email (Thunderbird wrapper) ──────────────────────────

    @app.route('/api/shell/email/status', methods=['GET'])
    def shell_email_status():
        """Check if Thunderbird is installed and running."""
        installed = False
        running = False
        r = subprocess.run(['which', 'thunderbird'], capture_output=True,
                           text=True, timeout=3)
        if r and r.returncode == 0:
            installed = True
        r2 = subprocess.run(['pgrep', '-x', 'thunderbird'], capture_output=True,
                            text=True, timeout=3)
        if r2 and r2.returncode == 0:
            running = True
        return jsonify({'installed': installed, 'running': running,
                        'client': 'thunderbird'})

    @app.route('/api/shell/email/launch', methods=['POST'])
    @_require_shell_auth
    def shell_email_launch():
        """Launch Thunderbird email client."""
        body = request.get_json(silent=True) or {}
        compose_to = body.get('to', '')
        try:
            if compose_to:
                subprocess.Popen(['thunderbird', '-compose', f'to={compose_to}'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(['thunderbird'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return jsonify({'launched': True})
        except FileNotFoundError:
            return jsonify({'error': 'thunderbird not installed'}), 404

    # ═══════════════════════════════════════════════════════════
    # File Explorer P1 — recursive search, thumbnails, chmod
    # (extends the SAME sandbox/auth/audit/destructive trio as the
    #  browse/move/copy/delete file-op surface above — NO parallel path)
    # ═══════════════════════════════════════════════════════════

    @app.route('/api/shell/files/search', methods=['GET'])
    @_require_shell_auth
    def shell_files_search():
        """Recursive filename search under a directory.

        Mirrors the /browse entry shape (name/path/is_dir/size/modified/extension)
        and adds a `rel` field (path relative to the search root) so the explorer
        can show where each hit lives. GIL-safe: bounded by a depth cap and a
        result cap, pruning dirs[] in place exactly like shell_file_search_by_tag
        (#151) so a deep tree never walks unboundedly on the shared event loop.
        """
        path = os.path.expanduser(request.args.get('path', '~'))
        query = (request.args.get('q', '') or '').strip()
        recursive = request.args.get('recursive', 'true').lower() == 'true'
        show_hidden = request.args.get('hidden', 'false').lower() == 'true'

        real_path = os.path.realpath(path)
        if not _is_path_allowed(real_path):
            return jsonify({'error': 'Path outside allowed roots'}), 403
        if not os.path.isdir(real_path):
            return jsonify({'error': 'Not a directory'}), 400
        if not query:
            return jsonify({'path': real_path, 'query': query,
                            'entries': [], 'count': 0, 'truncated': False})

        q = query.lower()
        MAX_DEPTH = 8        # generous but bounded
        MAX_RESULTS = 500    # hard cap on payload size
        entries = []
        truncated = False

        def _entry(fp, is_dir):
            try:
                stat = os.stat(fp)
            except (PermissionError, OSError):
                return None
            return {
                'name': os.path.basename(fp),
                'path': fp,
                'rel': os.path.relpath(fp, real_path),
                'is_dir': is_dir,
                'size': stat.st_size if not is_dir else 0,
                'modified': stat.st_mtime,
                'extension': os.path.splitext(fp)[1].lower() if not is_dir else '',
            }

        try:
            if recursive:
                for root, dirs, files in os.walk(real_path):
                    depth = root[len(real_path):].count(os.sep)
                    if depth >= MAX_DEPTH:
                        dirs[:] = []
                    if not show_hidden:
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                    # match directory names too (folders are searchable targets)
                    for dname in dirs:
                        if q in dname.lower():
                            e = _entry(os.path.join(root, dname), True)
                            if e:
                                entries.append(e)
                                if len(entries) >= MAX_RESULTS:
                                    truncated = True
                                    break
                    if truncated:
                        break
                    for fname in files:
                        if not show_hidden and fname.startswith('.'):
                            continue
                        if q in fname.lower():
                            e = _entry(os.path.join(root, fname), False)
                            if e:
                                entries.append(e)
                                if len(entries) >= MAX_RESULTS:
                                    truncated = True
                                    break
                    if truncated:
                        break
            else:
                for entry in os.scandir(real_path):
                    if not show_hidden and entry.name.startswith('.'):
                        continue
                    if q in entry.name.lower():
                        e = _entry(entry.path, entry.is_dir())
                        if e:
                            entries.append(e)
                            if len(entries) >= MAX_RESULTS:
                                truncated = True
                                break
        except PermissionError:
            return jsonify({'error': 'Permission denied'}), 403

        entries.sort(key=lambda e: (not e['is_dir'], e['name'].lower()))
        return jsonify({
            'path': real_path,
            'query': query,
            'recursive': recursive,
            'entries': entries,
            'count': len(entries),
            'truncated': truncated,
        })

    _THUMB_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff',
                   '.ico'}

    def _thumb_cache_dir():
        try:
            from core.platform_paths import get_db_dir
            base = get_db_dir()
        except Exception:
            base = os.path.join(tempfile.gettempdir(), 'hart')
        d = os.path.join(base, 'thumb_cache')
        os.makedirs(d, exist_ok=True)
        return d

    @app.route('/api/shell/files/thumbnail', methods=['GET'])
    @_require_shell_auth
    def shell_files_thumbnail():
        """Return a small thumbnail (PNG) for an image file.

        Pillow if importable, else a graceful 204 (the explorer falls back to the
        material glyph). Output dimension is capped; thumbnails are cached on disk
        under get_db_dir()/thumb_cache keyed by (realpath, mtime, size) so repeat
        views are cheap. Non-image / oversize / unreadable -> 204 (never 500),
        so a bad file never breaks the grid render.
        """
        path = request.args.get('path', '')
        try:
            size = int(request.args.get('size', 96))
        except (TypeError, ValueError):
            size = 96
        size = max(16, min(size, 512))  # clamp

        if not path or not os.path.isfile(path):
            return Response(status=204)
        if not _is_path_allowed(path):
            return jsonify({'error': 'Path outside allowed roots'}), 403
        if os.path.splitext(path)[1].lower() not in _THUMB_EXTS:
            return Response(status=204)

        try:
            from PIL import Image
        except Exception:
            return Response(status=204)  # Pillow absent -> graceful glyph fallback

        try:
            st = os.stat(path)
            real = os.path.realpath(path)
            import hashlib
            key = hashlib.sha1(
                f'{real}|{int(st.st_mtime)}|{st.st_size}|{size}'.encode('utf-8')
            ).hexdigest()
            cache_path = os.path.join(_thumb_cache_dir(), key + '.png')
            if os.path.isfile(cache_path):
                with open(cache_path, 'rb') as fh:
                    return Response(fh.read(), mimetype='image/png')

            with Image.open(path) as im:
                im.draft('RGB', (size, size))  # fast pre-scale on JPEG
                im = im.convert('RGBA')
                im.thumbnail((size, size))
                im.save(cache_path, format='PNG', optimize=True)
            with open(cache_path, 'rb') as fh:
                return Response(fh.read(), mimetype='image/png')
        except Exception:
            return Response(status=204)  # unreadable/corrupt -> fallback, never 500

    @app.route('/api/shell/files/chmod', methods=['POST'])
    @_require_shell_auth
    def shell_files_chmod():
        """Change a file's POSIX mode (owner/group/other rwx).

        chmod is a routine file op (like move/copy), NOT destructive like delete,
        so it gates on sandbox + auth + immutable audit only (no action-classifier
        gate, which 'unknown'-fail-closed-403'd every real change). On Windows
        os.chmod only honours the read-only bit, so this is a safe no-op there —
        we still return the requested mode for UI consistency. `mode` accepts an
        octal string ('755', '0644') or an int.
        """
        data = request.get_json(force=True)
        path = data.get('path', '')
        raw_mode = data.get('mode', '')

        if not path or not os.path.exists(path):
            return jsonify({'error': 'path not found'}), 404
        if not _is_path_allowed(path):
            return jsonify({'error': 'Path outside allowed roots'}), 403

        # Parse mode: octal string ('0755'/'755') or int -> 0..0o777
        try:
            if isinstance(raw_mode, int):
                mode_int = raw_mode
            else:
                mode_int = int(str(raw_mode).strip(), 8)
        except (TypeError, ValueError):
            return jsonify({'error': 'mode must be an octal string (e.g. "755")'}), 400
        if not (0 <= mode_int <= 0o777):
            return jsonify({'error': 'mode out of range (000-777)'}), 400

        # chmod is a routine file op (like move/copy), NOT destructive like
        # delete, so it is NOT routed through _classify_destructive:
        # classify_action('chmod 750: ...') returns 'unknown' -> fail-closed 403,
        # which broke every real permission change. Sandbox (_is_path_allowed) +
        # auth (_require_shell_auth) + immutable audit are the gate, matching the
        # working shell_files_move / shell_files_copy pattern.
        _audit_shell_op('file_chmod', {'path': path, 'mode': oct(mode_int)[-3:]})

        try:
            os.chmod(path, mode_int)
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

        # Re-read so the UI reflects the actual mode (Windows may clamp it).
        try:
            applied = oct(os.stat(path).st_mode)[-3:]
        except OSError:
            applied = oct(mode_int)[-3:]
        return jsonify({'path': path, 'mode': applied,
                        'requested': oct(mode_int)[-3:]})

    logger.info("Registered shell OS API routes (extended)")
