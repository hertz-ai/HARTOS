"""Central is not asked for a screen it does not have (owner, 2026-10-21).

Central has no screen.  From 10:06:01Z on 10-19, when HEVOLVE_OWNER_USER_ID
was set there, its VisionService screen-capture loop asked user 10202 for
screen_capture every 10 seconds: 30,487 consent.request emits by 10-21
04:44Z (central's docker logs, read-only).  The container has no DISPLAY,
WAYLAND_DISPLAY or SWAYSOCK, and HEVOLVE_NODE_TIER=central.

Owner: "hartos shd be hardware aware in requesting consents", then "this
shd be specific to central only the restriction", and "a potato in a local
hive shd have same capabilities as sum of all nodes is the goal".  So:

  * central, when its boot hardware check found no screen, files no
    screen_capture ask, its capture loop does not start, and its screen
    tools say central has no screen instead of claiming an ask was sent;
  * every other node (flat, regional, no tier set) asks exactly as before,
    with or without a screen: in a person's hive a node without a screen
    still reaches the screen of the hive;
  * central with a screen, or whose check has not run or could not tell,
    asks as before.

    python -m pytest tests/unit/test_consent_asks_only_for_hardware_here.py -q
"""
import ctypes
import logging
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# DRY (Gate 2): the one consent-table fixture and its owner, and the
# /visual_agent route harness, live with their own gate tests.
from tests.unit import test_computer_control_consent as _consent_tests  # noqa: E402
from tests.unit import test_visual_agent_screen_consent as _visual_tests  # noqa: E402

OWNER = _consent_tests.OWNER
consents = _consent_tests.consents
screen = _visual_tests.screen

_DISPLAY_ENV = ('DISPLAY', 'WAYLAND_DISPLAY', 'SWAYSOCK')
#: Every topology but central, as HEVOLVE_NODE_TIER spells it (None: unset).
_HIVE_TIERS = ('flat', 'regional', None)


def _found(monkeypatch, has_screen, tier='central'):
    """This node's topology, and what its boot hardware check
    (run_system_check) found: the profile object it caches, with the screen
    answer set."""
    from security import system_requirements as sr
    if tier is None:
        monkeypatch.delenv('HEVOLVE_NODE_TIER', raising=False)
    else:
        monkeypatch.setenv('HEVOLVE_NODE_TIER', tier)
    hw = sr.HardwareProfile()
    hw.has_display = has_screen
    monkeypatch.setattr(sr, '_capabilities', sr.NodeCapabilities(
        tier=sr.NodeTierLevel.STANDARD, hardware=hw, enabled_features=[],
        disabled_features={}, env_vars_set={}))


def _rows(consent_type='screen_capture'):
    from integrations.social import models
    from integrations.social.models import UserConsent
    with models.db_session() as db:
        return db.query(UserConsent).filter_by(
            consent_type=consent_type).count()


def _check_or_request(consent_type='screen_capture'):
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    with models.db_session() as db:
        return ConsentService.check_or_request(db, OWNER, consent_type)


# ── central ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('tier', ['central', ' Central '])
def test_central_with_no_screen_is_not_asked_for_one(consents, monkeypatch,
                                                     tier):
    _found(monkeypatch, False, tier=tier)
    assert _check_or_request() is False
    assert _rows() == 0, 'central filed a screen_capture ask with no screen'
    assert consents == []


def test_asking_central_again_sends_nothing(consents, monkeypatch):
    """The capture loop polls; no poll may emit."""
    _found(monkeypatch, False)
    for _ in range(3):
        _check_or_request()
    assert consents == []


def test_a_direct_ask_on_central_files_nothing(consents, monkeypatch):
    """Callers that skip check_or_request (DeviceRoutingService, the
    LangChain tools) reach request_consent itself."""
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    _found(monkeypatch, False)
    with models.db_session() as db:
        assert ConsentService.request_consent(db, OWNER, 'screen_capture') is None
    assert _rows() == 0 and consents == []


