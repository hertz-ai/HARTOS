"""
Native org.freedesktop.login1 (logind) client — the CANONICAL, RESULT-CHECKED
"ask the OS to reboot / power off / suspend / hibernate / arm firmware setup / lock /
terminate a session" call for HART OS (#133 / W3).

Two transports, tried in order:

  1. NATIVE D-Bus (``jeepney``, pure-Python) — speaks the D-Bus wire protocol
     directly over the system-bus socket. NO subprocess. This is the steward's
     directive: a native OS call, not a ``busctl`` / ``systemctl`` shell-out.
  2. FALLBACK ``busctl`` subprocess — used ONLY when the native transport is
     unavailable (jeepney absent, or the system bus cannot be opened, e.g. the
     Windows dev box). Preserves the existing bounded, result-checked behaviour so
     nothing regresses if the native path is missing.

INVARIANTS (all preserved from the shell baseline):
  * RESULT-CHECKED — a polkit denial / method error surfaces as ``(False, reason)``,
    NEVER a masked success. This is the whole point of #133.
  * BOUNDED — every call is time-bounded so a hung bus can never pin the 1-2 thread
    shell pool (the hang-free baseline).
  * DEGRADE-NOT-DIE — every failure mode is caught and returned, never raised into
    the request thread.

The matching polkit grant (the ``hart`` service user is authorized for the login1
actions) lives in nixos/modules/hart-base.nix ``security.polkit``.
"""

import logging
import subprocess
import threading

logger = logging.getLogger('hevolve.shell.os_bridge.logind')

# ── Well-known D-Bus names (login1 Manager) — the ONE canonical copy. ──
_LOGIN1_DEST = 'org.freedesktop.login1'
_LOGIN1_PATH = '/org/freedesktop/login1'
_LOGIN1_IFACE = 'org.freedesktop.login1.Manager'

# ── Native transport (jeepney) — optional. Guarded so this module imports cleanly
# on a box with no D-Bus (the Windows dev box): the fallback busctl path is used
# there instead. jeepney is pure-Python (no C extension), so it bundles under
# cx_Freeze and packages trivially in Nix — cleaner + more buildable here than
# libsystemd-backed sd-bus. ──
try:
    from jeepney import DBusAddress, new_method_call, MessageType, HeaderFields
    from jeepney.io.blocking import open_dbus_connection
    _NATIVE_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only where jeepney is absent
    DBusAddress = new_method_call = MessageType = HeaderFields = None
    open_dbus_connection = None
    _NATIVE_AVAILABLE = False


def _busctl_args_to_body(busctl_args):
    """Translate the ``(signature, *string_values)`` busctl-form tuple every caller
    passes into a jeepney ``(signature, body_tuple)``.

    Only the login1 method shapes HART OS actually calls are supported:
      * ``()``            -> ``('', ())``      — a no-arg method (LockSessions)
      * ``('b', 'true')`` -> ``('b', (True,))``  — the interactive / enable bool
        (Reboot / PowerOff / Suspend / Hibernate / SetRebootToFirmwareSetup)
      * ``('s', sid)``    -> ``('s', (sid,))``   — a session id (TerminateSession)

    Raises ``ValueError`` for anything else, so we NEVER send a malformed native
    message — the caller falls back to busctl instead of guessing the wire layout.
    """
    if not busctl_args:
        return '', ()
    signature = busctl_args[0]
    values = tuple(busctl_args[1:])
    if signature == '':
        return '', ()
    if signature == 'b':
        if len(values) != 1:
            raise ValueError("signature 'b' needs exactly one value")
        truthy = str(values[0]).strip().lower() in ('1', 'true', 'yes', 'on')
        return 'b', (truthy,)
    if signature == 's':
        if len(values) != 1:
            raise ValueError("signature 's' needs exactly one value")
        return 's', (str(values[0]),)
    raise ValueError('unsupported busctl signature: {!r}'.format(signature))


def _bounded(fn, wait):
    """Run ``fn()`` on a daemon worker, waiting at most ``wait`` seconds for it.

    Returns ``(finished, value)``. ``(False, None)`` means the worker is still
    running (it keeps going, but the caller is freed) — this guards against a hung
    bus pinning the shell pool. Mirrors shell_system_apis._run_async_bounded.
    """
    holder = {}
    done = threading.Event()

    def _worker():
        try:
            holder['value'] = fn()
        except Exception as e:  # pragma: no cover - defensive
            holder['value'] = ('__exc__', e)
        finally:
            done.set()

    threading.Thread(target=_worker, name='hart-logind-native', daemon=True).start()
    finished = done.wait(wait)
    return finished, holder.get('value')


