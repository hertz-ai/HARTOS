"""
Shell Desktop APIs — Desktop experience management for HART OS.

Covers: default apps, font manager, sound themes, clipboard history,
date/time/timezone, wallpaper, input methods, night light, workspaces.

All routes registered via register_shell_desktop_routes(app).
"""

import collections
import json
import logging
import os
import shutil
import subprocess
import threading
import time

logger = logging.getLogger('hevolve.shell.desktop')

# ─── Helpers ────────────────────────────────────────────────────

_HART_CONFIG = os.path.expanduser(os.environ.get(
    'HART_CONFIG_DIR', '~/.config/hart'))


def _config_path(name):
    os.makedirs(_HART_CONFIG, exist_ok=True)
    return os.path.join(_HART_CONFIG, name)


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


def _is_wayland():
    """Detect Wayland compositor — env var check + GNOME session fallback."""
    if os.environ.get('WAYLAND_DISPLAY'):
        return True
    # Fallback: some GNOME sessions don't set WAYLAND_DISPLAY
    if os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland':
        return True
    try:
        r = subprocess.run(['pgrep', '-x', 'sway|labwc|hyprland'],
                          capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def _run(cmd, timeout=10, **kw):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, **kw)
        return r
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ─── Clipboard state (in-memory) ───────────────────────────────

_clipboard_history = collections.deque(maxlen=100)
_clipboard_lock = threading.Lock()
_clipboard_counter = 0


# ═══════════════════════════════════════════════════════════════
# Route registration
# ═══════════════════════════════════════════════════════════════

