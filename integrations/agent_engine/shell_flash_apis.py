"""Shell APIs: Flash HART OS to a USB stick from the running desktop.

WHAT: lets a running HART OS node create more install sticks from inside the glass
shell — pick a removable disk, pick the variant, watch a live progress bar. This is
the desktop-side "installer wizard → flasher" the steward asked for: when the user
wants to flash to USB, the shell drives the flasher instead of needing a terminal.

DRY: this DRIVES the proven, hardened scripts/hart_usb_flasher.py (cross-platform
disk enumeration with the pnputil/diskpart self-heal, the exclusive-handle raw
write, the ISO9660 + 0x55AA bootable-signature verify). It does NOT reimplement any
disk writing — it imports the flasher and orchestrates one background job, reporting
the flasher's own `progress` fraction + log lines to a polled endpoint.

SAFETY (inherited from the flasher + enforced here):
  * Only removable / USB disks are ever offered (allow_system stays False) — a system
    disk can never be a target, so the wizard cannot brick the running machine.
  * One flash job at a time (a second start returns 409).
  * The long write runs in a daemon thread; the request returns immediately.

RUNTIME REALITY (surfaced to the wizard, never a crash): the flash needs the GitHub
CLI `gh` (to fetch the release image) and raw-disk write permission. If `gh` is
absent or the write is refused, the job ends in state 'error' with a clear message
and the wizard shows it. (Future: flash the running OS's OWN squashfs image instead
of downloading — no network/gh needed. Tracked TODO below.)
"""
import importlib.util
import os
import threading

from flask import jsonify, request

# ── Lazy one-time load of the proven flasher (scripts/ is not a package) ──
_FLASHER = None
_FLASHER_ERR = None


