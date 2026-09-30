"""core/teacher_avatar.py: the one avatar lookup, shared by the Generate_video
tool and the spoken reply.

The lookup moved out of Generate_video unchanged.  The Generate_video tests
below pin the exact body it posts to /video_generate_save (key order included,
so the JSON is byte-identical apart from the random uid) in the cases the old
inline code distinguished, so the move cannot have changed what a video gets.

    python -m pytest tests/unit/test_teacher_avatar.py -q
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core import teacher_avatar as ta

DB = 'http://db.test'


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _fake_db(image=None, voice=None, image_exc=None, voice_exc=None):
    """A pooled_get stand-in for central's two routes; records every URL."""
    calls = []

    def fake(url, *args, **kwargs):
        calls.append(url)
        if '/get_image_by_id/' in url:
            if image_exc:
                raise image_exc
            return _Resp(image)
        if '/get_voice_sample_id/' in url:
            if voice_exc:
                raise voice_exc
            return _Resp(voice)
        raise AssertionError(f'unexpected URL {url}')
    return fake, calls


# ── avatar_id_from ────────────────────────────────────────────────────

@pytest.mark.parametrize('value, expected', [
    (5, 5),
    ('12', 12),
    (' 7 ', 7),
    (ta.MAX_AVATAR_ID, ta.MAX_AVATAR_ID),
    (ta.MAX_AVATAR_ID + 1, None),   # past Android's Java Integer
    (0, None),
    ('0', None),
    (-1, None),                     # Android's "no avatar" extra default
    ('-1', None),
    (True, None),                   # bool is an int subclass; never an id
    (False, None),
    (None, None),
    ('', None),
    ('abc', None),
    ('\u00b2', None),               # '²'.isdigit() is True; int() rejects it
    ('\u0661\u0662', None),         # Arabic-Indic digits: not a wire id
    (3.0, None),
    ([5], None),
])
def test_avatar_id_from(value, expected):
    assert ta.avatar_id_from(value) == expected


def test_avatar_id_from_rejects_a_mock():
    """_chat_reply runs with a MagicMock flask in test_consent_fanout_p2;
    the mock's .get() result must not pass as an avatar id."""
    assert ta.avatar_id_from(MagicMock()) is None


# ── lookup_avatar ─────────────────────────────────────────────────────

def test_lookup_image_and_voice():
    fake, calls = _fake_db(image={'image_url': 'http://img/a.png', 'voice_id': 9},
                           voice={'voice_sample_url': '/uploads/audio/v.wav'})
    with patch.object(ta, 'pooled_get', fake):
        got = ta.lookup_avatar(5, DB)
    assert got == {'image_url': 'http://img/a.png', 'voice_id': 9,
                   'audio_sample_url': '/uploads/audio/v.wav', 'openvoice': False}
    assert calls == [f'{DB}/get_image_by_id/5', f'{DB}/get_voice_sample_id/9']


@pytest.mark.parametrize('image, image_exc', [
    (None, None),                     # central answers `null` for unknown/inactive
    ({'voice_id': 9}, None),          # no image_url key
    (None, ConnectionError('down')),  # unreachable
])
def test_lookup_image_failure_is_openvoice_and_skips_voice(image, image_exc):
    fake, calls = _fake_db(image=image, image_exc=image_exc)
    with patch.object(ta, 'pooled_get', fake):
        got = ta.lookup_avatar(5, DB)
    assert got == {'image_url': None, 'voice_id': None,
                   'audio_sample_url': None, 'openvoice': True}
    assert calls == [f'{DB}/get_image_by_id/5']


def test_lookup_avatar_without_voice():
    fake, calls = _fake_db(image={'image_url': 'u', 'voice_id': None})
    with patch.object(ta, 'pooled_get', fake):
        got = ta.lookup_avatar(5, DB)
    assert got == {'image_url': 'u', 'voice_id': None,
                   'audio_sample_url': None, 'openvoice': False}
    assert len(calls) == 1


def test_lookup_voice_id_zero_keeps_url_but_not_id():
    """The old inline code looked the sample up for any voice_id that was
    not None, then posted `int(voice_id) if voice_id else None`."""
    fake, _ = _fake_db(image={'image_url': 'u', 'voice_id': 0},
                       voice={'voice_sample_url': 'http://v/0.wav'})
    with patch.object(ta, 'pooled_get', fake):
        got = ta.lookup_avatar(5, DB)
    assert got['audio_sample_url'] == 'http://v/0.wav'
    assert got['voice_id'] is None


@pytest.mark.parametrize('voice, voice_exc', [
    (None, None),                    # `null` sample
    (None, TimeoutError('slow')),
])
def test_lookup_voice_failure_drops_both(voice, voice_exc):
    fake, _ = _fake_db(image={'image_url': 'u', 'voice_id': 9},
                       voice=voice, voice_exc=voice_exc)
    with patch.object(ta, 'pooled_get', fake):
        got = ta.lookup_avatar(5, DB)
    assert got == {'image_url': 'u', 'voice_id': None,
                   'audio_sample_url': None, 'openvoice': False}


def test_lookup_defaults_to_the_nodes_database():
    fake, calls = _fake_db(image={'image_url': 'u', 'voice_id': None})
    with patch.object(ta, 'pooled_get', fake), \
            patch('core.config_cache.get_db_url', return_value='http://local.test'):
        ta.lookup_avatar(3)
    assert calls == ['http://local.test/get_image_by_id/3']


# ── voice_reference ───────────────────────────────────────────────────

def _avatar(url):
    return {'image_url': 'u', 'voice_id': 9, 'audio_sample_url': url,
            'openvoice': False}


