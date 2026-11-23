"""The owner's copilot OFF is their consent answer, and it outlives its marker.

Measured on the owner's desktop: the Claude Code copilot was switched off in
Admin on 2026-09-16.  That switch wrote one empty file,
<Claude config dir>/hartos-copilot.off, and nothing else: no copilot_access
row.  By 2026-11-22 the file was gone, copilot_enabled() read ON, and Claude
Code served background goals of agents the owner had set to local.

So the answer is recorded the way the camera and screen answers are
(channels.admin.api._record_feed_consent, vision_service.restore_feed_answers):
- the settings switch writes a copilot_access consent row
  (claude_code_backend.switch_copilot -> ConsentService
  .record_capability_decision), and the existing hook flips the marker;
- a marker that is missing while the last answer on file is a no is put back
  (reconcile_copilot_switch), wherever the switch is read for real work;
- an OFF whose marker cannot be written still refuses.

The same writer makes a camera or screen Deny given with nothing on file
outlive a restart: it used to be announced and forgotten.

No test here touches the real ~/.claude: CLAUDE_CONFIG_DIR is a temp folder,
set before anything is imported and again for each test.

    python -m pytest tests/unit/test_the_copilot_off_outlives_its_marker.py -q
"""
import logging
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ['CLAUDE_CONFIG_DIR'] = tempfile.mkdtemp(prefix='copilot_switch_test_')

import pytest  # noqa: E402

from integrations.coding_agent import claude_code_backend as ccb  # noqa: E402
from integrations.social import consent_service as cs  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402
from integrations.social.models import Base, UserConsent, db_session, get_engine  # noqa: E402
import integrations.agent_engine.model_registry as registry  # noqa: E402

#: The expert registry's real sync; each test runs with a recorder in its place.
_REAL_SYNC = registry.ensure_claude_code_registered

OWNER, SOMEONE_ELSE = 'owner-copilot', 'another-account'
COPILOT = 'copilot_access'


