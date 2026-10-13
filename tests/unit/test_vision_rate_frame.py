"""VisionBackend.rate_frame: one keyframe judged by this node's vision model.

Reel Studio scores at most 25 keyframes per reel with a rubric prompt
(integrations/reel_studio/media/moments.py).  rate_frame is the one call it
makes per frame.  Its contract:

* it never starts a server: the caption backend's lazy launch
  (_ensure_running -> vlm_caption.requested -> a llama-server spawn) is for
  the live captioner, and a tool that rates frames must not spawn one;
* it asks GET /health first and answers None when the server is not up;
* the request is the shared vision transport's (_vision_complete), with the
  frame at a 768 px long side, temperature 0, thinking off, an 80-token
  answer budget, and the per-call timeout the caller passed;
* it never raises, and it is not one of the auto-reported methods: a frame
  the model cannot rate falls back, it is not a subsystem failure.

The page reader (read_document) now goes through the same transport; its
request must be exactly what it was.

Only the HTTP layer (pooled_get / pooled_post) is mocked.
"""
import base64
import io
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import integrations.vision.lightweight_backend as lvb
from core.http_pool import LLM_COMPLETION_TIMEOUT
from tests.conftest import refuse_all_network


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    refuse_all_network(monkeypatch, reason='test_vision_rate_frame: HTTP is mocked')


def _jpeg(size=(1080, 1920), color=(200, 150, 120)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def _answer(content='{"stage": "eyes", "appeal": 7, "face_visible": true}',
            finish='stop', status=200):
    resp = MagicMock(status_code=status)
    resp.json.return_value = {'choices': [{'message': {'content': content},
                                           'finish_reason': finish}]}
    resp.text = content
    return resp


def _health(status=200):
    return MagicMock(status_code=status)


def _sent_image(post):
    from PIL import Image
    url = post.call_args.kwargs['json']['messages'][0]['content'][0]['image_url']['url']
    assert url.startswith('data:image/jpeg;base64,')
    return Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1])))


PROMPT = 'Rate this frame. Answer with JSON only.'


# ── the caption server (Qwen3.5-0.8B) ─────────────────────────────────────

def test_the_caption_server_rates_a_portrait_frame_at_768_px():
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(lvb, 'pooled_get', return_value=_health()) as get, \
         patch.object(lvb, 'pooled_post', return_value=_answer()) as post:
        out = backend.rate_frame(_jpeg((1080, 1920)), PROMPT, timeout_s=7.5)
    assert out == '{"stage": "eyes", "appeal": 7, "face_visible": true}'
    # Health first, on the caption server's own port, with a short bound.
    assert get.call_args.args[0] == 'http://127.0.0.1:9555/health'
    assert get.call_args.kwargs['timeout'] == lvb.HEALTH_TIMEOUT == (1, 2)
    # Then one completion on the same server.
    assert post.call_count == 1
    assert post.call_args.args[0] == 'http://127.0.0.1:9555/v1/chat/completions'
    body = post.call_args.kwargs['json']
    assert body['max_tokens'] == 80
    assert body['temperature'] == 0
    assert body['chat_template_kwargs'] == {'enable_thinking': False}
    assert body['messages'][0]['content'][1] == {'type': 'text', 'text': PROMPT}
    # The caller's cap is the request's timeout, not the 120 s completion budget.
    assert post.call_args.kwargs['timeout'] == 7.5
    # Aspect kept: 1080x1920 goes as 432x768, not the caption shrink (512x288).
    assert _sent_image(post).size == (432, 768)