def register_shell_desktop_routes(app):
    """Register all desktop experience API routes."""
    from flask import jsonify, request

    # ─── 1. Default Apps / File Associations ────────────────

    _COMMON_MIMES = [
        'text/html', 'text/plain', 'application/pdf',
        'image/png', 'image/jpeg', 'image/gif', 'image/svg+xml',
        'video/mp4', 'video/x-matroska', 'audio/mpeg', 'audio/flac',
        'application/zip', 'application/json', 'application/xml',
        'inode/directory',
    ]

    _CATEGORIES = {
        'browser': ('xdg-settings', 'get', 'default-web-browser'),
        'email': ('xdg-settings', 'get', 'default-url-scheme-handler', 'mailto'),
    }

    @app.route('/api/shell/default-apps', methods=['GET'])
    def shell_default_apps():
        defaults = {}
        for mime in _COMMON_MIMES:
            r = _run(['xdg-mime', 'query', 'default', mime])
            if r and r.returncode == 0 and r.stdout.strip():
                desktop = r.stdout.strip()
                defaults[mime] = {
                    'app': desktop,
                    'name': desktop.replace('.desktop', '').replace('org.', '').replace('.', ' '),
                }
        categories = {}
        for cat, cmd in _CATEGORIES.items():
            r = _run(list(cmd))
            if r and r.returncode == 0 and r.stdout.strip():
                categories[cat] = r.stdout.strip()
        return jsonify({'defaults': defaults, 'categories': categories})

    @app.route('/api/shell/default-apps/candidates', methods=['GET'])
    def shell_default_apps_candidates():
        mime = request.args.get('mime_type', '')
        if not mime:
            return jsonify({'error': 'mime_type required'}), 400
        candidates = []
        apps_dir = '/usr/share/applications'
        if os.path.isdir(apps_dir):
            for f in os.listdir(apps_dir):
                if not f.endswith('.desktop'):
                    continue
                path = os.path.join(apps_dir, f)
                try:
                    with open(path) as fh:
                        content = fh.read()
                    if mime in content:
                        name = f.replace('.desktop', '')
                        for line in content.splitlines():
                            if line.startswith('Name='):
                                name = line.split('=', 1)[1]
                                break
                        candidates.append({'app': f, 'name': name})
                except (IOError, UnicodeDecodeError):
                    pass
        return jsonify({'mime_type': mime, 'candidates': candidates})

    @app.route('/api/shell/default-apps/set', methods=['POST'])
    def shell_default_apps_set():
        data = request.get_json(force=True)
        mime = data.get('mime_type', '')
        desktop_app = data.get('app', '')
        if not mime or not desktop_app:
            return jsonify({'error': 'mime_type and app required'}), 400
        r = _run(['xdg-mime', 'default', desktop_app, mime])
        if r and r.returncode == 0:
            return jsonify({'set': True, 'mime_type': mime, 'app': desktop_app})
        return jsonify({'set': False, 'error': r.stderr.strip() if r else 'xdg-mime not available'}), 500

    @app.route('/api/shell/default-apps/set-category', methods=['POST'])
    def shell_default_apps_set_category():
        data = request.get_json(force=True)
        cat = data.get('category', '')
        desktop_app = data.get('app', '')
        if not cat or not desktop_app:
            return jsonify({'error': 'category and app required'}), 400
        if cat == 'browser':
            r = _run(['xdg-settings', 'set', 'default-web-browser', desktop_app])
        elif cat == 'email':
            r = _run(['xdg-settings', 'set', 'default-url-scheme-handler', 'mailto', desktop_app])
        else:
            return jsonify({'error': f'Unknown category: {cat}'}), 400
        ok = r and r.returncode == 0
        return jsonify({'set': ok, 'category': cat, 'app': desktop_app})

    # ─── 2. Font Manager ───────────────────────────────────

    @app.route('/api/shell/fonts', methods=['GET'])
    def shell_fonts_list():
        search = request.args.get('search', '').lower()
        category = request.args.get('category', '')
        r = _run(['fc-list', '--format=%{family}|%{style}|%{file}|%{fontformat}\n'])
        fonts = []
        seen = set()
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = line.split('|')
                if len(parts) < 3:
                    continue
                family = parts[0].split(',')[0].strip()
                if family in seen:
                    continue
                seen.add(family)
                style = parts[1].strip() if len(parts) > 1 else ''
                path = parts[2].strip() if len(parts) > 2 else ''
                fmt = parts[3].strip() if len(parts) > 3 else ''
                cat = 'monospace' if any(k in family.lower() for k in ['mono', 'code', 'consol']) \
                    else 'serif' if any(k in family.lower() for k in ['serif', 'times', 'georgia']) \
                    else 'sans-serif'
                if search and search not in family.lower():
                    continue
                if category and category != cat:
                    continue
                fonts.append({
                    'family': family, 'style': style,
                    'path': path, 'format': fmt, 'category': cat,
                })
        fonts.sort(key=lambda f: f['family'])
        cats = {}
        for f in fonts:
            cats[f['category']] = cats.get(f['category'], 0) + 1
        return jsonify({'fonts': fonts, 'count': len(fonts), 'categories': cats})

    @app.route('/api/shell/fonts/preview', methods=['GET'])
    def shell_fonts_preview():
        family = request.args.get('family', '')
        text = request.args.get('text', 'The quick brown fox jumps over the lazy dog')
        size = request.args.get('size', '18')
        if not family:
            return jsonify({'error': 'family required'}), 400
        return jsonify({
            'family': family, 'text': text, 'size': int(size),
            'css': f'font-family: "{family}"; font-size: {size}px;',
        })

    @app.route('/api/shell/fonts/install', methods=['POST'])
    def shell_fonts_install():
        data = request.get_json(force=True)
        path = data.get('path', '')
        if not path or not os.path.isfile(path):
            return jsonify({'error': 'Valid font file path required'}), 400
        ext = os.path.splitext(path)[1].lower()
        if ext not in ('.ttf', '.otf', '.woff', '.woff2', '.ttc'):
            return jsonify({'error': f'Unsupported font format: {ext}'}), 400
        fonts_dir = os.path.expanduser('~/.local/share/fonts')
        os.makedirs(fonts_dir, exist_ok=True)
        dest = os.path.join(fonts_dir, os.path.basename(path))
        try:
            shutil.copy2(path, dest)
            _run(['fc-cache', '-f'], timeout=30)
            return jsonify({'installed': True, 'path': dest,
                            'family': os.path.splitext(os.path.basename(path))[0]})
        except (IOError, PermissionError) as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/shell/fonts/remove', methods=['POST'])
    def shell_fonts_remove():
        data = request.get_json(force=True)
        family = data.get('family', '')
        if not family:
            return jsonify({'error': 'family required'}), 400
        fonts_dir = os.path.expanduser('~/.local/share/fonts')
        removed = []
        if os.path.isdir(fonts_dir):
            for f in os.listdir(fonts_dir):
                if family.lower().replace(' ', '') in f.lower().replace(' ', ''):
                    fp = os.path.join(fonts_dir, f)
                    os.remove(fp)
                    removed.append(f)
        if removed:
            _run(['fc-cache', '-f'], timeout=30)
            return jsonify({'removed': True, 'family': family, 'files': removed})
        return jsonify({'removed': False, 'error': 'Font not found in user fonts (system fonts cannot be removed)'}), 404

    # ─── 3. Sound Manager & System Sounds ──────────────────

    _SOUND_EVENTS = [
        {'id': 'bell', 'name': 'Notification'},
        {'id': 'dialog-error', 'name': 'Error'},
        {'id': 'dialog-warning', 'name': 'Warning'},
        {'id': 'dialog-information', 'name': 'Information'},
        {'id': 'desktop-login', 'name': 'Login'},
        {'id': 'desktop-logout', 'name': 'Logout'},
        {'id': 'device-added', 'name': 'Device Connected'},
        {'id': 'device-removed', 'name': 'Device Disconnected'},
        {'id': 'screen-capture', 'name': 'Screenshot'},
        {'id': 'trash-empty', 'name': 'Empty Trash'},
        {'id': 'message-new-instant', 'name': 'New Message'},
        {'id': 'battery-low', 'name': 'Low Battery'},
    ]

    @app.route('/api/shell/sounds/themes', methods=['GET'])
    def shell_sounds_themes():
        themes = []
        for d in ['/usr/share/sounds', '/run/current-system/sw/share/sounds']:
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                theme_dir = os.path.join(d, name)
                if os.path.isdir(theme_dir) and name not in [t['id'] for t in themes]:
                    themes.append({'id': name, 'name': name.replace('-', ' ').title(),
                                   'path': theme_dir})
        if not themes:
            themes.append({'id': 'freedesktop', 'name': 'FreeDesktop (default)', 'path': ''})
        cfg = _load_json(_config_path('sound-theme.json'), {'active': 'freedesktop', 'enabled': True})
        return jsonify({'themes': themes, 'active': cfg.get('active', 'freedesktop'),
                        'enabled': cfg.get('enabled', True)})

    @app.route('/api/shell/sounds/events', methods=['GET'])
    def shell_sounds_events():
        cfg = _load_json(_config_path('sound-theme.json'), {'active': 'freedesktop', 'enabled': True})
        overrides = _load_json(_config_path('sound-overrides.json'), {})
        events = []
        for evt in _SOUND_EVENTS:
            entry = dict(evt)
            if evt['id'] in overrides:
                entry['file'] = overrides[evt['id']]
                entry['custom'] = True
            else:
                for ext in ('oga', 'ogg', 'wav'):
                    for base in ['/usr/share/sounds', '/run/current-system/sw/share/sounds']:
                        p = os.path.join(base, cfg.get('active', 'freedesktop'), 'stereo', f"{evt['id']}.{ext}")
                        if os.path.isfile(p):
                            entry['file'] = p
                            break
            events.append(entry)
        return jsonify({'events': events, 'enabled': cfg.get('enabled', True)})

    @app.route('/api/shell/sounds/set-theme', methods=['POST'])
    def shell_sounds_set_theme():
        data = request.get_json(force=True)
        theme = data.get('theme', '')
        if not theme:
            return jsonify({'error': 'theme required'}), 400
        cfg = _load_json(_config_path('sound-theme.json'), {})
        cfg['active'] = theme
        _save_json(_config_path('sound-theme.json'), cfg)
        return jsonify({'set': True, 'theme': theme})

    @app.route('/api/shell/sounds/set-event', methods=['POST'])
    def shell_sounds_set_event():
        data = request.get_json(force=True)
        event = data.get('event', '')
        file = data.get('file', '')
        if not event or not file:
            return jsonify({'error': 'event and file required'}), 400
        overrides = _load_json(_config_path('sound-overrides.json'), {})
        overrides[event] = file
        _save_json(_config_path('sound-overrides.json'), overrides)
        return jsonify({'set': True, 'event': event, 'file': file})

    @app.route('/api/shell/sounds/toggle', methods=['POST'])
    def shell_sounds_toggle():
        data = request.get_json(force=True)
        enabled = data.get('enabled', True)
        cfg = _load_json(_config_path('sound-theme.json'), {})
        cfg['enabled'] = bool(enabled)
        _save_json(_config_path('sound-theme.json'), cfg)
        return jsonify({'enabled': bool(enabled)})

    @app.route('/api/shell/sounds/play', methods=['POST'])
    def shell_sounds_play():
        data = request.get_json(force=True)
        file = data.get('file', '')
        event = data.get('event', '')
        if not file and not event:
            return jsonify({'error': 'file or event required'}), 400
        if event and not file:
            overrides = _load_json(_config_path('sound-overrides.json'), {})
            if event in overrides:
                file = overrides[event]
            else:
                cfg = _load_json(_config_path('sound-theme.json'), {'active': 'freedesktop'})
                theme = cfg.get('active', 'freedesktop')
                for ext in ('oga', 'ogg', 'wav'):
                    for base in ['/usr/share/sounds', '/run/current-system/sw/share/sounds']:
                        p = os.path.join(base, theme, 'stereo', f'{event}.{ext}')
                        if os.path.isfile(p):
                            file = p
                            break
                    if file:
                        break
        if not file:
            return jsonify({'played': False, 'error': 'Sound file not found'}), 404
        player = shutil.which('pw-play') or shutil.which('paplay') or shutil.which('aplay')
        if player:
            _run([player, file], timeout=5)
            return jsonify({'played': True, 'file': file})
        return jsonify({'played': False, 'error': 'No audio player available'}), 500

    # ─── 4. Clipboard Manager ──────────────────────────────

    @app.route('/api/shell/clipboard/history', methods=['GET'])
    def shell_clipboard_history():
        limit = int(request.args.get('limit', 50))
        ctype = request.args.get('type', '')
        with _clipboard_lock:
            entries = list(_clipboard_history)
        if ctype:
            entries = [e for e in entries if ctype in e.get('content_type', '')]
        entries = entries[:limit]
        return jsonify({'entries': entries, 'count': len(entries)})

    @app.route('/api/shell/clipboard/current', methods=['GET'])
    def shell_clipboard_current():
        if _is_wayland():
            r = _run(['wl-paste', '--no-newline'])
        else:
            r = _run(['xclip', '-selection', 'clipboard', '-o'])
        content = r.stdout if r and r.returncode == 0 else ''
        return jsonify({'content': content, 'content_type': 'text/plain',
                        'timestamp': time.time()})

    @app.route('/api/shell/clipboard/copy', methods=['POST'])
    def shell_clipboard_copy():
        global _clipboard_counter
        data = request.get_json(force=True)
        content = data.get('content', '')
        ctype = data.get('content_type', 'text/plain')
        if not content:
            return jsonify({'error': 'content required'}), 400
        if _is_wayland():
            _run(['wl-copy', content])
        else:
            subprocess.run(['xclip', '-selection', 'clipboard'],
                           input=content, text=True, timeout=5,
                           capture_output=True)
        with _clipboard_lock:
            _clipboard_counter += 1
            _clipboard_history.appendleft({
                'id': _clipboard_counter, 'content': content[:1000],
                'content_type': ctype, 'timestamp': time.time(),
                'pinned': False,
            })
        return jsonify({'copied': True, 'id': _clipboard_counter})

    @app.route('/api/shell/clipboard/pin', methods=['POST'])
    def shell_clipboard_pin():
        data = request.get_json(force=True)
        entry_id = data.get('id', 0)
        with _clipboard_lock:
            for e in _clipboard_history:
                if e['id'] == entry_id:
                    e['pinned'] = True
                    return jsonify({'pinned': True, 'id': entry_id})
        return jsonify({'error': 'Entry not found'}), 404

    @app.route('/api/shell/clipboard/clear', methods=['POST'])
    def shell_clipboard_clear():
        data = request.get_json(force=True)
        if data.get('all'):
            with _clipboard_lock:
                pinned = [e for e in _clipboard_history if e.get('pinned')]
                count = len(_clipboard_history) - len(pinned)
                _clipboard_history.clear()
                _clipboard_history.extend(pinned)
            return jsonify({'cleared': count})
        entry_id = data.get('id', 0)
        with _clipboard_lock:
            for i, e in enumerate(_clipboard_history):
                if e['id'] == entry_id:
                    del _clipboard_history[i]
                    return jsonify({'cleared': 1})
        return jsonify({'error': 'Entry not found'}), 404

    # ─── 5. Date/Time/Timezone ─────────────────────────────

    @app.route('/api/shell/datetime', methods=['GET'])
    def shell_datetime():
        info = {'timezone': 'UTC', 'utc_offset': '+00:00',
                'ntp_enabled': False, 'ntp_synced': False,
                'clock_format': '24h', 'rtc_in_local_time': False}
        r = _run(['timedatectl', 'show'])
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if '=' not in line:
                    continue
                k, v = line.split('=', 1)
                if k == 'Timezone':
                    info['timezone'] = v
                elif k == 'NTP':
                    info['ntp_enabled'] = v == 'yes'
                elif k == 'NTPSynchronized':
                    info['ntp_synced'] = v == 'yes'
                elif k == 'LocalRTC':
                    info['rtc_in_local_time'] = v == 'yes'
        import datetime
        now = datetime.datetime.now()
        info['datetime'] = now.isoformat()
        tz = datetime.datetime.now(datetime.timezone.utc).astimezone()
        off = tz.strftime('%z')
        info['utc_offset'] = f'{off[:3]}:{off[3:]}' if len(off) >= 5 else off
        cfg = _load_json(_config_path('clock-format.json'), {'format': '24h'})
        info['clock_format'] = cfg.get('format', '24h')
        return jsonify(info)

    @app.route('/api/shell/datetime/timezones', methods=['GET'])
    def shell_datetime_timezones():
        r = _run(['timedatectl', 'list-timezones'], timeout=5)
        if r and r.returncode == 0:
            zones = [z for z in r.stdout.strip().split('\n') if z]
            return jsonify({'timezones': zones, 'count': len(zones)})
        import zoneinfo
        zones = sorted(zoneinfo.available_timezones())
        return jsonify({'timezones': zones, 'count': len(zones)})

    @app.route('/api/shell/datetime/set-timezone', methods=['POST'])
    def shell_datetime_set_tz():
        data = request.get_json(force=True)
        tz = data.get('timezone', '')
        if not tz or '/' not in tz:
            return jsonify({'error': 'Valid timezone required (e.g. US/Pacific)'}), 400
        r = _run(['timedatectl', 'set-timezone', tz])
        if r and r.returncode == 0:
            return jsonify({'set': True, 'timezone': tz})
        return jsonify({'set': False, 'error': r.stderr.strip() if r else 'timedatectl not available'}), 500

    @app.route('/api/shell/datetime/set-ntp', methods=['POST'])
    def shell_datetime_set_ntp():
        data = request.get_json(force=True)
        enabled = data.get('enabled', True)
        val = 'true' if enabled else 'false'
        r = _run(['timedatectl', 'set-ntp', val])
        ok = r and r.returncode == 0
        return jsonify({'set': ok, 'ntp_enabled': bool(enabled)})

    @app.route('/api/shell/datetime/set-format', methods=['POST'])
    def shell_datetime_set_format():
        data = request.get_json(force=True)
        fmt = data.get('format', '')
        if fmt not in ('12h', '24h'):
            return jsonify({'error': 'format must be 12h or 24h'}), 400
        _save_json(_config_path('clock-format.json'), {'format': fmt})
        return jsonify({'set': True, 'clock_format': fmt})

    # ─── 6. Wallpaper Manager ──────────────────────────────

    _WALL_CFG = _config_path('wallpaper.json')
    _WALL_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.svg', '.bmp'}

    @app.route('/api/shell/wallpaper', methods=['GET'])
    def shell_wallpaper():
        cfg = _load_json(_WALL_CFG, {
            'current': '', 'lock_screen': '',
            'mode': 'fill',
            'slideshow': {'enabled': False, 'interval_minutes': 30, 'directory': ''},
        })
        return jsonify(cfg)

    @app.route('/api/shell/wallpaper/collection', methods=['GET'])
    def shell_wallpaper_collection():
        directory = request.args.get('directory', '/usr/share/backgrounds')
        if not os.path.isdir(directory):
            return jsonify({'images': [], 'count': 0})
        images = []
        for f in sorted(os.listdir(directory)):
            ext = os.path.splitext(f)[1].lower()
            if ext not in _WALL_EXTS:
                continue
            fp = os.path.join(directory, f)
            try:
                stat = os.stat(fp)
                images.append({
                    'path': fp, 'name': os.path.splitext(f)[0],
                    'size': stat.st_size,
                })
            except OSError:
                pass
        return jsonify({'images': images, 'count': len(images)})

    @app.route('/api/shell/wallpaper/set', methods=['POST'])
    def shell_wallpaper_set():
        data = request.get_json(force=True)
        path = data.get('path', '')
        mode = data.get('mode', 'fill')
        if not path:
            return jsonify({'error': 'path required'}), 400
        if _is_wayland():
            _run(['swaymsg', 'output', '*', 'bg', path, mode])
        else:
            if mode == 'fill':
                _run(['feh', '--bg-fill', path])
            elif mode == 'fit':
                _run(['feh', '--bg-max', path])
            elif mode == 'center':
                _run(['feh', '--bg-center', path])
            elif mode == 'tile':
                _run(['feh', '--bg-tile', path])
            else:
                _run(['feh', '--bg-scale', path])
        cfg = _load_json(_WALL_CFG, {})
        cfg['current'] = path
        cfg['mode'] = mode
        _save_json(_WALL_CFG, cfg)
        return jsonify({'set': True, 'path': path, 'mode': mode})

    @app.route('/api/shell/wallpaper/set-lock', methods=['POST'])
    def shell_wallpaper_set_lock():
        data = request.get_json(force=True)
        path = data.get('path', '')
        if not path:
            return jsonify({'error': 'path required'}), 400
        cfg = _load_json(_WALL_CFG, {})
        cfg['lock_screen'] = path
        _save_json(_WALL_CFG, cfg)
        return jsonify({'set': True, 'lock_screen': path})

    @app.route('/api/shell/wallpaper/slideshow', methods=['POST'])
    def shell_wallpaper_slideshow():
        data = request.get_json(force=True)
        enabled = data.get('enabled', False)
        interval = data.get('interval_minutes', 30)
        directory = data.get('directory', '')
        img_count = 0
        if directory and os.path.isdir(directory):
            img_count = sum(1 for f in os.listdir(directory)
                           if os.path.splitext(f)[1].lower() in _WALL_EXTS)
        cfg = _load_json(_WALL_CFG, {})
        cfg['slideshow'] = {
            'enabled': bool(enabled),
            'interval_minutes': int(interval),
            'directory': directory,
        }
        _save_json(_WALL_CFG, cfg)
        return jsonify({'slideshow': {**cfg['slideshow'], 'image_count': img_count}})

    # ─── 7. Input Methods / Keyboard Layouts ───────────────

    @app.route('/api/shell/input-methods', methods=['GET'])
    def shell_input_methods():
        info = {'layouts': [], 'active': '', 'compose_key': '',
                'ime': {'engine': 'none', 'running': False, 'input_methods': []}}
        r = _run(['setxkbmap', '-query'])
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if ':' not in line:
                    continue
                key, val = line.split(':', 1)
                key, val = key.strip(), val.strip()
                if key == 'layout':
                    layouts = val.split(',')
                    info['active'] = layouts[0] if layouts else ''
                    for l in layouts:
                        info['layouts'].append({'id': l.strip(), 'name': l.strip()})
                elif key == 'options':
                    for opt in val.split(','):
                        opt = opt.strip()
                        if opt.startswith('compose:'):
                            info['compose_key'] = opt.split(':')[1]
        for engine in ('fcitx5', 'ibus-daemon'):
            r2 = _run(['pgrep', '-x', engine])
            if r2 and r2.returncode == 0:
                info['ime']['engine'] = engine.replace('-daemon', '')
                info['ime']['running'] = True
                break
        return jsonify(info)

    @app.route('/api/shell/input-methods/available', methods=['GET'])
    def shell_input_methods_available():
        r = _run(['localectl', 'list-x11-keymap-layouts'], timeout=5)
        layouts = []
        if r and r.returncode == 0:
            for lay in r.stdout.strip().split('\n'):
                if lay.strip():
                    layouts.append({'id': lay.strip(), 'name': lay.strip()})
        ime_engines = ['pinyin', 'mozc', 'hangul', 'anthy', 'chewing', 'libpinyin']
        return jsonify({'layouts': layouts[:200], 'ime_engines': ime_engines})

    @app.route('/api/shell/input-methods/add', methods=['POST'])
    def shell_input_methods_add():
        data = request.get_json(force=True)
        layout = data.get('layout', '')
        if not layout:
            return jsonify({'error': 'layout required'}), 400
        r = _run(['setxkbmap', '-query'])
        current = ''
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if line.strip().startswith('layout'):
                    current = line.split(':', 1)[1].strip()
        new_layouts = f'{current},{layout}' if current else layout
        _run(['setxkbmap', '-layout', new_layouts, '-option', 'grp:alt_shift_toggle'])
        return jsonify({'added': True, 'layout': layout, 'all_layouts': new_layouts})

    @app.route('/api/shell/input-methods/remove', methods=['POST'])
    def shell_input_methods_remove():
        data = request.get_json(force=True)
        layout = data.get('layout', '')
        if not layout:
            return jsonify({'error': 'layout required'}), 400
        r = _run(['setxkbmap', '-query'])
        current_layouts = []
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                if line.strip().startswith('layout'):
                    current_layouts = [l.strip() for l in line.split(':', 1)[1].strip().split(',')]
        if layout in current_layouts:
            current_layouts.remove(layout)
        if current_layouts:
            _run(['setxkbmap', '-layout', ','.join(current_layouts)])
        return jsonify({'removed': True, 'layout': layout})

    @app.route('/api/shell/input-methods/switch', methods=['POST'])
    def shell_input_methods_switch():
        data = request.get_json(force=True)
        layout = data.get('layout', '')
        if not layout:
            return jsonify({'error': 'layout required'}), 400
        _run(['setxkbmap', layout])
        return jsonify({'switched': True, 'active': layout})

    @app.route('/api/shell/input-methods/compose-key', methods=['POST'])
    def shell_input_methods_compose_key():
        data = request.get_json(force=True)
        key = data.get('key', 'ralt')
        _run(['setxkbmap', '-option', f'compose:{key}'])
        return jsonify({'set': True, 'compose_key': key})

    # ─── 8. Night Light / Blue Light Filter ────────────────

    _NL_CFG = _config_path('nightlight.json')

    @app.route('/api/shell/nightlight', methods=['GET'])
    def shell_nightlight():
        cfg = _load_json(_NL_CFG, {
            'enabled': False, 'temperature': 4500,
            'schedule': {'mode': 'sunset', 'start': '20:00', 'end': '06:00'},
        })
        active = False
        for proc in ('gammastep', 'redshift'):
            r = _run(['pgrep', '-x', proc])
            if r and r.returncode == 0:
                active = True
                break
        cfg['active'] = active
        return jsonify(cfg)

    @app.route('/api/shell/nightlight/toggle', methods=['POST'])
    def shell_nightlight_toggle():
        data = request.get_json(force=True)
        enabled = data.get('enabled', True)
        cfg = _load_json(_NL_CFG, {'temperature': 4500})
        cfg['enabled'] = bool(enabled)
        _save_json(_NL_CFG, cfg)
        if enabled:
            temp = cfg.get('temperature', 4500)
            tool = shutil.which('gammastep') or shutil.which('redshift')
            if tool:
                _run(['pkill', '-x', os.path.basename(tool)])
                subprocess.Popen([tool, '-O', str(temp)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return jsonify({'enabled': True, 'active': True, 'temperature': temp})
            return jsonify({'enabled': True, 'active': False,
                            'error': 'Neither gammastep nor redshift available'}), 500
        for proc in ('gammastep', 'redshift'):
            _run(['pkill', '-x', proc])
        return jsonify({'enabled': False, 'active': False})

    @app.route('/api/shell/nightlight/temperature', methods=['POST'])
    def shell_nightlight_temperature():
        data = request.get_json(force=True)
        temp = data.get('temperature', 4500)
        if not (1000 <= temp <= 6500):
            return jsonify({'error': 'temperature must be 1000-6500'}), 400
        cfg = _load_json(_NL_CFG, {})
        cfg['temperature'] = temp
        _save_json(_NL_CFG, cfg)
        tool = shutil.which('gammastep') or shutil.which('redshift')
        if tool:
            _run(['pkill', '-x', os.path.basename(tool)])
            subprocess.Popen([tool, '-O', str(temp)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'set': True, 'temperature': temp})

    @app.route('/api/shell/nightlight/schedule', methods=['POST'])
    def shell_nightlight_schedule():
        data = request.get_json(force=True)
        mode = data.get('mode', 'manual')
        if mode not in ('sunset', 'manual', 'disabled'):
            return jsonify({'error': 'mode must be sunset, manual, or disabled'}), 400
        cfg = _load_json(_NL_CFG, {})
        cfg['schedule'] = {
            'mode': mode,
            'start': data.get('start', '20:00'),
            'end': data.get('end', '06:00'),
        }
        _save_json(_NL_CFG, cfg)
        return jsonify({'set': True, 'schedule': cfg['schedule']})

    # ─── 9. Window / Workspace Manager ─────────────────────

    def _detect_compositor():
        if os.environ.get('SWAYSOCK'):
            return 'sway'
        if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
            return 'hyprland'
        if _is_wayland():
            return 'wayland'
        return 'x11'

    @app.route('/api/shell/workspaces', methods=['GET'])
    def shell_workspaces():
        comp = _detect_compositor()
        workspaces = []
        if comp == 'sway':
            r = _run(['swaymsg', '-t', 'get_workspaces', '--raw'])
            if r and r.returncode == 0:
                try:
                    for ws in json.loads(r.stdout):
                        workspaces.append({
                            'id': ws.get('num', ws.get('id')),
                            'name': ws.get('name', ''),
                            'focused': ws.get('focused', False),
                            'visible': ws.get('visible', False),
                        })
                except json.JSONDecodeError:
                    pass
        elif comp == 'x11':
            r = _run(['wmctrl', '-d'])
            if r and r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    parts = line.split()
                    if len(parts) >= 9:
                        workspaces.append({
                            'id': int(parts[0]),
                            'name': parts[-1],
                            'focused': parts[1] == '*',
                            'visible': parts[1] == '*',
                        })
        if not workspaces:
            workspaces.append({'id': 1, 'name': 'Main', 'focused': True, 'visible': True})
        return jsonify({'workspaces': workspaces, 'compositor': comp})

    @app.route('/api/shell/workspaces/windows', methods=['GET'])
    def shell_workspaces_windows():
        comp = _detect_compositor()
        workspace = request.args.get('workspace', '')
        windows = []
        if comp == 'sway':
            r = _run(['swaymsg', '-t', 'get_tree', '--raw'])
            if r and r.returncode == 0:
                try:
                    tree = json.loads(r.stdout)
                    _extract_sway_windows(tree, windows)
                except json.JSONDecodeError:
                    pass
        elif comp == 'x11':
            r = _run(['wmctrl', '-l', '-G'])
            if r and r.returncode == 0:
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(None, 7)
                    if len(parts) >= 8:
                        windows.append({
                            'id': parts[0], 'workspace': int(parts[1]),
                            'x': int(parts[2]), 'y': int(parts[3]),
                            'w': int(parts[4]), 'h': int(parts[5]),
                            'title': parts[7] if len(parts) > 7 else '',
                        })
        if workspace:
            try:
                ws_id = int(workspace)
                windows = [w for w in windows if w.get('workspace') == ws_id]
            except ValueError:
                pass
        return jsonify({'windows': windows, 'count': len(windows)})

    def _extract_sway_windows(node, results):
        if node.get('type') == 'con' and node.get('name') and node.get('pid'):
            rect = node.get('rect', {})
            results.append({
                'id': node.get('id'),
                'title': node.get('name', ''),
                'app': node.get('app_id', ''),
                'workspace': node.get('workspace', ''),
                'focused': node.get('focused', False),
                'x': rect.get('x', 0), 'y': rect.get('y', 0),
                'w': rect.get('width', 0), 'h': rect.get('height', 0),
            })
        for child in node.get('nodes', []) + node.get('floating_nodes', []):
            _extract_sway_windows(child, results)

    @app.route('/api/shell/workspaces/create', methods=['POST'])
    def shell_workspaces_create():
        data = request.get_json(force=True)
        name = data.get('name', '')
        comp = _detect_compositor()
        if comp == 'sway':
            ws_name = name or str(int(time.time()) % 100)
            _run(['swaymsg', f'workspace {ws_name}'])
            return jsonify({'created': True, 'name': ws_name})
        return jsonify({'created': False, 'error': f'{comp}: workspace creation not supported'}), 500

    @app.route('/api/shell/workspaces/switch', methods=['POST'])
    def shell_workspaces_switch():
        data = request.get_json(force=True)
        ws_id = data.get('id', '')
        name = data.get('name', str(ws_id))
        comp = _detect_compositor()
        if comp == 'sway':
            _run(['swaymsg', f'workspace {name}'])
            return jsonify({'switched': True, 'workspace': name})
        elif comp == 'x11':
            _run(['wmctrl', '-s', str(ws_id)])
            return jsonify({'switched': True, 'workspace': ws_id})
        return jsonify({'switched': False}), 500

    @app.route('/api/shell/workspaces/close', methods=['POST'])
    def shell_workspaces_close():
        data = request.get_json(force=True)
        ws_id = data.get('id', '')
        return jsonify({'closed': True, 'id': ws_id})

    _SNAP_MAP = {
        'left-half': 'move position 0 0; resize set 50 ppt 100 ppt',
        'right-half': 'move position 50 ppt 0; resize set 50 ppt 100 ppt',
        'top-half': 'move position 0 0; resize set 100 ppt 50 ppt',
        'bottom-half': 'move position 0 50 ppt; resize set 100 ppt 50 ppt',
        'top-left': 'move position 0 0; resize set 50 ppt 50 ppt',
        'top-right': 'move position 50 ppt 0; resize set 50 ppt 50 ppt',
        'bottom-left': 'move position 0 50 ppt; resize set 50 ppt 50 ppt',
        'bottom-right': 'move position 50 ppt 50 ppt; resize set 50 ppt 50 ppt',
        'maximize': 'fullscreen enable',
        'center': 'move position center',
    }

    @app.route('/api/shell/workspaces/snap', methods=['POST'])
    def shell_workspaces_snap():
        data = request.get_json(force=True)
        window_id = data.get('window_id', '')
        position = data.get('position', '')
        if position not in _SNAP_MAP:
            return jsonify({'error': f'Invalid position. Valid: {list(_SNAP_MAP.keys())}'}), 400
        comp = _detect_compositor()
        if comp == 'sway':
            cmd_str = _SNAP_MAP[position]
            if window_id:
                _run(['swaymsg', f'[con_id={window_id}] {cmd_str}'])
            else:
                _run(['swaymsg', cmd_str])
            return jsonify({'snapped': True, 'position': position})
        elif comp == 'x11' and window_id:
            if position == 'maximize':
                _run(['wmctrl', '-i', '-r', window_id, '-b', 'add,maximized_vert,maximized_horz'])
            return jsonify({'snapped': True, 'position': position})
        return jsonify({'snapped': False, 'error': f'{comp}: snap not supported'}), 500

    # ─── Multi-Monitor Management ──────────────────────────

    @app.route('/api/shell/displays', methods=['GET'])
    def shell_displays_list():
        """List connected displays with resolution and position."""
        displays = []
        if _is_wayland():
            r = _run(['swaymsg', '-t', 'get_outputs', '-r'], timeout=5)
            if r and r.returncode == 0:
                try:
                    outputs = json.loads(r.stdout)
                    for out in outputs:
                        displays.append({
                            'name': out.get('name', ''),
                            'make': out.get('make', ''),
                            'model': out.get('model', ''),
                            'resolution': f"{out.get('rect', {}).get('width', 0)}x{out.get('rect', {}).get('height', 0)}",
                            'position': f"{out.get('rect', {}).get('x', 0)},{out.get('rect', {}).get('y', 0)}",
                            'scale': out.get('scale', 1.0),
                            'active': out.get('active', False),
                        })
                except (json.JSONDecodeError, KeyError):
                    pass
        else:
            r = _run(['xrandr', '--query'], timeout=5)
            if r and r.returncode == 0:
                import re
                for line in r.stdout.split('\n'):
                    m = re.match(r'^(\S+)\s+(connected|disconnected)\s*(primary)?\s*(\d+x\d+\+\d+\+\d+)?', line)
                    if m and m.group(2) == 'connected':
                        displays.append({
                            'name': m.group(1),
                            'primary': m.group(3) == 'primary',
                            'geometry': m.group(4) or '',
                            'connected': True,
                        })
        return jsonify({'displays': displays, 'compositor': 'wayland' if _is_wayland() else 'x11'})

    @app.route('/api/shell/displays/arrange', methods=['PUT'])
    def shell_displays_arrange():
        """Arrange displays (position, resolution, scale)."""
        body = request.get_json(silent=True) or {}
        display = body.get('display', '')
        if not display:
            return jsonify({'error': 'display name is required'}), 400
        if _is_wayland():
            cmd = ['swaymsg', 'output', display]
            if 'position' in body:
                cmd += ['position', str(body['position'])]
            if 'resolution' in body:
                cmd += ['resolution', str(body['resolution'])]
            if 'scale' in body:
                cmd += ['scale', str(body['scale'])]
            r = _run(cmd, timeout=5)
            ok = r and r.returncode == 0
        else:
            cmd = ['xrandr', '--output', display]
            if 'resolution' in body:
                cmd += ['--mode', str(body['resolution'])]
            if 'position' in body:
                cmd += ['--pos', str(body['position'])]
            r = _run(cmd, timeout=5)
            ok = r and r.returncode == 0
        return jsonify({'status': 'ok' if ok else 'error',
                       'message': (r.stderr if r else 'Command failed') if not ok else ''})

    # ─── HiDPI Scaling ─────────────────────────────────────

    @app.route('/api/shell/display/scale', methods=['GET', 'PUT'])
    def shell_display_scale():
        """Get or set display scale factor."""
        if request.method == 'GET':
            scale = 1.0
            if _is_wayland():
                r = _run(['swaymsg', '-t', 'get_outputs', '-r'], timeout=5)
                if r and r.returncode == 0:
                    try:
                        outputs = json.loads(r.stdout)
                        if outputs:
                            scale = outputs[0].get('scale', 1.0)
                    except (json.JSONDecodeError, KeyError):
                        pass
            else:
                gdk_scale = os.environ.get('GDK_SCALE', '1')
                try:
                    scale = float(gdk_scale)
                except ValueError:
                    scale = 1.0
            return jsonify({'scale': scale, 'compositor': 'wayland' if _is_wayland() else 'x11'})
        # PUT
        body = request.get_json(silent=True) or {}
        scale = body.get('scale', 1.0)
        display = body.get('display', '')
        if _is_wayland() and display:
            r = _run(['swaymsg', 'output', display, 'scale', str(scale)], timeout=5)
            ok = r and r.returncode == 0
        else:
            # X11 fallback: set GDK_SCALE env
            os.environ['GDK_SCALE'] = str(int(scale))
            ok = True
        return jsonify({'status': 'ok' if ok else 'error', 'scale': scale})

    # ─── Per-App Volume Control ────────────────────────────

    @app.route('/api/shell/audio/apps', methods=['GET'])
    def shell_audio_per_app():
        """List audio streams with per-app volume."""
        apps = []
        r = _run(['pw-cli', 'list-objects'], timeout=5)
        if r and r.returncode == 0:
            current = {}
            for line in r.stdout.split('\n'):
                line = line.strip()
                if line.startswith('id '):
                    if current.get('type') == 'PipeWire:Interface:Node':
                        apps.append(current)
                    current = {'id': line.split()[1].rstrip(',')}
                elif 'type' in line and 'PipeWire:Interface:Node' in line:
                    current['type'] = 'PipeWire:Interface:Node'
                elif 'application.name' in line:
                    current['name'] = line.split('=', 1)[1].strip().strip('"')
                elif 'node.name' in line:
                    current['node_name'] = line.split('=', 1)[1].strip().strip('"')
            if current.get('type') == 'PipeWire:Interface:Node':
                apps.append(current)
        # Filter to only app nodes
        app_nodes = [a for a in apps if a.get('name') or a.get('node_name')]
        return jsonify({'apps': app_nodes})

    @app.route('/api/shell/audio/apps/<node_id>/volume', methods=['PUT'])
    def shell_audio_per_app_volume(node_id):
        """Set volume for a specific app/node."""
        body = request.get_json(silent=True) or {}
        volume = body.get('volume', 1.0)
        if not isinstance(volume, (int, float)) or volume < 0 or volume > 2.0:
            return jsonify({'error': 'volume must be 0.0 to 2.0'}), 400
        r = _run(['pw-cli', 'set-param', node_id, 'Props',
                  json.dumps({'volume': volume})], timeout=5)
        if r and r.returncode == 0:
            return jsonify({'status': 'ok', 'node_id': node_id, 'volume': volume})
        return jsonify({'error': r.stderr if r else 'pw-cli not available'}), 500

    # ─── RTL Layout Support ────────────────────────────────

    _RTL_LOCALES = {'ar', 'he', 'fa', 'ur', 'yi', 'ps', 'sd'}

    @app.route('/api/shell/rtl/status', methods=['GET'])
    def shell_rtl_status():
        """Check if current locale requires RTL layout."""
        locale_val = os.environ.get('LANG', 'en_US.UTF-8')
        lang_code = locale_val.split('_')[0].lower()
        is_rtl = lang_code in _RTL_LOCALES
        return jsonify({
            'rtl': is_rtl,
            'locale': locale_val,
            'css_direction': 'rtl' if is_rtl else 'ltr',
        })

    logger.info("Registered shell desktop routes (13 features)")
