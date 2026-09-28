"""A language preference saved before 8bbe771c4 is still read.

8bbe771c4 routed core.user_lang's hart_language.json through
core.platform_paths.get_db_path.  On Windows that is the same file as before
(~/Documents/Nunba/data).  On macOS and Linux the data root is elsewhere
(~/Library/Application Support/Nunba, ~/.config/nunba), so a preference saved
at ~/Documents/Nunba/data/hart_language.json was no longer read and the
person's language fell back to 'en'.

core.user_lang carries it over once, non-destructively (the admin-config
pattern, 4b1796862): copy when the new file is missing; never touch the old.

Every test runs in a decoy home (HOME / USERPROFILE -> tmp_path), so no test
reads or writes the owner's real files.
"""
import json
import os

import pytest

import core.platform_paths as pp
import core.user_lang as ul


@pytest.fixture
def lang_paths(tmp_path, monkeypatch):
    """Point core.user_lang's new and old paths into tmp_path."""
    new = tmp_path / 'root' / 'data' / 'hart_language.json'
    old = tmp_path / 'home' / 'Documents' / 'Nunba' / 'data' / 'hart_language.json'
    monkeypatch.setattr(ul, '_HART_LANG_PATH', str(new))
    monkeypatch.setattr(ul, '_LEGACY_LANG_PATH', str(old))
    monkeypatch.setattr(ul, '_legacy_checked', False)
    monkeypatch.setattr(ul, '_cache', {'value': None, 'mtime': 0})
    monkeypatch.setattr(ul, '_listeners', [])
    monkeypatch.delenv('HART_USER_LANGUAGE', raising=False)
    monkeypatch.setattr(ul, '_load_from_node_identity', lambda: None)
    return new, old


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def test_a_preference_at_the_old_place_is_read_and_copied_once(lang_paths):
    new, old = lang_paths
    _write(old, {'language': 'ta'})
    before = old.read_bytes()

    assert ul.get_preferred_lang() == 'ta', 'the saved preference was lost'
    assert json.loads(new.read_text(encoding='utf-8')) == {'language': 'ta'}
    assert old.read_bytes() == before, 'the old file was changed or removed'


def test_a_preference_already_at_the_new_place_is_never_overwritten(lang_paths):
    new, old = lang_paths
    _write(old, {'language': 'ta'})
    _write(new, {'language': 'hi'})

    assert ul.get_preferred_lang() == 'hi'
    assert json.loads(new.read_text(encoding='utf-8')) == {'language': 'hi'}


def test_the_copy_itself_never_overwrites(lang_paths):
    # The reader asks only when the new file is missing; the copy re-checks,
    # for a set_preferred_lang in another thread or process between the two.
    new, old = lang_paths
    _write(old, {'language': 'ta'})
    _write(new, {'language': 'hi'})

    ul._adopt_legacy_file()

    assert json.loads(new.read_text(encoding='utf-8')) == {'language': 'hi'}


def test_a_write_after_the_copy_wins_and_the_old_file_stays(lang_paths):
    new, old = lang_paths
    _write(old, {'language': 'ta'})
    before = old.read_bytes()

    assert ul.set_preferred_lang('te') is True
    assert ul.get_preferred_lang() == 'te'
    assert old.read_bytes() == before


@pytest.mark.parametrize('content', ['not json', json.dumps({'language': 'xx'}),
                                     json.dumps(['ta'])])
def test_an_unusable_old_file_is_left_not_copied(lang_paths, content):
    new, old = lang_paths
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(content, encoding='utf-8')

    assert ul.get_preferred_lang() == 'en'
    assert not new.exists()
    assert old.read_text(encoding='utf-8') == content


def test_nothing_at_either_place_is_the_default(lang_paths):
    new, _ = lang_paths
    assert ul.get_preferred_lang() == 'en'
    assert not new.exists()


# ── The real platform paths, in a decoy home ────────────────────────────────

@pytest.fixture
def decoy_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    for var in ('HOME', 'USERPROFILE'):
        monkeypatch.setenv(var, str(home))
    for var in ('NUNBA_DATA_DIR', 'HARTOS_DATA_DIR', 'XDG_DATA_HOME'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(os.path, 'isfile',
                        lambda p, _f=os.path.isfile: False
                        if p == '/etc/hartos-release' else _f(p))
    return home


def _platform(monkeypatch, name):
    monkeypatch.setattr(pp, '_IS_WINDOWS', name == 'win32')
    monkeypatch.setattr(pp, '_IS_MACOS', name == 'darwin')
    monkeypatch.setattr(pp, '_IS_LINUX', name == 'linux')


@pytest.mark.parametrize('platform,moved', [('darwin', True), ('linux', True),
                                            ('win32', False)])
def test_each_platforms_old_and_new_paths(decoy_home, monkeypatch, platform,
                                          moved):
    """The unswapped paths each platform really uses (the pytest guard swaps
    the resolvers, so this builds them from the same two functions)."""
    _platform(monkeypatch, platform)
    new = os.path.join(pp._platform_default_data_dir(), 'data', 'hart_language.json')
    old = os.path.join(pp._legacy_documents_root(), 'data', 'hart_language.json')
    assert old.startswith(str(decoy_home))
    assert (os.path.normcase(new) != os.path.normcase(old)) is moved

    monkeypatch.setattr(ul, '_HART_LANG_PATH', new)
    monkeypatch.setattr(ul, '_LEGACY_LANG_PATH', old)
    monkeypatch.setattr(ul, '_legacy_checked', False)
    monkeypatch.setattr(ul, '_cache', {'value': None, 'mtime': 0})
    monkeypatch.delenv('HART_USER_LANGUAGE', raising=False)
    monkeypatch.setattr(ul, '_load_from_node_identity', lambda: None)
    os.makedirs(os.path.dirname(old), exist_ok=True)
    with open(old, 'w', encoding='utf-8') as f:
        json.dump({'language': 'ta'}, f)

    assert ul.get_preferred_lang() == 'ta'
    assert os.path.isfile(old)
    with open(new, encoding='utf-8') as f:
        assert json.load(f) == {'language': 'ta'}


def test_under_pytest_the_old_place_is_not_the_owners_real_one():
    real = os.path.normcase(pp._legacy_documents_root())
    assert not os.path.normcase(pp.legacy_documents_db_path('x.json')).startswith(real)
    assert not os.path.normcase(ul._LEGACY_LANG_PATH).startswith(real)