def test_a_landscape_frame_keeps_its_aspect_and_a_small_one_is_not_enlarged():
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(lvb, 'pooled_get', return_value=_health()), \
         patch.object(lvb, 'pooled_post', return_value=_answer()) as post:
        backend.rate_frame(_jpeg((1920, 1080)), PROMPT)
        assert _sent_image(post).size == (768, 432)
        backend.rate_frame(_jpeg((320, 240)), PROMPT, long_side=768)
        assert _sent_image(post).size == (320, 240)
        backend.rate_frame(_jpeg((1080, 1920)), PROMPT, long_side=512, max_tokens=40)
        assert _sent_image(post).size == (288, 512)
        assert post.call_args.kwargs['json']['max_tokens'] == 40
        # The defaults the signature promises: 20 s, 80 tokens.
        backend.rate_frame(_jpeg(), PROMPT)
        assert post.call_args.kwargs['timeout'] == 20.0
        assert post.call_args.kwargs['json']['max_tokens'] == 80


@pytest.mark.parametrize('health', [
    dict(side_effect=requests.ConnectionError('refused')),     # nothing listening
    dict(return_value=_health(503)),                          # model still loading
    dict(side_effect=requests.Timeout('slow')),               # wedged
])
def test_a_server_that_is_not_serving_is_never_started(health):
    """describe() lazy-starts the caption server.  rate_frame copying that
    would spawn a llama-server from inside a reel tool."""
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(lvb, 'pooled_get', **health), \
         patch.object(lvb, 'pooled_post') as post, \
         patch.object(lvb.Qwen08BBackend, '_ensure_running') as launch, \
         patch('subprocess.Popen') as popen, \
         patch('core.platform.events.emit_event') as emit:
        assert backend.rate_frame(_jpeg(), PROMPT, timeout_s=5.0) is None
    launch.assert_not_called()
    popen.assert_not_called()
    emit.assert_not_called()
    post.assert_not_called()


def test_no_time_left_means_no_request():
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(lvb, 'pooled_get', return_value=_health()) as get, \
         patch.object(lvb, 'pooled_post') as post:
        for spent in (0, -1.0, float('nan'), float('inf'), None, 'soon'):
            assert backend.rate_frame(_jpeg(), PROMPT, timeout_s=spent) is None
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize('answer,expected', [
    (dict(content='  {"stage": "lips"}  '), '{"stage": "lips"}'),
    (dict(content=''), ''),                                   # answered with nothing
    (dict(content='{"stage": "li', finish='length'), '{"stage": "li'),
    (dict(status=500), None),
])
def test_what_the_server_answered_is_what_comes_back(answer, expected):
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(lvb, 'pooled_get', return_value=_health()), \
         patch.object(lvb, 'pooled_post', return_value=_answer(**answer)):
        assert backend.rate_frame(_jpeg(), PROMPT) == expected


def test_failures_are_none_never_an_exception():
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(lvb, 'pooled_get', return_value=_health()):
        with patch.object(lvb, 'pooled_post', side_effect=requests.ConnectionError('gone')):
            assert backend.rate_frame(_jpeg(), PROMPT) is None
        with patch.object(lvb, 'pooled_post', side_effect=RuntimeError('scheduler broke')):
            assert backend.rate_frame(_jpeg(), PROMPT) is None
        bad = MagicMock(status_code=200)
        bad.json.side_effect = ValueError('not json')
        with patch.object(lvb, 'pooled_post', return_value=bad):
            assert backend.rate_frame(_jpeg(), PROMPT) is None
        with patch.object(lvb, 'pooled_post') as post:
            assert backend.rate_frame(b'not an image', PROMPT) is None
            post.assert_not_called()


# ── the main model's endpoint (Qwen3-VL backend) ──────────────────────────

def _qwen3vl(base='http://127.0.0.1:8080/v1'):
    backend = lvb.Qwen3VLVisionBackend()
    backend._backend = MagicMock(base_url=base, model_name='qwen-vl', api_key='k')
    return backend


