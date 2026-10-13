"""integrations.reel_studio.voice_profile: her voice profile, the one writer.

Stored per user at get_agent_data_dir()/reel_studio/<uid>/voice.json,
encrypted at rest through security.crypto.encrypt_json_file and written
atomically.  Fields come from igkit/resources/voice_template.json (derived from
instagram-agent-skill's voice.md, MIT); anything else is refused.
voice_brief() is what reel_skill puts in front of a skill text, so it is
bounded at 1,200 characters.
"""
import os
import threading

import pytest
from cryptography.fernet import Fernet

XID = 'ab' * 32


@pytest.fixture
def vp(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HEVOLVE_DATA_KEY', Fernet.generate_key().decode())
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.reel_studio import voice_profile
    yield voice_profile
    platform_paths.reset_cache()


def _file(tmp_path, uid):
    return tmp_path / 'data' / 'agent_data' / 'reel_studio' / uid / 'voice.json'


def test_round_trip(vp):
    vp.save_voice('u1', {
        'name': 'Meena Studio', 'city': 'Madurai', 'caption_language': 'ta', 'code_mix': True,
        'hashtag_bank': ['#maduraimua', 'bridalmakeup', '#மணப்பெண்'], 'emoji': ['✨', '💄'],
        'wpm': 150, 'logo_corner': 'top_right', 'style_notes': 'soft glam, no heavy contour',
    })
    got = vp.load_voice('u1')
    assert got == {
        'name': 'Meena Studio', 'city': 'Madurai', 'caption_language': 'ta', 'code_mix': True,
        'hashtag_bank': ['#maduraimua', '#bridalmakeup', '#மணப்பெண்'], 'emoji': ['✨', '💄'],
        'wpm': 150, 'logo_corner': 'top_right', 'style_notes': 'soft glam, no heavy contour',
    }


def test_encrypted_on_disk(vp, tmp_path):
    vp.save_voice('u1', {'city': 'Madurai', 'style_notes': 'soft glam'})
    raw = _file(tmp_path, 'u1').read_bytes()
    assert raw.startswith(b'gAAAAA')
    assert b'Madurai' not in raw and b'soft glam' not in raw


def test_written_atomically_with_no_leftovers(vp, tmp_path):
    for i in range(3):
        vp.update_field('u1', 'city', 'City %d' % i)
    assert os.listdir(_file(tmp_path, 'u1').parent) == ['voice.json']


def test_users_are_isolated(vp):
    vp.update_field('u1', 'city', 'Madurai')
    vp.update_field('u2', 'city', 'Chennai')
    assert vp.load_voice('u1') == {'city': 'Madurai'}
    assert vp.load_voice('u2') == {'city': 'Chennai'}
    assert vp.load_voice('u3') == {}


def test_an_unknown_field_is_refused(vp):
    with pytest.raises(ValueError, match='password'):
        vp.update_field('u1', 'password', 'x')
    with pytest.raises(ValueError):
        vp.save_voice('u1', {'city': 'Madurai', 'password': 'x'})
    assert vp.load_voice('u1') == {}


@pytest.mark.parametrize('field,value', [
    ('wpm', 'fast'), ('wpm', 20), ('logo_corner', 'middle'), ('caption_language', 'fr'),
    ('logo_xid', 'abc'), ('logo_xid', 'AB' * 32), ('city', 'x' * 500), ('code_mix', 'maybe'),
    ('hashtag_bank', ['#t%d' % i for i in range(16)]), ('hashtag_bank', ['#two words']),
    ('emoji', ['abc']), ('contact', 'telegram'),
])
def test_a_bad_value_is_refused(vp, field, value):
    with pytest.raises(ValueError):
        vp.update_field('u1', field, value)
    assert vp.load_voice('u1') == {}


def test_values_given_as_text_are_read(vp):
    vp.update_field('u1', 'wpm', '170')
    vp.update_field('u1', 'code_mix', 'yes')
    vp.update_field('u1', 'hashtag_bank', '#maduraimua, bridalmakeup\n#MaduraiMUA')
    vp.update_field('u1', 'caption_language', 'TA')
    assert vp.load_voice('u1') == {'wpm': 170, 'code_mix': True, 'caption_language': 'ta',
                                   'hashtag_bank': ['#maduraimua', '#bridalmakeup']}


def test_the_logo_xid_is_stored(vp):
    profile = vp.update_field('u1', 'logo_xid', XID)
    assert profile['logo_xid'] == XID
    assert vp.load_voice('u1')['logo_xid'] == XID


def test_an_empty_value_clears_the_field(vp):
    vp.update_field('u1', 'city', 'Madurai')
    assert vp.update_field('u1', 'city', '') == {}
    assert vp.load_voice('u1') == {}


@pytest.mark.parametrize('bad', ['../x', 'a/b', 'a\\b', '', '..', None, 'x' * 200])
def test_an_unsafe_user_id_is_refused(vp, bad):
    with pytest.raises(ValueError):
        vp.load_voice(bad)


def test_concurrent_updates_keep_every_field(vp):
    fields = ['name', 'city', 'tone', 'cta', 'style_notes', 'audience', 'services', 'price_band']
    threads = [threading.Thread(target=vp.update_field, args=('u1', f, f + ' value'))
               for f in fields]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert vp.load_voice('u1') == {f: f + ' value' for f in fields}


def _max_value(spec, i):
    kind = spec['type']
    if kind == 'text':
        return 'x' * spec['max_len']
    if kind == 'list':
        return [('%02d' % n) + 'y' * (spec['item_max_len'] - 2) for n in range(spec['max_items'])]
    if kind == 'hashtags':
        return ['#%02d' % n + 'h' * (spec['item_max_len'] - 3) for n in range(spec['max_items'])]
    if kind == 'emoji':
        return [chr(0x1F300 + n) for n in range(spec['max_items'])]
    if kind == 'choice':
        return spec['choices'][0]
    if kind == 'bool':
        return True
    if kind == 'int':
        return spec['max']
    if kind == 'xid':
        return XID
    raise AssertionError('unknown field type %r' % kind)


def test_the_brief_is_bounded_with_every_field_full(vp):
    full = {name: _max_value(spec, i) for i, (name, spec) in enumerate(vp.FIELD_SPECS.items())}
    vp.save_voice('u1', full)
    brief = vp.voice_brief('u1')
    assert 0 < len(brief) <= vp.MAX_BRIEF_CHARS == 1200


def test_the_brief_carries_what_her_agent_needs(vp):
    vp.save_voice('u1', {'city': 'Madurai', 'caption_language': 'ta', 'code_mix': True,
                         'hashtag_bank': ['#maduraimua', '#bridalmakeup'], 'emoji': ['✨'],
                         'cta': 'DM for bookings', 'logo_xid': XID})
    brief = vp.voice_brief('u1')
    for part in ('Madurai', 'ta', '#maduraimua', '#bridalmakeup', '✨', 'DM for bookings'):
        assert part in brief
    assert XID not in brief


def test_an_empty_profile_brief_says_so(vp):
    brief = vp.voice_brief('nobody')
    assert brief and len(brief) <= vp.MAX_BRIEF_CHARS
    assert 'no voice profile' in brief.lower()


def test_a_stored_field_the_template_no_longer_has_is_dropped_on_load(vp, tmp_path):
    from security.crypto import encrypt_json_file
    path = _file(tmp_path, 'u1')
    path.parent.mkdir(parents=True)
    encrypt_json_file(str(path), {'schema': vp.VOICE_SCHEMA,
                                  'fields': {'city': 'Madurai', 'retired_field': 1,
                                             'wpm': 'not a number'}})
    assert vp.load_voice('u1') == {'city': 'Madurai'}
