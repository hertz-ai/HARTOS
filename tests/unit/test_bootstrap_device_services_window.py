"""hartos_bootstrap keeps device HELLOs out while it installs what a phone's
link rides on, and lets them back in after -- however the steps went.

A phone keeps the hello_ack's capabilities for its whole session (the
mobile paths it may call the desktop on come from them and are never asked
again), so a HELLO answered between the device verifier (step 1c) and the
mobile adapter / file channel (5c, 5d) opens a link that can serve nothing.
Measured 2026-10-19 13:11:57 on the installed desktop: the ack went out
24 s before step 5c, and every Reel Studio turn after it was refused on
the phone until the link was remade.  The bootstrap therefore opens
core.peer_link.link's device-services window before step 1c and closes it
after 5d, and closes it again in its own finally so a step that raised
cannot leave every phone refused.

The steps themselves are replaced here (each is a thread, a socket or a
database in production); what is run is the real _run_bootstrap and the
real window in core.peer_link.link.
"""
import os
import sys

import pytest

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core.peer_link import link as link_mod  # noqa: E402
from hartos import hartos_bootstrap as hb  # noqa: E402

#: Every step _run_bootstrap calls, in its order (the source is the spec).
STEPS = [
    '_install_api_gate', '_install_device_verifier', '_install_peer_admission_ask',
    '_install_peer_link_relay', '_init_social_subsystem', '_register_core_blueprints',
    '_run_consumer_hook', '_register_hive_blueprints', '_init_a2a_server',
    '_install_mobile_adapter', '_install_file_channel', '_init_database',
    '_init_channel_adapters', '_init_hevolveai_subprocess',
    '_init_agent_engine_subsystem', '_init_livekit_supervisor',
    '_init_whatsapp_supervisor', '_run_on_bootstrap_complete',
]


@pytest.fixture
def steps(monkeypatch):
    """Every step a no-op that records whether the window was open when it
    ran; the bypass helpers no-ops; the done flag reset."""
    ready_during = {}

    def _stub(name):
        def run(*args, **kwargs):
            ready_during[name] = link_mod.device_services_are_ready()
        return run

    for name in STEPS:
        monkeypatch.setattr(hb, name, _stub(name))
    monkeypatch.setattr(hb, '_enable_setup_lock_bypass', lambda app: False)
    monkeypatch.setattr(hb, '_disable_setup_lock_bypass', lambda app: None)
    monkeypatch.setattr(hb, '_BOOTSTRAP_DONE', False)
    link_mod.device_services_ready()
    yield ready_during
    link_mod.device_services_ready()


def test_device_hellos_are_held_from_the_verifier_to_the_file_channel(steps):
    hb._run_bootstrap(object(), {})
    assert steps['_install_api_gate'] is True        # before the window
    for name in STEPS[1:11]:                          # 1c .. 5d
        assert steps[name] is False, name
    assert steps['_init_database'] is True           # after it
    assert link_mod.device_services_are_ready()
    assert hb.is_bootstrap_complete()


def test_a_step_that_raises_still_closes_the_window(steps, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('the file channel could not bind')

    monkeypatch.setattr(hb, '_install_file_channel', boom)
    hb._run_bootstrap(object(), {})
    assert steps['_install_mobile_adapter'] is False
    assert link_mod.device_services_are_ready()
    assert hb.is_bootstrap_complete()


def test_a_device_hello_inside_the_window_is_refused_and_after_it_answered(steps, monkeypatch):
    """The window as a phone meets it: the real handshake refuses 'not_ready'
    while a step runs, and admits after the bootstrap (device_links tests
    cover the handshake itself; this pins that the bootstrap drives it)."""
    seen = {}

    def during_adapter(app):
        seen['ready_during_5c'] = link_mod.device_services_are_ready()

    monkeypatch.setattr(hb, '_install_mobile_adapter', during_adapter)
    hb._run_bootstrap(object(), {})
    assert seen == {'ready_during_5c': False}
    assert link_mod.device_services_are_ready()
