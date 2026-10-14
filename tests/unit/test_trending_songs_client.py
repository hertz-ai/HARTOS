"""integrations.trends.trending_songs_client: her agent's read of the
trending-song list her own desktop made (owner ruling A10d: "all in local
nunba build not in central").

Only when the tool asks.  The list is the one trending_songs.publish wrote
on this computer (read_latest): nothing is fetched from central or any
other node.  With no list, the answer is an empty list whose ``reason`` and
``note`` say why -- above all, that no dedicated research Instagram account
is set up here (never the business account) -- so the reel reply can tell
her.  With no account, the read first asks the owner for one
(daily_job.ask_for_account: the consent card) and then says where that ask
stands.  A paused list says how long the pause runs.  Each song's language
comes from its title's script
(tts_router.detect_language: Tamil, Devanagari, Gurmukhi); only a Latin
title asks the local LLM, once per song, and only when this node's LLM stays
on the premises (core.autogen_config.llm_stays_on_premises); the answer is
cached in song_lang.json.
"""
import copy
import json

import pytest
import requests

NOW = 1792000000.0


def _song(key, title, score, start=38500):
    return {'song_key': key, 'title': title, 'artist': 'Fixture Artist',
            'suggested_start_ms': start, 'score': score, 'accounts': 3, 'reels': 4,
            'max_outlier': 6.1, 'highlight_start_ms': [start], 'language_hint': None,
            'is_original_audio': False, 'is_explicit': False, 'duration_ms': 180000}


def _doc(*songs, stale=False):
    """A published list holding copies of ``songs`` (a test that changes
    one must not change the module's SONGS for the tests after it)."""
    return {'schema': 'hart.trending_songs.v1', 'date': '2026-10-14',
            'generated_at': '2026-10-14T03:06:00Z', 'source': 'instagrapi', 'window_days': 7,
            'stale': stale, 'accounts_ok': 20, 'accounts_total': 20,
            'songs': copy.deepcopy(list(songs))}


SONGS = (_song('cluster:1', 'மல்லிகைப் பூ வாசம்', 4.0),
         _song('cluster:2', 'Saree Song', 3.0, start=61000),
         _song('cluster:3', 'ਮਹਿੰਦੀ ਦੀ ਰਾਤ', 2.0),
         _song('cluster:4', 'शादी की रात है आज', 1.0))


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError('no body')
        return json.loads(json.dumps(self._body))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    from tests.unit.module_swap import swap_modules
    from integrations.trends import daily_job
    from integrations.trends import trending_songs_client as c
    state = {'gets': [], 'posts': [], 'answers': [], 'now': NOW, 'premises': True,
             'asked': 0,
             'creds': {daily_job.USERNAME_CREDENTIAL: 'research_fixture',
                       daily_job.PASSWORD_CREDENTIAL: 'pw-fixture'}}

    def _ask_for_account():
        state['asked'] += 1
        return []

    class _Vault:
        def owner_credential_names(self):
            return set(state['creds'])

        def owner_credential(self, name, names=None):
            return state['creds'].get(name, '')

    def _get(url, timeout=None, **kw):
        state['gets'].append(url)
        raise AssertionError('the list is never fetched from another node')

    def _post(url, timeout=None, **kw):
        state['posts'].append({'url': url, 'json': kw.get('json'), 'timeout': timeout})
        answer = state['answers'].pop(0) if state['answers'] else 'en'
        if isinstance(answer, Exception):
            raise answer
        return _Resp(200, {'choices': [{'message': {'content': answer}}]})

    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'flat')
    monkeypatch.setattr('hartos.ai_key_vault.get_ai_key_vault', lambda: _Vault())
    # The card itself is daily_job's (test_trends_daily_job.py runs it on a
    # real consent table); here only that the read asks, and no owner is
    # named, so where the ask stands is 'no_account' unless a test says so.
    monkeypatch.setattr(daily_job, 'ask_for_account', _ask_for_account)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)
    monkeypatch.setattr('integrations.social.sync_engine.SyncEngine.parent_tier_url',
                        staticmethod(lambda: 'https://central.example.invalid'))
    monkeypatch.setattr('core.http_pool.pooled_get', _get)
    monkeypatch.setattr('core.http_pool.pooled_post', _post)
    monkeypatch.setattr('core.autogen_config.llm_stays_on_premises', lambda: state['premises'])
    monkeypatch.setattr('core.autogen_config.llm_http_target',
                        lambda draft=False: ('http://127.0.0.1:8080/v1/chat/completions', {},
                                             'local-model'))
    monkeypatch.setattr(c, '_now', lambda: state['now'])
    (tmp_path / 'trends').mkdir(exist_ok=True)
    (tmp_path / 'trends' / 'watchlist.json').write_text(json.dumps(
        {'accounts': ['mua_a', 'mua_b']}), encoding='utf-8')
    with swap_modules({'langdetect': None}):       # the script heuristic decides
        yield c, state
    platform_paths.reset_cache()


