"""pooled_post must log a multimodal prompt's TEXT, never its image bytes.

THE DEFECT (read in core/http_pool.py, 2026-09-13): the observability line
built its preview as ``msgs[-1].get('content', '')[:200]``. For an
OpenAI-style multimodal message ``content`` is a LIST of parts, and slicing a
list keeps the whole list, so the INFO line embedded every image part
verbatim -- a full base64 ``data:`` URL. Book pages reach the vision model
through pooled_post (integrations.vision.image_describe), at ~110 KB of
base64 per 200-DPI page.

    python -m pytest tests/unit/test_http_pool_prompt_preview.py -q --noconftest
"""
import logging
import sys
import types
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from core import http_pool

_B64 = 'QUJD' * 5000
_IMAGE_PART = {'type': 'image_url',
               'image_url': {'url': 'data:image/jpeg;base64,' + _B64}}
_TEXT_PART = {'type': 'text', 'text': 'read this page'}


class TestPreview:

    def test_keeps_the_text_and_counts_the_image(self):
        out = http_pool._prompt_text_preview([_IMAGE_PART, _TEXT_PART])
        assert out == 'read this page [+1 non-text part(s)]'

    def test_never_contains_image_bytes(self):
        out = http_pool._prompt_text_preview([_IMAGE_PART, _IMAGE_PART])
        assert _B64[:40] not in out and 'base64' not in out
        assert out == '[+2 non-text part(s)]'

    def test_plain_string_is_unchanged_and_bounded(self):
        assert http_pool._prompt_text_preview('hello') == 'hello'
        assert len(http_pool._prompt_text_preview('x' * 1000)) == 200


def test_the_llm_log_line_carries_text_not_base64(caplog):
    """Through the real pooled_post: the line is still emitted, with the
    prompt's text and none of the image."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        'choices': [{'message': {'content': 'a page of text'}}],
        'usage': {'completion_tokens': 4},
    }
    session = MagicMock()
    session.post.return_value = resp
    scheduler = MagicMock()
    scheduler.slot.return_value = nullcontext()
    fake_scheduler_module = types.SimpleNamespace(get_scheduler=lambda: scheduler)
    body = {'messages': [{'role': 'user', 'content': [_IMAGE_PART, _TEXT_PART]}]}

    caplog.set_level(logging.INFO, logger='hevolve_core')
    with patch.object(http_pool, '_is_llama_completion_url', return_value=True), \
         patch.object(http_pool, '_classify_llama_call', return_value=('', 'user')), \
         patch.object(http_pool, '_llama_session_for', return_value=session), \
         patch.dict(sys.modules, {'core.llama_scheduler': fake_scheduler_module}):
        out = http_pool.pooled_post(
            'http://127.0.0.1:8080/v1/chat/completions', json=body)

    assert out is resp
    lines = [r.getMessage() for r in caplog.records if '[LLM] IN:' in r.getMessage()]
    assert lines, 'the observability line must still be emitted'
    assert 'read this page' in lines[0]
    assert _B64[:40] not in lines[0] and 'base64' not in lines[0]
