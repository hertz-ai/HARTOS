"""GET /api/trends/songs/latest (integrations.trends.api_trends): central's
public, read-only song list, registered ONLY on a central node (critique
finding 42: a public route on every node is a new ingress serving nothing).

The body is the published hart.trending_songs.v1 list (or, before the first
publish, ``{"songs": [], "generated_at": null}``), filtered by ``lang`` and
cut by ``limit``, with an ETag and Cache-Control so desktops polling it every
six hours mostly get a 304.
"""
import json

import pytest
from flask import Flask


@pytest.fixture
def central(tmp_path, monkeypatch):
    from tests.conftest import refuse_all_network
    refuse_all_network(monkeypatch)
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    monkeypatch.setattr('security.key_delegation.get_node_tier', lambda: 'central')
    yield monkeypatch
    platform_paths.reset_cache()


def _app():
    from integrations.trends.api_trends import register_trends_api
    app = Flask('trends_test')
    registered = register_trends_api(app)
    return app, registered


def _rules(app):
    return {r.rule for r in app.url_map.iter_rules()}


def _song(key, lang, score):
    return {'song_key': key, 'title': 'T ' + key, 'artist': 'A', 'suggested_start_ms': 1000,
            'score': score, 'accounts': 2, 'reels': 3, 'highlight_start_ms': [1000],
            'language_hint': lang}


def _publish(*songs):
    from integrations.trends.trending_songs import publish
    doc = {'schema': 'hart.trending_songs.v1', 'date': '2026-10-14',
           'generated_at': '2026-10-14T03:06:00Z', 'source': 'instagrapi',
           'window_days': 7, 'stale': False, 'accounts_ok': 20, 'accounts_total': 20,
           'songs': list(songs)}
    assert publish(doc) is True
    return doc


@pytest.mark.parametrize('tier', ['flat', 'regional', 'local'])
def test_the_route_is_absent_off_central(central, tier):
    central.setattr('security.key_delegation.get_node_tier', lambda: tier)
    app, registered = _app()
    assert registered is False
    assert '/api/trends/songs/latest' not in _rules(app)
    assert app.test_client().get('/api/trends/songs/latest').status_code == 404


def test_before_the_first_publish_the_list_is_empty(central):
    app, registered = _app()
    assert registered is True
    resp = app.test_client().get('/api/trends/songs/latest')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['songs'] == [] and body['generated_at'] is None


def test_the_published_list_is_served_with_an_etag(central):
    doc = _publish(_song('cluster:1', 'ta', 3.0), _song('cluster:2', None, 2.0))
    app, _ = _app()
    client = app.test_client()
    resp = client.get('/api/trends/songs/latest')
    assert resp.status_code == 200
    assert resp.get_json() == doc
    etag = resp.headers['ETag']
    assert etag and 'public' in resp.headers['Cache-Control']
    again = client.get('/api/trends/songs/latest', headers={'If-None-Match': etag})
    assert again.status_code == 304 and again.data == b''
    assert again.headers['ETag'] == etag
    other = client.get('/api/trends/songs/latest', headers={'If-None-Match': '"stale-tag"'})
    assert other.status_code == 200


def test_lang_and_limit_narrow_the_list(central):
    _publish(_song('cluster:1', 'ta', 3.0), _song('cluster:2', None, 2.5),
             _song('cluster:3', 'hi', 2.0), _song('cluster:4', 'ta', 1.0))
    client = _app()[0].test_client()
    body = client.get('/api/trends/songs/latest?lang=ta').get_json()
    assert [s['song_key'] for s in body['songs']] == ['cluster:1', 'cluster:4']
    body = client.get('/api/trends/songs/latest?lang=ta,hi&limit=2').get_json()
    assert [s['song_key'] for s in body['songs']] == ['cluster:1', 'cluster:3']
    body = client.get('/api/trends/songs/latest?limit=1').get_json()
    assert [s['song_key'] for s in body['songs']] == ['cluster:1']
    etag_all = client.get('/api/trends/songs/latest').headers['ETag']
    assert client.get('/api/trends/songs/latest?limit=1').headers['ETag'] != etag_all


@pytest.mark.parametrize('query', ['limit=abc', 'limit=-5', 'limit=0', 'lang=', 'lang=%3Cscript%3E'])
def test_junk_parameters_never_fail_the_request(central, query):
    _publish(_song('cluster:1', 'ta', 3.0))
    resp = _app()[0].test_client().get('/api/trends/songs/latest?' + query)
    assert resp.status_code == 200
    assert isinstance(resp.get_json()['songs'], list)


def test_a_damaged_file_serves_the_empty_list(central, tmp_path):
    d = tmp_path / 'trends' / 'songs'
    d.mkdir(parents=True)
    (d / 'latest.json').write_text('{oops', encoding='utf-8')
    body = _app()[0].test_client().get('/api/trends/songs/latest').get_json()
    assert body['songs'] == [] and body['generated_at'] is None


def test_registering_twice_is_harmless(central):
    from integrations.trends.api_trends import register_trends_api
    app = Flask('trends_twice')
    assert register_trends_api(app) is True
    assert register_trends_api(app) is True


def _init_engine(monkeypatch, app):
    """init_agent_engine's phase 1 on ``app``, with its threads and the other
    blueprints' imports stood down (only the trends registration is under
    test)."""
    import threading
    import integrations.agent_engine as ae
    from tests.unit.module_swap import swap_modules

    class _NoThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass
    monkeypatch.setattr(threading, 'Thread', _NoThread)
    monkeypatch.setattr(ae, '_agent_engine_initialized', False)
    monkeypatch.setenv('HEVOLVE_AGENT_ENGINE_ENABLED', 'true')

    def _no_bp():
        raise RuntimeError('not under test')
    monkeypatch.setattr(ae, 'get_engine_blueprint', _no_bp)
    with swap_modules({'integrations.agent_engine.commercial_api': None,
                       'integrations.agent_engine.build_distribution': None,
                       'integrations.social.api_regional_host': None}):
        ae.init_agent_engine(app)


def test_the_agent_engine_registers_it_on_central(central):
    app = Flask('engine_central')
    _init_engine(central, app)
    assert '/api/trends/songs/latest' in _rules(app)


def test_the_agent_engine_leaves_it_off_a_desktop(central):
    central.setattr('security.key_delegation.get_node_tier', lambda: 'flat')
    app = Flask('engine_flat')
    _init_engine(central, app)
    assert '/api/trends/songs/latest' not in _rules(app)
