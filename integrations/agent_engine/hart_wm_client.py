"""HART OS — HartWmClient: the brain's privileged window-manager client (Phase 6).

The AI-native MOAT: agents arrange REAL native windows through ONE gated client —
the thing GNOME/Copilot cannot match (an AI that owns window-PLACEMENT POLICY,
not one that scripts a settings page).

TWO TRANSPORTS, ONE SURFACE. Tier-1 (native HART-comp) speaks the framed-JSON
`com.hart.Compositor` socket that `compositor/src/ipc.rs` serves against the
real `Space<Window>`; Tier-2 (sway) keeps the `swaymsg` shim. Every method below
takes the same arguments and returns the same shape on both, so no caller, and
no banked layout recipe, knows or cares which compositor answered. ipc.rs asked
for exactly this ("swap its swaymsg shim for a socket client speaking the SAME
framed JSON, same dispatch_verb surface"); until now only the Rust half existed,
so on the native tier every window verb failed silently.

DEPLOYMENT NOTE. The compositor binds its socket 0600 as the session user and
hart-liquid-ui runs as `hart`, so a system service reaches it only through a
root relay (`HART_COMP_SOCK`), the same shape the hart-sway-ipc relay already
uses for Tier-2. Without that variable this client still finds the socket
directly whenever the caller IS the session user.

CONSTITUTION (compositor/IPC_PROTOCOL.md §6): every DESTRUCTIVE verb is
fail-CLOSED — refused if the hive is halted OR the guardrail can't be consulted
— and recorded in the immutable audit log. An agent closing a real window is
governed EXACTLY like an agent dispatching a goal (dispatch.py:668-683). Read
ops + non-destructive arrange (focus/place/tile) are not gated.

Reuses integrations.agent_engine.shell_desktop_apis._run / _is_wayland (the one
canonical swaymsg/subprocess path — no parallel boilerplate).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.hart_wm')

# Verbs that MUTATE the desktop destructively — fail-CLOSED gated.
DESTRUCTIVE_VERBS = frozenset({'window.close', 'window.fullscreen'})

# HART-comp socket transport. The timeout is short on purpose: the compositor
# answers a window op from its calloop loop in well under a frame, so a wait
# longer than this means the peer is wedged, and the brain must degrade rather
# than block the shell request that is riding on it.
_HC_TIMEOUT = 2.0
# Mirrors ipc.rs::MAX_FRAME_LEN so a malformed length prefix cannot make the
# brain allocate gigabytes either.
_HC_MAX_FRAME = 1024 * 1024


def _run(cmd, timeout=10):
    """Reuse the shell's subprocess wrapper (DRY); minimal local fallback only
    if that module isn't importable (a non-shell node)."""
    try:
        from integrations.agent_engine.shell_desktop_apis import _run as _shell_run
        return _shell_run(cmd, timeout=timeout)
    except Exception:
        import subprocess

        from core.subprocess_safe import no_window_kwargs
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, **no_window_kwargs())
        except Exception:
            return None