@pytest.fixture(autouse=True)
def world(monkeypatch, tmp_path):
    """A temp Claude config dir, the real consent table, nothing sent, and
    the expert registry's sync recorded instead of run."""
    config_dir = tmp_path / 'claude'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config_dir))
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HARTOS_COPILOT_ENABLED', raising=False)
    Base.metadata.create_all(get_engine())
    with db_session(commit=True) as db:
        db.query(UserConsent).delete()
    seen = {'sent': [], 'synced': [], 'dir': config_dir}
    monkeypatch.setattr(cs, '_emit',
                        lambda topic, data, **k: seen['sent'].append((topic, dict(data))))
    monkeypatch.setattr(cs, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(cs, '_embodied_feed_from_consent', lambda *a, **k: None)
    monkeypatch.setattr(registry, 'ensure_claude_code_registered',
                        lambda: seen['synced'].append(ccb.copilot_enabled()))
    for name in ('_copilot_off_unwritten', '_copilot_on_unrecorded'):
        monkeypatch.setattr(ccb, name, False, raising=False)
    monkeypatch.setattr(ccb, '_reconciled_at', None, raising=False)
    yield seen
    with db_session(commit=True) as db:
        db.query(UserConsent).delete()


def _marker():
    path = ccb._copilot_switch_path()
    assert os.path.dirname(path) != os.path.join(os.path.expanduser('~'), '.claude'), (
        'a test reached the real Claude config dir')
    return path


def _rows():
    with db_session() as db:
        return [(r.user_id, r.agent_id, r.scope, bool(r.granted), r.revoked_at is not None)
                for r in db.query(UserConsent).filter_by(consent_type=COPILOT)
                .order_by(UserConsent.created_at).all()]


def _switch(enabled):
    """The settings page's switch, as Nunba's /api/admin/mcp/enable calls it."""
    assert hasattr(ccb, 'switch_copilot'), (
        'the settings switch has no road that records the answer')
    return ccb.switch_copilot(enabled)


def _reconcile(force=True):
    assert hasattr(ccb, 'reconcile_copilot_switch'), (
        'nothing puts a missing off marker back')
    return ccb.reconcile_copilot_switch(force=force)


def _marker_goes_missing():
    os.remove(_marker())
    assert ccb.copilot_enabled() is True, 'the test did not reproduce the lost marker'


def _no_spawn(*a, **k):
    raise AssertionError('a claude process was started')


def _notices(seen):
    return [d for t, d in seen['sent'] if t == 'consent.refused_after_revoke']


# -- the defect: an OFF kept by the marker alone -------------------------------

def test_off_from_the_settings_page_is_recorded_as_the_owners_no(world):
    assert _switch(False) is False
    assert os.path.exists(_marker())
    assert ccb.copilot_enabled() is False
    assert _rows() == [(OWNER, None, '*', False, True)], (
        'the OFF left no copilot_access row: only the marker says it')
    with db_session() as db:
        assert ConsentService.feed_said_no(db, COPILOT) is True


def test_an_off_whose_marker_is_gone_is_put_back(world):
    """The measured failure: the marker was deleted and the copilot came on."""
    _switch(False)
    _marker_goes_missing()
    assert _reconcile() is True
    assert os.path.exists(_marker()), 'the off marker was not put back'
    assert ccb.copilot_enabled() is False


def test_the_spawn_refuses_once_the_marker_is_gone(world, monkeypatch):
    """invoke_claude is where a `claude -p` starts: it must read the owner's
    answer, not only the file."""
    _switch(False)
    _marker_goes_missing()
    monkeypatch.setattr(ccb, '_account_egress', lambda *a, **k: None)
    monkeypatch.setattr(ccb, 'run_bounded', _no_spawn)
    result = ccb.invoke_claude('hello', mode='inference')
    assert result['ok'] is False and result['category'] == 'off', result
    assert os.path.exists(_marker())


def test_the_expert_registry_sync_puts_it_back_first(world, monkeypatch):
    """ensure_claude_code_registered runs at boot and before an expert turn:
    the marker is back before availability is read, so the backend is not
    offered against the owner's no."""
    _switch(False)
    _marker_goes_missing()
    marker_when_read = []
    monkeypatch.setattr(ccb, 'claude_code_available',
                        lambda: marker_when_read.append(os.path.exists(_marker())) or False)
    monkeypatch.setattr(registry.model_registry, 'unregister', lambda name: False)
    _REAL_SYNC()
    assert marker_when_read == [True], (
        'availability was read while the off marker was still missing')
    assert ccb.copilot_enabled() is False


def test_the_mcp_bridge_refuses_once_the_marker_is_gone(world):
    from flask import Flask
    from integrations.mcp import mcp_http_bridge as bridge
    _switch(False)
    _marker_goes_missing()
    with Flask('mcp-gate-test').test_request_context('/api/mcp/local/health'):
        answer = bridge._mcp_auth_gate()
    assert answer is not None and answer[1] == 503, (
        'the MCP surface answered while the owner\'s last answer is off')
    assert os.path.exists(_marker())


def test_the_owner_is_told_once_per_mismatch_with_where_to_turn_it_on(world):
    _switch(False)
    _marker_goes_missing()
    del world['sent'][:]
    _reconcile()
    _reconcile()
    _reconcile()
    notices = _notices(world)
    assert len(notices) == 1, f'told {len(notices)} times for one mismatch'
    assert notices[0]['user_id'] == OWNER and notices[0]['consent_type'] == COPILOT
    assert 'switched off again' in notices[0]['reason']
    assert 'Admin, Integrations, Claude Code' in notices[0]['reason']
    # A second loss is a second mismatch.
    _marker_goes_missing()
    _reconcile()
    assert len(_notices(world)) == 2


def test_the_put_back_is_logged_as_a_warning(world):
    _switch(False)
    _marker_goes_missing()
    records = []

    class _Catch(logging.Handler):
        def emit(self, record):
            records.append(record)
    before = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    handler = _Catch(level=logging.WARNING)
    ccb.logger.addHandler(handler)
    try:
        _reconcile()
    finally:
        ccb.logger.removeHandler(handler)
        logging.disable(before)
    said = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert any('was missing' in m and 'switched off again' in m for m in said), said


# -- ON, and what must not be switched off ---------------------------------------

def test_on_from_the_settings_page_is_the_newer_answer_and_stays_on(world):
    _switch(False)
    assert _switch(True) is True
    assert not os.path.exists(_marker())
    assert _rows()[-1] == (OWNER, None, '*', True, False), _rows()
    assert _reconcile() is False
    assert ccb.copilot_enabled() is True


def test_off_on_off_ends_off_and_the_last_answer_is_the_no(world):
    _switch(False)
    _switch(True)
    assert _switch(False) is False
    _marker_goes_missing()
    assert _reconcile() is True
    assert ccb.copilot_enabled() is False


def test_with_no_answer_on_file_a_missing_marker_means_on_as_before(world):
    assert _reconcile() is False
    assert ccb.copilot_enabled() is True and not os.path.exists(_marker())
    assert _notices(world) == []


def test_a_grant_that_stood_under_an_old_marker_is_switched_on_by_on(world):
    """Before this, OFF wrote the marker under a grant that stayed.  ON then
    finds the grant already there, and must still remove the marker."""
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, COPILOT)
    ccb.set_copilot_enabled(False)           # the old switch: marker only
    assert ccb.copilot_enabled() is False
    assert _switch(True) is True
    assert not os.path.exists(_marker())
    assert [r for r in _rows() if r[3]] == [(OWNER, None, '*', True, False)], (
        'a second grant row was written')