def _native_logind_call(method, signature, body, timeout):
    """Attempt the NATIVE D-Bus call. Returns:
        (True, None)     — logind accepted AND polkit authorized the method.
        (False, reason)  — a REAL method error (polkit denial, etc.). AUTHORITATIVE:
                           the caller must NOT fall back to busctl.
        None             — the native TRANSPORT is unavailable / could not connect;
                           the caller should fall back to busctl.
    """
    if not _NATIVE_AVAILABLE:
        return None
    try:
        conn = open_dbus_connection(bus='SYSTEM')
    except Exception:
        # No system bus reachable here (e.g. the dev box) -> fall back to busctl.
        return None
    try:
        addr = DBusAddress(_LOGIN1_PATH, bus_name=_LOGIN1_DEST, interface=_LOGIN1_IFACE)
        msg = new_method_call(addr, method, signature or None, body or ())
        try:
            reply = conn.send_and_get_reply(msg, timeout=timeout)
        except TypeError:
            # Older jeepney: send_and_get_reply has no timeout kwarg.
            reply = conn.send_and_get_reply(msg)
        if reply.header.message_type == MessageType.error:
            err_name = ''
            try:
                err_name = reply.header.fields.get(HeaderFields.error_name, '') or ''
            except Exception:
                pass
            detail = ''
            try:
                if reply.body and isinstance(reply.body[0], str):
                    detail = reply.body[0]
            except Exception:
                pass
            reason = 'logind {} denied or failed'.format(method)
            if err_name:
                reason += ': ' + err_name
            if detail:
                reason += ' ({})'.format(detail)
            return False, reason
        return True, None
    except Exception:
        # A send/parse error AFTER a good connect — transport-level uncertainty.
        # Return None so the caller falls back to the proven busctl path.
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _busctl_logind_call(method, busctl_args, timeout):
    """FALLBACK: invoke the method via ``busctl call --system`` and CHECK the exit
    status + stderr. ``busctl_args`` is the ``(signature, *string_values)`` tuple
    (e.g. ``('b', 'true')``). Returns ``(ok, error)``; ``ok`` is True only when
    busctl exits 0 (logind accepted AND polkit authorized). Every failure mode is
    caught + reported, never raised."""
    cmd = ['busctl', 'call', '--system',
           _LOGIN1_DEST, _LOGIN1_PATH, _LOGIN1_IFACE, method, *busctl_args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, 'busctl not available (systemd D-Bus tooling missing)'
    except subprocess.TimeoutExpired:
        return False, 'logind {} timed out after {}s'.format(method, timeout)
    except Exception as e:  # pragma: no cover - defensive
        return False, 'logind {} failed: {}'.format(method, e)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or '').strip() or 'exit code {}'.format(r.returncode)
        return False, 'logind {} denied or failed: {}'.format(method, detail)
    return True, None


def logind_call(method, busctl_args=(), timeout=10):
    """Invoke an ``org.freedesktop.login1.Manager`` method, RESULT-CHECKED.

    ``busctl_args`` is the ``(signature, *string_values)`` tuple that the existing
    call sites already pass (``('b', 'true')`` / ``('s', sid)`` / ``()``), kept so
    the ``shell_os_apis._logind_call`` shim delegates here with no signature change.

    Returns ``(ok: bool, error: Optional[str])``. ``ok`` is True ONLY when logind
    accepted the method AND polkit authorized it. A polkit denial / missing tool /
    timeout each return ``(False, <reason>)`` so the caller surfaces a REAL error
    instead of a masked success. Native D-Bus is tried first (no subprocess); the
    bounded busctl subprocess is the degrade fallback.
    """
    busctl_args = tuple(busctl_args)

    if _NATIVE_AVAILABLE:
        try:
            signature, body = _busctl_args_to_body(busctl_args)
        except ValueError:
            # An arg shape the native translator does not know — skip native, use
            # busctl (which passes the raw signature/values straight through).
            signature = None
        if signature is not None:
            # Bound the native attempt so a hung bus can never pin the request
            # thread. If it does not settle in time, that IS the authoritative
            # answer (an honest timeout) — do NOT also run busctl (it would likely
            # stall the same way).
            finished, value = _bounded(
                lambda: _native_logind_call(method, signature, body, timeout),
                wait=timeout + 2)
            if not finished:
                return False, 'logind {} timed out after {}s (native)'.format(method, timeout)
            if isinstance(value, tuple) and len(value) == 2 and value[0] == '__exc__':
                # A worker exception — treat as transport uncertainty, fall back.
                value = None
            if value is not None:
                # (True, None) success OR (False, reason) real denial — authoritative.
                return value
            # value is None -> native transport unavailable; fall through to busctl.

    return _busctl_logind_call(method, busctl_args, timeout)
