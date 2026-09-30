"""Liquid UI on the desktop is the Demopage agent component: ONE LiquidUIService
lives in the backend process, with no Flask app and no routes.

Owner ruling 2026-09-26: "demopage agent compoennt shd be part and parcel of
liqui i\\ui".  Before this, only ``_create_flask_app`` (the separate :6800 shell)
registered 'LiquidUIService', so in the Nunba / backend process every A2UI
emitter found nothing and the push was dropped.

Topology contract these tests pin (one owner per topology):
  * backend process, no separate shell (Nunba desktop, a bare HARTOS backend):
    bootstrap_platform registers ONE lazy LiquidUIService; its agent_ui_update
    emits ``agent.ui.update`` on the EventBus, stamped with the owning user_id,
    which the EventBus fans out to that user's SSE stream (the Demopage).
  * HART OS (os mode): the hart-liquid-ui unit is a separate :6800 process and
    owns the service.  The backend registers nothing, so run_home_compose keeps
    POSTing the live shell exactly as before.
  * a process that SERVES the shell: the serving instance is the owner and
    displaces a headless one bootstrap made; a second serving instance never
    displaces the first.

Behavioural: real ServiceRegistry, real bootstrap_platform, real EventBus, real
LiquidUIService, a real emitter (AppInstaller._push_desktop_icon).  Mocked only
at the boundaries: the SSE transport, the audit DB, the hive breaker, the owner
lookup, the OS-mode probe and HTTP.
"""
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from core.platform.registry import get_registry, reset_registry


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv('HEVOLVE_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('CBURL', raising=False)
    monkeypatch.delenv('WAMP_URL', raising=False)
    reset_registry()
    yield
    reset_registry()


def _bootstrap(tmp_path, os_mode: bool):
    """Run the REAL bootstrap_platform with only the host-probing and
    thread-starting legs stubbed (desktop-entry scan, native-binary detection,
    the remote-desktop orchestrator, PeerLink)."""
    from core.platform import bootstrap as B
    ext = tmp_path / 'no-extensions'
    ext.mkdir(exist_ok=True)
    with patch('core.port_registry.is_os_mode', return_value=os_mode), \
            patch.object(B, '_migrate_shell_manifest'), \
            patch.object(B, '_register_native_apps'), \
            patch.object(B, '_discover_desktop_apps'), \
            patch.object(B, '_register_orchestrator_services'), \
            patch.dict(sys.modules, {'core.peer_link.link_manager': None}):
        return B.bootstrap_platform(str(ext))


def _open_gates():
    """The boundaries agent_ui_update consults: the human kill-switch (open) and
    the audit DB (recorded, not written)."""
    audit = MagicMock()
    return (patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
                  return_value=False),
            patch('security.immutable_audit_log.get_audit_log',
                  return_value=audit))


def _capture_agent_ui(bus):
    """Subscribe to agent.ui.update and return (event, seen) so a test can wait
    on the async emit instead of sleeping."""
    seen = []
    got = threading.Event()

    def _on(topic, data):
        seen.append(data)
        got.set()
    bus.on('agent.ui.update', _on)
    return got, seen


def test_backend_bootstrap_registers_one_liquid_ui_that_reaches_the_users_sse(tmp_path):
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    reg = _bootstrap(tmp_path, os_mode=False)

    svc = reg.get_or_none('LiquidUIService')
    assert isinstance(svc, LiquidUIService)

    got, seen = _capture_agent_ui(reg.get('events'))
    halted, audit = _open_gates()
    with halted, audit, \
            patch('core.platform.events.broadcast_sse_safe') as sse:
        ok = svc.agent_ui_update(
            'model_ready', {'type': 'notification', 'title': 'Ready',
                            'message': 'the model is up'},
            user_id='user-42')
        assert ok is True
        assert got.wait(5), 'agent.ui.update was never emitted on the EventBus'

        # The event carries the owning user, so the P3a guard ROUTES it.
        assert seen[0]['user_id'] == 'user-42'
        assert seen[0]['component']['type'] == 'notification'
        # ...and the EventBus handed it to the SSE transport for THAT user.
        sse_calls = [c for c in sse.call_args_list
                     if c.args and c.args[0] == 'agent.ui.update']
        assert sse_calls, 'the push never reached the SSE transport'
        assert sse_calls[0].kwargs.get('user_id') == 'user-42'


