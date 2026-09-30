"""/visual_agent looks at the owner's screen only with their permission (#66).

The route's computer_use branch (hart_intelligence_entry.visual_agent)
grabbed the desktop with ImageGrab.grab() and sent it to the VLM with no
consent check.  call_visual_task (hartos/create_recipe.py) reaches it for an
agent's scheduled visual task: it posts request_from='Create' and no mode,
and mode 'auto' with any request_from but 'Reuse' takes this branch.
VisionService's own capture loop asks for screen_capture first
(vision_service._consent_ok, #701); this route asked nobody.

The permission is the desktop owner's (HEVOLVE_OWNER_USER_ID), never the
caller's user_id, and it is the same screen_capture consent the capture
loop asks for.

    python -m pytest tests/unit/test_visual_agent_screen_consent.py -q
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# DRY (Gate 2): the one consent-table fixture (a private in-memory
# UserConsent table behind models.db_session, the owner in
# HEVOLVE_OWNER_USER_ID, and the asks it sent) and its owner live with the
# computer_control gate tests.
from tests.unit import test_computer_control_consent as _consent_tests  # noqa: E402

OWNER = _consent_tests.OWNER
consents = _consent_tests.consents

API_KEY = 'test_visual_agent_key'
CALLER = '10077'           # the body user_id: whoever posted, not the owner
AGENT = '88659566083'


@pytest.fixture
def screen(monkeypatch):
    """A VLM that is up and a screen to grab.  Yields the list of grabs."""
    from PIL import Image
    grabs = []

    def _grab(*_a, **_kw):
        grabs.append(1)
        return Image.new('RGB', (64, 64))

    vision = MagicMock()
    vision.is_available.return_value = True
    vlm = MagicMock()
    vlm.point_and_act.return_value = {'reasoning': 'a desktop',
                                      'action': 'none', 'done': True}
    monkeypatch.setattr('PIL.ImageGrab.grab', _grab)
    monkeypatch.setattr(
        'integrations.vision.lightweight_backend.get_vision_backend',
        lambda: vision)
    monkeypatch.setattr(
        'integrations.vlm.qwen3vl_backend.get_qwen3vl_backend', lambda: vlm)
    monkeypatch.setenv('HEVOLVE_API_KEY', API_KEY)
    yield grabs


def _post(**body):
    from hart_intelligence_entry import app  # noqa: TID251 -- the route under test lives here
    payload = {'task_description': 'what is on the screen',
               'user_id': CALLER, 'prompt_id': AGENT,
               'request_from': 'Create'}
    payload.update(body)
    with app.test_client() as client:
        resp = client.post('/visual_agent', json=payload,
                           headers={'X-API-Key': API_KEY})
    return resp.status_code, resp.get_json()


def _grant(user_id):
    from integrations.social import models
    from integrations.social.consent_service import ConsentService
    with models.db_session() as db:
        ConsentService.grant_consent(db, user_id, 'screen_capture')


def _rows_for(user_id):
    from integrations.social import models
    from integrations.social.models import UserConsent
    with models.db_session() as db:
        return db.query(UserConsent).filter_by(
            user_id=user_id, consent_type='screen_capture').count()


def test_without_the_owners_consent_the_screen_is_not_grabbed(consents,
                                                              screen):
    status, body = _post()
    assert screen == [], 'the desktop was grabbed with no screen_capture consent'
    assert status == 200
    assert body['vlm_status'] == 'consent_required'


def test_the_owner_is_asked_not_the_caller(consents, screen):
    _post()
    assert _rows_for(OWNER) == 1, 'no screen_capture ask was filed for the owner'
    assert _rows_for(CALLER) == 0
    assert [a.get('consent_type') for a in consents] == ['screen_capture']


def test_with_the_owners_consent_the_screen_is_grabbed(consents, screen):
    _grant(OWNER)
    status, body = _post()
    assert screen == [1]
    assert body['vlm_status'] == 'ok'


def test_the_callers_consent_does_not_count(consents, screen):
    _grant(CALLER)
    _post()
    assert screen == [], ('the caller granted screen_capture for itself and '
                          'the owner\'s screen was grabbed')


def test_with_no_owner_nothing_is_grabbed_or_asked(consents, screen,
                                                   monkeypatch):
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID')
    _, body = _post()
    assert screen == []
    assert consents == []
    assert body['vlm_status'] == 'consent_required'


def test_a_failed_check_is_a_no(consents, screen, monkeypatch):
    from integrations.social import models

    def _broken(commit=True):
        raise RuntimeError('consent table unreachable')

    monkeypatch.setattr(models, 'db_session', _broken)
    _, body = _post()
    assert screen == []
    assert body['vlm_status'] == 'consent_required'


def test_an_explicit_computer_use_mode_is_gated_too(consents, screen):
    _post(request_from='Reuse', mode='computer_use')
    assert screen == []


def test_the_camera_path_is_unchanged(consents, screen):
    with patch('hart_intelligence_entry.visual_based_execution',
               return_value='Action completed') as run:
        _, body = _post(request_from='Reuse')
    assert body['response'] == 'Action completed'
    run.assert_called_once()
    assert screen == []
    assert consents == []