def test_the_owner_is_told_once_why_no_card_came(consents, monkeypatch,
                                                 caplog):
    """The log names the reason on the first ask, not on every poll."""
    from integrations.social import consent_service
    # A fresh once-set, so an earlier test's log does not hide this one's.
    monkeypatch.setattr(consent_service, '_said_missing', set(),
                        raising=False)
    _found(monkeypatch, False)
    with caplog.at_level(logging.INFO, logger='hevolve.consent'):
        for _ in range(3):
            _check_or_request()
    said = [r.getMessage() for r in caplog.records
            if 'central has no screen' in r.getMessage()]
    assert said == ['consent screen_capture not asked: central has no screen']


def test_central_with_a_screen_is_asked_as_before(consents, monkeypatch):
    _found(monkeypatch, True)
    assert _check_or_request() is False
    assert _rows() == 1
    assert [a['consent_type'] for a in consents] == ['screen_capture']


def test_central_before_its_hardware_check_runs_is_asked(consents,
                                                         monkeypatch):
    from security import system_requirements as sr
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'central')
    monkeypatch.setattr(sr, '_capabilities', None)
    _check_or_request()
    assert _rows() == 1


def test_central_whose_check_could_not_tell_is_asked(consents, monkeypatch):
    _found(monkeypatch, None)
    _check_or_request()
    assert _rows() == 1


def test_other_asks_on_central_are_unchanged(consents, monkeypatch):
    """camera_capture is deliberately not gated: its frames also arrive
    over the vision socket from a client's browser, and the camera probe
    sees only Linux devices."""
    _found(monkeypatch, False)
    for consent_type in ('computer_control', 'camera_capture', 'credential'):
        _check_or_request(consent_type)
    assert sorted(a['consent_type'] for a in consents) == [
        'camera_capture', 'computer_control', 'credential']


# ── a node in a person's hive ─────────────────────────────────────────────

@pytest.mark.parametrize('tier', _HIVE_TIERS)
def test_a_hive_node_with_no_screen_is_still_asked(consents, monkeypatch,
                                                   tier):
    """A headless compute node or a GPU-less desktop in the owner's hive
    reaches what the hive has; its own missing screen cuts nothing."""
    _found(monkeypatch, False, tier=tier)
    assert _check_or_request() is False
    assert _rows() == 1
    assert [a['consent_type'] for a in consents] == ['screen_capture']


# ── the screen-capture loop ───────────────────────────────────────────────

def _start_capture(monkeypatch):
    import integrations.vision.vision_service as vsmod
    started = []
    monkeypatch.setattr(vsmod, 'run_screen_capture_loop',
                        lambda *a, **kw: started.append(1))
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    vs = vsmod.VisionService.__new__(vsmod.VisionService)
    vs._running = True
    vs._start_screen_capture(interval=0)
    thread = getattr(vs, '_screen_capture_thread', None)
    if thread is not None:
        thread.join(timeout=5)
    return thread, started


def test_the_capture_loop_does_not_start_on_central_with_no_screen(
        monkeypatch):
    _found(monkeypatch, False)
    thread, started = _start_capture(monkeypatch)
    assert thread is None
    assert started == []


def test_the_capture_loop_starts_on_central_with_a_screen(monkeypatch):
    _found(monkeypatch, True)
    _, started = _start_capture(monkeypatch)
    assert started == [1]


@pytest.mark.parametrize('tier', _HIVE_TIERS)
def test_the_capture_loop_starts_on_a_hive_node_with_no_screen(monkeypatch,
                                                               tier):
    _found(monkeypatch, False, tier=tier)
    _, started = _start_capture(monkeypatch)
    assert started == [1]


# ── the screen tools say why ──────────────────────────────────────────────

def test_the_screen_tool_says_central_has_no_screen(consents, monkeypatch):
    import hart_intelligence_entry as hie  # noqa: TID251 -- the tool under test lives here
    _found(monkeypatch, False)
    reply = hie._request_screen_consent('what is on my screen')
    assert reply == 'Not asked: central has no screen.'
    assert _rows() == 0 and consents == []


@pytest.mark.parametrize('has_screen, tier', [
    (True, 'central'), (False, 'flat'), (False, 'regional'), (False, None)])
def test_the_screen_tool_still_asks_elsewhere(consents, monkeypatch,
                                              has_screen, tier):
    import hart_intelligence_entry as hie  # noqa: TID251 -- the tool under test lives here
    _found(monkeypatch, has_screen, tier=tier)
    reply = hie._request_screen_consent('what is on my screen')
    assert 'request sent' in reply, reply
    assert _rows() == 1