def test_voice_reference_local_upload(tmp_path):
    uploads = tmp_path / 'uploads'
    (uploads / 'audio').mkdir(parents=True)
    wav = uploads / 'audio' / 'v.wav'
    wav.write_bytes(b'RIFF')
    with patch.object(ta, 'lookup_avatar', return_value=_avatar('/uploads/audio/v.wav')), \
            patch('integrations.learning.book_pipeline.uploads_dir', return_value=uploads):
        assert ta.voice_reference(5) == str(wav.resolve())


def test_voice_reference_missing_upload_is_none(tmp_path):
    with patch.object(ta, 'lookup_avatar', return_value=_avatar('/uploads/audio/gone.wav')), \
            patch('integrations.learning.book_pipeline.uploads_dir', return_value=tmp_path):
        assert ta.voice_reference(5) is None


def test_voice_reference_remote_sample_goes_through_download_asset():
    url = 'http://voices.example/examples/v.wav'
    with patch.object(ta, 'lookup_avatar', return_value=_avatar(url)), \
            patch('integrations.agent_engine.video_orchestrator.download_asset',
                  return_value='/cache/audio/abc.wav') as dl:
        assert ta.voice_reference(5) == '/cache/audio/abc.wav'
    dl.assert_called_once_with(url, 'audio')


@pytest.mark.parametrize('url', [None, '', 'file:///etc/passwd',
                                 r'C:\Users\x\voice.wav', 'uploads/audio/v.wav'])
def test_voice_reference_anything_else_is_none(url):
    with patch.object(ta, 'lookup_avatar', return_value=_avatar(url)), \
            patch('integrations.agent_engine.video_orchestrator.download_asset') as dl:
        assert ta.voice_reference(5) is None
    dl.assert_not_called()


# ── Generate_video posts exactly what it posted before the move ──────

def _generate_video():
    from core import agent_tools
    ctx = {
        'user_id': 'u1', 'prompt_id': 'p1', 'agent_data': {},
        'helper_fun': MagicMock(), 'user_prompt': 'u1_p1',
        'request_id_list': {}, 'recent_file_id': {}, 'scheduler': MagicMock(),
        'send_message_to_user1': MagicMock(), 'retrieve_json': MagicMock(),
        'strip_json_values': MagicMock(),
        'save_conversation_db': MagicMock(return_value=77),
        'log_tool_execution': lambda f: f,
    }
    tools = agent_tools.build_core_tool_closures(ctx)
    return next(fn for name, _desc, fn in tools if name == 'Generate_video')


def _posted_body(db_fake, realtime):
    generate = _generate_video()
    posted = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        posted['url'] = url
        posted['body'] = json.loads(data)
    with patch.object(ta, 'pooled_get', db_fake), \
            patch('core.agent_tools.pooled_post', fake_post), \
            patch('core.config_cache.get_db_url', return_value=DB):
        generate(text='hello there', avatar_id=5, realtime=realtime)
    assert posted['url'] == f'{DB}/video_generate_save'
    body = posted['body']
    assert body.pop('uid')  # random per call
    return body


_KEY_ORDER = [
    'text', 'flag_hallo', 'chattts', 'openvoice', 'cartoon_image', 'bg_url',
    'vtoonify', 'image_url', 'im_crop', 'remove_bg', 'hd_video', 'gradient',
    'cus_bg', 'solid_color', 'inpainting', 'prompt', 'gender',
    'audio_sample_url', 'voice_id', 'conv_id', 'avatar_id', 'timeout',
]


def test_generate_video_body_with_image_and_voice():
    fake, _ = _fake_db(image={'image_url': 'http://img/a.png', 'voice_id': 9},
                       voice={'voice_sample_url': 'http://v/9.wav'})
    body = _posted_body(fake, realtime=True)
    assert list(body) == _KEY_ORDER
    assert body == {
        'text': 'hello there', 'flag_hallo': 'false', 'chattts': False,
        'openvoice': 'false', 'cartoon_image': 'True',
        'bg_url': 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg',
        'vtoonify': 'false', 'image_url': 'http://img/a.png', 'im_crop': 'false',
        'remove_bg': 'false', 'hd_video': 'false', 'gradient': 'true',
        'cus_bg': 'false', 'solid_color': 'false', 'inpainting': 'false',
        'prompt': '', 'gender': 'male', 'audio_sample_url': 'http://v/9.wav',
        'voice_id': 9, 'conv_id': 77, 'avatar_id': 5, 'timeout': 60,
    }


def test_generate_video_body_when_the_avatar_lookup_fails():
    fake, _ = _fake_db(image_exc=ConnectionError('down'))
    body = _posted_body(fake, realtime=False)
    assert list(body) == _KEY_ORDER
    assert body['openvoice'] == 'true'
    assert body['image_url'] is None
    assert body['audio_sample_url'] is None and body['voice_id'] is None
    assert body['chattts'] is True and body['flag_hallo'] == 'true'
    assert body['cartoon_image'] == 'False' and body['timeout'] == 600


def test_generate_video_has_no_second_lookup():
    """One lookup: Generate_video must not grow its own copy back."""
    src = Path(ta.__file__).with_name('agent_tools.py').read_text(encoding='utf-8')
    assert 'get_image_by_id' not in src
    assert 'get_voice_sample_id' not in src
    assert 'lookup_avatar(avatar_id, database_url)' in src


def test_a_reply_voice_comes_only_from_its_avatar():
    """The per-agent `voice` read from the agent's JSON onto flask `g`
    (01bbeaf70) was a second voice source beside the avatar; it is gone and
    must not come back."""
    hie = Path(ta.__file__).resolve().parents[1] / 'hart_intelligence_entry.py'
    assert 'agent_voice' not in hie.read_text(encoding='utf-8')
