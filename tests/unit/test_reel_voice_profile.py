"""integrations.reel_studio.voice_profile: her voice profile, the one writer.

Stored per user at get_agent_data_dir()/reel_studio/<sha256(uid)[:16]>/voice.json
(the user-id rule and directory naming of core.user_file_store), written
atomically through security.crypto.encrypt_json_file: encrypted at rest when
HEVOLVE_DATA_KEY is set, plaintext JSON when it is not.  Fields come from
igkit/resources/voice_template.json (derived from instagram-agent-skill's
voice.md, MIT); anything else is refused.  voice_brief() is what reel_skill
puts in front of a skill text, so it is bounded at 1,200 characters.

A profile that exists but cannot be read (HEVOLVE_DATA_KEY missing for a
launch, rotated, or the file damaged) is never overwritten: 46848b568 read it
as empty and the next update_field replaced her whole profile with one field.
"""
import hashlib
import os
import threading

import pytest
from cryptography.fernet import Fernet

XID = 'ab' * 32
FULL = {'name': 'Meena Studio', 'city': 'Madurai', 'hashtag_bank': ['#maduraimua', '#bridalmakeup'],
        'sample_captions': ['Reception look in 40 minutes, Chennai humidity and all.'],
        'style_notes': 'soft glam, no heavy contour'}


