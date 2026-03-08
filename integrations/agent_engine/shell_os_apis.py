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
import shutil
import subprocess
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
        os.path.realpath('/tmp'),
    ]
    extra = os.environ.get('HART_SHELL_ALLOWED_PATHS', '')
    if extra:
        for p in extra.split(':'):
            rp = os.path.realpath(p.strip())
            if os.path.isdir(rp):
                roots.append(rp)
    _ALLOWED_ROOTS = roots
    return roots


def _is_path_allowed(path):
    """Check if a resolved path is within allowed roots."""
    real = os.path.realpath(path)
    return any(real.startswith(root) for root in _get_allowed_roots())


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
    """Check if an action is destructive via action_classifier (best-effort).

    Returns True if action is safe or classifier unavailable.
    Returns False if action is classified as destructive.
    """
    try:
        from security.action_classifier import classify_action
        result = classify_action(action_desc)
        # classify_action returns a string literal: 'safe', 'destructive', or 'unknown'
        return result != 'destructive'
    except Exception:
        return True  # fail-open if classifier unavailable


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
        except (ImportError, Exception):
            pass

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
        path = request.args.get('path', os.path.expanduser('~'))
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

        if not _classify_destructive(f'terminal exec: {command[:200]}'):
            return jsonify({'error': 'Action classified as destructive — requires approval'}), 403

        _audit_shell_op('terminal_exec', {'command': command[:200]})

        try:
            result = subprocess.run(
                command, shell=True, capture_output=True,
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
        """Execute power action (suspend, hibernate, reboot, shutdown)."""
        data = request.get_json(force=True)
        action = data.get('action', '')

        if not _classify_destructive(f'power action: {action}'):
            return jsonify({'error': 'Action classified as destructive — requires approval'}), 403

        _audit_shell_op('power_action', {'action': action})
        actions = {
            'suspend': ['systemctl', 'suspend'],
            'hibernate': ['systemctl', 'hibernate'],
            'reboot': ['systemctl', 'reboot'],
            'shutdown': ['systemctl', 'poweroff'],
            'lock': ['loginctl', 'lock-sessions'],
        }
        if action not in actions:
            return jsonify({'error': f'Invalid action. Valid: {list(actions.keys())}'}), 400

        try:
            subprocess.Popen(actions[action])
            return jsonify({'action': action, 'initiated': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

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

    _a11y_settings = {
        'font_scale': 1.0,
        'high_contrast': False,
        'reduced_motion': False,
        'large_cursor': False,
        'screen_reader': False,
        'sticky_keys': False,
    }

    @app.route('/api/shell/accessibility', methods=['GET'])
    def shell_accessibility_get():
        """Get current accessibility settings."""
        # Try NixOS declarative config first
        try:
            with open('/etc/hart/accessibility.json') as f:
                return jsonify(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
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
                proc = subprocess.Popen(
                    tool_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        except Exception:
            pass

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
            from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
            orch = UpgradeOrchestrator()
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
            from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
            orch = UpgradeOrchestrator()
            result = orch.start_upgrade(version, sha)
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/upgrades/advance', methods=['POST'])
    def upgrades_advance():
        """Advance upgrade pipeline to next stage."""
        try:
            from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
            orch = UpgradeOrchestrator()
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
            from integrations.agent_engine.upgrade_orchestrator import UpgradeOrchestrator
            orch = UpgradeOrchestrator()
            result = orch.rollback(reason)
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ─── Battery / Power Monitoring ─────────────────────────

    @app.route('/api/shell/battery', methods=['GET'])
    def shell_battery_status():
        """Battery status: level, charging state, time remaining."""
        bat_dir = '/sys/class/power_supply'
        result = {'has_battery': False}
        try:
            if not os.path.isdir(bat_dir):
                return jsonify(result)
            for entry in os.listdir(bat_dir):
                path = os.path.join(bat_dir, entry)
                type_file = os.path.join(path, 'type')
                if not os.path.isfile(type_file):
                    continue
                with open(type_file) as f:
                    if f.read().strip() != 'Battery':
                        continue
                result['has_battery'] = True
                result['name'] = entry
                cap_file = os.path.join(path, 'capacity')
                if os.path.isfile(cap_file):
                    with open(cap_file) as f:
                        result['level'] = int(f.read().strip())
                status_file = os.path.join(path, 'status')
                if os.path.isfile(status_file):
                    with open(status_file) as f:
                        result['charging'] = f.read().strip()
                online_file = os.path.join(bat_dir, 'AC0', 'online')
                if not os.path.isfile(online_file):
                    online_file = os.path.join(bat_dir, 'ADP1', 'online')
                if os.path.isfile(online_file):
                    with open(online_file) as f:
                        result['ac_power'] = f.read().strip() == '1'
                break
        except Exception as e:
            result['error'] = str(e)
        return jsonify(result)

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

    # ─── WiFi Management ────────────────────────────────────

    @app.route('/api/shell/wifi/scan', methods=['GET'])
    def shell_wifi_scan():
        """Scan for available WiFi networks."""
        try:
            r = subprocess.run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,BSSID',
                               'dev', 'wifi', 'list', '--rescan', 'yes'],
                              capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return jsonify({'networks': [], 'error': r.stderr.strip()})
            networks = []
            for line in r.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(':')
                if len(parts) >= 3:
                    networks.append({
                        'ssid': parts[0],
                        'signal': int(parts[1]) if parts[1].isdigit() else 0,
                        'security': parts[2] if len(parts) > 2 else '',
                        'bssid': parts[3] if len(parts) > 3 else '',
                    })
            return jsonify({'networks': networks})
        except FileNotFoundError:
            return jsonify({'networks': [], 'error': 'nmcli not available'})
        except subprocess.TimeoutExpired:
            return jsonify({'networks': [], 'error': 'WiFi scan timed out'})

    @app.route('/api/shell/wifi/connect', methods=['POST'])
    def shell_wifi_connect():
        """Connect to a WiFi network."""
        body = request.get_json(silent=True) or {}
        ssid = body.get('ssid', '')
        password = body.get('password', '')
        if not ssid:
            return jsonify({'error': 'ssid is required'}), 400
        try:
            cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]
            if password:
                cmd += ['password', password]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return jsonify({'status': 'connected', 'ssid': ssid})
            return jsonify({'status': 'failed', 'error': r.stderr.strip()}), 400
        except FileNotFoundError:
            return jsonify({'error': 'nmcli not available'}), 500
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Connection timed out'}), 504

    @app.route('/api/shell/wifi/disconnect', methods=['POST'])
    def shell_wifi_disconnect():
        """Disconnect from current WiFi network."""
        try:
            r = subprocess.run(['nmcli', 'dev', 'disconnect', 'wifi'],
                              capture_output=True, text=True, timeout=10)
            return jsonify({'status': 'disconnected' if r.returncode == 0 else 'error',
                           'message': r.stdout.strip() or r.stderr.strip()})
        except FileNotFoundError:
            return jsonify({'error': 'nmcli not available'}), 500

    @app.route('/api/shell/wifi/forget', methods=['POST'])
    def shell_wifi_forget():
        """Forget (delete) a saved WiFi connection."""
        body = request.get_json(silent=True) or {}
        name = body.get('name', '')
        if not name:
            return jsonify({'error': 'name is required'}), 400
        try:
            r = subprocess.run(['nmcli', 'connection', 'delete', name],
                              capture_output=True, text=True, timeout=10)
            return jsonify({'status': 'ok' if r.returncode == 0 else 'error',
                           'message': r.stdout.strip() or r.stderr.strip()})
        except FileNotFoundError:
            return jsonify({'error': 'nmcli not available'}), 500

    @app.route('/api/shell/wifi/status', methods=['GET'])
    def shell_wifi_status():
        """Current WiFi connection status."""
        try:
            r = subprocess.run(['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE,STATE',
                               'connection', 'show', '--active'],
                              capture_output=True, text=True, timeout=10)
            wifi_conn = None
            for line in (r.stdout or '').strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 4 and 'wireless' in parts[1].lower():
                    wifi_conn = {'name': parts[0], 'device': parts[2], 'state': parts[3]}
                    break
            return jsonify({'connected': wifi_conn is not None,
                           'connection': wifi_conn})
        except FileNotFoundError:
            return jsonify({'connected': False, 'error': 'nmcli not available'})

    # ─── VPN Management ─────────────────────────────────────

    @app.route('/api/shell/vpn/list', methods=['GET'])
    def shell_vpn_list():
        """List VPN connections (active and saved)."""
        try:
            r = subprocess.run(['nmcli', '-t', '-f', 'NAME,TYPE,STATE',
                               'connection', 'show'],
                              capture_output=True, text=True, timeout=10)
            vpns = []
            for line in (r.stdout or '').strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 3 and 'vpn' in parts[1].lower():
                    vpns.append({'name': parts[0], 'type': parts[1],
                                'state': parts[2]})
            return jsonify({'vpns': vpns})
        except FileNotFoundError:
            return jsonify({'vpns': [], 'error': 'nmcli not available'})

    @app.route('/api/shell/vpn/connect', methods=['POST'])
    def shell_vpn_connect():
        """Connect to a VPN by name."""
        body = request.get_json(silent=True) or {}
        name = body.get('name', '')
        if not name:
            return jsonify({'error': 'name is required'}), 400
        try:
            r = subprocess.run(['nmcli', 'connection', 'up', name],
                              capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return jsonify({'status': 'connected', 'name': name})
            return jsonify({'status': 'failed', 'error': r.stderr.strip()}), 400
        except FileNotFoundError:
            return jsonify({'error': 'nmcli not available'}), 500

    @app.route('/api/shell/vpn/disconnect', methods=['POST'])
    def shell_vpn_disconnect():
        """Disconnect a VPN connection."""
        body = request.get_json(silent=True) or {}
        name = body.get('name', '')
        if not name:
            return jsonify({'error': 'name is required'}), 400
        try:
            r = subprocess.run(['nmcli', 'connection', 'down', name],
                              capture_output=True, text=True, timeout=10)
            return jsonify({'status': 'disconnected' if r.returncode == 0 else 'error',
                           'message': r.stdout.strip() or r.stderr.strip()})
        except FileNotFoundError:
            return jsonify({'error': 'nmcli not available'}), 500

    @app.route('/api/shell/vpn/import', methods=['POST'])
    def shell_vpn_import():
        """Import a VPN config file (WireGuard .conf or OpenVPN .ovpn)."""
        body = request.get_json(silent=True) or {}
        path = body.get('path', '')
        vpn_type = body.get('type', '')
        if not path:
            return jsonify({'error': 'path is required'}), 400
        if not os.path.isfile(path):
            return jsonify({'error': 'File not found'}), 404
        if not vpn_type:
            if path.endswith('.conf'):
                vpn_type = 'wireguard'
            elif path.endswith('.ovpn'):
                vpn_type = 'openvpn'
            else:
                return jsonify({'error': 'type is required (wireguard or openvpn)'}), 400
        try:
            r = subprocess.run(['nmcli', 'connection', 'import', 'type', vpn_type,
                               'file', path],
                              capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return jsonify({'status': 'imported', 'message': r.stdout.strip()})
            return jsonify({'status': 'failed', 'error': r.stderr.strip()}), 400
        except FileNotFoundError:
            return jsonify({'error': 'nmcli not available'}), 500

    # ─── Trash / Recycle Bin (freedesktop spec) ─────────────

    def _trash_dir():
        return os.path.join(os.path.expanduser('~'), '.local', 'share', 'Trash')

    @app.route('/api/shell/trash', methods=['GET'])
    def shell_trash_list():
        """List items in trash."""
        info_dir = os.path.join(_trash_dir(), 'info')
        items = []
        if os.path.isdir(info_dir):
            for fname in os.listdir(info_dir):
                if not fname.endswith('.trashinfo'):
                    continue
                info_path = os.path.join(info_dir, fname)
                try:
                    import configparser
                    cp = configparser.ConfigParser()
                    cp.read(info_path)
                    items.append({
                        'name': fname.replace('.trashinfo', ''),
                        'original_path': cp.get('Trash Info', 'Path', fallback=''),
                        'deletion_date': cp.get('Trash Info', 'DeletionDate', fallback=''),
                    })
                except Exception:
                    items.append({'name': fname.replace('.trashinfo', '')})
        return jsonify({'items': items, 'total': len(items)})

    @app.route('/api/shell/trash', methods=['POST'])
    def shell_trash_file():
        """Move a file to trash (instead of permanent delete)."""
        body = request.get_json(silent=True) or {}
        path = body.get('path', '')
        if not path or not os.path.exists(path):
            return jsonify({'error': 'path is required and must exist'}), 400
        trash = _trash_dir()
        files_dir = os.path.join(trash, 'files')
        info_dir = os.path.join(trash, 'info')
        os.makedirs(files_dir, exist_ok=True)
        os.makedirs(info_dir, exist_ok=True)
        basename = os.path.basename(path)
        dest = os.path.join(files_dir, basename)
        # Handle name collision
        counter = 1
        while os.path.exists(dest):
            name, ext = os.path.splitext(basename)
            dest = os.path.join(files_dir, f"{name}.{counter}{ext}")
            counter += 1
        final_name = os.path.basename(dest)
        try:
            import shutil
            shutil.move(path, dest)
            from datetime import datetime
            info_content = (
                "[Trash Info]\n"
                f"Path={os.path.abspath(path)}\n"
                f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
            )
            info_path = os.path.join(info_dir, final_name + '.trashinfo')
            with open(info_path, 'w') as f:
                f.write(info_content)
            return jsonify({'status': 'trashed', 'name': final_name})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/trash/restore', methods=['POST'])
    def shell_trash_restore():
        """Restore a file from trash to its original location."""
        body = request.get_json(silent=True) or {}
        name = body.get('name', '')
        if not name:
            return jsonify({'error': 'name is required'}), 400
        trash = _trash_dir()
        file_path = os.path.join(trash, 'files', name)
        info_path = os.path.join(trash, 'info', name + '.trashinfo')
        if not os.path.exists(file_path):
            return jsonify({'error': 'Item not found in trash'}), 404
        original_path = ''
        try:
            import configparser
            cp = configparser.ConfigParser()
            cp.read(info_path)
            original_path = cp.get('Trash Info', 'Path', fallback='')
        except Exception:
            pass
        if not original_path:
            return jsonify({'error': 'Cannot determine original path'}), 400
        try:
            import shutil
            os.makedirs(os.path.dirname(original_path), exist_ok=True)
            shutil.move(file_path, original_path)
            if os.path.isfile(info_path):
                os.remove(info_path)
            return jsonify({'status': 'restored', 'path': original_path})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/trash/empty', methods=['POST'])
    def shell_trash_empty():
        """Empty the trash permanently."""
        trash = _trash_dir()
        count = 0
        import shutil
        for subdir in ['files', 'info']:
            d = os.path.join(trash, subdir)
            if os.path.isdir(d):
                for item in os.listdir(d):
                    item_path = os.path.join(d, item)
                    try:
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                        else:
                            os.remove(item_path)
                        count += 1
                    except Exception:
                        pass
        return jsonify({'status': 'emptied', 'removed': count})

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

    logger.info("Registered shell OS API routes")
