"""An image that was never made must not be reported as made.

Found 2026-09-26 in the Nunba desktop logs: hartos.helper.txt2img posts to
a cloud host a desktop cannot reach, and on any failure (the request
failed, the circuit breaker is open, or the service answered with no
img_url) it returns ''.  media_agent._generate_image wrapped that '' in
{'status': 'completed', 'results': [{'url': ''}]}, so generate_media told
the agent an image was ready.  The user was told an image was made and got
nothing.

These tests call the real media_agent functions.  Only the boundary is
replaced: hartos.helper.txt2img (the network client), resolved through
sys.modules exactly as the function's own local import resolves it.
"""
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def txt2img(monkeypatch):
    fn = MagicMock(name='txt2img')
    monkeypatch.setitem(sys.modules, 'hartos.helper',
                        SimpleNamespace(txt2img=fn))
    return fn


def test_an_empty_url_is_an_error_not_a_completed_image(txt2img):
    from integrations.service_tools.media_agent import _generate_image
    txt2img.return_value = ''
    out = _generate_image('a red kite', '', '')
    assert out['status'] == 'error', out
    assert out.get('error'), 'the failure has to say why'
    assert out['output_modality'] == 'image'
    assert 'results' not in out, 'no result may be offered for no image'


def test_a_none_url_is_an_error(txt2img):
    from integrations.service_tools.media_agent import _generate_image
    txt2img.return_value = None
    assert _generate_image('a red kite', '', '')['status'] == 'error'


def test_a_real_url_is_still_a_completed_image(txt2img):
    from integrations.service_tools.media_agent import _generate_image
    txt2img.return_value = 'https://img.example/k.png'
    out = _generate_image('a red kite', 'kite at dusk', 'watercolour')
    assert out['status'] == 'completed', out
    assert out['results'] == [{'type': 'image',
                               'url': 'https://img.example/k.png',
                               'format': 'png'}]
    txt2img.assert_called_once_with('kite at dusk, watercolour style')


def test_generate_media_reports_the_failed_image(txt2img, monkeypatch):
    from integrations.service_tools import media_agent
    monkeypatch.setattr(media_agent, '_can_do', lambda *a, **k: True)
    txt2img.return_value = ''
    out = json.loads(media_agent.generate_media('a red kite', 'image'))
    assert out['status'] == 'error', out
    assert out.get('error')


def test_the_failure_is_not_read_as_nothing_installed(txt2img):
    """The service exists and did not deliver; offering an install is wrong."""
    from integrations.service_tools.media_agent import (
        _generate_image, classify_error, ABSENT)
    txt2img.return_value = ''
    assert classify_error(_generate_image('x', '', '')) != ABSENT