@pytest.fixture
def vp(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HEVOLVE_DATA_KEY', Fernet.generate_key().decode())
    # The secrets vault is crypto's fallback for a missing data key: keep it
    # out, so "no key" means no key on any machine that runs this.
    import security.secrets_manager as secrets_manager
    monkeypatch.setattr(secrets_manager, 'get_secret', lambda name, default='': default)
    from core import platform_paths
    platform_paths.reset_cache()
    from integrations.reel_studio import voice_profile
    yield voice_profile
    platform_paths.reset_cache()


def _root(tmp_path):
    return tmp_path / 'data' / 'agent_data' / 'reel_studio'


def _file(tmp_path, uid):
    name = hashlib.sha256(str(uid).strip().encode('utf-8')).hexdigest()[:16]
    return _root(tmp_path) / name / 'voice.json'


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


@pytest.mark.parametrize('bad', ['', '   ', None, True, 1.5, b'u1', ['u1']])
def test_a_user_id_that_is_not_text_or_a_number_is_refused(vp, bad):
    with pytest.raises(ValueError):
        vp.load_voice(bad)
    with pytest.raises(ValueError):
        vp.update_field(bad, 'city', 'Madurai')


@pytest.mark.parametrize('uid', ['a.b@example.com', 'peer:abc', '../x', 'a/b', 'a\\b', '..',
                                 'x' * 200, 'மீனா', 10077])
def test_any_user_id_the_file_store_takes_gets_a_profile_inside_the_root(vp, tmp_path, uid):
    # The reel tools hand voice_profile the user ids core.user_file_store
    # accepts; a stricter rule here made the tools fail for those users.
    assert vp.update_field(uid, 'city', 'Madurai') == {'city': 'Madurai'}
    assert vp.load_voice(uid) == {'city': 'Madurai'}
    path = _file(tmp_path, uid)
    assert path.is_file()
    assert [p.name for p in _root(tmp_path).iterdir()] == [path.parent.name]


@pytest.mark.parametrize('uid', ['u1', ' u1 ', 'a.b@example.com', 10077, '', '   ', None, True,
                                 1.5, b'u1'])
def test_the_user_id_rule_is_the_file_stores(vp, tmp_path, uid):
    from core.user_file_store import UserFileStore
    store = UserFileStore(root=str(tmp_path / 'store'))

    def refused(call):
        try:
            call()
        except ValueError:
            return True
        return False

    assert refused(lambda: vp.load_voice(uid)) == refused(lambda: store.usage(uid))


def test_an_int_and_its_text_are_one_user(vp):
    vp.update_field(10077, 'city', 'Madurai')
    assert vp.load_voice('10077') == {'city': 'Madurai'}
    assert vp.load_voice(' 10077 ') == {'city': 'Madurai'}


def test_a_profile_of_another_user_at_the_same_path_is_refused(vp, tmp_path):
    # sha256(uid)[:16] collided: never show one user another's profile, and
    # never overwrite it.
    from security.crypto import encrypt_json_file
    path = _file(tmp_path, 'u1')
    path.parent.mkdir(parents=True)
    encrypt_json_file(str(path), {'schema': vp.VOICE_SCHEMA, 'user_id': 'someone-else',
                                  'fields': {'city': 'Chennai'}})
    before = path.read_bytes()
    with pytest.raises(PermissionError):
        vp.load_voice('u1')
    with pytest.raises(PermissionError):
        vp.update_field('u1', 'city', 'Madurai')
    assert path.read_bytes() == before


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


# ── a profile that exists but cannot be read is never overwritten ──────────

def _lose_the_key(monkeypatch, path, how):
    """Make her saved profile unreadable the ways it happens in the field."""
    if how == 'no key':                 # HEVOLVE_DATA_KEY missing for one launch
        monkeypatch.delenv('HEVOLVE_DATA_KEY', raising=False)
    elif how == 'other key':            # the key was rotated
        monkeypatch.setenv('HEVOLVE_DATA_KEY', Fernet.generate_key().decode())
    elif how == 'damaged':
        raw = path.read_bytes()
        path.write_bytes(raw[:len(raw) // 2])
    else:
        raise AssertionError(how)


@pytest.mark.parametrize('how', ['no key', 'other key', 'damaged'])
def test_an_unreadable_profile_is_never_overwritten(vp, tmp_path, monkeypatch, how):
    vp.save_voice('u1', FULL)
    path = _file(tmp_path, 'u1')
    _lose_the_key(monkeypatch, path, how)
    before = path.read_bytes()

    with pytest.raises(vp.VoiceProfileUnreadable, match='HEVOLVE_DATA_KEY'):
        vp.update_field('u1', 'logo_xid', XID)
    with pytest.raises(vp.VoiceProfileUnreadable, match='HEVOLVE_DATA_KEY'):
        vp.save_voice('u1', {'city': 'Chennai'})
    with pytest.raises(vp.VoiceProfileUnreadable, match='HEVOLVE_DATA_KEY'):
        vp.load_voice('u1')

    assert path.read_bytes() == before
    assert os.listdir(path.parent) == ['voice.json']


def test_her_whole_profile_is_back_when_the_key_is(vp, tmp_path, monkeypatch):
    key = os.environ['HEVOLVE_DATA_KEY']
    vp.save_voice('u1', FULL)
    monkeypatch.delenv('HEVOLVE_DATA_KEY')
    with pytest.raises(vp.VoiceProfileUnreadable):
        vp.update_field('u1', 'logo_xid', XID)
    monkeypatch.setenv('HEVOLVE_DATA_KEY', key)
    assert vp.load_voice('u1') == FULL
    assert vp.update_field('u1', 'logo_xid', XID) == dict(FULL, logo_xid=XID)


def test_the_unreadable_error_is_not_a_refused_value(vp, tmp_path, monkeypatch):
    # The reel tools answer a ValueError with "fix the value"; a lost key is
    # not something the value can fix.
    vp.save_voice('u1', FULL)
    monkeypatch.delenv('HEVOLVE_DATA_KEY')
    with pytest.raises(vp.VoiceProfileUnreadable) as caught:
        vp.update_field('u1', 'city', 'Chennai')
    assert not isinstance(caught.value, ValueError)
    assert caught.value.path == str(_file(tmp_path, 'u1'))


def test_the_brief_says_her_profile_exists_but_could_not_be_read(vp, tmp_path, monkeypatch):
    vp.save_voice('u1', FULL)
    monkeypatch.setenv('HEVOLVE_DATA_KEY', Fernet.generate_key().decode())
    brief = vp.voice_brief('u1')
    assert 0 < len(brief) <= vp.MAX_BRIEF_CHARS
    assert 'could not be read' in brief
    assert 'HEVOLVE_DATA_KEY' in brief
    # Never the empty-profile text, which steers the agent to rebuild it
    # field by field.
    assert 'no voice profile' not in brief.lower()
    assert brief != vp.voice_brief('nobody')


def test_a_profile_saved_without_a_key_is_read_once_a_key_is_set(vp, tmp_path, monkeypatch):
    # Plaintext from a launch with no key is readable, not "unreadable"; the
    # next write encrypts it.
    key = os.environ['HEVOLVE_DATA_KEY']
    monkeypatch.delenv('HEVOLVE_DATA_KEY')
    vp.save_voice('u1', FULL)
    path = _file(tmp_path, 'u1')
    assert path.read_bytes().lstrip().startswith(b'{')
    monkeypatch.setenv('HEVOLVE_DATA_KEY', key)
    assert vp.load_voice('u1') == FULL
    vp.update_field('u1', 'city', 'Chennai')
    assert path.read_bytes().startswith(b'gAAAAA')
    assert vp.load_voice('u1') == dict(FULL, city='Chennai')


def test_the_new_profile_is_on_disk_before_it_replaces_the_old(vp, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append('fsync')
        return real_fsync(fd)

    def replace(src, dst):
        events.append('replace')
        return real_replace(src, dst)

    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    vp.update_field('u1', 'city', 'Madurai')
    assert 'replace' in events
    assert 'fsync' in events[:events.index('replace')]