def test_qwen3vl_rates_through_its_own_endpoint_after_a_health_probe():
    backend = _qwen3vl('http://127.0.0.1:8080/v1/')
    with patch.object(lvb, 'pooled_get', return_value=_health()) as get, \
         patch.object(lvb, 'pooled_post', return_value=_answer('{"stage": "base"}')) as post:
        assert backend.rate_frame(_jpeg(), PROMPT, timeout_s=12.0) == '{"stage": "base"}'
    assert get.call_args.args[0] == 'http://127.0.0.1:8080/health'
    assert get.call_args.kwargs['timeout'] == lvb.HEALTH_TIMEOUT
    assert post.call_args.args[0] == 'http://127.0.0.1:8080/v1/chat/completions'
    assert post.call_args.kwargs['headers'] == {'Authorization': 'Bearer k'}
    assert post.call_args.kwargs['timeout'] == 12.0
    body = post.call_args.kwargs['json']
    assert body['model'] == 'qwen-vl'
    assert body['temperature'] == 0
    assert body['max_tokens'] == 80
    assert body['chat_template_kwargs'] == {'enable_thinking': False}
    assert _sent_image(post).size == (432, 768)


def test_qwen3vl_that_is_down_is_none_and_sends_nothing():
    backend = _qwen3vl()
    with patch.object(lvb, 'pooled_get', side_effect=requests.ConnectionError('refused')), \
         patch.object(lvb, 'pooled_post') as post:
        assert backend.rate_frame(_jpeg(), PROMPT) is None
    post.assert_not_called()


def test_qwen3vl_whose_endpoint_cannot_be_built_is_none():
    backend = lvb.Qwen3VLVisionBackend()
    with patch('integrations.vlm.qwen3vl_backend.get_qwen3vl_backend',
               side_effect=RuntimeError('no endpoint')), \
         patch.object(lvb, 'pooled_get') as get, \
         patch.object(lvb, 'pooled_post') as post:
        assert backend.rate_frame(_jpeg(), PROMPT) is None
    get.assert_not_called()
    post.assert_not_called()


def test_qwen3vl_with_a_broken_endpoint_record_is_none_not_an_exception():
    backend = lvb.Qwen3VLVisionBackend()
    backend._backend = MagicMock(base_url=None, model_name='m', api_key='k')
    with patch.object(lvb, 'pooled_get') as get, patch.object(lvb, 'pooled_post') as post:
        assert backend.rate_frame(_jpeg(), PROMPT) is None
    get.assert_not_called()
    post.assert_not_called()


# ── backends that cannot rate a frame ─────────────────────────────────────

@pytest.mark.parametrize('cls', [lvb.MiniCPMBackend, lvb.MobileVLMBackend,
                                 lvb.CLIPBackend, lvb.NoneBackend])
def test_a_backend_that_cannot_rate_a_frame_says_none_without_asking_anyone(cls):
    with patch.object(lvb, 'pooled_get') as get, patch.object(lvb, 'pooled_post') as post:
        assert cls().rate_frame(_jpeg(), PROMPT) is None
    get.assert_not_called()
    post.assert_not_called()


# ── not a self-heal surface ───────────────────────────────────────────────

def test_rate_frame_is_not_auto_reported_while_describe_is():
    """A frame the model cannot rate falls back to a deterministic score; it
    is not a subsystem failure.  So a rate_frame that raises reaches the
    caller untouched and nothing is reported, while describe(), which IS
    auto-reported, reports its failure (the control half of this test)."""

    class Raising(lvb.NoneBackend):
        def describe(self, frame_bytes, prompt=''):
            raise RuntimeError('describe failed')

        def rate_frame(self, image_bytes, prompt, **kw):
            raise RuntimeError('rating failed')

    with patch('hartos.exception_collector.report_subsystem_failure') as report:
        with pytest.raises(RuntimeError, match='describe failed'):
            Raising().describe(b'x')
        assert report.call_count == 1
        with pytest.raises(RuntimeError, match='rating failed'):
            Raising().rate_frame(b'x', PROMPT)
        assert report.call_count == 1
    assert 'rate_frame' not in lvb.VisionBackend.AUTO_REPORTED_METHODS


