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
import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve.liquid_ui')

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
}

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
                context['variant'] = open(variant_file).read().strip()
        except Exception:
            pass
        try:
            tier_file = os.path.join(data_dir, 'capability_tier')
            if os.path.exists(tier_file):
                context['tier'] = open(tier_file).read().strip()
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
        import requests
        try:
            resp = requests.get(
                f'http://localhost:{self.model_bus_port}/v1/models', timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                return {'available': True, 'models': data.get('models', []),
                        'count': len(data.get('models', []))}
        except Exception:
            pass
        return {'available': False, 'models': [], 'count': 0}

    def _get_agent_context(self) -> dict:
        import requests
        try:
            resp = requests.get(
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
        import requests
        prompt = self._build_ui_prompt(context)
        try:
            resp = requests.post(
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
        if not self.a2ui_enabled:
            return False
        comp_type = component.get('type', '')
        if comp_type not in COMPONENT_TYPES:
            logger.warning("Invalid A2UI component type: %s", comp_type)
            return False
        with self._lock:
            if agent_id not in self._agent_components:
                self._agent_components[agent_id] = []
            self._agent_components[agent_id].append(component)
            if len(self._agent_components[agent_id]) > 5:
                self._agent_components[agent_id] = \
                    self._agent_components[agent_id][-5:]
        logger.info("A2UI: agent %s pushed %s component", agent_id, comp_type)
        return True

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

    # ─── Voice I/O — preserved ────────────────────────────────

    def handle_voice_input(self, audio_path: str) -> dict:
        if not self.voice_enabled:
            return {'error': 'Voice not enabled'}
        import requests
        try:
            with open(audio_path, 'rb') as f:
                resp = requests.post(
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
        import requests
        try:
            resp = requests.post(
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
            css_vars = ':root { --hart-background: #0F0E17; --hart-accent: #6C63FF; --hart-active: #00e676; --hart-text: #e0e0e0; --hart-glass-bg: rgba(15,14,23,0.65); --hart-glass-border: rgba(108,99,255,0.15); --hart-muted: #78909c; --hart-surface: #1a1a2e; --hart-blur: 20px; --hart-saturation: 180%; --hart-radius: 16px; --hart-panel-opacity: 0.65; --hart-topbar-height: 40px; --hart-icon-size: 20px; --hart-titlebar-height: 32px; --hart-font-family: "JetBrains Mono"; --hart-font-size: 13px; --hart-heading-size: 18px; --hart-font-weight: 400; --hart-heading-weight: 600; --hart-anim-speed: 200ms; --hart-error: #FF6B6B; --hart-caution: #ffab40; --hart-heading: #6C63FF; --hart-surface-hover: #252540; }'
            theme = {}

        # Performance tier detection
        perf = theme.get('performance', {})
        is_potato = perf.get('disable_blur', False)

        wallpaper = theme.get('wallpaper', {})
        wp_css = wallpaper.get('value', 'linear-gradient(135deg,#0F0E17 0%,#1a1a2e 50%,#16213e 100%)')
        if wallpaper.get('type') == 'solid':
            wp_css = wallpaper['value']

        # Import panel manifest
        try:
            from integrations.agent_engine.shell_manifest import (
                PANEL_MANIFEST, DYNAMIC_PANELS, SYSTEM_PANELS, PANEL_GROUPS)
            manifest_json = json.dumps(PANEL_MANIFEST)
            system_json = json.dumps(SYSTEM_PANELS)
            groups_json = json.dumps(PANEL_GROUPS)
        except Exception:
            manifest_json = '{}'
            system_json = '{}'
            groups_json = '[]'

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
.ds-btn:disabled,.ds-btn[disabled]{opacity:0.38;pointer-events:none}
.ds-btn .mi{font-size:18px}
.ds-btn-primary{background:var(--hart-accent);color:#fff}
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
.ds-input:focus{border-color:var(--hart-accent);box-shadow:0 0 0 2px rgba(108,99,255,0.2)}
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
'''

        return f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>HART OS</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@300;400;500;600;700&family=Fira+Code:wght@400;500;600&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/icon?family=Material+Icons+Round" rel="stylesheet">
<style>
{css_vars}
*{{margin:0;padding:0;box-sizing:border-box}}
::selection{{background:var(--hart-accent);color:#fff}}
html,body{{width:100%;height:100%;overflow:hidden;font-family:var(--hart-font-family),monospace;
  font-size:var(--hart-font-size);font-weight:var(--hart-font-weight);color:var(--hart-text)}}

/* ── Wallpaper ── */
.wallpaper{{position:fixed;inset:0;z-index:0;background:{wp_css}}}

/* ── Glass mixin (perf-aware) ── */
.glass{{background:var(--hart-glass-bg);
  {'backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation));-webkit-backdrop-filter:blur(var(--hart-blur)) saturate(var(--hart-saturation));' if not is_potato else '/* blur disabled for performance */'}
  border:1px solid var(--hart-glass-border);border-radius:var(--hart-radius)}}

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
  {'box-shadow:0 8px 32px rgba(0,0,0,0.4);' if not is_potato else 'box-shadow:0 2px 8px rgba(0,0,0,0.3);'}overflow:hidden;{'transition:box-shadow var(--hart-anim-speed)' if not is_potato else 'transition:none'}}}
.panel.focused{{{'box-shadow:0 12px 48px rgba(0,0,0,0.5);' if not is_potato else 'box-shadow:0 3px 12px rgba(0,0,0,0.4);'}z-index:999}}
.panel-titlebar{{height:var(--hart-titlebar-height);display:flex;align-items:center;padding:0 8px;
  gap:6px;cursor:grab;user-select:none;flex-shrink:0;border-bottom:1px solid var(--hart-glass-border)}}
.panel-titlebar:active{{cursor:grabbing}}
.panel-titlebar .mi{{font-size:16px;color:var(--hart-accent);flex-shrink:0}}
.panel-titlebar .title{{flex:1;font-size:12px;font-weight:500;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.panel-titlebar .ctrl{{display:flex;gap:2px}}
.panel-titlebar .ctrl span{{width:24px;height:24px;display:flex;align-items:center;justify-content:center;
  border-radius:6px;cursor:pointer;font-size:14px;transition:background var(--hart-anim-speed)}}
.panel-titlebar .ctrl span:hover{{background:rgba(255,255,255,0.1)}}
.panel-titlebar .ctrl .close:hover{{background:var(--hart-error)}}
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
  font-family:var(--hart-font-family);font-size:13px;outline:none;margin-bottom:12px}}
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

/* ── Agent Pill ── */
.agent-pill{{position:fixed;bottom:56px;right:16px;z-index:1500;display:flex;
  align-items:center;gap:8px;padding:8px 14px;cursor:pointer;
  transition:all var(--hart-anim-speed);max-width:360px}}
.agent-pill:hover{{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,0.3)}}
.agent-pill.expanded{{max-width:400px;padding:12px}}
.agent-pill .mi{{font-size:20px;color:var(--hart-accent);flex-shrink:0}}
.agent-pill input{{flex:1;background:transparent;border:none;color:var(--hart-text);
  font-family:var(--hart-font-family);font-size:13px;outline:none;min-width:0}}
.agent-pill input::placeholder{{color:var(--hart-muted)}}
.agent-response{{font-size:12px;color:var(--hart-muted);padding-top:6px;
  border-top:1px solid var(--hart-glass-border);display:none;width:100%}}
.agent-response.visible{{display:block}}

/* ── Context Menu ── */
.ctx-menu{{position:fixed;z-index:3000;min-width:180px;padding:4px;
  box-shadow:0 8px 24px rgba(0,0,0,0.5);font-size:12px}}
.ctx-menu-item{{display:flex;align-items:center;gap:8px;padding:6px 10px;
  border-radius:6px;cursor:pointer;transition:background 100ms}}
.ctx-menu-item:hover{{background:var(--hart-surface-hover,rgba(255,255,255,0.1))}}
.ctx-menu-item .mi{{font-size:16px;color:var(--hart-muted)}}
.ctx-menu-sep{{border-top:1px solid var(--hart-glass-border);margin:4px 0}}

/* ── Lock Screen ── */
.lock-screen{{position:fixed;inset:0;z-index:9999;display:none;align-items:center;
  justify-content:center;flex-direction:column;gap:16px;
  background:rgba(0,0,0,{'0.7);backdrop-filter:blur(40px)' if not is_potato else '0.9)'}}}
.lock-screen.active{{display:flex}}
.lock-clock{{font-size:64px;font-weight:300}}
.lock-date{{font-size:16px;color:var(--hart-muted)}}
.lock-input{{padding:10px 16px;border-radius:12px;border:1px solid var(--hart-glass-border);
  background:var(--hart-glass-bg);color:var(--hart-text);font-size:14px;
  font-family:var(--hart-font-family);outline:none;width:280px;text-align:center}}
.lock-status{{font-size:12px;color:var(--hart-muted)}}

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
</style>
</head>
<body>
<div class="wallpaper"></div>

<!-- Top Bar -->
<div class="top-bar glass">
  <div class="start-btn" onclick="toggleStartMenu()" title="Start Menu (Super)">
    <span class="mi material-icons-round">hexagon</span>
    <span>HART</span>
  </div>
  <div class="top-bar-center" id="agent-status"></div>
  <div class="top-bar-right">
    <div class="tray-btn" onclick="openPanel('notifications')" title="Notifications">
      <span class="mi material-icons-round">notifications</span>
      <div class="badge" id="notif-badge" style="display:none"></div>
    </div>
    <div class="tray-btn" onclick="openPanel('appearance')" title="Appearance">
      <span class="mi material-icons-round">palette</span>
    </div>
    <div class="tray-btn" onclick="openPanel('security')" title="Security">
      <span class="mi material-icons-round">shield</span>
    </div>
    <span class="clock" id="clock"></span>
  </div>
</div>

<!-- Panel Container -->
<div class="panel-container" id="panels"></div>

<!-- Agent Pill -->
<div class="agent-pill glass" id="agent-pill" onclick="focusAgent()">
  <span class="mi material-icons-round mic-btn" onclick="event.stopPropagation();toggleVoice()" title="Voice input">mic</span>
  <input id="agent-input" placeholder="Ask HART..." onkeydown="if(event.key==='Enter')askAgent()">
  <span class="mi material-icons-round" onclick="event.stopPropagation();askAgent()" style="font-size:18px;cursor:pointer;color:var(--hart-accent)">send</span>
  <div class="agent-response" id="agent-resp"></div>
</div>

<!-- Start Menu -->
<div class="start-menu glass" id="start-menu">
  <input class="start-search" id="start-search" placeholder="Search..." oninput="filterStart(this.value)">
  <div class="start-scroll" id="start-scroll"></div>
  <div class="start-footer">
    <div class="power-btn" onclick="shellAction('lock')"><span class="mi material-icons-round">lock</span>Lock</div>
    <div class="power-btn" onclick="shellAction('suspend')"><span class="mi material-icons-round">dark_mode</span>Sleep</div>
    <div class="power-btn" onclick="shellAction('restart')"><span class="mi material-icons-round">refresh</span>Restart</div>
    <div class="power-btn" onclick="shellAction('shutdown')"><span class="mi material-icons-round">power_settings_new</span>Shut Down</div>
  </div>
</div>

<!-- Lock Screen -->
<div class="lock-screen" id="lock-screen">
  <div class="lock-clock" id="lock-clock"></div>
  <div class="lock-date" id="lock-date"></div>
  <input class="lock-input" type="password" placeholder="Password" id="lock-pw"
    onkeydown="if(event.key==='Enter')unlock()">
  <div class="lock-status" id="lock-status"></div>
</div>

<!-- Taskbar (open panels as chips) -->
<div class="taskbar glass" id="taskbar"></div>

<!-- Toast Notifications -->
<div class="toast-container" id="toast-container"></div>

<!-- Context Menu -->
<div class="ctx-menu glass" id="ctx-menu" style="display:none"></div>

<script>
// ═══ Configuration ═══
const BACKEND = 'http://localhost:{self.backend_port}';
const SHELL = 'http://localhost:{self.port}';
const MANIFEST = {manifest_json};
const SYSTEM_PANELS = {system_json};
const GROUPS = {groups_json};
const NUNBA_BASE = '/app/#';

// ═══ Performance Config (auto-detected from theme) ═══
const PERF = {{
  potato: {'true' if is_potato else 'false'},
  clockMs: {perf.get('clock_interval_ms', 1000)},
  agentStatusMs: {perf.get('agent_status_interval_ms', 5000)},
  maxPanels: {perf.get('max_open_panels', 20)},
  destroyMinimized: {'true' if perf.get('destroy_minimized_iframes') else 'false'},
  lazyIframes: {'true' if perf.get('lazy_load_iframes') else 'false'},
}};

// ═══ State ═══
let panels = {{}};
let panelZ = 100;
let startOpen = false;
let focusedPanel = null;

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
    (id?'document.getElementById(\\''+id+'-val\\').textContent=this.value+\\''+unit+'\\';':'')+
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
        ' onkeydown="if(event.key===\\'Enter\\')document.getElementById(\\'ds-prompt-ok\\').click()">',
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
  if(PERF.potato) {{
    toast.style.borderLeft = '3px solid '+color;
    toast.innerHTML = '<div style="font-weight:600;margin-bottom:2px;color:'+color+'">'+title+'</div>'+
      '<div style="color:var(--hart-text)">'+message+'</div>';
  }} else {{
    toast.innerHTML = '<span class="mi material-icons-round ds-toast-icon" style="color:'+color+'">'+icon+'</span>'+
      '<div class="ds-toast-content"><div class="ds-toast-title">'+title+'</div>'+
      '<div class="ds-toast-message">'+message+'</div></div>'+
      '<div class="ds-toast-progress" style="background:'+color+'"></div>';
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
    const info = MANIFEST[id] || SYSTEM_PANELS[id] || {{}};
    const active = id===focusedPanel ? 'active' : '';
    const icon = info.icon || 'web_asset';
    const title = info.title || id;
    return '<div class="taskbar-chip glass '+active+'" onclick="bringToFront(\''+id+'\')" title="'+title+'">' +
      '<span class="mi material-icons-round">'+icon+'</span>' +
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
tickClock();

// ═══ Agent Status (top bar) ═══
function refreshAgentStatus() {{
  fetch(BACKEND+'/api/social/dashboard/agents',{{signal:AbortSignal.timeout(3000)}})
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
refreshAgentStatus();

// ═══ Start Menu ═══
function buildStartMenu() {{
  const scroll = document.getElementById('start-scroll');
  let html = '';
  GROUPS.forEach(group => {{
    const items = Object.entries(MANIFEST).filter(([_,v])=>v.group===group);
    if(!items.length) return;
    html += '<div class="start-group"><div class="start-group-label">'+group+'</div><div class="start-grid">';
    items.forEach(([id,p])=>{{
      html += '<div class="start-item" data-id="'+id+'" data-title="'+p.title+'" onclick="openPanel(\''+id+'\')">';
      html += '<span class="mi material-icons-round">'+(p.icon||'apps')+'</span>';
      html += '<span class="label">'+p.title+'</span></div>';
    }});
    html += '</div></div>';
  }});
  // System panels
  const sysItems = Object.entries(SYSTEM_PANELS);
  if(sysItems.length) {{
    html += '<div class="start-group"><div class="start-group-label">System</div><div class="start-grid">';
    sysItems.forEach(([id,p])=>{{
      html += '<div class="start-item" data-id="'+id+'" data-title="'+p.title+'" onclick="openPanel(\''+id+'\')">';
      html += '<span class="mi material-icons-round">'+(p.icon||'settings')+'</span>';
      html += '<span class="label">'+p.title+'</span></div>';
    }});
    html += '</div></div>';
  }}
  scroll.innerHTML = html;
}}
buildStartMenu();

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

// ═══ Panel Manager ═══
function openPanel(id, opts) {{
  opts = opts || {{}};
  // If panel already open, bring to front
  if(panels[id]) {{
    bringToFront(id);
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
  const def = MANIFEST[id] || SYSTEM_PANELS[id] || {{}};
  const sz = def.default_size || [700,500];
  const isSystem = !!SYSTEM_PANELS[id];

  // Position: cascade from center
  const cx = window.innerWidth/2, cy = window.innerHeight/2;
  const count = Object.keys(panels).length;
  const x = Math.max(20, cx - sz[0]/2 + count*30);
  const y = Math.max(50, cy - sz[1]/2 + count*30);

  const panel = document.createElement('div');
  panel.className = 'panel glass';
  panel.id = 'panel-'+id;
  panel.style.cssText = 'left:'+x+'px;top:'+y+'px;width:'+sz[0]+'px;height:'+sz[1]+'px;z-index:'+(++panelZ);

  const title = opts.title || def.title || id;
  const icon = def.icon || 'web_asset';

  panel.innerHTML = '<div class="panel-titlebar" onmousedown="startDrag(event,\''+id+'\')"'+
    ' ondblclick="toggleMax(\''+id+'\')">'+
    '<span class="mi material-icons-round">'+icon+'</span>'+
    '<span class="title">'+title+'</span>'+
    '<div class="ctrl">'+
    '<span title="Minimize" onclick="minimizePanel(\''+id+'\')"><span class="mi material-icons-round" style="font-size:14px">minimize</span></span>'+
    '<span title="Maximize" onclick="toggleMax(\''+id+'\')"><span class="mi material-icons-round" style="font-size:14px">crop_square</span></span>'+
    '<span class="close" title="Close" onclick="closePanel(\''+id+'\')"><span class="mi material-icons-round" style="font-size:14px">close</span></span>'+
    '</div></div>'+
    '<div class="panel-body" id="panel-body-'+id+'"></div>'+
    '<div class="panel-resize" onmousedown="startResize(event,\''+id+'\')"></div>';

  document.getElementById('panels').appendChild(panel);
  panel.addEventListener('mousedown', ()=>bringToFront(id));

  // Load content (potato: defer iframes until visible)
  const body = document.getElementById('panel-body-'+id);
  if(isSystem) {{
    loadSystemPanel(id, body);
  }} else if(def.route) {{
    if(PERF.lazyIframes) {{
      // Potato: placeholder until focused, then load iframe
      body.innerHTML = '<div class="native-content" style="display:flex;align-items:center;justify-content:center;height:100%"><span class="mi material-icons-round" style="font-size:48px;color:var(--hart-muted);cursor:pointer" onclick="loadIframe(\''+id+'\',\''+def.route+'\')">touch_app</span></div>';
      body.dataset.route = def.route;
      body.dataset.loaded = '0';
    }} else {{
      body.innerHTML = '<iframe src="'+NUNBA_BASE+def.route+'" loading="lazy"></iframe>';
    }}
  }} else {{
    body.innerHTML = '<div class="native-content">Panel: '+id+'</div>';
  }}

  panels[id] = {{el:panel, x, y, w:sz[0], h:sz[1], max:false, min:false}};
  bringToFront(id);
  updateTaskbar();
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

// Lazy iframe loader (potato mode)
function loadIframe(id, route) {{
  const body = document.getElementById('panel-body-'+id);
  if(body && body.dataset.loaded !== '1') {{
    body.innerHTML = '<iframe src="'+NUNBA_BASE+route+'" loading="lazy"></iframe>';
    body.dataset.loaded = '1';
  }}
}}

function toggleMax(id) {{
  const p = panels[id];
  if(!p) return;
  if(p.max) {{
    p.el.style.left = p.x+'px'; p.el.style.top = p.y+'px';
    p.el.style.width = p.w+'px'; p.el.style.height = p.h+'px';
    p.el.style.borderRadius = '';
    p.max = false;
  }} else {{
    p.el.style.left = '0'; p.el.style.top = '0';
    p.el.style.width = '100vw'; p.el.style.height = 'calc(100vh - var(--hart-topbar-height) - 44px)';
    p.el.style.borderRadius = '0';
    p.max = true;
  }}
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
  updateTaskbar();
}}

// ═══ Drag & Resize ═══
let dragState = null;
function startDrag(e, id) {{
  if(e.button!==0) return;
  const p = panels[id];
  if(!p||p.max) return;
  dragState = {{id, mode:'move', sx:e.clientX, sy:e.clientY, ox:p.el.offsetLeft, oy:p.el.offsetTop}};
  e.preventDefault();
}}
function startResize(e, id) {{
  if(e.button!==0) return;
  const p = panels[id];
  if(!p) return;
  dragState = {{id, mode:'resize', sx:e.clientX, sy:e.clientY, ow:p.el.offsetWidth, oh:p.el.offsetHeight}};
  e.preventDefault();
}}
document.addEventListener('mousemove', e=>{{
  if(!dragState) return;
  const dx = e.clientX - dragState.sx, dy = e.clientY - dragState.sy;
  const p = panels[dragState.id];
  if(!p) return;
  if(dragState.mode==='move') {{
    const nx = dragState.ox+dx, ny = dragState.oy+dy;
    p.el.style.left = nx+'px'; p.el.style.top = ny+'px';
    p.x = nx; p.y = ny;
  }} else {{
    const nw = Math.max(320, dragState.ow+dx), nh = Math.max(240, dragState.oh+dy);
    p.el.style.width = nw+'px'; p.el.style.height = nh+'px';
    p.w = nw; p.h = nh;
  }}
}});
document.addEventListener('mouseup', ()=>{{ dragState=null; }});

// ═══ System Panels (design system) ═══
function loadSystemPanel(id, body) {{
  const apis = (SYSTEM_PANELS[id]||{{}}).apis || [];
  // Show skeleton loader while fetching
  body.innerHTML = '<div class="native-content" id="sys-'+id+'">'+dsSkeleton('panel',3)+'</div>';
  const container = document.getElementById('sys-'+id);

  if(id==='hw_monitor') loadHardwareMonitor(container, apis);
  else if(id==='security') loadSecurityCenter(container, apis);
  else if(id==='network') loadNetworkPanel(container, apis);
  else if(id==='event_log') loadEventLog(container, apis);
  else if(id==='drivers') loadDriversPanel(container);
  else if(id==='audio') loadAudioPanel(container);
  else if(id==='bluetooth') loadBluetoothPanel(container);
  else if(id==='power') loadPowerPanel(container);
  else if(id==='display') loadDisplayPanel(container);
  else if(id==='remote_desktop') loadRemoteDesktopPanel(container, apis);
  else container.innerHTML = '<div class="ds-body-md ds-text-muted">Panel: '+id+'</div>';
}}

// Backward compat wrappers (used in old code references)
function metricBar(l,p,u,s) {{ return dsMetricBar(l,p,u,s); }}
function statusRow(i,l,v,c) {{ return dsStatusRow(i,l,v,c); }}

function loadHardwareMonitor(el, apis) {{
  Promise.all(apis.map(u=>fetch(BACKEND+u,{{signal:AbortSignal.timeout(3000)}}).then(r=>r.json()).catch(()=>({{}}))))
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
  Promise.all(apis.map(u=>fetch(BACKEND+u,{{signal:AbortSignal.timeout(3000)}}).then(r=>r.json()).catch(()=>({{}}))))
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
      body:JSON.stringify(body), signal:AbortSignal.timeout(35000)
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
      body:'{{}}', signal:AbortSignal.timeout(15000)
    }}).then(r=>r.json()).then(d=>{{
      if(d.success) {{ showToast('WiFi', 'Disconnected', 'info'); loadNetworkPanel(document.getElementById('sys-network'),
        (SYSTEM_PANELS['network']||{{}}).apis||[]); }}
      else dsAlert('Error', d.error||'Disconnect failed', 'error');
    }}).catch(e=>dsAlert('Error', e.message, 'error'));
  }});
}}

function loadNetworkPanel(el, apis) {{
  Promise.all([
    ...apis.map(u=>fetch(BACKEND+u,{{signal:AbortSignal.timeout(3000)}}).then(r=>r.json()).catch(()=>({{}}))),
    fetch(SHELL+'/api/shell/network/wifi',{{signal:AbortSignal.timeout(3000)}}).then(r=>r.json()).catch(()=>({{}})),
    fetch(SHELL+'/api/shell/network/status',{{signal:AbortSignal.timeout(3000)}}).then(r=>r.json()).catch(()=>({{}}))
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
            '<div class="ds-list-item ds-list-item-interactive" onclick="wifiConnect(\\''+n.ssid.replace(/'/g,"\\\\'")+'\\')">' +
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

function loadEventLog(el) {{
  fetch(SHELL+'/api/shell/events',{{signal:AbortSignal.timeout(3000)}})
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
  fetch(SHELL+'/api/shell/drivers',{{signal:AbortSignal.timeout(5000)}})
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
  fetch(SHELL+'/api/shell/audio',{{signal:AbortSignal.timeout(5000)}})
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
  fetch(SHELL+'/api/shell/bluetooth',{{signal:AbortSignal.timeout(5000)}})
    .then(r=>r.json()).then(data=>{{
      const devs = data.devices||[];
      el.innerHTML = '<div class="ds-panel-grid ds-fade-in"><div class="ds-panel-title">Bluetooth</div>'+
        (devs.length===0?'<div class="ds-body-md ds-text-muted ds-flex ds-flex-center" style="height:80px"><span class="mi material-icons-round" style="margin-right:8px;font-size:32px;opacity:0.3">bluetooth_disabled</span>No Bluetooth devices found</div>':
        '<div class="ds-stagger">'+devs.map(d=>dsStatusRow('bluetooth',d.name,d.mac,'var(--hart-accent)')).join('')+'</div>')+
        '</div>';
    }}).catch(()=>{{ el.innerHTML='<div class="ds-body-md ds-text-muted">Bluetooth unavailable</div>'; }});
}}

function loadPowerPanel(el) {{
  fetch(SHELL+'/api/shell/power',{{signal:AbortSignal.timeout(5000)}})
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
  fetch(SHELL+'/api/shell/display',{{signal:AbortSignal.timeout(5000)}})
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
  Promise.all(apis.map(u=>fetch(BACKEND+u,{{signal:AbortSignal.timeout(5000)}}).then(r=>r.json()).catch(()=>({{}}))))
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
      html += '<div class="ds-card ds-card-elevated ds-card-interactive" onclick="navigator.clipboard.writeText(\\''+deviceId+'\\').then(()=>{{this.querySelector(\\'.copy-hint\\').textContent=\\'Copied!\\';setTimeout(()=>this.querySelector(\\'.copy-hint\\').textContent=\\'Click to copy\\',2000)}})" title="Click to copy">';
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
function focusAgent() {{
  document.getElementById('agent-input').focus();
  document.getElementById('agent-pill').classList.add('expanded');
}}
function askAgent() {{
  const input = document.getElementById('agent-input');
  const text = input.value.trim();
  if(!text) return;
  input.value = '';
  const resp = document.getElementById('agent-resp');
  resp.textContent = 'Thinking...';
  resp.classList.add('visible');

  // Check for theme commands first
  const lower = text.toLowerCase();
  if(lower.includes('theme')||lower.includes('font')||lower.includes('bigger')||
     lower.includes('smaller')||lower.includes('dark')||lower.includes('light')) {{
    handleThemeCommand(lower, resp);
    return;
  }}
  // Check for panel open commands
  if(lower.startsWith('open ')) {{
    const target = lower.replace('open ','').trim();
    const match = Object.entries(MANIFEST).find(([k,v])=>
      v.title.toLowerCase().includes(target)||k.includes(target));
    if(match) {{ openPanel(match[0]); resp.textContent='Opened '+match[1].title; return; }}
  }}

  fetch(SHELL+'/api/agent/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{text}})}})
    .then(r=>r.json()).then(data=>{{
      const txt = data.response || data.error || 'No response';
      resp.textContent = txt;
      speakText(txt);
    }}).catch(()=>{{ resp.textContent='Could not reach agent'; }});
}}

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

  fetch(BACKEND+'/api/social/theme/customize',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},body:JSON.stringify(customization)}})
    .then(r=>r.json()).then(()=>{{
      resp.textContent='Done! Refreshing...';
      setTimeout(()=>location.reload(), 500);
    }}).catch(()=>{{ resp.textContent='Failed to customize'; }});
}}

function applyPreset(id, resp) {{
  fetch(BACKEND+'/api/social/theme/apply',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{theme_id:id}})}})
    .then(r=>r.json()).then(()=>{{
      resp.textContent='Applied '+id+'! Refreshing...';
      setTimeout(()=>location.reload(), 500);
    }}).catch(()=>{{ resp.textContent='Failed to apply theme'; }});
}}

// ═══ Context Menu ═══
document.addEventListener('contextmenu', e => {{
  e.preventDefault();
  const menu = document.getElementById('ctx-menu');
  // Desktop right-click
  if(e.target.classList.contains('wallpaper')||e.target===document.body) {{
    menu.innerHTML = [
      ctxItem('palette','Appearance','openPanel("appearance")'),
      ctxItem('wallpaper','Wallpaper','openPanel("appearance")'),
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

function ctxItem(icon,label,action) {{
  return '<div class="ctx-menu-item" onclick="'+action+';document.getElementById(\'ctx-menu\').style.display=\'none\'">'+
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
    const ids = Object.keys(panels);
    if(ids.length<2) return;
    const idx = (ids.indexOf(focusedPanel)+1)%ids.length;
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
  const labels = {{suspend:'put the system to sleep',restart:'restart the system',shutdown:'shut down the system'}};
  dsConfirm(action.charAt(0).toUpperCase()+action.slice(1),
    'Are you sure you want to '+(labels[action]||action)+'?',
    {{okLabel:action.charAt(0).toUpperCase()+action.slice(1), danger:action==='shutdown'}}).then(function(ok){{
    if(ok) fetch(SHELL+'/api/shell/session/'+action,{{method:'POST'}}).catch(()=>{{}});
  }});
}}
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

function toggleVoice() {{
  if(isRecording) {{ stopRecording(); return; }}
  startRecording();
}}

async function startRecording() {{
  try {{
    const stream = await navigator.mediaDevices.getUserMedia({{audio:true}});
    const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '';
    mediaRecorder = mimeType ? new MediaRecorder(stream,{{mimeType}}) : new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = function(e) {{ audioChunks.push(e.data); }};
    mediaRecorder.onstop = async function() {{
      stream.getTracks().forEach(function(t){{t.stop();}});
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
          document.getElementById('agent-input').value = data.text;
          askAgent();
        }} else if(data.error) {{
          resp.textContent = data.error;
        }}
      }} catch(err) {{ resp.textContent = 'Voice processing failed'; }}
    }};
    mediaRecorder.start();
    isRecording = true;
    document.querySelector('.mic-btn').classList.add('recording');
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
}}

// TTS helper
function speakText(text) {{
  if(!text || PERF.potato || !('speechSynthesis' in window)) return;
  const utt = new SpeechSynthesisUtterance(text);
  utt.rate = 1.0; utt.pitch = 1.0;
  speechSynthesis.speak(utt);
}}

// ═══ SSE Notification Stream ═══
if(!PERF.potato) {{
  try {{
    const evtSrc = new EventSource(SHELL+'/api/notifications/stream');
    evtSrc.onmessage = function(e) {{
      try {{
        const notifs = JSON.parse(e.data);
        notifs.forEach(function(n){{ showToast(n.title||n.agent||'Notification', n.message||'', n.severity||'info'); }});
      }} catch(err) {{}}
    }};
    evtSrc.onerror = function() {{ /* SSE reconnects automatically */ }};
  }} catch(err) {{}}
}}

// ═══ Recent Files in Start Menu ═══
(function loadRecentFiles() {{
  fetch(SHELL+'/api/shell/files/recent',{{signal:AbortSignal.timeout(3000)}})
    .then(function(r){{return r.json();}}).then(function(data) {{
      const files = data.files || [];
      if(files.length === 0) return;
      const scroll = document.getElementById('start-scroll');
      if(!scroll) return;
      const section = document.createElement('div');
      section.className = 'start-group';
      section.innerHTML = '<div class="start-group-label">Recent Files</div><div class="start-grid">' +
        files.slice(0,8).map(function(f) {{
          return '<div class="start-item" onclick="launchApp(\'xdg-open\')">' +
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
    fetch(BACKEND+'/api/social/dashboard/agents',{{signal:AbortSignal.timeout(3000)}}).then(function(r){{return r.json();}}).catch(function(){{return {{}}; }}),
    fetch(BACKEND+'/api/social/dashboard/health',{{signal:AbortSignal.timeout(3000)}}).then(function(r){{return r.json();}}).catch(function(){{return {{}}; }}),
  ]).then(function([agents,health]) {{
    const agentCount = (agents.agents||[]).filter(function(a){{return a.status==='running';}}).length;
    const peerCount = health.peer_count || 0;
    const hour = new Date().getHours();
    const greeting = hour<12?'Good morning':hour<17?'Good afternoon':'Good evening';
    const msg = greeting+'! '+agentCount+' agent'+(agentCount!==1?'s':'')+' running, '+peerCount+' peer'+(peerCount!==1?'s':'')+' connected.';
    showToast('HART', msg, 'info');
    setTimeout(function(){{ speakText(msg); }}, 1000);
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
        from flask import Flask, request, jsonify, Response, send_from_directory

        app = Flask(__name__)

        # ── Desktop Shell (the root page IS the OS) ──
        @app.route('/')
        def index():
            return Response(self.render_desktop_shell(), mimetype='text/html')

        # ── Nunba SPA embedding (React pages inside panel iframes) ──
        nunba_dir = os.environ.get('NUNBA_STATIC_DIR', '')
        if nunba_dir and os.path.isdir(nunba_dir):
            @app.route('/app/<path:path>')
            def nunba_static(path):
                return send_from_directory(nunba_dir, path)

            @app.route('/app/')
            def nunba_index():
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

        @app.route('/api/approval', methods=['POST'])
        def api_approval():
            data = request.get_json(force=True)
            result = self.agent_request_approval(
                data.get('agent_id', 'unknown'),
                data.get('action', 'unknown'),
                data.get('description', ''))
            return jsonify(result)

        # ── Voice ──
        @app.route('/api/voice', methods=['POST'])
        def api_voice():
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
            # Called by ThemeService when theme changes
            return jsonify({'status': 'updated'})

        # ── Agent ambient input (text from agent pill) ──
        @app.route('/api/agent/ask', methods=['POST'])
        def agent_ask():
            data = request.get_json(force=True, silent=True) or {}
            text = data.get('text', '').strip()
            if not text:
                return jsonify({'error': 'No text provided'})
            import requests as req
            try:
                resp = req.post(
                    f'http://localhost:{self.backend_port}/chat',
                    json={
                        'user_id': 'hart_desktop_user',
                        'prompt_id': 'desktop_agent',
                        'prompt': text,
                    }, timeout=30)
                return jsonify(resp.json())
            except Exception as e:
                return jsonify({'error': str(e)})

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
            import re
            if action not in ('lock', 'logout', 'suspend', 'shutdown', 'restart'):
                return jsonify({'error': 'Invalid action'}), 400
            cmds = {
                'lock': ['loginctl', 'lock-session'],
                'logout': ['loginctl', 'terminate-session', ''],
                'suspend': ['systemctl', 'suspend'],
                'shutdown': ['systemctl', 'poweroff'],
                'restart': ['systemctl', 'reboot'],
            }
            try:
                subprocess.Popen(
                    cmds[action],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return jsonify({'status': action})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

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

        # ── Shell APIs: WiFi ──
        @app.route('/api/shell/network/wifi', methods=['GET'])
        def shell_wifi():
            networks = []
            connected = {}
            try:
                r = subprocess.run(
                    ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,ACTIVE',
                     'device', 'wifi', 'list'],
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().split('\n'):
                    parts = line.split(':')
                    if len(parts) >= 4 and parts[0]:
                        net = {
                            'ssid': parts[0],
                            'signal': int(parts[1] or 0),
                            'security': parts[2],
                            'active': parts[3] == 'yes',
                        }
                        networks.append(net)
                        if net['active']:
                            connected = net
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ['hostname', '-I'],
                    capture_output=True, text=True, timeout=3)
                if r.stdout.strip():
                    connected['ip'] = r.stdout.strip().split()[0]
            except Exception:
                pass
            return jsonify({'networks': networks[:20], 'connected': connected})

        @app.route('/api/shell/network/wifi/connect', methods=['POST'])
        def shell_wifi_connect():
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

        @app.route('/api/shell/network/wifi/disconnect', methods=['POST'])
        def shell_wifi_disconnect():
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
            volume = max(0, min(150, int(volume)))
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

        @app.route('/api/shell/display/scale', methods=['POST'])
        def shell_display_scale():
            data = request.get_json(silent=True) or {}
            output = data.get('output', '')
            scale = data.get('scale')
            if not output or scale is None:
                return jsonify({'success': False, 'error': 'output and scale required'}), 400
            scale = max(0.5, min(3.0, float(scale)))
            try:
                # xrandr scale is inverse: scale 2.0 means 0.5x transform
                transform = str(round(1.0 / scale, 4))
                r = subprocess.run(
                    ['xrandr', '--output', output, '--scale', f'{transform}x{transform}'],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return jsonify({'success': True, 'scale': scale})
                return jsonify({'success': False, 'error': r.stderr.strip()}), 400
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        # ── Shell APIs: System Metrics ──
        @app.route('/api/shell/system/metrics', methods=['GET'])
        def shell_system_metrics():
            metrics = {}
            try:
                import psutil
                metrics['cpu_percent'] = psutil.cpu_percent(interval=0.5)
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
                from integrations.service_tools.vram_manager import VRAMManager
                gpu = VRAMManager.detect_gpu()
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
                for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
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

        # ── Notification SSE Stream ──
        @app.route('/api/notifications/stream', methods=['GET'])
        def notification_stream():
            import time as _time

            def generate():
                last_check = _time.time()
                while True:
                    _time.sleep(5)
                    notifs = []
                    for agent_id, comps in list(
                            self._agent_components.items()):
                        for c in comps:
                            ts = c.get('_ts', 0)
                            if (c.get('type') == 'notification'
                                    and ts > last_check):
                                notifs.append({
                                    'agent': agent_id,
                                    'title': c.get('title', ''),
                                    'message': c.get('message', ''),
                                    'severity': c.get('severity', 'info'),
                                })
                    last_check = _time.time()
                    if notifs:
                        yield f"data: {json.dumps(notifs)}\n\n"
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

        return app

    # ─── Serve ────────────────────────────────────────────────

    def serve_forever(self):
        """Start the glass desktop shell service."""
        self._running = True

        def _model_check_loop():
            import requests
            while self._running:
                try:
                    resp = requests.get(
                        f'http://localhost:{self.model_bus_port}/v1/status',
                        timeout=3)
                    self._model_available = (
                        resp.status_code == 200 and
                        resp.json().get('backend_count', 0) > 0)
                except Exception:
                    self._model_available = False
                time.sleep(10)

        threading.Thread(target=_model_check_loop, daemon=True).start()

        app = self._create_flask_app()
        logger.info("LiquidUI Glass Shell starting on port %d", self.port)

        # Auto-scale threads by hardware tier
        try:
            from security.system_requirements import get_tier_name
            tier = get_tier_name()
        except Exception:
            tier = 'standard'
        threads = 1 if tier in ('embedded', 'observer') else 2 if tier == 'lite' else 4

        try:
            from waitress import serve
            serve(app, host='0.0.0.0', port=self.port, threads=threads)
        except ImportError:
            app.run(host='0.0.0.0', port=self.port, threaded=True)
