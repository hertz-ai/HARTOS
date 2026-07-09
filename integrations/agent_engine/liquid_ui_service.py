"""
HART OS LiquidUI Service — Glass Desktop Shell.

The desktop IS HART. When you login to HART OS, LiquidUI renders the entire
desktop experience as a fullscreen frosted-glass shell (like explorer.exe):

  - Top bar with clock, notifications, agent status, tray
  - Start menu with all HART panels, apps, files, services, power
  - Floating glass panels — each Nunba page is a draggable/resizable window
  - Agent pill — ambient AI input always floating ("Hey HART, read my mails?")
  - System panels — hardware, security, events, network (rendered natively)

When a model is available:
  - Dashboard explains WHY the GPU is busy, not just the percentage
  - Voice says "your marketing agent finished" instead of beeping
  - Agent helps customize the desktop ("make fonts bigger", "switch theme")

When no model is available, graceful fallback:
  LLM available -> generative UI (best experience)
  No LLM        -> Nunba static panels (React SPA iframes)
  No GUI         -> terminal dashboard (textual TUI)
  Edge/headless  -> Conky metrics only

Multi-modal output:
  Screen  -> WebKit2 (GTK), fullscreen glass shell
  Voice   -> TTS via Model Bus -> PipeWire -> speaker
  Terminal -> Rich TUI (textual library)
  Haptic  -> Vibration patterns (phone, via Android bridge)
"""
import copy
import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.liquid_ui')

# Verbs that MUTATE the desktop (window.close, fullscreen takeover, …) — added
# in Phase 6.  These pass a fail-CLOSED guardrail; benign display cards do not
# (and must not risk the prompt gate's false-positives).
DESTRUCTIVE_COMPONENT_TYPES = frozenset()

# Obvious XSS vectors we REJECT server-side.  The client also escapes on render,
# so we reject (not escape) to avoid double-escaping legitimate content.
_A2UI_XSS_RE = re.compile(
    r'<\s*(?:script|iframe|img|svg|object|embed)\b'  # script/iframe + tags that carry onerror/onload
    r'|<[^>]*\son\w+\s*='                            # inline event-handler attr (onerror=/onload=/onclick=...)
    r'|javascript:'
    r'|data:text/html',
    re.I)


def _a2ui_has_xss(value) -> bool:
    """True if any nested string in the component carries an XSS vector."""
    if isinstance(value, str):
        return bool(_A2UI_XSS_RE.search(value))
    if isinstance(value, dict):
        return any(_a2ui_has_xss(v) for v in value.values())
    if isinstance(value, list):
        return any(_a2ui_has_xss(v) for v in value)
    return False


# ── GPU render verdict (#137 — reduced effects on software render) ───────────
# hart-gpu-probe (nixos/modules/hart-gpu-probe.nix) runs a boot-time GL smoke
# test BEFORE the display manager and writes a one-line verdict to
# /run/hart/gpu-render: `hardware` (the GPU created a real GL context) or
# `software` (forced llvmpipe/cairo — probe failed / disabled / timed out).
# The GTK4 layer-shell host already reads this SAME file to pick its GSK
# renderer; the desktop-shell render reads it too so the CSS can SHED the
# GPU-only cinematic (backdrop blur, layered shadows, continuous animations)
# when the shell is CPU-composited — that compositing is exactly what makes a
# keystroke lag ~500ms and pegs a core on real software-rendered hardware.
# REUSE the probe's verdict; do NOT invent a second probe.
_GPU_RENDER_VERDICT_FILE = '/run/hart/gpu-render'


def read_gpu_render_mode() -> str:
    """Return 'hardware' or 'software' from the hart-gpu-probe verdict file.

    Best-effort + fail-SOFTWARE: a missing/unreadable file or any value that is
    not exactly `hardware` yields 'software' (the safe floor = reduced effects),
    mirroring the probe's own fail-safe contract. Never raises."""
    try:
        with open(_GPU_RENDER_VERDICT_FILE, 'r') as f:
            verdict = (f.read() or '').strip().lower()
        return 'hardware' if verdict == 'hardware' else 'software'
    except (FileNotFoundError, PermissionError, OSError):
        return 'software'


# ── Default-sink volume probe (wpctl-first, pactl fallback) ──────────────────
# Module-level (NOT a route closure) so the background connectivity prober and
# the volume write routes share ONE implementation — no parallel volume path.
# Every call is subprocess.run with a bounded timeout and degrades to
# {'available': False} when neither wpctl nor pactl is present (the live USB may
# have neither); never crashes, never hangs.
def _vol_run(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _volume_get(timeout=4):
    # wpctl get-volume @DEFAULT_AUDIO_SINK@ -> "Volume: 0.55 [MUTED]"
    r = _vol_run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'], timeout=timeout)
    if r and r.returncode == 0 and 'Volume:' in r.stdout:
        try:
            frac = float(r.stdout.split('Volume:')[1].strip().split()[0])
            return {'available': True, 'tool': 'wpctl',
                    'volume': int(round(frac * 100)),
                    'muted': 'MUTED' in r.stdout.upper()}
        except (ValueError, IndexError):
            pass
    # pactl fallback
    mr = _vol_run(['pactl', 'get-sink-mute', '@DEFAULT_SINK@'], timeout=timeout)
    vr = _vol_run(['pactl', 'get-sink-volume', '@DEFAULT_SINK@'], timeout=timeout)
    if vr and vr.returncode == 0 and '%' in vr.stdout:
        try:
            pct = int(vr.stdout.split('/')[1].strip().rstrip('%'))
            muted = bool(mr and mr.returncode == 0 and
                         'yes' in mr.stdout.lower())
            return {'available': True, 'tool': 'pactl',
                    'volume': max(0, min(150, pct)), 'muted': muted}
        except (ValueError, IndexError):
            pass
    return {'available': False, 'volume': None, 'muted': None}


# ── Background connectivity prober + snapshot cache (CAUSE 1) ─────────────────
class _ConnectivityCache:
    """One daemon thread keeps a connectivity snapshot fresh; the request
    handlers read the cache INSTANTLY (no subprocess on the request path).

    hartConnectivity.js polls /api/shell/connectivity/summary every ~8s (plus on
    popover-open and on toggle) and /api/shell/network/wifi on open + every
    Rescan. The old handlers ran up to SIX synchronous 4s subprocess.run calls
    (nmcli x2, bluetoothctl x2, wpctl/pactl) in series ON the waitress request
    thread. On a software-rendered lite box (threads=1-2) the pool saturated and
    EVERY shell fetch queued behind it — the click-wifi / drag freeze. Aborting
    the JS fetch never cancelled the server subprocess.

    Fix: probe on ONE dedicated daemon thread on a ~9s cadence with SHORT (1.2s)
    per-tool timeouts, skipping tools already found absent. A single thread means
    probes are inherently debounced — they never overlap. Fail-safe: a probe
    error keeps the previous good cache; an unprimed cache reads
    'available': False everywhere, never a crash. The quick-settings WRITE
    actions (scan/connect/toggle/set-volume) still hit the per-domain endpoints
    inline; this is read-only aggregation, NOT a parallel control path.
    """

    REFRESH_INTERVAL_S = 9.0
    PROBE_TIMEOUT_S = 1.2

    def __init__(self):
        self._lock = threading.Lock()
        self._summary = self._empty_summary()
        self._wifi = {'networks': [], 'connected': {}}
        self._absent = set()  # tool names that raised FileNotFoundError once
        self._running = False

    @staticmethod
    def _empty_summary():
        return {
            'wifi': {'available': False, 'enabled': False, 'connected': False,
                     'ssid': None, 'signal': None, 'blocked': None},
            'bluetooth': {'available': False, 'powered': False,
                          'connected_count': 0},
            'battery': {'available': False, 'percent': None,
                        'plugged_in': False, 'state': 'unknown'},
            'volume': {'available': False, 'volume': None, 'muted': None},
        }

    def _run(self, cmd):
        """subprocess.run with the SHORT probe timeout. Records a tool the moment
        it raises FileNotFoundError and skips it forever after, so a known-absent
        tool never costs a spawn again. Returns None on any failure."""
        tool = cmd[0]
        if tool in self._absent:
            return None
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.PROBE_TIMEOUT_S)
        except FileNotFoundError:
            self._absent.add(tool)
            return None
        except (subprocess.TimeoutExpired, OSError):
            return None

    @staticmethod
    def _read_rfkill_flag(path):
        """Read a 0/1 rfkill sysfs flag. Returns 1/0, or None if unreadable."""
        try:
            with open(path) as f:
                v = f.read().strip()
            return 1 if v == '1' else 0
        except (OSError, ValueError):
            return None

    def _probe_rfkill_wifi(self, rfkill_dir='/sys/class/rfkill'):
        """Read the kernel rfkill state for the wifi radio from sysfs — a pure
        file read (NO subprocess, so it can never hang and never needs a tool on
        PATH). This is what lets a SOFT-BLOCK be told apart from "no hardware":

            'hard'    - a physical/BIOS kill switch is engaged. The CHIP IS
                        PRESENT but the radio is off in hardware (a software
                        toggle cannot turn it back on).
            'soft'    - a software block (airplane mode / `nmcli radio wifi off`).
                        The CHIP IS PRESENT and re-enableable in software.
            'none'    - a wlan rfkill entry exists and is unblocked (radio on).
            'absent'  - the rfkill subsystem is present but there is NO wlan
                        entry => the wifi chip is NOT enumerated (missing driver/
                        firmware, or simply no wifi hardware). THE honest
                        "hardware not detected" signal.
            'unknown' - /sys/class/rfkill is missing/unreadable (e.g. a container
                        or a VM without the rfkill subsystem) => cannot tell from
                        rfkill; the caller falls back to NetworkManager signals.

        'hard'/'soft'/'none' all PROVE the radio hardware is enumerated, so the
        UI can say "blocked" instead of mis-reporting "no hardware".
        """
        if not os.path.isdir(rfkill_dir):
            return 'unknown'
        try:
            entries = os.listdir(rfkill_dir)
        except OSError:
            return 'unknown'
        state = 'absent'   # subsystem present; a wlan entry downgrades this below
        for name in entries:
            base = os.path.join(rfkill_dir, name)
            try:
                with open(os.path.join(base, 'type')) as f:
                    if f.read().strip() != 'wlan':
                        continue
            except OSError:
                continue
            # A wlan rfkill entry exists => the wifi chip IS present.
            if self._read_rfkill_flag(os.path.join(base, 'hard')) == 1:
                return 'hard'          # hard block is the most restrictive — wins
            if self._read_rfkill_flag(os.path.join(base, 'soft')) == 1:
                state = 'soft'
            elif state != 'soft':
                state = 'none'
        return state

    def _probe_wifi(self):
        wifi = {'available': False, 'enabled': False, 'connected': False,
                'ssid': None, 'signal': None, 'blocked': None}

        # rfkill (sysfs) is the AUTHORITATIVE presence + block signal. NM's
        # `radio wifi` reports the SOFTWARE toggle and stays "enabled" even with
        # ZERO wifi devices, so on its own it falsely claims "available" on a box
        # with no wifi chip. rfkill tells present-vs-absent and soft-vs-hard block.
        rf = self._probe_rfkill_wifi()
        if rf in ('hard', 'soft', 'none'):
            wifi['available'] = True            # a wlan rfkill entry == chip present
            if rf in ('hard', 'soft'):
                wifi['blocked'] = rf            # distinct from "no hardware"
        # rf == 'absent' => rfkill present, no wlan entry => NO wifi chip. Leave
        # available False so the UI honestly says "hardware not detected", and
        # never let the NM fallbacks below flip it back to True.

        r = self._run(['nmcli', 'radio', 'wifi'])
        if r and r.returncode == 0:
            wifi['enabled'] = r.stdout.strip().lower() == 'enabled'
            # Fallback presence ONLY when rfkill could not tell us (no
            # /sys/class/rfkill, e.g. a container/VM) — never override a definitive
            # 'absent'. NM-down (rc != 0 / nmcli missing) is NOT "no hardware":
            # rfkill above already decides presence in that case.
            if rf == 'unknown':
                wifi['available'] = True

        r = self._run(['nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL',
                       'device', 'wifi'])
        if r and r.returncode == 0:
            # nmcli returns rc 0 here only when a wifi device exists (it errors
            # "No Wi-Fi device found" otherwise), so rc 0 corroborates presence —
            # but never override a definitive rfkill 'absent'.
            if rf != 'absent':
                wifi['available'] = True
            for line in r.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and parts[0] == 'yes':
                    wifi['connected'] = True
                    wifi['ssid'] = parts[1] or None
                    if len(parts) >= 3 and parts[2].isdigit():
                        wifi['signal'] = int(parts[2])
                    break
        return wifi

    def _probe_bluetooth(self):
        bt = {'available': False, 'powered': False, 'connected_count': 0}
        r = self._run(['bluetoothctl', 'show'])
        if r and r.returncode == 0:
            bt['available'] = True
            for line in r.stdout.split('\n'):
                if 'Powered:' in line:
                    bt['powered'] = 'yes' in line.lower()
                    break
        if bt['powered']:
            r = self._run(['bluetoothctl', 'devices', 'Connected'])
            if r and r.returncode == 0:
                bt['connected_count'] = len(
                    [ln for ln in r.stdout.strip().split('\n')
                     if ln.strip().startswith('Device')])
        return bt

    def _probe_battery(self):
        # psutil + sysfs (the canonical cross-platform path). No subprocess.
        battery = {'available': False, 'percent': None,
                   'plugged_in': False, 'state': 'unknown'}
        try:
            import psutil
            b = psutil.sensors_battery()
            if b is not None:
                battery['available'] = True
                battery['percent'] = int(round(b.percent))
                battery['plugged_in'] = bool(b.power_plugged)
                battery['state'] = ('charging' if b.power_plugged
                                    else 'discharging')
        except (ImportError, RuntimeError, OSError):
            pass
        if not battery['available']:
            try:
                import glob as _g
                bats = sorted(_g.glob('/sys/class/power_supply/BAT*'))
                if bats:
                    d = bats[0]
                    try:
                        with open(d + '/capacity') as f:
                            cap = f.read().strip()
                        if cap.isdigit():
                            battery['available'] = True
                            battery['percent'] = int(cap)
                    except (OSError, ValueError):
                        pass
                    try:
                        with open(d + '/status') as f:
                            st = f.read().strip().lower()
                        if st:
                            battery['state'] = st
                            battery['plugged_in'] = st in ('charging', 'full')
                    except OSError:
                        pass
            except OSError:
                pass
        return battery

    def _probe_wifi_list(self):
        networks = []
        connected = {}
        r = self._run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,ACTIVE',
                       'device', 'wifi', 'list'])
        if r and r.returncode == 0:
            for line in r.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 4 and parts[0]:
                    try:
                        net = {'ssid': parts[0], 'signal': int(parts[1] or 0),
                               'security': parts[2], 'active': parts[3] == 'yes'}
                    except ValueError:
                        continue
                    networks.append(net)
                    if net['active']:
                        connected = net
        r = self._run(['hostname', '-I'])
        if r and r.returncode == 0 and r.stdout.strip():
            connected['ip'] = r.stdout.strip().split()[0]
        return {'networks': networks[:20], 'connected': connected}

    def refresh(self):
        """Probe every domain once and atomically swap the cached snapshots.
        Safe to call from the daemon loop OR directly (tests). Builds fresh dicts
        and replaces the references under the lock — never mutates a dict a reader
        may be holding."""
        summary = {
            'wifi': self._probe_wifi(),
            'bluetooth': self._probe_bluetooth(),
            'battery': self._probe_battery(),
            'volume': _volume_get(timeout=self.PROBE_TIMEOUT_S),
        }
        wifi_list = self._probe_wifi_list()
        with self._lock:
            self._summary = summary
            self._wifi = wifi_list

    def summary(self):
        with self._lock:
            return copy.deepcopy(self._summary)

    def wifi_networks(self):
        with self._lock:
            return copy.deepcopy(self._wifi)

    def start(self):
        """Start the background prober once (idempotent). Spawns a daemon thread
        only — it never probes on the calling thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._loop, name='hart-connectivity-cache',
                         daemon=True).start()

    def _loop(self):
        while self._running:
            try:
                self.refresh()
            except Exception:
                pass
            time.sleep(self.REFRESH_INTERVAL_S)


# One process-wide prober, lazy-started (idempotently) by the connectivity
# request handlers on the first poll — so a process that builds the app but never
# polls (and unrelated tests) never spawns the daemon, and the start works in
# BOTH serve_forever() and the co-located Nunba bundle (the WebView polls either
# way).
_connectivity_cache = _ConnectivityCache()


# ═══════════════════════════════════════════════════════════════
# UI Component Schema (A2UI protocol)
# ═══════════════════════════════════════════════════════════════

COMPONENT_TYPES = {
    'card': {'props': ['title', 'content', 'icon', 'actions']},
    'list': {'props': ['items', 'ordered', 'interactive']},
    'form': {'props': ['fields', 'submit_label', 'action']},
    'chart': {'props': ['type', 'data', 'labels', 'title']},
    'progress': {'props': ['value', 'max', 'label', 'color']},
    'notification': {'props': ['title', 'message', 'severity', 'actions']},
    'approval': {'props': ['agent_id', 'action', 'description', 'options']},
    'code': {'props': ['language', 'content', 'filename']},
    'markdown': {'props': ['content']},
    'media': {'props': ['type', 'src', 'alt', 'controls']},
    'metric': {'props': ['label', 'value', 'unit', 'trend', 'explanation']},
    'layout': {'props': ['type', 'children', 'gap']},
    # ── Ecommerce / Agent Action Live Fragments ──
    'product_card': {'props': ['name', 'price', 'image', 'rating', 'description',
                               'buy_action', 'compare_action']},
    'cart': {'props': ['items', 'total', 'currency', 'checkout_action']},
    'checkout': {'props': ['items', 'total', 'payment_methods', 'shipping_options',
                           'confirm_action']},
    'payment_status': {'props': ['status', 'amount', 'method', 'transaction_id']},
    'order_tracking': {'props': ['order_id', 'status', 'steps', 'eta']},
    'comparison': {'props': ['apps', 'features', 'winner']},
    'agent_action': {'props': ['agent_id', 'action_type', 'description',
                               'status', 'result', 'timestamp']},
    'navigate': {'props': ['target', 'params', 'transition']},
    # ── External-room copilot (UNIF-G5) ──
    # Live transcript + decisions + action items for an external Discord
    # audio room / Teams meet / WhatsApp group voice / Reddit voice room
    # joined via UNIF-G2 Join_External_Room.  Idempotent overwrite — backend
    # emits the FULL state on every transcript chunk; frontend replaces.
    'meet_copilot': {'props': ['call_id', 'platform', 'room_id', 'state',
                               'transcript_lines', 'decisions',
                               'action_items', 'participants',
                               'agent_role']},
    # ── Device pairing QR (used by hart_intelligence_entry) ──
    # WAS missing from allowlist while the emit site + web QRPairOverlay
    # renderer both existed — emits were silently rejected here.  Added
    # 2026-05-14 after probe_liquid_ui_audit found the gap.
    'qr_pair': {'props': ['url', 'caption', 'expires_in_seconds',
                          'session_id']},
    # ── OAuth deep-link prompt ──
    # hart_intelligence_entry emits when a tool needs an OAuth handshake
    # (e.g. Reddit/Discord/Google sign-in).  Frontend renders as a
    # notification with a single navigate-to-external-URL action.
    'oauth_link': {'props': ['title', 'provider', 'authorize_url',
                             'description', 'scopes']},
    # ── Transient toast (channels/agent_tools success/error feedback) ──
    # Lightweight notification with short auto-dismiss; web maps to the
    # same NotificationCard renderer with `severity` driving the colour.
    'toast': {'props': ['title', 'message', 'severity']},
    # ── Channel pair-code consent card (gateway_qr auth_method) ──
    # Emitted by hart_intelligence_entry._start_gateway_qr_pair_push
    # while a user is conversationally connecting WhatsApp / Telegram /
    # etc.  AgentOverlay.jsx already has the PairCodeOverlay renderer
    # (auto-clipboard + countdown + Copy/Open).  Allowlisted here so
    # the LiquidUI validator stops silently dropping the card on the
    # desktop shell.  Added 2026-05-26 after the consent-fanout audit
    # (memory/consent_fanout_p0_p3_plan.md, P0-A).
    'pair_code': {'props': ['channel', 'channel_type', 'display_name',
                            'color', 'icon', 'code', 'expires_in',
                            'clipboard_payload', 'deeplink',
                            'instructions']},
    # ── Channel connected success card ──
    # Sibling of pair_code; rendered as a brief success toast once the
    # gateway confirms authentication.  Self-dismisses after 6s on web.
    'channel_connected': {'props': ['channel', 'display_name', 'color',
                                    'message']},
    # ── App installed → desktop icon (NixOS-style: install an app, its icon
    # appears) ── Emitted by app_installer._auto_register_app on a successful
    # install.  The desktop (hartDesktop.js) merges {id,title,icon,exec} into
    # window.MANIFEST and auto-pins via the EXISTING hartPinIcon, so the new
    # app's icon shows up live without a refresh.
    'app_installed': {'props': ['id', 'title', 'icon', 'exec', 'group',
                                'platform']},
    # ── Agentic HOME composition (the local LLM paints the Netflix home) ──
    # The ONE agentic home feed: `compose_home` pushes a {hero, rows} payload
    # through this type; the SSE consumer routes it to HartHome.compose ->
    # render (hartHome.js).  Without this allowlist entry agent_ui_update would
    # reject the push (unknown type) and the SSE consumer branch stays a dead
    # consumer with no feed.  `home_compose` is the canonical type; `home` is a
    # back-compat alias the consumer also accepts (so neither is silently
    # dropped).  hartHome's samplePayload() stays the offline skeleton; an
    # accepted push overrides it live.
    'home_compose': {'props': ['hero', 'rows']},
    'home': {'props': ['hero', 'rows']},
}

# ── Agentic HOME composition — the producer's schema allow-sets ──────────────
# compose_home pushes a {hero, rows} payload; hartHome.js (compose -> render) is
# the renderer.  These sets are the SINGLE server-side source of truth the
# producer validates a (deterministic OR LLM-authored) composition against, so a
# hallucinated row can never inject an unknown accent, a non-existent click verb,
# or a deep-link to a panel that does not exist.  They mirror hartHome.js's brand
# spectrum + the NAV_MAP / PANEL ids exactly — keep them in lockstep, do NOT fork
# a second palette.  cards hydrate their own imagery client-side (hartHome
# makeCard: card.image | card.image_url -> the /api/media cache | else a local
# media-index search by card.topic|title), so the producer only needs to supply
# good titles/topics + an optional real web image_url; it never embeds bytes.
HOME_ROW_ACCENTS = ('teal', 'cyan', 'blue', 'violet', 'magenta', 'amber')
HOME_CARD_ACTIONS = ('ask', 'open', 'resume')
# Panel ids a row "See all" / a card may deep-link to.  Subset of PANEL_MANIFEST
# + NAV_MAP + SYSTEM_PANELS ids that already exist (an unknown target opens an
# empty panel, so the producer is restricted to these).
HOME_PANEL_TARGETS = (
    'agents_browse', 'communities', 'recipes', 'app_store',
    'resonance', 'user_accounts',
)

# ═══════════════════════════════════════════════════════════════
# Context Engine
# ═══════════════════════════════════════════════════════════════


class ContextEngine:
    """Aggregates context signals for UI generation."""

    def __init__(self, backend_port: int = 6777, model_bus_port: int = 6790):
        self.backend_port = backend_port
        self.model_bus_port = model_bus_port
        self._cache: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def get_context(self) -> Dict[str, Any]:
        """Aggregate all context signals."""
        context = {
            'timestamp': time.time(),
            'device': self._get_device_context(),
            'models': self._get_model_context(),
            'agents': self._get_agent_context(),
            'system': self._get_system_context(),
        }
        with self._lock:
            self._cache = context
        return context

    def _get_device_context(self) -> dict:
        data_dir = os.environ.get('HEVOLVE_DATA_DIR', '/var/lib/hart')
        context = {
            'variant': 'unknown',
            'tier': 'unknown',
            'hostname': os.uname().nodename if hasattr(os, 'uname') else 'unknown',
        }
        try:
            variant_file = '/etc/hart/variant'
            if os.path.exists(variant_file):
                with open(variant_file) as f:
                    context['variant'] = f.read().strip()
        except Exception:
            pass
        try:
            tier_file = os.path.join(data_dir, 'capability_tier')
            if os.path.exists(tier_file):
                with open(tier_file) as f:
                    context['tier'] = f.read().strip()
        except Exception:
            pass
        import datetime
        now = datetime.datetime.now()
        context['hour'] = now.hour
        context['time_of_day'] = (
            'morning' if 5 <= now.hour < 12 else
            'afternoon' if 12 <= now.hour < 17 else
            'evening' if 17 <= now.hour < 22 else 'night'
        )
        context['day_of_week'] = now.strftime('%A')
        return context

    def _get_model_context(self) -> dict:
        from core.http_pool import pooled_get
        try:
            resp = pooled_get(
                f'http://localhost:{self.model_bus_port}/v1/models', timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                return {'available': True, 'models': data.get('models', []),
                        'count': len(data.get('models', []))}
        except Exception:
            pass
        return {'available': False, 'models': [], 'count': 0}

    def _get_agent_context(self) -> dict:
        from core.http_pool import pooled_get
        try:
            resp = pooled_get(
                f'http://localhost:{self.backend_port}/api/social/dashboard/agents',
                timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                agents = data.get('agents', [])
                return {
                    'running': len([a for a in agents if a.get('status') == 'running']),
                    'total': len(agents), 'agents': agents[:5],
                }
        except Exception:
            pass
        return {'running': 0, 'total': 0, 'agents': []}

    def _get_system_context(self) -> dict:
        context = {}
        try:
            with open('/proc/loadavg') as f:
                parts = f.read().split()
                context['load_1m'] = float(parts[0])
                context['load_5m'] = float(parts[1])
        except Exception:
            pass
        try:
            with open('/proc/meminfo') as f:
                mem = {}
                for line in f:
                    key, val = line.split(':')
                    mem[key.strip()] = int(val.strip().split()[0])
                total = mem.get('MemTotal', 1)
                available = mem.get('MemAvailable', 0)
                context['memory_used_percent'] = round(
                    (1 - available / total) * 100, 1)
        except Exception:
            pass
        try:
            with open('/proc/uptime') as f:
                context['uptime_hours'] = round(
                    float(f.read().split()[0]) / 3600, 1)
        except Exception:
            pass
        return context


def _resolve_shell_pool_threads(tier):
    """How many waitress worker threads the glass shell serves on, by HW tier.

    Why this is a FLOOR and not 1-2 (mid-session freeze RCA): waitress is a
    thread-PER-connection server. The shell holds at least one LONG-LIVED
    connection for the session — the ``/api/notifications/stream`` SSE
    (EventSource opened on load when not in potato mode) runs ``while True``
    and never returns, so it permanently OWNS one worker thread. The log-viewer
    SSE takes another whenever that panel is open. On top of that, several
    request handlers BLOCK for seconds and cannot be made non-blocking: the
    ``/api/agent/ask`` chat proxy waits up to 30s on the brain's ``/chat`` (it
    is gated on the single local-LLM slot), and the panel-open routes shell out
    to nmcli / pactl / journalctl for several seconds each.

    With a 1-2 thread pool, a SINGLE persistent SSE plus one blocking request
    leaves zero threads to serve the polls (connectivity 8s, metrics 4s,
    senses 4s) and every other interaction — the whole desktop freezes mid
    session. Idle waitress threads are nearly free (a parked thread blocked in
    recv), so the fix is to size the pool to absorb: persistent SSE(s) + one
    long chat + the steady poll cadence + a panel-open subprocess, with
    headroom. This complements (does not replace) the connectivity-cache fix
    that already took subprocess work OFF the polled request paths.
    """
    if tier in ('embedded', 'observer'):
        return 6
    if tier == 'lite':
        return 8
    return 12


# ═══════════════════════════════════════════════════════════════
# LiquidUI Service — Glass Desktop Shell
# ═══════════════════════════════════════════════════════════════


class LiquidUIService:
    """Glass desktop shell — the OS desktop itself."""

    def __init__(
        self,
        port: int = 6800,
        renderer: str = 'webkit',
        theme: str = 'auto',
        voice_enabled: bool = True,
        haptic_enabled: bool = False,
        context_refresh_ms: int = 2000,
        a2ui_enabled: bool = True,
        model_bus_port: int = 6790,
        backend_port: int = 6777,
    ):
        self.port = port
        self.renderer = renderer
        self.theme = theme
        self.voice_enabled = voice_enabled
        self.haptic_enabled = haptic_enabled
        self.context_refresh_ms = context_refresh_ms
        self.a2ui_enabled = a2ui_enabled
        self.model_bus_port = model_bus_port
        self.backend_port = backend_port

        self.context_engine = ContextEngine(backend_port, model_bus_port)
        self._agent_components: Dict[str, List[dict]] = {}
        self._a2ui_buckets: Dict[str, tuple] = {}   # agent_id -> (tokens, ts)
        self._lock = threading.Lock()
        self._running = False
        self._model_available = False

        # Session state (panel positions restored on login)
        self._data_dir = os.environ.get(
            'HEVOLVE_DATA_DIR', os.environ.get(
                'HART_DATA_DIR',
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))),
                    'agent_data')))

        logger.info(
            "LiquidUIService initialized: port=%d, renderer=%s, "
            "voice=%s, haptic=%s", port, renderer, voice_enabled, haptic_enabled)

    # ─── UI Generation (preserved) ────────────────────────────

    def generate_ui(self, context: Optional[dict] = None) -> Dict[str, Any]:
        """Generate adaptive UI layout based on current context."""
        if context is None:
            context = self.context_engine.get_context()
        if self._model_available:
            return self._generate_ai_ui(context)
        return self._generate_static_ui(context)

    def _generate_ai_ui(self, context: dict) -> dict:
        """Generate UI via LLM (when model is available)."""
        from core.http_pool import pooled_post
        prompt = self._build_ui_prompt(context)
        try:
            resp = pooled_post(
                f'http://localhost:{self.model_bus_port}/v1/chat',
                json={'prompt': prompt, 'max_tokens': 1024}, timeout=15)
            if resp.status_code == 200:
                response = resp.json().get('response', '')
                try:
                    json_str = response
                    if '```json' in json_str:
                        json_str = json_str.split('```json')[1].split('```')[0]
                    elif '```' in json_str:
                        json_str = json_str.split('```')[1].split('```')[0]
                    components = json.loads(json_str)
                    return {
                        'source': 'ai',
                        'components': components if isinstance(components, list) else [components],
                        'context_summary': self._summarize_context(context),
                    }
                except (json.JSONDecodeError, IndexError):
                    return {
                        'source': 'ai_text',
                        'components': [{'type': 'markdown', 'content': response}],
                        'context_summary': self._summarize_context(context),
                    }
        except Exception as e:
            logger.warning("AI UI generation failed: %s", e)
        return self._generate_static_ui(context)

    def _generate_static_ui(self, context: dict) -> dict:
        """Generate static dashboard UI (no LLM needed)."""
        components = []
        system = context.get('system', {})
        components.append({
            'type': 'card', 'title': 'System Status', 'content': '',
            'children': [
                {'type': 'metric', 'label': 'CPU Load',
                 'value': system.get('load_1m', 0), 'unit': '', 'trend': 'stable'},
                {'type': 'metric', 'label': 'Memory',
                 'value': system.get('memory_used_percent', 0), 'unit': '%'},
                {'type': 'metric', 'label': 'Uptime',
                 'value': system.get('uptime_hours', 0), 'unit': 'hours'},
            ],
        })
        agents = context.get('agents', {})
        if agents.get('total', 0) > 0:
            agent_items = [
                f"{a.get('name', '?')}: {a.get('status', '?')}"
                for a in agents.get('agents', [])
            ]
            components.append({
                'type': 'card',
                'title': f"Agents ({agents.get('running', 0)} running)",
                'children': [{'type': 'list', 'items': agent_items}],
            })
        models = context.get('models', {})
        if models.get('available'):
            model_names = [m.get('type', '?') for m in models.get('models', [])]
            components.append({
                'type': 'card',
                'title': f"AI Models ({models.get('count', 0)})",
                'content': ', '.join(model_names) or 'None loaded',
            })
        with self._lock:
            for _aid, comps in self._agent_components.items():
                components.extend(comps)
        return {
            'source': 'static', 'components': components,
            'context_summary': self._summarize_context(context),
        }

    def _build_ui_prompt(self, context: dict) -> str:
        device = context.get('device', {})
        models = context.get('models', {})
        agents = context.get('agents', {})
        system = context.get('system', {})
        return (
            "Generate a JSON array of UI components for a HART OS dashboard.\n\n"
            f"Context:\n"
            f"- Device: {device.get('variant', '?')} variant, {device.get('tier', '?')} tier\n"
            f"- Time: {device.get('time_of_day', '?')} ({device.get('day_of_week', '')})\n"
            f"- System: CPU {system.get('load_1m', 'N/A')}, "
            f"memory {system.get('memory_used_percent', 'N/A')}%, "
            f"uptime {system.get('uptime_hours', 'N/A')}h\n"
            f"- Models: {models.get('count', 0)} available\n"
            f"- Agents: {agents.get('running', 0)}/{agents.get('total', 0)}\n\n"
            "Return ONLY a JSON array. Valid types: card, metric, notification, "
            "list, progress, markdown. Max 5 components. Be concise and insightful."
        )

    def _summarize_context(self, context: dict) -> str:
        device = context.get('device', {})
        models = context.get('models', {})
        agents = context.get('agents', {})
        return (
            f"{device.get('variant', '?')} | {device.get('time_of_day', '?')} | "
            f"{models.get('count', 0)} models | {agents.get('running', 0)} agents"
        )

    # ─── Agent UI Protocol (A2UI) — preserved ─────────────────

    def agent_ui_update(self, agent_id: str, component: dict) -> bool:
        """Push a UI component from an agent to all connected frontends.

        Delivery paths (best-effort once accepted):
          1. In-memory store → polled by SSE stream → Nunba LiquidUI (web)
          2. EventBus → WAMP bridge → Android/iOS React Native via Crossbar
          3. EventBus → any other subscriber (desktop, CLI dashboard)

        Constitutional controls (an agent painting the screen is governed
        like an agent dispatch): the push is REFUSED while the human has
        halted the HiveCircuitBreaker, and every accepted push is recorded
        in the immutable audit log.  Returns False if disabled, the type is
        unknown, or the hive is halted.
        """
        if not self.a2ui_enabled:
            return False
        comp_type = component.get('type', '')
        if comp_type not in COMPONENT_TYPES:
            logger.warning("Invalid A2UI component type: %s", comp_type)
            return False

        # Kill-switch: when the human halts the hive, agent UI pushes stop
        # too — the constitution governs an agent painting the screen exactly
        # like an agent dispatching a goal (dispatch.py:668).  Fail-OPEN if
        # the guardrail module can't be consulted: a benign consent card must
        # never be lost to a guardrail import error.
        try:
            from security.hive_guardrails import HiveCircuitBreaker
            if HiveCircuitBreaker.is_halted():
                logger.warning(
                    "A2UI push refused (hive halted): %s from %s",
                    comp_type, agent_id)
                return False
        except Exception:
            pass

        # Per-agent rate cap (token bucket) — a runaway agent can't flood the
        # desktop with UI pushes.
        if not self._a2ui_rate_ok(agent_id):
            logger.warning("A2UI push rate-capped: %s from %s",
                           comp_type, agent_id)
            return False

        # Destructive verbs (window.close / fullscreen takeover, Phase 6) pass
        # the FULL fail-CLOSED guardrail; benign display cards do not.
        if (comp_type in DESTRUCTIVE_COMPONENT_TYPES
                and not self._a2ui_guardrail_ok(component)):
            return False

        # Server-side defense-in-depth: reject obvious XSS vectors.
        if _a2ui_has_xss(component):
            logger.warning("A2UI push rejected (unsafe content): %s from %s",
                           comp_type, agent_id)
            return False

        import time as _time
        component['_ts'] = _time.time()
        component['_agent_id'] = agent_id

        # Provable audit trail — every accepted push is recorded exactly like
        # a goal dispatch (dispatch.py:680).  Best-effort: an audit hiccup
        # must not drop a user's card.  Type + agent only (no user payload).
        try:
            from security.immutable_audit_log import get_audit_log
            get_audit_log().log_event(
                'a2ui_push', actor_id=str(agent_id),
                action=f'push {comp_type} component',
                detail={'type': comp_type}, target_id=str(agent_id))
        except Exception:
            pass

        # 1. Store for SSE polling (Nunba web LiquidUI)
        with self._lock:
            if agent_id not in self._agent_components:
                self._agent_components[agent_id] = []
            self._agent_components[agent_id].append(component)
            if len(self._agent_components[agent_id]) > 5:
                self._agent_components[agent_id] = \
                    self._agent_components[agent_id][-5:]

        # 2. Push to EventBus → WAMP → Android/iOS/Desktop
        # The WAMP bridge (core/platform/events.py) auto-publishes to
        # com.hartos.event.agent.ui.update which Android subscribes to
        # via AutobahnConnectionManager. Android renders as floating
        # overlay on top of AbstractChatActivity.
        try:
            from core.platform.events import emit_event
            emit_event('agent.ui.update', {
                'agent_id': agent_id,
                'component': component,
            })
        except Exception:
            pass  # EventBus emission is best-effort

        logger.info("A2UI: agent %s pushed %s component", agent_id, comp_type)
        return True

    def _a2ui_rate_ok(self, agent_id: str) -> bool:
        """Per-agent token bucket (20 burst, +2/s) — a runaway agent cannot
        flood the desktop with UI pushes."""
        now = time.monotonic()
        cap, refill = 20.0, 2.0
        with self._lock:
            tokens, last = self._a2ui_buckets.get(agent_id, (cap, now))
            tokens = min(cap, tokens + (now - last) * refill)
            if tokens < 1.0:
                self._a2ui_buckets[agent_id] = (tokens, now)
                return False
            self._a2ui_buckets[agent_id] = (tokens - 1.0, now)
            return True

    def _a2ui_guardrail_ok(self, component: dict) -> bool:
        """Fail-CLOSED guardrail for DESTRUCTIVE verbs — block if guardrails
        are unavailable (benign cards fail-open; a window mutation must not)."""
        try:
            from security.hive_guardrails import GuardrailEnforcer
            allowed, reason, _ = GuardrailEnforcer.before_dispatch(
                str(component.get('action') or component.get('type') or ''))
            if not allowed:
                logger.warning("A2UI destructive verb blocked: %s", reason)
            return allowed
        except Exception as e:
            logger.error("A2UI guardrail unavailable — blocking destructive "
                         "verb: %s", e)
            return False

    def _compose_intent_result(self, intent_text: str, chat_result: dict) -> bool:
        """M1 — turn a brain /chat decomposition into COMPOSED desktop UI.

        Single responsibility: take what the brain's intent classifier
        (CREATE / REUSE / tool / vision / casual) decided and PAINT it on the
        desktop as an A2UI card pushed through the now-wired ``agent_ui_update``
        channel — instead of only narrating a chat bubble.  The orb/command bar
        thereby becomes an intent COMPOSER, not a launcher.

        No parallel decompose path: ``chat_result`` is the verbatim payload from
        ``/chat`` (response text + intent + Agent_status + prompt_id).  Returns
        True iff a composed component was accepted by ``agent_ui_update`` (False
        when the hive is halted, rate-capped, a2ui disabled, or the reply was
        empty — the bubble still renders in every case).
        """
        reply = (chat_result.get('response')
                 or chat_result.get('error') or '').strip()
        if not reply:
            return False
        status = chat_result.get('Agent_status') or ''
        prompt_id = chat_result.get('prompt_id')
        # The brain's own routing decides the icon: a created/reused agent gets
        # the agent glyph, a plain answer gets the spark.
        icon = 'smart_toy' if (status or prompt_id) else 'auto_awesome'
        title = status or 'HART'
        component = {
            'type': 'card',
            'title': title,
            'icon': icon,
            'content': reply,
            'intent': intent_text,
            'timestamp': time.time(),
        }
        return self.agent_ui_update('desktop_intent', component)

    def agent_request_approval(
        self, agent_id: str, action: str, description: str
    ) -> dict:
        component = {
            'type': 'approval', 'agent_id': agent_id, 'action': action,
            'description': description,
            'options': ['Approve', 'Deny', 'Ask me later'],
            'timestamp': time.time(),
        }
        self.agent_ui_update(agent_id, component)
        return {'status': 'approval_requested', 'component': component}

    def compose_home(self, hero=None, rows=None, agent_id='home_composer'):
        """Push a composed HOME surface through the ONE wired A2UI channel.

        This is the single SERVER-SIDE producer of the agentic home feed: the
        local LLM (or any agent) hands a ``{hero, rows}`` composition and it
        flows agent_ui_update -> SSE -> HartHome.compose -> render (hartHome.js),
        the SAME governed transport every other agent UI push uses (no parallel
        path).  hartHome's ``samplePayload()`` is only the offline skeleton; an
        accepted push overrides it live.

        Single responsibility: build the ``home_compose`` component and delegate
        to ``agent_ui_update`` (which owns the kill-switch / rate-cap / audit /
        XSS gating).  Returns True iff the push was accepted (False when the hive
        is halted, rate-capped, a2ui is disabled, or both fields are empty).
        """
        if hero is None and rows is None:
            return False
        component = {'type': 'home_compose'}
        if hero is not None:
            component['hero'] = hero
        if rows is not None:
            component['rows'] = rows
        return self.agent_ui_update(agent_id, component)

    def compose_home_now(self, reason: str = 'manual') -> bool:
        """PRODUCER: compose the agentic home from live context + the local LLM,
        then push it through the wired feed.

        This is the producer half the agentic-home transport was missing (the
        home had a renderer + a governed channel but nobody ever composed for
        it).  It gathers the SAME surfaces hartHome.js reads (the agent
        dashboard, recipes on disk, the earnings wallet, the hive, system),
        composes a contextual ``{hero, rows}`` — the local LLM (the heart)
        curates the narrative + emphasis over a deterministic backbone so the
        home NEVER breaks when the on-device 4B can't emit clean JSON — and
        hands it to ``compose_home`` -> ``agent_ui_update`` (the ONE governed
        transport: the human kill-switch, the per-agent rate cap, the audit log
        and the XSS reject all live there, so this method adds no new gate and
        no new channel).

        Driven by the autonomous agent daemon when the box is idle
        (``agent_daemon._maybe_compose_home``) so the home stays alive whether
        the user is at the machine or away — the existing daemon, the existing
        feed, no new loop.  Returns True iff the push was accepted.  Never
        raises (a compose fault must never take down the shell or the daemon)."""
        try:
            payload = build_home_payload(
                backend_port=self.backend_port,
                model_bus_port=self.model_bus_port)
        except Exception as e:
            logger.debug("compose_home_now(%s): build failed: %s", reason, e)
            return False
        if not payload:
            return False
        ok = self.compose_home(
            hero=payload.get('hero'), rows=payload.get('rows'),
            agent_id='home_composer')
        if ok:
            logger.info(
                "compose_home_now(%s): pushed %d row(s)%s",
                reason, len(payload.get('rows') or []),
                ' + hero' if payload.get('hero') else '')
        return ok

    # ─── Voice I/O — preserved ────────────────────────────────

    def handle_voice_input(self, audio_path: str) -> dict:
        if not self.voice_enabled:
            return {'error': 'Voice not enabled'}
        from core.http_pool import pooled_post
        try:
            with open(audio_path, 'rb') as f:
                resp = pooled_post(
                    f'http://localhost:{self.model_bus_port}/v1/stt',
                    files={'audio': f}, timeout=30)
                if resp.status_code == 200:
                    text = resp.json().get('text', '')
                    if text:
                        return self._process_voice_command(text)
        except Exception as e:
            logger.warning("Voice input failed: %s", e)
        return {'error': 'Voice recognition failed'}

    def _process_voice_command(self, text: str) -> dict:
        from core.http_pool import pooled_post
        try:
            resp = pooled_post(
                f'http://localhost:{self.model_bus_port}/v1/chat',
                json={
                    'prompt': f'User said: "{text}". What action should the '
                              f'OS take? Respond with JSON: '
                              f'{{"action": "...", "params": {{}}}}',
                }, timeout=15)
            if resp.status_code == 200:
                return {'text': text, 'response': resp.json().get('response', ''),
                        'source': 'voice'}
        except Exception:
            pass
        return {'text': text, 'response': 'Could not process', 'source': 'voice'}

    # ─── Glass Desktop Shell Render ───────────────────────────

    def render_desktop_shell(self) -> str:
        """Render the complete glass desktop shell HTML.

        Auto-detects hardware tier and injects performance mode:
        - Potato/Observer: no blur, no animations, lazy iframes, reduced polling
        - Lite: reduced blur, fast animations
        - Standard+: full glass experience
        """
        try:
            from integrations.agent_engine.theme_service import ThemeService
            css_vars = ThemeService.get_css_variables()
            theme = ThemeService.get_active_theme()
        except Exception:
            css_vars = ':root { --hart-background: #0F0E17; --hart-accent: #00D4AA; --hart-on-accent: #0F0E17; --hart-active: #00e676; --hart-text: #e0e0e0; --hart-glass-bg: rgba(15,14,23,0.65); --hart-glass-border: rgba(0,212,170,0.18); --hart-muted: #78909c; --hart-surface: #1a1a2e; --hart-blur: 20px; --hart-saturation: 180%; --hart-radius: 16px; --hart-panel-opacity: 0.65; --hart-topbar-height: 40px; --hart-icon-size: 20px; --hart-titlebar-height: 32px; --hart-font-family: "JetBrains Mono"; --hart-font-size: 13px; --hart-heading-size: 18px; --hart-font-weight: 400; --hart-heading-weight: 600; --hart-anim-speed: 200ms; --hart-error: #FF6B6B; --hart-caution: #ffab40; --hart-heading: #00D4AA; --hart-surface-hover: #252540; }'
            theme = {}

        # Performance tier detection
        perf = theme.get('performance', {})

        # GPU render verdict (#137) — the hardware signal. Read ONCE here so the
        # JS reduced-effects gate (PERF.potato) and the CSS floor (body.gpu-*)
        # both derive from the SAME probe verdict (no second read, no parallel
        # path). When the shell is software-composited (GSK=cairo/llvmpipe) the
        # cinematic glass re-rasterises on the CPU every frame, so we tag <body>
        # `gpu-software` (hartResponsive.css strips the GPU-only effects from the
        # hot surfaces) AND force the potato tier below.
        gpu_mode = read_gpu_render_mode()  # 'hardware' | 'software'
        gpu_body_class = 'gpu-' + gpu_mode  # gpu-software | gpu-hardware

        # ── Glass-opaque fallback signal (#151 transparent-windows) ───────────────
        # The frosted .glass / .panel surfaces lean on backdrop-filter:blur over a
        # translucent fill. backdrop-filter ONLY paints when WebKit accelerated
        # COMPOSITING is on — which the glass-shell host enables ONLY when
        # hart.liquidUI.preferHardwareGL=true. With it false (the default), the host
        # exports WEBKIT_DISABLE_COMPOSITING_MODE=1 + HardwareAccelerationPolicy.NEVER,
        # so the blur renders NOTHING and a translucent panel reads SEE-THROUGH — the
        # steward's real-HW "windows have a transparent background". This is DECOUPLED
        # from the gpu-probe verdict above: a box whose probe says 'hardware' still has
        # WebKit compositing OFF unless preferHardwareGL is set, so gpu-hardware alone
        # never triggered the opaque fallback. Surface the host's compositing state via
        # LIQUID_UI_PREFER_HW_GL (set from ui.preferHardwareGL in hart-liquid-ui.nix)
        # and tag <body> `webkit-flat` whenever blur will NOT composite, so the CSS
        # floor solidifies the glass. Default '0' = the safe, legible opaque floor
        # (matches preferHardwareGL's default-false), so a bare/dev render is opaque
        # too, never see-through.
        webkit_compositing = os.environ.get('LIQUID_UI_PREFER_HW_GL', '0') == '1'
        # webkit-flat == "backdrop-filter blur won't paint" == solidify the glass.
        flat_body_class = '' if webkit_compositing else ' webkit-flat'

        # Potato (reduced-effects) tier: TRUE when the theme disables blur OR the
        # box is software-rendered. The GPU-only cinematic (backdrop blur, layered
        # shadows, continuous animation, ambient/grain) is exactly what pegs a core
        # and lags a keystroke ~500ms on a software-composited box, so software
        # render must shed it — the same verdict the CSS floor uses, wired so the
        # inline-script PERF.potato + window.HART_PERF.potato engage on real
        # software-render hardware, not just on the theme tier.
        is_potato = perf.get('disable_blur', False) or gpu_mode == 'software'

        # Ambient cinematic glow emission (2026-07-01, degrade-gracefully): the 3
        # drifting brand blooms are the single biggest "looks rich" lever (the
        # mockup paints them). On a SOFTWARE-rendered box we now STILL emit them —
        # hartResponsive.css renders them STATIC (no drift) + low-blur, a one-time
        # raster, so depth survives at ~zero per-frame cost (the floor only sheds
        # the per-FRAME drift/blur/grain). We keep them OFF only for an explicit
        # theme disable_blur on a CAPABLE GPU (the user asked for no blur and the
        # box can otherwise afford the full cinematic, so honour that choice).
        emit_ambient = (not is_potato) or (gpu_mode == 'software')

        # Accessibility state — the SAME live dict the /api/shell/accessibility
        # routes mutate (same process). High-contrast + reduced-motion apply as
        # <html> classes (CSS in the design system). Previously the toggles stored
        # nothing (key mismatch) AND the render never consumed the state; this
        # wires both ends.
        try:
            from integrations.agent_engine.shell_os_apis import get_a11y_settings
            _a11y = get_a11y_settings()
        except Exception:
            _a11y = {}
        a11y_cls = (('a11y-contrast ' if _a11y.get('high_contrast') else '')
                    + ('a11y-rmotion' if _a11y.get('reduced_motion') else '')).strip()
        # font_scale: the shell type is px, so scale the font-size tokens directly
        # via an override emitted after css_vars (later source wins). Clamped.
        try:
            _fs = max(0.8, min(2.0, float(_a11y.get('font_scale', 1.0) or 1.0)))
        except (TypeError, ValueError):
            _fs = 1.0
        a11y_fontscale = ''
        if abs(_fs - 1.0) > 0.01:
            _af_f = theme.get('font', {}) if isinstance(theme, dict) else {}
            _af_sh = theme.get('shell', {}) if isinstance(theme, dict) else {}
            a11y_fontscale = (':root{'
                + '--hart-font-size:' + str(round(_af_f.get('size', 13) * _fs)) + 'px;'
                + '--hart-heading-size:' + str(round(_af_f.get('heading_size', 18) * _fs)) + 'px;'
                + '--hart-icon-size:' + str(round(_af_sh.get('icon_size', 20) * _fs)) + 'px}')

        wallpaper = theme.get('wallpaper', {})
        wp_css = wallpaper.get('value', 'radial-gradient(120% 120% at 18% 0%,rgba(0,212,170,0.07),transparent 50%),radial-gradient(100% 100% at 100% 100%,rgba(22,33,62,0.55),transparent 60%),linear-gradient(135deg,#0F0E17 0%,#1a1a2e 50%,#16213e 100%)')
        if wallpaper.get('type') == 'solid':
            wp_css = wallpaper['value']

        # Living-Glass: emit the active accent as a comma-triple so every glow /
        # ring / selection re-tints when the theme accent changes. Parsed from the
        # SAME accent ThemeService resolves (#308-310). Emitted right after
        # {css_vars} (later source wins) so it overrides any earlier definition.
        # _CSS_LIVING_GLASS reads it via var(--hart-accent-rgb, 0,212,170); this
        # makes the variable real rather than relying only on the teal fallback.
        try:
            _ac = ((theme.get('colors', {}) or {}).get('accent', '00D4AA')
                   or '00D4AA').lstrip('#')
            _ar, _ag, _ab = (int(_ac[0:2], 16), int(_ac[2:4], 16), int(_ac[4:6], 16))
            accent_rgb_css = (':root{--hart-accent-rgb:'
                              + f'{_ar},{_ag},{_ab}' + '}')
        except (ValueError, IndexError, TypeError, AttributeError):
            accent_rgb_css = ':root{--hart-accent-rgb:0,212,170}'

        # Import panel manifest
        try:
            from integrations.agent_engine.shell_manifest import (
                PANEL_MANIFEST, DYNAMIC_PANELS, SYSTEM_PANELS, PANEL_GROUPS,
                with_icon_colors, get_settings_sections, get_pinned_panels)
            # Merge in installed apps (DESKTOP_APP/EXTENSION the app-installer
            # auto-registered) so their desktop icons survive a page refresh:
            # hartDesktop.render() only shows ids present in window.MANIFEST.
            # Same source of truth (AppRegistry.installed_app_manifest) the live
            # install push uses - no parallel manifest - merged BEFORE
            # with_icon_colors so installed apps get the same palette tint.
            panels = dict(PANEL_MANIFEST)
            try:
                from core.platform.registry import get_registry
                _reg = get_registry()
                if _reg.has('apps'):
                    panels.update(_reg.get('apps').installed_app_manifest())
            except Exception:
                pass
            # De-monochrome: stamp a resolved per-app 'color' on every entry from
            # the single-source palette so the JS render paths (start menu, dock,
            # desktop icons, titlebars) all tint with one agreed colour instead
            # of the old single --hart-accent wash.
            # Replace </ with <\/ so the browser HTML parser never sees
            # </script> inside the JSON and prematurely closes the script tag.
            manifest_json = json.dumps(with_icon_colors(panels)).replace('</', '<\\/')
            system_json = json.dumps(with_icon_colors(SYSTEM_PANELS)).replace('</', '<\\/')
            groups_json = json.dumps(PANEL_GROUPS).replace('</', '<\\/')
            # W4 Start-menu pins + Settings aggregator — composition only (lists
            # of EXISTING panel ids). The shell JS resolves each id's metadata
            # from the live MANIFEST/SYSTEM_PANELS above, so no panel is redefined
            # here (single source = shell_manifest).
            settings_sections_json = json.dumps(get_settings_sections()).replace('</', '<\\/')
            pinned_json = json.dumps(get_pinned_panels()).replace('</', '<\\/')
        except Exception:
            manifest_json = '{}'
            system_json = '{}'
            groups_json = '[]'
            settings_sections_json = '[]'
            pinned_json = '[]'

        # CSS animations — defined outside f-string to avoid brace conflicts
        _CSS_SLIDE_IN = '@keyframes slideInRight{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}'
        _CSS_FADE_OUT = '@keyframes fadeOutToast{to{opacity:0;transform:translateX(30px)}}'
        _CSS_PULSE = '@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}'
        _CSS_ANIMATIONS = (
            '@keyframes fadeIn{from{opacity:0;transform:scale(0.95) translateY(10px)}to{opacity:1;transform:scale(1) translateY(0)}}'
            ' .panel{animation:fadeIn var(--hart-anim-speed) ease-out}'
            ' .panel.closing{opacity:0;transform:scale(0.95);transition:opacity 0.2s,transform 0.2s}'
            ' .panel.minimizing{opacity:0;transform:scale(0.8) translateY(20px);transition:opacity 0.15s,transform 0.15s}'
            ' .start-menu{transform:translateY(20px);opacity:0;transition:transform 0.2s ease-out,opacity 0.15s ease-out}'
            ' .start-menu.open{transform:translateY(0);opacity:1}'
        )
        _CSS_NO_ANIMATIONS = '/* animations disabled for performance */ .panel{animation:none}'

        # ── HART Design System (Material Design 3 inspired) ──
        _CSS_DESIGN_SYSTEM = '''
/* ═══ HART Design System ═══ */
/* Content-first · Purposeful motion · 4dp grid */

:root {
  /* Typography tokens */
  --ds-font-body: "Inter", -apple-system, "Segoe UI", Roboto, sans-serif;
  --ds-font-mono: "JetBrains Mono", "Fira Code", monospace;

  /* Spacing scale (4dp grid) */
  --ds-space-0:0px; --ds-space-px:1px;
  --ds-space-1:4px; --ds-space-2:8px; --ds-space-3:12px; --ds-space-4:16px;
  --ds-space-5:20px; --ds-space-6:24px; --ds-space-8:32px; --ds-space-10:40px;
  --ds-space-12:48px; --ds-space-16:64px;

  /* Elevation (Material 3 dark-theme shadows) */
  --ds-elevation-0: none;
  --ds-elevation-1: 0 1px 3px 1px rgba(0,0,0,0.15), 0 1px 2px rgba(0,0,0,0.3);
  --ds-elevation-2: 0 2px 6px 2px rgba(0,0,0,0.15), 0 1px 2px rgba(0,0,0,0.3);
  --ds-elevation-3: 0 4px 8px 3px rgba(0,0,0,0.15), 0 1px 3px rgba(0,0,0,0.3);
  --ds-elevation-4: 0 6px 10px 4px rgba(0,0,0,0.15), 0 2px 3px rgba(0,0,0,0.3);
  --ds-elevation-5: 0 8px 12px 6px rgba(0,0,0,0.15), 0 4px 4px rgba(0,0,0,0.3);

  /* Motion */
  --ds-duration-short: 100ms; --ds-duration-medium: 200ms;
  --ds-duration-long: 350ms; --ds-duration-extra-long: 500ms;
  --ds-ease-standard: cubic-bezier(0.2, 0, 0, 1);
  --ds-ease-decelerate: cubic-bezier(0, 0, 0, 1);
  --ds-ease-accelerate: cubic-bezier(0.3, 0, 1, 1);
  --ds-ease-spring: cubic-bezier(0.175, 0.885, 0.32, 1.275);

  /* Surface tones (elevation tint on dark) */
  --ds-surface-dim: rgba(15,14,23,0.85);
  --ds-surface-1: rgba(255,255,255,0.05);
  --ds-surface-2: rgba(255,255,255,0.08);
  --ds-surface-3: rgba(255,255,255,0.11);
  --ds-surface-4: rgba(255,255,255,0.12);
  --ds-surface-5: rgba(255,255,255,0.14);

  /* State layers */
  --ds-state-hover: rgba(255,255,255,0.08);
  --ds-state-focus: rgba(255,255,255,0.12);
  --ds-state-pressed: rgba(255,255,255,0.16);
  --ds-state-dragged: rgba(255,255,255,0.16);

  /* Border radius scale */
  --ds-radius-xs:4px; --ds-radius-sm:8px; --ds-radius-md:12px;
  --ds-radius-lg:16px; --ds-radius-xl:24px; --ds-radius-full:9999px;

  /* Icon sizes */
  --ds-icon-xs:16px; --ds-icon-sm:20px; --ds-icon-md:24px;
  --ds-icon-lg:32px; --ds-icon-xl:48px;
}

/* ── Body font override: Inter for body, JetBrains Mono for code ── */
html, body { font-family: var(--ds-font-body); line-height: 1.5 }

/* ── Type Scale ── */
.ds-display-lg{font-size:57px;line-height:64px;font-weight:400;letter-spacing:-0.25px}
.ds-display-md{font-size:45px;line-height:52px;font-weight:400}
.ds-display-sm{font-size:36px;line-height:44px;font-weight:400}
.ds-headline-lg{font-size:32px;line-height:40px;font-weight:600}
.ds-headline-md{font-size:28px;line-height:36px;font-weight:600}
.ds-headline-sm{font-size:24px;line-height:32px;font-weight:600}
.ds-title-lg{font-size:22px;line-height:28px;font-weight:500}
.ds-title-md{font-size:16px;line-height:24px;font-weight:500;letter-spacing:0.15px}
.ds-title-sm{font-size:14px;line-height:20px;font-weight:500;letter-spacing:0.1px}
.ds-body-lg{font-size:16px;line-height:24px;font-weight:400;letter-spacing:0.5px}
.ds-body-md{font-size:14px;line-height:20px;font-weight:400;letter-spacing:0.25px}
.ds-body-sm{font-size:12px;line-height:16px;font-weight:400;letter-spacing:0.4px}
.ds-label-lg{font-size:14px;line-height:20px;font-weight:500;letter-spacing:0.1px}
.ds-label-md{font-size:12px;line-height:16px;font-weight:500;letter-spacing:0.5px}
.ds-label-sm{font-size:11px;line-height:16px;font-weight:500;letter-spacing:0.5px}
.ds-mono{font-family:var(--ds-font-mono)}

/* ── Elevation ── */
.ds-elevation-0{box-shadow:var(--ds-elevation-0)}
.ds-elevation-1{box-shadow:var(--ds-elevation-1)}
.ds-elevation-2{box-shadow:var(--ds-elevation-2)}
.ds-elevation-3{box-shadow:var(--ds-elevation-3)}
.ds-elevation-4{box-shadow:var(--ds-elevation-4)}
.ds-elevation-5{box-shadow:var(--ds-elevation-5)}

/* ── Button ── */
.ds-btn{display:inline-flex;align-items:center;justify-content:center;gap:var(--ds-space-2);
  padding:10px var(--ds-space-6);border-radius:var(--ds-radius-full);
  font-family:var(--ds-font-body);font-size:14px;font-weight:500;letter-spacing:0.1px;
  line-height:20px;cursor:pointer;border:none;outline:none;position:relative;overflow:hidden;
  transition:box-shadow var(--ds-duration-medium) var(--ds-ease-standard),
    background var(--ds-duration-short) var(--ds-ease-standard),
    filter var(--ds-duration-short) var(--ds-ease-standard);
  user-select:none;-webkit-tap-highlight-color:transparent}
.ds-btn:focus-visible{outline:2px solid var(--hart-accent);outline-offset:2px}
/* Global keyboard focus ring (a11y). :focus-visible = keyboard-only (mouse clicks
   draw no ring), matching Win11/macOS. The shell chrome controls had NO focus
   style at all, so keyboard users couldn't see where they were. */
:focus-visible{outline:2px solid var(--hart-accent);outline-offset:2px}
.start-btn:focus-visible,.tray-btn:focus-visible,.start-item:focus-visible,.power-btn:focus-visible,.taskbar-chip:focus-visible,.agent-pill:focus-visible,.ctx-menu-item:focus-visible{outline:2px solid var(--hart-accent);outline-offset:-2px}
/* Skip link (a11y): jump straight to content; off-screen until keyboard-focused. */
.skip-link{position:fixed;top:-200px;left:8px;z-index:100000;padding:8px 16px;background:var(--hart-accent);color:var(--hart-on-accent);border-radius:8px;font-weight:600;text-decoration:none;transition:top 0.2s}
.skip-link:focus{top:8px}
.ds-btn:disabled,.ds-btn[disabled]{opacity:0.38;pointer-events:none}
.ds-btn .mi{font-size:18px}
.ds-btn-primary{background:var(--hart-accent);color:var(--hart-on-accent)}
.ds-btn-primary:hover{box-shadow:var(--ds-elevation-1);filter:brightness(1.1)}
.ds-btn-primary:active{filter:brightness(0.9)}
.ds-btn-secondary{background:transparent;color:var(--hart-accent);border:1px solid var(--hart-glass-border)}
.ds-btn-secondary:hover{background:var(--ds-state-hover)}
.ds-btn-secondary:active{background:var(--ds-state-pressed)}
.ds-btn-text{background:transparent;color:var(--hart-accent);padding:10px var(--ds-space-3)}
.ds-btn-text:hover{background:var(--ds-state-hover)}
.ds-btn-tonal{background:var(--ds-surface-3);color:var(--hart-accent)}
.ds-btn-tonal:hover{box-shadow:var(--ds-elevation-1);background:var(--ds-surface-4)}
.ds-btn-danger{background:var(--hart-error);color:#fff}
.ds-btn-danger:hover{box-shadow:var(--ds-elevation-1);filter:brightness(1.1)}
.ds-btn-icon{padding:var(--ds-space-2);border-radius:var(--ds-radius-full);
  min-width:40px;min-height:40px}
.ds-btn-sm{padding:6px var(--ds-space-4);font-size:12px;line-height:16px}

/* Ripple */
.ds-ripple{position:absolute;border-radius:50%;background:rgba(255,255,255,0.2);
  transform:scale(0);animation:ds-ripple-anim 500ms ease-out forwards;pointer-events:none}
@keyframes ds-ripple-anim{to{transform:scale(2.5);opacity:0}}

/* ── Input ── */
.ds-input-wrap{position:relative;display:flex;flex-direction:column;gap:var(--ds-space-1)}
.ds-input{width:100%;padding:var(--ds-space-3) var(--ds-space-4);
  border-radius:var(--ds-radius-sm);border:1px solid var(--hart-glass-border);
  background:var(--ds-surface-1);color:var(--hart-text);
  font-family:var(--ds-font-body);font-size:14px;line-height:20px;outline:none;
  transition:border-color var(--ds-duration-medium) var(--ds-ease-standard),
    box-shadow var(--ds-duration-medium) var(--ds-ease-standard)}
.ds-input:focus{border-color:var(--hart-accent);box-shadow:0 0 0 2px rgba(0,212,170,0.25)}
.ds-input::placeholder{color:var(--hart-muted)}
.ds-input-label{font-size:12px;font-weight:500;letter-spacing:0.5px;
  color:var(--hart-muted);text-transform:uppercase}
.ds-input-error{border-color:var(--hart-error)}
.ds-input-error:focus{box-shadow:0 0 0 2px rgba(255,107,107,0.2)}
.ds-input-help{font-size:12px;color:var(--hart-muted);margin-top:var(--ds-space-1)}

/* ── Select ── */
.ds-select{width:100%;padding:var(--ds-space-3) var(--ds-space-4);padding-right:var(--ds-space-8);
  border-radius:var(--ds-radius-sm);border:1px solid var(--hart-glass-border);
  background:var(--ds-surface-1);color:var(--hart-text);font-family:var(--ds-font-body);
  font-size:14px;outline:none;appearance:none;cursor:pointer;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='%2378909c'%3E%3Cpath d='M7 10l5 5 5-5z'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center;
  transition:border-color var(--ds-duration-medium) var(--ds-ease-standard)}
.ds-select:focus{border-color:var(--hart-accent)}
.ds-select option{background:var(--hart-surface);color:var(--hart-text)}

/* ── Slider ── */
.ds-slider{-webkit-appearance:none;appearance:none;width:100%;height:4px;
  background:var(--ds-surface-3);border-radius:var(--ds-radius-full);outline:none;
  transition:background var(--ds-duration-medium)}
.ds-slider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:20px;height:20px;
  border-radius:50%;background:var(--hart-accent);cursor:pointer;box-shadow:var(--ds-elevation-1);
  transition:box-shadow var(--ds-duration-short) var(--ds-ease-standard),
    transform var(--ds-duration-short) var(--ds-ease-spring)}
.ds-slider::-webkit-slider-thumb:hover{box-shadow:var(--ds-elevation-2);transform:scale(1.15)}
.ds-slider::-webkit-slider-thumb:active{box-shadow:var(--ds-elevation-3);transform:scale(1.25)}
.ds-slider::-moz-range-thumb{width:20px;height:20px;border-radius:50%;
  background:var(--hart-accent);cursor:pointer;border:none;box-shadow:var(--ds-elevation-1)}

/* ── Toggle switch ── */
/* Used by the a11y + startup panels (class was used but never defined, so the
   switches rendered as a raw checkbox + empty span). The native <input> is
   visually hidden but stays FOCUSABLE (not display:none, which drops tab order);
   the .ds-switch-slider is its next sibling. */
.ds-switch{display:inline-flex;align-items:center;cursor:pointer}
.ds-switch input{position:absolute;width:1px;height:1px;opacity:0;margin:0}
.ds-switch-slider{display:inline-block;width:38px;height:22px;border-radius:999px;
  background:var(--ds-surface-3);position:relative;flex-shrink:0;
  transition:background var(--ds-duration-short) var(--ds-ease-standard)}
.ds-switch-slider::before{content:'';position:absolute;top:2px;left:2px;width:18px;height:18px;
  border-radius:50%;background:#fff;box-shadow:var(--ds-elevation-1);
  transition:transform var(--ds-duration-short) var(--ds-ease-spring)}
.ds-switch input:checked + .ds-switch-slider{background:var(--hart-accent)}
.ds-switch input:checked + .ds-switch-slider::before{transform:translateX(16px)}
.ds-switch input:focus-visible + .ds-switch-slider{outline:2px solid var(--hart-accent);outline-offset:2px}

/* ── Card ── */
.ds-card{background:var(--hart-surface);border-radius:var(--ds-radius-md);
  padding:var(--ds-space-4);border:1px solid var(--hart-glass-border);
  transition:box-shadow var(--ds-duration-medium) var(--ds-ease-standard),
    transform var(--ds-duration-medium) var(--ds-ease-standard)}
.ds-card-elevated{box-shadow:var(--ds-elevation-1)}
.ds-card-interactive{cursor:pointer}
.ds-card-interactive:hover{box-shadow:var(--ds-elevation-2);transform:translateY(-1px)}
.ds-card-interactive:active{transform:translateY(0);box-shadow:var(--ds-elevation-1)}

/* ── Status Chip ── */
.ds-chip{display:inline-flex;align-items:center;gap:var(--ds-space-1);
  padding:var(--ds-space-1) var(--ds-space-3);border-radius:var(--ds-radius-full);
  font-size:12px;font-weight:500;letter-spacing:0.5px;line-height:16px;
  border:1px solid var(--hart-glass-border);background:var(--ds-surface-1)}
.ds-chip-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.ds-chip-success .ds-chip-dot{background:var(--hart-active)}
.ds-chip-warning .ds-chip-dot{background:var(--hart-caution)}
.ds-chip-error .ds-chip-dot{background:var(--hart-error)}

/* ── Progress Bar ── */
.ds-progress{height:6px;background:var(--ds-surface-3);border-radius:var(--ds-radius-full);overflow:hidden}
.ds-progress-fill{height:100%;border-radius:var(--ds-radius-full);
  transition:width var(--ds-duration-long) var(--ds-ease-decelerate)}

/* ── Skeleton Loader ── */
.ds-skeleton{background:linear-gradient(90deg,var(--ds-surface-2) 25%,var(--ds-surface-4) 50%,var(--ds-surface-2) 75%);
  background-size:200% 100%;border-radius:var(--ds-radius-sm);
  animation:ds-shimmer 1.5s ease-in-out infinite}
.ds-skeleton-text{height:14px;margin-bottom:var(--ds-space-2);border-radius:var(--ds-radius-xs)}
.ds-skeleton-title{height:22px;width:50%;margin-bottom:var(--ds-space-3)}
.ds-skeleton-circle{border-radius:50%}
.ds-skeleton-bar{height:6px;border-radius:var(--ds-radius-full)}
.ds-skeleton-card{height:64px;border-radius:var(--ds-radius-md);margin-bottom:var(--ds-space-2)}
@keyframes ds-shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* ── Modal ── */
.ds-modal-overlay{position:fixed;inset:0;z-index:10000;display:flex;
  align-items:center;justify-content:center;background:rgba(0,0,0,0.6);
  opacity:0;visibility:hidden;
  transition:opacity var(--ds-duration-medium) var(--ds-ease-standard),visibility var(--ds-duration-medium)}
.ds-modal-overlay.ds-open{opacity:1;visibility:visible}
.ds-modal{background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);
  border-radius:var(--ds-radius-lg);padding:var(--ds-space-6);
  max-width:480px;width:calc(100% - var(--ds-space-8));box-shadow:var(--ds-elevation-5);
  backdrop-filter:blur(20px) saturate(180%);-webkit-backdrop-filter:blur(20px) saturate(180%);
  transform:scale(0.92) translateY(20px);opacity:0;
  transition:transform var(--ds-duration-long) var(--ds-ease-spring),
    opacity var(--ds-duration-medium) var(--ds-ease-decelerate)}
.ds-modal-overlay.ds-open .ds-modal{transform:scale(1) translateY(0);opacity:1}
.ds-modal-title{font-size:22px;line-height:28px;font-weight:500;margin-bottom:var(--ds-space-4)}
.ds-modal-body{font-size:14px;line-height:20px;color:var(--hart-muted);margin-bottom:var(--ds-space-6)}
.ds-modal-actions{display:flex;justify-content:flex-end;gap:var(--ds-space-2)}

/* ── Toast (upgraded) ── */
.ds-toast{display:flex;align-items:flex-start;gap:var(--ds-space-3);padding:var(--ds-space-4);
  border-radius:var(--ds-radius-md);background:var(--hart-glass-bg);
  border:1px solid var(--hart-glass-border);box-shadow:var(--ds-elevation-3);
  max-width:380px;pointer-events:auto;cursor:pointer;position:relative;overflow:hidden;
  backdrop-filter:blur(16px) saturate(150%);-webkit-backdrop-filter:blur(16px) saturate(150%);
  animation:ds-toast-in var(--ds-duration-long) var(--ds-ease-spring)}
.ds-toast-icon{font-size:20px;flex-shrink:0;margin-top:1px}
.ds-toast-content{flex:1;min-width:0}
.ds-toast-title{font-size:14px;font-weight:500;line-height:20px}
.ds-toast-message{font-size:12px;line-height:16px;color:var(--hart-muted);margin-top:2px}
.ds-toast-progress{position:absolute;bottom:0;left:0;height:2px;background:var(--hart-accent);
  animation:ds-toast-countdown 5s linear forwards}
.ds-toast-exit{animation:ds-toast-out var(--ds-duration-medium) var(--ds-ease-accelerate) forwards}
@keyframes ds-toast-in{from{transform:translateX(100%) scale(0.95);opacity:0}to{transform:translateX(0) scale(1);opacity:1}}
@keyframes ds-toast-out{to{transform:translateX(30px);opacity:0}}
@keyframes ds-toast-countdown{from{width:100%}to{width:0%}}

/* ── Panel Content Layout ── */
.ds-panel-grid{display:grid;gap:var(--ds-space-3)}
.ds-panel-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--ds-space-2)}
.ds-panel-title{font-size:22px;line-height:28px;font-weight:500;color:var(--hart-heading)}
.ds-panel-subtitle{font-size:14px;color:var(--hart-muted)}
.ds-section-label{font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:1.5px;color:var(--hart-muted);padding:var(--ds-space-2) 0}

/* ── List Item ── */
.ds-list-item{display:flex;align-items:center;gap:var(--ds-space-3);
  padding:var(--ds-space-3);border-radius:var(--ds-radius-sm);background:var(--hart-surface);
  transition:background var(--ds-duration-short) var(--ds-ease-standard),
    transform var(--ds-duration-short) var(--ds-ease-standard)}
.ds-list-item-interactive{cursor:pointer}
.ds-list-item-interactive:hover{background:var(--hart-surface-hover);transform:translateY(-1px)}
.ds-list-item-icon{font-size:var(--ds-icon-sm);flex-shrink:0}
.ds-list-item-content{flex:1;min-width:0}
.ds-list-item-primary{font-size:14px;line-height:20px}
.ds-list-item-secondary{font-size:12px;line-height:16px;color:var(--hart-muted)}
.ds-list-item-trailing{font-size:12px;flex-shrink:0}

/* ── Metric Display ── */
.ds-metric{text-align:center;padding:var(--ds-space-4)}
.ds-metric-value{font-size:32px;font-weight:600;line-height:40px}
.ds-metric-label{font-size:12px;color:var(--hart-muted);margin-top:var(--ds-space-1)}
.ds-metric-icon{font-size:var(--ds-icon-xl);margin-bottom:var(--ds-space-2)}

/* ── Dot / Divider ── */
.ds-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.ds-divider{border:none;border-top:1px solid var(--hart-glass-border);margin:var(--ds-space-3) 0}

/* ── Flex utilities ── */
.ds-flex{display:flex}.ds-flex-col{flex-direction:column}
.ds-flex-center{align-items:center;justify-content:center}
.ds-flex-between{justify-content:space-between}.ds-flex-wrap{flex-wrap:wrap}
.ds-gap-1{gap:var(--ds-space-1)}.ds-gap-2{gap:var(--ds-space-2)}
.ds-gap-3{gap:var(--ds-space-3)}.ds-gap-4{gap:var(--ds-space-4)}
.ds-flex-1{flex:1;min-width:0}

/* ── Color utilities ── */
.ds-text-accent{color:var(--hart-accent)}.ds-text-active{color:var(--hart-active)}
.ds-text-error{color:var(--hart-error)}.ds-text-caution{color:var(--hart-caution)}
.ds-text-muted{color:var(--hart-muted)}.ds-text-heading{color:var(--hart-heading)}

/* ── Animations: fade-in, stagger ── */
.ds-fade-in{animation:ds-content-enter var(--ds-duration-medium) var(--ds-ease-decelerate)}
@keyframes ds-content-enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.ds-stagger>*{animation:ds-content-enter var(--ds-duration-medium) var(--ds-ease-decelerate) both}
.ds-stagger>*:nth-child(1){animation-delay:0ms}
.ds-stagger>*:nth-child(2){animation-delay:30ms}
.ds-stagger>*:nth-child(3){animation-delay:40ms}
.ds-stagger>*:nth-child(4){animation-delay:50ms}
.ds-stagger>*:nth-child(5){animation-delay:60ms}
.ds-stagger>*:nth-child(6){animation-delay:70ms}
.ds-stagger>*:nth-child(7){animation-delay:80ms}
.ds-stagger>*:nth-child(8){animation-delay:90ms}
.ds-stagger>*:nth-child(n+9){animation-delay:100ms}

/* ── Reduced motion ── */
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:0.01ms!important;
    animation-iteration-count:1!important;transition-duration:0.01ms!important}
}

/* ── Accessibility: applied from the live a11y state via <html> classes ── */
html.a11y-contrast{--hart-muted:#e8eef2;--hart-glass-bg:#0a0a12;--hart-glass-border:#ffffff;--hart-text:#ffffff}
html.a11y-contrast .glass{background:#0a0a12;border-width:2px}
html.a11y-rmotion *,html.a11y-rmotion *::before,html.a11y-rmotion *::after{
  animation-duration:0.01ms!important;animation-iteration-count:1!important;transition-duration:0.01ms!important}

/* ── Material Icons: BUNDLED woff2 — works fully offline, every glyph ── */
/* Every shell icon is <span class="mi material-icons-round">name</span>.
   The shell ships its OWN icon font (integrations/agent_engine/static/
   MaterialSymbolsRounded.woff2 — a static, filled instance of Material Symbols
   Rounded, ~440KB, 6.5k glyphs incl. smart_toy/shield). It is loaded via the
   @font-face below from /shell/static, so EVERY glyph renders on a fresh
   OFFLINE USB boot AND on the frozen Win/macOS desktop (which has no Material
   font at all). The legacy 'Material Icons Round' lacked newer glyphs
   (smart_toy, shield) — Material Symbols is a strict superset, so nothing
   regresses. The Google <link> in <head> stays as a progressive-enhancement
   only (online round variant); the bundled font is authoritative. The `liga`
   feature turns the ligature names ("smart_toy") into glyphs. */
@font-face {
  font-family: 'Material Symbols Rounded';
  font-style: normal; font-weight: 400; font-display: block;
  src: url('/shell/static/MaterialSymbolsRounded.woff2') format('woff2');
}
.mi, .material-icons-round {
  font-family: 'Material Symbols Rounded', 'Material Icons Round', 'Material Icons', 'Material Symbols Outlined';
  font-weight: normal; font-style: normal; line-height: 1;
  letter-spacing: normal; text-transform: none; white-space: nowrap;
  word-wrap: normal; direction: ltr; display: inline-block;
  -webkit-font-feature-settings: 'liga'; font-feature-settings: 'liga';
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
'''

        # Potato-only perf override, interpolated after the design system above and
        # ONLY when is_potato. The design-system block hardcodes backdrop-filter on
        # .ds-modal/.ds-toast and infinite skeleton/toast animations, none gated —
        # on llvmpipe those re-rasterise a region every frame. Disable the blur (+
        # opaque bg for legibility), stop the decorative animations, and honour the
        # previously-dead potato `disable_shadows` intent by zeroing elevations.
        # Plain string (literal CSS braces) → no f-string escaping pitfalls.
        _CSS_POTATO_OVERRIDE = (
            '.ds-modal,.ds-toast{backdrop-filter:none;-webkit-backdrop-filter:none;'
            'background:var(--hart-surface)}'
            ' .ds-skeleton{animation:none;background:var(--ds-surface-2)}'
            ' .ds-toast-progress{animation:none}'
            ' .ds-elevation-1,.ds-elevation-2,.ds-elevation-3,.ds-elevation-4,.ds-elevation-5{box-shadow:none}'
        )

        # ═══ Voice-first hero + native depth + buttery motion + de-monochrome ═══
        # Plain string (literal CSS braces) interpolated whole via {_CSS_HERO} in
        # the <style>, like _CSS_DESIGN_SYSTEM — so no f-string escaping. Promotes
        # the EXISTING #hart-voice-orb to the desktop centerpiece and fuses it with
        # a central command bar; reuses the shell's voice/dispatch pipeline.
        _CSS_HERO = '''
/* ── Native-app feel: kill the web tells, crisp font rendering ── */
html,body{overscroll-behavior:none;-webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;text-rendering:optimizeLegibility;-webkit-tap-highlight-color:transparent}
.top-bar,.taskbar,.start-menu,.panel-titlebar,.start-item,.taskbar-chip,
.ds-btn,.hart-hero-chip,.hart-hero-status,.hart-hero-brand{cursor:default}
img{-webkit-user-drag:none;user-select:none}

/* ── Ambient colour wash (de-monochrome): slow drifting multi-hue blobs above
   the wallpaper, theme-independent, so the desktop has living colour. ── */
.hart-ambient{position:fixed;inset:-12%;z-index:1;pointer-events:none;opacity:0.5;
  filter:blur(64px) saturate(140%);
  background:
    radial-gradient(38% 42% at 22% 26%, rgba(0,212,170,0.42), transparent 70%),
    radial-gradient(34% 40% at 80% 30%, rgba(108,99,255,0.40), transparent 70%),
    radial-gradient(42% 46% at 60% 80%, rgba(34,176,255,0.30), transparent 72%),
    radial-gradient(30% 36% at 28% 82%, rgba(255,120,180,0.24), transparent 72%);
  animation:hart-ambient-drift 30s ease-in-out infinite alternate}
@keyframes hart-ambient-drift{0%{transform:translate3d(0,0,0) scale(1)}
  50%{transform:translate3d(2.4%,-2.2%,0) scale(1.08)}100%{transform:translate3d(-2.4%,2.2%,0) scale(1.05)}}
.hart-grain{position:fixed;inset:0;z-index:2;pointer-events:none;opacity:0.045;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.hart-vignette{position:fixed;inset:0;z-index:2;pointer-events:none;
  background:radial-gradient(120% 120% at 50% 38%, transparent 56%, rgba(0,0,0,0.30) 100%)}

/* ── The hero ── */
.hart-hero{position:fixed;left:50%;top:46%;transform:translate(-50%,-50%);z-index:40;
  display:flex;flex-direction:column;align-items:center;gap:16px;text-align:center;
  width:min(660px,86vw);pointer-events:none;
  transition:opacity .55s cubic-bezier(.2,0,0,1),transform .55s cubic-bezier(.2,0,0,1),filter .55s}
.hart-hero>*{pointer-events:auto}
.hart-hero.dimmed{opacity:0;transform:translate(-50%,-56%) scale(.96);filter:blur(6px)}
.hart-hero.dimmed>*{pointer-events:none}
.hart-hero-brand{display:flex;align-items:center;gap:9px;opacity:.92}
.hart-hero-brand img{width:34px;height:34px;filter:drop-shadow(0 3px 12px rgba(0,212,170,.4))}
.hart-hero-brand span{font-size:14px;font-weight:600;letter-spacing:2.5px;opacity:.8}
/* The ORB ITSELF is the click-to-talk control (no mic glyph inside it). The
   canvas keeps pointer-events:none; the orbwrap captures the click + carries
   the listening glow that the old centre mic used to show. */
.hart-hero-orbwrap{position:relative;width:300px;height:300px;display:flex;align-items:center;justify-content:center;
  cursor:pointer;border-radius:50%;
  transition:transform .25s cubic-bezier(.175,.885,.32,1.275),box-shadow .25s}
.hart-hero-orbwrap:hover{transform:scale(1.03)}
.hart-hero-orbwrap:active{transform:scale(.985)}
.hart-hero-orbwrap:focus-visible{outline:none;box-shadow:0 0 0 3px var(--hart-accent)}
/* The orb's bloom = the mockup's layered "0 0 90px teal + 0 0 160px violet": a
   teal INNER glow + a brand-violet OUTER halo. Replaces the leftover indigo
   #6C63FF drop-shadow b1.1 flagged (which read flat/blue); core+body stay teal,
   so this adds a violet HALO without washing the orb blue. */
#hart-voice-orb{width:300px;height:300px;background:transparent;pointer-events:none;
  filter:drop-shadow(0 0 46px rgba(0,230,195,.34)) drop-shadow(0 8px 64px rgba(155,92,255,.26))}
.hart-hero-orbwrap.listening{box-shadow:0 0 0 5px rgba(255,107,107,.22),0 10px 44px rgba(255,107,107,.35)}
.hart-hero-orbwrap.listening #hart-voice-orb{filter:drop-shadow(0 12px 44px rgba(255,107,107,.4))}
.hart-hero-status{font-size:13px;font-weight:500;letter-spacing:.3px;color:var(--hart-muted);min-height:18px;
  transition:color .3s;max-width:560px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hart-hero-status.thinking{color:var(--hart-accent)}
.hart-hero-bar{display:flex;align-items:center;gap:10px;width:100%;padding:7px 8px 7px 18px;
  border-radius:var(--ds-radius-full);transition:box-shadow .25s,border-color .25s}
.hart-hero-bar:focus-within{box-shadow:0 0 0 2px var(--hart-accent),0 18px 50px rgba(0,0,0,.42)}
.hart-hero-bar-ic{font-size:21px;color:var(--hart-muted);flex-shrink:0}
.hart-hero-input{flex:1;min-width:0;background:transparent;border:none;outline:none;color:var(--hart-text);
  font-family:var(--ds-font-body);font-size:15.5px;letter-spacing:.2px}
.hart-hero-input::placeholder{color:var(--hart-muted)}
.hart-hero-go{width:42px;height:42px;border-radius:50%;border:none;flex-shrink:0;cursor:pointer;
  background:var(--hart-accent);color:var(--hart-on-accent);display:flex;align-items:center;justify-content:center;
  transition:transform .18s cubic-bezier(.175,.885,.32,1.275),filter .18s}
.hart-hero-go:hover{filter:brightness(1.12);transform:scale(1.08)}
.hart-hero-go:active{transform:scale(.94)}
.hart-hero-go .mi{font-size:21px}
.hart-hero-hevolve{display:flex;align-items:center;gap:7px;height:16px;opacity:0;font-size:11px;font-weight:600;
  letter-spacing:1.2px;text-transform:uppercase;color:var(--hart-muted);transition:opacity .3s}
.hart-hero-hevolve.on{opacity:.9}
.hart-hero-hevolve .dot{width:7px;height:7px;border-radius:50%;background:var(--hart-accent);
  box-shadow:0 0 10px var(--hart-accent);animation:hart-hevolve-pulse 1s ease-in-out infinite}
@keyframes hart-hevolve-pulse{0%,100%{transform:scale(.7);opacity:.5}50%{transform:scale(1.15);opacity:1}}
.hart-hero-chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:2px}
.hart-hero-chip{padding:7px 15px;border-radius:var(--ds-radius-full);font-size:12px;font-weight:500;
  background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);color:var(--hart-text);cursor:pointer;
  font-family:var(--ds-font-body);transition:background .18s,transform .18s cubic-bezier(.175,.885,.32,1.275),box-shadow .18s}
.hart-hero-chip:hover{background:var(--hart-surface-hover);transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.28)}
.hart-hero-chip:active{transform:translateY(0)}

/* ── Buttery spring micro-animations on existing chrome ── */
.start-item{transition:background var(--hart-anim-speed),transform .18s cubic-bezier(.175,.885,.32,1.275)}
.start-item:hover{transform:translateY(-2px) scale(1.02)}
.taskbar-chip{transition:background .15s,transform .18s cubic-bezier(.175,.885,.32,1.275)}
.taskbar-chip:hover{transform:translateY(-2px)}
.tray-btn{transition:background var(--hart-anim-speed),transform .18s cubic-bezier(.175,.885,.32,1.275)}
.tray-btn:hover{transform:translateY(-1px) scale(1.05)}
.start-logo{width:20px;height:20px;flex-shrink:0}
.top-bar .start-btn:hover .start-logo{filter:drop-shadow(0 0 8px var(--hart-accent))}
@media(prefers-reduced-motion:reduce){.hart-ambient,.hart-hero-hevolve .dot{animation:none}}
html.a11y-rmotion .hart-ambient,html.a11y-rmotion .hart-hero-hevolve .dot{animation:none}
'''

        # ═══ Desktop icon layer (drag-drop, grid-snapped, persisted) ═══
        # Plain string interpolated via {_CSS_DESKTOP}. The layer is
        # pointer-events:none (so empty-desktop right-click still reaches the
        # wallpaper menu); only the icons capture pointer events.
        _CSS_DESKTOP = '''
.hart-desktop{position:fixed;left:0;right:0;top:var(--hart-topbar-height);bottom:44px;z-index:20;pointer-events:none}
.desktop-icon{position:absolute;width:84px;display:flex;flex-direction:column;align-items:center;gap:6px;
  padding:8px 4px;border-radius:12px;cursor:default;pointer-events:auto;user-select:none;
  transition:background .15s,transform .12s cubic-bezier(.175,.885,.32,1.275);will-change:transform}
.desktop-icon:hover{background:rgba(255,255,255,0.08)}
.desktop-icon.selected{background:rgba(108,99,255,0.28);outline:1px solid rgba(108,99,255,0.5)}
.desktop-icon:focus-visible{outline:2px solid var(--hart-accent);outline-offset:2px}
.desktop-icon.dragging{z-index:60;transition:none;opacity:.92;cursor:grabbing}
.desktop-icon .di-glyph{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;justify-content:center;
  background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);
  box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.08),0 4px 12px rgba(0,0,0,.28)}
.desktop-icon:hover .di-glyph{box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.12),0 8px 20px rgba(0,0,0,.36);transform:translateY(-1px)}
.desktop-icon .di-glyph .mi{font-size:28px;color:var(--hart-accent)}
.desktop-icon .di-label{font-size:11px;line-height:1.25;text-align:center;max-width:80px;color:var(--hart-text);
  text-shadow:0 1px 3px rgba(0,0,0,.6);overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
/* Emoji/unicode glyph (not the Material icon font) — same size as .mi, normal family */
.desktop-icon .di-glyph .di-emoji{font-family:"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",system-ui;font-size:30px;line-height:1}
/* ── Per-icon Customize dialog (macOS-/Windows-style glyph/label/color) ── */
.hart-icustom-backdrop{position:fixed;inset:0;z-index:9000;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,0.42);backdrop-filter:blur(2px);-webkit-backdrop-filter:blur(2px)}
.hart-icustom{width:340px;max-width:92vw;padding:18px;border-radius:16px;display:flex;flex-direction:column;gap:12px;
  color:var(--hart-text);font-family:var(--ds-font-body,system-ui);box-shadow:0 18px 50px rgba(0,0,0,.5)}
.hart-icustom-head{display:flex;align-items:center;gap:14px}
.hart-icustom .hic-prev{flex:none}
.hart-icustom .hic-prev .di-glyph{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;justify-content:center;
  background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border)}
.hart-icustom .hic-prev .di-glyph .mi{font-size:28px;color:var(--hart-accent)}
.hart-icustom .hic-prev .di-glyph .di-emoji{font-family:"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",system-ui;font-size:30px;line-height:1}
.hart-icustom-title{font-size:15px;font-weight:600}
.hart-icustom-row{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--hart-muted)}
.hart-icustom-row input[type=text]{padding:8px 10px;border-radius:9px;border:1px solid var(--hart-glass-border);
  background:var(--hart-surface,rgba(255,255,255,0.05));color:var(--hart-text);font:14px var(--ds-font-body,system-ui)}
.hart-icustom-row input[type=text]:focus{outline:none;border-color:var(--hart-accent)}
.hart-icustom .hic-color-wrap{display:flex;align-items:center;gap:10px}
.hart-icustom .hic-color-wrap input[type=color]{width:40px;height:30px;padding:0;border:1px solid var(--hart-glass-border);
  border-radius:8px;background:none;cursor:pointer}
.hart-icustom-actions{display:flex;align-items:center;gap:8px;margin-top:4px}
.hart-icustom-btn{padding:7px 14px;border-radius:9px;border:1px solid var(--hart-glass-border);cursor:pointer;
  background:var(--hart-surface,rgba(255,255,255,0.06));color:var(--hart-text);font:13px var(--ds-font-body,system-ui);
  transition:background .15s,transform .12s}
.hart-icustom-btn:hover{background:var(--hart-surface-hover,rgba(255,255,255,0.12));transform:translateY(-1px)}
.hart-icustom-btn.primary{background:var(--hart-accent);color:var(--hart-on-accent,#fff);border-color:transparent}
.hart-icustom-btn.ghost{background:transparent}
.hart-icustom-btn:focus-visible{outline:2px solid var(--hart-accent);outline-offset:2px}
/* ── Virtual-desktop switcher (bottom-center) + settings squares ── */
.hart-ws-switcher{position:fixed;bottom:6px;left:50%;transform:translateX(-50%);z-index:8050;display:flex;gap:4px;padding:4px 6px;border-radius:999px}
.hart-ws-dot{width:26px;height:20px;border:none;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;
  background:transparent;color:var(--hart-muted);font-family:var(--ds-font-body);
  transition:background .15s,color .15s,transform .15s cubic-bezier(.175,.885,.32,1.275)}
.hart-ws-dot:hover{background:var(--hart-surface-hover);transform:translateY(-1px)}
.hart-ws-dot.active{background:var(--hart-accent);color:var(--hart-on-accent)}
.hart-ws-square{aspect-ratio:16/9;border-radius:8px;background:#1a1a1a;border:2px solid transparent;
  display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--hart-muted);font-weight:600;
  transition:border-color .15s,background .15s,color .15s}
.hart-ws-square:hover{background:rgba(255,255,255,0.06)}
.hart-ws-square.active{border-color:var(--hart-accent);background:rgba(255,255,255,0.08);color:var(--hart-accent)}
/* ── Themes / Wallpaper gallery (Personalize panel) ── */
.hart-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(116px,1fr));gap:10px;padding:4px 0 8px}
.hart-tile{cursor:pointer;border-radius:10px;transition:transform .15s cubic-bezier(.175,.885,.32,1.275)}
.hart-tile:hover{transform:translateY(-3px)}
.hart-tile:focus-visible{outline:2px solid var(--hart-accent);outline-offset:2px}
.hart-tile .htc-prev{position:relative;aspect-ratio:16/10;border-radius:10px;overflow:hidden;
  border:1px solid var(--hart-glass-border);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.hart-tile:hover .htc-prev{border-color:var(--hart-accent)}
.hart-tile .htc-dot{position:absolute;left:8px;bottom:8px;width:16px;height:16px;border-radius:50%;
  box-shadow:0 0 8px currentColor,inset 0 0 0 2px rgba(255,255,255,0.25)}
.hart-tile .htc-name{font-size:11px;color:var(--hart-text);text-align:center;margin-top:5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Customization hub (#140/#161/#162): orb-variety selection ring + the custom
   colour picker + the media-by-URL row. Palette/orb cards reuse .hart-tile. */
.hart-orb-card.active .htc-prev{border-color:var(--hart-accent);
  box-shadow:0 0 0 2px var(--hart-accent),0 4px 12px rgba(0,0,0,.3)}
.hart-custom-palette{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px;padding:2px 0 12px}
.hart-cp-field{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--hart-muted)}
.hart-cp-field input[type=color]{width:52px;height:34px;padding:0;border:1px solid var(--hart-glass-border);
  border-radius:8px;background:transparent;cursor:pointer}
.hart-cp-apply{align-self:flex-end}
.hart-media-url{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:2px 0 12px}
.hart-media-url .ds-input{flex:1;min-width:160px}
.hart-media-url .ds-select{width:96px;flex:0 0 auto}
/* ── Marketplace (App Store) — premium liquid-glass cards ── */
.hart-mkt{padding:var(--ds-space-2) var(--ds-space-1) var(--ds-space-6)}
.hart-mkt-head{margin-bottom:var(--ds-space-5)}
.hart-mkt-search{display:flex;gap:var(--ds-space-2);margin:var(--ds-space-4) 0 var(--ds-space-2);
  position:sticky;top:0;z-index:3;padding-bottom:var(--ds-space-2);
  background:linear-gradient(to bottom,var(--hart-surface) 70%,transparent)}
.hart-mkt-search .ds-input{flex:1}
.hart-mkt-section{margin-top:var(--ds-space-5)}
.hart-mkt-section:first-child{margin-top:var(--ds-space-2)}
/* Already-installed / pre-bundled apps section (sits above the featured catalogue) */
.hart-mkt-installed{margin-top:var(--ds-space-2);margin-bottom:var(--ds-space-2)}
.hart-app-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(248px,1fr));
  gap:var(--ds-space-3);margin-top:var(--ds-space-3)}
.hart-app-card{position:relative;display:flex;flex-direction:column;gap:var(--ds-space-3);
  padding:var(--ds-space-4);border-radius:var(--ds-radius-lg);overflow:hidden;
  background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);
  backdrop-filter:blur(12px) saturate(1.2);-webkit-backdrop-filter:blur(12px) saturate(1.2);
  box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.06),0 2px 10px rgba(0,0,0,0.22);
  transition:transform .18s cubic-bezier(.175,.885,.32,1.275),box-shadow .18s,border-color .18s}
/* subtle top-light grain wash so cards feel like frosted glass, not flat tiles */
.hart-app-card::before{content:'';position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(120% 90% at 0% 0%,rgba(255,255,255,0.06),transparent 60%);opacity:.9}
.hart-app-card:hover{transform:translateY(-3px);border-color:var(--hart-accent);
  box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.10),0 12px 30px rgba(0,0,0,0.34)}
.hart-app-card .hac-top{display:flex;align-items:flex-start;gap:var(--ds-space-3);position:relative}
.hart-app-card .hac-ic{width:52px;height:52px;flex-shrink:0;border-radius:var(--ds-radius-md);
  display:flex;align-items:center;justify-content:center;
  background:linear-gradient(150deg,rgba(255,255,255,0.10),rgba(255,255,255,0.03));
  border:1px solid var(--hart-glass-border);box-shadow:inset 0 1px 0 rgba(255,255,255,0.10)}
.hart-app-card .hac-ic .mi{font-size:28px;color:var(--hart-accent)}
.hart-app-card .hac-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.hart-app-card .hac-name{font-size:14px;font-weight:600;line-height:18px;color:var(--hart-heading);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hart-app-card .hac-desc{font-size:12px;line-height:16px;color:var(--hart-muted);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hart-app-card .hac-cat{font-size:10px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;
  color:var(--hart-accent);opacity:.8}
.hart-app-card .ds-btn{position:relative;align-self:stretch;justify-content:center}
/* Already-installed action: a calm, non-interactive "done" state (not a CTA) */
.hart-app-card .ds-btn.is-installed{background:rgba(0,230,118,.14);color:var(--hart-active);
  border:1px solid rgba(0,230,118,.35);cursor:default;opacity:1}
.hart-app-card .ds-btn.is-installed:hover{transform:none;filter:none}
/* ── Buttery: window spring-open + dock perf hint (Phase D) ── */
@keyframes hart-panel-in{from{opacity:0;transform:scale(.92) translateY(14px)}to{opacity:1;transform:scale(1) translateY(0)}}
.panel{animation:hart-panel-in .3s cubic-bezier(.175,.885,.32,1.275)}
.taskbar-chip{will-change:transform}
@media(prefers-reduced-motion:reduce){.panel{animation:none}}
html.a11y-rmotion .panel{animation:none}
/* ── AI sensory cluster — a FLOATING, DRAGGABLE widget (not rigid like cage).
   The whole #hart-senses is picked up + dropped anywhere (pointer + touch),
   constrained to the viewport, position persisted to localStorage. JS sets
   left/top inline; this default is the bottom-left spot it falls back to.
   `touch-action:none` so a drag doesn't scroll/zoom the page on touch. ── */
.hart-senses{position:fixed;left:14px;top:auto;bottom:54px;z-index:8100;touch-action:none}
.hart-senses.dragging{cursor:grabbing;user-select:none}
.hart-senses.dragging .hart-senses-cluster{box-shadow:0 12px 36px rgba(0,0,0,.5);transform:scale(1.03)}
/* Eye + mic grouped as one floating glass "sensory" pair (grip | vision | audio). */
.hart-senses-cluster{display:flex;align-items:center;gap:8px;padding:6px;border-radius:999px;
  background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.08),0 6px 22px rgba(0,0,0,.4);
  transition:box-shadow .2s,transform .2s cubic-bezier(.175,.885,.32,1.275)}
/* Drag affordance - the whole widget body is draggable; the grip is VISUAL ONLY
   and stays hidden (opacity:0) until a drag is in progress. Width + pointer-events
   are preserved so the grip remains part of the drag hit-area (hide via opacity,
   not display). The reveal-on-drag rule lives with the Dimension-2 grip block. */
.hart-senses-grip{display:flex;align-items:center;justify-content:center;width:22px;height:40px;flex-shrink:0;
  cursor:grab;color:var(--hart-muted);border-radius:8px;touch-action:none;opacity:0;transition:opacity .18s}
.hart-senses.dragging .hart-senses-grip{cursor:grabbing}
.hart-senses-grip .mi{font-size:20px;opacity:.85}
.hart-senses-mic .mi{color:var(--hart-accent)}
.hart-senses-mic.listening{background:rgba(255,107,107,.18);border-color:var(--hart-error);
  box-shadow:0 0 0 3px rgba(255,107,107,.18),0 4px 16px rgba(0,0,0,.4)}
.hart-senses-mic.listening .mi{color:var(--hart-error)}
.hart-senses-btn{width:46px;height:46px;border-radius:50%;border:1px solid var(--hart-glass-border);cursor:pointer;
  background:var(--hart-glass-bg);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  display:flex;align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(0,0,0,.35);
  transition:transform .18s cubic-bezier(.175,.885,.32,1.275),background .2s,box-shadow .2s}
.hart-senses-btn:hover{transform:scale(1.08)}
.hart-senses-btn .mi{font-size:24px;color:var(--hart-accent)}
.hart-senses-btn.off{background:rgba(255,107,107,.18);border-color:var(--hart-error);
  box-shadow:0 0 0 3px rgba(255,107,107,.18),0 4px 16px rgba(0,0,0,.4)}
.hart-senses-btn.off .mi{color:var(--hart-error)}
.hart-senses-panel{position:absolute;left:0;bottom:56px;display:none;flex-direction:column;gap:6px;
  min-width:248px;max-width:300px;padding:12px 14px;border-radius:12px;background:var(--hart-glass-bg);
  border:1px solid var(--hart-glass-border);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  box-shadow:0 8px 28px rgba(0,0,0,.4)}
.hart-senses-panel.open{display:flex}
.hart-senses-panel .hsp-title{font-size:12px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--hart-muted)}
.hsp-row{display:flex;align-items:center;gap:8px;font-size:12px}
.hsp-row .mi{font-size:16px;color:var(--hart-muted)}
.hsp-name{flex:1}
.hsp-state{font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;padding:2px 7px;border-radius:8px}
.hsp-state.on{color:var(--hart-active);background:rgba(0,230,118,.12)}
.hsp-state.off{color:var(--hart-error);background:rgba(255,107,107,.14)}
.hsp-detail{font-size:10px;color:var(--hart-muted)}
.hsp-foot{font-size:10px;color:var(--hart-muted);margin-top:4px;line-height:1.3}
/* Orb closes its eyes when the human cuts the AI's senses */
.hart-hero.ai-blind #hart-voice-orb{opacity:.12;filter:grayscale(1) brightness(.4);transition:opacity .5s,filter .5s}
.hart-hero.ai-blind .hart-hero-orbwrap{cursor:default;box-shadow:none}
/* ── First-run "Light Your HART" ceremony overlay ── */
.hart-onboarding{position:fixed;inset:0;z-index:12000;display:none;flex-direction:column;align-items:center;justify-content:center;
  gap:26px;text-align:center;padding:48px;background:radial-gradient(circle at 50% 38%,#16142e,#07060f 72%)}
.hart-onboarding.open{display:flex}
/* Brand duotone (b1.2 / GF3): the orb reads TEAL core with a teal-inner +
   VIOLET-outer layered halo (the mockup look), NOT the deprecated indigo
   #6C63FF. Teal LEADS the functional surfaces (orb core, name reveal, option
   chips); violet ACCENTS (the outer halo + the option hover glow). */
.hart-onboarding .hob-orb{width:150px;height:150px;border-radius:50%;flex-shrink:0;
  background:radial-gradient(circle at 50% 40%,rgba(0,230,195,.95),rgba(0,230,195,.34) 45%,transparent 70%);
  box-shadow:0 0 70px rgba(0,230,195,.5),0 0 150px rgba(155,92,255,.24);animation:hob-breathe 3.2s ease-in-out infinite}
@keyframes hob-breathe{0%,100%{transform:scale(1);opacity:.85}50%{transform:scale(1.08);opacity:1}}
.hart-onboarding .hob-name{font-size:34px;font-weight:600;letter-spacing:1px;color:#fff;min-height:0;opacity:0;
  transform:translateY(8px);transition:opacity .6s,transform .6s}
.hart-onboarding .hob-name.show{opacity:1;transform:none;text-shadow:0 0 30px rgba(0,230,195,.55)}
.hart-onboarding .hob-narr{max-width:640px;min-height:84px;display:flex;flex-direction:column;gap:10px}
.hart-onboarding .hob-line{font-size:20px;line-height:1.5;color:#e9f7f3;font-family:var(--ds-font-body);
  opacity:0;transform:translateY(6px);transition:opacity .6s,transform .6s}
.hart-onboarding .hob-line.in{opacity:1;transform:none}
.hart-onboarding .hob-opts{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;max-width:700px}
.hart-onboarding .hob-opt{padding:12px 22px;border-radius:999px;border:1px solid rgba(0,230,195,.4);
  background:rgba(0,230,195,.12);color:#fff;font-size:15px;font-family:var(--ds-font-body);cursor:pointer;
  transition:background .18s,transform .18s cubic-bezier(.175,.885,.32,1.275),box-shadow .18s}
.hart-onboarding .hob-opt:hover{background:rgba(0,230,195,.22);transform:translateY(-2px);box-shadow:0 8px 24px rgba(155,92,255,.35)}
.hart-onboarding .hob-skip{position:fixed;bottom:20px;font-size:12px;color:rgba(255,255,255,.4)}
'''

        # ═══ "Living Glass" unified design system (overhaul) ═══
        # Plain string (literal CSS braces) concatenated whole via
        # {_CSS_LIVING_GLASS} in the <style> AFTER {_CSS_HERO} + {_CSS_DESKTOP}
        # (later source wins — so it re-skins existing selectors without deleting
        # them; a stale cached build still works). Defines ONLY new tokens + new
        # component classes and IMPORTS --hart-accent / --ds-* / --ds-ease-* rather
        # than redefining them. One accent light, one overhead source, one motion
        # grammar; chrome lights only on DETERMINISTIC real-state hooks (the JS half
        # writes #hart-hero-orbwrap[data-orb-state], <html data-*>, .is-sensing,
        # senses .listening, the pager classes — see the implementation contract).
        _CSS_LIVING_GLASS = '''
:root{
  /* ── Accent triad (theme-driven; re-tints on theme change) ── */
  --lg-accent: var(--hart-accent);
  --lg-accent-rgb: var(--hart-accent-rgb, 0,212,170);
  --lg-glow-0: rgba(var(--lg-accent-rgb),.55);
  --lg-glow-1: rgba(var(--lg-accent-rgb),.26);
  --lg-glow-2: rgba(var(--lg-accent-rgb),.12);
  /* ── Deterministic STATE lights — one hue per real machine signal ── */
  --lg-listen-rgb: 0,224,194;     /* mic open / listening */
  --lg-think-rgb:  108,99,255;    /* AI computing (the reclaimed purple) */
  --lg-speak-rgb:  25,227,125;    /* TTS out */
  --lg-vision-rgb: 52,176,255;    /* camera/screen being read */
  --lg-blind-rgb:  120,120,132;   /* senses shut by the human */
  --lg-alert-rgb:  255,92,122;    /* errors / hard danger */
  /* ── Neutral ink ladder ── */
  --lg-heading: #F4F6FF; --lg-text: #E4E7F2; --lg-muted: #9AA2B8; --lg-faint: #646B82;
  /* ── Glass depth ladder — 4 honest elevations ── */
  --lg-1-bg: rgba(20,19,33,.42);  --lg-1-blur:14px; --lg-1-bd: rgba(255,255,255,.07);
  --lg-2-bg: rgba(18,17,30,.56);  --lg-2-blur:20px; --lg-2-bd: rgba(255,255,255,.10);
  --lg-3-bg: rgba(15,14,26,.70);  --lg-3-blur:26px; --lg-3-bd: rgba(255,255,255,.13);
  --lg-4-bg: rgba(12,11,22,.82);  --lg-4-blur:34px; --lg-4-bd: rgba(255,255,255,.16);
  --lg-sat: 1.4;
  --lg-spec: inset 0 1px 0 0 rgba(255,255,255,.14);
  --lg-sh-1: 0 2px 10px rgba(0,0,0,.30);
  --lg-sh-2: 0 8px 26px rgba(0,0,0,.42);
  --lg-sh-3: 0 18px 50px rgba(0,0,0,.52);
  --lg-sh-4: 0 30px 72px rgba(0,0,0,.58);
  /* ── Signature lit "presence ring" — orb, mic, eye, focus reuse these ── */
  --lg-ring-listen: 0 0 0 2px rgba(var(--lg-listen-rgb),.85), 0 0 0 7px rgba(var(--lg-listen-rgb),.20), 0 8px 30px rgba(var(--lg-listen-rgb),.38);
  --lg-ring-think:  0 0 0 2px rgba(var(--lg-think-rgb),.85),  0 0 0 8px rgba(var(--lg-think-rgb),.18),  0 8px 30px rgba(var(--lg-think-rgb),.36);
  --lg-ring-speak:  0 0 0 2px rgba(var(--lg-speak-rgb),.85),  0 0 0 7px rgba(var(--lg-speak-rgb),.18),  0 8px 28px rgba(var(--lg-speak-rgb),.34);
  --lg-ring-vision: 0 0 0 2px rgba(var(--lg-vision-rgb),.80), 0 0 0 6px rgba(var(--lg-vision-rgb),.18), 0 8px 26px rgba(var(--lg-vision-rgb),.32);
  /* ── Type ── */
  --lg-num: "tnum" 1;
  --lg-ls-display: -.4px; --lg-ls-title: -.1px; --lg-ls-micro: .6px;
  /* ── Motion roles ── */
  --lg-spring: var(--ds-ease-spring);
  --lg-glide:  var(--ds-ease-standard);
  --lg-enter:  cubic-bezier(.16,1,.3,1);
  --lg-exit:   cubic-bezier(.4,0,1,1);
  --lg-breathe:cubic-bezier(.37,0,.63,1);
  --t-micro:140ms; --t-fast:180ms; --t-move:220ms; --t-reveal:320ms; --t-ceremony:560ms;
  --lg-stagger:28ms;
  /* ── Shared geometry (single source; matches hartDesktop.js GRID/PAD) ── */
  --lg-grid: 92px; --lg-pad: 24px; --lg-snap-widget: 24px;
}
@media (prefers-reduced-motion: reduce){
  :root{--t-micro:0ms;--t-fast:0ms;--t-move:0ms;--t-reveal:0ms;--t-ceremony:0ms}
}
/* ── Canonical glass mixin (4 rungs) ── */
.lg-1,.lg-2,.lg-3,.lg-4{border:1px solid var(--lg-1-bd);box-shadow:var(--lg-spec),var(--lg-sh-1);
  background:var(--lg-1-bg);-webkit-backdrop-filter:blur(var(--lg-1-blur)) saturate(var(--lg-sat));backdrop-filter:blur(var(--lg-1-blur)) saturate(var(--lg-sat))}
.lg-2{background:var(--lg-2-bg);border-color:var(--lg-2-bd);box-shadow:var(--lg-spec),var(--lg-sh-2);-webkit-backdrop-filter:blur(var(--lg-2-blur)) saturate(var(--lg-sat));backdrop-filter:blur(var(--lg-2-blur)) saturate(var(--lg-sat))}
.lg-3{background:var(--lg-3-bg);border-color:var(--lg-3-bd);box-shadow:var(--lg-spec),var(--lg-sh-3);-webkit-backdrop-filter:blur(var(--lg-3-blur)) saturate(var(--lg-sat));backdrop-filter:blur(var(--lg-3-blur)) saturate(var(--lg-sat))}
.lg-4{background:var(--lg-4-bg);border-color:var(--lg-4-bd);box-shadow:var(--lg-spec),var(--lg-sh-4);-webkit-backdrop-filter:blur(var(--lg-4-blur)) saturate(var(--lg-sat));backdrop-filter:blur(var(--lg-4-blur)) saturate(var(--lg-sat))}
.lg-num{font-variant-numeric:tabular-nums;font-feature-settings:var(--lg-num)}

/* ═══ DIMENSION 1 — THE ORB (deterministic lit voice control) ═══
   #hart-hero-orbwrap[data-orb-state] (hartHero.js writes it). Supersede the
   legacy RED .listening glow (:1157) — later source wins, do NOT delete it. */
.hart-hero-orbwrap{transition:transform .25s var(--lg-spring),box-shadow var(--t-move) var(--lg-breathe)}
.hart-hero-orbwrap::after{content:'';position:absolute;inset:-6px;border-radius:50%;pointer-events:none;
  opacity:0;transition:opacity var(--t-reveal) var(--lg-enter),box-shadow var(--t-move)}
.hart-hero-orbwrap[data-orb-state="listening"]::after{opacity:1;box-shadow:var(--lg-ring-listen);animation:lg-breathe-ring 2.2s var(--lg-breathe) infinite}
.hart-hero-orbwrap[data-orb-state="speaking"]::after {opacity:1;box-shadow:var(--lg-ring-speak)}
.hart-hero-orbwrap[data-orb-state="thinking"]::after {opacity:1;box-shadow:var(--lg-ring-think)}
@keyframes lg-breathe-ring{0%,100%{transform:scale(1);opacity:.85}50%{transform:scale(1.03);opacity:1}}
/* Supersede the legacy RED listening glow so it never shows: */
.hart-hero-orbwrap.listening{box-shadow:none}
/* Thinking comet — conic sweep masked to the ring */
.hart-hero-orbwrap[data-orb-state="thinking"]::before{content:'';position:absolute;inset:-6px;border-radius:50%;pointer-events:none;
  background:conic-gradient(from 0deg,transparent 0 78%,rgba(var(--lg-think-rgb),.9) 90%,transparent 100%);
  -webkit-mask:radial-gradient(closest-side,transparent calc(100% - 5px),#000 calc(100% - 4px));
          mask:radial-gradient(closest-side,transparent calc(100% - 5px),#000 calc(100% - 4px));
  animation:lg-comet 1.4s linear infinite}
@keyframes lg-comet{to{transform:rotate(360deg)}}
/* Press ripple from click point */
.lg-orb-ripple{position:absolute;border-radius:50%;pointer-events:none;background:radial-gradient(circle,rgba(var(--lg-accent-rgb),.35),transparent 70%);animation:lg-ripple .45s var(--lg-exit) forwards}
@keyframes lg-ripple{from{transform:scale(.2);opacity:.7}to{transform:scale(2.4);opacity:0}}
html.a11y-rmotion .hart-hero-orbwrap[data-orb-state="thinking"]::before,
html.a11y-rmotion .hart-hero-orbwrap[data-orb-state="listening"]::after{animation:none}

/* ═══ DIMENSION 2 — THE SENSORY POD (floating, draggable, grid-snapping) ═══
   Supersede the legacy senses block (:1327-1371). The cluster inherits .lg-1
   (class added in markup). hartSenses.js writes .dragging/.settle/[data-edge]/
   #hart-senses-btn.is-sensing; mic .listening already exists (restyle only). */
.hart-senses-cluster{padding:6px;gap:6px}
.hart-senses.dragging .hart-senses-cluster{transform:scale(1.04);box-shadow:var(--lg-spec),var(--lg-sh-3)}
.hart-senses.settle .hart-senses-cluster{animation:lg-settle .34s var(--lg-spring)}
@keyframes lg-settle{0%{transform:scale(1.06)}100%{transform:scale(1)}}
/* FIX A (drag-affordance discipline): the grip appears ONLY while dragging (hidden
   at rest AND on hover) - the cluster body stays the drag hit-area, grip is visual
   only. opacity (not display) hides it, so width + pointer-events are preserved. */
.hart-senses-grip{cursor:grab}
.hart-senses.dragging .hart-senses-grip{cursor:grabbing;opacity:1;color:var(--hart-text);background:rgba(255,255,255,0.06)}
/* EYE — deterministic 3-state (was only .off red) */
.hart-senses-btn.is-sensing{background:rgba(var(--lg-vision-rgb),.16);border-color:rgb(var(--lg-vision-rgb));box-shadow:var(--lg-ring-vision)}
.hart-senses-btn.is-sensing .mi{color:rgb(var(--lg-vision-rgb));animation:lg-pulse 2.4s var(--lg-breathe) infinite}
.hart-senses-btn.off{background:rgba(var(--lg-blind-rgb),.20);border-color:rgb(var(--lg-blind-rgb))}
.hart-senses-btn.off .mi{color:rgb(var(--lg-blind-rgb))}
/* MIC — listening cyan (supersede the legacy red .hart-senses-mic.listening :1343) */
.hart-senses-mic.listening{background:rgba(var(--lg-listen-rgb),.18);border-color:rgb(var(--lg-listen-rgb));box-shadow:var(--lg-ring-listen)}
.hart-senses-mic.listening .mi{color:rgb(var(--lg-listen-rgb))}
@keyframes lg-pulse{0%,100%{opacity:.7}50%{opacity:1}}
/* Edge-aware proof popover: opens AWAY from the nearest screen edge */
.hart-senses[data-edge~="b"] .hart-senses-panel{bottom:56px;top:auto}
.hart-senses[data-edge~="t"] .hart-senses-panel{top:56px;bottom:auto}
.hart-senses[data-edge~="r"] .hart-senses-panel{right:0;left:auto}
.hart-senses[data-edge~="l"] .hart-senses-panel{left:0;right:auto}
/* Snap-grid ghost while dragging */
.lg-senses-ghost{position:fixed;inset:0;z-index:8090;pointer-events:none;opacity:0;transition:opacity var(--t-reveal);
  background-image:radial-gradient(rgba(var(--lg-accent-rgb),.16) 1px,transparent 1px);background-size:24px 24px}
.lg-senses-ghost.show{opacity:1}
html.a11y-rmotion .hart-senses-btn.is-sensing .mi{animation:none}

/* ═══ DIMENSION 3 — CONTEXTUAL / DETERMINISTIC VISIBILITY ENGINE ═══
   hartVisibility.js is the sole writer of <html data-*> (data-multiws owned by
   hartWorkspaces.js). All show/hide is declarative CSS on EXISTING markup. */
.hart-hero-chips{transition:opacity var(--t-move) var(--lg-enter),transform var(--t-move) var(--lg-enter)}
html[data-busy="1"] .hart-hero-chips,html[data-panels="1"] .hart-hero-chips,html[data-typing="1"] .hart-hero-chips{opacity:0;transform:translateY(6px) scale(.98);pointer-events:none}
/* Sensory pod: SAFETY control — dims when idle, NEVER hides; full while sensing/voice */
.hart-senses{transition:opacity var(--t-reveal) var(--lg-glide)}
html[data-idle="1"] .hart-senses{opacity:.55}
html[data-voice="1"] .hart-senses,html[data-blind="1"] .hart-senses{opacity:1}
/* Pager: hidden only on a pristine, empty desktop; reveals as soon as the virtual-
   desktop feature is USABLE — any window is open OR you've navigated off desktop 1
   (data-multiws set by hartWorkspaces). Seeded data-multiws="0" in the <html> markup
   so the rule matches at first paint (an ABSENT attr would not) — no reveal FOUC. */
.hart-ws-switcher{transition:opacity var(--t-move) var(--lg-enter),transform var(--t-move) var(--lg-enter)}
html[data-multiws="0"] .hart-ws-switcher{opacity:0;transform:translate(-50%,8px);pointer-events:none}
/* Ambient wash subtly shifts toward the active state (the WHOLE room reacts).
   thinking / voice / speaking each tint the room so the active state is felt
   peripherally — speaking gets parity with the other two (it previously had no
   <html>-level consumer, so the "AI is talking" signal lit nothing). */
html[data-thinking="1"] .hart-ambient{filter:blur(64px) saturate(150%) hue-rotate(16deg);transition:filter var(--t-reveal)}
html[data-voice="1"] .hart-ambient{filter:blur(64px) saturate(160%) hue-rotate(-10deg);transition:filter var(--t-reveal)}
html[data-speaking="1"] .hart-ambient{filter:blur(64px) saturate(155%) hue-rotate(-26deg);transition:filter var(--t-reveal)}
/* While HART speaks, the hevolve "live" pip in the spine glows the TTS-out green so
   the speaking state has a deterministic on-screen cue (not just the orb canvas). */
html[data-speaking="1"] .hart-hero-hevolve{opacity:.9}
html[data-speaking="1"] .hart-hero-hevolve .dot{background:rgb(var(--lg-speak-rgb));box-shadow:0 0 0 3px rgba(var(--lg-speak-rgb),.22)}
/* Agents-running signal: when >=1 agent is live, the top-bar agent cluster reads
   as ACTIVE (chip dots gain a soft live-glow); with none, it stays muted. This
   makes data-agents actionable (it had no consumer before). */
html[data-agents="1"] .top-bar-center .agent-chip .dot{box-shadow:0 0 0 3px rgba(0,230,118,.22)}
html[data-agents="0"] .top-bar-center{opacity:.72}
/* Offline signal: when the network is down, fade + desaturate the agent-status
   cluster (the live network surface) so "offline" is visible, not silent. */
html[data-online="0"] .top-bar-center{opacity:.5;filter:grayscale(.7);transition:opacity var(--t-reveal),filter var(--t-reveal)}

/* ═══ DIMENSION 4 — DESKTOP ICONS (sort + marquee + drop-cell) ═══
   Re-skin .desktop-icon (:1207); supersede flat-purple .selected (:1211).
   hartDesktop.js writes .arranging on #hart-desktop + .lg-drop-cell/.lg-marquee. */
.desktop-icon.selected{background:rgba(var(--lg-accent-rgb),.22);outline:1px solid rgba(var(--lg-accent-rgb),.55);box-shadow:0 8px 30px rgba(var(--lg-accent-rgb),.30)}
.desktop-icon.dragging{transform:scale(1.06);box-shadow:var(--lg-sh-3);z-index:60}
.hart-desktop::before{content:'';position:absolute;inset:0;opacity:0;pointer-events:none;transition:opacity var(--t-fast);
  background-image:radial-gradient(rgba(var(--lg-accent-rgb),.16) 1.5px,transparent 1.5px);background-size:var(--lg-grid) var(--lg-grid);background-position:var(--lg-pad) var(--lg-pad)}
.hart-desktop.arranging::before{opacity:1}
.lg-drop-cell{position:absolute;width:84px;height:84px;border-radius:14px;pointer-events:none;border:2px dashed rgba(var(--lg-accent-rgb),.6);background:rgba(var(--lg-accent-rgb),.08);transition:left var(--t-fast) var(--lg-glide),top var(--t-fast) var(--lg-glide)}
.lg-marquee{position:fixed;z-index:55;pointer-events:none;border:1px solid rgba(var(--lg-accent-rgb),.7);background:rgba(var(--lg-accent-rgb),.10);border-radius:6px}

/* ═══ DIMENSION 5 — THE WORKSPACE PAGER (segmented, occupancy-aware, thumb) ═══
   Supersede the .hart-ws-dot block (:1251). .hart-ws-switcher inherits .lg-2
   (class already on markup :1793). hartWorkspaces.js writes the pager DOM. */
.hart-ws-switcher{padding:3px;gap:2px;height:30px;align-items:center}
.hart-pager-thumb{position:absolute;top:3px;left:3px;height:24px;border-radius:var(--ds-radius-full);background:rgba(var(--lg-accent-rgb),.92);box-shadow:0 2px 10px rgba(var(--lg-accent-rgb),.4);transition:transform var(--t-move) var(--lg-spring),width var(--t-move) var(--lg-spring);z-index:0}
.hart-pager-seg{position:relative;z-index:1;min-width:34px;height:24px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;border:none;background:transparent;cursor:pointer;color:var(--lg-muted);border-radius:var(--ds-radius-full);transition:color var(--t-fast)}
.hart-pager-seg .hps-n{font-size:11px;font-weight:700;line-height:1}
.hart-pager-seg .hps-occ{display:flex;gap:2px;height:3px}
.hart-pager-seg .hps-occ i{width:3px;height:3px;border-radius:50%;background:currentColor;opacity:.7}
.hart-pager-seg.empty .hps-occ{opacity:.35}
.hart-pager-seg:hover{color:var(--lg-text)}
.hart-pager-seg.active{color:var(--hart-on-accent)}
@media(prefers-reduced-motion:reduce){.hart-pager-thumb{transition:none}}

/* ═══ DIMENSION 7 — OFFLINE / EMPTY STATES (designed, never naive) ═══
   hartStates.js builds .lg-empty / .lg-empty-{loading,offline,empty}. */
.lg-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:var(--ds-space-3);padding:var(--ds-space-12) var(--ds-space-6);min-height:240px;animation:lg-empty-in var(--t-reveal) var(--lg-enter)}
@keyframes lg-empty-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.lg-empty-disc{width:56px;height:56px;border-radius:var(--ds-radius-lg);display:flex;align-items:center;justify-content:center;background:var(--lg-1-bg);border:1px solid var(--lg-1-bd);box-shadow:var(--lg-spec)}
.lg-empty-disc .mi{font-size:28px;color:var(--lg-muted)}
.lg-empty-offline .lg-empty-disc .mi{color:rgb(var(--lg-blind-rgb));animation:lg-empty-breathe 3s var(--lg-breathe) infinite}
@keyframes lg-empty-breathe{0%,100%{opacity:.6;transform:scale(1)}50%{opacity:1;transform:scale(1.06)}}
.lg-empty-title{font-size:15px;font-weight:600;color:var(--lg-heading);letter-spacing:-.1px}
.lg-empty-msg{font-size:13px;line-height:1.5;color:var(--lg-muted);max-width:340px}
.lg-empty-retry{margin-top:var(--ds-space-1)}
html.a11y-rmotion .lg-empty-offline .lg-empty-disc .mi{animation:none}

/* ═══ DIMENSION 6 — VISUAL DESIGN LANGUAGE (cohesion pass) ═══
   Chips spring + tabular numerals on clock/pager/counters. */
.hart-hero-chip{transition:background .18s,transform .18s var(--lg-spring),box-shadow .18s}
.hart-hero-chip:hover{transform:translateY(-2px);box-shadow:var(--lg-sh-2)}
.hart-hero-chip:active{transform:scale(.97)}
.top-bar-right .clock{font-variant-numeric:tabular-nums;font-feature-settings:var(--lg-num)}
'''
        # Potato (no-blur) variant of the .lg-1..4 mixin + state surfaces — mirrors
        # the .glass branch (:1414) + _CSS_POTATO_OVERRIDE (:1097): drop the
        # backdrop-filter and raise bg opacity so the chrome stays legible without
        # the GPU-costly blur on low-end hardware.
        if is_potato:
            _CSS_LIVING_GLASS += (
                '.lg-1,.lg-2,.lg-3,.lg-4{backdrop-filter:none;-webkit-backdrop-filter:none}'
                '.lg-1{background:rgba(20,19,33,.92)}'
                '.lg-2{background:rgba(18,17,30,.94)}'
                '.lg-3{background:rgba(15,14,26,.95)}'
                '.lg-4{background:rgba(12,11,22,.96)}'
                '.lg-senses-ghost,.hart-desktop::before{display:none}'
            )

        # ── Boot lock overlay (#166: FOUC + security) ─────────────────────────
        # The lock is a per-user SHELL lock (hartSessionUI.js / window.HartLock),
        # persisted in the SAME server-backed session blob the JS reads
        # (shell_session.json -> key `lock_pw_hash`). When a password IS set the
        # shell must boot LOCKED, and the overlay has to COVER the desktop from
        # the very FIRST paint — otherwise the desktop paints for a frame before
        # hartSessionUI's deferred JS can add `.active` (the reported FOUC / an
        # information leak of the desktop behind the lock). So we seed `.active`
        # into the served #lock-screen markup HERE (frame 1), reading the exact
        # same blob + key the JS lock owns — one source of truth, no parallel
        # lock state. hartSessionUI.js then focuses the field and drives unlock,
        # which removes `.active` and reveals the desktop. No password set (fresh
        # install) => not seeded => normal boot (the first-run setup prompt in
        # hartSessionUI still offers to create one).
        boot_locked = False
        try:
            _ss_path = os.path.join(self._data_dir, 'shell_session.json')
            if os.path.isfile(_ss_path):
                with open(_ss_path, 'r') as _sf:
                    _ss_blob = json.load(_sf)
                boot_locked = bool(isinstance(_ss_blob, dict) and _ss_blob.get('lock_pw_hash'))
        except Exception:
            boot_locked = False
        lock_boot_class = ' active' if boot_locked else ''

        return f'''<!DOCTYPE html>
<html lang="en" class="{a11y_cls}" data-multiws="0"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>HART OS</title>
<script>if(navigator.onLine){{var _l=document.createElement('link');_l.rel='stylesheet';_l.href='https://fonts.googleapis.com/icon?family=Material+Icons+Round';document.head.appendChild(_l);}}</script>
<style>/* Icons: the BUNDLED /shell/static/MaterialSymbolsRounded.woff2 @font-face below is authoritative (every glyph, fully offline). This CDN <link> is progressive-enhancement ONLY (online round variant). */</style>
<style>
{css_vars}
{accent_rgb_css}
{a11y_fontscale}
*{{margin:0;padding:0;box-sizing:border-box}}
::selection{{background:var(--hart-accent);color:#fff}}
html,body{{width:100%;height:100%;overflow:hidden;font-family:var(--hart-font-family),monospace;
  font-size:var(--hart-font-size);font-weight:var(--hart-font-weight);color:var(--hart-text)}}

/* ── Wallpaper ── */
.wallpaper{{position:fixed;inset:0;z-index:0;background:{wp_css}}}

/* ── Glass mixin (perf-aware) ── */
.glass{{background:var(--hart-glass-bg);
  {'backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation));-webkit-backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation));' if not is_potato else '/* blur disabled for performance */'}
  border:1px solid var(--hart-glass-border);border-top-color:rgba(255,255,255,0.16);
  box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.08),inset 0 -1px 0 0 rgba(0,0,0,0.18);
  border-radius:var(--hart-radius)}}

/* ── Top Bar ── */
.top-bar{{position:fixed;top:0;left:0;right:0;height:var(--hart-topbar-height);z-index:1000;
  display:flex;align-items:center;padding:0 12px;gap:8px;border-radius:0;
  border-bottom:1px solid var(--hart-glass-border);border-top:0}}
.top-bar .start-btn{{display:flex;align-items:center;gap:6px;padding:4px 12px;
  border-radius:8px;cursor:pointer;transition:background var(--hart-anim-speed);
  font-weight:var(--hart-heading-weight);font-size:13px;user-select:none}}
.top-bar .start-btn:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.08))}}
.top-bar .start-btn .mi{{font-size:20px;color:var(--hart-accent)}}
.top-bar-center{{flex:1;display:flex;align-items:center;gap:6px;padding:0 12px;
  font-size:12px;color:var(--hart-muted);overflow:hidden}}
.top-bar-center .agent-chip{{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;
  border-radius:10px;background:var(--hart-surface,rgba(255,255,255,0.05));font-size:11px}}
.top-bar-center .agent-chip .dot{{width:6px;height:6px;border-radius:50%;background:var(--hart-active)}}
.top-bar-right{{display:flex;align-items:center;gap:8px}}
.top-bar-right .tray-btn{{width:32px;height:32px;display:flex;align-items:center;justify-content:center;
  border-radius:8px;cursor:pointer;transition:background var(--hart-anim-speed);position:relative}}
.top-bar-right .tray-btn:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.08))}}
.top-bar-right .tray-btn .mi{{font-size:var(--hart-icon-size);color:var(--hart-muted)}}
.top-bar-right .clock{{font-size:12px;font-weight:500;padding:0 8px}}
.badge{{position:absolute;top:2px;right:2px;width:8px;height:8px;border-radius:50%;background:var(--hart-error)}}

/* ── Panel Container ── */
.panel-container{{position:fixed;top:var(--hart-topbar-height);left:0;right:0;
  bottom:44px;z-index:100;pointer-events:none}}
.panel-container>*{{pointer-events:auto}}

/* ── Glass Panel (floating window) ── */
.panel{{position:absolute;display:flex;flex-direction:column;min-width:320px;min-height:240px;
  {'box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.08),0 1px 1px rgba(0,0,0,0.22),0 8px 32px rgba(0,0,0,0.38);' if not is_potato else 'box-shadow:0 2px 8px rgba(0,0,0,0.3);'}overflow:hidden;{'transition:box-shadow var(--hart-anim-speed)' if not is_potato else 'transition:none'}}}
.panel.focused{{{'box-shadow:inset 0 1px 0 0 rgba(255,255,255,0.10),0 2px 4px rgba(0,0,0,0.28),0 16px 56px rgba(0,0,0,0.48);' if not is_potato else 'box-shadow:0 3px 12px rgba(0,0,0,0.4);'}z-index:999}}
.panel-titlebar{{height:var(--hart-titlebar-height);display:flex;align-items:center;padding:0 8px;
  gap:6px;cursor:grab;user-select:none;flex-shrink:0;border-bottom:1px solid var(--hart-glass-border)}}
.panel-titlebar:active{{cursor:grabbing}}
.panel-titlebar .mi{{font-size:16px;color:var(--hart-accent);flex-shrink:0}}
.panel-titlebar .title{{flex:1;font-size:12px;font-weight:500;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.panel-titlebar .ctrl{{display:flex;gap:4px}}
.panel-titlebar .ctrl span{{width:24px;height:24px;display:flex;align-items:center;justify-content:center;
  border-radius:6px;cursor:pointer;font-size:14px;color:var(--hart-text);background:rgba(255,255,255,0.06);
  transition:background var(--hart-anim-speed),color var(--hart-anim-speed)}}
/* Window controls (close/min/max) get a SOLID rest background + a crisp NEUTRAL glyph
   instead of the low-contrast teal accent — otherwise they read as transparent/clumsy
   floating icons on the glass (steward, real-HW). Close hover = red with a white X. */
.panel-titlebar .ctrl span .mi{{color:inherit}}
.panel-titlebar .ctrl span:hover{{background:rgba(255,255,255,0.16)}}
.panel-titlebar .ctrl .close:hover{{background:var(--hart-error);color:#fff}}
.panel-body{{flex:1;overflow:hidden;position:relative}}
.panel-body iframe{{width:100%;height:100%;border:none;background:transparent}}
.panel-body .native-content{{padding:16px;overflow-y:auto;height:100%;font-size:13px}}
.panel-resize{{position:absolute;right:0;bottom:0;width:16px;height:16px;cursor:nwse-resize}}

/* ── Start Menu ── */
.start-menu{{position:fixed;bottom:calc(var(--hart-topbar-height));left:8px;
  width:720px;max-height:calc(100vh - var(--hart-topbar-height) - 24px);
  z-index:2000;padding:16px;display:none;flex-direction:column;overflow:hidden}}
.start-menu.open{{display:flex}}
.start-search{{width:100%;padding:8px 12px;border-radius:10px;border:1px solid var(--hart-glass-border);
  background:var(--hart-surface,rgba(255,255,255,0.05));color:var(--hart-text);
  font-family:var(--ds-font-body);font-size:13px;outline:none;margin-bottom:12px}}
.start-search:focus{{border-color:var(--hart-accent)}}
.start-scroll{{flex:1;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;
  scrollbar-color:var(--hart-muted) transparent}}
.start-group{{margin-bottom:12px}}
.start-group-label{{font-size:10px;text-transform:uppercase;letter-spacing:1.5px;
  color:var(--hart-muted);padding:4px 4px 6px;font-weight:600}}
.start-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:4px}}
.start-item{{display:flex;flex-direction:column;align-items:center;padding:10px 4px;
  border-radius:10px;cursor:pointer;transition:background var(--hart-anim-speed);
  text-align:center;gap:4px;user-select:none}}
.start-item:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.08))}}
.start-item .mi{{font-size:24px;color:var(--hart-accent)}}
.start-item .label{{font-size:11px;line-height:1.2;opacity:0.85}}
.start-divider{{border-top:1px solid var(--hart-glass-border);margin:8px 0}}
.start-footer{{display:flex;justify-content:center;gap:16px;padding-top:8px;border-top:1px solid var(--hart-glass-border)}}
.start-footer .power-btn{{display:flex;align-items:center;gap:4px;padding:6px 12px;
  border-radius:8px;cursor:pointer;font-size:12px;transition:background var(--hart-anim-speed)}}
.start-footer .power-btn:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.08))}}
.start-footer .power-btn .mi{{font-size:16px}}

/* ── Agent Pill (collapsed floating bubble) ── */
.agent-pill{{position:fixed;bottom:56px;right:16px;z-index:1500;display:flex;
  align-items:center;gap:8px;padding:8px 14px;cursor:pointer;
  transition:all var(--hart-anim-speed);max-width:360px}}
.agent-pill:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,0.3)}}
.agent-pill.expanded{{max-width:400px;padding:12px}}
.agent-pill.hidden{{display:none}}
.agent-pill .mi{{font-size:20px;color:var(--hart-accent);flex-shrink:0}}
.agent-pill input{{flex:1;background:transparent;border:none;color:var(--hart-text);
  font-family:var(--ds-font-body);font-size:13px;outline:none;min-width:0}}
.agent-pill input::placeholder{{color:var(--hart-muted)}}
.agent-response{{font-size:12px;color:var(--hart-muted);padding-top:6px;
  border-top:1px solid var(--hart-glass-border);display:none;width:100%}}
.agent-response.visible{{display:block}}

/* ── Floating Assistant Chat Panel ── */
.assistant-chat{{position:fixed;bottom:56px;right:16px;z-index:1600;
  width:380px;height:520px;display:none;flex-direction:column;
  border-radius:var(--hart-radius);overflow:hidden;
  resize:both;min-width:320px;min-height:400px;max-width:600px;max-height:80vh}}
.assistant-chat.open{{display:flex}}
.assistant-chat .ac-header{{display:flex;align-items:center;gap:8px;
  padding:10px 14px;cursor:grab;user-select:none;
  border-bottom:1px solid var(--hart-glass-border);flex-shrink:0}}
.assistant-chat .ac-header:active{{cursor:grabbing}}
.assistant-chat .ac-title{{flex:1;font-size:13px;font-weight:500}}
.assistant-chat .ac-btn{{background:none;border:none;color:var(--hart-muted);
  cursor:pointer;padding:2px;font-size:18px}}
.assistant-chat .ac-btn:hover{{color:var(--hart-text)}}
.assistant-chat .ac-caps{{display:flex;gap:6px;padding:8px 14px;overflow-x:auto;
  flex-shrink:0;border-bottom:1px solid var(--hart-glass-border)}}
.assistant-chat .ac-cap{{display:flex;align-items:center;gap:4px;
  padding:4px 10px;border-radius:12px;font-size:11px;white-space:nowrap;
  background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);
  cursor:pointer;transition:background 120ms}}
.assistant-chat .ac-cap:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.1))}}
.assistant-chat .ac-cap.active{{background:var(--hart-accent);color:#fff;border-color:var(--hart-accent)}}
.assistant-chat .ac-cap .mi{{font-size:14px}}
.assistant-chat .ac-messages{{flex:1;overflow-y:auto;padding:12px 14px;
  display:flex;flex-direction:column;gap:8px}}
.assistant-chat .ac-msg{{max-width:85%;padding:8px 12px;border-radius:12px;
  font-size:13px;line-height:1.4;word-break:break-word}}
.assistant-chat .ac-msg.user{{align-self:flex-end;
  background:var(--hart-accent);color:#fff;border-bottom-right-radius:4px}}
.assistant-chat .ac-msg.assistant{{align-self:flex-start;
  background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);
  border-bottom-left-radius:4px}}
.assistant-chat .ac-msg.typing{{opacity:0.6;font-style:italic}}
.assistant-chat .ac-input-row{{display:flex;align-items:center;gap:6px;
  padding:8px 10px;border-top:1px solid var(--hart-glass-border);flex-shrink:0}}
.assistant-chat .ac-input{{flex:1;background:transparent;border:1px solid var(--hart-glass-border);
  border-radius:20px;padding:8px 14px;color:var(--hart-text);
  font-family:var(--ds-font-body);font-size:13px;outline:none;resize:none}}
.assistant-chat .ac-input:focus{{border-color:var(--hart-accent)}}
.assistant-chat .ac-input::placeholder{{color:var(--hart-muted)}}
.assistant-chat .ac-send{{background:var(--hart-accent);border:none;
  width:32px;height:32px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;cursor:pointer;flex-shrink:0;transition:opacity 120ms}}
.assistant-chat .ac-send:hover{{opacity:0.85}}
.assistant-chat .ac-send .mi{{font-size:16px;color:#fff}}

/* ── Context Menu ── */
.ctx-menu{{position:fixed;z-index:3000;min-width:180px;padding:4px;
  box-shadow:0 8px 24px rgba(0,0,0,0.5);font-size:12px}}
.ctx-menu-item{{display:flex;align-items:center;gap:8px;padding:6px 10px;
  border-radius:6px;cursor:pointer;transition:background 100ms}}
.ctx-menu-item:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.1))}}
.ctx-menu-item .mi{{font-size:16px;color:var(--hart-muted)}}
.ctx-menu-sep{{border-top:1px solid var(--hart-glass-border);margin:4px 0}}

/* ── Lock Screen ── */
/* OPAQUE base (--hart-background) UNDER the translucent tint: a lock / password
   takeover must fully cover the desktop. backdrop-filter is unreliable on the
   kiosk WebKitGTK, so relying on a 70% tint + blur let the hero search bar bleed
   THROUGH — the "Create a password" card visually overlapped the search hint.
   An opaque solid layer first guarantees nothing behind it shows, on every
   renderer; the blur stays a progressive enhancement on top. */
.lock-screen{{position:fixed;inset:0;z-index:9999;display:none;align-items:center;
  justify-content:center;flex-direction:column;gap:16px;
  background:var(--hart-background,#0F0E17);
  background:linear-gradient(rgba(7,6,15,{'0.82),rgba(7,6,15,0.82)),var(--hart-background,#0F0E17);backdrop-filter:blur(24px)' if not is_potato else '0.97),rgba(7,6,15,0.97)),var(--hart-background,#0F0E17)'}}}
.lock-screen.active{{display:flex}}
.lock-clock{{font-size:64px;font-weight:300}}
.lock-date{{font-size:16px;color:var(--hart-muted)}}
.lock-input{{padding:10px 16px;border-radius:12px;border:1px solid var(--hart-glass-border);
  background:var(--hart-glass-bg);color:var(--hart-text);font-size:14px;
  font-family:var(--ds-font-body);outline:none;width:280px;text-align:center}}
.lock-status{{font-size:12px;color:var(--hart-muted)}}
.lock-brand{{display:flex;align-items:center;gap:10px;margin-bottom:8px;opacity:.92}}
.lock-brand img{{width:30px;height:30px;filter:drop-shadow(0 2px 10px rgba(0,212,170,.4))}}
.lock-brand span{{font-size:13px;letter-spacing:2.5px;font-weight:600;opacity:.8}}
.lock-screen.setup .lock-clock,.lock-screen.setup .lock-date{{display:none}}
/* ── Desktop widgets (live clock + system) ── */
.hart-widgets{{position:fixed;top:calc(var(--hart-topbar-height,40px) + 18px);right:16px;z-index:30;
  display:flex;flex-direction:column;gap:12px;width:222px}}
.hart-widget{{background:var(--hart-glass-bg);border:1px solid var(--hart-glass-border);border-radius:16px;
  padding:14px 16px;{'backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation));' if not is_potato else ''}
  box-shadow:0 8px 30px rgba(0,0,0,.28)}}
.hw-clock{{text-align:center}}
.hw-clock-time{{font-size:30px;font-weight:300;letter-spacing:.5px;color:var(--hart-text);font-variant-numeric:tabular-nums}}
.hw-clock-date{{font-size:12px;color:var(--hart-muted);margin-top:2px}}
.hw-title{{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:var(--hart-muted);margin-bottom:6px}}
.hw-row{{display:flex;justify-content:space-between;font-size:12px;color:var(--hart-text);margin-top:7px}}
.hw-val{{color:var(--hart-accent);font-variant-numeric:tabular-nums}}
.hw-bar{{height:5px;border-radius:3px;background:var(--hart-surface);overflow:hidden;margin-top:3px}}
.hw-bar>i{{display:block;height:100%;background:var(--hart-accent);border-radius:3px;transition:width .5s}}
/* ── Microanimations: buttery hover lifts + soft open transitions (scoped to
   NON-draggable elements so icon/window drag stays instant) ── */
.start-item,.ctx-menu-item,.hart-hero-chip,.taskbar-chip,.tray-btn,.start-btn,.power-btn,.start-app{{
  transition:transform .16s cubic-bezier(.22,1,.36,1),box-shadow .16s ease,background .16s ease,filter .16s ease}}
.start-item:hover,.taskbar-chip:hover,.start-app:hover{{transform:translateY(-2px)}}
.tray-btn:hover,.start-btn:hover,.power-btn:hover{{transform:scale(1.08)}}
.ctx-menu-item:hover{{transform:translateX(2px)}}
.hart-widget{{transition:transform .2s cubic-bezier(.22,1,.36,1),box-shadow .2s ease}}
.hart-widget:hover{{transform:translateY(-2px);box-shadow:0 14px 40px rgba(0,0,0,.34)}}
@keyframes hart-fade-in{{from{{opacity:0}}to{{opacity:1}}}}
.panel{{animation:hart-fade-in .18s ease}}
@media(prefers-reduced-motion:reduce){{.panel{{animation:none}}
  .start-item,.ctx-menu-item,.hart-hero-chip,.taskbar-chip,.tray-btn,.start-btn,.power-btn,.start-app,.hart-widget{{transition:none}}}}

/* ── Scrollbar ── */
::-webkit-scrollbar{{width:6px}}
::-webkit-scrollbar-track{{background:transparent}}
::-webkit-scrollbar-thumb{{background:var(--hart-muted);border-radius:3px}}
::-webkit-scrollbar-thumb:hover{{background:var(--hart-accent)}}

/* ── Taskbar ── */
.taskbar{{position:fixed;bottom:0;left:0;right:0;height:44px;z-index:8000;
  display:flex;gap:2px;padding:0 8px;align-items:center;border-radius:0;
  border-top:1px solid var(--hart-glass-border)}}
.taskbar-chip{{height:34px;padding:0 12px;display:flex;align-items:center;gap:4px;
  border-radius:8px;cursor:pointer;{'transition:background 0.15s;' if not is_potato else 'transition:none;'}
  font-size:12px;user-select:none;border:1px solid transparent}}
.taskbar-chip:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.08))}}
.taskbar-chip.active{{border-bottom:2px solid var(--hart-accent);
  background:var(--hart-surface,rgba(255,255,255,0.05))}}
.taskbar-chip .mi{{font-size:16px;color:var(--hart-accent)}}
.taskbar-chip .chip-label{{max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}

/* ── Notification Toasts ── */
.toast-container{{position:fixed;top:calc(var(--hart-topbar-height) + 12px);right:16px;
  display:flex;flex-direction:column;gap:8px;z-index:9500;pointer-events:none}}
.toast{{padding:12px 16px;border-radius:12px;pointer-events:auto;cursor:pointer;
  max-width:340px;font-size:12px;{'animation:slideInRight 0.3s ease-out,fadeOutToast 0.3s ease-in 4.7s forwards' if not is_potato else ''}}}
.toast:hover{{opacity:1!important}}
{_CSS_SLIDE_IN if not is_potato else ''}
{_CSS_FADE_OUT if not is_potato else ''}

/* ── Voice Recording ── */
.mic-btn{{cursor:pointer}}
.mic-btn.recording{{color:var(--hart-error)!important;{'animation:pulse 1s infinite' if not is_potato else ''}}}
{_CSS_PULSE if not is_potato else ''}

/* ── Animations ── */
{_CSS_ANIMATIONS if not is_potato else _CSS_NO_ANIMATIONS}
{_CSS_DESIGN_SYSTEM}
{_CSS_HERO}
{_CSS_DESKTOP}
{_CSS_LIVING_GLASS}
{_CSS_POTATO_OVERRIDE if is_potato else ''}
</style>
</head>
<body class="{gpu_body_class}{flat_body_class}">
<!-- Hevolve brand boot splash (Lottie). Inline styles: this HTML is inside an
     f-string, so a CSS block would need brace-escaping; the overlay is a single
     element + hartBootSplash.js drives the fade, so inline is cleaner here. -->
<div id="hart-boot" aria-hidden="true" style="position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;background:#0F0E17;transition:opacity .6s ease"><div id="hart-boot-lottie" style="width:min(46vw,360px);height:min(64vw,497px)"></div></div>
<div class="wallpaper"></div>
{'<div class="hart-ambient" aria-hidden="true"></div>' if emit_ambient else ''}{'<div class="hart-grain" aria-hidden="true"></div>' if not is_potato else ''}
<div class="hart-vignette" aria-hidden="true"></div>
<!-- Desktop icon layer (drag-drop apps); populated by hartDesktop.js -->
<div class="hart-desktop" id="hart-desktop" aria-label="Desktop icons"></div>
<div class="hart-widgets" id="hart-widgets" aria-label="Desktop widgets"></div>
<a href="#panels" class="skip-link">Skip to content</a>

<!-- HART OS hero — voice-first command center (the desktop centerpiece). The
     orb canvas below is the SAME #hart-voice-orb driven by initHartOrb; the bar
     fuses search + agent dispatch + the voice transcript sink. -->
<div class="hart-hero" id="hart-hero" role="search" aria-label="HART command center">
  <div class="hart-hero-orbwrap" id="hart-hero-orbwrap" data-orb-state="idle" role="button" tabindex="0" aria-label="Speak to HART (Super+Space)" title="Click or press Super+Space to speak">
    <canvas id="hart-voice-orb" width="360" height="360" aria-hidden="true"></canvas>
  </div>
  <div class="hart-hero-status" id="hart-hero-status" role="status" aria-live="polite">Ask HART anything - say it or type it</div>
  <div class="hart-hero-bar glass">
    <span class="mi material-icons-round hart-hero-bar-ic" aria-hidden="true">search</span>
    <input id="hart-hero-input" class="hart-hero-input" type="text" autocomplete="off" spellcheck="false" placeholder="Search apps, ask the agent, or speak…" aria-label="Command and search">
    <button class="hart-hero-go" id="hart-hero-go" type="button" aria-label="Send"><span class="mi material-icons-round" aria-hidden="true">arrow_forward</span></button>
  </div>
  <div class="hart-hero-hevolve" id="hart-hero-hevolve" aria-hidden="true"><span class="dot"></span>Hevolve AI</div>
  <div class="hart-hero-chips" id="hart-hero-chips"></div>
</div>

<!-- Top Bar - restructured (e1): brand | nav tabs | agent-status | omnibox |
     orb-sm | avatar | tray. #agent-status and .top-bar-right are KEPT verbatim
     (hartVisibility reads #agent-status chips; hartConnectivity mounts into
     .top-bar-right) - the restructure only ADDS the nav/omnibox/orb-sm/avatar. -->
<div class="top-bar glass" role="banner">
  <div class="start-btn" role="button" tabindex="0" aria-haspopup="menu" aria-label="Start menu" onclick="toggleStartMenu()" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}" title="Start Menu (Super)">
    <img src="/shell/static/hevolve-logo.png" class="start-logo" alt="" aria-hidden="true" draggable="false">
    <span class="hart-wordmark"><b style="color:var(--hart-accent,#00E6C3);font-weight:800">HART</b> <span style="color:var(--hart-a2,#9B5CFF);font-weight:700">OS</span></span>
  </div>
  <nav class="top-bar-nav" role="navigation" aria-label="Primary">
    <button class="tb-tab tb-active" type="button" data-tab="home" onclick="if(window.HartHomeNav)HartHomeNav('home')">Home</button>
    <button class="tb-tab" type="button" data-tab="agents" onclick="if(window.HartHomeNav)HartHomeNav('agents')">Agents</button>
    <button class="tb-tab" type="button" data-tab="apps" onclick="if(window.HartHomeNav)HartHomeNav('apps')">Apps</button>
    <button class="tb-tab" type="button" data-tab="hive" onclick="if(window.HartHomeNav)HartHomeNav('hive')">Hive</button>
    <button class="tb-tab" type="button" data-tab="earn" onclick="if(window.HartHomeNav)HartHomeNav('earn')">Earn</button>
  </nav>
  <div class="top-bar-center" id="agent-status" role="status" aria-live="polite" aria-label="Agent status"></div>
  <button class="top-bar-omni" type="button" aria-label="Ask or search anything" onclick="if(window.HartHome)HartHome.ask('')">
    <span class="mi material-icons-round" aria-hidden="true">search</span>
    <span>Ask or search anything</span>
    <span class="tbo-kbd" aria-hidden="true">Super K</span>
  </button>
  <button class="top-bar-orb" id="top-bar-orb" type="button" aria-label="Talk to HART" title="Talk to HART (Super+Space)" onclick="if(window.toggleVoice)toggleVoice()"></button>
  <button class="top-bar-avatar" id="top-bar-avatar" type="button" aria-label="Your account" title="Your account" onclick="if(window.HartHomeNav)HartHomeNav('account')">H</button>
  <div class="top-bar-right">
    <div class="tray-btn" role="button" tabindex="0" aria-label="Notifications" onclick="openPanel('notifications')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}" title="Notifications">
      <span class="mi material-icons-round" aria-hidden="true">notifications</span>
      <div class="badge" id="notif-badge" style="display:none"></div>
    </div>
    <div class="tray-btn" role="button" tabindex="0" aria-label="Appearance" onclick="openPanel('appearance')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}" title="Appearance">
      <span class="mi material-icons-round" aria-hidden="true">palette</span>
    </div>
    <div class="tray-btn" role="button" tabindex="0" aria-label="Security" onclick="openPanel('security')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}" title="Security">
      <span class="mi material-icons-round" aria-hidden="true">shield</span>
    </div>
    <span class="clock" id="clock"></span>
  </div>
</div>

<!-- Panel Container -->
<div class="panel-container" id="panels" role="main" aria-label="Open windows"></div>

<!-- Agent Pill (click to expand floating chat) -->
<!-- Voice orb visualiser + hero orchestrator. The orb canvas now lives inside
     #hart-hero (centerpiece); initHartOrb still finds #hart-voice-orb and drives
     it. hartHero.js fuses the orb with the command bar, reusing toggleVoice /
     acSend / openPanel — one pipeline, no fork. Loaded after the inline script. -->
<!-- All shell modules use `defer`: they must NOT block HTML parsing / first
     paint (≈456KB total, lottie.min.js alone is 305KB).  `defer` keeps them
     in document order AND guarantees they run AFTER the inline config script
     below sets window.MANIFEST / BACKEND / GROUPS — which is exactly the
     contract each module already documents ("loaded after the inline shell
     JS") and self-enforces via its `document.readyState==='loading'` init
     gate.  So deferring is strictly safer ordering, not a behaviour change. -->
<script defer src="/shell/static/lottie.min.js"></script>
<script defer src="/shell/static/hartBootSplash.js"></script>
<script defer src="/shell/static/hartSession.js"></script>
<script defer src="/shell/static/hartOSBridge.js"></script>
<script defer src="/shell/static/voiceOrbViz.js"></script>
<script defer src="/shell/static/hartHero.js"></script>
<!-- Assembled Netflix HOME (W1): the value-first cinematic canvas. Loaded after
     hartHero.js (it docks the orb via HartOrbHomeMode) and reuses openPanel /
     acSend / speakText / the resonance+compute-earnings endpoints. The agent
     re-composes it live via the SSE 'home_compose' branch below. -->
<link rel="stylesheet" href="/shell/static/hartHome.css">
<!-- ONE source of the brand-spectrum art language (gradients + glyph rendering),
     shared by hartHome.js (card art) and hartDesktop.js (icon art tiles). Loaded
     BEFORE both so window.HartBrandArt is defined when they paint. -->
<script defer src="/shell/static/hartBrandArt.js"></script>
<script defer src="/shell/static/hartHome.js"></script>
<script defer src="/shell/static/hartDesktop.js"></script>
<script defer src="/shell/static/hartWorkspaces.js"></script>
<script defer src="/shell/static/hartEffects.js"></script>
<script defer src="/shell/static/hartPersonalize.js"></script>
<script defer src="/shell/static/hartMarketplace.js"></script>
<script defer src="/shell/static/hartCredits.js"></script>
<script defer src="/shell/static/hartDock.js"></script>
<script defer src="/shell/static/hartSenses.js"></script>
<!-- Living-Glass: deterministic visibility engine (sole writer of <html data-*>)
     + designed offline/empty-state component. Loaded after hartSenses.js (it reads
     its #hart-hero / #panels / senses hooks) and before hartSessionUI.js. -->
<script defer src="/shell/static/hartVisibility.js"></script>
<script defer src="/shell/static/hartStates.js"></script>
<script defer src="/shell/static/hartOnboarding.js"></script>
<script defer src="/shell/static/hartSessionUI.js"></script>
<link rel="stylesheet" href="/shell/static/hartResponsive.css">
<script defer src="/shell/static/hartFiles.js"></script>
<!-- Unified navigation framework (#169): a shell-wide back/forward/breadcrumb
     history over the openPanel single-instance registry (panel id == location),
     generalising hartFiles.js's proven navigate/back/forward primitive. Loaded
     after the inline shell script (which defines openPanel/panels/bringToFront)
     and after hartFiles.js. Exposes window.HartNav + window.HartNavCore (the
     pure history/reuse-vs-new core, unit-tested by test_hart_nav.mjs). -->
<script defer src="/shell/static/hartNav.js"></script>
<!-- OS connectivity cluster (wifi/bluetooth/battery/volume indicators +
     quick-settings) in the top-bar tray. Loaded after hartStates.js (it reuses
     the designed-state helpers) and hartSession.js (HartTimeoutSignal). Polls
     /api/shell/connectivity/summary on SHELL=:6800 (same-process) and degrades
     to a neutral 'unknown' glyph when a tool/hardware is absent. -->
<script defer src="/shell/static/hartConnectivity.js"></script>
<!-- Flash HART OS to USB wizard (System panel 'flash'): drives the proven
     scripts/hart_usb_flasher.py via /api/shell/flash/* so a running node can
     create more install sticks. Exposes window.loadFlashWizard. -->
<script defer src="/shell/static/hartFlash.js"></script>

<!-- Agent Pill (click to expand floating chat) -->
<div class="agent-pill glass hidden" id="agent-pill" onclick="toggleAssistantChat()">
  <span class="mi material-icons-round" style="color:var(--hart-accent)">chat_bubble</span>
  <input id="agent-input" placeholder="Ask HART..." onclick="event.stopPropagation();toggleAssistantChat()" onkeydown="if(event.key==='Enter'){{event.stopPropagation();toggleAssistantChat();setTimeout(function(){{var i=document.getElementById('ac-input');if(i){{i.value=document.getElementById('agent-input').value;document.getElementById('agent-input').value=''}}}},100)}}">
  <div class="agent-response" id="agent-resp"></div>
</div>

<!-- Floating Assistant Chat Panel -->
<div class="assistant-chat glass" id="assistant-chat" role="dialog" aria-label="HART Assistant" aria-modal="false">
  <div class="ac-header" id="ac-drag-handle">
    <span class="mi material-icons-round" style="font-size:20px;color:var(--hart-accent)">chat_bubble</span>
    <span class="ac-title">HART Assistant</span>
    <button class="ac-btn mi material-icons-round" onclick="minimizeAssistant()" title="Minimize">remove</button>
    <button class="ac-btn mi material-icons-round" onclick="toggleAssistantChat()" title="Close">close</button>
  </div>
  <div class="ac-caps" id="ac-caps"></div>
  <div class="ac-messages" id="ac-messages" role="log" aria-live="polite">
    <div class="ac-msg assistant">Hi! I can help with anything - chat, code, agents, vision, voice, remote desktop, and 3,200+ OpenClaw skills. What would you like to do?</div>
  </div>
  <div class="ac-input-row">
    <span class="mi material-icons-round ac-btn" onclick="acVoiceInput()" title="Voice input" style="font-size:20px">mic</span>
    <input class="ac-input" id="ac-input" placeholder="Ask anything..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();acSend()}}">
    <button class="ac-send" onclick="acSend()"><span class="mi material-icons-round">send</span></button>
  </div>
</div>

<!-- Start Menu -->
<div class="start-menu glass" id="start-menu">
  <input class="start-search" id="start-search" placeholder="Search..." oninput="filterStart(this.value)" onkeydown="if(event.key==='Enter'){{event.preventDefault();startSearchEnter()}}">
  <div class="start-scroll" id="start-scroll"></div>
  <div class="start-footer">
    <div class="power-btn" role="button" tabindex="0" onclick="shellAction('lock')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}"><span class="mi material-icons-round" aria-hidden="true">lock</span>Lock</div>
    <div class="power-btn" role="button" tabindex="0" onclick="shellAction('suspend')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}"><span class="mi material-icons-round" aria-hidden="true">dark_mode</span>Sleep</div>
    <div class="power-btn" role="button" tabindex="0" onclick="shellAction('restart')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}"><span class="mi material-icons-round" aria-hidden="true">refresh</span>Restart</div>
    <div class="power-btn" id="power-btn-firmware" role="button" tabindex="0" style="display:none" onclick="shellAction('firmware')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}"><span class="mi material-icons-round" aria-hidden="true">developer_board</span>Restart to Firmware (UEFI)</div>
    <div class="power-btn" role="button" tabindex="0" onclick="shellAction('shutdown')" onkeydown="if(event.key==='Enter'||event.key===' '){{event.preventDefault();this.click()}}"><span class="mi material-icons-round" aria-hidden="true">power_settings_new</span>Shut Down</div>
  </div>
</div>

<!-- Lock Screen. #166: `lock_boot_class` is ' active' when a lock password is
     already set, so this opaque overlay COVERS the desktop from the first paint
     (no FOUC / no desktop leak); hartSessionUI.js drives unlock + reveal. -->
<div class="lock-screen{lock_boot_class}" id="lock-screen" role="dialog" aria-modal="true" aria-label="Screen locked">
  <div class="lock-brand"><img src="/shell/static/hevolve-logo.png" alt="HART OS" draggable="false"><span>HART OS</span></div>
  <div class="lock-clock" id="lock-clock"></div>
  <div class="lock-date" id="lock-date"></div>
  <input class="lock-input" type="password" placeholder="Password" id="lock-pw"
    onkeydown="if(event.key==='Enter')unlock()">
  <div class="lock-status" id="lock-status"></div>
</div>

<!-- Taskbar (open panels as chips) -->
<div class="taskbar glass" id="taskbar" role="navigation" aria-label="Taskbar"></div>

<!-- Virtual-desktop switcher (client-side; populated by hartWorkspaces.js) -->
<div class="hart-ws-switcher glass" id="hart-ws-switcher" role="tablist" aria-label="Virtual desktops"></div>

<!-- AI sensory cluster (bottom-left): vision (eye kill-switch) + audio (mic) read
     as one grouped "sensory" pair. The eye hard-cuts the AI's senses + shows live
     proof (hartSenses.js); the mic toggles voice input (toggleVoice). This is the
     SMALL bottom mic — NOT the central voice orb, which stays untouched. -->
<div class="hart-senses" id="hart-senses" data-edge="b">
  <div class="hart-senses-panel" id="hart-senses-panel" role="status" aria-live="polite">
    <div class="hsp-title">AI sensory state</div>
    <div id="hart-senses-proof"></div>
  </div>
  <div class="hart-senses-cluster lg-1" role="group" aria-label="AI senses (vision &amp; audio)">
    <span class="hart-senses-grip" aria-hidden="true"><span class="mi material-icons-round">drag_indicator</span></span>
    <button class="hart-senses-btn" id="hart-senses-btn" type="button" aria-pressed="false" aria-label="Shut or wake the AI's senses" title="Shut the AI's eyes &amp; ears (right-click for live proof)">
      <span class="mi material-icons-round" aria-hidden="true">visibility</span>
    </button>
    <button class="hart-senses-btn hart-senses-mic" id="hart-senses-mic" type="button" aria-label="Talk to HART (toggle voice)" title="Talk to HART - toggle voice input" onclick="window.toggleVoice&amp;&amp;toggleVoice()">
      <span class="mi material-icons-round" aria-hidden="true">mic</span>
    </button>
  </div>
</div>

<!-- First-run "Light Your HART" ceremony (web, in-shell; auto-runs after OS install when not onboarded) -->
<div class="hart-onboarding" id="hart-onboarding" role="dialog" aria-modal="true" aria-label="Light Your HART">
  <div class="hob-orb" aria-hidden="true"></div>
  <div class="hob-name" id="hart-onboarding-name"></div>
  <div class="hob-narr" id="hart-onboarding-narr" role="status" aria-live="polite"></div>
  <div class="hob-opts" id="hart-onboarding-opts"></div>
  <div class="hob-skip">Press Esc to skip</div>
</div>

<!-- Toast Notifications -->
<div class="toast-container" id="toast-container" role="status" aria-live="polite"></div>

<!-- Context Menu -->
<div class="ctx-menu glass" id="ctx-menu" style="display:none"></div>

<script>
// Catch any uncaught JS error and show it on screen — helps debug on kiosk
// where DevTools inspector isn't accessible via right-click.
window.onerror = function(msg, src, line, col, err) {{
  var d = document.getElementById('_js_err');
  if(!d) {{
    d = document.createElement('div');
    d.id = '_js_err';
    d.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#c00;color:#fff;'
      + 'font:13px monospace;padding:8px 12px;z-index:99999;white-space:pre-wrap;max-height:40vh;overflow-y:auto';
    document.body.appendChild(d);
  }}
  d.textContent += '[JS ERROR] ' + msg + ' (' + src + ':' + line + ':' + col + ')\\n';
  return false;
}};
window.addEventListener('unhandledrejection', function(e) {{
  window.onerror('[UnhandledPromise] ' + (e.reason||e), '', 0, 0, e.reason);
}});

// Helper: get panel id from nearest .panel ancestor (avoids quote-escaping in onclick strings)
function _pid(el) {{ return el.closest('[data-panel-id]').dataset.panelId; }}

// ═══ Configuration ═══
const BACKEND = 'http://localhost:{self.backend_port}';
const SHELL = 'http://localhost:{self.port}';
const MANIFEST = {manifest_json};
// External /shell/static modules (hartDesktop.js, hartDock.js, …) read
// window.MANIFEST. A top-level `const` in a classic script is a lexical global,
// NOT a property of window — so without this the desktop icons never render
// (hartDesktop.js gates on window.MANIFEST). Expose it explicitly.
window.MANIFEST = MANIFEST;
const SYSTEM_PANELS = {system_json};
const GROUPS = {groups_json};
// W4 Start-menu PINNED ids + Settings aggregator sections (composition only —
// each is a list of EXISTING panel ids from shell_manifest). buildStartMenu and
// loadSettingsPanel resolve each id's metadata from MANIFEST/SYSTEM_PANELS above,
// so there is no second copy of any panel definition (single source).
const PINNED = {pinned_json};
const SETTINGS_SECTIONS = {settings_sections_json};
// The Nunba React dist is a no-basename BrowserRouter (history) SPA served at
// the ORIGIN ROOT of this shell (see _create_flask_app: /static passthrough +
// the SPA history fallback). The manifest routes already carry a leading slash
// ('/social', '/agents', ...), so NUNBA_BASE must be '' (empty): the iframe src
// becomes a real root-relative HISTORY path the router matches. It must NOT be
// '/' (that would make '/'+'/social' => '//social', a protocol-relative URL),
// and must NOT be the old hash-fragment mount: a history router ignores the
// '#' fragment, so the hashed src resolved to the SPA root => a blank panel.
const NUNBA_BASE = '';

// De-monochrome: every manifest/system entry carries a resolved per-app `color`
// (stamped server-side from shell_manifest.with_icon_colors — the single source
// of truth). miStyle() turns it into an inline glyph tint so the start menu,
// dock, desktop icons and titlebars all colour-agree instead of one accent
// wash. Exposed on window so the /shell/static modules (hartDesktop.js) reuse
// the SAME resolver — no parallel palette.
function miStyle(def) {{ return (def && def.color) ? ' style="color:'+def.color+'"' : ''; }}
window.miStyle = miStyle;

// ═══ Performance Config (auto-detected from theme) ═══
const PERF = {{
  potato: {'true' if is_potato else 'false'},
  clockMs: {perf.get('clock_interval_ms', 1000)},
  agentStatusMs: {perf.get('agent_status_interval_ms', 5000)},
  maxPanels: {perf.get('max_open_panels', 20)},
  destroyMinimized: {'true' if perf.get('destroy_minimized_iframes') else 'false'},
  lazyIframes: {'true' if perf.get('lazy_load_iframes') else 'false'},
}};
// Mirror PERF onto window so the /shell/static effects module (hartEffects.js)
// can read the SAME software-render gate (potato) without depending on the
// inline-script const being reachable across script tags. Single source: PERF.
window.HART_PERF = PERF;

// ═══ State ═══
let panels = {{}};
let panelZ = 100;
let startOpen = false;
let focusedPanel = null;
let mru = [];

// AbortSignal.timeout() was added in WebKit 615 / Safari 15.4. Older WebKitGTK
// builds (e.g. NixOS 24.11 ISO) may not have it — fall back to AbortController.
function _sig(ms) {{
  if(typeof AbortSignal !== 'undefined' && AbortSignal.timeout) return AbortSignal.timeout(ms);
  const c = new AbortController();
  setTimeout(()=>c.abort(), ms);
  return c.signal;
}}

// ═══════════════════════════════════════════════
//  HART Design System — Component Library
// ═══════════════════════════════════════════════

// ── Ripple Effect ──
function dsRipple(e) {{
  if(PERF.potato) return;
  const el = e.currentTarget;
  const rect = el.getBoundingClientRect();
  const ripple = document.createElement('span');
  ripple.className = 'ds-ripple';
  const size = Math.max(rect.width, rect.height) * 2;
  ripple.style.width = ripple.style.height = size + 'px';
  ripple.style.left = (e.clientX - rect.left - size/2) + 'px';
  ripple.style.top = (e.clientY - rect.top - size/2) + 'px';
  el.appendChild(ripple);
  ripple.addEventListener('animationend', function(){{ ripple.remove(); }});
}}

// ── Button Component ──
function dsBtn(label, opts) {{
  opts = opts || {{}};
  const variant = opts.variant || 'primary';
  const icon = opts.icon || '';
  const cls = opts.cls || '';
  const disabled = opts.disabled ? ' disabled' : '';
  const onclick = opts.onclick || '';
  return '<button class="ds-btn ds-btn-'+variant+' '+cls+'"'+disabled+
    ' onclick="dsRipple(event);'+(onclick.replace(/"/g,'&quot;'))+'">' +
    (icon ? '<span class="mi material-icons-round">'+icon+'</span>' : '') +
    '<span>'+label+'</span></button>';
}}

// ── Input Component ──
function dsInput(opts) {{
  opts = opts || {{}};
  const type = opts.type || 'text';
  const id = opts.id || '';
  const label = opts.label || '';
  const placeholder = opts.placeholder || '';
  const value = opts.value || '';
  const oninput = opts.oninput || '';
  const onkeydown = opts.onkeydown || '';
  const cls = opts.error ? 'ds-input ds-input-error' : 'ds-input';
  let html = '<div class="ds-input-wrap">';
  if(label) html += '<label class="ds-input-label"'+(id?' for="'+id+'"':'')+'>'+label+'</label>';
  html += '<input class="'+cls+'" type="'+type+'"'+(id?' id="'+id+'"':'')+
    ' placeholder="'+placeholder+'" value="'+value+'"'+
    (oninput?' oninput="'+oninput.replace(/"/g,'&quot;')+'"':'') +
    (onkeydown?' onkeydown="'+onkeydown.replace(/"/g,'&quot;')+'"':'') + '>';
  if(opts.help) html += '<div class="ds-input-help">'+opts.help+'</div>';
  if(opts.errorText) html += '<div class="ds-input-help" style="color:var(--hart-error)">'+opts.errorText+'</div>';
  html += '</div>';
  return html;
}}

// ── Select Component ──
function dsSelect(opts) {{
  opts = opts || {{}};
  const id = opts.id || '';
  const label = opts.label || '';
  const options = opts.options || [];
  const onchange = opts.onchange || '';
  let html = '<div class="ds-input-wrap">';
  if(label) html += '<label class="ds-input-label">'+label+'</label>';
  html += '<select class="ds-select"'+(id?' id="'+id+'"':'')+
    (onchange?' onchange="'+onchange.replace(/"/g,'&quot;')+'"':'')+'>';
  options.forEach(function(o){{
    const sel = o.selected ? ' selected' : '';
    html += '<option value="'+o.value+'"'+sel+'>'+o.label+'</option>';
  }});
  html += '</select></div>';
  return html;
}}

// ── Slider Component ──
function dsSlider(opts) {{
  opts = opts || {{}};
  const id = opts.id || '';
  const min = opts.min !== undefined ? opts.min : 0;
  const max = opts.max !== undefined ? opts.max : 100;
  const value = opts.value !== undefined ? opts.value : 50;
  const label = opts.label || '';
  const unit = opts.unit || '';
  const oninput = opts.oninput || '';
  let html = '<div class="ds-flex ds-gap-3" style="align-items:center">';
  if(label) html += '<span class="ds-label-sm ds-text-muted" style="min-width:80px">'+label+'</span>';
  html += '<input type="range" class="ds-slider" min="'+min+'" max="'+max+'" value="'+value+'"'+
    (id?' id="'+id+'"':'')+
    ' oninput="'+
    (oninput?oninput.replace(/"/g,'&quot;')+';':'')+
    (id?'document.getElementById("'+id+'-val").textContent=this.value+""+unit+"";':'')+
    '">';
  if(id) html += '<span class="ds-label-md" id="'+id+'-val" style="min-width:40px;text-align:right">'+value+unit+'</span>';
  html += '</div>';
  return html;
}}

// ── Skeleton Loader ──
function dsSkeleton(type, count) {{
  count = count || 3;
  if(type === 'panel') {{
    return '<div class="ds-panel-grid">' +
      '<div class="ds-skeleton ds-skeleton-title"></div>' +
      Array.from({{length:count}}).map(function(){{return '<div class="ds-skeleton ds-skeleton-card"></div>';}}).join('') +
      '</div>';
  }}
  if(type === 'list') {{
    return Array.from({{length:count}}).map(function(){{
      return '<div class="ds-flex ds-gap-3" style="align-items:center;margin-bottom:8px">' +
        '<div class="ds-skeleton ds-skeleton-circle" style="width:32px;height:32px"></div>' +
        '<div style="flex:1"><div class="ds-skeleton ds-skeleton-text" style="width:70%"></div>' +
        '<div class="ds-skeleton ds-skeleton-text" style="width:40%"></div></div></div>';
    }}).join('');
  }}
  return Array.from({{length:count}}).map(function(){{return '<div class="ds-skeleton ds-skeleton-text"></div>';}}).join('');
}}

// ── Status Row (design system) ──
function dsStatusRow(icon, label, value, color, opts) {{
  opts = opts || {{}};
  const sublabel = opts.sublabel || '';
  const trailing = opts.trailing || '';
  return '<div class="ds-list-item'+(opts.interactive?' ds-list-item-interactive':'')+'">'+
    '<span class="mi material-icons-round ds-list-item-icon" style="color:'+color+'">'+icon+'</span>'+
    '<div class="ds-list-item-content">'+
    '<div class="ds-list-item-primary">'+label+'</div>'+
    (sublabel?'<div class="ds-list-item-secondary">'+sublabel+'</div>':'')+
    '</div>'+
    '<span class="ds-list-item-trailing" style="color:'+color+'">'+value+'</span>'+
    (trailing?trailing:'')+
    '</div>';
}}

// ── Metric Bar (design system) ──
function dsMetricBar(label, pct, unit, sub) {{
  const color = pct>80?'var(--hart-error)':pct>60?'var(--hart-caution)':'var(--hart-active)';
  const colorClass = pct>80?'ds-progress-error':pct>60?'ds-progress-warning':'ds-progress-active';
  return '<div style="margin-bottom:var(--ds-space-2)">' +
    '<div class="ds-flex ds-flex-between" style="margin-bottom:var(--ds-space-1)">'+
    '<span class="ds-body-md">'+label+'</span>'+
    '<span class="ds-label-lg" style="font-weight:600">'+pct+unit+'</span></div>'+
    '<div class="ds-progress"><div class="ds-progress-fill '+colorClass+'" style="width:'+pct+'%"></div></div>'+
    (sub?'<div class="ds-label-sm ds-text-muted" style="margin-top:var(--ds-space-1)">'+sub+'</div>':'')+
    '</div>';
}}

// ── Card Component ──
function dsCard(content, opts) {{
  opts = opts || {{}};
  const cls = 'ds-card'+(opts.elevated?' ds-card-elevated':'')+(opts.interactive?' ds-card-interactive':'');
  const onclick = opts.onclick || '';
  return '<div class="'+cls+'"'+(onclick?' onclick="dsRipple(event);'+onclick.replace(/"/g,'&quot;')+'"':'')+'>'+content+'</div>';
}}

// ── Modal System ──
let _dsModalOverlay = null;
function dsModal(opts) {{
  opts = opts || {{}};
  // Remove existing modal
  if(_dsModalOverlay) {{ _dsModalOverlay.remove(); _dsModalOverlay = null; }}

  const overlay = document.createElement('div');
  overlay.className = 'ds-modal-overlay';
  overlay.innerHTML = '<div class="ds-modal">'+
    '<div class="ds-modal-title">'+(opts.title||'')+'</div>'+
    '<div class="ds-modal-body" id="ds-modal-body">'+(opts.body||'')+'</div>'+
    '<div class="ds-modal-actions" id="ds-modal-actions"></div></div>';

  document.body.appendChild(overlay);
  _dsModalOverlay = overlay;

  // Close on overlay click (not modal body)
  overlay.addEventListener('click', function(e){{
    if(e.target === overlay) dsModalClose();
  }});

  // Close on ESC
  const escHandler = function(e) {{
    if(e.key === 'Escape') {{ dsModalClose(); document.removeEventListener('keydown', escHandler); }}
  }};
  document.addEventListener('keydown', escHandler);

  // Add action buttons
  const actions = document.getElementById('ds-modal-actions');
  if(opts.actions) {{
    opts.actions.forEach(function(a) {{
      const btn = document.createElement('button');
      btn.className = 'ds-btn ds-btn-'+(a.variant||'text');
      btn.textContent = a.label;
      btn.onclick = function(e){{ dsRipple(e); if(a.action) a.action(); }};
      actions.appendChild(btn);
    }});
  }}

  // Trigger open animation (next frame)
  requestAnimationFrame(function(){{
    requestAnimationFrame(function(){{ overlay.classList.add('ds-open'); }});
  }});

  // Focus trap: focus first input or first button
  setTimeout(function(){{
    const target = overlay.querySelector('input,select,textarea') || overlay.querySelector('.ds-btn');
    if(target) target.focus();
  }}, 100);

  return overlay;
}}

function dsModalClose() {{
  if(!_dsModalOverlay) return;
  _dsModalOverlay.classList.remove('ds-open');
  const el = _dsModalOverlay;
  setTimeout(function(){{ el.remove(); }}, 250);
  _dsModalOverlay = null;
}}

// ── Prompt Modal (replaces window.prompt) ──
function dsPrompt(title, message, opts) {{
  opts = opts || {{}};
  const inputType = opts.type || 'text';
  const placeholder = opts.placeholder || '';
  const defaultValue = opts.defaultValue || '';

  return new Promise(function(resolve) {{
    const modal = dsModal({{
      title: title,
      body: '<div class="ds-body-md ds-text-muted" style="margin-bottom:var(--ds-space-4)">'+(message||'')+'</div>'+
        '<input class="ds-input" type="'+inputType+'" id="ds-prompt-input" placeholder="'+placeholder+'" value="'+defaultValue+'"'+
        ' onkeydown="if(event.key===&quot;Enter&quot;)document.getElementById(&quot;ds-prompt-ok&quot;).click()">',
      actions: [
        {{ label: 'Cancel', variant: 'text', action: function(){{ dsModalClose(); resolve(null); }} }},
        {{ label: opts.okLabel||'OK', variant: 'primary', action: function(){{
          const val = document.getElementById('ds-prompt-input').value;
          dsModalClose(); resolve(val);
        }} }}
      ]
    }});
    // Add id for enter-key handling
    setTimeout(function(){{
      const btns = modal.querySelectorAll('.ds-btn-primary');
      if(btns.length) btns[btns.length-1].id = 'ds-prompt-ok';
    }}, 50);
  }});
}}

// ── Confirm Modal (replaces window.confirm) ──
function dsConfirm(title, message, opts) {{
  opts = opts || {{}};
  return new Promise(function(resolve) {{
    dsModal({{
      title: title,
      body: message,
      actions: [
        {{ label: opts.cancelLabel||'Cancel', variant: 'text', action: function(){{ dsModalClose(); resolve(false); }} }},
        {{ label: opts.okLabel||'Confirm', variant: opts.danger?'danger':'primary',
          action: function(){{ dsModalClose(); resolve(true); }} }}
      ]
    }});
  }});
}}

// ── Alert Modal (replaces window.alert) ──
function dsAlert(title, message, severity) {{
  const icons = {{info:'info',success:'check_circle',warning:'warning',error:'error'}};
  const colors = {{info:'var(--hart-accent)',success:'var(--hart-active)',warning:'var(--hart-caution)',error:'var(--hart-error)'}};
  const icon = icons[severity||'info']||'info';
  const color = colors[severity||'info']||colors.info;
  return new Promise(function(resolve) {{
    dsModal({{
      title: '<span class="mi material-icons-round" style="font-size:24px;color:'+color+';vertical-align:middle;margin-right:8px">'+icon+'</span>'+title,
      body: message,
      actions: [{{ label: 'OK', variant: 'primary', action: function(){{ dsModalClose(); resolve(); }} }}]
    }});
  }});
}}

// ═══ Toast Notifications (upgraded) ═══
function showToast(title, message, severity) {{
  severity = severity || 'info';
  const container = document.getElementById('toast-container');
  if(!container) return;
  const icons = {{info:'info',warning:'warning',error:'error',success:'check_circle'}};
  const colors = {{info:'var(--hart-accent)',warning:'var(--hart-caution)',error:'var(--hart-error)',success:'var(--hart-active)'}};
  const icon = icons[severity]||icons.info;
  const color = colors[severity]||colors.info;
  const toast = document.createElement('div');
  toast.className = PERF.potato ? 'toast glass' : 'ds-toast';
  // XSS-safe: build the fixed structure via innerHTML (NO untrusted interpolation),
  // then set the caller-supplied title/message (and the icon ligature) via
  // textContent so a hostile notification body can never inject markup.
  if(PERF.potato) {{
    toast.style.borderLeft = '3px solid '+color;
    toast.innerHTML = '<div class="ds-tt" style="font-weight:600;margin-bottom:2px"></div>'+
      '<div class="ds-tm" style="color:var(--hart-text)"></div>';
    var _tt = toast.querySelector('.ds-tt'); _tt.style.color = color; _tt.textContent = title;
    toast.querySelector('.ds-tm').textContent = message;
  }} else {{
    toast.innerHTML = '<span class="mi material-icons-round ds-toast-icon"></span>'+
      '<div class="ds-toast-content"><div class="ds-toast-title"></div>'+
      '<div class="ds-toast-message"></div></div>'+
      '<div class="ds-toast-progress"></div>';
    var _ic = toast.querySelector('.ds-toast-icon'); _ic.style.color = color; _ic.textContent = icon;
    toast.querySelector('.ds-toast-title').textContent = title;
    toast.querySelector('.ds-toast-message').textContent = message;
    toast.querySelector('.ds-toast-progress').style.background = color;
  }}
  toast.onclick = function(){{
    if(!PERF.potato) toast.classList.add('ds-toast-exit');
    setTimeout(function(){{ toast.remove(); }}, PERF.potato?0:200);
  }};
  container.appendChild(toast);
  setTimeout(function(){{
    if(toast.parentNode) {{
      if(!PERF.potato) {{ toast.classList.add('ds-toast-exit'); setTimeout(function(){{ toast.remove(); }},200); }}
      else toast.remove();
    }}
  }}, 5000);
}}

// ═══ Taskbar ═══
function updateTaskbar() {{
  const bar = document.getElementById('taskbar');
  if(!bar) return;
  bar.innerHTML = Object.entries(panels).map(function([id,p]) {{
    // Instance ids ('x#2') have no direct manifest entry — fall back to the base
    // panel definition for the icon, and prefer the stored per-instance title.
    const base = (p&&p.base) || (''+id).split('#')[0];
    const info = MANIFEST[id] || SYSTEM_PANELS[id] || MANIFEST[base] || SYSTEM_PANELS[base] || {{}};
    const active = id===focusedPanel ? 'active' : '';
    const icon = info.icon || 'web_asset';
    const title = (p&&p.title) || info.title || id;
    return '<div class="taskbar-chip glass '+active+'" data-panel-id="'+id+'" onclick="taskbarClick(this.dataset.panelId)" title="'+title+'">' +
      '<span class="mi material-icons-round"'+miStyle(info)+'>'+icon+'</span>' +
      '<span class="chip-label">'+title+'</span></div>';
  }}).join('');
}}

// ═══ Panel Snap ═══
function snapPanel(id, side) {{
  const p = panels[id];
  if(!p) return;
  const topH = 40;
  const taskH = 44;
  if(!PERF.potato) p.el.style.transition = 'all 0.2s ease-out';
  if(side==='left') {{
    p.el.style.left='0';p.el.style.top=topH+'px';
    p.el.style.width='50vw';p.el.style.height='calc(100vh - '+(topH+taskH)+'px)';
  }} else {{
    p.el.style.left='50vw';p.el.style.top=topH+'px';
    p.el.style.width='50vw';p.el.style.height='calc(100vh - '+(topH+taskH)+'px)';
  }}
  p.el.style.borderRadius='0';
  p.max=false;
  setTimeout(function(){{p.el.style.transition='';}},250);
}}
// Expose the CANONICAL snap so the /shell/static effects module (hartEffects.js
// snap-zones) reuses it — no parallel snap geometry. Same pattern as miStyle /
// hartSwitchWorkspace.
window.snapPanel = snapPanel;

// ═══ Clock ═══
function tickClock() {{
  const now = new Date();
  const t = now.toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit'}});
  const d = now.toLocaleDateString([], {{weekday:'long',month:'long',day:'numeric'}});
  const el = document.getElementById('clock');
  if(el) el.textContent = t;
  const lc = document.getElementById('lock-clock');
  if(lc) lc.textContent = t;
  const ld = document.getElementById('lock-date');
  if(ld) ld.textContent = d;
}}
setInterval(tickClock, PERF.clockMs);
try {{ tickClock(); }} catch(e) {{ console.error('[HART] tickClock:', e); }}

// ═══ Agent Status (top bar) ═══
function refreshAgentStatus() {{
  fetch(BACKEND+'/api/social/dashboard/agents',{{signal:_sig(3000)}})
    .then(r=>r.json()).then(data=>{{
      const bar = document.getElementById('agent-status');
      const agents = (data.agents||[]).filter(a=>a.status==='running');
      if(agents.length===0){{bar.innerHTML='<span style="opacity:0.5">No agents running</span>';return;}}
      bar.innerHTML = agents.slice(0,4).map(a=>
        '<span class="agent-chip"><span class="dot"></span>'+
        (a.name||a.goal_type||'agent').substring(0,16)+'</span>'
      ).join('');
    }}).catch(()=>{{}});
}}
setInterval(refreshAgentStatus, PERF.agentStatusMs);
try {{ refreshAgentStatus(); }} catch(e) {{ console.error('[HART] refreshAgentStatus:', e); }}

// ═══ Start Menu ═══
function buildStartMenu() {{
  const scroll = document.getElementById('start-scroll');
  let html = '';
  // Pinned (curated, top) — resolves each id from the live manifest and opens
  // via openPanel (single-instance reuse), same as every other start item.
  const pins = (PINNED||[]).map(function(id){{
    return [id, MANIFEST[id] || SYSTEM_PANELS[id]];
  }}).filter(function(pair){{ return !!pair[1]; }});
  if(pins.length) {{
    html += '<div class="start-group" data-group="Pinned"><div class="start-group-label">Pinned</div><div class="start-grid">';
    pins.forEach(function(pair){{
      const id = pair[0], p = pair[1];
      html += '<div class="start-item" data-id="'+id+'" data-title="'+p.title+'" onclick="openPanel(this.dataset.id)">';
      html += '<span class="mi material-icons-round"'+miStyle(p)+'>'+(p.icon||'apps')+'</span>';
      html += '<span class="label">'+p.title+'</span></div>';
    }});
    html += '</div></div>';
  }}
  GROUPS.forEach(group => {{
    const items = Object.entries(MANIFEST).filter(([_,v])=>v.group===group);
    if(!items.length) return;
    html += '<div class="start-group"><div class="start-group-label">'+group+'</div><div class="start-grid">';
    items.forEach(([id,p])=>{{
      html += '<div class="start-item" data-id="'+id+'" data-title="'+p.title+'" onclick="openPanel(this.dataset.id)">';
      html += '<span class="mi material-icons-round"'+miStyle(p)+'>'+(p.icon||'apps')+'</span>';
      html += '<span class="label">'+p.title+'</span></div>';
    }});
    html += '</div></div>';
  }});
  // System panels
  const sysItems = Object.entries(SYSTEM_PANELS);
  if(sysItems.length) {{
    html += '<div class="start-group"><div class="start-group-label">System</div><div class="start-grid">';
    sysItems.forEach(([id,p])=>{{
      html += '<div class="start-item" data-id="'+id+'" data-title="'+p.title+'" onclick="openPanel(this.dataset.id)">';
      html += '<span class="mi material-icons-round"'+miStyle(p)+'>'+(p.icon||'settings')+'</span>';
      html += '<span class="label">'+p.title+'</span></div>';
    }});
    html += '</div></div>';
  }}
  scroll.innerHTML = html;
}}
try {{ buildStartMenu(); }} catch(e) {{ console.error('[HART] buildStartMenu:', e); }}

function toggleStartMenu() {{
  const m = document.getElementById('start-menu');
  startOpen = !startOpen;
  m.classList.toggle('open', startOpen);
  if(startOpen) document.getElementById('start-search').focus();
}}

function filterStart(q) {{
  const items = document.querySelectorAll('.start-item');
  const lq = q.toLowerCase();
  items.forEach(el => {{
    const title = (el.dataset.title||'').toLowerCase();
    el.style.display = title.includes(lq) ? '' : 'none';
  }});
}}
// Enter in the start search launches the first visible result (Spotlight-style).
function startSearchEnter() {{
  const items = document.querySelectorAll('.start-item');
  for(const el of items) {{
    if(el.style.display !== 'none') {{ el.click(); return; }}
  }}
}}

// ═══ Panel Manager ═══
function openPanel(id, opts) {{
  opts = opts || {{}};
  // Unified navigation (#169): the panels registry is single-instance by
  // DEFAULT — opening an already-open panel just brings it to front (reuse).
  // An explicit openPanel(id,{{newInstance:true}}) opts INTO a second instance:
  // hartNav mints a distinct instance id (base#N) so both coexist, while the
  // panel definition is still looked up by the BASE id. hartNav also owns the
  // shell-wide back/forward history keyed by panel id (static/hartNav.js).
  const baseId = (''+id).split('#')[0];
  if(opts.newInstance) {{
    id = (window.HartNav && HartNav.nextInstance) ? HartNav.nextInstance(baseId) : baseId+'#'+Date.now();
  }}
  // If panel already open, bring to front (the default reuse policy).
  if(panels[id]) {{
    bringToFront(id);
    if(window.HartNav) HartNav.onOpen(id, (panels[id]&&panels[id].title)||baseId);
    return;
  }}
  // Potato mode: limit open panels to save memory
  if(PERF.potato && PERF.maxPanels > 0) {{
    const openCount = Object.keys(panels).length;
    if(openCount >= PERF.maxPanels) {{
      // Close oldest non-focused panel
      const oldest = Object.keys(panels).find(k=>k!==focusedPanel);
      if(oldest) closePanel(oldest);
    }}
  }}
  // The panel DEFINITION is keyed by the BASE id (so a second instance 'x#2'
  // renders the same 'x' panel); the DOM element ids below use the instance id.
  const def = MANIFEST[baseId] || SYSTEM_PANELS[baseId] || {{}};
  // Installed native app (from the app-installer): no in-shell panel to draw —
  // hand off to the EXISTING launch path (gtk-launch via /api/shell/launch).
  // These entries carry an `exec` and no `route`; everything else falls through
  // to the normal panel render below.
  if(def.exec && !def.route && !SYSTEM_PANELS[baseId]) {{
    launchApp(def.exec);
    if(startOpen) toggleStartMenu();
    return;
  }}
  const sz = def.default_size || [700,500];
  const isSystem = !!SYSTEM_PANELS[baseId];

  // Position: cascade from center
  const cx = window.innerWidth/2, cy = window.innerHeight/2;
  const count = Object.keys(panels).length;
  const x = Math.max(20, cx - sz[0]/2 + count*30);
  const y = Math.max(50, cy - sz[1]/2 + count*30);

  const panel = document.createElement('div');
  panel.className = 'panel glass';
  panel.id = 'panel-'+id;
  panel.dataset.panelId = id;
  panel.style.cssText = 'left:'+x+'px;top:'+y+'px;width:'+sz[0]+'px;height:'+sz[1]+'px;z-index:'+(++panelZ);

  const title = opts.title || def.title || baseId;
  const icon = def.icon || 'web_asset';

  panel.innerHTML = '<div class="panel-titlebar" onmousedown="startDrag(event,_pid(this))"'+
    ' ondblclick="toggleMax(_pid(this))">'+
    '<span class="mi material-icons-round"'+miStyle(def)+'>'+icon+'</span>'+
    '<span class="title">'+title+'</span>'+
    '<div class="ctrl">'+
    '<span title="Minimize" onclick="minimizePanel(_pid(this))"><span class="mi material-icons-round" style="font-size:14px">minimize</span></span>'+
    '<span title="Maximize" onclick="toggleMax(_pid(this))"><span class="mi material-icons-round" style="font-size:14px">crop_square</span></span>'+
    '<span class="close" title="Close" onclick="closePanel(_pid(this))"><span class="mi material-icons-round" style="font-size:14px">close</span></span>'+
    '</div></div>'+
    '<div class="panel-body" id="panel-body-'+id+'"></div>'+
    '<div class="panel-resize" onmousedown="startResize(event,_pid(this))"></div>';

  document.getElementById('panels').appendChild(panel);
  panel.addEventListener('mousedown', ()=>bringToFront(id));

  // Load content (potato: defer iframes until visible)
  const body = document.getElementById('panel-body-'+id);
  if(isSystem) {{
    // System loader dispatches on the panel TYPE (base id); the body element it
    // fills is the instance's own (passed directly), so instances stay distinct.
    loadSystemPanel(baseId, body);
  }} else if(def.route) {{
    if(PERF.lazyIframes) {{
      // Potato: tap-to-load placeholder until focused (keeps memory low), but it
      // is itself a content container — never a blank body.
      body.innerHTML = '<div class="panel-route-stage native-content" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;height:100%;text-align:center">'+
        '<span class="mi material-icons-round" style="font-size:48px;color:var(--hart-accent);cursor:pointer" data-route="'+def.route+'" onclick="loadIframe(_pid(this),this.dataset.route)">touch_app</span>'+
        '<div class="ds-body-sm ds-text-muted">Tap to load '+(def.title||id)+'</div></div>';
      body.dataset.route = def.route;
      body.dataset.loaded = '0';
    }} else {{
      renderRoutePanel(id, body, def.route, def.title||id);
    }}
  }} else {{
    body.innerHTML = '<div class="native-content">Panel: '+id+'</div>';
  }}

  panels[id] = {{el:panel, x, y, w:sz[0], h:sz[1], max:false, min:false, title:title, base:baseId}};
  // Open MAXIMIZED by default — the glass shell is a full desktop, so panels
  // should fill the workspace (Win/macOS "open large") rather than a tiny
  // cascade window. Floating bubbles (assistant) keep their compact size.
  if(!def.floating && !opts.noMax) applyMax(id);
  bringToFront(id);
  updateTaskbar();
  // #169: record this location on the shell-wide nav history (panel id == the
  // location). hartNav loads deferred; openPanel is only called post-load, so
  // window.HartNav is defined by the time a user opens anything.
  if(window.HartNav) HartNav.onOpen(id, title);
  if(startOpen) toggleStartMenu();
}}

function closePanel(id) {{
  const p = panels[id];
  if(!p) return;
  if(!PERF.potato) {{
    p.el.classList.add('closing');
    setTimeout(function(){{ p.el.remove(); delete panels[id]; updateTaskbar(); }}, 200);
  }} else {{
    p.el.remove(); delete panels[id]; updateTaskbar();
  }}
  if(focusedPanel===id) focusedPanel=null;
  // #169: drop the closed panel from nav history so back/forward never target a
  // window that no longer exists.
  if(window.HartNav) HartNav.onClose(id);
}}

function minimizePanel(id) {{
  const p = panels[id];
  if(!p) return;
  if(!PERF.potato) {{
    p.el.classList.add('minimizing');
    setTimeout(function(){{ p.el.style.display='none'; p.el.classList.remove('minimizing'); }}, 150);
  }} else {{
    p.el.style.display = 'none';
  }}
  p.min = true;
  updateTaskbar();
  // Potato: destroy iframe to free memory, will reload on restore
  if(PERF.destroyMinimized) {{
    const body = document.getElementById('panel-body-'+id);
    const iframe = body && body.querySelector('iframe');
    if(iframe) {{
      body.dataset.route = body.dataset.route || iframe.src.replace(NUNBA_BASE,'');
      iframe.remove();
      body.dataset.loaded = '0';
    }}
  }}
}}

// Lazy iframe loader (potato mode) — routes through the canonical staged loader
// so even the deferred path gets a skeleton + graceful reconnecting fallback.
function loadIframe(id, route) {{
  const body = document.getElementById('panel-body-'+id);
  if(body && body.dataset.loaded !== '1') {{
    const def = MANIFEST[id] || SYSTEM_PANELS[id] || {{}};
    renderRoutePanel(id, body, route, def.title||id);
    body.dataset.loaded = '1';
  }}
}}

// ═══ Canonical route-panel loader ═══
// Every iframe panel (agents/recipes/communities/…) ALWAYS gets a content
// container: a loading skeleton first, then the SPA iframe once it loads. If the
// backend never answers (SPA down / tier-dropped / offline) the iframe stays
// blank — so we watch it and swap in a graceful "reconnecting" empty state with a
// retry, never leaving the panel body blank.
function renderRoutePanel(id, body, route, title) {{
  if(!body) return;
  // 1) Always-visible content container: skeleton + (hidden) iframe stacked.
  body.innerHTML =
    '<div class="route-skeleton native-content" style="position:absolute;inset:0;z-index:2;background:transparent">'+
      dsSkeleton('list',5)+
    '</div>'+
    '<iframe class="route-frame" style="opacity:0;transition:opacity .25s" '+
      'src="'+NUNBA_BASE+route+'" loading="lazy"></iframe>';
  body.dataset.route = route;
  const frame = body.querySelector('.route-frame');
  const skel = body.querySelector('.route-skeleton');
  if(!frame) return;
  let settled = false;
  function reveal() {{
    if(settled) return; settled = true;
    frame.style.opacity = '1';
    if(skel) skel.remove();
  }}
  function reconnecting() {{
    if(settled) return; settled = true;
    if(frame) frame.remove();
    body.innerHTML =
      '<div class="route-empty native-content" style="display:flex;flex-direction:column;'+
        'align-items:center;justify-content:center;gap:12px;height:100%;text-align:center;padding:24px">'+
        '<span class="mi material-icons-round" style="font-size:46px;color:var(--hart-muted)">cloud_off</span>'+
        '<div class="ds-title-sm">Reconnecting&hellip;</div>'+
        '<div class="ds-body-sm ds-text-muted" style="max-width:280px">'+
          (title||'This view')+" couldn't load yet. It will appear once the connection is restored."+
        '</div>'+
        '<button class="ds-btn ds-btn-tonal ds-btn-sm" type="button" '+
          'onclick="retryRoutePanel(\\''+id+'\\')">Retry</button>'+
      '</div>';
  }}
  // iframe onload fires for both real content AND error pages (e.g. a 404 when the
  // Nunba SPA bundle isn't served — the W2/#116 gap). The frame is opacity:0 until
  // reveal(), so VERIFY the route actually serves 2xx before revealing; a non-2xx
  // routes to the graceful empty state instead of unveiling a raw "Not Found" page
  // (the "url not working" the steward saw on real HW). NUNBA_BASE is same-origin
  // (:6800), so this status check costs no CORS round-trip. A never-loading frame
  // still falls to the 8s timeout → reconnecting.
  frame.addEventListener('load', function(){{
    fetch(NUNBA_BASE+route, {{method:'GET', cache:'no-store'}})
      .then(function(r){{ if(r.ok) reveal(); else reconnecting(); }})
      .catch(function(){{ reveal(); }});
  }});
  frame.addEventListener('error', reconnecting);
  setTimeout(function(){{ if(!settled) reconnecting(); }}, 8000);
}}

// Retry handler for the reconnecting empty state — re-stage the same route.
function retryRoutePanel(id) {{
  const body = document.getElementById('panel-body-'+id);
  if(!body) return;
  const route = body.dataset.route;
  const def = MANIFEST[id] || SYSTEM_PANELS[id] || {{}};
  if(route) renderRoutePanel(id, body, route, def.title||id);
}}

// Canonical maximize: fill the workspace (below the top bar, above the taskbar).
function applyMax(id) {{
  const p = panels[id];
  if(!p || p.max) return;
  p.el.style.left = '0'; p.el.style.top = '0';
  p.el.style.width = '100vw'; p.el.style.height = 'calc(100vh - var(--hart-topbar-height) - 44px)';
  p.el.style.borderRadius = '0';
  p.el.classList.add('maximized');
  p.max = true;
}}
// Canonical restore: back to the remembered float geometry.
function applyRestore(id) {{
  const p = panels[id];
  if(!p || !p.max) return;
  p.el.style.left = p.x+'px'; p.el.style.top = p.y+'px';
  p.el.style.width = p.w+'px'; p.el.style.height = p.h+'px';
  p.el.style.borderRadius = '';
  p.el.classList.remove('maximized');
  p.max = false;
}}
function toggleMax(id) {{
  const p = panels[id];
  if(!p) return;
  if(p.max) applyRestore(id); else applyMax(id);
}}

function bringToFront(id) {{
  const p = panels[id];
  if(!p) return;
  if(p.min) {{
    p.el.style.display=''; p.min=false;
    // Potato: reload iframe if it was destroyed on minimize
    if(PERF.destroyMinimized) {{
      const body = document.getElementById('panel-body-'+id);
      if(body && body.dataset.route && body.dataset.loaded === '0') {{
        loadIframe(id, body.dataset.route);
      }}
    }}
  }}
  p.el.style.zIndex = ++panelZ;
  Object.keys(panels).forEach(k=>panels[k].el.classList.toggle('focused',k===id));
  focusedPanel = id;
  mru = [id, ...mru.filter(x=>x!==id)];
  updateTaskbar();
}}

// Taskbar chip click: clicking the FOCUSED window's chip minimizes it (the
// Win11/macOS dock gesture); otherwise raise/un-minimize it. Was bringToFront-
// only, so there was no way to minimize a window via the taskbar.
function taskbarClick(id) {{
  const p = panels[id];
  if(!p) return;
  if(id===focusedPanel && !p.min) {{ minimizePanel(id); }}
  else {{ bringToFront(id); }}
}}

// ═══ Drag & Resize ═══
// rAF-batched + GPU transform (matches hartDesktop.js icon + touch-titlebar
// drag). The old handler wrote p.el.style.left/top on EVERY mousemove (layout-
// inducing) AND read window.innerWidth/innerHeight right after dirtying layout
// (a forced reflow) 60-125x/sec — on a software-composited box cairo re-raster-
// ised the whole glass per event and pinned the UI thread (the drag freeze).
// Now a MOVE only sets transform:translate (no layout) and commits left/top once
// on drop; innerWidth/innerHeight are cached at dragstart so the commit never
// reads layout it just wrote. Resize is rAF-coalesced so width/height change at
// most once per frame instead of per event.
let dragState = null;
function startDrag(e, id) {{
  if(e.button!==0) return;
  const p = panels[id];
  if(!p||p.max) return;
  dragState = {{id, mode:'move', sx:e.clientX, sy:e.clientY,
    ox:p.el.offsetLeft, oy:p.el.offsetTop,
    vw:window.innerWidth, vh:window.innerHeight, dx:0, dy:0, raf:0}};
  p.el.style.willChange = 'transform';
  // Notify the effects module (snap-zones) of the canonical drag start so it
  // reuses THIS drag lifecycle instead of forking its own mousedown detection.
  // No-op unless hartEffects.js is loaded + effects are enabled (not potato).
  try{{ window.dispatchEvent(new CustomEvent('hart:dragstart',{{detail:{{id:id}}}})); }}catch(_e){{}}
  e.preventDefault();
}}
function startResize(e, id) {{
  if(e.button!==0) return;
  const p = panels[id];
  if(!p) return;
  dragState = {{id, mode:'resize', sx:e.clientX, sy:e.clientY,
    ow:p.el.offsetWidth, oh:p.el.offsetHeight, dx:0, dy:0, raf:0}};
  e.preventDefault();
}}
function _dragFrame() {{
  const d = dragState;
  if(!d) return;
  d.raf = 0;
  const p = panels[d.id];
  if(!p) return;
  if(d.mode==='move') {{
    // GPU-composited move — translate only, no layout. left/top commit on drop.
    p.el.style.transform = 'translate('+d.dx+'px,'+d.dy+'px)';
  }} else {{
    const nw = Math.max(320, d.ow+d.dx), nh = Math.max(240, d.oh+d.dy);
    p.el.style.width = nw+'px'; p.el.style.height = nh+'px';
    p.w = nw; p.h = nh;
  }}
}}
document.addEventListener('mousemove', e=>{{
  const d = dragState;
  if(!d) return;
  d.dx = e.clientX - d.sx; d.dy = e.clientY - d.sy;
  if(d.raf) return;                                  // already scheduled this frame
  d.raf = requestAnimationFrame(_dragFrame);
}});
document.addEventListener('mouseup', e=>{{
  const d = dragState;
  dragState = null;
  if(!d) return;
  if(d.raf) {{ cancelAnimationFrame(d.raf); d.raf = 0; }}
  const p = panels[d.id];
  if(p) {{
    p.el.style.willChange = '';
    if(d.mode==='move') {{
      // Commit the GPU transform back to left/top, clamped on-screen using the
      // innerWidth/innerHeight cached at dragstart (no forced reflow). Keep the
      // titlebar >=80px reachable so a window can't be lost under the bars.
      p.el.style.transform = '';
      const KEEP=80, TOP=40, TASK=44;
      let nx = d.ox+d.dx, ny = d.oy+d.dy;
      nx = Math.min(Math.max(nx, KEEP - p.el.offsetWidth), d.vw - KEEP);
      ny = Math.min(Math.max(ny, TOP), d.vh - TASK - 28);
      p.el.style.left = nx+'px'; p.el.style.top = ny+'px';
      p.x = nx; p.y = ny;
    }}
  }}
  // Hand the effects module the final pointer position + the panel id so a
  // snap-zone it highlighted during the drag can commit via the canonical
  // snapPanel. Uses the id captured before dragState was cleared.
  if(d.mode==='move'){{
    try{{ window.dispatchEvent(new CustomEvent('hart:dragend',
      {{detail:{{id:d.id, x:e.clientX, y:e.clientY}}}})); }}catch(_e){{}}
  }}
}});

// ═══ System Panels (design system) ═══
function loadSystemPanel(id, body) {{
  const apis = (SYSTEM_PANELS[id]||{{}}).apis || [];
  // Show skeleton loader while fetching
  body.innerHTML = '<div class="native-content" id="sys-'+id+'">'+dsSkeleton('panel',3)+'</div>';
  const container = document.getElementById('sys-'+id);

  if(id==='settings') loadSettingsPanel(container);
  else if(id==='hw_monitor') loadHardwareMonitor(container, apis);
  else if(id==='security') loadSecurityCenter(container, apis);
  else if(id==='network') loadNetworkPanel(container, apis);
  else if(id==='event_log') loadEventLog(container, apis);
  else if(id==='drivers') loadDriversPanel(container);
  else if(id==='audio') loadAudioPanel(container);
  else if(id==='bluetooth') loadBluetoothPanel(container);
  else if(id==='power') loadPowerPanel(container);
  else if(id==='display') loadDisplayPanel(container);
  else if(id==='flash') {{ if(window.loadFlashWizard) window.loadFlashWizard(container); }}
  else if(id==='remote_desktop') loadRemoteDesktopPanel(container, apis);
  else if(id==='hart_identity') loadHartIdentityPanel(container, apis);
  else if(id==='self_build') loadSelfBuildPanel(container, apis);
  else if(id==='task_manager') loadTaskManagerPanel(container);
  else if(id==='storage_manager') loadStoragePanel(container);
  else if(id==='startup_apps') loadStartupAppsPanel(container);
  else if(id==='bluetooth_manager') loadBluetoothManagerPanel(container);
  else if(id==='print_manager') loadPrintManagerPanel(container);
  else if(id==='media_library') loadMediaLibraryPanel(container);
  else if(id==='file_manager') loadFileManagerPanel(container);
  else if(id==='terminal') loadTerminalPanel(container);
  else if(id==='user_accounts') loadUserAccountsPanel(container);
  else if(id==='notification_center') loadNotificationCenterPanel(container);
  else if(id==='updates') loadUpdatesPanel(container);
  else if(id==='backup_restore') loadBackupRestorePanel(container);
  else if(id==='devices') loadDevicesPanel(container);
  else if(id==='i18n') loadI18nPanel(container);
  else if(id==='accessibility') loadAccessibilityPanel(container);
  else if(id==='screenshot') loadScreenshotPanel(container);
  else if(id==='firewall') loadFirewallPanel(container);
  else if(id==='default_apps') loadDefaultAppsPanel(container);
  else if(id==='font_manager') loadFontManagerPanel(container);
  else if(id==='sound_manager') loadSoundManagerPanel(container);
  else if(id==='clipboard_manager') loadClipboardPanel(container);
  else if(id==='datetime') loadDateTimePanel(container);
  else if(id==='wallpaper_manager') loadWallpaperPanel(container);
  else if(id==='input_methods') loadInputMethodsPanel(container);
  else if(id==='nightlight') loadNightLightPanel(container);
  else if(id==='workspaces') loadWorkspacesPanel(container);
  else if(id==='calculator') loadCalculatorPanel(container);
  else if(id==='image_viewer') loadImageViewerPanel(container);
  else if(id==='notes_app') loadNotesAppPanel(container);
  else if(id==='app_store') loadAppStorePanel(container);
  else if(id==='app_permissions') loadAppPermissionsPanel(container);
  else if(id==='battery_monitor') loadBatteryMonitorPanel(container);
  else if(id==='wifi_manager') loadWiFiManagerPanel(container);
  else if(id==='vpn_manager') loadVPNManagerPanel(container);
  else if(id==='trash_bin') loadTrashBinPanel(container);
  else if(id==='webcam_viewer') loadWebcamViewerPanel(container);
  else if(id==='scanner') loadScannerPanel(container);
  else if(id==='weather_widget') loadWeatherPanel(container);
  else if(id==='keyboard_shortcuts') loadKeyboardShortcutsPanel(container);
  else if(id==='credits') loadCreditsPanel(container);
  else container.innerHTML = '<div class="ds-body-md ds-text-muted">Panel: '+id+'</div>';
}}

// ═══ Settings (aggregator) ═══
// NOT a new settings app: a categorized INDEX whose every tile OPENS AN EXISTING
// panel via openPanel (single-instance reuse). SETTINGS_SECTIONS is composition
// only (section -> existing panel ids, from shell_manifest); each id's title /
// icon / colour is resolved here from the live MANIFEST/SYSTEM_PANELS, so no
// panel definition is duplicated. A search box filters the tiles in place, the
// same data-title convention the start menu uses.
function loadSettingsPanel(el) {{
  const sections = SETTINGS_SECTIONS || [];
  let html = '<div class="ds-panel-grid ds-fade-in settings-root">'+
    '<div class="ds-panel-title">Settings</div>'+
    '<input class="start-search settings-search" id="settings-search" placeholder="Search settings..." '+
      'oninput="filterSettings(this.value)" aria-label="Search settings">';
  sections.forEach(function(sec){{
    const ids = (sec.ids||[]).filter(function(id){{ return !!(MANIFEST[id]||SYSTEM_PANELS[id]); }});
    if(!ids.length) return;
    html += '<div class="settings-section" data-section="'+sec.title+'">'+
      '<div class="ds-section-label">'+sec.title+'</div><div class="start-grid">';
    ids.forEach(function(id){{
      const p = MANIFEST[id] || SYSTEM_PANELS[id] || {{}};
      const t = p.title || id;
      html += '<div class="start-item settings-tile" data-id="'+id+'" data-title="'+t+'" '+
        'onclick="openPanel(this.dataset.id)">';
      html += '<span class="mi material-icons-round"'+miStyle(p)+'>'+(p.icon||'settings')+'</span>';
      html += '<span class="label">'+t+'</span></div>';
    }});
    html += '</div></div>';
  }});
  if(!sections.length) {{
    html += '<div class="ds-body-md ds-text-muted">No settings available.</div>';
  }}
  html += '</div>';
  el.innerHTML = html;
}}

// Filter Settings tiles in place (hides empty sections) — mirrors filterStart.
function filterSettings(q) {{
  const root = document.querySelector('.settings-root');
  if(!root) return;
  const lq = (q||'').toLowerCase();
  root.querySelectorAll('.settings-section').forEach(function(sec){{
    let anyVisible = false;
    sec.querySelectorAll('.settings-tile').forEach(function(el){{
      const title = (el.dataset.title||'').toLowerCase();
      const show = title.includes(lq);
      el.style.display = show ? '' : 'none';
      if(show) anyVisible = true;
    }});
    sec.style.display = anyVisible ? '' : 'none';
  }});
}}

// Backward compat wrappers (used in old code references)
function metricBar(l,p,u,s) {{ return dsMetricBar(l,p,u,s); }}
function statusRow(i,l,v,c) {{ return dsStatusRow(i,l,v,c); }}

function loadHardwareMonitor(el, apis) {{
  Promise.all(apis.map(u=>fetch(BACKEND+u,{{signal:_sig(3000)}}).then(r=>r.json()).catch(()=>({{}}))))
    .then(([sys,caps])=>{{
      const cpu=sys.cpu_percent||0, ram_used=sys.ram_used_gb||0, ram_total=sys.ram_total_gb||0;
      const disk_used=sys.disk_used_gb||0, disk_total=sys.disk_total_gb||0;
      const tier=caps.tier_name||sys.tier||'unknown', uptime=sys.uptime||'';
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in">'+
        '<div class="ds-panel-header">'+
        '<span class="ds-panel-title">Hardware</span>'+
        '<span class="ds-chip"><span class="ds-chip-dot" style="background:var(--hart-accent)"></span>'+tier+'</span>'+
        '</div>'+
        dsMetricBar('CPU', cpu, '%')+
        dsMetricBar('RAM', ram_total>0?Math.round(ram_used/ram_total*100):0, '%', ram_used.toFixed(1)+' / '+ram_total.toFixed(1)+' GB')+
        dsMetricBar('Disk', disk_total>0?Math.round(disk_used/disk_total*100):0, '%', disk_used.toFixed(0)+' / '+disk_total.toFixed(0)+' GB')+
        '<div class="ds-label-sm ds-text-muted">Uptime: '+uptime+'</div>'+
        '</div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Hardware info unavailable</div>'; }});
}}

function loadSecurityCenter(el, apis) {{
  Promise.all(apis.map(u=>fetch(BACKEND+u,{{signal:_sig(3000)}}).then(r=>r.json()).catch(()=>({{}}))))
    .then(([health,guardrail])=>{{
      const ghash = guardrail.guardrail_hash||'unknown';
      const wm = health.world_model||{{}};
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in">'+
        '<div class="ds-panel-title">Security</div>'+
        '<div class="ds-stagger">'+
        dsStatusRow('shield', 'Guardrail Hash', ghash.substring(0,16)+'...', 'var(--hart-active)', {{sublabel:'Structural integrity verified'}})+
        dsStatusRow('verified_user', 'Integrity', health.status==='ok'?'Verified':'Check Required',
            health.status==='ok'?'var(--hart-active)':'var(--hart-caution)')+
        dsStatusRow('psychology', 'World Model', wm.status||'disconnected',
            wm.status==='healthy'?'var(--hart-active)':'var(--hart-muted)')+
        '</div></div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Security info unavailable</div>'; }});
}}

function wifiConnect(ssid) {{
  dsPrompt('Connect to WiFi', 'Enter password for <strong>'+ssid+'</strong><br><span class="ds-label-sm ds-text-muted">Leave empty for open networks</span>', {{
    type:'password', placeholder:'Password', okLabel:'Connect'
  }}).then(function(pwd){{
    if(pwd===null) return;
    const body = {{ssid: ssid}};
    if(pwd) body.password = pwd;
    showToast('WiFi', 'Connecting to '+ssid+'...', 'info');
    fetch(SHELL+'/api/shell/network/wifi/connect', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(body), signal:_sig(35000)
    }}).then(r=>r.json()).then(d=>{{
      if(d.success) {{ showToast('WiFi', 'Connected to '+ssid, 'success'); loadNetworkPanel(document.getElementById('sys-network'),
        (SYSTEM_PANELS['network']||{{}}).apis||[]); }}
      else dsAlert('Connection Failed', d.error||'Unknown error', 'error');
    }}).catch(e=>dsAlert('Connection Error', e.message, 'error'));
  }});
}}
function wifiDisconnect() {{
  dsConfirm('Disconnect WiFi', 'Are you sure you want to disconnect from WiFi?', {{okLabel:'Disconnect', danger:true}}).then(function(ok){{
    if(!ok) return;
    fetch(SHELL+'/api/shell/network/wifi/disconnect', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body:'{{}}', signal:_sig(15000)
    }}).then(r=>r.json()).then(d=>{{
      if(d.success) {{ showToast('WiFi', 'Disconnected', 'info'); loadNetworkPanel(document.getElementById('sys-network'),
        (SYSTEM_PANELS['network']||{{}}).apis||[]); }}
      else dsAlert('Error', d.error||'Disconnect failed', 'error');
    }}).catch(e=>dsAlert('Error', e.message, 'error'));
  }});
}}

function loadNetworkPanel(el, apis) {{
  Promise.all([
    ...apis.map(u=>fetch(BACKEND+u,{{signal:_sig(3000)}}).then(r=>r.json()).catch(()=>({{}}))),
    fetch(SHELL+'/api/shell/network/wifi',{{signal:_sig(3000)}}).then(r=>r.json()).catch(()=>({{}})),
    fetch(SHELL+'/api/shell/network/status',{{signal:_sig(3000)}}).then(r=>r.json()).catch(()=>({{}}))
  ]).then(results=>{{
      const topo = results[0]||{{}};
      const wifi = results[results.length-2]||{{}};
      const netStatus = results[results.length-1]||{{}};
      const nodes = topo.nodes||[];
      const connected = wifi.connected||{{}};
      const networks = wifi.networks||[];
      const gateway = netStatus.gateway||'';
      let wifiHtml = '';
      if(connected.ssid) {{
        wifiHtml = dsCard(
          '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-2">'+
          '<span class="mi material-icons-round ds-text-active" style="font-size:28px">wifi</span>'+
          '<div class="ds-title-sm ds-text-active">'+connected.ssid+'</div>'+
          '<div class="ds-label-sm ds-text-muted">'+(connected.ip||'')+(gateway?' &middot; GW '+gateway:'')+'</div>'+
          dsBtn('Disconnect', {{variant:'secondary', cls:'ds-btn-sm', onclick:"wifiDisconnect()"}})+
          '</div>', {{elevated:true}});
      }}
      let html = '<div class="ds-panel-grid ds-fade-in">';
      html += '<div class="ds-panel-title">Network</div>';
      html += '<div class="ds-flex ds-gap-3 ds-flex-wrap">';
      html += dsCard('<div class="ds-metric"><div class="ds-metric-value ds-text-accent">'+nodes.length+'</div><div class="ds-metric-label">Hive Peers</div></div>', {{elevated:true}});
      html += wifiHtml;
      html += '</div>';
      if(nodes.length>0) {{
        html += '<div class="ds-section-label">Connected Peers</div><div class="ds-stagger">';
        html += nodes.slice(0,6).map(n=>
          dsStatusRow('dns', (n.node_id||'').substring(0,12)+'...', n.status||'active',
            'var(--hart-active)', {{sublabel:n.ip||''}})
        ).join('');
        html += '</div>';
      }}
      if(networks.length>0) {{
        const available = networks.filter(n=>!n.active);
        if(available.length>0) {{
          html += '<div class="ds-section-label">Available WiFi Networks</div><div class="ds-stagger">';
          html += available.slice(0,6).map(n=>
            '<div class="ds-list-item ds-list-item-interactive" data-ssid="'+n.ssid+'" onclick="wifiConnect(this.dataset.ssid)">' +
            '<span class="mi material-icons-round ds-list-item-icon ds-text-accent">wifi</span>' +
            '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+n.ssid+'</div>'+
            '<div class="ds-list-item-secondary">'+n.security+'</div></div>' +
            '<span class="ds-list-item-trailing ds-text-muted">'+n.signal+'%</span></div>'
          ).join('');
          html += '</div>';
        }}
      }}
      html += '</div>';
      el.innerHTML = html;
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Network info unavailable</div>'; }});
}}

function loadHartIdentityPanel(el, apis) {{
  const profileUrl = apis[0] || '/api/onboarding/profile';
  const statusUrl = apis[1] || '/api/onboarding/status';
  fetch(SHELL+statusUrl,{{signal:_sig(3000)}}).then(r=>r.json()).then(st=>{{
    if(!st.onboarded) {{
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">My HART</div>'+
        '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-3" style="padding:40px 0">'+
        '<span class="mi material-icons-round ds-text-muted" style="font-size:48px">person_outline</span>'+
        '<div class="ds-body-md ds-text-muted">You haven&#39;t lit your HART yet.</div>'+
        dsBtn('Light Your HART',{{variant:'primary', onclick:"fetch(SHELL+'/api/onboarding/start',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{user_id:'1'}})}}).then(()=>showToast('Onboarding','Opening onboarding...','info')).catch(()=>{{}})"}})+'</div></div>';
      return;
    }}
    fetch(SHELL+profileUrl,{{signal:_sig(3000)}}).then(r=>r.json()).then(p=>{{
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">My HART</div>'+
        '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-3" style="padding:24px 0">'+
        '<span class="mi material-icons-round ds-text-accent" style="font-size:56px">badge</span>'+
        '<div class="ds-display-sm ds-text-accent">'+(p.hart_name||p.name||'Unknown')+'</div>'+
        (p.hart_tag?'<div class="ds-title-sm ds-text-muted">'+p.hart_tag+'</div>':'')+
        '</div><div class="ds-stagger">'+
        (p.element?dsStatusRow('flare','Element',p.element,'var(--hart-accent)'):'')+
        (p.spirit?dsStatusRow('pets','Spirit',p.spirit,'var(--hart-active)'):'')+
        (p.passion?dsStatusRow('favorite','Passion',p.passion,'var(--hart-accent)'):'')+
        (p.escape?dsStatusRow('landscape','Escape',p.escape,'var(--hart-active)'):'')+
        (p.locale?dsStatusRow('language','Language',p.locale,'var(--hart-muted)'):'')+
        '</div></div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px">Could not load identity</div>'; }});
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px">Identity service unavailable</div>'; }});
}}

function selfBuildInstall() {{
  dsPrompt('Install Package','Enter NixOS package name (e.g. <code>htop</code>, <code>nodejs_20</code>)',{{
    placeholder:'Package name', okLabel:'Stage Install'
  }}).then(function(pkg){{
    if(!pkg) return;
    showToast('Self-Build','Staging '+pkg+'...','info');
    fetch(SHELL+'/api/system/self-build/install',{{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{package:pkg}}), signal:_sig(10000)
    }}).then(r=>r.json()).then(d=>{{
      if(d.success) {{ showToast('Self-Build','Staged: '+pkg,'success'); loadSelfBuildPanel(document.getElementById('sys-self_build'),
        (SYSTEM_PANELS['self_build']||{{}}).apis||[]); }}
      else dsAlert('Stage Failed', d.error||'Unknown error', 'error');
    }}).catch(e=>dsAlert('Error', e.message, 'error'));
  }});
}}
function selfBuildRemove(pkg) {{
  dsConfirm('Remove Package','Remove <strong>'+pkg+'</strong> from runtime config?',{{okLabel:'Remove',danger:true}}).then(function(ok){{
    if(!ok) return;
    fetch(SHELL+'/api/system/self-build/remove',{{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{package:pkg}}), signal:_sig(10000)
    }}).then(r=>r.json()).then(d=>{{
      if(d.success) {{ showToast('Self-Build','Removed: '+pkg,'info'); loadSelfBuildPanel(document.getElementById('sys-self_build'),
        (SYSTEM_PANELS['self_build']||{{}}).apis||[]); }}
      else dsAlert('Remove Failed', d.error||'Unknown error', 'error');
    }}).catch(e=>dsAlert('Error', e.message, 'error'));
  }});
}}
function selfBuildTrigger(mode) {{
  dsConfirm('Trigger Build','Run <strong>'+mode+'</strong> build? This may take a few minutes.',{{okLabel:'Build'}}).then(function(ok){{
    if(!ok) return;
    showToast('Self-Build','Building ('+mode+')...','info');
    fetch(SHELL+'/api/system/self-build/trigger',{{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{mode:mode}}), signal:_sig(600000)
    }}).then(r=>r.json()).then(d=>{{
      if(d.success) showToast('Self-Build','Build complete!','success');
      else dsAlert('Build Failed', d.error||d.stderr||'Unknown error', 'error');
      loadSelfBuildPanel(document.getElementById('sys-self_build'),
        (SYSTEM_PANELS['self_build']||{{}}).apis||[]);
    }}).catch(e=>dsAlert('Build Error', e.message, 'error'));
  }});
}}

function loadSelfBuildPanel(el, apis) {{
  const statusUrl = apis[0] || '/api/system/self-build/status';
  const pkgsUrl = apis[1] || '/api/system/self-build/packages';
  Promise.all([
    fetch(SHELL+statusUrl,{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}})),
    fetch(SHELL+pkgsUrl,{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}}))
  ]).then(([status,pkgData])=>{{
    const gen = status.generation||'?';
    const version = status.nixos_version||'unknown';
    const builds = status.recent_builds||[];
    const pkgs = pkgData.packages||[];
    let html = '<div class="ds-panel-grid ds-fade-in">';
    html += '<div class="ds-panel-header"><span class="ds-panel-title">Self-Build</span>'+
      '<span class="ds-chip"><span class="ds-chip-dot" style="background:var(--hart-active)"></span>Gen '+gen+'</span></div>';
    html += '<div class="ds-flex ds-gap-3 ds-flex-wrap">';
    html += dsCard('<div class="ds-metric"><div class="ds-metric-value ds-text-accent">'+version+'</div><div class="ds-metric-label">NixOS Version</div></div>',{{elevated:true}});
    html += dsCard('<div class="ds-metric"><div class="ds-metric-value ds-text-active">'+pkgs.length+'</div><div class="ds-metric-label">Runtime Packages</div></div>',{{elevated:true}});
    html += '</div>';
    html += '<div class="ds-flex ds-gap-2">'+
      dsBtn('Install Package',{{variant:'primary', cls:'ds-btn-sm', onclick:'selfBuildInstall()'}})+
      dsBtn('Dry Run',{{variant:'secondary', cls:'ds-btn-sm', onclick:"selfBuildTrigger('dry-run')"}})+
      dsBtn('Apply (Switch)',{{variant:'secondary', cls:'ds-btn-sm', onclick:"selfBuildTrigger('switch')"}})+
      '</div>';
    if(pkgs.length>0) {{
      html += '<div class="ds-section-label">Runtime Packages</div><div class="ds-stagger">';
      html += pkgs.map(p=>
        '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-accent">inventory_2</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+p+'</div></div>'+
        '<span class="ds-list-item-trailing" style="cursor:pointer" data-pkg="'+p+'" onclick="selfBuildRemove(this.dataset.pkg)">' +
        '<span class="mi material-icons-round ds-text-muted" style="font-size:18px">delete_outline</span></span></div>'
      ).join('');
      html += '</div>';
    }}
    if(builds.length>0) {{
      html += '<div class="ds-section-label">Recent Builds</div><div class="ds-stagger">';
      html += builds.slice(0,5).map(b=>
        dsStatusRow(b.success?'check_circle':'error', b.mode||'build',
          (b.timestamp||'').substring(0,19), b.success?'var(--hart-active)':'var(--hart-caution)')
      ).join('');
      html += '</div>';
    }}
    html += '</div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Self-Build unavailable</div>'; }});
}}

// ═══ Keyboard Shortcuts ═══
function loadKeyboardShortcutsPanel(el) {{
  fetch(SHELL+'/api/shell/shortcuts',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const profile = data.profile||'windows';
    const profiles = data.available_profiles||['windows','mac'];
    const sc = data.shortcuts||{{}};
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-header"><span class="ds-panel-title">Keyboard Shortcuts</span>'+
      '<div class="ds-flex ds-gap-2">';
    profiles.forEach(p=>{{
      html += dsBtn(p.charAt(0).toUpperCase()+p.slice(1),{{
        variant:p===profile?'primary':'secondary', cls:'ds-btn-sm',
        onclick:"fetch(SHELL+'/api/shell/shortcuts/profile',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{profile:'"+p+"'}})}}).then(()=>{{showToast('Shortcuts','Switched to "+p+"','success');loadKeyboardShortcutsPanel(document.getElementById('sys-keyboard_shortcuts'))}})"
      }});
    }});
    html += '</div></div>';
    const groups = {{
      'Window Management': ['close_window','minimize','maximize','snap_left','snap_right','switch_apps','switch_windows'],
      'Navigation': ['overview','app_grid','search','workspace_left','workspace_right','move_workspace_left','move_workspace_right'],
      'System': ['lock_screen','file_manager','terminal','browser','calculator','task_manager','screenshot','screenshot_window','screenshot_area'],
      'Editing': ['copy','paste','cut','undo','redo','select_all','save','find'],
    }};
    Object.keys(groups).forEach(group=>{{
      html += '<div class="ds-section-label">'+group+'</div><div class="ds-stagger">';
      groups[group].forEach(key=>{{
        if(!sc[key]) return;
        const label = key.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
        html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-muted">keyboard</span>'+
          '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+label+'</div></div>'+
          '<span class="ds-list-item-trailing"><code style="background:#1a1a1a;padding:2px 8px;border-radius:4px;font-size:12px;color:var(--hart-accent)">'+sc[key]+'</code></span></div>';
      }});
      html += '</div>';
    }});
    html += '</div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Keyboard shortcuts unavailable</div>'; }});
}}

// ═══ Task Manager ═══
function loadTaskManagerPanel(el) {{
  Promise.all([
    fetch(SHELL+'/api/shell/tasks/processes',{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}})),
    fetch(SHELL+'/api/shell/tasks/resources',{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}}))
  ]).then(([procData,res])=>{{
    const procs = procData.processes||[];
    const cpu = res.cpu_percent||0, mem = res.memory_percent||0;
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Task Manager</div>';
    html += dsMetricBar('CPU', cpu, '%')+dsMetricBar('Memory', mem, '%');
    html += '<div class="ds-section-label">Processes ('+procs.length+')</div><div class="ds-stagger">';
    html += procs.slice(0,20).map(p=>
      '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-accent">memory</span>'+
      '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+p.name+'</div>'+
      '<div class="ds-list-item-secondary">PID '+p.pid+' &middot; CPU '+((p.cpu_percent||0).toFixed(1))+'% &middot; Mem '+((p.memory_percent||0).toFixed(1))+'%</div></div>'+
      '<span class="ds-list-item-trailing" style="cursor:pointer" onclick="taskKill('+p.pid+')">'+
      '<span class="mi material-icons-round ds-text-muted" style="font-size:18px">close</span></span></div>'
    ).join('');
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Task info unavailable</div>'; }});
}}
function taskKill(pid) {{
  dsConfirm('End Process','Kill process PID '+pid+'?',{{okLabel:'Kill',danger:true}}).then(function(ok){{
    if(!ok) return;
    fetch(SHELL+'/api/shell/tasks/kill',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{pid:pid}})}}
    ).then(r=>r.json()).then(d=>{{
      if(d.success) {{ showToast('Task Manager','Process killed','info'); loadTaskManagerPanel(document.getElementById('sys-task_manager')); }}
      else dsAlert('Error', d.error||'Failed','error');
    }}).catch(e=>dsAlert('Error',e.message,'error'));
  }});
}}

// ═══ Storage ═══
function loadStoragePanel(el) {{
  Promise.all([
    fetch(SHELL+'/api/shell/storage',{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}})),
    fetch(SHELL+'/api/shell/storage/cleanup',{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}}))
  ]).then(([st,cl])=>{{
    const disks = st.disks||[];
    const cleanable = cl.total_cleanable_mb||0;
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Storage</div>';
    disks.forEach(d=>{{
      html += dsMetricBar(d.mountpoint||d.device, d.percent||0, '%', (d.used_gb||0).toFixed(1)+' / '+(d.total_gb||0).toFixed(1)+' GB');
    }});
    if(cleanable>0) html += '<div class="ds-flex ds-gap-2" style="margin-top:8px">'+
      '<div class="ds-body-md ds-text-muted">'+(cleanable/1024).toFixed(1)+' GB cleanable</div>'+
      dsBtn('Clean Up',{{variant:'secondary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/shell/storage/clean',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}}).then(()=>showToast('Storage','Cleaned up','success'))"}})+'</div>';
    html += '</div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Storage info unavailable</div>'; }});
}}

// ═══ Startup Apps ═══
function loadStartupAppsPanel(el) {{
  fetch(SHELL+'/api/shell/startup',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const apps = data.apps||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Startup Apps</div><div class="ds-stagger">';
    if(apps.length===0) html += '<div class="ds-body-md ds-text-muted">No startup apps configured</div>';
    else apps.forEach(a=>{{
      html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon '+(a.enabled?'ds-text-active':'ds-text-muted')+'">play_circle</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+a.name+'</div>'+
        '<div class="ds-list-item-secondary">'+(a.comment||a.exec||'')+'</div></div>'+
        '<label class="ds-switch"><input type="checkbox" '+(a.enabled?'checked':'')+' data-id="'+a.id+'" onchange="toggleStartup(this.dataset.id,this.checked)"><span class="ds-switch-slider"></span></label></div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Startup apps unavailable</div>'; }});
}}
function toggleStartup(id,en) {{
  fetch(SHELL+'/api/shell/startup/toggle',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:id,enabled:en}})}}).catch(()=>{{}});
}}

// ═══ Bluetooth Manager ═══
function loadBluetoothManagerPanel(el) {{
  fetch(SHELL+'/api/shell/bluetooth/status',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const devs = data.devices||[];
    const powered = data.powered!==false;
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-header"><span class="ds-panel-title">Bluetooth</span>'+
      '<span class="ds-chip"><span class="ds-chip-dot" style="background:var('+(powered?'--hart-active':'--hart-muted')+')"></span>'+(powered?'On':'Off')+'</span></div>';
    html += dsBtn('Scan',{{variant:'secondary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/shell/bluetooth/scan',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}}).then(()=>{{showToast('Bluetooth','Scanning...','info');setTimeout(()=>loadBluetoothManagerPanel(document.getElementById('sys-bluetooth_manager')),5000);}})"}});
    html += '<div class="ds-stagger">';
    devs.forEach(d=>{{
      html += dsStatusRow(d.connected?'bluetooth_connected':'bluetooth', d.name||d.address, d.connected?'Connected':'Paired',
        d.connected?'var(--hart-active)':'var(--hart-muted)',{{sublabel:d.address||''}});
    }});
    if(devs.length===0) html += '<div class="ds-body-md ds-text-muted">No paired devices</div>';
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Bluetooth unavailable</div>'; }});
}}

// ═══ Print Manager ═══
function loadPrintManagerPanel(el) {{
  fetch(SHELL+'/api/shell/printers',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const printers = data.printers||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Printers</div><div class="ds-stagger">';
    if(printers.length===0) html += '<div class="ds-body-md ds-text-muted">No printers found</div>';
    else printers.forEach(p=>{{
      html += dsStatusRow('print', p.name, p.is_default?'Default':p.status||'Ready',
        p.is_default?'var(--hart-accent)':'var(--hart-muted)',{{sublabel:p.location||p.device||''}});
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Printers unavailable</div>'; }});
}}

// ═══ Media Library ═══
function loadMediaLibraryPanel(el) {{
  fetch(SHELL+'/api/shell/media/status',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Media Library</div>';
    html += '<div class="ds-flex ds-gap-3 ds-flex-wrap">';
    html += dsCard('<div class="ds-metric"><div class="ds-metric-value ds-text-accent">'+(data.photo_count||0)+'</div><div class="ds-metric-label">Photos</div></div>',{{elevated:true}});
    html += dsCard('<div class="ds-metric"><div class="ds-metric-value ds-text-active">'+(data.video_count||0)+'</div><div class="ds-metric-label">Videos</div></div>',{{elevated:true}});
    html += dsCard('<div class="ds-metric"><div class="ds-metric-value ds-text-muted">'+(data.audio_count||0)+'</div><div class="ds-metric-label">Audio</div></div>',{{elevated:true}});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Media library unavailable</div>'; }});
}}

// ═══ File Manager ═══
function loadFileManagerPanel(el) {{
  // Delegates to the canonical File Explorer module (static/hartFiles.js),
  // which wires to the SAME /api/shell/files/* backend. No parallel browser.
  if (window.HartFiles && window.HartFiles.mount) {{ window.HartFiles.mount(el); return; }}
  el.innerHTML = '<div class="ds-body-md ds-text-muted">File manager loading…</div>';
}}
function browseDir(path) {{
  // Legacy entry kept for any stray caller — routes into the canonical module.
  if (window.HartFiles && window.HartFiles.navigate) window.HartFiles.navigate(path);
}}

// ═══ Terminal ═══
function loadTerminalPanel(el) {{
  // #138 — IDEMPOTENT mount. A periodic panel re-render (or a re-open) must NOT
  // wipe a terminal that is mid-command: re-rendering replaces #term-output, so
  // the running fetch's output node detaches and the session scrollback is lost.
  // If a terminal is already live in this body, leave it (and any in-flight exec)
  // untouched instead of recreating it.
  if(el.querySelector && el.querySelector('#term-output')) return;
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Terminal</div>'+
    '<div style="background:#0d0d0d;border-radius:8px;padding:12px;font-family:monospace;min-height:200px;position:relative">'+
    '<div id="term-output" style="color:#a0ffa0;white-space:pre-wrap;max-height:280px;overflow-y:auto;font-size:13px;line-height:1.5"></div>'+
    '<div style="display:flex;align-items:center;margin-top:8px">'+
    '<span style="color:#a0ffa0;margin-right:4px">$</span>'+
    '<input id="term-input" type="text" style="flex:1;background:transparent;border:none;color:#a0ffa0;font-family:monospace;font-size:13px;outline:none" '+
    'placeholder="Type command..." onkeydown="if(event.key===&quot;Enter&quot;)termExec()">'+
    '</div></div></div>';
}}
function termExec() {{
  // #138 — never launch a second command while one is in flight. A re-entrant
  // call (Enter mashed, or the panel re-rendering its input) would spin up a
  // SECOND AbortController/fetch on the same budget and make the CPU-pegged
  // software-rendered shell feel hung. One command at a time; the busy flag
  // lives on window so it survives any panel re-render and is never recreated.
  if(window._hartTermBusy) return;
  var inp = document.getElementById('term-input');
  var out = document.getElementById('term-output');
  if(!inp||!out) return;
  var cmd = inp.value.trim();
  if(!cmd) return;
  inp.value = '';
  out.textContent += '$ '+cmd+'\\n';
  window._hartTermBusy = true;
  // Longer budget than the old 30s: a real command (build, large dir scan) on a
  // CPU-pegged shell can exceed 30s, and a premature abort surfaced the cryptic
  // "Fetch is aborted". 120s + a friendly timeout message on AbortError.
  fetch(SHELL+'/api/shell/terminal/exec',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{command:cmd}}),signal:_sig(120000)}}
  ).then(function(r){{ return r.json(); }}).then(function(d){{
    out.textContent += (d.stdout||'')+(d.stderr?'\\n'+d.stderr:'')+'\\n';
    out.scrollTop = out.scrollHeight;
    window._hartTermBusy = false;
  }}).catch(function(e){{
    var aborted = e && (e.name==='AbortError' || e.name==='TimeoutError');
    out.textContent += (aborted ? 'Command timed out after 120s.' : ('Error: '+((e&&e.message)||e)))+'\\n';
    out.scrollTop = out.scrollHeight;
    window._hartTermBusy = false;
  }});
}}

// ═══ User Accounts ═══
function loadUserAccountsPanel(el) {{
  fetch(SHELL+'/api/shell/users',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const users = data.users||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">User Accounts</div><div class="ds-stagger">';
    users.forEach(u=>{{
      html += dsStatusRow('person', u.username||u.name, u.is_admin?'Admin':'User',
        u.is_admin?'var(--hart-accent)':'var(--hart-muted)',{{sublabel:'UID '+(u.uid||'')}});
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">User accounts unavailable</div>'; }});
}}

// ═══ Notification Center ═══
function loadNotificationCenterPanel(el) {{
  fetch(SHELL+'/api/shell/notifications',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const notifs = data.notifications||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Notifications</div><div class="ds-stagger">';
    if(notifs.length===0) html += '<div class="ds-body-md ds-text-muted">No notifications</div>';
    else notifs.slice(0,20).forEach(n=>{{
      html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon '+(n.read?'ds-text-muted':'ds-text-accent')+'">'+
        (n.read?'notifications_none':'notifications_active')+'</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+(n.title||n.message||'Notification')+'</div>'+
        '<div class="ds-list-item-secondary">'+(n.time||n.created_at||'')+'</div></div></div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Notifications unavailable</div>'; }});
}}

// ═══ Updates ═══
function loadUpdatesPanel(el) {{
  fetch(BACKEND+'/api/upgrades/status',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">System Updates</div>';
    html += dsStatusRow('system_update', 'Current Version', data.current_version||'unknown', 'var(--hart-active)');
    if(data.new_version) html += dsStatusRow('upgrade', 'Available', data.new_version, 'var(--hart-accent)');
    else html += '<div class="ds-body-md ds-text-active" style="padding:12px 0">System is up to date</div>';
    html += dsStatusRow('schedule', 'Pipeline', data.pipeline_stage||'idle', 'var(--hart-muted)');
    html += '</div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Update status unavailable</div>'; }});
}}

// ═══ Backup & Restore ═══
function loadBackupRestorePanel(el) {{
  fetch(SHELL+'/api/shell/backup/list',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const backups = data.backups||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Backup &amp; Restore</div><div class="ds-stagger">';
    if(backups.length===0) html += '<div class="ds-body-md ds-text-muted">No backups found</div>';
    else backups.forEach(b=>{{
      html += dsStatusRow('backup', b.name||b.path, b.date||b.created||'', 'var(--hart-muted)');
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Backup info unavailable</div>'; }});
}}

// ═══ Devices & Mesh ═══
function loadDevicesPanel(el) {{
  fetch(SHELL+'/api/shell/devices',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const devs = data.devices||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Devices &amp; Mesh</div><div class="ds-stagger">';
    if(devs.length===0) html += '<div class="ds-body-md ds-text-muted">No paired devices</div>';
    else devs.forEach(d=>{{
      html += dsStatusRow('devices_other', d.name||d.device_id||'Device', d.status||'unknown',
        d.status==='paired'?'var(--hart-active)':'var(--hart-muted)',{{sublabel:d.device_id||''}});
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Devices unavailable</div>'; }});
}}

// ═══ Language & Region ═══
function loadI18nPanel(el) {{
  fetch(SHELL+'/api/shell/i18n/locales',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const current = data.current||'en';
    const locales = data.available||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Language &amp; Region</div>';
    html += dsStatusRow('language', 'Current', current, 'var(--hart-accent)');
    html += '<div class="ds-section-label">Available Languages</div><div class="ds-stagger">';
    locales.slice(0,15).forEach(l=>{{
      const active = l.code===current;
      html += '<div class="ds-list-item'+(active?'':' ds-list-item-interactive')+'"'+
        (active?'':' data-code="'+l.code+'" onclick="setLocale(this.dataset.code)"')+'>'+
        '<span class="mi material-icons-round ds-list-item-icon '+(active?'ds-text-active':'ds-text-muted')+'">translate</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+(l.name||l.code)+'</div></div>'+
        (active?'<span class="ds-list-item-trailing ds-text-active"><span class="mi material-icons-round">check</span></span>':'')+
        '</div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Language settings unavailable</div>'; }});
}}
function setLocale(code) {{
  fetch(SHELL+'/api/shell/i18n/set',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{locale:code}})}})
  .then(()=>{{showToast('Language','Set to '+code,'success');loadI18nPanel(document.getElementById('sys-i18n'));}}).catch(()=>{{}});
}}

// ═══ Accessibility ═══
function loadAccessibilityPanel(el) {{
  fetch(SHELL+'/api/shell/accessibility',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Accessibility</div><div class="ds-stagger">';
    const items = [
      ['contrast', 'High Contrast', 'high_contrast', data.high_contrast],
      ['animation', 'Reduce Motion', 'reduced_motion', data.reduced_motion],
      ['record_voice_over', 'Screen Reader', 'screen_reader', data.screen_reader],
      ['back_hand', 'Large Cursor', 'large_cursor', data.large_cursor],
      ['keyboard', 'Sticky Keys', 'sticky_keys', data.sticky_keys],
    ];
    items.forEach(([icon,label,key,val])=>{{
      html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-accent" aria-hidden="true">'+icon+'</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+label+'</div></div>'+
        '<label class="ds-switch"><input type="checkbox" role="switch" aria-label="'+label+'" '+(val?'checked':'')+' data-key="'+key+'" onchange="toggleA11y(this.dataset.key,this.checked)"><span class="ds-switch-slider"></span></label></div>';
    }});
    const _fsv = data.font_scale || 1;
    html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-accent" aria-hidden="true">format_size</span>'+
      '<div class="ds-list-item-content"><div class="ds-list-item-primary">Font Scale</div></div>'+
      '<div class="ds-flex ds-gap-2" style="align-items:center">'+
        '<button class="ds-btn ds-btn-icon ds-btn-tonal" aria-label="Decrease font size" onclick="setFontScale('+_fsv+'-0.1)"><span class="mi material-icons-round" aria-hidden="true">remove</span></button>'+
        '<span style="min-width:46px;text-align:center">'+Math.round(_fsv*100)+'%</span>'+
        '<button class="ds-btn ds-btn-icon ds-btn-tonal" aria-label="Increase font size" onclick="setFontScale('+_fsv+'+0.1)"><span class="mi material-icons-round" aria-hidden="true">add</span></button>'+
      '</div></div>';
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Accessibility unavailable</div>'; }});
}}
function toggleA11y(key,val) {{
  const body = {{}};
  body[key] = val;
  // Reload after a successful PUT so the render re-reads the live a11y state and
  // applies the <html> class (high-contrast / reduced-motion). Same pattern the
  // theme switcher uses.
  fetch(SHELL+'/api/shell/accessibility',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}})
    .then(()=>location.reload()).catch(()=>{{}});
}}
function setFontScale(v) {{
  v = Math.max(0.8, Math.min(2.0, Math.round(v*10)/10));
  fetch(SHELL+'/api/shell/accessibility',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{font_scale:v}})}})
    .then(()=>location.reload()).catch(()=>{{}});
}}

// ═══ Screenshot & Recording ═══
function loadScreenshotPanel(el) {{
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Screenshot &amp; Recording</div>'+
    '<div class="ds-flex ds-gap-3 ds-flex-wrap" style="padding:24px 0">'+
    dsBtn('Take Screenshot',{{variant:'primary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/shell/screenshot',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{type:'full'}})}}).then(r=>r.json()).then(d=>showToast('Screenshot',d.path||'Captured','success')).catch(()=>showToast('Screenshot','Failed','error'))"}})+
    dsBtn('Window Screenshot',{{variant:'secondary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/shell/screenshot',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{type:'window'}})}}).then(r=>r.json()).then(d=>showToast('Screenshot',d.path||'Captured','success')).catch(()=>{{}})"}})+
    dsBtn('Start Recording',{{variant:'secondary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/shell/recording/start',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}}).then(()=>showToast('Recording','Started','info')).catch(()=>{{}})"}})+
    dsBtn('Stop Recording',{{variant:'secondary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/shell/recording/stop',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}}).then(r=>r.json()).then(d=>showToast('Recording','Saved: '+(d.path||''),'success')).catch(()=>{{}})"}})+
    '</div></div>';
}}

// ═══ Firewall ═══
function loadFirewallPanel(el) {{
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Firewall &amp; Firmware</div>'+
    '<div class="ds-stagger">'+
    dsStatusRow('shield', 'Firewall', 'Active (nftables)', 'var(--hart-active)',{{sublabel:'Managed by NixOS declarative config'}})+
    dsStatusRow('security', 'Zones', 'trusted / hive / public', 'var(--hart-muted)')+
    dsStatusRow('verified_user', 'Firmware Updates', 'fwupd enabled', 'var(--hart-active)')+
    '</div></div>';
}}

// ═══ Default Apps ═══
function loadDefaultAppsPanel(el) {{
  fetch(SHELL+'/api/shell/default-apps',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const apps = data.defaults||{{}};
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Default Apps</div><div class="ds-stagger">';
    const cats = [['web-browser','Web Browser','public'],['text-editor','Text Editor','edit_note'],
      ['file-manager','File Manager','folder'],['terminal','Terminal','terminal'],
      ['image-viewer','Image Viewer','photo'],['video-player','Video Player','play_circle'],
      ['music-player','Music Player','music_note'],['email-client','Email','email'],
      ['pdf-viewer','PDF Viewer','picture_as_pdf']];
    cats.forEach(([key,label,icon])=>{{
      html += dsStatusRow(icon, label, apps[key]||'Not set', apps[key]?'var(--hart-accent)':'var(--hart-muted)');
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Default apps unavailable</div>'; }});
}}

// ═══ Font Manager ═══
function loadFontManagerPanel(el) {{
  fetch(SHELL+'/api/shell/fonts',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const fonts = data.fonts||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-header"><span class="ds-panel-title">Fonts</span>'+
      '<span class="ds-chip"><span class="ds-chip-dot" style="background:var(--hart-accent)"></span>'+fonts.length+' installed</span></div>';
    html += '<div class="ds-stagger">';
    fonts.slice(0,20).forEach(f=>{{
      html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-accent">font_download</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary" data-font="'+f.family+'">'+f.family+'</div>'+
        '<div class="ds-list-item-secondary">'+(f.style||f.styles||'')+'</div></div></div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Fonts unavailable</div>'; }});
}}

// ── Shell action helpers (avoid quote-escaping in inline onclick) ──
function _shellPost(url, body, onOk) {{
  fetch(SHELL+url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}})
    .then(()=>{{ if(onOk) onOk(); }}).catch(()=>{{}});
}}
function shellSetSoundTheme(el) {{
  _shellPost('/api/shell/sounds/set-theme', {{theme:el.dataset.theme}},
    ()=>loadSoundManagerPanel(document.getElementById('sys-sound_manager')));
}}
function shellCopyClipboard(el) {{
  _shellPost('/api/shell/clipboard/copy', {{text:el.dataset.clip}},
    ()=>showToast('Clipboard','Copied','info'));
}}
function shellSetWallpaper(el) {{
  _shellPost('/api/shell/wallpaper/set', {{path:el.dataset.path}},
    ()=>showToast('Wallpaper','Set','success'));
}}
function shellDeleteNote(el) {{
  _shellPost('/api/shell/notes/delete', {{id:el.dataset.id}},
    ()=>loadNotesAppPanel(document.getElementById('sys-notes_app')));
}}
function shellRestoreTrash(el) {{
  _shellPost('/api/shell/trash/restore', {{path:el.dataset.path}},
    ()=>loadTrashBinPanel(document.getElementById('sys-trash_bin')));
}}

// ═══ Sound Manager ═══
function loadSoundManagerPanel(el) {{
  fetch(SHELL+'/api/shell/sounds/themes',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const themes = data.themes||[];
    const current = data.current||'freedesktop';
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Sound Theme</div>';
    html += dsStatusRow('music_note', 'Current Theme', current, 'var(--hart-accent)');
    html += '<div class="ds-stagger">';
    themes.forEach(t=>{{
      html += '<div class="ds-list-item'+(t===current?'':' ds-list-item-interactive')+'"'+
        (t===current?'':' data-theme="'+t+'" onclick="shellSetSoundTheme(this)"')+'>'+
        '<span class="mi material-icons-round ds-list-item-icon '+(t===current?'ds-text-active':'ds-text-muted')+'">volume_up</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+t+'</div></div>'+
        (t===current?'<span class="ds-list-item-trailing ds-text-active"><span class="mi material-icons-round">check</span></span>':'')+
        '</div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Sound settings unavailable</div>'; }});
}}

// ═══ Clipboard ═══
function loadClipboardPanel(el) {{
  fetch(SHELL+'/api/shell/clipboard/history',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const items = data.history||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-header"><span class="ds-panel-title">Clipboard</span>'+
      dsBtn('Clear',{{variant:'secondary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/shell/clipboard/clear',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}}).then(()=>loadClipboardPanel(document.getElementById('sys-clipboard_manager')))"}})+
      '</div><div class="ds-stagger">';
    if(items.length===0) html += '<div class="ds-body-md ds-text-muted">Clipboard empty</div>';
    else items.slice(0,15).forEach((c,i)=>{{
      const preview = (c.text||c.content||'').substring(0,80);
      html += '<div class="ds-list-item ds-list-item-interactive" data-clip="'+preview+'" onclick="shellCopyClipboard(this)">'+
        '<span class="mi material-icons-round ds-list-item-icon ds-text-muted">content_paste</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary ds-truncate">'+preview+'</div>'+
        '<div class="ds-list-item-secondary">'+(c.time||'')+(c.pinned?' &middot; Pinned':'')+'</div></div></div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Clipboard unavailable</div>'; }});
}}

// ═══ Date & Time ═══
function loadDateTimePanel(el) {{
  fetch(SHELL+'/api/shell/datetime',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Date &amp; Time</div>';
    html += '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-2" style="padding:16px 0">'+
      '<div class="ds-display-sm ds-text-accent">'+(data.time||'')+'</div>'+
      '<div class="ds-title-sm ds-text-muted">'+(data.date||'')+'</div></div>';
    html += '<div class="ds-stagger">';
    html += dsStatusRow('schedule', 'Timezone', data.timezone||'UTC', 'var(--hart-accent)');
    html += dsStatusRow('sync', 'NTP Sync', data.ntp_enabled?'Enabled':'Disabled', data.ntp_enabled?'var(--hart-active)':'var(--hart-muted)');
    html += dsStatusRow('today', 'Format', data.format||'24h', 'var(--hart-muted)');
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Date/time unavailable</div>'; }});
}}

// ═══ Wallpaper ═══
function loadWallpaperPanel(el) {{
  // Personalize = themes gallery + wallpaper chooser (Phase B). The heavy HTML
  // lives in hartPersonalize.js (window.hartRenderPersonalize) so this stays a
  // brace-escape-free delegate; it reuses applyPreset + the wallpaper routes.
  if(window.hartRenderPersonalize) {{ window.hartRenderPersonalize(el); }}
  else {{ el.innerHTML = '<div class="ds-body-md ds-text-muted">Personalize loading&hellip;</div>'; setTimeout(function(){{loadWallpaperPanel(el)}}, 400); }}
}}

// ═══ Keyboard & Input Methods ═══
function loadInputMethodsPanel(el) {{
  fetch(SHELL+'/api/shell/input-methods',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const layout = data.layout||'us';
    const variant = data.variant||'';
    const methods = data.input_methods||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Keyboard &amp; Input</div><div class="ds-stagger">';
    html += dsStatusRow('keyboard', 'Layout', layout+(variant?' ('+variant+')':''), 'var(--hart-accent)');
    if(methods.length>0) {{
      html += '<div class="ds-section-label">Input Methods</div>';
      methods.forEach(m=>{{
        html += dsStatusRow('translate', m.name||m.id, m.active?'Active':'Available',
          m.active?'var(--hart-active)':'var(--hart-muted)');
      }});
    }}
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Input settings unavailable</div>'; }});
}}

// ═══ Night Light ═══
function loadNightLightPanel(el) {{
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Night Light</div>'+
    '<div class="ds-stagger">'+
    dsStatusRow('nightlight', 'Status', 'Managed by gammastep', 'var(--hart-accent)',{{sublabel:'Reduces blue light in the evening'}})+
    dsStatusRow('schedule', 'Schedule', 'Sunset to Sunrise', 'var(--hart-muted)')+
    dsStatusRow('thermostat', 'Temperature', '3500K', 'var(--hart-accent)')+
    '</div><div class="ds-body-sm ds-text-muted" style="margin-top:12px">Configured via NixOS module hart-nightlight.nix</div></div>';
}}

// ═══ Workspaces ═══
function loadWorkspacesPanel(el) {{
  // Two distinct layers, deliberately: the COMPOSITOR-workspace route
  // /api/shell/workspaces (shell_desktop_apis.py, sway-backed) returns a single
  // fallback workspace under the cage kiosk, so it is NOT the source here. The
  // shell's own floating panels are grouped into virtual desktops client-side by
  // hartWorkspaces.js — the meaningful "desktops" on a one-window kiosk. This
  // panel reflects THAT state; squares live-sync via hartWorkspaces.apply().
  var info = (window.hartWorkspaceInfo && window.hartWorkspaceInfo()) || {{count:4,current:1}};
  var html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Workspaces</div>'+
    '<div class="ds-body-sm ds-text-muted" style="margin-bottom:8px">Virtual desktops &mdash; switch with Ctrl+Alt+Arrows, Ctrl+Alt+number, the bottom switcher, or click below.</div>'+
    '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px;padding:8px 0">';
  for(var i=1;i<=info.count;i++) {{
    html += '<div class="hart-ws-square'+(i===info.current?' active':'')+'" data-ws-square="'+i+'" onclick="window.hartSwitchWorkspace&&hartSwitchWorkspace('+i+')">'+i+'</div>';
  }}
  html += '</div><div class="ds-body-sm ds-text-muted" style="margin-top:8px">Open windows stay on the desktop where you launched them.</div></div>';
  el.innerHTML = html;
}}

// ═══ Calculator ═══
function loadCalculatorPanel(el) {{
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Calculator</div>'+
    '<input id="calc-display" type="text" readonly value="0" style="width:100%;background:#0d0d0d;color:var(--hart-text);border:none;border-radius:8px;padding:16px;font-size:28px;text-align:right;font-family:monospace;margin-bottom:8px">'+
    '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px" id="calc-grid"></div></div>';
  const grid = document.getElementById('calc-grid');
  const btns = ['C','(',')','/',7,8,9,'*',4,5,6,'-',1,2,3,'+',0,'.','%','='];
  btns.forEach(b=>{{
    const isOp = typeof b==='string'&&b!=='C';
    const el2 = document.createElement('button');
    el2.className = 'ds-btn ds-btn-sm';
    el2.style.cssText = 'padding:14px;font-size:18px;'+(isOp?'color:var(--hart-accent)':'');
    el2.textContent = b;
    el2.onclick = ()=>calcPress(String(b));
    grid.appendChild(el2);
  }});
}}
function calcPress(b) {{
  const d = document.getElementById('calc-display');
  if(!d) return;
  if(b==='C') {{ d.value='0'; return; }}
  if(b==='=') {{ try {{ d.value=String(Function('"use strict";return('+d.value+')')());}} catch {{ d.value='Error'; }} return; }}
  if(d.value==='0'&&b!=='.') d.value=b; else d.value+=b;
}}

// ═══ Image Viewer ═══
function loadImageViewerPanel(el) {{
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Image Viewer</div>'+
    '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-3" style="padding:40px 0">'+
    '<span class="mi material-icons-round ds-text-muted" style="font-size:48px">photo</span>'+
    '<div class="ds-body-md ds-text-muted">Open an image from the File Manager</div></div></div>';
}}

// ═══ Notes ═══
function loadNotesAppPanel(el) {{
  fetch(SHELL+'/api/shell/notes',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const notes = data.notes||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-header"><span class="ds-panel-title">Notes</span>'+
      dsBtn('New',{{variant:'primary',cls:'ds-btn-sm',onclick:"dsPrompt('New Note','',{{placeholder:'Write your note...',okLabel:'Save'}}).then(c=>{{if(!c)return;fetch(SHELL+'/api/shell/notes',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{content:c}})}}).then(()=>loadNotesAppPanel(document.getElementById('sys-notes_app')))}})"}})+'</div>';
    html += '<div class="ds-stagger">';
    if(notes.length===0) html += '<div class="ds-body-md ds-text-muted">No notes yet</div>';
    else notes.forEach(n=>{{
      html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-accent">sticky_note_2</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary ds-truncate">'+(n.content||'').substring(0,80)+'</div>'+
        '<div class="ds-list-item-secondary">'+(n.created||n.date||'')+'</div></div>'+
        '<span class="ds-list-item-trailing" style="cursor:pointer" data-id="'+n.id+'" onclick="shellDeleteNote(this)">'+
        '<span class="mi material-icons-round ds-text-muted" style="font-size:18px">delete_outline</span></span></div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Notes unavailable</div>'; }});
}}

// ═══ App Store ═══
function loadAppStorePanel(el) {{
  // Marketplace (Phase C): curated Flathub catalog + search + AI-recommend. The
  // heavy HTML lives in hartMarketplace.js (window.hartRenderMarketplace) so this
  // stays a brace-safe delegate; it reuses /api/apps/search + /api/apps/install.
  if(window.hartRenderMarketplace) {{ window.hartRenderMarketplace(el); }}
  else {{ el.innerHTML = '<div class="ds-body-md ds-text-muted">Marketplace loading&hellip;</div>'; setTimeout(function(){{loadAppStorePanel(el)}}, 400); }}
}}
function loadCreditsPanel(el) {{
  // About > Credits (#143): the third-party art licence ledger. Heavy DOM lives
  // in hartCredits.js (window.hartRenderCredits) so this stays a brace-safe
  // delegate; it reads /api/shell/credits (offline, bundled doc).
  if(window.hartRenderCredits) {{ window.hartRenderCredits(el); }}
  else {{ el.innerHTML = '<div class="ds-body-md ds-text-muted">Credits loading&hellip;</div>'; setTimeout(function(){{loadCreditsPanel(el)}}, 400); }}
}}
function appStoreSearch() {{
  const q = document.getElementById('appstore-search');
  const r = document.getElementById('appstore-results');
  if(!q||!r||!q.value.trim()) return;
  r.innerHTML = dsSkeleton('panel',2);
  fetch(SHELL+'/api/apps/search?q='+encodeURIComponent(q.value),{{signal:_sig(15000)}}).then(r2=>r2.json()).then(data=>{{
    const pkgs = data.results||[];
    if(pkgs.length===0) {{ r.innerHTML='<div class="ds-body-md ds-text-muted">No packages found</div>'; return; }}
    r.innerHTML = pkgs.slice(0,15).map(p=>
      '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-accent">inventory_2</span>'+
      '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+p.name+'</div>'+
      '<div class="ds-list-item-secondary">'+(p.platform||p.source||'')+' &middot; '+(p.version||'')+'</div></div>'+
      dsBtn('Install',{{variant:'secondary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'/api/apps/install',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{package:'"+p.name+"',platform:'"+(p.platform||'nix')+"'}})}}).then(()=>showToast('App Store','Installing "+p.name+"','info'))"}})+'</div>'
    ).join('');
  }}).catch(()=>{{ r.innerHTML='<div class="ds-body-md ds-text-muted">Search failed</div>'; }});
}}

// ═══ App Permissions ═══
function loadAppPermissionsPanel(el) {{
  fetch(SHELL+'/api/apps/installed',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const apps = data.apps||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">App Permissions</div><div class="ds-stagger">';
    if(apps.length===0) html += '<div class="ds-body-md ds-text-muted">No apps installed</div>';
    else apps.slice(0,20).forEach(a=>{{
      html += dsStatusRow('admin_panel_settings', a.name||a.id, a.platform||'system',
        'var(--hart-muted)',{{sublabel:(a.permissions||[]).join(', ')||'No special permissions'}});
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">App permissions unavailable</div>'; }});
}}

// ═══ Battery Monitor ═══
function loadBatteryMonitorPanel(el) {{
  fetch(SHELL+'/api/shell/battery',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const pct = data.percent||0;
    const charging = data.charging||false;
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Battery</div>'+
      '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-2" style="padding:24px 0">'+
      '<span class="mi material-icons-round ds-text-accent" style="font-size:56px">'+(charging?'battery_charging_full':pct>20?'battery_full':'battery_alert')+'</span>'+
      '<div class="ds-display-sm ds-text-accent">'+pct+'%</div>'+
      '<div class="ds-label-sm ds-text-muted">'+(charging?'Charging':'On Battery')+(data.time_remaining?' &middot; '+data.time_remaining+' remaining':'')+'</div></div>';
    html += dsMetricBar('Level', pct, '%');
    if(data.power_profile) html += dsStatusRow('power', 'Profile', data.power_profile, 'var(--hart-muted)');
    html += '</div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Battery info unavailable</div>'; }});
}}

// ═══ WiFi Manager ═══
function loadWiFiManagerPanel(el) {{
  Promise.all([
    fetch(SHELL+'/api/shell/wifi/status',{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}})),
    fetch(SHELL+'/api/shell/wifi/scan',{{signal:_sig(8000)}}).then(r=>r.json()).catch(()=>({{}}))
  ]).then(([status,scan])=>{{
    const connected = status.connected||{{}};
    const networks = scan.networks||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">WiFi</div>';
    if(connected.ssid) {{
      html += dsCard('<div class="ds-flex ds-flex-center ds-flex-col ds-gap-2">'+
        '<span class="mi material-icons-round ds-text-active" style="font-size:28px">wifi</span>'+
        '<div class="ds-title-sm ds-text-active">'+connected.ssid+'</div>'+
        '<div class="ds-label-sm ds-text-muted">'+(connected.ip||'')+'</div>'+
        dsBtn('Disconnect',{{variant:'secondary',cls:'ds-btn-sm',onclick:"wifiDisconnect()"}})+'</div>',{{elevated:true}});
    }}
    if(networks.length>0) {{
      html += '<div class="ds-section-label">Available Networks</div><div class="ds-stagger">';
      networks.filter(n=>!n.active).slice(0,8).forEach(n=>{{
        html += '<div class="ds-list-item ds-list-item-interactive" data-ssid="'+n.ssid+'" onclick="wifiConnect(this.dataset.ssid)">'+
          '<span class="mi material-icons-round ds-list-item-icon ds-text-accent">wifi</span>'+
          '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+n.ssid+'</div>'+
          '<div class="ds-list-item-secondary">'+n.security+' &middot; '+n.signal+'%</div></div></div>';
      }});
      html += '</div>';
    }}
    html += '</div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">WiFi unavailable</div>'; }});
}}

// ═══ VPN Manager ═══
function loadVPNManagerPanel(el) {{
  fetch(SHELL+'/api/shell/vpn/list',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const vpns = data.connections||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-header"><span class="ds-panel-title">VPN</span>'+
      dsBtn('Import',{{variant:'secondary',cls:'ds-btn-sm',onclick:"dsPrompt('Import VPN','Enter WireGuard config path',{{placeholder:'/path/to/wg0.conf',okLabel:'Import'}}).then(p=>{{if(!p)return;fetch(SHELL+'/api/shell/vpn/import',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{config_path:p,type:'wireguard'}})}}).then(r=>r.json()).then(d=>{{showToast('VPN',d.message||'Imported','success');loadVPNManagerPanel(document.getElementById('sys-vpn_manager'))}})}})"}})+'</div><div class="ds-stagger">';
    if(vpns.length===0) html += '<div class="ds-body-md ds-text-muted">No VPN connections</div>';
    else vpns.forEach(v=>{{
      html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon '+(v.active?'ds-text-active':'ds-text-muted')+'">vpn_key</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+v.name+'</div>'+
        '<div class="ds-list-item-secondary">'+(v.type||'')+'</div></div>'+
        dsBtn(v.active?'Disconnect':'Connect',{{variant:'secondary',cls:'ds-btn-sm',
          onclick:"fetch(SHELL+'/api/shell/vpn/"+(v.active?'disconnect':'connect')+"',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name:'"+v.name+"'}})}}).then(()=>loadVPNManagerPanel(document.getElementById('sys-vpn_manager')))"}})+
        '</div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">VPN unavailable</div>'; }});
}}

// ═══ Trash Bin ═══
function loadTrashBinPanel(el) {{
  fetch(SHELL+'/api/shell/trash',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const items = data.items||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-header"><span class="ds-panel-title">Trash</span>'+
      (items.length>0?dsBtn('Empty Trash',{{variant:'secondary',cls:'ds-btn-sm',onclick:"dsConfirm('Empty Trash','Permanently delete all items?',{{okLabel:'Empty',danger:true}}).then(ok=>{{if(!ok)return;fetch(SHELL+'/api/shell/trash/empty',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}}).then(()=>loadTrashBinPanel(document.getElementById('sys-trash_bin')))}})"}}):'')+
      '</div><div class="ds-stagger">';
    if(items.length===0) html += '<div class="ds-body-md ds-text-muted">Trash is empty</div>';
    else items.slice(0,20).forEach(t=>{{
      html += '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-muted">delete</span>'+
        '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+t.name+'</div>'+
        '<div class="ds-list-item-secondary">'+(t.deleted_at||'')+'</div></div>'+
        '<span class="ds-list-item-trailing" style="cursor:pointer" data-path="'+t.original_path+'" onclick="shellRestoreTrash(this)">'+
        '<span class="mi material-icons-round ds-text-accent" style="font-size:18px">restore</span></span></div>';
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Trash unavailable</div>'; }});
}}

// ═══ Webcam Viewer ═══
function loadWebcamViewerPanel(el) {{
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Camera</div>'+
    '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-3" style="padding:40px 0">'+
    '<span class="mi material-icons-round ds-text-muted" style="font-size:48px">videocam</span>'+
    '<div class="ds-body-md ds-text-muted">Camera preview requires native GNOME Cheese or direct device access</div>'+
    dsBtn('Open Camera App',{{variant:'secondary',cls:'ds-btn-sm',onclick:"showToast('Camera','Opening cheese...','info')"}})+'</div></div>';
}}

// ═══ Scanner ═══
function loadScannerPanel(el) {{
  fetch(SHELL+'/api/shell/scanner/list',{{signal:_sig(5000)}}).then(r=>r.json()).then(data=>{{
    const scanners = data.scanners||[];
    let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Scanner</div><div class="ds-stagger">';
    if(scanners.length===0) html += '<div class="ds-body-md ds-text-muted">No scanners detected</div>';
    else scanners.forEach(s=>{{
      html += dsStatusRow('scanner', s.name||s.device, s.status||'Ready', 'var(--hart-active)');
    }});
    html += '</div></div>';
    el.innerHTML = html;
  }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Scanner unavailable</div>'; }});
}}

// ═══ Weather ═══
function loadWeatherPanel(el) {{
  el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Weather</div>'+
    '<div class="ds-flex ds-flex-center ds-flex-col ds-gap-3" style="padding:40px 0">'+
    '<span class="mi material-icons-round ds-text-accent" style="font-size:56px">cloud</span>'+
    '<div class="ds-body-md ds-text-muted">Weather widget uses GNOME Weather or wttr.in</div>'+
    '<div class="ds-label-sm ds-text-muted">Connect location services for automatic weather</div></div></div>';
}}

function loadEventLog(el) {{
  fetch(SHELL+'/api/shell/events',{{signal:_sig(3000)}})
    .then(r=>r.json()).then(data=>{{
      const events = data.events||[];
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Events</div>'+
        (events.length===0?'<div class="ds-body-md ds-text-muted">No events recorded</div>':
        '<div class="ds-stagger">'+events.slice(0,20).map(e=>
          '<div class="ds-list-item"><span class="mi material-icons-round ds-list-item-icon ds-text-muted">schedule</span>'+
          '<div class="ds-list-item-content"><div class="ds-list-item-primary">'+e.message+'</div>'+
          '<div class="ds-list-item-secondary">'+e.time+'</div></div></div>'
        ).join('')+'</div>')+
        '</div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">No events</div>'; }});
}}

function loadDriversPanel(el) {{
  fetch(SHELL+'/api/shell/drivers',{{signal:_sig(5000)}})
    .then(r=>r.json()).then(data=>{{
      const devs = data.devices||[];
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Drivers &amp; Devices</div>'+
        (devs.length===0?'<div class="ds-body-md ds-text-muted">No devices detected</div>':
        '<div class="ds-stagger">'+devs.slice(0,20).map(d=>
          dsStatusRow(d.type==='usb'?'usb':'memory', d.info, d.type.toUpperCase(),
            d.type==='usb'?'var(--hart-active)':'var(--hart-accent)')
        ).join('')+'</div>')+
        '</div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Drivers panel unavailable</div>'; }});
}}

function setVolume(sinkId, vol) {{
  fetch(SHELL+'/api/shell/audio/volume', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{sink_id:sinkId, volume:vol}})
  }}).catch(()=>{{}});
}}
function toggleMute(sinkId, muted) {{
  fetch(SHELL+'/api/shell/audio/mute', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{sink_id:sinkId, muted:muted}})
  }}).then(()=>loadAudioPanel(document.getElementById('sys-audio'))).catch(()=>{{}});
}}
function setDefaultSink(sinkId) {{
  fetch(SHELL+'/api/shell/audio/default', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{sink_id:sinkId}})
  }}).then(()=>loadAudioPanel(document.getElementById('sys-audio'))).catch(()=>{{}});
}}
function setSourceVolume(srcId, vol) {{
  fetch(SHELL+'/api/shell/audio/source/volume', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{source_id:srcId, volume:vol}})
  }}).catch(()=>{{}});
}}

function loadAudioPanel(el) {{
  fetch(SHELL+'/api/shell/audio',{{signal:_sig(5000)}})
    .then(r=>r.json()).then(data=>{{
      const sinks = data.sinks||[];
      const sources = data.sources||[];
      let html = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Audio</div>';
      html += '<div class="ds-section-label">Output</div>';
      if(sinks.length===0) html += '<div class="ds-body-sm ds-text-muted">No audio outputs</div>';
      else html += '<div class="ds-stagger">'+sinks.map(s=>
        '<div class="ds-card" style="margin-bottom:var(--ds-space-2)">'+
        '<div class="ds-flex ds-gap-3" style="align-items:center;margin-bottom:var(--ds-space-3)">'+
        '<span class="mi material-icons-round" style="font-size:24px;color:'+(s.mute?'var(--hart-caution)':'var(--hart-active)')+'">'+
        (s.mute?'volume_off':'volume_up')+'</span>'+
        '<div class="ds-flex-1"><div class="ds-title-sm">'+s.name+'</div>'+
        (s.default?'<span class="ds-chip ds-chip-success" style="margin-top:2px"><span class="ds-chip-dot"></span>Default</span>':'')+
        '</div>'+
        dsBtn(s.mute?'Unmute':'Mute', {{variant:'secondary', cls:'ds-btn-sm', onclick:"toggleMute(\\'"+s.id+"\\',"+(!s.mute)+")"}})+
        (!s.default?dsBtn('Set Default', {{variant:'text', cls:'ds-btn-sm', onclick:"setDefaultSink(\\'"+s.id+"\\')"}}):'')+
        '</div>'+
        dsSlider({{id:'vol-'+s.id.replace(/[^a-z0-9]/gi,''), min:0, max:150, value:s.volume, label:'Volume', unit:'%',
          oninput:"setVolume(\\'"+s.id+"\\',this.value)"}})+
        '</div>'
      ).join('')+'</div>';
      html += '<div class="ds-section-label" style="margin-top:var(--ds-space-3)">Input</div>';
      if(sources.length===0) html += '<div class="ds-body-sm ds-text-muted">No audio inputs</div>';
      else html += '<div class="ds-stagger">'+sources.map(s=>
        '<div class="ds-card" style="margin-bottom:var(--ds-space-2)">'+
        '<div class="ds-flex ds-gap-3" style="align-items:center;margin-bottom:var(--ds-space-3)">'+
        '<span class="mi material-icons-round ds-text-active" style="font-size:24px">mic</span>'+
        '<div class="ds-title-sm ds-flex-1">'+s.name+'</div></div>'+
        dsSlider({{id:'src-'+s.id.replace(/[^a-z0-9]/gi,''), min:0, max:150, value:s.volume, label:'Volume', unit:'%',
          oninput:"setSourceVolume(\\'"+s.id+"\\',this.value)"}})+
        '</div>'
      ).join('')+'</div>';
      html += '</div>';
      el.innerHTML = html;
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Audio panel unavailable</div>'; }});
}}

function loadBluetoothPanel(el) {{
  fetch(SHELL+'/api/shell/bluetooth',{{signal:_sig(5000)}})
    .then(r=>r.json()).then(data=>{{
      const devs = data.devices||[];
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Bluetooth</div>'+
        (devs.length===0?'<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:80px"><span class="mi material-icons-round" style="margin-right:8px;font-size:32px;opacity:0.3">bluetooth_disabled</span>No Bluetooth devices found</div>':
        '<div class="ds-stagger">'+devs.map(d=>dsStatusRow('bluetooth',d.name,d.mac,'var(--hart-accent)')).join('')+'</div>')+
        '</div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Bluetooth unavailable</div>'; }});
}}

function loadPowerPanel(el) {{
  fetch(SHELL+'/api/shell/power',{{signal:_sig(5000)}})
    .then(r=>r.json()).then(data=>{{
      const pct = data.percent||100;
      const state = data.state||'unknown';
      const remaining = data.time_remaining||'';
      const icon = pct>80?'battery_full':pct>50?'battery_5_bar':pct>20?'battery_3_bar':'battery_1_bar';
      const color = pct>20?'var(--hart-active)':pct>10?'var(--hart-caution)':'var(--hart-error)';
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Power</div>'+
        '<div class="ds-card ds-card-elevated">'+
        '<div class="ds-metric">'+
        '<span class="mi material-icons-round ds-metric-icon" style="color:'+color+'">'+icon+'</span>'+
        '<div class="ds-metric-value" style="color:'+color+'">'+pct+'%</div>'+
        '<div class="ds-metric-label">'+state+(remaining?' &middot; '+remaining:'')+'</div></div></div>'+
        dsMetricBar('Battery', pct, '%')+
        '</div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Power info unavailable</div>'; }});
}}

function setResolution(output, res, rate) {{
  fetch(SHELL+'/api/shell/display/resolution', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{output:output, resolution:res, rate:rate}})
  }}).then(r=>r.json()).then(d=>{{
    if(d.success) {{ showToast('Display', 'Resolution updated', 'success'); loadDisplayPanel(document.getElementById('sys-display')); }}
    else dsAlert('Resolution Change Failed', d.error||'Unknown error', 'error');
  }}).catch(e=>dsAlert('Error', e.message, 'error'));
}}
function setBrightness(output, val) {{
  fetch(SHELL+'/api/shell/display/brightness', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{output:output, brightness:val}})
  }}).catch(()=>{{}});
}}

function loadDisplayPanel(el) {{
  fetch(SHELL+'/api/shell/display',{{signal:_sig(5000)}})
    .then(r=>r.json()).then(data=>{{
      const displays = data.displays||[];
      if(displays.length===0) {{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px;font-size:32px;opacity:0.3">desktop_access_disabled</span>No displays detected</div>'; return; }}
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Displays</div>'+
        '<div class="ds-stagger">'+displays.map(d=>{{
          const modes = d.modes||[];
          let html = '<div class="ds-card" style="margin-bottom:var(--ds-space-2)">';
          html += '<div class="ds-flex ds-gap-3" style="align-items:center;margin-bottom:var(--ds-space-4)">'+
            '<span class="mi material-icons-round ds-text-accent" style="font-size:28px">desktop_windows</span>'+
            '<div class="ds-flex-1"><div class="ds-title-sm">'+d.name+'</div>'+
            '<span class="ds-label-sm ds-text-active">'+d.resolution+'</span></div></div>';
          if(modes.length>0) {{
            const options = modes.map(m=>{{
              const r = m.rates&&m.rates[0]?m.rates[0]:'';
              return {{value:m.resolution+'@'+r, label:m.resolution+(r?' @ '+r+'Hz':'')+(m.active?' (current)':''), selected:m.active}};
            }});
            html += dsSelect({{label:'Resolution', options:options,
              onchange:"const p=this.value.split(\\'@\\');setResolution(\\'"+d.name+"\\',p[0],p[1])"}});
          }}
          html += '<div style="margin-top:var(--ds-space-4)">'+
            dsSlider({{id:'bright-'+d.name.replace(/[^a-z0-9]/gi,''), min:10, max:100, value:100, label:'Brightness', unit:'%',
              oninput:"setBrightness(\\'"+d.name+"\\',this.value/100)"}})+
            '</div></div>';
          return html;
        }}).join('')+'</div></div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Display info unavailable</div>'; }});
}}

// ═══ Remote Desktop Panel ═══
function rdStartHost() {{
  showToast('Remote Desktop', 'Starting host session...', 'info');
  fetch(BACKEND+'/api/remote-desktop/host',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{engine:'auto'}})}}).then(r=>r.json()).then(d=>{{
    dsAlert('Host Started', 'Device ID: <strong>'+d.formatted_id+'</strong><br>Password: <strong>'+d.password+'</strong><br><br><span class="ds-label-sm ds-text-muted">Share these with the person connecting</span>', 'success');
  }}).catch(e=>dsAlert('Host Failed', e.message, 'error'));
}}
function rdConnect() {{
  dsPrompt('Connect to Device', 'Enter the remote device ID', {{placeholder:'XXX-XXX-XXX', okLabel:'Next'}}).then(function(id){{
    if(!id) return;
    dsPrompt('Enter Password', 'Password for device <strong>'+id+'</strong>', {{type:'password', placeholder:'Password', okLabel:'Connect'}}).then(function(pw){{
      if(!pw) return;
      showToast('Remote Desktop', 'Connecting to '+id+'...', 'info');
      fetch(BACKEND+'/api/remote-desktop/connect',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{device_id:id,password:pw}})}}).then(r=>r.json()).then(d=>{{
        if(d.error) dsAlert('Connection Failed', d.error, 'error');
        else showToast('Remote Desktop', d.message||'Connected', 'success');
      }}).catch(e=>dsAlert('Connection Failed', e.message, 'error'));
    }});
  }});
}}

function loadRemoteDesktopPanel(el, apis) {{
  Promise.all(apis.map(u=>fetch(BACKEND+u,{{signal:_sig(5000)}}).then(r=>r.json()).catch(()=>({{}}))))
    .then(([status,engines,sessions])=>{{
      const did = status.formatted_id || 'Unknown';
      const deviceId = status.device_id || '';
      const engineList = status.engines || engines.engines || {{}};
      const sess = (sessions.sessions || status.active_sessions || []);
      const recs = engines.install_recommendations || status.install_recommendations || [];

      let html = '<div class="ds-panel-grid ds-fade-in">';
      html += '<div class="ds-panel-header"><span class="ds-panel-title">Remote Desktop</span>'+
        '<span class="mi material-icons-round ds-text-active" style="font-size:24px">connected_tv</span></div>';

      // Device ID card
      html += '<div class="ds-card ds-card-elevated ds-card-interactive" data-did="'+deviceId+'" onclick="navigator.clipboard.writeText(this.dataset.did).then(()=>{{var h=this.querySelector(&quot;.copy-hint&quot;);h.textContent=&quot;Copied!&quot;;setTimeout(()=>h.textContent=&quot;Click to copy&quot;,2000)}})" title="Click to copy">';
      html += '<div class="ds-metric"><div class="ds-label-sm ds-text-muted">Your Device ID</div>';
      html += '<div class="ds-headline-md ds-text-heading" style="letter-spacing:3px;margin:var(--ds-space-2) 0">'+did+'</div>';
      html += '<div class="copy-hint ds-label-sm ds-text-muted">Click to copy</div></div></div>';

      // Engines
      html += '<div class="ds-section-label">Engines</div><div class="ds-stagger">';
      for(const [name,info] of Object.entries(engineList)) {{
        const avail = info.available;
        html += dsStatusRow(avail?'check_circle':'cancel',
          name.charAt(0).toUpperCase()+name.slice(1),
          avail?'Available':'Not installed',
          avail?'var(--hart-active)':'var(--hart-muted)');
      }}
      html += '</div>';

      // Sessions
      if(sess.length > 0) {{
        html += '<div class="ds-section-label">Active Sessions ('+sess.length+')</div><div class="ds-stagger">';
        for(const s of sess) {{
          html += dsStatusRow('cast_connected', s.session_id.substring(0,8)+' &mdash; '+s.mode, s.state, 'var(--hart-active)');
        }}
        html += '</div>';
      }}

      // Recommendations
      if(recs.length > 0) {{
        html += '<div class="ds-section-label">Recommended</div><div class="ds-stagger">';
        for(const r of recs) {{
          html += dsStatusRow('recommend', r.engine, r.reason, 'var(--hart-accent)');
        }}
        html += '</div>';
      }}

      // Action buttons
      html += '<div class="ds-flex ds-gap-3" style="margin-top:var(--ds-space-2)">';
      html += dsBtn('Host', {{variant:'primary', icon:'screen_share', onclick:'rdStartHost()'}});
      html += dsBtn('Connect', {{variant:'secondary', icon:'cast', onclick:'rdConnect()'}});
      html += '</div>';

      html += '</div>';
      el.innerHTML = html;
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:100px"><span class="mi material-icons-round" style="margin-right:8px">error_outline</span>Remote desktop unavailable</div>'; }});
}}

// ═══ Agent Pill ═══
// focusAgent() (Super+A) focuses the pill input; the pill's onkeydown opens the
// assistant chat and copies the text into #ac-input, where acSend() is the SOLE
// intent dispatcher (theme/open fast-paths + the default /api/agent/ask compose).
// The old askAgent() was a DEAD parallel copy of that same M1 block - no handler
// invoked it - so it was removed (acSend is the one live path).
function focusAgent() {{
  document.getElementById('agent-input').focus();
  document.getElementById('agent-pill').classList.add('expanded');
}}

// ═══ Floating Assistant Chat ═══
const AC_CAPS = [
  {{id:'chat',name:'Chat',icon:'chat'}},
  {{id:'recipe',name:'Recipes',icon:'receipt_long'}},
  {{id:'agents',name:'Agents',icon:'smart_toy'}},
  {{id:'vision',name:'Vision',icon:'visibility'}},
  {{id:'voice',name:'Voice',icon:'record_voice_over'}},
  {{id:'expert',name:'Experts',icon:'psychology'}},
  {{id:'openclaw',name:'OpenClaw',icon:'extension'}},
  {{id:'code',name:'Code',icon:'code'}},
  {{id:'remote',name:'Remote',icon:'desktop_windows'}},
  {{id:'channels',name:'Channels',icon:'forum'}},
];
let acMessages = [];
let acActiveCap = 'chat';
let acDragging = false;
let acInit = false;
let acDragOfs = {{x:0,y:0}};

function initAssistantChat() {{
  // Render capability pills
  const capsEl = document.getElementById('ac-caps');
  if(!capsEl) return;
  capsEl.innerHTML = AC_CAPS.map(c=>
    '<div class="ac-cap'+(c.id===acActiveCap?' active':'')+'" data-cap-id="'+c.id+'" onclick="acSelectCap(this.dataset.capId)" title="'+c.name+'">'+
    '<span class="mi material-icons-round">'+c.icon+'</span>'+c.name+'</div>'
  ).join('');

  // Drag support — bind the global mousemove/mouseup listeners ONCE. This fn runs
  // on EVERY chat open, so without this guard each open added another live
  // document listener (a growing perf leak on the low-end target).
  if(acInit) return;
  const handle = document.getElementById('ac-drag-handle');
  const chat = document.getElementById('assistant-chat');
  if(!handle||!chat) return;
  acInit = true;
  handle.addEventListener('mousedown', function(e) {{
    if(e.target.closest('.ac-btn')) return;
    acDragging = true;
    const rect = chat.getBoundingClientRect();
    acDragOfs = {{x: e.clientX - rect.left, y: e.clientY - rect.top}};
    e.preventDefault();
  }});
  document.addEventListener('mousemove', function(e) {{
    if(!acDragging) return;
    const chat = document.getElementById('assistant-chat');
    let nx = e.clientX - acDragOfs.x, ny = e.clientY - acDragOfs.y;
    nx = Math.min(Math.max(nx, 8), window.innerWidth - 80);
    ny = Math.min(Math.max(ny, 40), window.innerHeight - 80);
    chat.style.left = nx + 'px';
    chat.style.top = ny + 'px';
    chat.style.right = 'auto';
    chat.style.bottom = 'auto';
  }});
  document.addEventListener('mouseup', function() {{ acDragging = false; }});
}}

function toggleAssistantChat() {{
  const chat = document.getElementById('assistant-chat');
  const pill = document.getElementById('agent-pill');
  if(!chat) return;
  const isOpen = chat.classList.contains('open');
  if(isOpen) {{
    chat.classList.remove('open');
    pill.classList.remove('hidden');
  }} else {{
    chat.classList.add('open');
    pill.classList.add('hidden');
    initAssistantChat();
    const input = document.getElementById('ac-input');
    if(input) setTimeout(function(){{input.focus()}},100);
  }}
}}

function minimizeAssistant() {{
  const chat = document.getElementById('assistant-chat');
  const pill = document.getElementById('agent-pill');
  if(chat) chat.classList.remove('open');
  if(pill) pill.classList.remove('hidden');
}}

function acSelectCap(id) {{
  acActiveCap = id;
  document.querySelectorAll('.ac-cap').forEach(function(el) {{
    el.classList.toggle('active', el.dataset.capId === id);
  }});
}}

function acAddMsg(role, text) {{
  const msgsEl = document.getElementById('ac-messages');
  if(!msgsEl) return;
  const div = document.createElement('div');
  div.className = 'ac-msg ' + role;
  div.textContent = text;
  msgsEl.appendChild(div);
  msgsEl.scrollTop = msgsEl.scrollHeight;
  return div;
}}

function acSend() {{
  const input = document.getElementById('ac-input');
  if(!input) return;
  const text = input.value.trim();
  if(!text) return;
  input.value = '';

  acAddMsg('user', text);

  // Show typing indicator
  const typing = acAddMsg('assistant', 'Thinking...');
  typing.classList.add('typing');
  // Drive the voice orb's energetic animation for the whole PROCESSING window
  // (not just TTS/mic) so it never looks frozen while the brain is thinking.
  // Cleared on EVERY terminal path below (theme/open fast-paths, success, error)
  // so it can't get stuck true. The orb poll ORs this flag in.
  window._hartThinking = true;

  // M1 — INTENT IS THE DEFAULT OPERATING SURFACE.
  // The orb/command bar composes the desktop from what the human wants: the
  // DEFAULT path sends free-form intent to /api/agent/ask, which routes it
  // through the brain's EXISTING decompose (/chat → CREATE/REUSE) and PUSHES
  // the result as a composed A2UI card via agent_ui_update (the SSE stream
  // paints it through renderAgentOverlay).  'open <named app>' and theme words
  // are demoted to explicit FALLBACK fast-paths, not the spine.
  const lower = text.toLowerCase();
  if(lower.includes('theme')||lower.includes('font')||lower.includes('bigger')||
     lower.includes('smaller')||lower.includes('dark')||lower.includes('light')) {{
    const fakeResp = {{set textContent(v){{typing.textContent=v;typing.classList.remove('typing')}}}};
    handleThemeCommand(lower, fakeResp);
    window._hartThinking = false;  // terminal: handled locally, no brain wait
    return;
  }}
  // Fallback fast-path: launch a NAMED app directly (no brain round-trip).
  if(lower.startsWith('open ')) {{
    const target = lower.replace('open ','').trim();
    const match = Object.entries(MANIFEST).find(([k,v])=>
      v.title.toLowerCase().includes(target)||k.includes(target));
    if(match) {{
      openPanel(match[0]);
      typing.textContent = 'Opened ' + match[1].title;
      typing.classList.remove('typing');
      window._hartThinking = false;  // terminal: launched locally, no brain wait
      return;
    }}
  }}

  // Default: route the intent through the brain and COMPOSE the desktop.
  // Bound the client wait (server /chat caps at 30s): without a signal a wedged
  // brain or a saturated shell pool left this fetch — and the 'Thinking...'
  // bubble — hung forever. _sig aborts at 32s into the friendly catch below.
  fetch(SHELL+'/api/agent/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{text:text,capability:acActiveCap}}),signal:_sig(32000)}})
    .then(function(r){{return r.json()}}).then(function(data){{
      const reply = data.response || data.error || 'No response';
      // The composed card is painted on the desktop by the SSE overlay stream;
      // the bubble is the spoken acknowledgement (casual chat still replies).
      typing.textContent = data.composed ? ('✦ ' + reply) : reply;
      typing.classList.remove('typing');
      window._hartThinking = false;  // terminal: response arrived
      speakText(reply, 'chat_response');
    }}).catch(function(){{
      typing.textContent = 'Assistant unavailable. It may still be starting - try again in a moment.';
      typing.classList.remove('typing');
      window._hartThinking = false;  // terminal: request failed
    }});
}}

function acVoiceInput() {{
  toggleVoice();
}}

// Init on load
setTimeout(initAssistantChat, 500);

function handleThemeCommand(text, resp) {{
  let customization = {{}};
  if(text.includes('bigger')||text.includes('larger')) customization = {{font:{{size:16,heading_size:22}}}};
  else if(text.includes('smaller')) customization = {{font:{{size:12,heading_size:16}}}};
  else if(text.includes('dark')) {{ applyPreset('hart-default',resp); return; }}
  else if(text.includes('light')||text.includes('arctic')) {{ applyPreset('arctic',resp); return; }}
  else if(text.includes('cyberpunk')) {{ applyPreset('cyberpunk',resp); return; }}
  else if(text.includes('midnight')) {{ applyPreset('midnight',resp); return; }}
  else if(text.includes('forest')) {{ applyPreset('forest',resp); return; }}
  else if(text.includes('sunset')||text.includes('warm')) {{ applyPreset('sunset',resp); return; }}
  else if(text.includes('minimal')) {{ applyPreset('minimal',resp); return; }}
  else if(text.includes('potato')||text.includes('ultra')||text.includes('lite')||text.includes('performance')||text.includes('fast')) {{ applyPreset('potato',resp); return; }}
  else {{ resp.textContent='Try: dark, light, cyberpunk, midnight, forest, sunset, potato, bigger, smaller'; return; }}

  fetch(BACKEND+'/api/appearance/customize',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},body:JSON.stringify(customization)}})
    .then(r=>r.json()).then(()=>{{
      resp.textContent='Done! Refreshing...';
      setTimeout(()=>location.reload(), 500);
    }}).catch(()=>{{ resp.textContent='Failed to customize'; }});
}}

function applyPreset(id, resp) {{
  fetch(BACKEND+'/api/appearance/apply',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{theme_id:id}})}})
    .then(r=>r.json()).then(()=>{{
      resp.textContent='Applied '+id+'! Refreshing...';
      setTimeout(()=>location.reload(), 500);
    }}).catch(()=>{{ resp.textContent='Failed to apply theme'; }});
}}

// Focus trap: keep Tab within the active modal surface (lock screen / start menu
// / dialog) so keyboard focus can't escape behind it. No-op when none is open.
document.addEventListener('keydown', function(e) {{
  if(e.key!=='Tab') return;
  const trap = document.querySelector('.lock-screen.active, .start-menu.open, .ds-modal-overlay.ds-open .ds-modal');
  if(!trap) return;
  const nodes = trap.querySelectorAll('button,input,select,textarea,a[href],[tabindex]:not([tabindex="-1"]),[role="button"][tabindex]');
  const f = Array.prototype.filter.call(nodes, el=>el.offsetParent!==null);
  if(!f.length) return;
  const first=f[0], last=f[f.length-1], act=document.activeElement;
  if(!trap.contains(act)) {{ e.preventDefault(); first.focus(); }}
  else if(e.shiftKey && act===first) {{ e.preventDefault(); last.focus(); }}
  else if(!e.shiftKey && act===last) {{ e.preventDefault(); first.focus(); }}
}});

// ═══ Context Menu ═══
document.addEventListener('contextmenu', e => {{
  e.preventDefault();
  const menu = document.getElementById('ctx-menu');
  // Desktop right-click
  if(e.target.classList.contains('wallpaper')||e.target===document.body) {{
    menu.innerHTML = [
      ctxItem('add_to_home_screen','Add app to desktop','window.hartAddAppPicker&&hartAddAppPicker()'),
      ctxItem('grid_view','Auto-arrange icons','window.hartAutoArrange&&hartAutoArrange()'),
      ctxSep(),
      ctxItem('palette','Personalize','openPanel("wallpaper_manager")'),
      ctxItem('wallpaper','Wallpaper','openPanel("wallpaper_manager")'),
      ctxSep(),
      ctxItem('terminal','Terminal','launchApp("terminal")'),
      ctxItem('refresh','Refresh','location.reload()'),
    ].join('');
  }} else {{
    menu.innerHTML = [
      ctxItem('open_in_new','Open in New Panel',''),
      ctxItem('info','Properties',''),
    ].join('');
  }}
  menu.style.left = e.clientX+'px';
  menu.style.top = e.clientY+'px';
  menu.style.display = 'block';
}});
document.addEventListener('click', ()=>{{document.getElementById('ctx-menu').style.display='none';}});
function _closeCtx() {{ document.getElementById('ctx-menu').style.display='none'; }}

function ctxItem(icon,label,action) {{
  return '<div class="ctx-menu-item" onclick="'+action+';_closeCtx()">'+
    '<span class="mi material-icons-round">'+icon+'</span>'+label+'</div>';
}}
function ctxSep() {{ return '<div class="ctx-menu-sep"></div>'; }}

// ═══ Keyboard Shortcuts ═══
document.addEventListener('keydown', e => {{
  // Super key (Meta) — toggle start menu
  if(e.key==='Meta'&&!e.ctrlKey&&!e.altKey) {{ e.preventDefault(); toggleStartMenu(); }}
  // Alt+F4 — close focused panel
  if(e.key==='F4'&&e.altKey&&focusedPanel) {{ e.preventDefault(); closePanel(focusedPanel); }}
  // Alt+Tab — cycle through panels
  if(e.key==='Tab'&&e.altKey) {{
    e.preventDefault();
    // MRU order: Alt+Tab flips to the PREVIOUSLY-focused window (Win11/macOS),
    // not creation order. Fall back to creation order if MRU is incomplete.
    const order = mru.filter(id=>panels[id]);
    const ids = order.length>=2 ? order : Object.keys(panels);
    if(ids.length<2) return;
    const cur = ids.indexOf(focusedPanel);
    const idx = cur<0 ? 0 : (cur+1)%ids.length;
    bringToFront(ids[idx]);
  }}
  // Super+D — show desktop (minimize all)
  if(e.key==='d'&&e.metaKey) {{ e.preventDefault(); Object.keys(panels).forEach(minimizePanel); }}
  // Super+L — lock
  if(e.key==='l'&&e.metaKey) {{ e.preventDefault(); shellAction('lock'); }}
  // Super+E — files
  if(e.key==='e'&&e.metaKey) {{ e.preventDefault(); openPanel('backup'); }}
  // Super+A — agent
  if(e.key==='a'&&e.metaKey) {{ e.preventDefault(); focusAgent(); }}
  // Super+Left/Right — snap panel
  if(e.key==='ArrowLeft'&&e.metaKey&&focusedPanel) {{ e.preventDefault(); snapPanel(focusedPanel,'left'); }}
  if(e.key==='ArrowRight'&&e.metaKey&&focusedPanel) {{ e.preventDefault(); snapPanel(focusedPanel,'right'); }}
  // Super+Up — maximize, Super+Down — minimize
  if(e.key==='ArrowUp'&&e.metaKey&&focusedPanel) {{ e.preventDefault(); toggleMax(focusedPanel); }}
  if(e.key==='ArrowDown'&&e.metaKey&&focusedPanel) {{ e.preventDefault(); minimizePanel(focusedPanel); }}
  // Escape — close start menu
  if(e.key==='Escape'&&startOpen) toggleStartMenu();
  // F11 — fullscreen focused
  if(e.key==='F11'&&focusedPanel) {{ e.preventDefault(); toggleMax(focusedPanel); }}
}});

// ═══ Shell Actions ═══
function shellAction(action) {{
  if(action==='lock') {{
    document.getElementById('lock-screen').classList.add('active');
    document.getElementById('lock-pw').focus();
    return;
  }}
  const labels = {{suspend:'put the system to sleep',restart:'restart the system',shutdown:'shut down the system',firmware:'restart into the Firmware (UEFI) setup'}};
  const titles = {{firmware:'Restart to Firmware'}};
  dsConfirm(titles[action]||action.charAt(0).toUpperCase()+action.slice(1),
    'Are you sure you want to '+(labels[action]||action)+'?',
    {{okLabel:(titles[action]||action.charAt(0).toUpperCase()+action.slice(1)), danger:action==='shutdown'}}).then(function(ok){{
    if(ok) fetch(SHELL+'/api/shell/session/'+action,{{method:'POST'}}).then(function(r){{
      // The 'firmware' action is gated server-side too — surface a clean refusal
      // if the box turns out not to support boot-to-firmware (legacy BIOS).
      if(action==='firmware' && r && !r.ok) r.json().then(function(j){{
        dsConfirm('Firmware setup unavailable', (j&&j.error)||'Not supported on this system.', {{okLabel:'OK'}});
      }}).catch(()=>{{}});
    }}).catch(()=>{{}});
  }});
}}
// Reveal the "Restart to Firmware (UEFI)" power button only on a UEFI box that
// advertises the boot-to-firmware capability (hidden on legacy BIOS). Pure read.
fetch(SHELL+'/api/shell/session/firmware-capable').then(function(r){{return r.json();}}).then(function(j){{
  if(j && j.supported){{ var b=document.getElementById('power-btn-firmware'); if(b) b.style.display=''; }}
}}).catch(()=>{{}});
function unlock() {{
  // In production: PAM verification. Dev mode: any password works.
  document.getElementById('lock-screen').classList.remove('active');
}}

// ═══ App Launch ═══
function launchApp(appId) {{
  fetch(SHELL+'/api/shell/launch',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{app_id:appId,subsystem:'linux'}})}}).catch(()=>{{}});
}}

// ═══ Close start menu on outside click ═══
document.addEventListener('click', e => {{
  if(startOpen && !document.getElementById('start-menu').contains(e.target) &&
     !e.target.closest('.start-btn')) {{
    toggleStartMenu();
  }}
}});

// ═══ Voice I/O (push-to-talk + TTS) ═══
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let _acAudio = null;  // current server-TTS <audio>, tracked so user speech can interrupt it (barge-in)

function toggleVoice() {{
  if(isRecording) {{ stopRecording(); return; }}
  startRecording();
}}

async function startRecording() {{
  try {{
    const stream = await navigator.mediaDevices.getUserMedia({{audio:true}});
    // Feed the live mic into the voice orb for REAL listening reactivity (the
    // orb analyses, never plays it back — no echo). Safe no-op if not ready yet.
    try {{ if(window._hartVoiceOrb) window._hartVoiceOrb.connectStream(stream); }} catch(e) {{}}
    const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '';
    mediaRecorder = mimeType ? new MediaRecorder(stream,{{mimeType}}) : new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = function(e) {{ audioChunks.push(e.data); }};
    mediaRecorder.onstop = async function() {{
      stream.getTracks().forEach(function(t){{t.stop();}});
      // Release the mic from the orb so the stopped track doesn't linger on the
      // analyser; the speaking/idle cases fall back to the synthetic sine.
      try {{ if(window._hartVoiceOrb) window._hartVoiceOrb.disconnect(); }} catch(e) {{}}
      const blob = new Blob(audioChunks, {{type: mediaRecorder.mimeType || 'audio/webm'}});
      const formData = new FormData();
      formData.append('audio', blob, 'voice.webm');
      const resp = document.getElementById('agent-resp');
      resp.textContent = 'Transcribing...';
      resp.classList.add('visible');
      try {{
        const r = await fetch(SHELL+'/api/voice', {{method:'POST', body:formData}});
        const data = await r.json();
        if(data.text) {{
          if(window.HartHeroShowTranscript) window.HartHeroShowTranscript(data.text);
          const aci = document.getElementById('ac-input');
          if(aci) {{ aci.value = data.text; acSend(); }}
        }} else if(data.error) {{
          resp.textContent = data.error;
        }}
      }} catch(err) {{ resp.textContent = 'Voice processing failed'; }}
    }};
    acStopSpeaking();  // barge-in: stop any in-progress TTS the instant the user starts speaking
    mediaRecorder.start();
    isRecording = true;
    var _mb = document.querySelector('.mic-btn');
    if(_mb) _mb.classList.add('recording');  // guarded: no such el when the mic lives in the chat (was an unguarded null-deref)
    var _sm = document.getElementById('hart-senses-mic');  // bottom sensory-cluster mic mirrors listening state
    if(_sm) _sm.classList.add('listening');
    showToast('Voice','Recording... click mic again to stop','info');
  }} catch(err) {{
    showToast('Voice','Microphone access denied','warning');
  }}
}}

function stopRecording() {{
  if(mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  isRecording = false;
  const btn = document.querySelector('.mic-btn');
  if(btn) btn.classList.remove('recording');
  const _sm = document.getElementById('hart-senses-mic');  // clear the bottom mic's listening state
  if(_sm) _sm.classList.remove('listening');
}}

// Stop any in-progress TTS — browser SpeechSynthesis + the server <audio>.
// Single canonical "stop talking": used for barge-in (startRecording) and to avoid overlapping replies (speakText).
function acStopSpeaking() {{
  try {{ if('speechSynthesis' in window) speechSynthesis.cancel(); }} catch(e) {{}}
  try {{ if(_acAudio) {{ _acAudio.pause(); _acAudio = null; }} }} catch(e) {{}}
}}

// TTS helper — hybrid: browser instant + server quality
function speakText(text, source) {{
  if(!text || PERF.potato) return;
  source = source || 'chat_response';
  acStopSpeaking();  // never overlap two replies; also gives _acAudio a clean handle
  // 1. Browser instant feedback (Web Speech API)
  if('speechSynthesis' in window) {{
    const utt = new SpeechSynthesisUtterance(text);
    utt.rate = 1.0; utt.pitch = 1.0;
    speechSynthesis.speak(utt);
  }}
  // 2. Server quality audio (async, replaces browser TTS when ready)
  fetch(SHELL+'/api/voice/speak', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{text:text, source:source}})
  }}).then(function(r){{ return r.json(); }}).then(function(d){{
    if(d.audio_url && !d.error) {{
      if('speechSynthesis' in window) speechSynthesis.cancel();
      _acAudio = new Audio(SHELL+d.audio_url);
      _acAudio.play().catch(function(){{}});
    }}
  }}).catch(function(){{}});
}}

// ── HART OS native voice orb: reflect the shell's EXISTING voice state ──
// Reuses isRecording (listening) + _acAudio (speaking) by polling.
// setActive drives the viz's built-in speech-energy animation.
// ALWAYS init (NOT potato-gated): the orb's idle breathing animation is cheap
// (a single rAF loop + a 200ms two-boolean poll) and is the centerpiece of the
// voice-first desktop — a frozen/absent orb on a live USB (which classifies as
// "potato") makes the shell look dead. The EXPENSIVE audio-reactive path
// (getByteFrequencyData) is already gated INSIDE voiceOrbViz.js by `active`, so
// it only runs while actually speaking/listening — no cost when idle.
(function initHartOrb() {{
  var c = document.getElementById('hart-voice-orb');
  if(!c || !window.HartVoiceOrbViz) {{ setTimeout(initHartOrb, 400); return; }}
  var orb = window.HartVoiceOrbViz(c, {{}});
  // Expose the orb so the record path can feed it the REAL mic stream
  // (connectStream) for true listening reactivity. The mic is NOT routed to the
  // speakers inside voiceOrbViz.js (analyser only), so this can't echo/feedback.
  window._hartVoiceOrb = orb;
  // FIX B: sync the canvas breathe glow to the persisted orb-breathing pref. hartHero
  // OWNS the pref + the 'hart_orb_breathing' key; we read it back through
  // HartOrbBreathing.get() (no parallel localStorage parse). If hartHero has not
  // loaded yet the orb stays default-ON and hartHero's own sync damps it on load -
  // race-free either way.
  try {{ if (window.HartOrbBreathing && orb.setBreathing) orb.setBreathing(window.HartOrbBreathing.get()); }} catch(e) {{}}
  // #140: apply the persisted orb VARIETY. hartPersonalize owns the pref (the
  // customization hub) via HartSession.orb_style; we read it back through
  // window.HartOrbStyle (no parallel persistence). If hartPersonalize hasn't
  // loaded yet the orb stays default 'vibrant' and HartOrbStyle.restore() applies
  // it once ready - idempotent either way.
  try {{ if (window.HartOrbStyle && orb.setStyle) orb.setStyle(window.HartOrbStyle.get()); }} catch(e) {{}}
  c.style.opacity = '0.9';
  setInterval(function() {{
    var speaking = _acAudio && !_acAudio.paused && !_acAudio.ended;
    // Animate while SPEAKING (TTS), LISTENING (mic), or PROCESSING (the
    // "Thinking…" window set by acSend) — so the orb is never frozen mid-thought.
    orb.setActive(!!(speaking || isRecording || window._hartThinking));
  }}, 200);
}})();

// ═══ SSE Live Agent Action Stream ═══
// Renders ALL agent components as floating overlay fragments in real-time.
// Notification = toast. Everything else = floating glass panel overlay.
if(!PERF.potato) {{
  try {{
    const evtSrc = new EventSource(SHELL+'/api/notifications/stream');
    evtSrc.onmessage = function(e) {{
      try {{
        const events = JSON.parse(e.data);
        events.forEach(function(ev) {{
          const type = ev.type || 'notification';
          if(type === 'notification') {{
            showToast(ev.title||ev.agent||'Notification', ev.message||'', ev.severity||'info');
          }} else if(type === 'app_installed') {{
            // Installed app -> live desktop icon. Reuse hartDesktop's manifest
            // merge + hartPinIcon (no fork); icon appears without a refresh.
            if(window.hartInstallIcon) window.hartInstallIcon(ev);
            showToast('Installed', (ev.title||ev.id||'App')+' added to your desktop', 'info');
          }} else if(type === 'home' || type === 'home_compose') {{
            // The local LLM re-composes the assembled HOME live (i1, agentic
            // Liquid UI): route the A2UI payload to HartHome.compose instead of a
            // floating overlay. ev.payload is the {{hero,rows}} composition; we
            // pass ev itself as the fallback so a flat payload also works.
            if(window.HartHome) window.HartHome.compose(ev.payload || ev);
          }} else {{
            // Render as floating overlay fragment
            renderAgentOverlay(ev);
          }}
        }});
      }} catch(err) {{}}
    }};
    evtSrc.onerror = function() {{ /* SSE reconnects automatically */ }};
  }} catch(err) {{}}
}}

// ═══ Approval Helper ═══
function _postApproval(agentId, action, decision) {{
  try {{
    fetch(SHELL+'/api/agent/approval', {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{agent_id:agentId, action:action, decision:decision}})
    }}).catch(function(){{}});
  }} catch(e) {{}}
}}

// ═══ Agent Action Floating Overlay Renderer ═══
var _overlayStack = [];
// HTML escape — prevents XSS from agent-pushed content
function _esc(s){{if(!s)return'';var d=document.createElement('div');d.textContent=String(s);return d.innerHTML;}}
function _submitA2UIForm(form) {{
  event.preventDefault();
  var action = form.dataset.action || '/api/a2ui';
  var fd = {{}};
  new FormData(form).forEach(function(v,k){{fd[k]=v;}});
  fetch(SHELL+action,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(fd)}}).catch(function(){{}});
  return false;
}}
function shellA2UIListSelect(el) {{
  try {{
    fetch(SHELL+(el.dataset.action||'/api/a2ui'),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{selected:parseInt(el.dataset.idx||0),item:el.dataset.item||''}})}}).catch(function(){{}});
  }} catch(e) {{}}
}}
function _doApproval(btn, verdict) {{
  var div = btn.closest('[data-agent-id]');
  if(div) {{ _postApproval(div.dataset.agentId||'', div.dataset.action||'', verdict); }}
  var ov = btn.closest('.agent-overlay');
  if(ov) ov.remove();
}}

function renderAgentOverlay(ev) {{
  // Sanitize all string fields to prevent XSS injection
  var _orig = ev;
  ev = {{}};
  for(var k in _orig) {{ ev[k] = (typeof _orig[k] === 'string') ? _esc(_orig[k]) : _orig[k]; }}
  // Preserve arrays/objects that need special handling
  if(_orig.items) ev.items = _orig.items;
  if(_orig.apps) ev.apps = _orig.apps;
  if(_orig.steps) ev.steps = _orig.steps;
  if(_orig.fields) ev.fields = _orig.fields;
  if(_orig.data) ev.data = _orig.data;
  if(_orig.labels) ev.labels = _orig.labels;
  if(_orig.children) ev.children = _orig.children;
  var id = 'overlay-'+(ev.agent||'')+(ev._ts||Date.now());
  // Remove oldest if > 3 overlays
  while(_overlayStack.length >= 3) {{
    var oldest = _overlayStack.shift();
    var el = document.getElementById(oldest);
    if(el) el.remove();
  }}
  var overlay = document.createElement('div');
  overlay.id = id;
  overlay.className = 'agent-overlay glass ds-fade-in';
  overlay.style.cssText = 'position:fixed;bottom:'+(80+_overlayStack.length*220)+'px;right:16px;z-index:'+(2000+_overlayStack.length)+';width:360px;max-height:200px;overflow-y:auto;border-radius:16px;padding:16px;backdrop-filter:blur(20px);background:rgba(20,20,30,0.85);border:1px solid rgba(255,255,255,0.08);box-shadow:0 8px 32px rgba(0,0,0,0.4);animation:dsSlideUp 0.3s ease;';
  var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><span class="ds-label-sm ds-text-accent">'+(ev.agent||'Agent')+'</span><span class="mi material-icons-round" style="cursor:pointer;font-size:16px;color:var(--hart-muted)" onclick="this.parentElement.parentElement.remove()">close</span></div>';
  var type = ev.type||'card';

  if(type === 'product_card') {{
    html += '<div style="display:flex;gap:12px">';
    if(ev.image) html += '<img src="'+ev.image+'" style="width:64px;height:64px;border-radius:8px;object-fit:cover">';
    html += '<div><div class="ds-body-md" style="font-weight:600">'+(ev.name||'Product')+'</div>';
    html += '<div class="ds-body-sm ds-text-muted">'+(ev.description||'').substring(0,100)+'</div>';
    html += '<div style="margin-top:4px"><span class="ds-label-sm ds-text-accent">'+(ev.price||'Free')+'</span>';
    if(ev.rating) html += ' <span class="ds-label-sm ds-text-muted">★ '+ev.rating+'</span>';
    html += '</div></div></div>';
    if(ev.buy_action) html += '<div style="margin-top:8px;text-align:right">'+dsBtn('Buy',{{variant:'primary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'"+ev.buy_action+"',{{method:'POST'}})"}})+'</div>';

  }} else if(type === 'cart') {{
    html += '<div class="ds-body-md" style="font-weight:600">🛒 Cart ('+(ev.items||[]).length+' items)</div>';
    (ev.items||[]).forEach(function(item){{
      html += '<div class="ds-list-item" style="padding:4px 0"><span class="ds-body-sm">'+_esc(item.name)+'</span><span class="ds-label-sm ds-text-accent" style="margin-left:auto">'+_esc(item.price)+'</span></div>';
    }});
    html += '<div style="border-top:1px solid rgba(255,255,255,0.1);margin-top:8px;padding-top:8px;text-align:right"><span class="ds-body-md ds-text-accent">Total: '+(ev.total||0)+' '+(ev.currency||'Spark')+'</span></div>';

  }} else if(type === 'checkout') {{
    html += '<div class="ds-body-md" style="font-weight:600">Checkout</div>';
    html += '<div class="ds-body-sm ds-text-muted">'+(ev.items||[]).length+' items - '+(ev.total||0)+' '+(ev.currency||'Spark')+'</div>';
    if(ev.confirm_action) html += '<div style="margin-top:8px;text-align:right">'+dsBtn('Confirm Payment',{{variant:'primary',cls:'ds-btn-sm',onclick:"fetch(SHELL+'"+ev.confirm_action+"',{{method:'POST'}})"}})+'</div>';

  }} else if(type === 'payment_status') {{
    var statusIcon = ev.status==='success'?'check_circle':ev.status==='pending'?'hourglass_empty':'error';
    var statusColor = ev.status==='success'?'var(--hart-success)':ev.status==='pending'?'var(--hart-accent)':'var(--hart-error)';
    html += '<div style="text-align:center;padding:8px"><span class="mi material-icons-round" style="font-size:40px;color:'+statusColor+'">'+statusIcon+'</span><div class="ds-body-md" style="margin-top:8px">'+(ev.status||'unknown').toUpperCase()+'</div><div class="ds-body-sm ds-text-muted">'+(ev.amount||'')+' via '+(ev.method||'')+'</div></div>';

  }} else if(type === 'order_tracking') {{
    html += '<div class="ds-body-md" style="font-weight:600">Order '+(ev.order_id||'')+'</div>';
    (ev.steps||[]).forEach(function(step,i){{
      var done = i < (ev.current_step||0);
      html += '<div style="display:flex;align-items:center;gap:8px;padding:2px 0"><span class="mi material-icons-round" style="font-size:16px;color:'+(done?'var(--hart-success)':'var(--hart-muted)')+'">'+(done?'check_circle':'radio_button_unchecked')+'</span><span class="ds-body-sm">'+(step.label||step)+'</span></div>';
    }});
    if(ev.eta) html += '<div class="ds-label-sm ds-text-muted" style="margin-top:4px">ETA: '+ev.eta+'</div>';

  }} else if(type === 'comparison') {{
    html += '<div class="ds-body-md" style="font-weight:600">Feature Comparison</div>';
    (ev.apps||[]).forEach(function(a){{
      html += '<div class="ds-list-item" style="padding:4px 0"><span class="ds-body-sm" style="font-weight:600">'+a.name+'</span><span class="ds-label-sm ds-text-muted" style="margin-left:auto">★ '+(a.rating||'-')+'</span></div>';
    }});
    if(ev.winner) html += '<div class="ds-label-sm ds-text-accent" style="margin-top:4px">Winner: '+ev.winner+'</div>';

  }} else if(type === 'progress') {{
    var pct = Math.min(100, Math.max(0, (ev.value||0)/(ev.max||100)*100));
    html += '<div class="ds-body-sm">'+(ev.label||'Progress')+'</div>';
    html += '<div style="background:#1a1a1a;border-radius:4px;height:8px;margin-top:4px"><div style="width:'+pct+'%;height:100%;border-radius:4px;background:'+(ev.color||'var(--hart-accent)')+';transition:width 0.3s"></div></div>';
    html += '<div class="ds-label-sm ds-text-muted" style="margin-top:2px">'+Math.round(pct)+'%</div>';

  }} else if(type === 'agent_action') {{
    var actionIcon = ev.status==='completed'?'check_circle':ev.status==='error'?'error':'play_circle';
    html += '<div style="display:flex;align-items:center;gap:8px"><span class="mi material-icons-round" style="font-size:20px">'+actionIcon+'</span><div><div class="ds-body-sm">'+(ev.description||ev.action_type||'Action')+'</div><div class="ds-label-sm ds-text-muted">'+(ev.status||'running')+'</div></div></div>';
    if(ev.result) html += '<div class="ds-body-sm ds-text-muted" style="margin-top:4px;font-style:italic">'+String(ev.result).substring(0,150)+'</div>';

  }} else if(type === 'chart') {{
    html += '<div class="ds-body-sm" style="font-weight:600">'+(ev.title||'Chart')+'</div>';
    var chartData = ev.data||[];
    var chartLabels = ev.labels||[];
    var chartType = ev.chart_type||'bar';
    var maxVal = Math.max.apply(null, chartData.length?chartData:[1]);
    if(chartType === 'bar') {{
      html += '<div style="display:flex;align-items:flex-end;gap:4px;height:100px;margin-top:8px;padding-top:4px;border-bottom:1px solid rgba(255,255,255,0.1)">';
      chartData.forEach(function(v,i){{
        var h = Math.max(4, (v/maxVal)*90);
        html += '<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px">';
        html += '<span class="ds-label-sm" style="font-size:9px;color:var(--hart-accent)">'+v+'</span>';
        html += '<div style="width:100%;height:'+h+'px;background:var(--hart-accent);border-radius:3px 3px 0 0;min-width:12px"></div>';
        html += '</div>';
      }});
      html += '</div>';
      if(chartLabels.length) {{
        html += '<div style="display:flex;gap:4px;margin-top:2px">';
        chartLabels.forEach(function(l){{ html += '<span class="ds-label-sm ds-text-muted" style="flex:1;text-align:center;font-size:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+l+'</span>'; }});
        html += '</div>';
      }}
    }} else {{
      // Line chart: SVG polyline
      var w = 320, h = 80;
      var pts = chartData.map(function(v,i){{ return ((i/(Math.max(1,chartData.length-1)))*w)+','+(h - (v/maxVal)*h); }}).join(' ');
      html += '<svg width="'+w+'" height="'+(h+10)+'" style="margin-top:8px"><polyline points="'+pts+'" fill="none" stroke="var(--hart-accent)" stroke-width="2" stroke-linejoin="round"/>';
      chartData.forEach(function(v,i){{
        var cx = (i/(Math.max(1,chartData.length-1)))*w;
        var cy = h - (v/maxVal)*h;
        html += '<circle cx="'+cx+'" cy="'+cy+'" r="3" fill="var(--hart-accent)"/>';
      }});
      html += '</svg>';
      if(chartLabels.length) {{
        html += '<div style="display:flex;justify-content:space-between;margin-top:2px">';
        chartLabels.forEach(function(l){{ html += '<span class="ds-label-sm ds-text-muted" style="font-size:8px">'+l+'</span>'; }});
        html += '</div>';
      }}
    }}

  }} else if(type === 'code') {{
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
    if(ev.filename) html += '<span class="ds-label-sm ds-text-muted" style="font-family:monospace">'+(ev.filename)+'</span>';
    if(ev.language) html += '<span class="ds-label-sm" style="color:var(--hart-accent);font-size:9px;text-transform:uppercase">'+(ev.language)+'</span>';
    html += '</div>';
    html += '<pre style="margin:0;padding:10px;background:rgba(0,0,0,0.5);border-radius:8px;overflow-x:auto;font-family:"Fira Code","Cascadia Code",monospace;font-size:12px;line-height:1.4;color:#e0e0e0;white-space:pre-wrap;word-break:break-all"><code>'+(ev.content||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</code></pre>';

  }} else if(type === 'markdown') {{
    var md = ev.content||'';
    // Basic markdown→HTML: bold, italic, links, inline code, headers, lists
    md = md.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    md = md.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
    md = md.replace(/\*(.+?)\*/g,'<em>$1</em>');
    md = md.replace(/`([^`]+)`/g,'<code style="background:rgba(255,255,255,0.08);padding:1px 4px;border-radius:3px;font-family:monospace;font-size:0.9em">$1</code>');
    md = md.replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank" style="color:var(--hart-accent);text-decoration:underline">$1</a>');
    md = md.replace(/^### (.+)$/gm,'<div class="ds-body-md" style="font-weight:700;margin-top:6px">$1</div>');
    md = md.replace(/^## (.+)$/gm,'<div class="ds-body-md" style="font-weight:700;font-size:1.1em;margin-top:6px">$1</div>');
    md = md.replace(/^# (.+)$/gm,'<div class="ds-body-lg" style="font-weight:700;margin-top:6px">$1</div>');
    md = md.replace(/^[-*] (.+)$/gm,'<div style="padding-left:12px">&#8226; $1</div>');
    md = md.replace(/^\d+\. (.+)$/gm,function(m,p1){{ return '<div style="padding-left:12px">'+m.split('.')[0]+'. '+p1+'</div>'; }});
    md = md.split(String.fromCharCode(10)).join('<br>');
    html += '<div class="ds-body-sm" style="line-height:1.5">'+md+'</div>';

  }} else if(type === 'media') {{
    var mediaType = ev.media_type||ev.type||'image';
    var src = ev.src||ev.url||'';
    var alt = ev.alt||'Media';
    if(ev.title) html += '<div class="ds-body-sm" style="font-weight:600;margin-bottom:4px">'+(ev.title)+'</div>';
    if(mediaType === 'video' || src.match(/\.(mp4|webm|ogg)($|\?)/i)) {{
      html += '<video src="'+src+'" '+(ev.controls!==false?'controls':'')+' style="width:100%;border-radius:8px;max-height:160px" preload="metadata">'+alt+'</video>';
    }} else if(mediaType === 'audio' || src.match(/\.(mp3|wav|ogg|aac)($|\?)/i)) {{
      html += '<audio src="'+src+'" '+(ev.controls!==false?'controls':'')+' style="width:100%">'+alt+'</audio>';
    }} else {{
      html += '<img src="'+src+'" alt="'+alt+'" style="width:100%;border-radius:8px;max-height:160px;object-fit:cover" onerror="this.hidden=true">';
    }}
    if(ev.caption) html += '<div class="ds-label-sm ds-text-muted" style="margin-top:4px">'+(ev.caption)+'</div>';

  }} else if(type === 'metric') {{
    var trend = ev.trend||'flat';
    var arrow = trend==='up'?'\\u2191':trend==='down'?'\\u2193':'\\u2192';
    var tColor = trend==='up'?'var(--hart-success)':trend==='down'?'var(--hart-error)':'var(--hart-muted)';
    html += '<div style="text-align:center;padding:8px 0">';
    html += '<div style="font-size:32px;font-weight:700;color:var(--hart-text)">'+(ev.value||0)+'<span class="ds-label-sm" style="font-size:14px;margin-left:4px;color:var(--hart-muted)">'+(ev.unit||'')+'</span></div>';
    html += '<div class="ds-body-sm" style="margin-top:2px">'+(ev.label||'Metric')+' <span style="color:'+tColor+';font-weight:600">'+arrow+'</span></div>';
    if(ev.explanation) html += '<div class="ds-label-sm ds-text-muted" style="margin-top:4px">'+(ev.explanation)+'</div>';
    html += '</div>';

  }} else if(type === 'form') {{
    html += '<div class="ds-body-md" style="font-weight:600;margin-bottom:8px">'+(ev.title||'Form')+'</div>';
    var formId = 'form-'+(ev._ts||Date.now());
    html += '<form id="'+formId+'" data-action="'+(ev.action||'/api/a2ui')+'" style="display:flex;flex-direction:column;gap:6px" onsubmit="return _submitA2UIForm(this)">';
    (ev.fields||[]).forEach(function(f){{
      var ftype = f.type||'text';
      var fname = f.name||f.label||'field';
      html += '<div>';
      if(f.label) html += '<label class="ds-label-sm" style="display:block;margin-bottom:2px;color:var(--hart-muted)">'+f.label+'</label>';
      if(ftype === 'textarea') {{
        html += '<textarea name="'+fname+'" placeholder="'+(f.placeholder||'')+'" style="width:100%;padding:6px 8px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:6px;color:var(--hart-text);font-size:13px;resize:vertical;min-height:40px">'+(f.value||'')+'</textarea>';
      }} else if(ftype === 'select') {{
        html += '<select name="'+fname+'" style="width:100%;padding:6px 8px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:6px;color:var(--hart-text);font-size:13px">';
        (f.options||[]).forEach(function(o){{ html += '<option value="'+(o.value||o)+'">'+(o.label||o)+'</option>'; }});
        html += '</select>';
      }} else {{
        html += '<input type="'+ftype+'" name="'+fname+'" placeholder="'+(f.placeholder||'')+'" value="'+(f.value||'')+'" style="width:100%;padding:6px 8px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:6px;color:var(--hart-text);font-size:13px">';
      }}
      html += '</div>';
    }});
    html += '<div style="text-align:right;margin-top:4px">'+dsBtn(ev.submit_label||'Submit',{{variant:'primary',cls:'ds-btn-sm'}})+'</div></form>';

  }} else if(type === 'list') {{
    html += '<div class="ds-body-md" style="font-weight:600;margin-bottom:4px">'+(ev.title||'List')+'</div>';
    var ordered = ev.ordered||false;
    var tag = ordered?'ol':'ul';
    html += '<'+tag+' style="margin:0;padding-left:18px;color:var(--hart-text)">';
    (ev.items||[]).forEach(function(item,i){{
      var text = typeof item === 'string' ? item : (item.label||item.text||item.name||JSON.stringify(item));
      var action = typeof item === 'object' ? item.action : null;
      if(action || ev.interactive) {{
        html += '<li style="padding:2px 0;cursor:pointer;color:var(--hart-accent)" data-action="'+(action||'/api/a2ui')+'" data-idx="'+i+'" data-item="'+_esc(text)+'" onclick="shellA2UIListSelect(this)">'+(text)+'</li>';
      }} else {{
        html += '<li style="padding:2px 0">'+(text)+'</li>';
      }}
    }});
    html += '</'+tag+'>';

  }} else if(type === 'approval') {{
    html += '<div class="ds-body-md" style="font-weight:600;margin-bottom:4px">Approval Required</div>';
    html += '<div class="ds-body-sm ds-text-muted" style="margin-bottom:8px">'+(ev.description||ev.action||'An agent requests your approval.')+'</div>';
    if(ev.agent_id) html += '<div class="ds-label-sm ds-text-muted" style="margin-bottom:6px">Agent: '+(ev.agent_id)+'</div>';
    html += '<div style="display:flex;gap:6px;justify-content:flex-end" data-agent-id="'+(ev.agent_id||'')+'" data-action="'+(ev.action||'')+'">';
    html += '<button class="ds-btn ds-btn-primary ds-btn-sm" onclick="dsRipple(event);_doApproval(this,&quot;approve&quot;)"><span>Approve</span></button>';
    html += '<button class="ds-btn ds-btn-outline ds-btn-sm" onclick="dsRipple(event);_doApproval(this,&quot;deny&quot;)"><span>Deny</span></button>';
    html += '<button class="ds-btn ds-btn-ghost ds-btn-sm" onclick="dsRipple(event);this.closest(&quot;.agent-overlay&quot;).remove()"><span>Later</span></button>';
    html += '</div>';

  }} else if(type === 'navigate') {{
    var target = ev.target||'';
    var transition = ev.transition||'default';
    // Only allow known panel IDs and safe internal /api/ routes — no external URLs
    if(MANIFEST[target] || SYSTEM_PANELS[target]) {{
      openPanel(target, ev.params||{{}});
    }} else if(target.indexOf('/api/') === 0 && target.indexOf('..') === -1) {{
      fetch(SHELL+target, {{method:'GET',signal:_sig(5000)}}).catch(function(){{}});
    }}
    // External URLs and arbitrary paths are BLOCKED — prevents SSRF/open redirect
    // Minimal overlay confirmation
    html += '<div style="text-align:center;padding:8px"><span class="mi material-icons-round" style="font-size:24px;color:var(--hart-accent)">open_in_new</span><div class="ds-body-sm" style="margin-top:4px">Navigating to '+(ev.title||target||'...')+'</div></div>';

  }} else {{
    // Generic fallback
    html += '<div class="ds-body-md" style="font-weight:600">'+(ev.title||type)+'</div>';
    html += '<div class="ds-body-sm ds-text-muted">'+(ev.content||ev.message||JSON.stringify(ev).substring(0,200))+'</div>';
  }}

  overlay.innerHTML = html;
  document.body.appendChild(overlay);
  _overlayStack.push(id);

  // Auto-dismiss after 15s (except checkout/approval)
  if(type !== 'checkout' && type !== 'approval') {{
    setTimeout(function(){{
      var el = document.getElementById(id);
      if(el) {{ el.style.opacity='0'; el.style.transform='translateX(100px)'; setTimeout(function(){{el.remove()}},300); }}
      _overlayStack = _overlayStack.filter(function(x){{return x!==id}});
    }}, 15000);
  }}
}}

// ═══ Recent Files in Start Menu ═══
(function loadRecentFiles() {{
  fetch(SHELL+'/api/shell/files/recent',{{signal:_sig(3000)}})
    .then(function(r){{return r.json();}}).then(function(data) {{
      const files = data.files || [];
      if(files.length === 0) return;
      const scroll = document.getElementById('start-scroll');
      if(!scroll) return;
      const section = document.createElement('div');
      section.className = 'start-group';
      section.innerHTML = '<div class="start-group-label">Recent Files</div><div class="start-grid">' +
        files.slice(0,8).map(function(f) {{
          return '<div class="start-item" data-path="'+f.path.replace(/"/g,'&quot;')+'" onclick="launchApp(&quot;xdg-open&quot;,this.dataset.path)">' +
            '<span class="mi material-icons-round" style="color:var(--hart-muted)">description</span>' +
            '<span class="label" title="'+f.path+'">'+f.name+'</span></div>';
        }}).join('') + '</div>';
      scroll.appendChild(section);
    }}).catch(function(){{}});
}})();

// ═══ Login Greeting ═══
(function loginGreeting() {{
  if(PERF.potato) return;
  Promise.all([
    fetch(BACKEND+'/api/social/dashboard/agents',{{signal:_sig(3000)}}).then(function(r){{return r.json();}}).catch(function(){{return {{}}; }}),
    fetch(BACKEND+'/api/social/dashboard/health',{{signal:_sig(3000)}}).then(function(r){{return r.json();}}).catch(function(){{return {{}}; }}),
  ]).then(function([agents,health]) {{
    const agentCount = (agents.agents||[]).filter(function(a){{return a.status==='running';}}).length;
    const peerCount = health.peer_count || 0;
    const hour = new Date().getHours();
    const greeting = hour<12?'Good morning':hour<17?'Good afternoon':'Good evening';
    const msg = greeting+'! '+agentCount+' agent'+(agentCount!==1?'s':'')+' running, '+peerCount+' peer'+(peerCount!==1?'s':'')+' connected.';
    showToast('HART', msg, 'info');
    setTimeout(function(){{ speakText(msg, 'greeting'); }}, 1000);
  }});
}})();
</script>
</body></html>'''

    def _render_component(self, comp: dict) -> str:
        """Render a single A2UI component to HTML snippet."""
        comp_type = comp.get('type', 'card')
        if comp_type == 'card':
            title = comp.get('title', '')
            content = comp.get('content', '')
            children_html = ''.join(
                self._render_component(c) for c in comp.get('children', []))
            return (f'<div class="card"><h3>{title}</h3>'
                    f'<p>{content}</p>{children_html}</div>')
        elif comp_type == 'metric':
            return (
                f'<div class="metric">'
                f'<span>{comp.get("label", "")}</span>'
                f'<span style="font-weight:600">{comp.get("value", "")}'
                f'{comp.get("unit", "")}</span></div>')
        elif comp_type == 'notification':
            return (
                f'<div class="notification notification-{comp.get("severity", "info")}">'
                f'<strong>{comp.get("title", "")}</strong>: '
                f'{comp.get("message", "")}</div>')
        elif comp_type == 'list':
            items = ''.join(f'<li>{i}</li>' for i in comp.get('items', []))
            return f'<ul>{items}</ul>'
        elif comp_type == 'markdown':
            return f'<div>{comp.get("content", "")}</div>'
        elif comp_type == 'approval':
            return (
                f'<div style="padding:12px;background:var(--hart-surface);'
                f'border-radius:8px;margin:8px 0">'
                f'<strong>Agent "{comp.get("agent_id", "?")}"</strong> '
                f'requests: {comp.get("action", "?")}<br>'
                f'{comp.get("description", "")}</div>')
        elif comp_type == 'progress':
            value = comp.get('value', 0)
            max_val = comp.get('max', 100)
            pct = int((value / max_val) * 100) if max_val else 0
            return (
                f'<div><label>{comp.get("label", "")}</label>'
                f'<div style="height:6px;background:var(--hart-surface);'
                f'border-radius:3px;overflow:hidden">'
                f'<div style="height:100%;width:{pct}%;'
                f'background:var(--hart-active);border-radius:3px"></div>'
                f'</div></div>')
        return f'<div>{json.dumps(comp)}</div>'

    # ─── HTTP Server (Glass Shell + Shell APIs) ───────────────

    def _create_flask_app(self):
        """Create Flask app serving the glass desktop shell + APIs."""
        # Register this instance the moment the shell is wired to be served —
        # covers BOTH standalone serve_forever() AND the Nunba desktop bundle
        # (HART OS *is* the Nunba desktop, co-located in-process), so every
        # in-process A2UI emitter reaches the LIVE shell via
        # get_registry().get_or_none('LiquidUIService').  Idempotent.
        self._register_self()
        from flask import Flask, request, jsonify, Response, send_from_directory

        # The shell HTML loads its logo + every external script from
        # the ``/shell/static/`` prefix (see render_desktop_shell: hart-logo.svg,
        # voiceOrbViz.js, hartHero.js, hartDesktop.js, hartOnboarding.js, ...).
        # Flask's DEFAULT static route is ``/static`` — so without this prefix
        # EVERY ``/shell/static/*`` request 404s on a real boot: the orb never
        # animates (only the static mic shows), the hero input/desktop never
        # wire (dead clicks, can't type), onboarding never fires, and the logo
        # renders as a broken-image "?". The ``static/`` dir is bundled into the
        # ISO (hart-app.nix copies the tree) and sits next to this module, so
        # Flask's built-in handler serves it directly — no parallel route. This
        # was invisible to "inline render" testing, which never fetches
        # ``/shell/static/``; the route test below exercises the real fetch.
        app = Flask(__name__, static_url_path='/shell/static',
                    static_folder='static')

        # ── Desktop Shell (the root page IS the OS) ──
        @app.route('/')
        def index():
            return Response(self.render_desktop_shell(), mimetype='text/html')

        @app.route('/favicon.ico')
        def favicon():
            return Response(status=204)

        # ── build/index.html's 12s liveness probe ──
        # The Nunba dist falls back to a `fetch('/cors/test')` to decide whether
        # the server is up; without this route it 404s and the loader shows a
        # misleading "Server is starting up… Reload to retry." Stub it 200 so the
        # probe reports the shell is alive. Harmless + unconditional (it answers
        # even when no Nunba dist is mounted).
        @app.route('/cors/test')
        def cors_test():
            return Response('ok', mimetype='text/plain')

        # ── Central-owned agent art (offline, by name-slug) ──
        # Serves the real owned agent image the central instance drops into
        # HART_AGENT_ART_DIR (or the bundled static/app_art/agents/ dir), resolved
        # by app_poster.find_central_agent_file. That resolver re-slugs the id
        # ([a-z0-9-] only) and only ever builds paths INSIDE the known drop dirs,
        # so an arbitrary <slug> can never traverse out. A miss returns 404 and the
        # agent card falls back to the generated art / brand-art scrim. No network.
        @app.route('/shell/agent-art/<slug>')
        def shell_agent_art(slug):
            try:
                from integrations.agent_engine import app_poster
                path = app_poster.find_central_agent_file(slug)
            except Exception:
                path = None
            if not path or not os.path.isfile(path):
                return Response(status=404)
            return send_from_directory(os.path.dirname(path),
                                       os.path.basename(path))

        # ── Nunba SPA embedding (React pages inside panel iframes) ──
        # The dist is a no-basename BrowserRouter (history) SPA whose bundle refs
        # are origin-root absolute ('/static/js/main.*.js', '/static/css/…'), so
        # it can ONLY be served at the origin root, exactly how Nunba's own
        # app.py:4033-4040 serves it (file-or-index catch-all from the build
        # dir). We mirror that single pattern here (no parallel path): a /static
        # passthrough for the hashed bundles plus one SPA history fallback that
        # serves a real file when it exists, else index.html so the in-browser
        # router resolves '/social', '/agents', '/admin', … itself. Both are
        # gated on NUNBA_STATIC_DIR: when it is unset, /static stays a 404
        # (the shell's own assets live at the distinct /shell/static prefix, so
        # the floor-lock is preserved). Werkzeug matches by rule specificity, so
        # the '/<path:path>' catch-all is tried LAST: every explicit route ('/',
        # '/favicon.ico', '/health', '/cors/test', all '/api/*', '/api/shell/*',
        # '/shell/static/*') still wins.
        nunba_dir = os.environ.get('NUNBA_STATIC_DIR', '')
        if nunba_dir and os.path.isdir(nunba_dir):
            @app.route('/static/<path:path>')
            def nunba_bundle(path):
                return send_from_directory(
                    os.path.join(nunba_dir, 'static'), path)

            @app.route('/<path:path>')
            def nunba_spa(path):
                file_path = os.path.join(nunba_dir, path)
                if os.path.isfile(file_path):
                    return send_from_directory(nunba_dir, path)
                return send_from_directory(nunba_dir, 'index.html')

        # ── Legacy API: UI components (for terminal/Conky fallback) ──
        @app.route('/api/ui', methods=['GET'])
        def api_ui():
            context = self.context_engine.get_context()
            ui = self.generate_ui(context)
            inner_html = ''.join(
                self._render_component(c) for c in ui.get('components', []))
            return jsonify({
                'source': ui.get('source'), 'html': inner_html,
                'context': ui.get('context_summary'),
                'component_count': len(ui.get('components', [])),
            })

        @app.route('/api/context', methods=['GET'])
        def api_context():
            return jsonify(self.context_engine.get_context())

        # ── A2UI (agent pushes UI components) ──
        @app.route('/api/a2ui', methods=['POST'])
        def api_a2ui():
            import time as _time
            data = request.get_json(force=True)
            comp = data.get('component', {})
            comp['_ts'] = _time.time()
            success = self.agent_ui_update(
                data.get('agent_id', 'unknown'), comp)
            return jsonify({'success': success})

        # ── Agentic HOME compose (the local LLM paints the Netflix home) ──
        # The single producer entry point for the agentic home feed: a {hero,
        # rows} composition flows through compose_home -> agent_ui_update (the
        # governed A2UI channel) -> SSE -> HartHome.compose -> render. Accepts
        # either a top-level {hero, rows} body or a wrapped {payload:{...}} one.
        @app.route('/api/home/compose', methods=['POST'])
        def api_home_compose():
            data = request.get_json(force=True, silent=True) or {}
            payload = data.get('payload')
            if not isinstance(payload, dict):
                payload = data
            ok = self.compose_home(
                hero=payload.get('hero'), rows=payload.get('rows'),
                agent_id=str(data.get('agent_id', 'home_composer')))
            return jsonify({'success': ok})

        @app.route('/api/approval', methods=['POST'])
        def api_approval():
            data = request.get_json(force=True)
            result = self.agent_request_approval(
                data.get('agent_id', 'unknown'),
                data.get('action', 'unknown'),
                data.get('description', ''))
            return jsonify(result)

        @app.route('/api/agent/approval', methods=['POST'])
        def handle_agent_approval():
            """Handle approval decisions from Nunba JS or Android clients."""
            data = request.get_json(force=True)
            agent_id = data.get('agent_id', '')
            action = data.get('action', '')
            decision = data.get('decision', '')  # approve / deny / later
            if decision not in ('approve', 'deny', 'later'):
                return jsonify({'error': 'Invalid decision, must be approve/deny/later'}), 400
            # Resolve matching pending approval in _agent_components
            resolved = False
            if agent_id in self._agent_components:
                for comp in self._agent_components[agent_id]:
                    if (comp.get('type') == 'approval'
                            and comp.get('action') == action
                            and comp.get('_decision') is None):
                        comp['_decision'] = decision
                        comp['_decided_at'] = time.time()
                        resolved = True
                        break
            # Push decision via EventBus so other frontends can react
            try:
                from core.platform.events import emit_event
                emit_event('agent.approval.decision', {
                    'agent_id': agent_id,
                    'action': action,
                    'decision': decision,
                })
            except Exception:
                pass
            logger.info("Approval decision: agent=%s action=%s decision=%s resolved=%s",
                        agent_id, action, decision, resolved)
            return jsonify({
                'status': 'ok',
                'agent_id': agent_id,
                'action': action,
                'decision': decision,
                'resolved': resolved,
            })

        # ── Voice ──
        @app.route('/api/voice', methods=['POST'])
        def api_voice():
            # Human kill-switch: if the user has cut the AI's hearing, refuse to
            # transcribe — no mic audio is consumed (core.ai_sensing gate).
            try:
                from core.ai_sensing import allowed
                if not allowed('mic'):
                    return jsonify({'error': 'AI hearing is disabled by the user',
                                    'sensing_disabled': True}), 403
            except Exception:
                pass
            audio = request.files.get('audio')
            if audio:
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as f:
                    audio.save(f)
                    result = self.handle_voice_input(f.name)
            else:
                result = {'error': 'No audio provided'}
            return jsonify(result)

        # ── Theme hot-reload ──
        @app.route('/api/theme', methods=['POST'])
        def update_theme():
            data = request.get_json(force=True, silent=True) or {}
            theme_id = data.get('theme_id', '').strip()
            if not theme_id:
                return jsonify({'error': 'theme_id required'}), 400
            try:
                from integrations.agent_engine.theme_service import ThemeService
                result = ThemeService.apply_theme(theme_id)
                if 'error' in result:
                    return jsonify(result), 404
                return jsonify({'status': 'updated', 'theme': result.get('id')})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        # ── Agent ambient input (text from agent pill) ──
        @app.route('/api/agent/ask', methods=['POST'])
        def agent_ask():
            data = request.get_json(force=True, silent=True) or {}
            text = data.get('text', '').strip()
            if not text:
                return jsonify({'error': 'No text provided'})
            import requests as req
            # M1 — INTENT → DECOMPOSE → COMPOSE.  Route free-form intent through
            # the brain's EXISTING intent classifier (/chat → CREATE/REUSE/tool/
            # vision/casual — no parallel path) and COMPOSE the result onto the
            # desktop as an A2UI card pushed through agent_ui_update (the now-wired
            # B1/B2 push channel), instead of only narrating a chat bubble.  The
            # reply text is still returned so casual chat keeps speaking.
            try:
                resp = req.post(
                    f'http://localhost:{self.backend_port}/chat',
                    json={
                        'user_id': 'hart_desktop_user',
                        'prompt_id': 'desktop_agent',
                        'prompt': text,
                    }, timeout=30)
                payload = resp.json()
            except Exception as e:
                return jsonify({'error': str(e)})
            payload['composed'] = self._compose_intent_result(text, payload)
            return jsonify(payload)

        # ── Shell APIs: Events ──
        @app.route('/api/shell/events', methods=['GET'])
        def shell_events():
            events = []
            try:
                result = subprocess.run(
                    ['journalctl', '--since', '1 hour ago', '-p', '0..5',
                     '--no-pager', '-o', 'short', '-n', '50'],
                    capture_output=True, text=True, timeout=5)
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        parts = line.split(None, 3)
                        events.append({
                            'time': ' '.join(parts[:2]) if len(parts) > 2 else '',
                            'message': parts[-1] if parts else line,
                        })
            except Exception:
                events.append({
                    'time': '', 'message': 'Event log not available'})
            return jsonify({'events': events})

        # ── Shell APIs: Apps ──
        @app.route('/api/shell/apps', methods=['GET'])
        def shell_apps():
            apps = []
            # Linux .desktop files
            app_dirs = ['/usr/share/applications',
                        os.path.expanduser('~/.local/share/applications')]
            for d in app_dirs:
                if not os.path.isdir(d):
                    continue
                try:
                    for fname in os.listdir(d):
                        if not fname.endswith('.desktop'):
                            continue
                        apps.append({
                            'id': fname.replace('.desktop', ''),
                            'name': fname.replace('.desktop', '').replace('-', ' ').title(),
                            'subsystem': 'linux',
                        })
                except OSError:
                    pass
            return jsonify({'apps': apps[:100]})

        # ── Shell APIs: Launch ──
        @app.route('/api/shell/launch', methods=['POST'])
        def shell_launch():
            import re
            data = request.get_json(force=True, silent=True) or {}
            app_id = data.get('app_id', '')
            if not app_id or not re.match(r'^[a-zA-Z0-9._-]+$', app_id):
                return jsonify({'error': 'Invalid app_id'}), 400
            try:
                subprocess.Popen(
                    ['gtk-launch', app_id],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return jsonify({'status': 'launched'})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        # ── Shell APIs: Session ──
        @app.route('/api/shell/session/<action>', methods=['POST'])
        def shell_session(action):
            # #133 — NATIVE logind power actions, result-checked. The shell server
            # runs as the unprivileged `hart` service user; the old fire-and-forget
            # subprocess.Popen(['systemctl', ...]) delegated to logind but never
            # read the polkit verdict, so a DENIED reboot/shutdown/firmware was
            # masked as {'status': action} while the box did nothing. We now invoke
            # the org.freedesktop.login1.Manager method DIRECTLY via the SHARED
            # `_logind_call` helper (busctl, exit-status + stderr checked) — the
            # SAME canonical home /api/shell/power/action uses, so there is one
            # busctl implementation, not two. Failure surfaces a real 500 + error.
            from integrations.agent_engine.shell_os_apis import (
                _logind_call, firmware_setup_supported)
            if action not in ('lock', 'logout', 'suspend', 'shutdown', 'restart',
                              'firmware'):
                return jsonify({'error': 'Invalid action'}), 400
            # 'firmware' = "Restart into Firmware (UEFI)": arm the UEFI boot-to-
            # firmware-UI flag (SetRebootToFirmwareSetup true), THEN reboot — the
            # next boot enters BIOS/UEFI setup. Only meaningful on a UEFI box whose
            # firmware advertises the boot-to-fw capability; refuse on legacy BIOS
            # so the user never gets a plain reboot when they asked for firmware.
            # Single source of truth for the capability probe (shell_os_apis).
            if action == 'firmware' and not firmware_setup_supported():
                return jsonify({
                    'error': 'Reboot to firmware setup is not supported on this '
                             'system (legacy BIOS or capability not advertised)'}), 400
            # The interactive boolean is `true` so logind may consult polkit; the
            # hart-base.nix security.polkit rule grants the `hart` user these
            # login1 actions outright, so the authorized call executes.
            if action == 'lock':
                ok, err = _logind_call('LockSessions')
            elif action == 'logout':
                # Terminate THIS seat session (the shell runs inside the user's
                # session). login1 needs the session id; refuse honestly if it is
                # not in the environment rather than mask a no-op as success.
                sid = os.environ.get('XDG_SESSION_ID', '')
                if not sid:
                    return jsonify({'action': action,
                                    'error': 'No active session id to terminate '
                                             '(XDG_SESSION_ID unset)'}), 500
                ok, err = _logind_call('TerminateSession', 's', sid)
            elif action == 'firmware':
                # Two-step; if arming fails we do NOT reboot (a plain reboot would
                # be the wrong action for the user's 'enter firmware setup' intent).
                ok, err = _logind_call('SetRebootToFirmwareSetup', 'b', 'true')
                if ok:
                    ok, err = _logind_call('Reboot', 'b', 'true')
            else:
                # DRY (#165): reuse the ONE canonical verb->login1 method map
                # (os_bridge.power._POWER_METHOD) instead of a second inline dict.
                # 'restart' is this session route's public verb for a reboot.
                from integrations.agent_engine.os_bridge.power import _POWER_METHOD
                method = _POWER_METHOD['reboot' if action == 'restart' else action]
                ok, err = _logind_call(method, 'b', 'true')
            if not ok:
                # Real failure (polkit denied, busctl missing, timeout) — surface
                # it, never a masked success.
                return jsonify({'action': action,
                                'error': err or 'power action failed'}), 500
            return jsonify({'status': action})

        @app.route('/api/shell/session/firmware-capable', methods=['GET'])
        def shell_session_firmware_capable():
            """Report whether 'Restart into Firmware (UEFI)' is available so the
            power menu can SHOW the button only on a capable UEFI box (hidden on
            legacy BIOS). Pure read; no privileged action."""
            from integrations.agent_engine.shell_os_apis import (
                firmware_setup_supported)
            return jsonify({'supported': firmware_setup_supported()})

        # ── Shell APIs: Services ──
        @app.route('/api/shell/services', methods=['GET'])
        def shell_services():
            services = []
            svc_names = [
                'hart-backend', 'hart-agent-daemon', 'hart-vision',
                'hart-llm', 'hart-discovery', 'hart-liquid-ui', 'hart-conky']
            for name in svc_names:
                status = 'unknown'
                try:
                    result = subprocess.run(
                        ['systemctl', 'is-active', name],
                        capture_output=True, text=True, timeout=3)
                    status = result.stdout.strip()
                except Exception:
                    pass
                services.append({'name': name, 'status': status})
            return jsonify({'services': services})

        # ── Shell APIs: Session state persistence ──
        @app.route('/api/shell/session-state', methods=['GET'])
        def get_session_state():
            path = os.path.join(self._data_dir, 'shell_session.json')
            if os.path.isfile(path):
                try:
                    with open(path, 'r') as f:
                        return jsonify(json.load(f))
                except Exception:
                    pass
            return jsonify({})

        @app.route('/api/shell/session-state', methods=['POST'])
        def save_session_state():
            data = request.get_json(force=True, silent=True) or {}
            path = os.path.join(self._data_dir, 'shell_session.json')
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w') as f:
                    json.dump(data, f)
                return jsonify({'status': 'saved'})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        # ── Shell APIs: Drivers ──
        @app.route('/api/shell/drivers', methods=['GET'])
        def shell_drivers():
            devices = []
            for cmd, dev_type in [(['lspci', '-mm'], 'pci'), (['lsusb'], 'usb')]:
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    for line in r.stdout.strip().split('\n'):
                        if line.strip():
                            devices.append({'type': dev_type, 'info': line.strip()})
                except Exception:
                    pass
            return jsonify({'devices': devices[:50]})

        # ── Shell APIs: WiFi (CACHED) ──
        # Returns the wifi network list the background _connectivity_cache prober
        # keeps fresh — INSTANT, no nmcli/hostname subprocess on the request
        # thread (CAUSE 1). loadNetworks() fires this on popover-open + every
        # Rescan; serving the cache means a Rescan can never freeze the shell.
        @app.route('/api/shell/network/wifi', methods=['GET'])
        def shell_wifi():
            _connectivity_cache.start()  # idempotent — lazy-start the prober
            return jsonify(_connectivity_cache.wifi_networks())

        # NOTE: distinct view-function name (shell_network_wifi_connect, NOT
        # shell_wifi_connect) + explicit endpoint=. The canonical hardware-control
        # module shell_system_apis.register_shell_system_routes ALSO defines a
        # view named shell_wifi_connect (rule /api/shell/wifi/connect). Flask
        # derives the endpoint from the function name, so a name clash made
        # register_shell_system_routes raise AssertionError ("overwriting an
        # existing endpoint") — which aborted it mid-registration and silently
        # dropped its remaining ~16 routes (all /api/shell/vpn/*, the rest of
        # /api/shell/wifi/*, trash, display rotation). This rule (/network/wifi/*)
        # is the one the shell's own JS calls (liquid_ui_service ~2596), so it
        # stays — only the endpoint name is de-conflicted.
        @app.route('/api/shell/network/wifi/connect', methods=['POST'],
                   endpoint='shell_network_wifi_connect')
        def shell_network_wifi_connect():
            data = request.get_json(silent=True) or {}
            ssid = data.get('ssid', '').strip()
            password = data.get('password', '')
            if not ssid:
                return jsonify({'success': False, 'error': 'SSID required'}), 400
            try:
                cmd = ['nmcli', 'device', 'wifi', 'connect', ssid]
                if password:
                    cmd += ['password', password]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    return jsonify({'success': True, 'message': f'Connected to {ssid}'})
                return jsonify({'success': False, 'error': r.stderr.strip() or 'Connection failed'}), 400
            except subprocess.TimeoutExpired:
                return jsonify({'success': False, 'error': 'Connection timed out'}), 504
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        # Distinct endpoint name (see shell_network_wifi_connect above): the
        # canonical shell_system_apis also defines shell_wifi_disconnect.
        @app.route('/api/shell/network/wifi/disconnect', methods=['POST'],
                   endpoint='shell_network_wifi_disconnect')
        def shell_network_wifi_disconnect():
            try:
                r = subprocess.run(
                    ['nmcli', 'device', 'disconnect', 'wlan0'],
                    capture_output=True, text=True, timeout=10)
                # Try common interface names if wlan0 fails
                if r.returncode != 0:
                    r = subprocess.run(
                        ['nmcli', 'device', 'disconnect', 'wlp0s20f3'],
                        capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    return jsonify({'success': True, 'message': 'Disconnected from WiFi'})
                return jsonify({'success': False, 'error': r.stderr.strip() or 'Disconnect failed'}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @app.route('/api/shell/network/status', methods=['GET'])
        def shell_network_status():
            status = {'interfaces': [], 'dns': [], 'gateway': ''}
            try:
                r = subprocess.run(
                    ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION',
                     'device', 'status'],
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(':')
                    if len(parts) >= 4:
                        status['interfaces'].append({
                            'device': parts[0], 'type': parts[1],
                            'state': parts[2], 'connection': parts[3],
                        })
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ['ip', 'route', 'show', 'default'],
                    capture_output=True, text=True, timeout=3)
                parts = r.stdout.strip().split()
                if 'via' in parts:
                    status['gateway'] = parts[parts.index('via') + 1]
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ['resolvectl', 'status', '--no-pager'],
                    capture_output=True, text=True, timeout=3)
                for line in r.stdout.split('\n'):
                    if 'DNS Servers' in line:
                        status['dns'] = line.split(':',1)[1].strip().split()
                        break
            except Exception:
                pass
            return jsonify(status)

        # ── Shell APIs: Audio ──
        def _parse_volume(vol_info):
            """Extract volume percentage from pactl volume info dict."""
            if isinstance(vol_info, dict):
                for ch in vol_info.values():
                    if isinstance(ch, dict) and 'value_percent' in ch:
                        return int(ch['value_percent'].rstrip('%'))
                    if isinstance(ch, dict) and 'value' in ch:
                        # value is 0-65536 scale
                        return round(int(ch['value']) / 655.36)
            return 100

        @app.route('/api/shell/audio', methods=['GET'])
        def shell_audio():
            sinks = []
            sources = []
            default_sink = ''
            try:
                r = subprocess.run(
                    ['pactl', 'get-default-sink'],
                    capture_output=True, text=True, timeout=3)
                default_sink = r.stdout.strip()
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ['pactl', '--format=json', 'list', 'sinks'],
                    capture_output=True, text=True, timeout=5)
                if r.stdout.strip():
                    raw = json.loads(r.stdout)
                    sinks = [{
                        'id': s.get('name', ''),
                        'name': s.get('description', ''),
                        'mute': s.get('mute', False),
                        'volume': _parse_volume(s.get('volume', {})),
                        'default': s.get('name', '') == default_sink,
                    } for s in raw]
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ['pactl', '--format=json', 'list', 'sources'],
                    capture_output=True, text=True, timeout=5)
                if r.stdout.strip():
                    raw = json.loads(r.stdout)
                    sources = [{
                        'id': s.get('name', ''),
                        'name': s.get('description', ''),
                        'volume': _parse_volume(s.get('volume', {})),
                    } for s in raw]
            except Exception:
                pass
            return jsonify({'sinks': sinks, 'sources': sources})

        @app.route('/api/shell/audio/volume', methods=['POST'])
        def shell_audio_volume():
            data = request.get_json(silent=True) or {}
            sink_id = data.get('sink_id', '')
            volume = data.get('volume')
            if not sink_id or volume is None:
                return jsonify({'success': False, 'error': 'sink_id and volume required'}), 400
            try:
                volume = max(0, min(150, int(volume)))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'volume must be an integer'}), 400
            try:
                r = subprocess.run(
                    ['pactl', 'set-sink-volume', sink_id, f'{volume}%'],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return jsonify({'success': True, 'volume': volume})
                return jsonify({'success': False, 'error': r.stderr.strip()}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @app.route('/api/shell/audio/mute', methods=['POST'])
        def shell_audio_mute():
            data = request.get_json(silent=True) or {}
            sink_id = data.get('sink_id', '')
            muted = data.get('muted', True)
            if not sink_id:
                return jsonify({'success': False, 'error': 'sink_id required'}), 400
            try:
                val = '1' if muted else '0'
                r = subprocess.run(
                    ['pactl', 'set-sink-mute', sink_id, val],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return jsonify({'success': True, 'muted': muted})
                return jsonify({'success': False, 'error': r.stderr.strip()}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @app.route('/api/shell/audio/default', methods=['POST'])
        def shell_audio_default():
            data = request.get_json(silent=True) or {}
            sink_id = data.get('sink_id', '')
            if not sink_id:
                return jsonify({'success': False, 'error': 'sink_id required'}), 400
            try:
                r = subprocess.run(
                    ['pactl', 'set-default-sink', sink_id],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return jsonify({'success': True, 'default_sink': sink_id})
                return jsonify({'success': False, 'error': r.stderr.strip()}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @app.route('/api/shell/audio/source/volume', methods=['POST'])
        def shell_audio_source_volume():
            data = request.get_json(silent=True) or {}
            source_id = data.get('source_id', '')
            volume = data.get('volume')
            if not source_id or volume is None:
                return jsonify({'success': False, 'error': 'source_id and volume required'}), 400
            volume = max(0, min(150, int(volume)))
            try:
                r = subprocess.run(
                    ['pactl', 'set-source-volume', source_id, f'{volume}%'],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return jsonify({'success': True, 'volume': volume})
                return jsonify({'success': False, 'error': r.stderr.strip()}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── Shell APIs: Bluetooth ──
        @app.route('/api/shell/bluetooth', methods=['GET'])
        def shell_bluetooth():
            devices = []
            try:
                r = subprocess.run(
                    ['bluetoothctl', 'devices'],
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(None, 2)
                    if len(parts) == 3:
                        devices.append({'mac': parts[1], 'name': parts[2]})
            except Exception:
                pass
            return jsonify({'devices': devices})

        # ── Shell APIs: Power/Battery ──
        @app.route('/api/shell/power', methods=['GET'])
        def shell_power():
            info = {
                'on_battery': False, 'percent': 100,
                'time_remaining': '', 'state': 'unknown',
            }
            try:
                r = subprocess.run(
                    ['upower', '-i',
                     '/org/freedesktop/UPower/devices/battery_BAT0'],
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.split('\n'):
                    line = line.strip()
                    if 'percentage:' in line:
                        info['percent'] = int(
                            line.split(':')[1].strip().replace('%', ''))
                    elif 'state:' in line:
                        info['state'] = line.split(':')[1].strip()
                        info['on_battery'] = info['state'] == 'discharging'
                    elif 'time to empty:' in line:
                        info['time_remaining'] = line.split(':', 1)[1].strip()
            except Exception:
                pass
            return jsonify(info)

        # ── Shell APIs: Volume (wpctl-first, pactl fallback) ──
        # The default-sink volume get/set/mute helpers (_vol_run / _volume_get)
        # are module-level (defined next to read_gpu_render_mode) so the
        # background connectivity prober and these WRITE routes share ONE
        # implementation — no parallel volume path. The GET below reads the
        # default sink directly (a user action, not the 8s poll); the poll's
        # volume rides the cached connectivity snapshot instead.
        @app.route('/api/shell/volume', methods=['GET'],
                   endpoint='shell_volume_get')
        def shell_volume_get():
            return jsonify(_volume_get())

        @app.route('/api/shell/volume', methods=['POST'],
                   endpoint='shell_volume_set')
        def shell_volume_set():
            data = request.get_json(silent=True) or {}
            volume = data.get('volume')
            if volume is None:
                return jsonify({'available': False,
                                'error': 'volume required'}), 400
            try:
                volume = max(0, min(150, int(volume)))
            except (TypeError, ValueError):
                return jsonify({'available': False,
                                'error': 'volume must be an integer'}), 400
            r = _vol_run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@',
                          str(volume / 100.0)])
            if r and r.returncode == 0:
                return jsonify({'available': True, 'volume': volume,
                                'tool': 'wpctl'})
            r = _vol_run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@',
                          str(volume) + '%'])
            if r and r.returncode == 0:
                return jsonify({'available': True, 'volume': volume,
                                'tool': 'pactl'})
            return jsonify({'available': False,
                            'error': 'no volume tool (wpctl/pactl)'}), 200

        @app.route('/api/shell/volume/mute', methods=['POST'],
                   endpoint='shell_volume_mute')
        def shell_volume_mute():
            data = request.get_json(silent=True) or {}
            muted = data.get('muted', None)
            # wpctl uses toggle|1|0
            arg = 'toggle' if muted is None else ('1' if muted else '0')
            r = _vol_run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', arg])
            if r and r.returncode == 0:
                return jsonify(_volume_get())
            parg = 'toggle' if muted is None else ('1' if muted else '0')
            r = _vol_run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', parg])
            if r and r.returncode == 0:
                return jsonify(_volume_get())
            return jsonify({'available': False,
                            'error': 'no volume tool (wpctl/pactl)'}), 200

        # ── Shell APIs: Connectivity summary (ONE poll for the top-bar) ──
        # hartConnectivity.js polls THIS single endpoint every ~8s (plus on
        # popover-open + toggle). It returns the snapshot the background
        # _connectivity_cache prober keeps fresh — INSTANT, no nmcli/bluetoothctl/
        # wpctl subprocess on the waitress request thread (CAUSE 1: the synchronous
        # six-subprocess probe here saturated the 1-2 thread pool on a software-
        # rendered box and froze every shell fetch). The quick-settings WRITE
        # actions (scan/connect/toggle/set-volume) still hit the per-domain
        # endpoints inline; this is read-only aggregation, NOT a parallel control
        # path. The prober's wifi-radio / bt-power / battery probes still mirror
        # the canonical ones in shell_system_apis.py (those remain nested closures
        # inside register_shell_system_routes; promoting them to module-level pure
        # functions would let both call ONE implementation — tracked TODO).
        @app.route('/api/shell/connectivity/summary', methods=['GET'],
                   endpoint='shell_connectivity_summary')
        def shell_connectivity_summary():
            # Lazy-start the background prober on the first poll (idempotent — it
            # only spawns a daemon thread, never probes on this request thread).
            # Started here, not at app build, so a process that builds the app but
            # never polls (and unrelated tests) never spawns the prober.
            _connectivity_cache.start()
            return jsonify(_connectivity_cache.summary())

        # ── Shell APIs: Display ──
        @app.route('/api/shell/display', methods=['GET'])
        def shell_display():
            displays = []
            try:
                r = subprocess.run(
                    ['xrandr', '--current'],
                    capture_output=True, text=True, timeout=5)
                current_display = None
                for line in r.stdout.split('\n'):
                    if ' connected' in line:
                        parts = line.split()
                        # Find resolution: skip 'primary' keyword if present
                        res = 'unknown'
                        for p in parts[2:]:
                            if 'x' in p and p[0].isdigit():
                                res = p.split('+')[0]  # strip offset
                                break
                        current_display = {
                            'name': parts[0],
                            'resolution': res,
                            'modes': [],
                        }
                        displays.append(current_display)
                    elif current_display and line.startswith('   '):
                        # Mode line: "   1920x1080     60.00*+  50.00"
                        mode_parts = line.strip().split()
                        if mode_parts:
                            mode = mode_parts[0]
                            rates = []
                            active = False
                            for p in mode_parts[1:]:
                                clean = p.replace('*', '').replace('+', '')
                                if '*' in p:
                                    active = True
                                try:
                                    rates.append(float(clean))
                                except ValueError:
                                    pass
                            current_display['modes'].append({
                                'resolution': mode,
                                'rates': rates,
                                'active': active,
                            })
                    elif not line.startswith(' '):
                        current_display = None
            except Exception:
                pass
            return jsonify({'displays': displays})

        @app.route('/api/shell/display/resolution', methods=['POST'])
        def shell_display_resolution():
            data = request.get_json(silent=True) or {}
            output = data.get('output', '')
            resolution = data.get('resolution', '')
            rate = data.get('rate')
            if not output or not resolution:
                return jsonify({'success': False, 'error': 'output and resolution required'}), 400
            try:
                cmd = ['xrandr', '--output', output, '--mode', resolution]
                if rate:
                    cmd += ['--rate', str(rate)]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    return jsonify({'success': True, 'output': output, 'resolution': resolution})
                return jsonify({'success': False, 'error': r.stderr.strip() or 'Failed to set resolution'}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @app.route('/api/shell/display/brightness', methods=['POST'])
        def shell_display_brightness():
            data = request.get_json(silent=True) or {}
            output = data.get('output', '')
            brightness = data.get('brightness')
            if not output or brightness is None:
                return jsonify({'success': False, 'error': 'output and brightness required'}), 400
            brightness = max(0.1, min(1.0, float(brightness)))
            try:
                r = subprocess.run(
                    ['xrandr', '--output', output, '--brightness', str(brightness)],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return jsonify({'success': True, 'brightness': brightness})
                return jsonify({'success': False, 'error': r.stderr.strip()}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        # NOTE: /api/shell/display/scale is registered canonically by
        # shell_desktop_apis.register_shell_desktop_routes (GET/PUT, with both
        # Wayland swaymsg + X11 GDK_SCALE handling). A SECOND inline definition
        # here used the SAME Flask view-function name 'shell_display_scale',
        # which made Flask raise AssertionError ("overwriting an existing
        # endpoint") inside register_shell_desktop_routes — swallowed by the
        # broad except below, but the raise ABORTED the try block so the next
        # registrations (register_shell_system_routes + register_app_install_
        # routes) never ran, silently dropping all /api/shell/* + /api/apps/*
        # (the app store). Removed the inline duplicate; the canonical one wins.

        # ── Shell APIs: System Metrics ──
        @app.route('/api/shell/system/metrics', methods=['GET'])
        def shell_system_metrics():
            metrics = {}
            try:
                import psutil
                # NON-BLOCKING sample (interval=None): return CPU% since the last
                # call instead of sleeping 0.5s on the request thread. This route
                # is POLLED every 4s by hartSessionUI; a blocking 0.5s here pinned
                # a waitress worker for 0.5s out of every 4s forever (12.5% of a
                # 1-thread pool) — a recurring mid-session micro-freeze. The 4s
                # poll cadence is a fine sampling window; the first call after boot
                # reads 0.0 and every subsequent poll is an accurate delta.
                metrics['cpu_percent'] = psutil.cpu_percent(interval=None)
                metrics['cpu_count'] = psutil.cpu_count()
                mem = psutil.virtual_memory()
                metrics['ram'] = {
                    'total_gb': round(mem.total / (1024**3), 1),
                    'used_gb': round(mem.used / (1024**3), 1),
                    'percent': mem.percent,
                }
                disks = []
                for part in psutil.disk_partitions():
                    try:
                        usage = psutil.disk_usage(part.mountpoint)
                        disks.append({
                            'mount': part.mountpoint,
                            'device': part.device,
                            'total_gb': round(usage.total / (1024**3), 1),
                            'used_gb': round(usage.used / (1024**3), 1),
                            'percent': usage.percent,
                        })
                    except (PermissionError, OSError):
                        pass
                metrics['disks'] = disks
                net = psutil.net_io_counters()
                metrics['network'] = {
                    'bytes_sent': net.bytes_sent,
                    'bytes_recv': net.bytes_recv,
                }
                metrics['load_avg'] = list(psutil.getloadavg()) if hasattr(psutil, 'getloadavg') else []
                metrics['uptime_seconds'] = int(
                    __import__('time').time() - psutil.boot_time())
                # Temperatures if available
                try:
                    temps = psutil.sensors_temperatures()
                    if temps:
                        metrics['temperatures'] = {
                            name: [{'label': s.label, 'current': s.current}
                                   for s in sensors[:3]]
                            for name, sensors in temps.items()
                        }
                except (AttributeError, Exception):
                    pass
            except ImportError:
                metrics['error'] = 'psutil not installed'
            # GPU via VRAMManager
            try:
                from integrations.service_tools.vram_manager import get_vram_manager
                gpu = get_vram_manager().detect_gpu()  # instance method — call on the singleton, not the class
                if gpu and gpu.get('name'):
                    metrics['gpu'] = gpu
            except Exception:
                pass
            return jsonify(metrics)

        @app.route('/api/shell/system/processes', methods=['GET'])
        def shell_system_processes():
            procs = []
            try:
                import psutil
                from core.compute_optimizer import iter_processes
                # GIL-safe walker (yields mid-walk) so a polled task-manager
                # panel can't starve the event loop — the #151 class.
                for p in iter_processes(['pid', 'name', 'cpu_percent', 'memory_percent']):
                    try:
                        info = p.info
                        if info.get('cpu_percent', 0) > 0 or info.get('memory_percent', 0) > 0.1:
                            procs.append({
                                'pid': info['pid'],
                                'name': info['name'],
                                'cpu': round(info.get('cpu_percent', 0), 1),
                                'mem': round(info.get('memory_percent', 0), 1),
                            })
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                procs.sort(key=lambda p: p['cpu'], reverse=True)
            except ImportError:
                pass
            return jsonify({'processes': procs[:30]})

        # ── Shell APIs: Log Viewer ──
        @app.route('/api/shell/system/logs', methods=['GET'])
        def shell_system_logs():
            unit = request.args.get('unit', 'hart-*')
            lines = int(request.args.get('lines', 100))
            priority = request.args.get('priority', '')
            since = request.args.get('since', '')
            grep_pattern = request.args.get('grep', '')
            lines = max(1, min(1000, lines))
            try:
                cmd = ['journalctl', '--output=json', '--no-pager',
                       '-u', unit, '-n', str(lines)]
                if priority:
                    cmd += ['-p', priority]
                if since:
                    cmd += ['--since', since]
                if grep_pattern:
                    cmd += ['-g', grep_pattern]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                entries = []
                for line in r.stdout.strip().split('\n'):
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append({
                            'timestamp': entry.get('__REALTIME_TIMESTAMP', ''),
                            'unit': entry.get('_SYSTEMD_UNIT', ''),
                            'priority': entry.get('PRIORITY', ''),
                            'message': entry.get('MESSAGE', ''),
                        })
                    except json.JSONDecodeError:
                        pass
                return jsonify({'entries': entries, 'count': len(entries)})
            except FileNotFoundError:
                return jsonify({'entries': [], 'count': 0,
                                'error': 'journalctl not available'}), 200
            except Exception as e:
                return jsonify({'entries': [], 'error': str(e)}), 500

        @app.route('/api/shell/system/logs/stream', methods=['GET'])
        def shell_system_logs_stream():
            unit = request.args.get('unit', 'hart-*')
            def generate():
                try:
                    proc = subprocess.Popen(
                        ['journalctl', '--output=json', '--no-pager',
                         '-f', '-u', unit],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True)
                    for line in proc.stdout:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            data = json.dumps({
                                'timestamp': entry.get('__REALTIME_TIMESTAMP', ''),
                                'unit': entry.get('_SYSTEMD_UNIT', ''),
                                'message': entry.get('MESSAGE', ''),
                            })
                            yield f'data: {data}\n\n'
                        except json.JSONDecodeError:
                            pass
                except Exception:
                    yield 'data: {"error": "stream unavailable"}\n\n'
            return Response(generate(), mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache',
                                     'X-Accel-Buffering': 'no'})

        # ── Shell APIs: Recent Files ──
        @app.route('/api/shell/files/recent', methods=['GET'])
        def shell_recent_files():
            files = []
            xbel_path = os.path.expanduser(
                '~/.local/share/recently-used.xbel')
            if os.path.isfile(xbel_path):
                try:
                    import xml.etree.ElementTree as ET
                    tree = ET.parse(xbel_path)
                    for bookmark in list(tree.getroot())[-20:]:
                        href = bookmark.get('href', '')
                        if href.startswith('file://'):
                            path = href.replace('file://', '')
                            name = os.path.basename(path)
                            modified = bookmark.get('modified', '')
                            files.append({
                                'name': name, 'path': path,
                                'modified': modified,
                            })
                except Exception:
                    pass
            return jsonify({'files': files[-10:]})

        # ── Agent Action SSE Stream (ALL component types, not just notifications) ──
        @app.route('/api/notifications/stream', methods=['GET'])
        def notification_stream():
            import time as _time

            def generate():
                last_check = _time.time()
                while True:
                    _time.sleep(2)  # 2s for snappier live updates
                    events = []
                    for agent_id, comps in list(
                            self._agent_components.items()):
                        for c in comps:
                            ts = c.get('_ts', 0)
                            if ts > last_check:
                                # Push ALL component types — not just notifications
                                event = dict(c)
                                event['agent'] = agent_id
                                events.append(event)
                    last_check = _time.time()
                    if events:
                        yield f"data: {json.dumps(events)}\n\n"
            return Response(
                generate(), mimetype='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                })

        @app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                'status': 'ok', 'service': 'liquid-ui-shell',
                'model_available': self._model_available,
                'renderer': self.renderer,
            })

        # Register OS management APIs (shell file manager, terminal, desktop
        # settings, system monitoring, app installer).
        #
        # #18 route-drop hardening: EACH module registers inside its OWN
        # try/except. A duplicate-endpoint AssertionError (or an import failure)
        # in one module must NEVER abort a shared block and silently drop the
        # siblings — most visibly the app store (register_app_install_routes) and
        # the OS bridge. The previous single bundled try/except let ONE collision
        # (see the /api/shell/display/scale + /api/shell/wifi/connect notes above)
        # cascade into "next registrations never ran → /api/apps/* dropped → app
        # store dead". Per-module isolation makes that class of drop impossible by
        # construction, and mirrors what flash/media/openclaw/onboarding already do
        # below. Order is unchanged (os → desktop → system → app-install → bridge).
        try:
            from integrations.agent_engine.shell_os_apis import (
                register_shell_os_routes)
            register_shell_os_routes(app)
        except Exception as e:
            logger.warning("Shell OS APIs registration: %s", e)
        try:
            from integrations.agent_engine.shell_desktop_apis import (
                register_shell_desktop_routes)
            register_shell_desktop_routes(app)
        except Exception as e:
            logger.warning("Shell desktop APIs registration: %s", e)
        try:
            from integrations.agent_engine.shell_system_apis import (
                register_shell_system_routes)
            register_shell_system_routes(app)
        except Exception as e:
            logger.warning("Shell system APIs registration: %s", e)
        try:
            from integrations.agent_engine.app_installer import (
                register_app_install_routes)
            register_app_install_routes(app)
        except Exception as e:
            logger.warning("App-install (store) APIs registration: %s", e)
        try:
            # Typed native OS-bridge (#133/W3): POST /api/os/invoke +
            # /api/os/contract + /api/os/power/capabilities. The forward path for
            # the WebView SDK (hartOSBridge.js); it reuses shell_os_apis auth/audit
            # + os_bridge.power (one dispatch, no parallel path). The old
            # /api/shell/power/action stays as the backward-compat surface.
            from integrations.agent_engine.os_bridge.routes import (
                register_os_bridge_routes)
            register_os_bridge_routes(app)
        except Exception as e:
            logger.warning("OS-bridge APIs registration: %s", e)

        # Flash-to-USB routes registered SEPARATELY so a failure in this newer,
        # optional module (e.g. the flasher import) can NEVER cascade and drop the
        # core shell APIs above (the #18 route-drop class). Best-effort: the Flash
        # panel just won't work if this fails; everything else stays up.
        try:
            from integrations.agent_engine.shell_flash_apis import (
                register_shell_flash_routes)
            register_shell_flash_routes(app)
        except Exception as e:
            logger.warning("Flash-to-USB API registration: %s", e)

        # AI-senses cross-process authority (Phase 7) — THIS process is the
        # canonical holder of core.ai_sensing._state: register_shell_desktop_routes
        # above mounts POST /api/shell/ai-sensing here, the ONE writer the human's
        # floating-eye button hits. So the authority socket MUST be served from
        # here (not the :6777 backend, whose _state is a different process's copy),
        # so a SEPARATE process — the xdg-desktop-portal-hart ScreenCast handler,
        # its own systemd unit — consults allowed('screen') fail-closed before any
        # native capture. Without this server the portal's query_authority() denies
        # (fail-closed): a missing server never OPENS a capture, it only keeps the
        # portal shut. Best-effort + idempotent: no-op where AF_UNIX is unavailable
        # (Windows dev) or the bind fails.
        try:
            from core import ai_sensing as _ai_sensing
            if _ai_sensing.start_authority_server():
                logger.info(
                    "AI-senses authority server started (cross-process screen "
                    "gate for the portal) on %s", _ai_sensing._authority_path())
            else:
                logger.debug(
                    "AI-senses authority server not started (no AF_UNIX / bind "
                    "failed) — portal screencast stays fail-closed")
        except Exception as e:
            logger.debug("AI-senses authority server start skipped: %s", e)

        # Register OpenClaw + floating assistant APIs
        try:
            from integrations.openclaw.shell_openclaw_apis import (
                register_openclaw_routes)
            register_openclaw_routes(app)
        except Exception as e:
            logger.warning("OpenClaw APIs registration: %s", e)

        # Register HART onboarding ceremony APIs
        try:
            from integrations.agent_engine.onboarding_routes import (
                register_onboarding_routes)
            register_onboarding_routes(app)
        except Exception as e:
            logger.warning("Onboarding APIs registration: %s", e)

        # Register the local semantic media index routes (W10): media search +
        # the fetch-once web-image cache that feed the agentic home's card art
        # (GET /api/media/search, /api/media/image, /api/media/index/status, ...).
        # These mount on the SAME origin that serves hartHome.js, so the in-WebView
        # fetch is loopback and passes _require_system_auth with no token. Own
        # try/except so a failure can NEVER cascade and drop the sibling shell
        # routes above (the #18 route-drop class).
        try:
            from integrations.agent_engine.media_semantic_index import (
                register_media_routes, register_idle_indexer)
            register_media_routes(app)
            # Start the idle captioner here too (idempotent) so the co-located
            # Nunba desktop bundle — which builds the app via _create_flask_app
            # but may not run serve_forever — still populates the local caption
            # catalog the home cards search for photos.  Self-gating (yields to
            # the user) + local-only, so it never competes with a live session.
            register_idle_indexer()
        except Exception as e:
            logger.warning("Media index API registration: %s", e)

        return app

    # ─── Serve ────────────────────────────────────────────────

    def _register_self(self) -> None:
        """Register this instance so in-process A2UI emitters (channel consent
        cards, the voice bridge, model-ready toasts) can reach it via
        get_registry().get_or_none('LiquidUIService') — the in-process half of
        the A2UI push channel.  A separately-hosted :6800 shell additionally
        receives pushes through the EventBus/WAMP fan-out inside
        agent_ui_update.  Idempotent: a second serve is a no-op, not a
        double-register error.
        """
        try:
            from core.platform.registry import get_registry
            reg = get_registry()
            if not reg.has('LiquidUIService'):
                reg.register('LiquidUIService', lambda: self)
        except Exception as e:
            logger.debug("LiquidUIService self-register skipped: %s", e)

    def serve_forever(self):
        """Start the glass desktop shell service."""
        self._running = True

        # Ensure platform substrate is ready (EventBus, AppRegistry, Extensions)
        try:
            from core.platform.boot_service import ensure_platform
            ensure_platform()
        except Exception as e:
            logger.warning("Platform boot: %s", e)

        def _model_check_loop():
            from core.http_pool import pooled_get
            while self._running:
                try:
                    resp = pooled_get(
                        f'http://localhost:{self.model_bus_port}/v1/status',
                        timeout=3)
                    self._model_available = (
                        resp.status_code == 200 and
                        resp.json().get('backend_count', 0) > 0)
                except Exception:
                    self._model_available = False
                time.sleep(10)

        threading.Thread(target=_model_check_loop, daemon=True).start()

        # Start the low-priority idle media indexer (W10): a self-contained daemon
        # thread that captions Pictures/Videos ONLY while the user is idle (it
        # yields on should_yield_to_user), populating the local catalog the home's
        # cards search for real photos. Idempotent + its own try/except so an
        # indexer fault never blocks the shell from serving.
        try:
            from integrations.agent_engine.media_semantic_index import (
                register_idle_indexer)
            register_idle_indexer()
        except Exception as e:
            logger.warning("Media idle indexer start: %s", e)

        app = self._create_flask_app()
        logger.info("LiquidUI Glass Shell starting on port %d", self.port)

        # Auto-scale threads by hardware tier. The FLOOR is sized so the always-on
        # notifications SSE (a per-connection waitress thread held for the whole
        # session) plus one inherently-blocking request (the 30s chat proxy, a
        # multi-second nmcli/pactl/journalctl panel route) can never starve the
        # steady UI polls — the mid-session freeze. See _resolve_shell_pool_threads.
        try:
            from security.system_requirements import get_tier_name
            tier = get_tier_name()
        except Exception:
            tier = 'standard'
        threads = _resolve_shell_pool_threads(tier)

        try:
            from waitress import serve
            serve(app, host='0.0.0.0', port=self.port, threads=threads)
        except ImportError:
            app.run(host='0.0.0.0', port=self.port, threaded=True)


# ════════════════════════════════════════════════════════════════════════════
# AGENTIC HOME PRODUCER (gap Q2 - "the home composes itself live")
# ════════════════════════════════════════════════════════════════════════════
# The agentic-home transport was wired end to end (compose_home ->
# agent_ui_update -> SSE -> HartHome.compose -> render) but had NO producer: the
# only callers of compose_home were the manual /api/home/compose route + tests,
# so in practice the surface was hartHome.js's offline samplePayload upgraded by
# direct client fetches - never an agent composition. These functions ARE that
# producer. They:
#   1. gather the live surfaces the home is composed FROM (the agent dashboard,
#      recipes on disk, the earnings wallet) - the SAME truth hartHome.js reads,
#      so producer and client never diverge (no parallel data path);
#   2. compose a deterministic {hero, rows} BACKBONE from that real context;
#   3. let the local LLM (the heart) CURATE the narrative + emphasis over the
#      backbone - a small, reliable JSON so the on-device 4B succeeds; any
#      failure leaves the deterministic backbone standing, so the home NEVER
#      breaks when the model can't emit clean JSON;
#   4. hand it to compose_home -> agent_ui_update - the ONE governed transport
#      (the human kill-switch, the per-agent rate cap, the immutable audit and
#      the XSS reject all live there). No new channel, no new gate.
# The autonomous agent daemon drives run_home_compose() when the box is idle, so
# the home stays alive whether the user is at the machine or away. Card imagery
# is hydrated client-side by hartHome.js (card.image_url -> the /api/media cache
# | else a local media-index search by card.topic|title), so the producer only
# supplies good titles/topics + an optional real web image_url; it never embeds
# bytes and reads no personal media.


def _home_time_of_day() -> str:
    """A natural time-of-day phrase for the contextual hero narrative."""
    try:
        h = time.localtime().tm_hour
    except Exception:
        return 'today'
    if h < 5:
        return 'overnight'
    if h < 12:
        return 'this morning'
    if h < 17:
        return 'this afternoon'
    if h < 21:
        return 'this evening'
    return 'tonight'


# keyword -> Material icon, so a card without an explicit icon still reads right.
_HOME_ICON_MAP = (
    ('research', 'travel_explore'), ('trade', 'candlestick_chart'),
    ('market', 'campaign'), ('content', 'edit_note'), ('video', 'movie'),
    ('coding', 'terminal'), ('code', 'terminal'), ('social', 'groups'),
    ('tutor', 'school'), ('learn', 'school'), ('english', 'menu_book'),
    ('speech', 'record_voice_over'), ('finance', 'payments'),
    ('news', 'newspaper'), ('vision', 'visibility'), ('image', 'image'),
    ('robot', 'smart_toy'), ('analytics', 'insights'),
)


def _home_icon_for(s) -> str:
    sl = str(s or '').lower()
    for needle, icon in _HOME_ICON_MAP:
        if needle in sl:
            return icon
    return 'smart_toy'


def _home_clean_text(v, n: int) -> str:
    """Trim + de-em-dash + strip angle brackets (so the A2UI XSS reject in
    agent_ui_update never has to drop the whole push) + clamp to n chars."""
    if v is None:
        return ''
    s = str(v).replace('—', '-').replace('–', '-')
    s = re.sub(r'[<>]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:n]


def _home_agent_card(a: dict, action: str, target: Optional[str] = None) -> dict:
    """One Netflix card from a dashboard agent row (reuses the canonical
    dashboard agent shape; no parallel agent model)."""
    name = _home_clean_text(
        a.get('name') or a.get('current_task') or 'Agent', 60) or 'Agent'
    gtype = str(a.get('type') or '').replace('_goal', '')
    card = {'title': name, 'topic': name,
            'icon': _home_icon_for(gtype or name), 'action': action}
    # Per-agent art (#143), OFFLINE-FIRST resolution order:
    #   1. CENTRAL-owned image by name (app_poster.central_agent_art) - real owned
    #      art the central instance drops/bundles, served same-origin with NO
    #      network. Stamped on card.image (which the client prefers), so it wins.
    #   2. LOCAL generated art (app_poster.agent_art_url) - only when an on-device
    #      image generator is reachable via the Model Bus; stamped on image_url.
    #   3. neither -> the client composites HartBrandArt + the dark-to-light scrim
    #      + the name (the honest default). The scrim/text-over-art is preserved
    #      in every case (makeCard always lays the scrim over card.image).
    try:
        from integrations.agent_engine import app_poster
        central = app_poster.central_agent_art(name)
    except Exception:
        central = None
    if central:
        card['image'] = central
    else:
        try:
            art = app_poster.agent_art_url(name)
        except Exception:
            art = None
        if art:
            card['image_url'] = art
    status = str(a.get('status') or '').lower()
    if status in ('running', 'in_progress', 'active'):
        card['live'] = 'running'
    if target:
        card['target'] = target
    elif action == 'open':
        card['target'] = 'agents_browse'
    return card


def _home_flagship_row() -> dict:
    """The curated, always-present product agents (ready to run, fully local).
    Mirrors hartHome.js samplePayload's Flagship row - the canonical HART OS
    product agents - so a daemon push (which replaces the row set) never drops
    them. Product/curation data, not a logic fork."""
    return {
        'title': 'Flagship agents', 'note': 'ready to run, fully local',
        'accent': 'violet', 'see_all': 'agents_browse', 'flagship': True,
        'cards': [
            {'title': 'Auto Research', 'topic': 'research',
             'icon': 'travel_explore', 'meta': 'scout the web, then synthesize',
             'action': 'ask',
             'prompt': 'Start the Auto Research agent on a topic I care about'},
            {'title': 'Trading', 'topic': 'trading charts',
             'icon': 'candlestick_chart', 'meta': 'paper-trade live signals',
             'action': 'ask', 'prompt': 'Open the Trading agent'},
            {'title': 'Tutor', 'topic': 'studying', 'icon': 'school',
             'meta': 'learn anything, step by step', 'action': 'ask',
             'prompt': 'Be my Tutor'},
            {'title': 'English Learning', 'topic': 'books', 'icon': 'menu_book',
             'meta': 'grammar and vocabulary', 'action': 'ask',
             'prompt': 'Start English Learning'},
            {'title': 'Spoken English', 'topic': 'conversation',
             'icon': 'record_voice_over', 'meta': 'practice speaking out loud',
             'action': 'ask', 'prompt': 'Practice Spoken English with me'},
            {'title': 'Speech Therapy', 'topic': 'therapy',
             'icon': 'spatial_audio', 'meta': 'guided exercises',
             'action': 'ask', 'prompt': 'Start a Speech Therapy session'},
        ],
    }


# Curated App Store fill for the Apps row (reverse-DNS Flathub id + display
# name). High-recognition picks mirroring hart-app-catalog.json; product
# curation, not a logic fork (same rationale as _home_flagship_row).
_HOME_FLAGSHIP_APPS = (
    ('org.mozilla.firefox', 'Firefox'),
    ('org.videolan.VLC', 'VLC'),
    ('org.libreoffice.LibreOffice', 'LibreOffice'),
    ('org.gimp.GIMP', 'GIMP'),
    ('com.obsproject.Studio', 'OBS Studio'),
    ('org.blender.Blender', 'Blender'),
)


def _home_app_card(app_id: str, name: str) -> dict:
    """One Netflix card for an app (#143). OFFLINE-FIRST: a BUNDLED official/brand
    logo (shell_manifest.bundled_app_logo, served same-origin, no network) is
    PREFERRED and stamped on card.image, which the client (makeCard) prefers over
    the network card.image_url - so a known app shows real art with the network
    OFF. Only when no bundled logo exists do we resolve the marketplace/official
    poster (fetched + cached ONCE by the W10 ImageCache) onto card.image_url; a
    miss there leaves both unset so the client paints the deterministic brand-art
    tile. The card opens the App Store."""
    from integrations.agent_engine import app_poster, shell_manifest
    disp = _home_clean_text(name, 60) or 'App'
    card = {'title': disp, 'topic': disp, 'icon': 'apps',
            'action': 'open', 'target': 'app_store'}
    try:
        logo = shell_manifest.bundled_app_logo(app_id)
    except Exception:
        logo = None
    if logo:
        card['image'] = logo                     # bundled, offline, wins
        return card
    try:
        poster = app_poster.resolve_app_poster(app_id, prefer='poster')
    except Exception:
        poster = None
    if poster:
        card['image_url'] = poster               # network enhancement only
    return card


def _home_app_cards(installed: List[dict]) -> List[dict]:
    """The Apps row: the user's INSTALLED apps first, then curated flagship
    fill, capped at 8, de-duped by app id / title."""
    cards: List[dict] = []
    seen = set()
    for app in (installed or []):
        aid = str((app or {}).get('app_id') or '').strip()
        nm = _home_clean_text((app or {}).get('name'), 60)
        if not nm:
            continue
        key = aid.lower() or nm.lower()
        if key in seen:
            continue
        seen.add(key)
        cards.append(_home_app_card(aid, nm))
        if len(cards) >= 8:
            return cards
    for aid, nm in _HOME_FLAGSHIP_APPS:
        if aid.lower() in seen:
            continue
        seen.add(aid.lower())
        cards.append(_home_app_card(aid, nm))
        if len(cards) >= 8:
            break
    return cards


def _home_recipe_cards() -> List[dict]:
    """Recipe cards from the flow-0 recipe artifacts on disk (the SAME files the
    REUSE pipeline reads). Newest first, capped. Never raises."""
    cards: List[dict] = []
    try:
        from core.platform_paths import get_recipe_prompts_dir
        d = get_recipe_prompts_dir()
    except Exception:
        d = 'prompts'
    try:
        import glob as _glob
        files = _glob.glob(os.path.join(d, '*_recipe.json'))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except Exception:
        files = []
    for fp in files[:10]:
        title = os.path.basename(fp).replace('_recipe.json', '')
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                r = json.load(f)
            if isinstance(r, dict):
                title = (r.get('name') or r.get('title') or r.get('goal')
                         or r.get('prompt') or title)
        except Exception:
            pass
        title = _home_clean_text(title, 60) or 'Recipe'
        cards.append({'title': title, 'topic': title, 'icon': 'auto_awesome',
                      'badge': 'Replay', 'action': 'open', 'target': 'recipes'})
    return cards


def _home_resolve_owner_earnings():
    """Best-effort (owner_uid, spark_balance) for the value-first hero.

    Resolves the node owner from the most recent goal (the canonical
    goal_owner_user_id helper) and reads their REAL Spark via the canonical
    ResonanceService wallet - no shadow ledger, no invented figure. Returns
    (None, None) on a fresh node so the hero stays honest-empty (the client's
    own session-scoped earnings read then stands). Never raises."""
    try:
        from integrations.social.models import get_db, AgentGoal
        from integrations.social.resonance_engine import ResonanceService
    except Exception:
        return (None, None)
    db = None
    try:
        db = get_db()
        goal = db.query(AgentGoal).order_by(AgentGoal.created_at.desc()).first()
        uid = None
        if goal is not None:
            try:
                from core.event_attribution import goal_owner_user_id
                uid = goal_owner_user_id(goal)
            except Exception:
                uid = (getattr(goal, 'user_id', None)
                       or getattr(goal, 'created_by', None))
        spark = None
        if uid:
            wallet = ResonanceService.get_wallet(db, str(uid))
            if wallet:
                spark = wallet.get('spark')
                if spark is None:
                    spark = wallet.get('balance')
        spark_i = int(spark) if isinstance(spark, (int, float)) else None
        return (str(uid) if uid else None, spark_i)
    except Exception as e:
        logger.debug("home earnings resolve: %s", e)
        return (None, None)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _gather_home_context(backend_port: int = 6777,
                         model_bus_port: int = 6790) -> dict:
    """Read the live surfaces the home is composed FROM (best-effort; degrades
    cleanly per-source so one dead surface never empties the home)."""
    ctx = {
        'time_of_day': _home_time_of_day(),
        'agents_total': 0, 'agents_running': 0,
        'continue': [], 'hive': [], 'recipes': [],
        'owner_uid': None, 'spark': None,
    }
    # Agents - the canonical truth-grounded dashboard view (no parallel query).
    try:
        from integrations.social.models import get_db
        from integrations.social.dashboard_service import DashboardService
        db = get_db()
        try:
            dash = DashboardService.get_dashboard(db)
        finally:
            db.close()
        agents = [a for a in (dash.get('agents') or []) if isinstance(a, dict)]
        ctx['agents_total'] = len(agents)
        running = [a for a in agents
                   if str(a.get('status') or '').lower()
                   in ('running', 'in_progress', 'active')]
        ctx['agents_running'] = len(running)
        ctx['continue'] = [_home_agent_card(a, 'resume')
                           for a in (running or agents)[:8]]
        hive = [a for a in agents
                if any(k in str(a.get('type') or '').lower()
                       for k in ('expert', 'trained', 'agent'))]
        ctx['hive'] = [_home_agent_card(a, 'open', target='communities')
                       for a in hive[:8]]
    except Exception as e:
        logger.debug("home ctx agents: %s", e)
    # Recipes on disk (REUSE replay surface).
    try:
        ctx['recipes'] = _home_recipe_cards()
    except Exception as e:
        logger.debug("home ctx recipes: %s", e)
    # Real earnings (value-first hero).
    ctx['owner_uid'], ctx['spark'] = _home_resolve_owner_earnings()
    return ctx


def _deterministic_home_payload(ctx: dict) -> dict:
    """The reliable backbone: a {hero, rows} built purely from real context.
    Always valid (Flagship is always present), so the LLM curation only has to
    colour it - never carry it."""
    rows: List[dict] = []
    cont = ctx.get('continue') or []
    if cont:
        rows.append({'title': 'Continue', 'accent': 'teal',
                     'see_all': 'agents_browse', 'cards': cont})
    rows.append(_home_flagship_row())
    apps = ctx.get('apps') or []
    if apps:
        rows.append({'title': 'Apps', 'note': 'installed + from the store',
                     'accent': 'cyan', 'see_all': 'app_store', 'cards': apps})
    rec = ctx.get('recipes') or []
    if rec:
        rows.append({'title': 'Recipes', 'note': 'replay without re-thinking',
                     'accent': 'amber', 'see_all': 'recipes', 'cards': rec})
    hive = ctx.get('hive') or []
    if hive:
        rows.append({'title': 'Top agents in the hive', 'note': 'from the network',
                     'accent': 'magenta', 'see_all': 'communities',
                     'ranked': True, 'cards': hive})
    # Hero ONLY when there is a REAL positive Spark balance to lead with. A 0 /
    # unresolved balance pushes rows-only so the client's own session-scoped
    # earnings hero is preserved (never clobber a real figure with an empty one).
    hero = None
    spark = ctx.get('spark')
    if isinstance(spark, (int, float)) and spark > 0:
        hero = {
            'eyebrow': 'Earned on the hive',
            'amount': int(spark), 'amount_unit': 'Spark',
            'agents': int(ctx.get('agents_running') or 0),
            'tasks': int(ctx.get('agents_total') or 0),
            'local': True, 'payout_pending': True,
            'primary': {'label': 'Resume', 'action': 'resume',
                        'target': 'recipes'},
            'secondary': {'label': 'Ask anything', 'action': 'ask'},
        }
    return {'hero': hero, 'rows': rows}


def _home_sanitize_card(c) -> Optional[dict]:
    if not isinstance(c, dict):
        return None
    title = _home_clean_text(c.get('title'), 60)
    if not title:
        return None
    card = {'title': title}
    action = c.get('action')
    card['action'] = action if action in HOME_CARD_ACTIONS else 'open'
    icon = _home_clean_text(c.get('icon'), 40)
    if icon and re.match(r'^[a-z0-9_]+$', icon):
        card['icon'] = icon
    meta = _home_clean_text(c.get('meta'), 80)
    if meta:
        card['meta'] = meta
    card['topic'] = _home_clean_text(c.get('topic'), 60) or title
    tgt = c.get('target')
    if tgt in HOME_PANEL_TARGETS:
        card['target'] = tgt
    badge = _home_clean_text(c.get('badge'), 16)
    if badge:
        card['badge'] = badge
    live = _home_clean_text(c.get('live'), 16)
    if live:
        card['live'] = live
    if card['action'] == 'ask':
        prompt = _home_clean_text(c.get('prompt'), 200)
        if prompt:
            card['prompt'] = prompt
    img = c.get('image_url')
    if isinstance(img, str) and (img.startswith('http://')
                                 or img.startswith('https://')):
        card['image_url'] = img[:500]
    # card.image is the OFFLINE-preferred, same-origin art (bundled app logo /
    # central agent image, #143). Allow ONLY the tightly-scoped served prefixes so
    # a hallucinated/hostile string can never smuggle a scheme (javascript:, data:)
    # or an off-origin URL onto the surface - it is not routed through the media
    # cache, the browser loads it directly.
    local_img = c.get('image')
    if (isinstance(local_img, str)
            and (local_img.startswith('/shell/static/app_art/')
                 or local_img.startswith('/shell/agent-art/'))):
        card['image'] = local_img[:200]
    p = c.get('progress')
    if isinstance(p, (int, float)) and 0 <= p <= 1:
        card['progress'] = round(float(p), 3)
    return card


def _home_sanitize_hero(h) -> Optional[dict]:
    if not isinstance(h, dict):
        return None
    amount = h.get('amount')
    if not isinstance(amount, (int, float)) or amount <= 0:
        return None
    hero = {
        'eyebrow': _home_clean_text(h.get('eyebrow'), 40) or 'Earned on the hive',
        'amount': int(amount),
        'amount_unit': _home_clean_text(h.get('amount_unit'), 12) or 'Spark',
        'local': True, 'payout_pending': True,
        'primary': {'label': 'Resume', 'action': 'resume', 'target': 'recipes'},
        'secondary': {'label': 'Ask anything', 'action': 'ask'},
    }
    a = h.get('agents')
    t = h.get('tasks')
    if isinstance(a, (int, float)):
        hero['agents'] = int(a)
    if isinstance(t, (int, float)):
        hero['tasks'] = int(t)
    return hero


def _sanitize_home_payload(payload) -> Optional[dict]:
    """Coerce a (possibly LLM-authored) {hero, rows} to the schema + allow-sets,
    dropping anything unknown/unsafe. Returns a clean payload or None when there
    is no usable row. This is the load-bearing guard that lets the LLM compose
    freely without being able to inject a bad accent / verb / deep-link / markup."""
    if not isinstance(payload, dict):
        return None
    rows_in = payload.get('rows')
    if not isinstance(rows_in, list):
        return None
    rows: List[dict] = []
    for r in rows_in[:8]:
        if not isinstance(r, dict):
            continue
        cards_in = r.get('cards')
        if not isinstance(cards_in, list):
            continue
        cards = []
        for c in cards_in[:12]:
            cc = _home_sanitize_card(c)
            if cc:
                cards.append(cc)
        if not cards:
            continue
        accent = r.get('accent')
        row = {
            'title': _home_clean_text(r.get('title'), 60) or 'Agents',
            'accent': accent if accent in HOME_ROW_ACCENTS else 'teal',
            'cards': cards,
        }
        note = _home_clean_text(r.get('note'), 60)
        if note:
            row['note'] = note
        sa = r.get('see_all')
        if sa in HOME_PANEL_TARGETS:
            row['see_all'] = sa
        if r.get('ranked') is True:
            row['ranked'] = True
        if r.get('flagship') is True:
            row['flagship'] = True
        rows.append(row)
    if not rows:
        return None
    return {'hero': _home_sanitize_hero(payload.get('hero')), 'rows': rows}


def _home_extract_json_obj(text: str):
    """Pull the first JSON object out of an LLM reply (tolerant of code fences
    and surrounding prose - the on-device 4B rarely returns bare JSON)."""
    if not text:
        return None
    s = text
    if '```' in s:
        for part in s.split('```'):
            p = part.strip()
            if p.lower().startswith('json'):
                p = p[4:].strip()
            if p.startswith('{'):
                s = p
                break
    i = s.find('{')
    j = s.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def _llm_curate_home(ctx: dict, backbone: dict, model_bus_port: int):
    """The local LLM (the heart) writes the contextual narrative + chooses which
    row leads, given the REAL context. Deliberately a small, reliable JSON ask
    so the on-device model succeeds; the data backbone is already real, so this
    only colours emphasis. Returns a refined payload or None (-> backbone stands)."""
    try:
        from core.http_pool import pooled_post
    except Exception:
        return None
    titles = [r.get('title') for r in (backbone.get('rows') or [])]
    prompt = (
        "Compose the HART OS desktop home. Real on-device context:\n"
        + json.dumps({
            'time_of_day': ctx.get('time_of_day'),
            'agents_running': ctx.get('agents_running'),
            'agents_total': ctx.get('agents_total'),
            'spark_earned': ctx.get('spark'),
            'rows': titles,
        }, ensure_ascii=False)
        + "\nReturn ONLY compact JSON: {\"eyebrow\": <label, max 5 words>, "
          "\"feature\": <one row title from rows to show first>}. "
          "No em dashes, no extra text."
    )
    try:
        resp = pooled_post('http://localhost:%d/v1/chat' % model_bus_port,
                           json={'prompt': prompt, 'max_tokens': 120},
                           timeout=12)
        if getattr(resp, 'status_code', 0) != 200:
            return None
        text = resp.json().get('response', '') or ''
    except Exception as e:
        logger.debug("home LLM curate failed: %s", e)
        return None
    data = _home_extract_json_obj(text)
    if not isinstance(data, dict):
        return None
    out = {'hero': backbone.get('hero'),
           'rows': list(backbone.get('rows') or [])}
    eyebrow = _home_clean_text(data.get('eyebrow'), 40)
    if eyebrow and out['hero']:
        out['hero'] = dict(out['hero'])
        out['hero']['eyebrow'] = eyebrow
    feat = _home_clean_text(data.get('feature'), 60).lower()
    if feat:
        for i, r in enumerate(out['rows']):
            if str(r.get('title') or '').lower() == feat and i > 0:
                out['rows'] = ([out['rows'][i]] + out['rows'][:i]
                               + out['rows'][i + 1:])
                break
    return out


def build_home_payload(backend_port: int = 6777,
                       model_bus_port: int = 6790) -> Optional[dict]:
    """Compose the agentic home {hero, rows} from live context + the local LLM.
    Deterministic backbone -> LLM curation -> strict sanitize, with the backbone
    as the fallback at every step. Returns a clean payload or None. Never raises."""
    try:
        ctx = _gather_home_context(backend_port, model_bus_port)
        backbone = _deterministic_home_payload(ctx)
        if not backbone.get('rows'):
            return None
        curated = _llm_curate_home(ctx, backbone, model_bus_port)
        clean = _sanitize_home_payload(curated) if curated else None
        if not clean:
            clean = _sanitize_home_payload(backbone)
        return clean
    except Exception as e:
        logger.debug("build_home_payload failed: %s", e)
        return None


def run_home_compose(reason: str = 'idle') -> bool:
    """Daemon entry point: compose the agentic home and push it through the
    EXISTING feed. Prefers the live in-process shell (registry) so the push
    rides compose_home -> agent_ui_update directly; falls back, cross-process
    (e.g. NixOS where the agent daemon and the shell are separate units), to the
    EXISTING /api/home/compose route which calls compose_home on the live shell.
    No new loop, no new transport. Returns True iff a push was accepted."""
    # Cheap kill-switch short-circuit so a halted hive doesn't even spend the
    # LLM call. The AUTHORITATIVE gate is inside agent_ui_update.
    try:
        from security.hive_guardrails import HiveCircuitBreaker
        if HiveCircuitBreaker.is_halted():
            return False
    except Exception:
        pass
    svc = None
    try:
        from core.platform.registry import get_registry
        svc = get_registry().get_or_none('LiquidUIService')
    except Exception:
        svc = None
    if svc is not None and hasattr(svc, 'compose_home_now'):
        try:
            return bool(svc.compose_home_now(reason=reason))
        except Exception as e:
            logger.debug("run_home_compose in-process failed: %s", e)
            return False
    # Cross-process: build here, POST to the existing compose route.
    try:
        payload = build_home_payload()
        if not payload:
            return False
        from core.http_pool import pooled_post
        port = int(os.environ.get('HART_SHELL_PORT', '6800'))
        resp = pooled_post('http://127.0.0.1:%d/api/home/compose' % port,
                           json={'payload': payload,
                                 'agent_id': 'home_composer'}, timeout=10)
        return getattr(resp, 'status_code', 0) == 200
    except Exception as e:
        logger.debug("run_home_compose cross-process failed: %s", e)
        return False