def test_an_approve_on_any_surface_switches_a_standing_grant_back_on(world):
    """The one writer every approval surface uses (record_capability_decision),
    called as a card or /api/agent/approval calls it: the grant already
    stands, so no row is written, and the marker must still go."""
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, COPILOT)
    ccb.set_copilot_enabled(False)           # the old switch: marker only
    with db_session(commit=True) as db:
        assert ConsentService.record_capability_decision(
            db, OWNER, 'copilot', True) == COPILOT
    assert ccb.copilot_enabled() is True, 'the Approve left the copilot off'
    assert len(_rows()) == 1


def test_the_owners_yes_on_a_card_still_switches_it_on(world):
    _switch(False)
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, COPILOT)
    assert ccb.copilot_enabled() is True
    assert _reconcile() is False


# -- whose answer, and how a reopen counts ----------------------------------------

def test_the_last_answer_on_the_node_stands_whoever_filed_it(world):
    """As for the camera and screen: the owner's two ids, or a no filed
    signed in as someone else, are one node-wide answer."""
    with db_session(commit=True) as db:
        ConsentService.record_capability_decision(db, SOMEONE_ELSE, 'copilot', False)
    assert ccb.copilot_enabled() is False
    _marker_goes_missing()
    assert _reconcile() is True
    assert ccb.copilot_enabled() is False


def test_a_no_that_was_reopened_is_still_the_last_answer(world):
    """"Allow asking again" lets an agent ask; it does not switch the
    copilot on.  feed_said_no does not read reopened_at, and neither does
    this."""
    _switch(False)
    with db_session(commit=True) as db:
        assert ConsentService.reopen(db, OWNER, COPILOT) == 1
    with db_session() as db:
        assert ConsentService.declined(db, OWNER, COPILOT) is False
        assert ConsentService.feed_said_no(db, COPILOT) is True
    _marker_goes_missing()
    assert _reconcile() is True
    assert ccb.copilot_enabled() is False


# -- a marker that cannot be written --------------------------------------------

def _refuse_the_write(*a, **k):
    raise OSError(30, 'Read-only file system')


