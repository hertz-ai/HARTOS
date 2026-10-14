"""integrations.trends.trending_songs_client: how a desktop gets central's
song list for her agent's reel_trending_songs tool.

Only when the tool asks (the owner's rule: no background work on her
desktop).  The base URL is SyncEngine.parent_tier_url(); the list is fetched
with core.http_pool.pooled_get (8 s read timeout, If-None-Match) and cached
six hours under get_data_dir()/trends/client/.  Offline, the cached list is
served marked stale with the reason; with no central and no cache, an empty
list says why.  Each song's language comes from its title's script
(tts_router.detect_language: Tamil, Devanagari, Gurmukhi); only a Latin
title asks the local LLM, once per song, and only when this node's LLM stays
on the premises (core.autogen_config.llm_stays_on_premises); the answer is
cached in song_lang.json.
"""
import copy
import json

import pytest
import requests

BASE = 'https://central.example.invalid'
HOUR = 3600.0
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
    def __init__(self, status, body=None, etag=None):
        self.status_code = status
        self._body = body
        self.headers = {'ETag': etag} if etag else {}

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
    from integrations.trends import trending_songs_client as c
    state = {'base': BASE, 'gets': [], 'posts': [], 'answers': [], 'reply': None,
             'now': NOW, 'premises': True}

    def _get(url, timeout=None, **kw):
        state['gets'].append({'url': url, 'timeout': timeout, 'headers': kw.get('headers') or {}})
        reply = state['reply']
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _post(url, timeout=None, **kw):
        state['posts'].append({'url': url, 'json': kw.get('json'), 'timeout': timeout})
        answer = state['answers'].pop(0) if state['answers'] else 'en'
        if isinstance(answer, Exception):
            raise answer
        return _Resp(200, {'choices': [{'message': {'content': answer}}]})

    monkeypatch.setattr('integrations.social.sync_engine.SyncEngine.parent_tier_url',
                        staticmethod(lambda: state['base']))
    monkeypatch.setattr('core.http_pool.pooled_get', _get)
    monkeypatch.setattr('core.http_pool.pooled_post', _post)
    monkeypatch.setattr('core.autogen_config.llm_stays_on_premises', lambda: state['premises'])
    monkeypatch.setattr('core.autogen_config.llm_http_target',
                        lambda draft=False: ('http://127.0.0.1:8080/v1/chat/completions', {},
                                             'local-model'))
    monkeypatch.setattr(c, '_now', lambda: state['now'])
    with swap_modules({'langdetect': None}):       # the script heuristic decides
        yield c, state
    platform_paths.reset_cache()


def test_a_fresh_list_is_fetched_tagged_and_shaped_for_her_agent(client):
    from integrations.reel_studio.contracts import validate_trending_songs
    c, st = client
    st['reply'] = _Resp(200, _doc(*SONGS), etag='"v1"')
    st['answers'] = ['hi']
    out = c.get_trending_songs(limit=10)
    assert validate_trending_songs(out) == []
    assert out['schema'] == 'hart.trending_songs.client.v1'
    assert out['stale'] is False and out['reason'] is None
    assert out['generated_at'] == '2026-10-14T03:06:00Z'
    assert [s['song_key'] for s in out['songs']] == ['cluster:1', 'cluster:2', 'cluster:3',
                                                      'cluster:4']
    langs = {s['song_key']: s['lang'] for s in out['songs']}
    assert langs == {'cluster:1': 'ta', 'cluster:2': 'hi', 'cluster:3': 'pa', 'cluster:4': 'hi'}
    saree = out['songs'][1]
    assert (saree['suggested_start_ms'], saree['start_label']) == (61000, '1:01')
    get = st['gets'][0]
    assert get['url'] == BASE + '/api/trends/songs/latest'
    assert get['timeout'] is not None
    assert len(st['posts']) == 1                      # only the Latin title asked


def test_within_six_hours_the_cache_answers(client):
    c, st = client
    st['reply'] = _Resp(200, _doc(*SONGS), etag='"v1"')
    c.get_trending_songs()
    st['now'] = NOW + 5 * HOUR
    st['reply'] = AssertionError('must not fetch')
    out = c.get_trending_songs()
    assert len(st['gets']) == 1 and out['stale'] is False


def test_after_six_hours_a_304_keeps_the_cached_list(client):
    c, st = client
    st['reply'] = _Resp(200, _doc(*SONGS), etag='"v1"')
    c.get_trending_songs()
    st['now'] = NOW + 7 * HOUR
    st['reply'] = _Resp(304)
    out = c.get_trending_songs()
    assert st['gets'][-1]['headers'].get('If-None-Match') == '"v1"'
    assert out['stale'] is False and len(out['songs']) == 4
    st['now'] = NOW + 12 * HOUR                       # 304 renewed the six hours
    c.get_trending_songs()
    assert len(st['gets']) == 2


def test_offline_the_cache_is_served_stale_with_the_reason(client):
    c, st = client
    st['reply'] = _Resp(200, _doc(*SONGS), etag='"v1"')
    c.get_trending_songs()
    st['now'] = NOW + 7 * HOUR
    st['reply'] = requests.exceptions.ConnectionError('offline')
    out = c.get_trending_songs()
    assert out['stale'] is True and out['reason'] == 'unreachable'
    assert len(out['songs']) == 4


def test_no_central_and_no_cache_is_an_empty_list_that_says_why(client):
    c, st = client
    st['base'] = ''
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['stale'] is True and out['reason'] == 'no_central'
    assert st['gets'] == []


def test_a_list_central_marked_stale_is_passed_on_as_stale(client):
    c, st = client
    doc = _doc(*SONGS, stale=True)
    doc['stale_reason'] = 'challenge'
    st['reply'] = _Resp(200, doc)
    out = c.get_trending_songs()
    assert out['stale'] is True and out['reason'] == 'challenge'


def test_a_list_carrying_a_url_is_not_used_or_cached(client):
    c, st = client
    bad = _doc(*SONGS)
    bad['songs'][0]['preview_url'] = 'https://example.invalid/a.m4a'
    st['reply'] = _Resp(200, bad)
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['reason'] == 'invalid_list'
    st['reply'] = _Resp(200, _doc(*SONGS))
    assert len(c.get_trending_songs()['songs']) == 4     # nothing bad was cached


def test_an_unpublished_list_is_empty_not_an_error(client):
    c, st = client
    st['reply'] = _Resp(200, {'schema': 'hart.trending_songs.v1', 'generated_at': None,
                              'songs': []})
    out = c.get_trending_songs()
    assert out['songs'] == [] and out['reason'] == 'not_published'


def test_centrals_language_hint_is_used_without_asking(client):
    c, st = client
    song = _song('cluster:7', 'Mallige Poo Vaasam', 5.0)
    song['language_hint'] = 'ta'
    st['reply'] = _Resp(200, _doc(song))
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
    cached = json.loads((tmp_path / 'trends' / 'client' / 'song_lang.json')
                        .read_text(encoding='utf-8'))
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
    st['reply'] = _Resp(200, _doc(*songs))
    st['answers'] = [requests.exceptions.ConnectionError('llm down')] * 4
    out = c.get_trending_songs()
    assert [s['lang'] for s in out['songs']] == [None] * 4
    assert len(st['posts']) == 1


def test_langs_filter_and_limit(client):
    c, st = client
    st['reply'] = _Resp(200, _doc(*SONGS))
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