class HartWmClient:
    """Brain-side WM client. Tier-1 = the HART-comp socket, Tier-2 = swaymsg."""

    def __init__(self):
        # Resolved once: the probe below costs a round trip, and this client is
        # a singleton (get_wm_client). _hc re-probes if the cached path dies,
        # so a compositor restart recovers without a new client.
        self._hc_path = self._hart_comp_socket()
        self._backend = self._detect_backend()

    # ── transport discovery ──
    @classmethod
    def _hart_comp_socket(cls) -> Optional[str]:
        """The live ``com.hart.Compositor`` socket, or None.

        Resolution order, first CONNECTABLE candidate wins:
          1. ``HART_COMP_SOCK`` — how a SYSTEM service reaches it. The
             compositor binds 0600 as the session user (ipc.rs::socket_path);
             hart-liquid-ui runs as ``hart`` and cannot open that inode, so a
             root relay re-exports it under /run/hart exactly as the existing
             hart-sway-ipc relay already does for Tier-2. The unit points this
             variable at that relay.
          2. ``$XDG_RUNTIME_DIR/hart-comp.sock`` — the compositor's own bind
             path, reachable when the caller IS the session user.

        A candidate counts only if it ANSWERS a real ``window.list``. Neither
        existence nor a successful connect is enough, for two different
        reasons: a socket file outlives the process that bound it, and a
        systemd socket-activated relay ALWAYS accepts, then exits 1 when it
        cannot find an upstream. Accepting either signal would recreate the
        exact bug this replaces, where SWAYSOCK being set was read as proof of
        a working window manager on a tier that had none. Only a well-formed
        reply proves a compositor is behind the socket.
        """
        # No AF_UNIX (Windows Python) means no HART-comp transport at all, and
        # saying so here keeps the AttributeError out of the connect loop below,
        # where only OSError is caught. The dev host runs the tests; the node
        # runs the compositor.
        if not hasattr(socket, 'AF_UNIX'):
            return None
        cands = []
        env = os.environ.get('HART_COMP_SOCK')
        if env:
            cands.append(env)
        xdg = os.environ.get('XDG_RUNTIME_DIR')
        if xdg:
            cands.append(os.path.join(xdg, 'hart-comp.sock'))
        for path in cands:
            if cls._call_on(path, 'window.list').get('ok'):
                return path
        return None

    @classmethod
    def _detect_backend(cls) -> Optional[str]:
        """Which window transport this node ACTUALLY has.

        HART-comp is probed FIRST, and by connecting rather than by env var.
        The native Tier-1 session runs no sway at all, yet hart-liquid-ui.nix
        sets ``SWAYSOCK`` unconditionally and ``_is_wayland()`` returns True on
        that alone — so the old sway-only detection reported ``available=True``
        on the tier where the relay has no upstream and EVERY verb failed. That
        inverted this module's honest-failure contract exactly where it matters
        most. Measured on the box 2026-09-07 under Tier-1 hart-comp:
        ``replay_layout`` returned available=true, replayed 0 of 3, every step
        a bare ok=false carrying no reason at all.
        """
        if cls._hart_comp_socket():
            return 'hart-comp'
        try:
            from integrations.agent_engine.shell_desktop_apis import _is_wayland
            if _is_wayland():
                return 'sway'
        except Exception:
            pass
        return None

    @property
    def available(self) -> bool:
        return self._backend is not None

    def _sway(self, args: List[str], timeout=10):
        return _run(['swaymsg'] + args, timeout=timeout)

    # ── HART-comp framed-JSON transport (compositor/IPC_PROTOCOL.md §2) ──
    def _hc(self, method: str, args: Optional[dict] = None) -> Dict[str, Any]:
        """One request, one response, over the compositor's Unix socket.

        The wire is a 4-byte big-endian length then a UTF-8 JSON object, both
        directions (ipc.rs::write_frame). The compositor answers
        ``{v, id, ok, result, error{code,message}}``; this flattens that into
        the ``{'ok': bool, ...}`` shape every caller here already returns, and
        carries the compositor's own error code through rather than replacing
        it with a bare False. ``window.summon`` is deliberately NOT routed here
        (the compositor has no such method); it keeps its honest ``unsupported``
        in ``summon_app``.
        """
        if not self._hc_path:
            return {'ok': False, 'error': 'no hart-comp socket', '_transport': True}
        reply = self._call_on(self._hc_path, method, args)
        if reply.get('_transport'):
            # A TRANSPORT failure, not a refusal. The compositor may have
            # restarted under us, which invalidates the cached path without
            # anything being wrong with the request. Re-probe ONCE and retry so
            # a session restart does not leave the brain permanently blind. A
            # real ``not_found`` never lands here, so a legitimate refusal is
            # never retried into a second dispatch.
            self._hc_path = self._hart_comp_socket()
            if self._hc_path:
                return self._call_on(self._hc_path, method, args)
        return reply

    @classmethod
    def _call_on(cls, path: str, method: str,
                 args: Optional[dict] = None) -> Dict[str, Any]:
        """One framed request/response on an EXPLICIT socket path.

        Detection and dispatch share this, so the probe proves the transport
        with exactly the machinery the real calls use. Nothing here is allowed
        to raise: every caller treats a failure as "no window manager", which
        is the honest reading.
        """
        if not hasattr(socket, 'AF_UNIX'):
            return {'ok': False, 'error': 'no AF_UNIX on this platform',
                    '_transport': True}
        body = json.dumps({'id': 'brain', 'method': method,
                           'args': args or {}}).encode('utf-8')
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(_HC_TIMEOUT)
            s.connect(path)
            s.sendall(struct.pack('>I', len(body)) + body)
            head = cls._recv_exactly(s, 4)
            if head is None:
                # Exactly what a socket-activated relay does when it cannot find
                # an upstream: accept, then exit without answering.
                return {'ok': False, 'error': 'no response frame from hart-comp',
                        '_transport': True}
            (length,) = struct.unpack('>I', head)
            if length > _HC_MAX_FRAME:
                return {'ok': False, '_transport': True,
                        'error': 'hart-comp frame too large: %d' % length}
            payload = cls._recv_exactly(s, length)
            if payload is None:
                return {'ok': False, 'error': 'truncated hart-comp frame',
                        '_transport': True}
            reply = json.loads(payload.decode('utf-8'))
        except (OSError, ValueError) as e:
            return {'ok': False, 'error': 'hart-comp call failed: %s' % e,
                    '_transport': True}
        finally:
            try:
                s.close()
            except OSError:
                pass
        if reply.get('ok'):
            out = {'ok': True}
            result = reply.get('result')
            if isinstance(result, dict):
                out.update(result)
            return out
        err = reply.get('error') or {}
        return {'ok': False,
                'error': err.get('code') or 'error',
                'message': err.get('message') or ''}

    @staticmethod
    def _recv_exactly(sock, n: int) -> Optional[bytes]:
        """Exactly ``n`` bytes, or None if the peer closed first. A short read
        is normal on a stream socket; treating one as the whole frame is how
        framed protocols silently corrupt."""
        buf = b''
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    # ── read (un-gated) ──
    def list_windows(self) -> List[Dict[str, Any]]:
        """Real toplevels (id/app_id/name/focused/rect). Empty when no
        compositor is present (cage Tier-3 — the brain feature-detects).

        ONE output shape whichever transport answered. HART-comp speaks
        handle/title/geometry where sway speaks id/name/rect, so its records
        are mapped onto the shape callers already consume, keeping its extra
        truth (workspace, visible, kind, mapped) alongside rather than
        discarding it. Both sources obey the same honesty rule: a window
        appears only because it really mapped.
        """
        if self._backend == 'hart-comp':
            reply = self._hc('window.list')
            if not reply.get('ok'):
                return []
            out = []
            for w in (reply.get('windows') or []):
                geo = w.get('geometry') or {}
                out.append({
                    'id': w.get('handle'),
                    'app_id': w.get('app_id'),
                    'name': w.get('title'),
                    'focused': bool(w.get('focused')),
                    'rect': geo,
                    'workspace': w.get('workspace'),
                    'visible': w.get('visible'),
                    'kind': w.get('kind'),
                })
            return out
        if self._backend != 'sway':
            return []
        r = self._sway(['-t', 'get_tree'])
        if r is None or getattr(r, 'returncode', 1) != 0:
            return []
        try:
            tree = json.loads(r.stdout)
        except Exception:
            return []
        out: List[Dict[str, Any]] = []

        def walk(node):
            wp = node.get('window_properties') or {}
            if node.get('type') in ('con', 'floating_con') and (
                    node.get('app_id') or wp.get('class')):
                out.append({
                    'id': node.get('id'),
                    'app_id': node.get('app_id') or wp.get('class'),
                    'name': node.get('name'),
                    'focused': bool(node.get('focused')),
                    'rect': node.get('rect'),
                })
            for child in (node.get('nodes') or []) + \
                    (node.get('floating_nodes') or []):
                walk(child)
        walk(tree)
        return out

    # ── non-destructive arrange (un-gated) ──
    # Each of these is ONE verb with two transports, never two code paths for
    # the same tier: HART-comp takes a string ``handle`` and framed JSON, sway
    # takes an int ``con_id`` and a command string. The public signature and
    # the returned shape are identical either way, so nothing above this line
    # knows which compositor answered.
    def focus_window(self, con_id: int) -> Dict[str, Any]:
        if self._backend == 'hart-comp':
            return self._hc('window.focus', {'handle': str(con_id)})
        return self._ok(self._sway(['[con_id=%d]' % int(con_id), 'focus']))

    def place_window(self, con_id: int, x: int, y: int,
                     w: int, h: int) -> Dict[str, Any]:
        if self._backend == 'hart-comp':
            return self._hc('window.place', {
                'handle': str(con_id),
                'target': {'x': int(x), 'y': int(y),
                           'w': int(w), 'h': int(h)},
            })
        cmd = ('[con_id=%d] floating enable, move position %d %d, '
               'resize set %d %d' % (int(con_id), int(x), int(y),
                                     int(w), int(h)))
        return self._ok(self._sway([cmd]))

    def tile_layout(self, layout: str) -> Dict[str, Any]:
        # The allowlist below is sway's container-layout vocabulary. HART-comp
        # tiles the whole workspace and names its own arrangements (grid and
        # friends), so validation belongs to whichever backend will execute it:
        # rejecting "grid" for a compositor that implements it would be this
        # client inventing a limit the WM does not have.
        if self._backend == 'hart-comp':
            return self._hc('window.tile', {'layout': str(layout)})
        if layout not in ('splith', 'splitv', 'tabbed', 'stacking'):
            return {'ok': False, 'error': 'bad layout'}
        return self._ok(self._sway(['layout', layout]))

    def move_to_workspace(self, con_id: int, n: int) -> Dict[str, Any]:
        """window.move_to_workspace (IPC §4.7). Non-destructive arrange."""
        if self._backend == 'hart-comp':
            return self._hc('window.move_to_workspace',
                            {'handle': str(con_id), 'workspace': int(n)})
        cmd = '[con_id=%d] move container to workspace number %d' % (
            int(con_id), int(n))
        return self._ok(self._sway([cmd]))

    def switch_workspace(self, n: int) -> Dict[str, Any]:
        """workspace.switch (IPC §4.8) — moves REAL native windows only; the
        shell's hartWorkspaces.js keeps its own client-side panel show/hide on
        every tier (one source of truth per object class). Non-destructive.

        Both transports take the SAME 1-based workspace number on the wire
        (hart-comp converts to its 0-based internal index and echoes 1-based
        back), so a banked layout recipe replays identically on either tier."""
        if self._backend == 'hart-comp':
            return self._hc('workspace.switch', {'workspace': int(n)})
        return self._ok(self._sway(['workspace', 'number', str(int(n))]))

    def summon_app(self, manifest_id: str) -> Dict[str, Any]:
        """window.summon (IPC §4.6) — launch an app by manifest id, then surface
        the result HONESTLY.

        The native-window launch path (ALONGSIDE the iframe panels — additive, the
        panels are untouched): if the app is ALREADY open as a native window the
        brain knows about (AppRegistry's manifest↔handle map, fed by HART-comp's
        real ``window.opened`` map events), return that EXISTING handle — that
        handle came from a real map, so it is not a phantom.

        Otherwise a fresh summon needs to LAUNCH and AWAIT A REAL MAP. On the sway
        Tier-2 shim we can neither tag a launcher's child for map-correlation nor
        await a wlr-foreign-toplevel map event, so we return ``unsupported`` and
        NEVER fabricate a window handle (the no-phantom-windows rule, §1.4/§4.6).
        HART-comp Tier-1 is where SummonApp awaits the real map (the Rust
        ``SummonResolver``/``State::on_real_map`` keyed on a map within
        ``SUMMON_MAP_TIMEOUT``); a banked ``window.summon`` step replayed on REUSE
        therefore surfaces this honest ``unsupported`` rather than a phantom-success
        no-op (§8). Non-destructive."""
        manifest_id = str(manifest_id or '').strip()
        if not manifest_id:
            return {'ok': False, 'error': 'manifest_id required'}
        # Additive native-window path: if HART-comp already told us this manifest
        # is open as a native toplevel (a REAL map), hand back that handle. This is
        # the brain-side WindowRegistry mirror — never a fabricated handle.
        existing = self._native_window_handle(manifest_id)
        if existing:
            return {'ok': True, 'manifest_id': manifest_id,
                    'handle': existing, 'mapped': True, 'reused': True}
        # Fresh summon: feature-detect a map-await launch backend. None exists at
        # Tier-2 (the swaymsg shim can't correlate a launch to a map). HART-comp
        # Tier-1's IPC server is where the launch→await-map wiring lands, keyed on
        # the map event, not an exit code.
        return {'ok': False, 'error': 'unsupported', 'manifest_id': manifest_id,
                'note': 'summon needs HART-comp Tier-1 map-await; Tier-2 shim '
                        'cannot confirm a toplevel mapped (no phantom handle)'}

    @staticmethod
    def _native_window_handle(manifest_id: str) -> Optional[str]:
        """The compositor handle for ``manifest_id`` if it is open as a native
        window (AppRegistry's manifest↔handle map, populated from real HART-comp
        ``window.opened`` events). None if not open or the registry is unavailable
        (e.g. headless node). Read-only — never mints a handle."""
        try:
            from core.platform.registry import get_registry
            reg = get_registry()
            if reg.has('apps'):
                app_registry = reg.get('apps')
                fn = getattr(app_registry, 'window_handle_for', None)
                if callable(fn):
                    return fn(manifest_id)
        except Exception:
            pass
        return None

    # ── DESTRUCTIVE (fail-closed gated + audited) ──
    def close_window(self, con_id: int, agent_id: str) -> Dict[str, Any]:
        # The gate runs BEFORE any transport is chosen, so adding HART-comp
        # cannot become a way around the constitution: a refused close is
        # refused on every tier, and audited either way.
        if not self._guard_destructive('window.close', agent_id, con_id):
            return {'ok': False, 'error': 'refused-by-constitution'}
        if self._backend == 'hart-comp':
            return self._hc('window.close', {'handle': str(con_id)})
        return self._ok(self._sway(['[con_id=%d]' % int(con_id), 'kill']))

    # ── agent/MCP entry point ──
    def dispatch_verb(self, verb: str, args: dict, agent_id: str) -> Dict[str, Any]:
        """Single entry point for agent verbs + MCP co-pilot tools — routes a
        window.* verb to the right method (destructive ones stay fail-closed
        gated). This is what an A2UI window.* component or an MCP tool calls."""
        args = args or {}
        try:
            if verb == 'window.list':
                return {'ok': True, 'windows': self.list_windows()}
            if verb == 'window.focus':
                return self.focus_window(int(args['con_id']))
            if verb == 'window.place':
                return self.place_window(int(args['con_id']), int(args['x']),
                                         int(args['y']), int(args['w']),
                                         int(args['h']))
            if verb == 'window.tile':
                return self.tile_layout(str(args['layout']))
            if verb == 'window.move_to_workspace':
                return self.move_to_workspace(int(args['con_id']),
                                              int(args['workspace']))
            if verb in ('workspace.switch', 'window.switch_workspace'):
                return self.switch_workspace(int(args['workspace']))
            if verb == 'window.summon':
                return self.summon_app(str(args['manifest_id']))
            if verb == 'window.close':
                return self.close_window(int(args['con_id']), agent_id)
        except (KeyError, ValueError, TypeError) as e:
            return {'ok': False, 'error': f'bad args for {verb}: {e}'}
        return {'ok': False, 'error': f'unknown verb: {verb}'}

    # ── helpers ──
    @staticmethod
    def _ok(r) -> Dict[str, Any]:
        """Interpret a swaymsg run. A FAILURE CARRIES ITS REASON.

        This used to return a bare ``{'ok': False}``. On the box under Tier-1
        that made a whole banked layout replay as three anonymous false values,
        and the actual cause (``Unable to receive IPC response`` from a relay
        with no upstream sway) never reached anyone. The reason is the
        difference between a report you can act on and one you cannot.
        """
        if r is not None and getattr(r, 'returncode', 1) == 0:
            return {'ok': True}
        if r is None:
            return {'ok': False, 'error': 'no window-manager transport'}
        detail = (getattr(r, 'stderr', '') or '').strip()
        return {'ok': False,
                'error': detail or 'swaymsg exit %s' % getattr(
                    r, 'returncode', '?')}

    def _guard_destructive(self, verb: str, agent_id: str, target) -> bool:
        """Fail-CLOSED constitutional gate for destructive window ops, recorded
        in the immutable audit log either way."""
        try:
            from security.hive_guardrails import (
                HiveCircuitBreaker, GuardrailEnforcer)
            if HiveCircuitBreaker.is_halted():
                logger.warning("WM %s refused (hive halted): %s", verb, agent_id)
                self._audit(verb, agent_id, target,
                            allowed=False, reason='hive-halted')
                return False
            allowed, reason, _ = GuardrailEnforcer.before_dispatch(verb)
        except Exception as e:
            # A destructive op must NOT proceed if the gate is unreachable.
            logger.error("WM %s blocked — guardrail unavailable: %s", verb, e)
            self._audit(verb, agent_id, target,
                        allowed=False, reason='guardrail-unavailable')
            return False
        self._audit(verb, agent_id, target, allowed=allowed, reason=reason)
        return bool(allowed)

    @staticmethod
    def _audit(verb, agent_id, target, *, allowed, reason):
        try:
            from security.immutable_audit_log import get_audit_log
            get_audit_log().log_event(
                'wm_window_op', actor_id=str(agent_id),
                action=f'{verb} target={target} allowed={allowed} ({reason})',
                detail={'verb': verb, 'allowed': bool(allowed)},
                target_id=str(target))
        except Exception:
            pass


_client: Optional[HartWmClient] = None


def get_wm_client() -> HartWmClient:
    """The brain's singleton WM client, registered in ServiceRegistry so MCP
    co-pilot tools + agent verbs resolve it (mirrors get_liquid_ui)."""
    global _client
    if _client is None:
        _client = HartWmClient()
        try:
            from core.platform.registry import get_registry
            reg = get_registry()
            if not reg.has('HartWmClient'):
                reg.register('HartWmClient', lambda: _client)
        except Exception:
            pass
    return _client