def test_an_off_that_cannot_reach_the_disk_still_refuses(world, monkeypatch):
    monkeypatch.setattr(ccb, '_account_egress', lambda *a, **k: None)
    monkeypatch.setattr(ccb, 'run_bounded', _no_spawn)
    with monkeypatch.context() as disk:
        disk.setattr(os, 'makedirs', _refuse_the_write)
        assert _switch(False) is False, 'OFF was reported as ON'
    assert not os.path.exists(_marker())
    assert ccb.copilot_enabled() is False, 'an OFF that could not be written left it on'
    assert ccb.invoke_claude('hello', mode='inference')['category'] == 'off'
    # The owner's ON brings it back.
    assert _switch(True) is True
    assert ccb.copilot_enabled() is True


def test_a_put_back_that_cannot_reach_the_disk_still_refuses_and_tells_once(
        world, monkeypatch):
    _switch(False)
    _marker_goes_missing()
    del world['sent'][:]
    with monkeypatch.context() as disk:
        disk.setattr(os, 'makedirs', _refuse_the_write)
        assert _reconcile() is True
    assert not os.path.exists(_marker())
    assert ccb.copilot_enabled() is False, 'the copilot ran against the last no on file'
    _reconcile()
    assert len(_notices(world)) == 1


# -- cheap, read-only, and never against the owner's ON -----------------------------

def _count_reads(monkeypatch):
    reads = []
    real = ConsentService.feed_said_no
    monkeypatch.setattr(ConsentService, 'feed_said_no', staticmethod(
        lambda *a, **k: reads.append(1) or real(*a, **k)))
    return reads


def test_nothing_is_read_while_the_marker_is_there(world, monkeypatch):
    _switch(False)
    reads = _count_reads(monkeypatch)
    for _ in range(5):
        assert _reconcile(force=True) is False
    assert reads == []


def test_without_force_the_store_is_read_at_most_once_per_interval(world, monkeypatch):
    reads = _count_reads(monkeypatch)
    for _ in range(5):
        _reconcile(force=False)
    assert reads == [1]
    assert ccb.COPILOT_RECONCILE_EVERY_S >= 10


def test_a_store_that_cannot_be_read_leaves_the_switch_as_it_is(world, monkeypatch):
    monkeypatch.setattr(ConsentService, 'feed_said_no',
                        staticmethod(lambda *a, **k: 1 / 0))
    assert _reconcile() is False
    assert ccb.copilot_enabled() is True and not os.path.exists(_marker())


def test_the_revoke_flips_the_switch_once_and_does_not_call_itself(world, monkeypatch):
    """Writing the row from inside set_copilot_enabled would recurse:
    revoke -> announce -> the switch -> revoke ...  The row is written by
    switch_copilot, and the switch is flipped once, by the hook."""
    flips = []
    real = ccb.set_copilot_enabled
    monkeypatch.setattr(ccb, 'set_copilot_enabled',
                        lambda enabled: flips.append(enabled) or real(enabled))
    _switch(False)
    assert flips == [False]
    assert len(_rows()) == 1
    del flips[:]
    _switch(True)
    assert flips == [True]


def test_an_on_that_could_not_be_recorded_is_not_undone_by_an_older_no(world, monkeypatch):
    """A consent problem never costs the owner their switch."""
    _switch(False)
    monkeypatch.setattr(ConsentService, 'record_capability_decision',
                        staticmethod(lambda *a, **k: 1 / 0))
    assert _switch(True) is True
    assert not os.path.exists(_marker())
    assert _reconcile() is False, 'the owner\'s ON was switched off again'
    assert ccb.copilot_enabled() is True


def test_with_no_owner_the_marker_alone_keeps_the_answer_as_before(world, monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    assert _switch(False) is False
    assert os.path.exists(_marker()) and _rows() == []
    assert _switch(True) is True
    assert not os.path.exists(_marker())


def test_an_env_pin_still_only_takes_away(world, monkeypatch):
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '0')
    assert _switch(True) is False, 'an ON was reported in force under an off pin'
    monkeypatch.setenv('HARTOS_COPILOT_ENABLED', '1')
    _switch(False)
    assert ccb.copilot_enabled() is False