def test_visual_agent_on_central_with_no_screen_grabs_and_asks_nothing(
        consents, screen, monkeypatch):
    import hart_intelligence_entry  # noqa: F401,TID251 -- its boot check runs first
    _found(monkeypatch, False)
    status, body = _visual_tests._post()
    assert status == 200
    assert screen == []
    assert body['vlm_status'] == 'no_screen'
    assert _rows() == 0 and consents == []


@pytest.mark.parametrize('tier', _HIVE_TIERS)
def test_visual_agent_on_a_hive_node_with_no_screen_asks_the_owner(
        consents, screen, monkeypatch, tier):
    import hart_intelligence_entry  # noqa: F401,TID251 -- its boot check runs first
    _found(monkeypatch, False, tier=tier)
    status, body = _visual_tests._post()
    assert status == 200
    assert screen == []
    assert body['vlm_status'] == 'consent_required'
    assert _rows() == 1


# ── the probe: can this process reach a screen ────────────────────────────

@pytest.mark.parametrize('env, expected', [
    ({}, False),
    ({'DISPLAY': ':0'}, True),
    ({'WAYLAND_DISPLAY': 'wayland-0'}, True),
    ({'SWAYSOCK': '/run/hart/sway-ipc.sock'}, True),
])
def test_linux_has_a_screen_only_with_a_display_session(monkeypatch, env,
                                                        expected):
    from security import system_requirements as sr
    monkeypatch.setattr(sr.platform, 'system', lambda: 'Linux')
    for name in _DISPLAY_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert sr._detect_display() is expected


@pytest.mark.parametrize('monitors, expected', [(0, False), (1, True), (3, True)])
def test_windows_has_a_screen_when_it_counts_a_monitor(monkeypatch, monitors,
                                                       expected):
    from security import system_requirements as sr
    asked = []

    def metrics(index):
        asked.append(index)
        return monitors

    monkeypatch.setattr(sr.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(
        user32=SimpleNamespace(GetSystemMetrics=metrics)), raising=False)
    assert sr._detect_display() is expected
    assert asked == [80]                  # SM_CMONITORS


@pytest.mark.parametrize('displays, status, expected', [
    (0, 0, False), (2, 0, True), (2, 1001, None)])
def test_macos_counts_its_active_displays(monkeypatch, displays, status,
                                          expected):
    from security import system_requirements as sr

    def active_display_list(max_displays, displays_out, count_ref):
        count_ref._obj.value = displays
        return status

    monkeypatch.setattr(sr.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(ctypes.cdll, 'LoadLibrary', lambda _path: SimpleNamespace(
        CGGetActiveDisplayList=active_display_list))
    assert sr._detect_display() is expected


def test_a_probe_that_fails_cannot_tell(monkeypatch):
    from security import system_requirements as sr

    def broken(index):
        raise OSError('user32 is not there')

    monkeypatch.setattr(sr.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(
        user32=SimpleNamespace(GetSystemMetrics=broken)), raising=False)
    assert sr._detect_display() is None


def test_an_unknown_system_cannot_tell(monkeypatch):
    from security import system_requirements as sr
    monkeypatch.setattr(sr.platform, 'system', lambda: 'Plan9')
    assert sr._detect_display() is None


def test_has_display_is_what_the_boot_check_found(monkeypatch):
    from security import system_requirements as sr
    monkeypatch.setattr(sr, '_capabilities', None)
    assert sr.has_display() is None
    for answer in (True, False, None):
        _found(monkeypatch, answer)
        assert sr.has_display() is answer


def test_the_boot_check_records_the_screen_and_reports_it(monkeypatch):
    from security import system_requirements as sr
    from tests.unit.module_swap import swap_modules
    monkeypatch.setattr(sr, 'check_network_connectivity', lambda: False)
    monkeypatch.setattr(sr, '_detect_gps', lambda: False)
    monkeypatch.setattr(sr, '_detect_display', lambda: False)
    with swap_modules({'integrations.service_tools.vram_manager': None}):
        hw = sr.detect_hardware()
    assert hw.has_display is False
    assert hw.to_dict()['has_display'] is False