def _load_flasher():
    """Import scripts/hart_usb_flasher.py once. Its top level is pure defs +
    constants (main() is __main__-gated, tkinter is lazy), so exec is side-effect
    free. Returns the module or None (with _FLASHER_ERR set)."""
    global _FLASHER, _FLASHER_ERR
    if _FLASHER is not None:
        return _FLASHER
    if _FLASHER_ERR is not None:
        return None
    try:
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..'))
        path = os.path.join(repo_root, 'scripts', 'hart_usb_flasher.py')
        spec = importlib.util.spec_from_file_location('hart_usb_flasher', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _FLASHER = mod
        return mod
    except Exception as e:                       # never raise into the route
        _FLASHER_ERR = str(e)
        return None


# ── Single-job state (polled by the wizard) ──
_JOB = {
    'state': 'idle',          # idle | running | done | error
    'fraction': 0.0,          # 0.0 .. 1.0 (the flasher's own progress)
    'message': '',
    'lines': [],              # rolling tail of the flasher log
    'device': None,
    'tag': None,
    'variant': None,
    'ok': None,
}
_JOB_LOCK = threading.Lock()


def _set(**kw):
    with _JOB_LOCK:
        _JOB.update(kw)


def _log_line(msg):
    with _JOB_LOCK:
        _JOB['lines'].append(str(msg))
        if len(_JOB['lines']) > 200:
            _JOB['lines'] = _JOB['lines'][-200:]
        _JOB['message'] = str(msg)


def _run_flash(device_number, variant, tag):
    """Background worker: resolve the disk + tag, then drive flasher.flash().
    Every failure path ends in state='error' with a clear message — never raises."""
    f = _load_flasher()
    if f is None:
        _set(state='error', ok=False,
             message='Flasher unavailable: ' + str(_FLASHER_ERR or 'unknown'))
        return
    try:
        _set(state='running', fraction=0.0, ok=None, lines=[],
             device=device_number, tag=tag, variant=variant)
        gh = f.find_gh()
        if not gh:
            _set(state='error', ok=False,
                 message='GitHub CLI (gh) not found - cannot fetch the release '
                         'image to flash.')
            return
        if not tag:
            tag = f.latest_nightly_tag(gh)
        if not tag:
            _set(state='error', ok=False,
                 message='No nightly release found to flash.')
            return
        _, candidates = f.list_disks_with_self_heal(allow_system=False,
                                                     log=_log_line)
        disk = None
        for d in candidates:
            if str(d['number']) == str(device_number):
                disk = d
                break
        if disk is None:
            _set(state='error', ok=False,
                 message='That disk is no longer an offered removable/USB target.')
            return
        if disk.get('system'):                   # belt-and-suspenders (never offered)
            _set(state='error', ok=False, message='Refusing to flash a system disk.')
            return
        _set(tag=tag, message='Preparing ' + str(disk.get('model', 'disk')) + '...')
        tmp = f.default_tmp()
        os.makedirs(tmp, exist_ok=True)

        def _progress(frac):
            try:
                _set(fraction=float(frac))
            except (TypeError, ValueError):
                pass

        ok = f.flash(tag, variant, disk, 'download', tmp,
                     progress=_progress, log=_log_line)
        if ok:
            _set(state='done', fraction=1.0, ok=True,
                 message='Flash complete - the stick is bootable.')
        else:
            _set(state='error', ok=False,
                 message='Flash finished but the bootable-signature check failed.')
    except Exception as e:
        _set(state='error', ok=False, message='Flash failed: ' + str(e))


def register_shell_flash_routes(app):
    """Mount /api/shell/flash/{disks,start,progress} on the shell app (:6800)."""

    @app.route('/api/shell/flash/disks', methods=['GET'],
               endpoint='shell_flash_disks')
    def shell_flash_disks():
        f = _load_flasher()
        if f is None:
            return jsonify({'available': False,
                            'error': 'flasher unavailable: '
                                     + str(_FLASHER_ERR or 'unknown')}), 200
        try:
            _, candidates = f.list_disks_with_self_heal(allow_system=False)
            gh = f.find_gh()
            tag = f.latest_nightly_tag(gh) if gh else None
            disks = [{
                'number': d['number'],
                'model': d.get('model', '?'),
                'size': d.get('size', 0),
                'size_human': f.human(d.get('size', 0)),
                'removable': bool(d.get('removable')),
            } for d in candidates]
            return jsonify({
                'available': True,
                'disks': disks,
                'gh': bool(gh),                  # false -> wizard explains gh is needed
                'tag': tag,
                'variants': ['desktop', 'server', 'edge'],
            })
        except Exception as e:
            return jsonify({'available': False, 'error': str(e)}), 200

    @app.route('/api/shell/flash/start', methods=['POST'],
               endpoint='shell_flash_start')
    def shell_flash_start():
        data = request.get_json(silent=True) or {}
        device = data.get('device')
        variant = data.get('variant', 'desktop')
        tag = data.get('tag') or None
        if device is None:
            return jsonify({'success': False, 'error': 'device required'}), 400
        if variant not in ('desktop', 'server', 'edge'):
            return jsonify({'success': False, 'error': 'invalid variant'}), 400
        # ATOMIC check-and-claim: set state='running' under the SAME lock as the
        # is-running check, so two concurrent POSTs can NEVER both launch a flash
        # (two flashers writing one disk would be catastrophic). _run_flash drives
        # it from here and always reaches a terminal state (done/error), releasing
        # the claim. The worker re-asserts these fields (harmless, same values).
        with _JOB_LOCK:
            if _JOB['state'] == 'running':
                return jsonify({'success': False,
                                'error': 'A flash is already in progress.'}), 409
            _JOB.update({'state': 'running', 'fraction': 0.0, 'ok': None,
                         'lines': [], 'message': 'Starting...',
                         'device': device, 'variant': variant, 'tag': tag})
        t = threading.Thread(target=_run_flash, args=(device, variant, tag),
                             daemon=True)
        t.start()
        return jsonify({'success': True, 'state': 'running'})

    @app.route('/api/shell/flash/progress', methods=['GET'],
               endpoint='shell_flash_progress')
    def shell_flash_progress():
        with _JOB_LOCK:
            snap = dict(_JOB)
            snap['lines'] = list(_JOB['lines'][-12:])   # only the recent tail
        return jsonify(snap)