def test_off_asks_nobody_and_rings_no_phone(world):
    del world['sent'][:]
    _switch(False)
    topics = [t for t, _d in world['sent']]
    assert 'consent.request' not in topics, topics
    assert topics.count('consent.revoked') == 1


# -- the same writer: a camera or screen Deny with nothing on file ---------------------

@pytest.mark.parametrize('action, consent_type', [
    ('enable_camera', 'camera_capture'), ('enable_screen', 'screen_capture')])
def test_a_feed_deny_with_nothing_on_file_is_on_file_for_the_next_start(
        world, action, consent_type):
    """It was announced (the feed stopped) and no row was written, so the
    next start read no answer and opened the feed again."""
    with db_session(commit=True) as db:
        assert ConsentService.record_capability_decision(
            db, OWNER, action, False) == consent_type
    with db_session() as db:
        rows = db.query(UserConsent).filter_by(consent_type=consent_type).all()
        assert [(r.user_id, r.agent_id, bool(r.granted), r.revoked_at is not None)
                for r in rows] == [(OWNER, None, False, True)], (
            'the Deny left nothing on file')
        assert ConsentService.feed_said_no(db, consent_type) is True
    assert [t for t, _d in world['sent']].count('consent.revoked') == 1


def test_a_deny_naming_an_agent_with_nothing_on_file_is_as_before(world):
    with db_session(commit=True) as db:
        ConsentService.record_capability_decision(
            db, OWNER, 'enable_camera', False, agent_id='111')
    with db_session() as db:
        assert db.query(UserConsent).filter_by(consent_type='camera_capture').count() == 0
    assert [t for t, _d in world['sent']].count('consent.revoked') == 1


def test_every_other_revoke_with_nothing_on_file_is_as_before(world):
    """Only the capability Deny keeps the no.  The decline route's 404, and
    a plain revoke's None, are unchanged."""
    with db_session(commit=True) as db:
        assert ConsentService.revoke_consent(db, OWNER, COPILOT) is None
        assert ConsentService.decline(db, OWNER, 'camera_capture') is None
        assert ConsentService.revoke_consent(db, OWNER, 'public_exposure') is None
    with db_session() as db:
        assert db.query(UserConsent).count() == 0
    assert world['sent'] == []


# -- the reason an agent's goal is handed on -----------------------------------------

def _parked_reason(monkeypatch):
    from types import SimpleNamespace
    from integrations.agent_engine import agent_daemon
    from integrations.agent_engine.goal_manager import GoalManager
    handed = []
    monkeypatch.setattr(GoalManager, 'escalate_goal', staticmethod(
        lambda db, goal_id, escalation: handed.append(escalation) or {}))
    goal = SimpleNamespace(id='g1', title='Write the report', config_json={})
    with db_session(commit=True) as db:
        verdict = agent_daemon._copilot_consent(db, goal, {'action': 'finish the step'})
    assert verdict == (None, True) and len(handed) == 1, (verdict, handed)
    return handed[0]['reason']


def test_after_off_the_goal_is_handed_on_with_where_the_copilot_is_turned_on(
        world, monkeypatch):
    _switch(False)
    reason = _parked_reason(monkeypatch)
    assert 'the owner has not allowed agents to use Claude' in reason, reason
    assert 'turned back on in Admin, Integrations, Claude Code' in reason
    assert 'Privacy settings' in reason


def test_a_grant_under_an_old_marker_gets_its_own_reason(world, monkeypatch):
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, OWNER, COPILOT)
    ccb.set_copilot_enabled(False)
    reason = _parked_reason(monkeypatch)
    assert 'although the owner allowed agents to use it' in reason, reason
    assert 'Admin, Integrations, Claude Code' in reason


def test_a_check_that_fails_says_what_failed(world, monkeypatch):
    ccb.set_copilot_enabled(False)
    monkeypatch.setattr(ConsentService, 'check_consent',
                        staticmethod(lambda *a, **k: 1 / 0))
    reason = _parked_reason(monkeypatch)
    assert 'ZeroDivisionError' in reason and 'Admin, Integrations, Claude Code' in reason