def _publish(doc):
    from integrations.trends.trending_songs import publish
    assert publish(doc) is True


def test_the_local_list_is_read_tagged_and_shaped_for_her_agent(client):
    from integrations.reel_studio.contracts import validate_trending_songs
    c, st = client
    _publish(_doc(*SONGS))
    st['answers'] = ['hi']
    out = c.get_trending_songs(limit=10)
    assert validate_trending_songs(out) == []
    assert out['schema'] == 'hart.trending_songs.client.v1'
    assert out['stale'] is False and out['reason'] is None
    assert out['generated_at'] == '2026-10-14T03:06:00Z' and out['date'] == '2026-10-14'
    assert [s['song_key'] for s in out['songs']] == ['cluster:1', 'cluster:2', 'cluster:3',
                                                      'cluster:4']
    langs = {s['song_key']: s['lang'] for s in out['songs']}
    assert langs == {'cluster:1': 'ta', 'cluster:2': 'hi', 'cluster:3': 'pa', 'cluster:4': 'hi'}
    saree = out['songs'][1]
    assert (saree['suggested_start_ms'], saree['start_label']) == (61000, '1:01')
    assert len(st['posts']) == 1                      # only the Latin title asked
    assert st['gets'] == []                           # nothing fetched from central
    assert st['asked'] == 0                           # a list: nothing to ask for


def test_with_no_research_account_the_list_is_empty_and_says_so(client):
    from integrations.reel_studio.contracts import validate_trending_songs
    c, st = client
    st['creds'].clear()
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['reason'] == 'no_account'
    assert st['asked'] == 1                           # her agent's read asked first
    note = out['note']
    assert 'research Instagram account' in note
    assert 'never the business account' in note
    assert 'could not be asked' in note               # nobody here to ask
    assert validate_trending_songs(out) == []
    assert st['gets'] == [] and st['posts'] == []


@pytest.mark.parametrize('answer,words', [
    ('account_asked', ('consent card', 'username and password',
                       'never the business account')),
    ('account_declined', ('chose not to', 'Allow asking again')),
])
def test_the_note_says_where_the_ask_for_the_account_stands(client, monkeypatch, answer,
                                                             words):
    from integrations.reel_studio.contracts import validate_trending_songs
    c, st = client
    st['creds'].clear()
    monkeypatch.setattr('integrations.trends.daily_job._account_ask_state', lambda: answer)
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['reason'] == answer and st['asked'] == 1
    for word in words:
        assert word in out['note']
    assert validate_trending_songs(out) == []


