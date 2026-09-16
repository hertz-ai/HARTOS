"""integrations.vision.image_describe: the one local-VLM image describer.

Ported from Nunba's tests/test_vision_reasoning_budget.py and
tests/test_upload_routes.py::TestDescribeImageViaLlm when the function moved
here (2026-09-13). The thinking-off pins are why this file exists: see the
module docstring for the live measurements behind them.

    python -m pytest tests/unit/test_image_describe.py -q --noconftest
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.http_pool import LLM_COMPLETION_TIMEOUT
from integrations.vision import image_describe as vis

LLM_URL = 'http://127.0.0.1:8080/v1'


def _resp(content, finish='stop', status=200, reasoning=''):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {'choices': [{
        'finish_reason': finish,
        'message': {'content': content, 'reasoning_content': reasoning},
    }]}
    r.text = ''
    return r


@pytest.fixture
def img(tmp_path):
    p = tmp_path / 'x.png'
    p.write_bytes(bytes.fromhex(
        '89504e470d0a1a0a0000000d4948445200000001000000010802000000907753'
        'de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082'))
    return str(p)


def _call(image, response=None, side_effect=None, **kwargs):
    with patch('core.port_registry.get_local_llm_url', return_value=LLM_URL), \
         patch('core.http_pool.pooled_post', return_value=response,
               side_effect=side_effect) as post:
        out = vis.describe_image(image, **kwargs)
    return out, post


def test_request_asks_the_model_not_to_think(img):
    """THE fix. Verified live: enable_thinking=false took reasoning 760 -> 0."""
    _, post = _call(img, _resp('ok'))
    kw = post.call_args.kwargs['json'].get('chat_template_kwargs') or {}
    assert kw.get('enable_thinking') is False


def test_does_not_use_reasoning_effort_none(img):
    """Probed live and it does NOT suppress thinking on this server."""
    _, post = _call(img, _resp('ok'))
    assert post.call_args.kwargs['json'].get('reasoning_effort') != 'none'


def test_token_budget_has_headroom(img):
    _, post = _call(img, _resp('ok'))
    assert post.call_args.kwargs['json'].get('max_tokens', 0) >= 800


def test_truncated_empty_answer_warns_instead_of_failing_silently(img, caplog):
    """finish_reason='length' + empty content is the spent-budget signature."""
    out, _ = _call(img, _resp('', finish='length', reasoning='thinking...'))
    assert out in (None, '')
    joined = ' '.join(r.message for r in caplog.records).lower()
    assert 'empty' in joined or 'length' in joined


def test_normal_answer_is_returned_stripped(img):
    out, _ = _call(img, _resp('  a red square  '))
    assert out == 'a red square'


def test_non_200_returns_none(img):
    out, _ = _call(img, _resp('', status=500))
    assert out is None


def test_unreachable_model_returns_none(img):
    out, _ = _call(img, side_effect=requests.ConnectionError('refused'))
    assert out is None


def test_unreadable_image_returns_none_without_calling_the_model():
    out, post = _call('/no/such/image.jpg', _resp('ok'))
    assert out is None
    post.assert_not_called()


def test_custom_prompt_is_sent(img):
    _, post = _call(img, _resp('ok'), prompt='What colour is this?')
    parts = post.call_args.kwargs['json']['messages'][0]['content']
    assert parts[1] == {'type': 'text', 'text': 'What colour is this?'}


def test_image_goes_as_a_data_url_with_its_own_type(img):
    _, post = _call(img, _resp('ok'))
    part = post.call_args.kwargs['json']['messages'][0]['content'][0]
    assert part['image_url']['url'].startswith('data:image/png;base64,')


def test_it_uses_the_canonical_endpoint_and_the_scheduled_transport(img):
    """pooled_post (the priority scheduler) and get_local_llm_url, never a
    private URL, with the canonical completion timeout."""
    _, post = _call(img, _resp('ok'))
    assert post.call_args.args[0] == LLM_URL + '/chat/completions'
    assert post.call_args.kwargs['timeout'] == LLM_COMPLETION_TIMEOUT
