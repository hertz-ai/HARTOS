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


# ── Review of e1a1aa233 (F3) ────────────────────────────────────────────────

def test_an_unusable_old_file_is_asked_about_once_per_process(lang_paths, caplog):
    # Without the once-per-process flag every /chat re-reads the old file
    # and warns again.
    _, old = lang_paths
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({'language': 'xx'}), encoding='utf-8')

    with caplog.at_level('WARNING', logger='core.user_lang'):
        for _ in range(3):
            assert ul.get_preferred_lang() == 'en'

    warned = [r for r in caplog.records if 'not carried over' in r.getMessage()]
    assert len(warned) == 1, [r.getMessage() for r in warned]


def test_the_old_path_is_the_one_platform_paths_names():
    # Every other test patches _LEGACY_LANG_PATH; this pins the production
    # wiring, so pointing it at the new path (the feature silently off)
    # fails.
    assert ul._LEGACY_LANG_PATH == pp.legacy_documents_db_path('hart_language.json')
    assert os.path.normcase(ul._LEGACY_LANG_PATH) != os.path.normcase(ul._HART_LANG_PATH)


def test_only_user_lang_names_the_file():
    """A second reader of hart_language.json skips the carry-over and the
    data root (Nunba's TTS warm-up did, review of 924b8e9dc).  Nunba's
    tests/test_preferred_lang_fallback.py scans both repos the same way."""
    import ast
    from tests.unit.test_identity_is_hermetic import _docstring_ids, _shipped_sources
    offenders = []
    for rel in _shipped_sources():
        if rel == 'core/user_lang.py':
            continue
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), rel), encoding='utf-8', errors='replace') as fh:
            text = fh.read()
        if 'hart_language.json' not in text:
            continue
        tree = ast.parse(text)
        docs = _docstring_ids(tree)
        offenders += ['%s:%d' % (rel, n.lineno) for n in ast.walk(tree)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and id(n) not in docs and 'hart_language.json' in n.value
                      and not any(c.isspace() for c in n.value)]
    assert offenders == []



# ── Once means once (the admin config's marker, 4b1796862's review) ─────────
#
# The per-process flag was the only guard: delete hart_language.json (the way
# to reset the preference) and restart, and the old file was copied back.  A
# marker beside the new file (core.file_cache.adopt_legacy_json_once) now
# records that the move is done.

def _restart(monkeypatch):
    """A new process: the once-per-process flag and the read cache reset."""
    monkeypatch.setattr(ul, '_legacy_checked', False)
    monkeypatch.setattr(ul, '_cache', {'value': None, 'mtime': 0})


def _marker(new):
    return new.parent / 'hart_language.migrated.json'


def test_a_deleted_preference_does_not_bring_the_old_one_back(lang_paths, monkeypatch):
    new, old = lang_paths
    _write(old, {'language': 'ta'})
    assert ul.get_preferred_lang() == 'ta'
    assert _marker(new).exists()

    new.unlink()                        # the owner resets the preference
    _restart(monkeypatch)

    assert ul.get_preferred_lang() == 'en', 'the old preference came back'
    assert not new.exists()
    assert json.loads(old.read_text(encoding='utf-8')) == {'language': 'ta'}


def test_an_install_that_moved_before_the_marker_is_marked_on_first_read(
        lang_paths, monkeypatch):
    """A preference already at the new place (copied before the marker
    existed) is marked on the first read of a process, so deleting it later
    does not copy the old one either."""
    new, old = lang_paths
    _write(old, {'language': 'ta'})
    _write(new, {'language': 'hi'})

    assert ul.get_preferred_lang() == 'hi'
    assert _marker(new).exists()
    new.unlink()
    _restart(monkeypatch)
    assert ul.get_preferred_lang() == 'en'
    assert not new.exists()


def test_an_unusable_old_file_leaves_no_marker_and_moves_once_fixed(
        lang_paths, monkeypatch):
    new, old = lang_paths
    _write(old, {'language': 'xx'})
    assert ul.get_preferred_lang() == 'en'
    assert not _marker(new).exists()

    _write(old, {'language': 'ta'})
    _restart(monkeypatch)
    assert ul.get_preferred_lang() == 'ta'


def test_the_first_read_of_a_process_is_the_only_one_that_looks(lang_paths, monkeypatch):
    """The /chat hot path: after the first read nothing asks again."""
    calls = []
    import core.file_cache as fc
    real = fc.adopt_legacy_json_once
    monkeypatch.setattr(fc, 'adopt_legacy_json_once',
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(3):
        ul.get_preferred_lang()
    assert calls == [1]



def test_the_settle_step_itself_runs_once_per_process(lang_paths, monkeypatch):
    """Two threads can both see the flag unset before either sets it; the
    step checks it again under its lock, so it still runs once."""
    calls = []
    import core.file_cache as fc
    monkeypatch.setattr(fc, 'adopt_legacy_json_once',
                        lambda *a, **k: calls.append(1) or 'none')
    ul._adopt_legacy_file()
    ul._adopt_legacy_file()
    assert calls == [1]