@pytest.mark.parametrize('code', ['login', 'challenge'])
def test_a_paused_list_says_how_long_the_pause_runs(client, code):
    """The pause is daily_job.FREEZE_S (doubling on a pause in a row) and
    entering the account again does not shorten it: the note says so, and
    never promises the list back once something is entered."""
    from integrations.trends import daily_job
    from integrations.trends.trending_songs import mark_stale
    c, _ = client
    _publish(_doc(*SONGS))
    assert mark_stale(code) is True
    out = c.get_trending_songs()
    assert out['reason'] == code and len(out['songs']) == 4
    note = out['note']
    assert '%d hours' % (daily_job.FREEZE_S // 3600) in note
    assert ('consent card' if code == 'login' else 'by hand') in note


def test_a_list_made_earlier_is_still_served_after_the_account_is_removed(client):
    c, st = client
    _publish(_doc(*SONGS))
    st['creds'].clear()
    out = c.get_trending_songs(langs='ta')
    assert [s['song_key'] for s in out['songs']] == ['cluster:1']


def test_with_no_watchlist_it_says_so(client, tmp_path):
    c, _ = client
    (tmp_path / 'trends' / 'watchlist.json').unlink()
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['reason'] == 'no_watchlist'
    assert 'watch' in out['note']


def test_on_central_there_is_no_list(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'central')
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['reason'] == 'central'
    assert out['note']           # and nothing is asked there: test_trends_daily_job.py


def test_before_the_first_run_it_says_the_list_is_not_made_yet(client):
    c, _ = client
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['reason'] == 'not_run_yet'
    assert 'once a day' in out['note']


def test_a_stale_local_list_is_served_marked_stale_with_the_reason(client):
    from integrations.trends.trending_songs import mark_stale
    c, _ = client
    _publish(_doc(*SONGS))
    assert mark_stale('challenge') is True
    out = c.get_trending_songs()
    assert out['stale'] is True and out['reason'] == 'challenge'
    assert len(out['songs']) == 4
    assert out['note']


def test_a_list_carrying_a_url_is_never_served(client, tmp_path):
    c, _ = client
    bad = _doc(*SONGS)
    bad['songs'][0]['preview_url'] = 'https://example.invalid/a.m4a'
    folder = tmp_path / 'trends' / 'songs'
    folder.mkdir(parents=True)
    (folder / 'latest.json').write_text(json.dumps(bad), encoding='utf-8')
    out = c.get_trending_songs()
    assert out['songs'] == [] and 'example.invalid' not in json.dumps(out)


def test_the_jobs_language_hint_is_used_without_asking(client):
    c, st = client
    song = _song('cluster:7', 'Mallige Poo Vaasam', 5.0)
    song['language_hint'] = 'ta'
    _publish(_doc(song))
    out = c.get_trending_songs(langs='ta')
    assert [(s['song_key'], s['lang']) for s in out['songs']] == [('cluster:7', 'ta')]
    assert st['posts'] == []


def test_native_script_titles_never_ask_the_llm(client):
    c, st = client
    assert c.tag_language({'song_key': 'k1', 'title': 'மல்லிகைப் பூ வாசம்'}) == 'ta'
    assert c.tag_language({'song_key': 'k2', 'title': 'ਮਹਿੰਦੀ ਦੀ ਰਾਤ'}) == 'pa'
    assert c.tag_language({'song_key': 'k3', 'title': 'शादी की रात है आज'}) == 'hi'
    assert c.tag_language({'song_key': 'k4', 'title': '사랑해요 오늘 밤'}) == 'other'
    assert st['posts'] == []


def test_a_latin_title_asks_the_local_llm_once_then_the_cache_answers(client, tmp_path):
    c, st = client
    st['answers'] = ['Language: pa']
    song = {'song_key': 'cluster:9', 'title': 'Laung Fixture', 'artist': 'Fixture Singer'}
    assert c.tag_language(song) == 'pa'
    assert c.tag_language(song) == 'pa'
    assert len(st['posts']) == 1
    body = st['posts'][0]['json']
    prompt = body['messages'][-1]['content']
    assert 'Laung Fixture' in prompt and 'ta, hi, pa, en, other' in prompt
    assert body['temperature'] == 0 and body['max_tokens'] <= 8
    assert body['chat_template_kwargs'] == {'enable_thinking': False}
    assert st['posts'][0]['timeout'] is not None
    cached = json.loads((tmp_path / 'trends' / 'song_lang.json').read_text(encoding='utf-8'))
    assert cached['cluster:9'] == 'pa'


def test_an_llm_off_the_premises_is_never_asked(client):
    c, st = client
    st['premises'] = False
    assert c.tag_language({'song_key': 'cluster:9', 'title': 'Saree Song'}) is None
    assert st['posts'] == []


def test_an_answer_outside_the_set_is_not_kept(client):
    c, st = client
    st['answers'] = ['I think it is Spanish', 'en']
    song = {'song_key': 'cluster:9', 'title': 'Saree Song'}
    assert c.tag_language(song) is None
    assert c.tag_language(song) == 'en'               # asked again, then kept
    assert len(st['posts']) == 2


def test_a_failing_llm_stops_asking_for_the_rest_of_the_call(client):
    c, st = client
    songs = [_song('cluster:%d' % i, 'Latin Title %d' % i, 10.0 - i) for i in range(4)]
    _publish(_doc(*songs))
    st['answers'] = [requests.exceptions.ConnectionError('llm down')] * 4
    out = c.get_trending_songs()
    assert [s['lang'] for s in out['songs']] == [None] * 4
    assert len(st['posts']) == 1


def test_langs_filter_and_limit(client):
    c, st = client
    _publish(_doc(*SONGS))
    st['answers'] = ['en']
    out = c.get_trending_songs(langs=['ta', 'pa'], limit=5)
    assert [s['song_key'] for s in out['songs']] == ['cluster:1', 'cluster:3']
    out = c.get_trending_songs(langs='hi', limit=1)
    assert [s['song_key'] for s in out['songs']] == ['cluster:4']
    out = c.get_trending_songs(limit=2)
    assert len(out['songs']) == 2


def test_gurmukhi_text_is_punjabi_to_the_one_language_detector(client):
    """tts_router.detect_language's script fallback named no Gurmukhi range,
    so Punjabi text read as 'en' (and was spoken by an English voice)."""
    from integrations.channels.media.tts_router import detect_language
    assert detect_language('ਸਤ ਸ੍ਰੀ ਅਕਾਲ ਜੀ ਆਇਆਂ ਨੂੰ') == 'pa'
    assert detect_language('नमस्ते दुनिया यह एक परीक्षण है') == 'hi'