def test_no_explicit_user_falls_back_to_the_resolved_owner(tmp_path):
    reg = _bootstrap(tmp_path, os_mode=False)
    svc = reg.get('LiquidUIService')
    got, seen = _capture_agent_ui(reg.get('events'))
    halted, audit = _open_gates()
    with halted, audit, \
            patch('core.platform.events.broadcast_sse_safe'), \
            patch('core.event_attribution.owner_user_id',
                  return_value='sole-owner'):
        assert svc.agent_ui_update('system', {'type': 'card', 'title': 'x'})
        assert got.wait(5)
    assert seen[0]['user_id'] == 'sole-owner'


def test_a_real_emitter_now_delivers_instead_of_dropping(tmp_path):
    """AppInstaller._push_desktop_icon returns early when the registry has no
    LiquidUIService.  In the backend process that was always true, so every
    install's live-icon card was dropped.  It must now reach the SSE transport."""
    from core.platform.app_manifest import AppManifest, AppType
    from integrations.agent_engine.app_installer import AppInstaller

    reg = _bootstrap(tmp_path, os_mode=False)
    got, seen = _capture_agent_ui(reg.get('events'))
    manifest = AppManifest(
        id='org.gnome.Calculator', name='Calculator', version='1.0',
        type=AppType.DESKTOP_APP.value, icon='calculate',
        entry={'exec': 'gnome-calculator'}, group='Installed',
        tags=['installed', 'flatpak'])
    halted, audit = _open_gates()
    with halted, audit, \
            patch('core.platform.events.broadcast_sse_safe') as sse, \
            patch('core.event_attribution.owner_user_id',
                  return_value='owner-7'):
        AppInstaller()._push_desktop_icon('org.gnome.Calculator', manifest)
        assert got.wait(5), 'the installer push was dropped'
    assert seen[0]['component']['type'] == 'app_installed'
    assert seen[0]['component']['id'] == 'org.gnome.Calculator'
    assert any(c.args and c.args[0] == 'agent.ui.update'
               and c.kwargs.get('user_id') == 'owner-7'
               for c in sse.call_args_list)


def test_an_emitter_that_knows_the_user_routes_to_them_on_a_multi_user_node(tmp_path):
    """offer_sound_for_review knows whose sound it is.  On a node with more
    than one human the sole-tenant fallback answers None and the P3a guard
    refuses the SSE leg, so the card only reaches the person when the
    emitter names them (user_id=).  This desktop's DB measured 315 human +
    21 guest rows, i.e. exactly that node."""
    from core.agent_tools import offer_sound_for_review
    reg = _bootstrap(tmp_path, os_mode=False)
    bus = reg.get('events')
    halted, audit = _open_gates()
    with halted, audit, \
            patch('core.platform.events.broadcast_sse_safe') as sse, \
            patch('core.platform.events.emit_event',
                  side_effect=lambda t, d=None, async_=True: bus.emit(t, d)), \
            patch('core.event_attribution.owner_user_id', return_value=None):
        shown = offer_sound_for_review(
            'user-9', 'prompt-1', 'g1', 'win',
            {'url': 'http://127.0.0.1:5000/a.wav'})
    assert shown is True
    routed = [c.kwargs.get('user_id') for c in sse.call_args_list
              if c.args and c.args[0] == 'agent.ui.update']
    assert routed == ['user-9', 'user-9']      # the media card + the ask


def test_bootstrap_is_idempotent_one_instance(tmp_path):
    reg = _bootstrap(tmp_path, os_mode=False)
    first = reg.get('LiquidUIService')
    again = _bootstrap(tmp_path, os_mode=False)
    assert again is reg
    assert reg.get('LiquidUIService') is first
    assert reg.names().count('LiquidUIService') == 1


def test_register_helper_never_displaces_an_existing_owner():
    """Whatever registered first (a serving shell, a probe stub) stays the
    owner; the bootstrap helper only fills an empty seat."""
    from core.platform.bootstrap import _register_liquid_ui
    reg = get_registry()
    owner = object()
    reg.register('LiquidUIService', lambda: owner)
    with patch('core.port_registry.is_os_mode', return_value=False):
        _register_liquid_ui(reg)
    assert reg.get('LiquidUIService') is owner


