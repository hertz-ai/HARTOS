"""HART OS pillar proofs — falsifiable probes that each load-bearing pillar HOLDS.

This is the "claim-validation ledger" as executable tests: one (or more) probe per
OS pillar, each importing the REAL code and asserting an observable truth that
must hold IFF the pillar bears load.  A pillar that's "erected" but cracked fails
its probe.  Runtime-only proofs (robot hardware, multi-node hive, booted ISO) are
marked and assert the strongest CONTRACT provable off-target.

Pillars:
  P1 generated/liquid UI (A2UI)        P5 learns-you / you-own-it
  P2 ambient co-presence (sees you)    P6 one fabric (cross-OS apps)
  P3 humans hold the wheel             P7 embodiment-native
  P4 composable (proven: test_shell_route_no_collision)
                                       P8 hive-native
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ════════════════════════════════════════════════════════════════════
# P3 — Humans hold the wheel: the audit trail is the backbone.
# It must round-trip-verify (regression for #48) AND be tamper-evident.
# ════════════════════════════════════════════════════════════════════

def _fake_social_models():
    """Fake integrations.social.models that reproduces the REAL #48 condition:
    AuditLogEntry.created_at defaults to a FRESH datetime.utcnow() when not
    explicitly provided — exactly the model's `Column(DateTime, default=
    datetime.utcnow)` firing at flush.  If the fix didn't pass created_at, the
    stored value differs from the hashed one and verify_chain breaks."""
    store: list = []

    class _Col:           # class-level column stand-in so AuditLogEntry.id.desc() works
        def desc(self): return self
        def asc(self): return self

    class FakeEntry:
        id = _Col()
        def __init__(self, **kw):
            store.append(self)
            self.id = len(store)
            for k, v in kw.items():
                setattr(self, k, v)
            if not hasattr(self, 'created_at') or self.created_at is None:
                self.created_at = datetime.utcnow()  # the model default, at flush

    class _Q:
        def __init__(self, rows): self._rows = rows
        def order_by(self, *a, **k): return self
        def first(self): return self._rows[-1] if self._rows else None
        def limit(self, n): return self
        def all(self): return list(self._rows)

    class FakeDB:
        def query(self, model): return _Q(store)
        def add(self, e): pass          # FakeEntry self-appends
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    m = types.ModuleType('integrations.social.models')
    m.AuditLogEntry = FakeEntry
    m.get_db = lambda: FakeDB()
    m._store = store
    return m


def _audit_db_mode(monkeypatch):
    monkeypatch.setitem(sys.modules, 'integrations.social.models', _fake_social_models())
    from security.immutable_audit_log import ImmutableAuditLog
    a = ImmutableAuditLog()
    a._use_db = True  # force the DB path — where #48 lived
    return a


def test_p3_audit_chain_roundtrips_through_db(monkeypatch):
    a = _audit_db_mode(monkeypatch)
    a.log_event('state_change', actor_id='user_1', action='completed task 5')
    a.log_event('tool_call', actor_id='agent_2', action='ran get_weather',
                detail={'city': 'Pune', 'api_key': 'sk-secret'})
    ok, reason = a.verify_chain()
    assert ok, f"P3: audit chain must verify clean on DB round-trip — got: {reason}"


def test_p3_audit_is_tamper_evident(monkeypatch):
    a = _audit_db_mode(monkeypatch)
    a.log_event('auth', actor_id='user_1', action='login')
    a.log_event('security', actor_id='user_1', action='granted admin')
    sys.modules['integrations.social.models']._store[0].action = 'login as root'
    ok, reason = a.verify_chain()
    assert not ok, "P3: tampering with an entry MUST be detected"
    assert 'broken' in reason.lower()


def test_p3_audit_redacts_secrets(monkeypatch):
    """Humans-in-control also means secrets never land in the trail."""
    a = _audit_db_mode(monkeypatch)
    a.log_event('tool_call', actor_id='a', action='x',
                detail={'api_key': 'sk-live-123', 'city': 'Pune'})
    store = sys.modules['integrations.social.models']._store
    assert 'sk-live-123' not in (store[-1].detail_json or '')
    assert 'Pune' in store[-1].detail_json


# ════════════════════════════════════════════════════════════════════
# P1 — Generated/liquid UI: an agent can push a live UI component (A2UI)
# that is validated, stamped, and stored for delivery to the frontend.
# ════════════════════════════════════════════════════════════════════

def _bare_liquid_ui(data_dir=None):
    """A LiquidUIService carrying ONLY the state the A2UI transport + the runtime
    component registry touch — no server boot, no sockets, no threads.  Sets
    EXACTLY what __init__ sets for these paths (a2ui flag, component store, the
    plain Lock, the per-agent rate bucket, the runtime-registered component map,
    the on-disk data dir), so the probe drives the REAL governance — the ONE
    allowlist gate, the human kill-switch, the token bucket, the XSS reject, and
    the persisted registry — not a stub of it."""
    import threading, tempfile
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    svc = LiquidUIService.__new__(LiquidUIService)   # no server boot
    svc.a2ui_enabled = True
    svc._agent_components = {}
    svc._lock = threading.Lock()             # plain Lock — mirrors production (line 772)
    svc._a2ui_buckets = {}                    # per-agent token bucket (_a2ui_rate_ok)
    svc._custom_component_types = {}          # runtime-registered component specs
    svc._data_dir = data_dir or tempfile.mkdtemp(prefix='hart_p1_')
    return svc


def test_p1_agent_can_push_valid_ui_component():
    svc = _bare_liquid_ui()
    ok = svc.agent_ui_update('agent_1', {'type': 'card', 'title': 'Done', 'content': 'task 5'})
    assert ok is True, "P1: a valid A2UI component must be accepted"
    stored = svc._agent_components.get('agent_1', [])
    assert stored and stored[-1]['type'] == 'card'
    assert stored[-1]['_agent_id'] == 'agent_1', "component must be stamped with the agent"


def test_p1_invalid_component_type_is_rejected():
    svc = _bare_liquid_ui()
    assert svc.agent_ui_update('a', {'type': 'definitely_not_a_type'}) is False


def test_p1_a2ui_respects_the_off_switch():
    """Cross-pillar with P3 (full control): the generative UI can be turned OFF."""
    svc = _bare_liquid_ui()
    svc.a2ui_enabled = False
    assert svc.agent_ui_update('a', {'type': 'card'}) is False


# ── P1, load-bearing: the AGENTIC framework, not just one card ──────────────
# The weak probe above proves the transport accepts a single builtin card.  The
# real P1 claim (the steward's: "agents shd be able to extend the framework at
# runtime … bake new UI on the fly … agent readable specs for each … all handles
# controlled by the local intelligence") needs four more things to bear load:
#   (a) an agent REGISTERS a brand-new component type at runtime, and the ONE
#       allowlist gate immediately accepts a push of it (runtime-extensible, not
#       forked);   (b) every component exposes an AGENT-READABLE spec so the local
#       intelligence composes from the spec alone;   (c) an agent RECOMPOSES the
#       whole desktop into a different design (Aura) at runtime through the SAME
#       governed transport;   (d) all of it obeys the human kill-switch + XSS gate.

def test_p1_agent_registers_new_component_type_at_runtime_and_can_push_it(tmp_path):
    """(a) The runtime-extensible framework: a HART agent invents a component the
    OS never shipped, and the SAME transport that gates builtins accepts it — one
    allowlist, extended at runtime, no second gate."""
    svc = _bare_liquid_ui(data_dir=str(tmp_path))
    # Before registration the type is unknown → the ONE gate rejects it.
    assert svc.agent_ui_update('composer', {'type': 'aura_ring'}) is False
    res = svc.register_component_type('composer', 'aura_ring', {
        'props': ['radius', 'hue', 'pulse'],
        'events': ['tap'],
        'behaviors': ['breathe'],
    })
    assert res.get('status') == 'registered' and res.get('type') == 'aura_ring'
    # Now the very SAME gate accepts a push of the new type — proving the
    # allowlist was extended at runtime, not that a parallel path was opened.
    assert svc.agent_ui_update('composer',
                               {'type': 'aura_ring', 'radius': 80, 'hue': 280}) is True
    stored = svc._agent_components.get('composer', [])
    assert stored and stored[-1]['type'] == 'aura_ring'
    # And it persisted to the ONE custom-types map the gate reads (one writer).
    assert 'aura_ring' in svc._custom_component_types


def test_p1_builtin_component_type_is_protected_from_override(tmp_path):
    """(a′) Runtime extension cannot overwrite a shipped builtin — the starter
    component set (like the starter skins) is protected."""
    svc = _bare_liquid_ui(data_dir=str(tmp_path))
    res = svc.register_component_type('rogue', 'card', {'props': ['x']})
    assert 'error' in res and 'builtin' in res['error'].lower()


def test_p1_every_component_exposes_an_agent_readable_spec():
    """(b) Each component (builtin + registered) publishes a machine spec — name +
    attribute schema + how to mount/compose it — so the local HART intelligence can
    drive ANY component from the spec alone (the "agent readable specs for each"
    requirement).  A synthesized builtin and the hand-enriched `metric` both hold."""
    svc = _bare_liquid_ui()
    specs = svc.list_component_specs()
    by_name = {s.get('name'): s for s in specs if isinstance(s, dict)}
    # A plain builtin's spec is SYNTHESIZED from its props — still complete.
    card = by_name.get('card')
    assert card and 'title' in card['attributes'] and card['mount'] == 'a2ui'
    assert 'agent_ui_update' in card['compose'], "spec must tell an agent how to compose it"
    # The enriched builtin declares its own events/behaviours verbatim.
    metric = svc.get_component_spec('metric')
    assert metric and 'click' in metric['emits'] and 'live_update' in metric['behaviors']
    assert svc.get_component_spec('no_such_component') is None


def test_p1_agent_recomposes_whole_desktop_into_a_new_design_at_runtime(tmp_path):
    """(c) THE liquid-UI proof: an agent recomposes the ENTIRE home surface —
    hero + rows + ambient mood/palette (Aura) — at runtime, through the SAME
    governed A2UI channel every other push uses.  This is "the user can have an
    entirely new design while all components still work", driven by an agent."""
    svc = _bare_liquid_ui(data_dir=str(tmp_path))
    ok = svc.compose_home(
        hero={'title': 'Good evening', 'subtitle': 'Aura'},
        rows=[{'title': 'Continue', 'accent': 'violet', 'cards': []}],
        agent_id='home_composer',
        mood='aura',
    )
    assert ok is True, "P1: an agent must be able to recompose the home at runtime"
    stored = svc._agent_components.get('home_composer', [])
    assert stored, "the composed home must flow through the ONE A2UI store"
    comp = stored[-1]
    assert comp['type'] == 'home_compose'
    assert comp.get('mood') == 'aura', "the new design's ambient palette must ride the same push"
    assert comp['hero']['subtitle'] == 'Aura' and comp['rows'][0]['accent'] == 'violet'
    # Empty composition is a no-op (nothing to paint).
    assert svc.compose_home() is False


def test_p1_runtime_registration_obeys_kill_switch_and_xss_gate(tmp_path):
    """(d) Extending the framework is governed exactly like painting the screen:
    the human off-switch stops it, and an XSS-bearing spec is refused — the same
    constitutional controls agent_ui_update enforces (P3 crosses P1)."""
    svc = _bare_liquid_ui(data_dir=str(tmp_path))
    svc.a2ui_enabled = False
    assert 'error' in svc.register_component_type('a', 'widget_x', {'props': ['q']})
    svc.a2ui_enabled = True
    bad = svc.register_component_type('a', 'evil_x',
                                      {'props': ['q'], 'template': '<script>steal()</script>'})
    assert 'error' in bad, "a spec carrying an XSS vector must be rejected"
    assert 'evil_x' not in svc._custom_component_types


# ════════════════════════════════════════════════════════════════════
# P5 — Learns you, you own it: the per-user model persists LOCALLY and
# round-trips (the data is a file on YOUR disk, not a cloud account).
# ════════════════════════════════════════════════════════════════════

def test_p5_resonance_profile_is_local_first_and_roundtrips(tmp_path):
    from core.resonance_profile import (
        UserResonanceProfile, save_resonance_profile, load_resonance_profile)
    p = UserResonanceProfile(user_id='u1')
    dim = list(p.tuning.keys())[0]
    p.set_tuning(dim, 0.83)
    save_resonance_profile(p, base_dir=str(tmp_path))
    assert (tmp_path / 'u1_resonance.json').exists(), "P5: profile must persist locally"
    loaded = load_resonance_profile('u1', base_dir=str(tmp_path))
    assert loaded is not None and loaded.user_id == 'u1'
    assert abs(loaded.get_tuning(dim) - 0.83) < 1e-6, "learned tuning must round-trip"


def test_p5_get_or_create_returns_fresh_for_new_user(tmp_path):
    from core.resonance_profile import get_or_create_profile
    p = get_or_create_profile('brand_new', base_dir=str(tmp_path))
    assert p is not None and p.user_id == 'brand_new'


# ════════════════════════════════════════════════════════════════════
# P6 — One fabric: a SINGLE installer interface spans Windows + Linux +
# Android ecosystems (the "runs everything" claim).  detect_platform is
# pure logic and fully provable here; ACTUALLY installing/running a
# Win/Android app is runtime-gated on the booted subsystems (ISO).
# ════════════════════════════════════════════════════════════════════

def test_p6_unified_installer_spans_os_ecosystems():
    from integrations.agent_engine.app_installer import detect_platform, InstallerPlatform
    assert detect_platform('game.apk') == InstallerPlatform.ANDROID
    assert detect_platform('tool.appimage') == InstallerPlatform.APPIMAGE
    assert detect_platform('app.flatpakref') == InstallerPlatform.FLATPAK
    names = {m.name for m in InstallerPlatform}
    assert {'ANDROID', 'FLATPAK', 'APPIMAGE'}.issubset(names), f"missing ecosystems: {names}"
    assert any(n in names for n in ('WINDOWS', 'WINE', 'EXE', 'MSI')), \
        f"P6: no Windows installer platform — 'runs Windows apps' unproven ({sorted(names)})"
    # RUNTIME-GATED: the real install (flatpak/wine/apk) needs the booted subsystems.


# ════════════════════════════════════════════════════════════════════
# P2 — Sees you: the perceive surfaces (voice + camera) are WIRED as
# routes.  Actual STT transcription + camera capture + VLM→action is
# runtime-gated (mic/camera/model).
# ════════════════════════════════════════════════════════════════════

def test_p2_perceive_surfaces_are_wired():
    try:
        from flask import Flask
        from integrations.agent_engine.shell_desktop_apis import register_shell_desktop_routes
        from integrations.agent_engine.shell_system_apis import register_shell_system_routes
    except Exception as e:
        pytest.skip(f"shell route modules unavailable: {e}")
    voice_app = Flask('p2_voice'); register_shell_desktop_routes(voice_app)
    cam_app = Flask('p2_cam'); register_shell_system_routes(cam_app)
    voice_rules = {r.rule for r in voice_app.url_map.iter_rules()}
    cam_rules = {r.rule for r in cam_app.url_map.iter_rules()}
    assert any('voice' in r for r in voice_rules), \
        f"P2: no voice perceive route ({sorted(r for r in voice_rules if 'shell' in r)[:8]})"
    assert any(('webcam' in r) or ('camera' in r) for r in cam_rules), "P2: no camera perceive route"
    # RUNTIME-GATED: transcription + capture + VLM action execution need real I/O.


# ════════════════════════════════════════════════════════════════════
# P7 — Embodiment scaffolding: the embodied-learning skill relay exists
# in HARTOS (in/out of the local world model).  The real perceive→act ML
# lives in HevolveAI and needs a body — runtime-gated.
# ════════════════════════════════════════════════════════════════════

def test_p7_embodiment_skill_relay_present():
    from integrations.agent_engine.world_model_bridge import WorldModelBridge
    assert hasattr(WorldModelBridge, 'ingest_skill_packet'), "P7: no skill ingest"
    assert hasattr(WorldModelBridge, 'distribute_skill_packet'), "P7: no skill distribute"
    # CONTRACT only: the perceive→act loop on real hardware runs in HevolveAI.


# ════════════════════════════════════════════════════════════════════
# P8 — Hive: a node can EXPORT its learning delta (the data primitive).
# VERDICT: primitives exist (export_resonance_delta / ingest_skill_packet
# / import_hive_resonance) BUT the cross-PROCESS transport (Direction B:
# inbound WAMP skill → HARTOS gossip) is BROKEN — #66.  So learning does
# NOT yet propagate between separate machines.  P8 = primitives ✅, wire ❌.
# ════════════════════════════════════════════════════════════════════

def test_p8_node_can_export_learning_delta(tmp_path):
    try:
        from core.resonance_tuner import get_resonance_tuner
        tuner = get_resonance_tuner()
    except Exception as e:
        pytest.skip(f"resonance tuner unavailable: {e}")
    delta = tuner.export_resonance_delta(base_dir=str(tmp_path))
    assert isinstance(delta, dict), "P8: a node must be able to export its learning delta"
    # KNOWN GAP (#66): the cross-process relay that would carry this to peers is dead.
