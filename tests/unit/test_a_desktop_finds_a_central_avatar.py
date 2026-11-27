"""A desktop finds an avatar that exists only on central (owner's list, item 9, 27 Nov).

A desktop's database is its local Nunba store (bundled get_db_url), which holds only the avatars made
on it.  A phone's agent carries a CENTRAL avatar id, so the face (and the cloned voice) of every phone
agent was "no portrait here" on a desktop.  lookup_avatar now asks central when this node finds no
image, on the default path only; a caller that names its store is answered from it alone, as before.
"""
from unittest.mock import patch

import pytest

from core import teacher_avatar as ta

LOCAL = 'http://127.0.0.1:5000'
CENTRAL = 'https://central.test/db'


@pytest.fixture(autouse=True)
def fresh_lookups(monkeypatch):
    """lookup_avatar remembers default-path answers (rv_face2 B): each test starts with none."""
    monkeypatch.setattr(ta, '_lookups', {})


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _stores(local_image=None, central_image=None, central_voice=None, central_down=False):
    calls = []

    def fake(url, *a, **k):
        calls.append(url)
        if url.startswith(CENTRAL):
            if central_down:
                raise ConnectionError('central down')
            if '/get_image_by_id/' in url:
                return _Resp(central_image)
            return _Resp(central_voice)
        if '/get_image_by_id/' in url:
            return _Resp(local_image)
        return _Resp(None)
    return fake, calls


def _lookup(fake, avatar_id=1802, central=CENTRAL, **kw):
    with patch.object(ta, 'pooled_get', fake), \
            patch('core.config_cache.get_db_url', return_value=LOCAL), \
            patch('core.config_cache.get_central_db_url', return_value=central):
        return ta.lookup_avatar(avatar_id, **kw)


def test_a_central_avatar_is_found_on_a_desktop_with_its_voice():
    fake, calls = _stores(local_image=None,
                          central_image={'image_url': 'https://cdn.test/a.png', 'voice_id': 733},
                          central_voice={'voice_sample_url': 'https://cdn.test/v.wav'})
    got = _lookup(fake)
    assert got == {'image_url': 'https://cdn.test/a.png', 'voice_id': 733,
                   'audio_sample_url': 'https://cdn.test/v.wav', 'openvoice': False}
    assert calls == [f'{LOCAL}/get_image_by_id/1802', f'{CENTRAL}/get_image_by_id/1802',
                     f'{CENTRAL}/get_voice_sample_id/733']


def test_a_local_avatar_never_asks_central():
    fake, calls = _stores(local_image={'image_url': '/uploads/avatars/a.png', 'voice_id': None})
    got = _lookup(fake, avatar_id=1000000003)
    assert got['image_url'] == '/uploads/avatars/a.png'
    assert calls == [f'{LOCAL}/get_image_by_id/1000000003']


def test_a_named_store_is_the_only_one_asked():
    """Generate_video names its store: unchanged, no central fallback."""
    fake, calls = _stores(local_image=None, central_image={'image_url': 'x', 'voice_id': None})
    got = _lookup(fake, database_url=LOCAL)
    assert got['image_url'] is None and got['openvoice'] is True
    assert calls == [f'{LOCAL}/get_image_by_id/1802']


def test_unknown_everywhere_is_the_old_answer():
    fake, calls = _stores(local_image=None, central_image=None)
    got = _lookup(fake)
    assert got == {'image_url': None, 'voice_id': None, 'audio_sample_url': None, 'openvoice': True}
    assert len(calls) == 2


def test_central_down_or_switched_off_is_the_old_answer():
    fake, _calls = _stores(local_image=None, central_down=True)
    assert _lookup(fake)['openvoice'] is True
    fake, calls = _stores(local_image=None, central_image={'image_url': 'x', 'voice_id': None})
    assert _lookup(fake, central='')['image_url'] is None  # '' = no central (operator override)
    assert calls == [f'{LOCAL}/get_image_by_id/1802']


def test_on_central_the_store_is_asked_once():
    fake, calls = _stores(local_image=None)
    with patch.object(ta, 'pooled_get', fake), \
            patch('core.config_cache.get_db_url', return_value=CENTRAL), \
            patch('core.config_cache.get_central_db_url', return_value=CENTRAL + '/'):
        ta.lookup_avatar(5)
    assert calls == [f'{CENTRAL}/get_image_by_id/5']


def test_a_local_id_this_node_lost_is_never_asked_of_central():
    """rv_face2 B: a Nunba node's own ids (>= LOCAL_AVATAR_ID_BASE) are ones central never issues."""
    fake, calls = _stores(local_image=None, central_image={'image_url': 'x', 'voice_id': None})
    assert _lookup(fake, avatar_id=ta.LOCAL_AVATAR_ID_BASE + 7)['image_url'] is None
    assert calls == [f'{LOCAL}/get_image_by_id/{ta.LOCAL_AVATAR_ID_BASE + 7}']


def test_a_spoken_reply_does_not_ask_the_stores_again_on_every_turn():
    """rv_face2 B: voice_reference runs on every reply; a found avatar is remembered LOOKUP_TTL_S."""
    fake, calls = _stores(local_image=None,
                          central_image={'image_url': 'https://cdn.test/a.png', 'voice_id': None})
    for _ in range(5):
        assert _lookup(fake)['image_url'] == 'https://cdn.test/a.png'
    assert len(calls) == 2  # local miss + central hit, once