def test_hart_os_backend_leaves_the_seat_to_the_separate_shell(tmp_path):
    """On HART OS the :6800 unit owns Liquid UI.  The backend must not grab a
    headless copy, or run_home_compose would compose into it and the live shell
    would never see the home."""
    from integrations.agent_engine import liquid_ui_service as L
    reg = _bootstrap(tmp_path, os_mode=True)
    assert not reg.has('LiquidUIService')

    payload = {'hero': None, 'rows': [{'title': 'R', 'accent': 'teal',
               'cards': [{'title': 'C', 'action': 'open'}]}]}
    resp = MagicMock()
    resp.status_code = 200
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
            patch.object(L, 'build_home_payload', return_value=payload), \
            patch('core.http_pool.pooled_post', return_value=resp) as post:
        assert L.run_home_compose(reason='idle_tick') is True
    assert post.call_args[0][0].endswith('/api/home/compose')


def test_backend_owner_does_not_swallow_the_home_compose(tmp_path):
    """The headless backend instance has no home renderer.  run_home_compose
    must not treat it as 'the live shell': it keeps the existing cross-process
    path, so a separately served shell still receives the home."""
    from integrations.agent_engine import liquid_ui_service as L
    reg = _bootstrap(tmp_path, os_mode=False)
    svc = reg.get('LiquidUIService')
    resp = MagicMock()
    resp.status_code = 200
    payload = {'hero': None, 'rows': [{'title': 'R', 'accent': 'teal',
               'cards': [{'title': 'C', 'action': 'open'}]}]}
    with patch('security.hive_guardrails.HiveCircuitBreaker.is_halted',
               return_value=False), \
            patch.object(svc, 'compose_home_now') as in_proc, \
            patch.object(L, 'build_home_payload', return_value=payload), \
            patch('core.http_pool.pooled_post', return_value=resp) as post:
        L.run_home_compose(reason='idle_tick')
    in_proc.assert_not_called()
    assert post.called


def test_a_serving_shell_displaces_the_headless_backend_instance(tmp_path):
    """A process that bootstraps AND serves the shell (serve_forever calls
    ensure_platform first) must end with the SERVING instance as the owner, or
    in-process emitters would paint a copy nobody is streaming."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    reg = _bootstrap(tmp_path, os_mode=False)
    headless = reg.get('LiquidUIService')

    shell = LiquidUIService(a2ui_enabled=True)
    shell._register_self()
    assert reg.get_or_none('LiquidUIService') is shell
    assert reg.get_or_none('LiquidUIService') is not headless


def test_hart_os_shell_process_owns_its_serving_instance(tmp_path):
    """The :6800 unit on HART OS: serve_forever -> ensure_platform ->
    bootstrap (os mode, registers nothing) -> _create_flask_app ->
    _register_self.  The serving instance ends up the one owner."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    reg = _bootstrap(tmp_path, os_mode=True)
    shell = LiquidUIService(a2ui_enabled=True)
    shell._register_self()
    assert reg.get_or_none('LiquidUIService') is shell
    assert reg.names().count('LiquidUIService') == 1


def test_a_second_serving_shell_does_not_displace_the_first():
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    first = LiquidUIService(a2ui_enabled=True)
    first._register_self()
    second = LiquidUIService(a2ui_enabled=True)
    second._register_self()
    assert get_registry().get_or_none('LiquidUIService') is first


def test_a_serving_shell_does_not_build_a_throwaway_headless_instance(tmp_path):
    """Review of a0ecafe09 (item 3): _register_self called get_or_none to
    inspect the seat, which ran bootstrap's lazy factory and built a full
    headless LiquidUIService only to unregister it.  An un-instantiated
    seat can only be headless (a serving registration is materialised as
    it registers), so it is displaced without being built."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    reg = _bootstrap(tmp_path, os_mode=False)
    shell = LiquidUIService(a2ui_enabled=True)
    built = []
    real_init = LiquidUIService.__init__

    def _counting_init(self, *a, **k):
        built.append(self)
        real_init(self, *a, **k)
    with patch.object(LiquidUIService, '__init__', _counting_init):
        shell._register_self()
    assert built == []
    assert reg.get_or_none('LiquidUIService') is shell


def test_a_lazily_registered_serving_shell_is_still_not_displaced():
    """The first serving shell's seat is materialised when it registers, so
    a later peek sees it and a second shell leaves it alone."""
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    first = LiquidUIService(a2ui_enabled=True)
    first._register_self()
    assert get_registry().peek('LiquidUIService') is first
