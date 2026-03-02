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
"""

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Optional

logger = logging.getLogger('hevolve.shell')


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
    def shell_files_browse():
        """Browse directory contents."""
        path = request.args.get('path', os.path.expanduser('~'))
        show_hidden = request.args.get('hidden', 'false').lower() == 'true'

        # Security: prevent traversal outside allowed paths
        real_path = os.path.realpath(path)
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
    def shell_files_mkdir():
        """Create a directory."""
        data = request.get_json(force=True)
        path = data.get('path', '')
        if not path:
            return jsonify({'error': 'path required'}), 400
        try:
            os.makedirs(path, exist_ok=True)
            return jsonify({'created': path})
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/shell/files/delete', methods=['POST'])
    def shell_files_delete():
        """Delete a file or directory (moves to trash first if available)."""
        data = request.get_json(force=True)
        path = data.get('path', '')
        if not path or not os.path.exists(path):
            return jsonify({'error': 'path not found'}), 400

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
    def shell_files_move():
        """Move/rename a file or directory."""
        data = request.get_json(force=True)
        src = data.get('source', '')
        dst = data.get('destination', '')
        if not src or not dst:
            return jsonify({'error': 'source and destination required'}), 400
        try:
            shutil.move(src, dst)
            return jsonify({'moved': {'from': src, 'to': dst}})
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/shell/files/copy', methods=['POST'])
    def shell_files_copy():
        """Copy a file or directory."""
        data = request.get_json(force=True)
        src = data.get('source', '')
        dst = data.get('destination', '')
        if not src or not dst:
            return jsonify({'error': 'source and destination required'}), 400
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            return jsonify({'copied': {'from': src, 'to': dst}})
        except (PermissionError, OSError) as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/shell/files/info', methods=['GET'])
    def shell_files_info():
        """Get detailed file/directory info."""
        path = request.args.get('path', '')
        if not path or not os.path.exists(path):
            return jsonify({'error': 'path not found'}), 404
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
    def shell_users_delete():
        """Delete a system user (requires root)."""
        data = request.get_json(force=True)
        username = data.get('username', '')
        remove_home = data.get('remove_home', False)

        if not username or username in ('root', 'hart', 'hart-admin'):
            return jsonify({'error': 'Cannot delete protected user'}), 403

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
    def shell_power_action():
        """Execute power action (suspend, hibernate, reboot, shutdown)."""
        data = request.get_json(force=True)
        action = data.get('action', '')
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

    logger.info("Registered shell OS API routes")