def test_a_parse_miss_does_not_reach_the_auto_report_hook(tmp_path):
    """The model answers prose; moments falls back for that frame.  Nothing
    reaches the self-heal pipeline."""
    from integrations.reel_studio.contracts import Candidate, Quality
    from integrations.reel_studio.media import moments

    keyframe = Candidate(clip='c' * 64, t_s=1.0, segment=0, jpeg=_jpeg((432, 768)),
                         quality=Quality(score=0.5, sharp=50.0, luma=0.5, clipped=0.0))
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(lvb, 'pooled_get', return_value=_health()), \
         patch.object(lvb, 'pooled_post',
                      return_value=_answer('I think this is a lovely bride.')) as post, \
         patch('hartos.exception_collector.report_subsystem_failure') as report, \
         patch('hartos.exception_collector.ExceptionCollector.record') as record:
        doc = moments.score_moments([keyframe], backend=backend, cache_dir=str(tmp_path))
    assert post.call_count == 1
    assert doc['moments'][0]['src'] == 'fallback'
    report.assert_not_called()
    record.assert_not_called()


# ── the page reader is unchanged by the shared transport ─────────────────

def _page(size=(1700, 2200)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, 'white').save(buf, 'JPEG')
    return buf.getvalue()


def _expected_page_body(page, prompt, model):
    """read_document's request as it was before rate_frame existed: the page
    at a 1280 px long side (LANCZOS, JPEG q85), image first, thinking off,
    4096 tokens, temperature 0.3."""
    from PIL import Image
    img = Image.open(io.BytesIO(page))
    scale = 1280 / max(img.size)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'JPEG', quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return {
        'model': model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            {'type': 'text', 'text': prompt},
        ]}],
        'chat_template_kwargs': {'enable_thinking': False},
        'max_tokens': 4096,
        'temperature': 0.3,
    }


def test_read_document_sends_the_same_bytes_as_before():
    page = _page()
    backend = lvb.Qwen08BBackend(port=9555)
    with patch.object(backend, '_ensure_running', return_value=True), \
         patch.object(lvb, 'pooled_post', return_value=_answer('page text')) as post:
        assert backend.read_document(page, 'read every line') == 'page text'
    assert post.call_args.args == ('http://127.0.0.1:9555/v1/chat/completions',)
    assert set(post.call_args.kwargs) == {'json', 'timeout'}
    assert post.call_args.kwargs['timeout'] == LLM_COMPLETION_TIMEOUT
    assert json.dumps(post.call_args.kwargs['json']) == json.dumps(
        _expected_page_body(page, 'read every line', 'local'))


def test_read_document_on_qwen3vl_sends_the_same_bytes_as_before():
    page = _page((2400, 1600))
    backend = _qwen3vl('http://10.0.0.5:7000/v1/')
    with patch.object(lvb, 'pooled_post', return_value=_answer('p')) as post, \
         patch.object(lvb, 'pooled_get') as get:
        assert backend.read_document(page, 'x') == 'p'
    get.assert_not_called()               # pages keep their old path: no health probe
    assert post.call_args.args == ('http://10.0.0.5:7000/v1/chat/completions',)
    assert set(post.call_args.kwargs) == {'json', 'timeout', 'headers'}
    assert post.call_args.kwargs['headers'] == {'Authorization': 'Bearer k'}
    assert post.call_args.kwargs['timeout'] == LLM_COMPLETION_TIMEOUT
    assert json.dumps(post.call_args.kwargs['json']) == json.dumps(
        _expected_page_body(page, 'x', 'qwen-vl'))


def test_the_main_model_page_reader_sends_the_same_bytes_as_before():
    page = _page()
    node = lvb.NoneBackend()
    with patch.object(lvb, 'get_vision_backend', return_value=node), \
         patch('core.port_registry.get_local_llm_url', return_value='http://127.0.0.1:8123/v1'):
        readers = lvb.get_document_readers()
    with patch.object(lvb, 'pooled_post', return_value=_answer('p')) as post:
        assert readers[0](page, 'x') == 'p'
    assert post.call_args.args == ('http://127.0.0.1:8123/v1/chat/completions',)
    assert set(post.call_args.kwargs) == {'json', 'timeout'}
    assert json.dumps(post.call_args.kwargs['json']) == json.dumps(
        _expected_page_body(page, 'x', 'qwen'))
