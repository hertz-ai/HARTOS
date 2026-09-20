"""Camera and screen consent go through ConsentService, like every other ask.

The defect these pin (owner, 2026-09-20: "why the fuck we created a parallel
path instead of using canonical one we already had"):

``hart_intelligence_entry._request_consent`` -- what the agent tools
Request_Camera_Access and Request_Screen_Access call -- never touched
ConsentService.  It pushed a ``{'type': 'approval', ...}`` component through
LiquidUIService (or broadcast_sse_safe as a fallback) and stopped there.  So:

  * no UserConsent row was ever written, which means the grant could not be
    revoked, did not appear on the privacy page, and left no audit entry;
  * the ask arrived as type 'approval', which only AgentOverlay renders.  The
    floating companion -- the surface that exists for when Nunba is NOT the
    foreground window -- accepts 'consent.request' only, so it dropped every
    camera and screen ask (#863);
  * ``/api/agent/approval`` flipped the embodied_ai feed flags directly, so
    the owner's ANSWER was equally unrecorded.

The canonical path has existed the whole time and its own siblings use it:
integrations/vlm/safety.computer_control_block, vision_service's capture loop,
whisper_tool, social/auth, ai_governance and hive_guardrails all go through
ConsentService.  The fork simply never migrated (upstream fixed the missing
emit in e991309da on 2026-08-26; nobody carried it here).

Two halves, pinned separately:
  1. the ASK is filed as a consent record (the action->type map + the row);
  2. the ANSWER is recorded as a grant or a revoke, on every answer surface.

The source assertions exist because hart_intelligence_entry.py cannot be
imported in a unit test (13k lines, pulls in LangChain + autogen at module
scope) -- the same reason tests/test_agent_approval_wamp.py is AST-only.  They
are not vacuous: each one fails on the pre-migration source.
"""
import ast
import os
import re
from pathlib import Path

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

import pytest

from integrations.social.models import Base, db_session, get_engine

_HIE = Path(__file__).resolve().parents[2] / 'hart_intelligence_entry.py'


@pytest.fixture(autouse=True)
def _fresh_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


# ── 1. the vocabulary: ONE map, shared by the ask and the answer ───────

def test_camera_capture_is_a_consent_type():
    """The screen already had 'screen_capture'.  The camera had nothing, which
    is why the camera ask could not be filed canonically at all."""
    from integrations.social.consent_service import CONSENT_TYPES
    assert 'camera_capture' in CONSENT_TYPES


@pytest.mark.parametrize('action,expected', [
    # Exactly the aliases agent_approval already accepts, so the ask side and
    # the answer side cannot drift into two vocabularies for one capability.
    ('enable_camera', 'camera_capture'),
    ('camera', 'camera_capture'),
    ('vision', 'camera_capture'),
    ('enable_screen', 'screen_capture'),
    ('screen', 'screen_capture'),
    ('computer_use', 'screen_capture'),
    ('ENABLE_CAMERA', 'camera_capture'),   # the endpoint lowercases; the map must not care
    ('  enable_screen  ', 'screen_capture'),
])
def test_consent_type_for_action(action, expected):
    from integrations.social.consent_service import consent_type_for_action
    assert consent_type_for_action(action) == expected


@pytest.mark.parametrize('action', ['', None, 'enable_audio', 'mic', 'launch_missiles'])
def test_unmapped_action_has_no_consent_type(action):
    """An action nothing asks for must not silently mint a consent type.
    'enable_audio'/'mic' have no ask producer today; mapping them here would
    manufacture a grant the owner was never shown."""
    from integrations.social.consent_service import consent_type_for_action
    assert consent_type_for_action(action) is None


# ── 2. the ANSWER is recorded, so the grant is revocable ───────────────

def test_approval_records_a_grant_that_check_consent_sees():
    from integrations.social.consent_service import ConsentService
    with db_session(commit=True) as db:
        out = ConsentService.record_capability_decision(
            db, 'owner-1', 'enable_camera', True, agent_id='vision')
        assert out == 'camera_capture'
    with db_session() as db:
        assert ConsentService.check_consent(db, 'owner-1', 'camera_capture') is True


def test_denial_records_a_revoke_not_a_grant():
    """A "Deny" used to be a log line and nothing else.  It must leave a
    decided record, or request_consent re-asks on the very next tick and the
    owner is pestered forever."""
    from integrations.social.consent_service import ConsentService
    with db_session(commit=True) as db:
        ConsentService.request_consent(db, 'owner-2', 'screen_capture')
    with db_session(commit=True) as db:
        ConsentService.record_capability_decision(
            db, 'owner-2', 'enable_screen', False)
    with db_session() as db:
        assert ConsentService.check_consent(db, 'owner-2', 'screen_capture') is False
        assert ConsentService.declined(db, 'owner-2', 'screen_capture') is True


def test_an_unmapped_action_records_nothing():
    from integrations.social.consent_service import ConsentService
    from integrations.social.models import UserConsent
    with db_session(commit=True) as db:
        assert ConsentService.record_capability_decision(
            db, 'owner-3', 'enable_audio', True) is None
    with db_session() as db:
        assert db.query(UserConsent).filter_by(user_id='owner-3').count() == 0


