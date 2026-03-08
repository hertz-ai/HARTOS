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
import signal
import subprocess
import threading
import time

logger = logging.getLogger('hevolve.shell.system')

# ─── Helpers ────────────────────────────────────────────────────

def _run(cmd, timeout=10, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, **kw)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


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

def register_shell_system_routes(app):
    """Register all system management API routes."""
    from flask import jsonify, request

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
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent',
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
        try:
            import psutil
            proc = psutil.Process(pid)
            if proc.name() in _PROTECTED_NAMES:
                return jsonify({'error': f'Cannot kill protected process: {proc.name()}'}), 403
        except Exception:
            pass
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
                res['gpu'] = {
                    'name': gpu.get('name', ''),
                    'memory_gb': round(gpu.get('vram_mb', 0) / 1024, 1),
                    'utilization': gpu.get('utilization', 0),
                    'temperature': gpu.get('temperature', 0),
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
        partitions = []
        for part in psutil.disk_partitions(all=False):
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
                        size_mb = int(r.stdout.split()[0]) if r and r.returncode == 0 else 0
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
                size = int(r.stdout.split()[0]) if r and r.returncode == 0 else 0
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
                size = int(r.stdout.split()[0]) if r and r.returncode == 0 else 0
                _run(['find', cache_dir, '-type', 'f', '-atime', '+7', '-delete'], timeout=30)
                freed['cache'] = size
            elif cat == 'temp':
                r = _run(['du', '-sm', '/tmp'], timeout=5)
                size = int(r.stdout.split()[0]) if r and r.returncode == 0 else 0
                _run(['find', '/tmp', '-user', os.environ.get('USER', 'hart'),
                      '-type', 'f', '-mtime', '+1', '-delete'], timeout=30)
                freed['temp'] = size
            elif cat == 'trash':
                trash = os.path.join(home, '.local/share/Trash')
                r = _run(['du', '-sm', trash], timeout=5)
                size = int(r.stdout.split()[0]) if r and r.returncode == 0 else 0
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
        return jsonify({'status': 'error',
                       'error': r.stderr if r else 'ffmpeg not available'}), 500

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
                       'error': r.stderr if r else 'scanimage not available'}), 500

    logger.info("Registered shell system routes (6 features)")
