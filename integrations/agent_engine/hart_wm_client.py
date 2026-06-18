"""HART OS — HartWmClient: the brain's privileged window-manager client (Phase 6).

The AI-native MOAT: agents arrange REAL native windows through ONE gated client —
the thing GNOME/Copilot cannot match (an AI that owns window-PLACEMENT POLICY,
not one that scripts a settings page). For the sway Tier-1 fast path this shims
to `swaymsg` (the degraded-but-present moat); a HART-comp Unix-socket/D-Bus
transport (compositor/IPC_PROTOCOL.md) replaces the shim later — the method
surface here is what both must satisfy brain-side.

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
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.hart_wm')

# Verbs that MUTATE the desktop destructively — fail-CLOSED gated.
DESTRUCTIVE_VERBS = frozenset({'window.close', 'window.fullscreen'})


def _run(cmd, timeout=10):
    """Reuse the shell's subprocess wrapper (DRY); minimal local fallback only
    if that module isn't importable (a non-shell node)."""
    try:
        from integrations.agent_engine.shell_desktop_apis import _run as _shell_run
        return _shell_run(cmd, timeout=timeout)
    except Exception:
        import subprocess
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except Exception:
            return None


class HartWmClient:
    """Brain-side WM client. Tier-1 transport = swaymsg shim; HART-comp later."""

    def __init__(self):
        self._backend = self._detect_backend()

    @staticmethod
    def _detect_backend() -> Optional[str]:
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

    # ── read (un-gated) ──
    def list_windows(self) -> List[Dict[str, Any]]:
        """Real toplevels (id/app_id/name/focused/rect). Empty when no
        compositor is present (cage Tier-3 — the brain feature-detects)."""
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
    def focus_window(self, con_id: int) -> Dict[str, Any]:
        return self._ok(self._sway(['[con_id=%d]' % int(con_id), 'focus']))

    def place_window(self, con_id: int, x: int, y: int,
                     w: int, h: int) -> Dict[str, Any]:
        cmd = ('[con_id=%d] floating enable, move position %d %d, '
               'resize set %d %d' % (int(con_id), int(x), int(y),
                                     int(w), int(h)))
        return self._ok(self._sway([cmd]))

    def tile_layout(self, layout: str) -> Dict[str, Any]:
        if layout not in ('splith', 'splitv', 'tabbed', 'stacking'):
            return {'ok': False, 'error': 'bad layout'}
        return self._ok(self._sway(['layout', layout]))

    def move_to_workspace(self, con_id: int, n: int) -> Dict[str, Any]:
        """window.move_to_workspace (IPC §4.7). Non-destructive arrange."""
        cmd = '[con_id=%d] move container to workspace number %d' % (
            int(con_id), int(n))
        return self._ok(self._sway([cmd]))

    def switch_workspace(self, n: int) -> Dict[str, Any]:
        """workspace.switch (IPC §4.8) — moves REAL native windows only; the
        shell's hartWorkspaces.js keeps its own client-side panel show/hide on
        every tier (one source of truth per object class). Non-destructive."""
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
        if not self._guard_destructive('window.close', agent_id, con_id):
            return {'ok': False, 'error': 'refused-by-constitution'}
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
        return {'ok': r is not None and getattr(r, 'returncode', 1) == 0}

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