# ── 3. the ASK: no camera/screen consent leaves by the parallel path ───

def _hie_source():
    return _HIE.read_text(encoding='utf-8')


def _function_node(src, name):
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _function_src(src, name):
    """The source of one top-level def, by AST line span -- a regex on a
    13k-line file picks up the wrong body too easily."""
    node = _function_node(src, name)
    if node is None:
        return ''
    return '\n'.join(src.splitlines()[node.lineno - 1:node.end_lineno])


def _function_code(src, name):
    """The function's CODE, with its docstring and every comment removed.

    The 'no approval component' assertion below has to read code and only
    code: the migrated function's docstring NAMES the payload it stopped
    emitting, to explain why, and a text search cannot tell that apart from
    the real thing.  ast.unparse drops comments, and the docstring is
    dropped explicitly.
    """
    node = _function_node(src, name)
    if node is None:
        return ''
    body = node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return '\n'.join(ast.unparse(stmt) for stmt in body)


def test_request_consent_goes_through_consent_service():
    """RED pre-migration: _request_consent's whole body was
    LiquidUIService.agent_request_approval + broadcast_sse_safe."""
    body = _function_src(_hie_source(), '_request_consent')
    assert body, '_request_consent not found in hart_intelligence_entry.py'
    assert 'ConsentService' in body, (
        'the camera/screen ask must be filed through ConsentService — without '
        'a UserConsent row the grant cannot be revoked, shows on no privacy '
        'page, and the ask never reaches the floating companion (#863)'
    )


def test_request_consent_no_longer_emits_a_bare_approval_component():
    """The 'approval' payload type is the parallel path itself: only
    AgentOverlay renders it, so the ask died whenever the main window was not
    the one in front."""
    body = _function_code(_hie_source(), '_request_consent')
    offenders = [ln for ln in body.splitlines()
                 if re.search(r"""['"]type['"]\s*:\s*['"]approval['"]""", ln)
                 or 'agent_request_approval' in ln]
    assert not offenders, (
        '_request_consent still emits the non-canonical approval component: '
        + '; '.join(s.strip() for s in offenders)
    )


def test_agent_approval_records_the_decision():
    """RED pre-migration: the endpoint flipped embodied_ai.*_enabled and
    published WAMP, and that was the entire record of the owner's answer."""
    body = _function_src(_hie_source(), 'agent_approval')
    assert body, 'agent_approval not found'
    assert 'record_capability_decision' in body, (
        "the owner's answer must be written through ConsentService, or the "
        'feed turns on with no revocable grant behind it'
    )


def test_a_grant_from_any_surface_starts_the_feed(monkeypatch):
    """The point of the migration, behaviourally.

    Before it, only /api/agent/approval applied an embodied feed, so a grant
    made on the privacy page, on the floating companion's card or from a
    phone wrote a row that turned nothing on.  Now grant_consent drives the
    ONE actuator the admin toggle uses, whatever surface answered.
    """
    from integrations.social import consent_service
    from integrations.social.consent_service import ConsentService

    applied = []
    monkeypatch.setattr(consent_service, '_embodied_feed_from_consent',
                        lambda ct, granted: applied.append((ct, granted)))
    with db_session(commit=True) as db:
        ConsentService.grant_consent(db, 'owner-4', 'camera_capture')
    assert applied == [('camera_capture', True)], (
        'a camera_capture grant did not reach the embodied-feed actuator'
    )

    applied.clear()
    with db_session(commit=True) as db:
        ConsentService.revoke_consent(db, 'owner-4', 'camera_capture')
    assert applied == [('camera_capture', False)], (
        'revoking camera_capture did not stop the feed'
    )


def test_the_feed_actuator_drives_the_one_admin_lifecycle_path(monkeypatch):
    """_embodied_feed_from_consent must call the SAME _apply_embodied_toggle
    admin settings use — not a second start/stop implementation."""
    import integrations.channels.admin.api as admin_api
    from integrations.social import consent_service

    calls = []

    class _Cfg:
        camera_enabled = False
        screen_capture_enabled = False

    class _Api:
        _global_config = type('G', (), {'embodied_ai': _Cfg()})()

        def _save_config(self):
            calls.append('saved')

    monkeypatch.setattr(admin_api, 'get_api', lambda: _Api())
    monkeypatch.setattr(admin_api, '_apply_embodied_toggle',
                        lambda feed, on, cfg: calls.append((feed, on)))

    consent_service._embodied_feed_from_consent('screen_capture', True)
    assert ('screen', True) in calls
    assert 'saved' in calls, 'the persisted flag was not written — a restart would forget'

    calls.clear()
    consent_service._embodied_feed_from_consent('copilot_access', True)
    assert calls == [], 'a non-feed consent type must not touch the embodied feeds'


def test_agent_approval_still_applies_the_feed():
    """The other side of the guard: recording the consent must not replace
    the ONE feed lifecycle path (_apply_embodied_toggle), or approving stops
    actually starting the camera."""
    body = _function_src(_hie_source(), 'agent_approval')
    assert '_apply_embodied_toggle' in body
