"""
Behavioral tests for integrations/agent_engine/media_semantic_index.py.

These import the REAL module and mock only the boundaries:
  - the vision describe captioner (a plain callable)
  - the chromadb collection (an in-memory fake)
  - the filesystem (real temp files under tmp_path)
  - the ScopeGuard egress gate (monkeypatched get_scope_guard)
  - the pooled HTTP client (monkeypatched core.http_pool.pooled_get)

We call the real functions and assert observable behavior, never grep source.
"""
import os

import pytest

from integrations.agent_engine import media_semantic_index as msi


# ─── Fakes / fixtures ───────────────────────────────────────────────────────

class FakeCollection:
    """Minimal chromadb-collection stand-in. Records adds; answers queries
    from whatever the test pre-seeds in ``_seed``."""

    def __init__(self):
        self.added = []          # list of (ids, documents, metadatas)
        self.upserts = []
        self._seed = None        # a query() result dict, or None

    def upsert(self, ids, documents, metadatas):
        self.upserts.append((ids, documents, metadatas))
        self.added.append((ids, documents, metadatas))

    def query(self, query_texts, n_results=10):
        if self._seed is not None:
            return self._seed
        return {'ids': [[]], 'documents': [[]], 'metadatas': [[]], 'distances': [[]]}


def _make_image(path, content=b'\xff\xd8\xff\xe0fakejpeg'):
    with open(path, 'wb') as f:
        f.write(content)
    return str(path)


def _index(tmp_path, captioner, collection=None):
    """Construct a MediaSemanticIndex with mocked boundaries + temp base dir."""
    col = collection if collection is not None else FakeCollection()
    return msi.MediaSemanticIndex(
        base_dir=str(tmp_path / 'mediaidx'),
        captioner=captioner,
        collection_factory=lambda: col,
    ), col


# ─── 1. index: caption + embed + store ONCE, not re-done ────────────────────

def test_new_file_is_captioned_embedded_and_stored_once(tmp_path):
    calls = {'n': 0}

    def captioner(frame_bytes, prompt):
        calls['n'] += 1
        assert isinstance(frame_bytes, (bytes, bytearray))
        return 'a cat sitting on a blue sofa'

    idx, col = _index(tmp_path, captioner)
    img = _make_image(tmp_path / 'cat.jpg')

    status1 = idx.index_file(img)
    assert status1 == 'indexed'
    assert calls['n'] == 1
    # Stored in the catalog with the caption + marked embedded.
    rec = idx._catalog.get(img)
    assert rec is not None
    assert rec['caption'] == 'a cat sitting on a blue sofa'
    assert rec['embedded'] is True
    assert rec['hash']
    # Embedded into the vector store exactly once.
    assert len(col.added) == 1

    # Idempotent: same content -> skip, captioner NOT called again, no re-embed.
    status2 = idx.index_file(img)
    assert status2 == 'skipped'
    assert calls['n'] == 1
    assert len(col.added) == 1


def test_changed_file_is_reindexed(tmp_path):
    def captioner(frame_bytes, prompt):
        return 'caption ' + str(len(frame_bytes))

    idx, col = _index(tmp_path, captioner)
    img = _make_image(tmp_path / 'pic.png', content=b'aaaa')
    assert idx.index_file(img) == 'indexed'
    # Rewrite with different content -> fingerprint changes -> re-index.
    _make_image(tmp_path / 'pic.png', content=b'bbbbbbbb')
    assert idx.index_file(img) == 'indexed'
    assert len(col.added) == 2


# ─── 2. search: deterministic filename hits THEN semantic caption hits ──────

def test_search_returns_deterministic_then_semantic(tmp_path):
    def captioner(frame_bytes, prompt):
        return 'the sea at sunset'

    col = FakeCollection()
    idx, _ = _index(tmp_path, captioner, collection=col)

    beach = _make_image(tmp_path / 'beach.jpg')
    other = _make_image(tmp_path / 'random.png')
    idx.index_file(beach)
    idx.index_file(other)

    # Seed the semantic side to return the 'random.png' caption hit.
    col._seed = {
        'ids': [['media_x']],
        'documents': [['the sea at sunset']],
        'metadatas': [[{'path': os.path.abspath(other), 'name': 'random.png'}]],
        'distances': [[0.2]],
    }

    results = idx.search('beach', limit=10)
    assert results, 'expected at least the deterministic filename hit'
    # First result is the deterministic filename match on beach.jpg.
    assert results[0]['name'] == 'beach.jpg'
    assert results[0]['match'] in ('exact', 'prefix')
    # The semantic hit on the other file appears too, tagged 'semantic'.
    matches = {r['name']: r['match'] for r in results}
    assert matches.get('random.png') == 'semantic'
    # Deterministic ranks before semantic.
    names = [r['name'] for r in results]
    assert names.index('beach.jpg') < names.index('random.png')


def test_search_semantic_only_when_no_filename_match(tmp_path):
    def captioner(frame_bytes, prompt):
        return 'a mountain lake'

    col = FakeCollection()
    idx, _ = _index(tmp_path, captioner, collection=col)
    f = _make_image(tmp_path / 'IMG_4821.jpg')
    idx.index_file(f)

    col._seed = {
        'ids': [['media_y']],
        'documents': [['a mountain lake']],
        'metadatas': [[{'path': os.path.abspath(f), 'name': 'IMG_4821.jpg'}]],
        'distances': [[0.1]],
    }
    results = idx.search('lake', limit=10)
    assert len(results) == 1
    assert results[0]['match'] == 'semantic'
    assert results[0]['name'] == 'IMG_4821.jpg'


# ─── 3. consent: local index needs none; export is BLOCKED without consent ──

def test_local_index_never_calls_egress_gate(tmp_path, monkeypatch):
    called = {'egress': 0}

    class Guard:
        def check_egress(self, data, dest, context=None):
            called['egress'] += 1
            return True, 'ok'

    import security.edge_privacy as ep
    monkeypatch.setattr(ep, 'get_scope_guard', lambda: Guard())

    idx, _ = _index(tmp_path, lambda fb, p: 'local caption')
    idx.index_file(_make_image(tmp_path / 'a.jpg'))
    # Indexing is fully local -> the egress gate must never be consulted.
    assert called['egress'] == 0


def test_export_blocked_without_consent(tmp_path, monkeypatch):
    class DenyingGuard:
        def check_egress(self, data, dest, context=None):
            return False, 'Scope violation: blocked'

    import security.edge_privacy as ep
    monkeypatch.setattr(ep, 'get_scope_guard', lambda: DenyingGuard())

    idx, _ = _index(tmp_path, lambda fb, p: 'a private family photo')
    idx.index_file(_make_image(tmp_path / 'family.jpg'))

    verdict = idx.export_captions(destination_scope='federated')
    assert verdict['allowed'] is False
    assert 'blocked' in verdict['reason'].lower()
    assert 'payload' not in verdict


def test_export_allowed_when_consent_passes(tmp_path, monkeypatch):
    class AllowGuard:
        def check_egress(self, data, dest, context=None):
            return True, 'Scope check passed'

    import security.edge_privacy as ep
    monkeypatch.setattr(ep, 'get_scope_guard', lambda: AllowGuard())

    idx, _ = _index(tmp_path, lambda fb, p: 'a public landscape')
    idx.index_file(_make_image(tmp_path / 'land.jpg'))

    verdict = idx.export_captions(destination_scope='federated')
    assert verdict['allowed'] is True
    assert verdict['count'] == 1
    assert verdict['payload']['captions'][0]['caption'] == 'a public landscape'


def test_export_exposes_caption_text_to_egress_scanner(tmp_path, monkeypatch):
    # ScopeGuard's DLP/secret scan only inspects TOP-LEVEL string fields. The
    # caption text lives nested in a list, so the export must ALSO surface it as
    # a flat top-level string or the PII scan never sees it. Capture what the
    # guard actually receives and prove the sensitive text is scannable.
    seen = {}

    class SpyGuard:
        def check_egress(self, data, dest, context=None):
            seen['data'] = data
            return True, 'ok'

    import security.edge_privacy as ep
    monkeypatch.setattr(ep, 'get_scope_guard', lambda: SpyGuard())

    idx, _ = _index(tmp_path, lambda fb, p: 'a sign reading a private name')
    idx.index_file(_make_image(tmp_path / 'vacay.jpg'))
    idx.export_captions(destination_scope='federated')

    # At least one top-level, non-underscore string field carries the caption
    # text so ScopeGuard._extract_text (top-level str only) can DLP-scan it.
    flat = [v for k, v in seen['data'].items()
            if not k.startswith('_') and isinstance(v, str)]
    assert any('a sign reading a private name' in v for v in flat), \
        'caption text must be flat-scannable by the egress DLP gate'


# ─── 4. offline / missing model degrades, never crashes ─────────────────────

def test_offline_model_degrades_to_no_caption(tmp_path):
    def offline_captioner(frame_bytes, prompt):
        return None  # model offline / unavailable

    idx, col = _index(tmp_path, offline_captioner)
    img = _make_image(tmp_path / 'x.jpg')
    status = idx.index_file(img)
    assert status == 'no_caption'
    # File is still recorded so deterministic search finds it; no embedding.
    rec = idx._catalog.get(img)
    assert rec is not None
    assert rec['caption'] is None
    assert rec['embedded'] is False
    assert col.added == []
    # Deterministic search still returns it.
    results = idx.search('x', limit=5)
    assert results and results[0]['name'] == 'x.jpg'


def test_search_degrades_when_vector_store_unavailable(tmp_path):
    # collection_factory returns None -> semantic disabled, deterministic works.
    idx = msi.MediaSemanticIndex(
        base_dir=str(tmp_path / 'm'),
        captioner=lambda fb, p: 'cap',
        collection_factory=lambda: None,
    )
    idx.index_file(_make_image(tmp_path / 'holiday.jpg'))
    results = idx.search('holiday', limit=5)
    assert results and results[0]['name'] == 'holiday.jpg'
    assert idx.status()['semantic_available'] is False


def test_index_embed_failure_keeps_catalog_usable(tmp_path):
    class BoomCollection:
        def upsert(self, *a, **k):
            raise RuntimeError('chroma boom')

    idx = msi.MediaSemanticIndex(
        base_dir=str(tmp_path / 'm'),
        captioner=lambda fb, p: 'a caption',
        collection_factory=lambda: BoomCollection(),
    )
    img = _make_image(tmp_path / 'b.jpg')
    status = idx.index_file(img)
    # Captioned + stored, but embedding failed -> embedded False, no crash.
    assert status == 'indexed'
    assert idx._catalog.get(img)['embedded'] is False


# ─── 5. idle work yields to the foreground ──────────────────────────────────

def test_idle_batch_yields_to_foreground(tmp_path):
    calls = {'n': 0}

    def captioner(frame_bytes, prompt):
        calls['n'] += 1
        return 'cap'

    media = tmp_path / 'Pictures'
    media.mkdir()
    _make_image(media / '1.jpg')
    _make_image(media / '2.jpg')

    idx, _ = _index(tmp_path, captioner)

    # Foreground active -> yield -> index nothing.
    n = idx.idle_index_batch(batch_size=10, dirs=[str(media)],
                             yield_check=lambda: True)
    assert n == 0
    assert calls['n'] == 0

    # Foreground clear -> index the batch.
    n2 = idx.idle_index_batch(batch_size=10, dirs=[str(media)],
                              yield_check=lambda: False)
    assert n2 == 2
    assert calls['n'] == 2

    # Re-run with the same files: already indexed -> nothing new.
    n3 = idx.idle_index_batch(batch_size=10, dirs=[str(media)],
                              yield_check=lambda: False)
    assert n3 == 0


def test_register_idle_indexer_is_idempotent(tmp_path, monkeypatch):
    # Force the yield gate True so the spawned loop does no real work.
    monkeypatch.setattr(msi, '_should_yield', lambda: True)
    # Reset module state for a clean assertion.
    monkeypatch.setattr(msi, '_idle_started', False)
    monkeypatch.setattr(msi, '_idle_thread', None)
    started_first = msi.register_idle_indexer(dirs=[str(tmp_path)])
    started_again = msi.register_idle_indexer(dirs=[str(tmp_path)])
    assert started_first is True
    assert started_again is False


# ─── 6. dynamic image cache: fetch-once, serve-from-cache, offline-graceful ─

class FakeResp:
    def __init__(self, status, content=b'', content_type='image/jpeg'):
        self.status_code = status
        self.content = content
        self.headers = {'Content-Type': content_type}


def test_image_cache_fetches_once_then_serves_from_disk(tmp_path, monkeypatch):
    fetches = {'n': 0}

    def fake_get(url, timeout=None, **kw):
        fetches['n'] += 1
        return FakeResp(200, content=b'IMAGEBYTES', content_type='image/png')

    import core.http_pool as hp
    monkeypatch.setattr(hp, 'pooled_get', fake_get)

    cache = msi.ImageCache(base_dir=str(tmp_path / 'c'))
    url = 'https://example.com/news/photo.png'

    p1 = cache.get_path(url)
    assert p1 and os.path.isfile(p1)
    assert fetches['n'] == 1
    with open(p1, 'rb') as f:
        assert f.read() == b'IMAGEBYTES'

    # Second call is a cache hit -> no new fetch.
    p2 = cache.get_path(url)
    assert p2 == p1
    assert fetches['n'] == 1


def test_image_cache_offline_returns_none(tmp_path, monkeypatch):
    def boom(url, timeout=None, **kw):
        raise OSError('network down')

    import core.http_pool as hp
    monkeypatch.setattr(hp, 'pooled_get', boom)

    cache = msi.ImageCache(base_dir=str(tmp_path / 'c'))
    assert cache.get_path('https://example.com/x.jpg') is None


def test_image_cache_lru_eviction(tmp_path, monkeypatch):
    payloads = {}

    def fake_get(url, timeout=None, **kw):
        return FakeResp(200, content=payloads[url], content_type='image/jpeg')

    import core.http_pool as hp
    monkeypatch.setattr(hp, 'pooled_get', fake_get)

    # max_bytes small so the third insert evicts the least-recently-accessed.
    cache = msi.ImageCache(base_dir=str(tmp_path / 'c'), max_bytes=20)
    payloads['u1'] = b'A' * 10
    payloads['u2'] = b'B' * 10
    payloads['u3'] = b'C' * 10

    cache.get_path('u1')
    cache.get_path('u2')
    # Touch u2 so u1 is least-recently-accessed.
    cache.get_path('u2')
    cache.get_path('u3')   # total would be 30 > 20 -> evict u1

    st = cache.stats()
    assert st['bytes'] <= 20
    # u1 evicted: its index entry is gone (re-get would refetch).
    key1 = msi.ImageCache._key('u1')
    assert key1 not in cache._index


# ─── 7. search skips a cold semantic query on an empty store ────────────────

def test_search_skips_semantic_query_when_store_empty(tmp_path):
    # On a brand-new store a query would lazily download the embedding model.
    # With nothing embedded there is nothing to find, so query() must be skipped
    # rather than blocking a user search on that one-time fetch.
    class EmptyCountingCollection:
        def __init__(self):
            self.queried = False

        def count(self):
            return 0

        def query(self, *a, **k):
            self.queried = True
            raise AssertionError('query must not run on an empty store')

    col = EmptyCountingCollection()
    idx = msi.MediaSemanticIndex(
        base_dir=str(tmp_path / 'm'),
        captioner=lambda fb, p: 'cap',
        collection_factory=lambda: col,
    )
    results = idx.search('anything', limit=5)
    assert results == []
    assert col.queried is False


# ─── 8. routes: every endpoint is local-only (loopback OR shell token) ──────

def test_media_routes_are_local_only(tmp_path, monkeypatch):
    from flask import Flask

    monkeypatch.delenv('HART_SHELL_TOKEN', raising=False)
    # Isolate the singleton from real user data for the loopback success case.
    tmpidx, _ = _index(tmp_path, lambda fb, p: 'cap')
    monkeypatch.setattr(msi, 'get_index', lambda: tmpidx)

    app = Flask(__name__)
    msi.register_media_routes(app)
    client = app.test_client()

    remote = {'REMOTE_ADDR': '10.0.0.9'}
    # Non-loopback, no shell token -> rejected on read AND egress routes.
    assert client.get('/api/media/search?q=x',
                      environ_overrides=remote).status_code == 403
    assert client.get('/api/media/index/status',
                      environ_overrides=remote).status_code == 403
    assert client.post('/api/media/export', json={'scope': 'federated'},
                       environ_overrides=remote).status_code == 403
    assert client.get('/api/media/image?url=http://x/y.jpg',
                      environ_overrides=remote).status_code == 403

    # Loopback (default test-client REMOTE_ADDR is 127.0.0.1) is allowed.
    ok = client.get('/api/media/search?q=nothing')
    assert ok.status_code == 200
    assert ok.get_json()['query'] == 'nothing'


# --- 9. degrade-not-crash on unsupported / missing files --------------------

def test_unsupported_file_type_returns_unsupported(tmp_path):
    calls = {'n': 0}

    def captioner(frame_bytes, prompt):
        calls['n'] += 1
        return 'cap'

    idx, col = _index(tmp_path, captioner)
    txt = tmp_path / 'notes.txt'
    txt.write_text('not media')
    status = idx.index_file(str(txt))
    assert status == 'unsupported'
    # Non-media is never captioned, never embedded, never catalogued.
    assert calls['n'] == 0
    assert col.added == []
    assert idx._catalog.get(str(txt)) is None


def test_missing_file_degrades_gracefully(tmp_path):
    calls = {'n': 0}

    def captioner(frame_bytes, prompt):
        calls['n'] += 1
        return 'cap'

    idx, col = _index(tmp_path, captioner)
    ghost = str(tmp_path / 'ghost.jpg')   # never created
    status = idx.index_file(ghost)
    assert status == 'missing'
    assert calls['n'] == 0
    assert col.added == []
    # An unreadable file does not crash and is not recorded.
    assert idx._catalog.get(ghost) is None


def test_video_without_decodable_frame_degrades(tmp_path):
    # A junk .mp4 cannot be opened by the real frame extractor, so the file is
    # recorded WITHOUT a caption rather than crashing, and the captioner is
    # never reached (there is no frame to describe).
    calls = {'n': 0}

    def captioner(frame_bytes, prompt):
        calls['n'] += 1
        return 'a video'

    idx, col = _index(tmp_path, captioner)
    vid = tmp_path / 'clip.mp4'
    vid.write_bytes(b'not a real video container')
    status = idx.index_file(str(vid))
    assert status == 'no_caption'
    assert calls['n'] == 0
    rec = idx._catalog.get(str(vid))
    assert rec is not None and rec['kind'] == 'video' and rec['caption'] is None
    assert col.added == []


# --- 10. search: empty query is a no-op --------------------------------------

def test_search_empty_query_returns_empty(tmp_path):
    idx, _ = _index(tmp_path, lambda fb, p: 'cap')
    idx.index_file(_make_image(tmp_path / 'a.jpg'))
    assert idx.search('') == []
    assert idx.search('   ') == []


# --- 11. idle indexer batches: caps at batch_size + re-checks yield ----------

def test_idle_batch_caps_at_batch_size_and_resumes(tmp_path):
    calls = {'n': 0}

    def captioner(frame_bytes, prompt):
        calls['n'] += 1
        return 'cap %d' % calls['n']

    media = tmp_path / 'Pictures'
    media.mkdir()
    for i in range(5):
        _make_image(media / ('p%d.jpg' % i), content=bytes([i]) * 8)

    idx, _ = _index(tmp_path, captioner)

    # First call indexes exactly batch_size (2), not all five.
    n1 = idx.idle_index_batch(batch_size=2, dirs=[str(media)],
                              yield_check=lambda: False)
    assert n1 == 2
    assert calls['n'] == 2

    # Second call resumes on the still-unindexed files (idempotent skip of the
    # first two), indexing the next batch.
    n2 = idx.idle_index_batch(batch_size=2, dirs=[str(media)],
                              yield_check=lambda: False)
    assert n2 == 2

    # Third call mops up the last one; fourth finds nothing new.
    n3 = idx.idle_index_batch(batch_size=2, dirs=[str(media)],
                              yield_check=lambda: False)
    assert n3 == 1
    n4 = idx.idle_index_batch(batch_size=2, dirs=[str(media)],
                              yield_check=lambda: False)
    assert n4 == 0
    # All five captioned exactly once across the run, never twice.
    assert calls['n'] == 5


def test_idle_batch_rechecks_yield_midbatch(tmp_path):
    # The user starts interacting AFTER the first file: the batch must stop on
    # the next per-file gate check rather than plough through the whole batch.
    calls = {'n': 0}

    def captioner(frame_bytes, prompt):
        calls['n'] += 1
        return 'cap'

    media = tmp_path / 'Pictures'
    media.mkdir()
    for i in range(4):
        _make_image(media / ('q%d.jpg' % i), content=bytes([i + 1]) * 8)

    idx, _ = _index(tmp_path, captioner)

    gate = {'n': 0}

    def flipping_yield():
        # False for the top-of-function guard and the first file, True after,
        # so exactly one file is indexed before the user reclaims the machine.
        gate['n'] += 1
        return gate['n'] > 2

    n = idx.idle_index_batch(batch_size=10, dirs=[str(media)],
                             yield_check=flipping_yield)
    assert n == 1
    assert calls['n'] == 1


# --- 12. the default captioner reuses the existing vision describe() path ----

def test_default_captioner_returns_none_when_backend_offline(tmp_path, monkeypatch):
    import integrations.vision.lightweight_backend as lb
    monkeypatch.setattr(lb, 'get_vision_backend', lambda: None)
    cap = msi._DefaultCaptioner()
    # No backend -> describe is never attempted, caption is None (graceful).
    assert cap(b'\xff\xd8fakejpeg', 'prompt') is None


def test_default_captioner_calls_backend_describe(tmp_path, monkeypatch):
    seen = {}

    class FakeBackend:
        name = 'fake-vlm'

        def start(self):
            seen['started'] = True

        def describe(self, frame_bytes, prompt):
            seen['frame'] = frame_bytes
            seen['prompt'] = prompt
            return '  a red bicycle  '

    import integrations.vision.lightweight_backend as lb
    monkeypatch.setattr(lb, 'get_vision_backend', lambda: FakeBackend())

    cap = msi._DefaultCaptioner()
    out = cap(b'JPEGBYTES', 'Describe this image.')
    # It calls the real describe() path and strips the returned text.
    assert out == 'a red bicycle'
    assert seen['frame'] == b'JPEGBYTES'
    assert seen['prompt'] == 'Describe this image.'
    assert seen.get('started') is True


def test_default_captioner_swallows_backend_exception(tmp_path, monkeypatch):
    class BoomBackend:
        name = 'boom'

        def start(self):
            pass

        def describe(self, frame_bytes, prompt):
            raise RuntimeError('model crashed')

    import integrations.vision.lightweight_backend as lb
    monkeypatch.setattr(lb, 'get_vision_backend', lambda: BoomBackend())
    cap = msi._DefaultCaptioner()
    # A describe() that raises must degrade to None, not propagate.
    assert cap(b'x', 'p') is None


# --- 13. catalog idempotency survives a process restart ----------------------

def test_catalog_persists_across_reload(tmp_path):
    base = str(tmp_path / 'persist')
    img = _make_image(tmp_path / 'keep.jpg', content=b'stable-bytes')

    idx1 = msi.MediaSemanticIndex(
        base_dir=base, captioner=lambda fb, p: 'persisted caption',
        collection_factory=lambda: FakeCollection())
    assert idx1.index_file(img) == 'indexed'

    # A brand-new index over the SAME base dir loads the gzip catalog from disk
    # and treats the unchanged file as already-current -> no recaption.
    calls = {'n': 0}

    def captioner2(fb, p):
        calls['n'] += 1
        return 'should not run'

    idx2 = msi.MediaSemanticIndex(
        base_dir=base, captioner=captioner2,
        collection_factory=lambda: FakeCollection())
    assert idx2.index_file(img) == 'skipped'
    assert calls['n'] == 0


# --- 14. export edge cases: path filter, fail-closed, scope coercion ---------

def test_export_filters_to_requested_paths(tmp_path, monkeypatch):
    class AllowGuard:
        def check_egress(self, data, dest, context=None):
            return True, 'ok'

    import security.edge_privacy as ep
    monkeypatch.setattr(ep, 'get_scope_guard', lambda: AllowGuard())

    idx, _ = _index(tmp_path, lambda fb, p: 'cap-' + os.path.basename(p))
    a = _make_image(tmp_path / 'a.jpg', content=b'aaaa')
    b = _make_image(tmp_path / 'b.jpg', content=b'bbbb')
    idx.index_file(a)
    idx.index_file(b)

    verdict = idx.export_captions(destination_scope='federated', paths=[a])
    assert verdict['allowed'] is True
    assert verdict['count'] == 1
    names = {c['name'] for c in verdict['payload']['captions']}
    assert names == {'a.jpg'}


def test_export_fails_closed_when_edge_privacy_missing(tmp_path, monkeypatch):
    import sys
    # Simulate the edge_privacy module being unavailable: the import inside
    # export_captions raises, and the export must fail CLOSED (never leak).
    monkeypatch.setitem(sys.modules, 'security.edge_privacy', None)

    idx, _ = _index(tmp_path, lambda fb, p: 'a private caption')
    idx.index_file(_make_image(tmp_path / 'p.jpg'))
    verdict = idx.export_captions(destination_scope='federated')
    assert verdict['allowed'] is False
    assert verdict['count'] == 0
    assert 'payload' not in verdict


def test_export_invalid_scope_coerces_to_edge_only(tmp_path, monkeypatch):
    import security.edge_privacy as ep
    seen = {}

    class SpyGuard:
        def check_egress(self, data, dest, context=None):
            seen['dest'] = dest
            return True, 'ok'

    monkeypatch.setattr(ep, 'get_scope_guard', lambda: SpyGuard())

    idx, _ = _index(tmp_path, lambda fb, p: 'cap')
    idx.index_file(_make_image(tmp_path / 'a.jpg'))
    idx.export_captions(destination_scope='not-a-real-scope')
    # An unknown scope is coerced to the most restrictive scope, never silently
    # widened to a broader one.
    assert seen['dest'] == ep.PrivacyScope.EDGE_ONLY


# --- 15. status + singletons -------------------------------------------------

def test_status_reports_counts_and_semantic_availability(tmp_path):
    idx, _ = _index(tmp_path, lambda fb, p: 'a caption')
    idx.index_file(_make_image(tmp_path / 'a.jpg'))
    st = idx.status()
    assert st['total'] == 1
    assert st['captioned'] == 1
    assert st['embedded'] == 1
    assert st['semantic_available'] is True
    assert st['base_dir'].endswith('mediaidx')


def test_get_index_and_image_cache_are_singletons(monkeypatch):
    monkeypatch.setattr(msi, '_index_singleton', None)
    monkeypatch.setattr(msi, '_img_cache_singleton', None)
    assert msi.get_index() is msi.get_index()
    assert msi.get_image_cache() is msi.get_image_cache()


# --- 16. remaining route success paths (loopback) ----------------------------

def test_media_routes_success_paths(tmp_path, monkeypatch):
    from flask import Flask

    monkeypatch.delenv('HART_SHELL_TOKEN', raising=False)

    # An allowing guard so the export route returns an allowed verdict.
    import security.edge_privacy as ep

    class AllowGuard:
        def check_egress(self, data, dest, context=None):
            return True, 'ok'

    monkeypatch.setattr(ep, 'get_scope_guard', lambda: AllowGuard())

    tmpidx, _ = _index(tmp_path, lambda fb, p: 'route caption')
    tmpcache = msi.ImageCache(base_dir=str(tmp_path / 'imgc'))
    monkeypatch.setattr(msi, 'get_index', lambda: tmpidx)
    monkeypatch.setattr(msi, 'get_image_cache', lambda: tmpcache)

    app = Flask(__name__)
    msi.register_media_routes(app)
    client = app.test_client()

    img = _make_image(tmp_path / 'seed.jpg')

    # POST /index indexes a concrete file and reports its per-path status.
    r_index = client.post('/api/media/index', json={'path': img})
    assert r_index.status_code == 200
    assert r_index.get_json()['results'][img] == 'indexed'

    # Missing body -> 400.
    assert client.post('/api/media/index', json={}).status_code == 400

    # GET /index/status returns catalog + image-cache stats.
    r_status = client.get('/api/media/index/status')
    assert r_status.status_code == 200
    body = r_status.get_json()
    assert body['index']['total'] >= 1
    assert 'image_cache' in body

    # POST /index/scan over an empty dir indexes nothing but still 200s.
    empty = tmp_path / 'empty'
    empty.mkdir()
    r_scan = client.post('/api/media/index/scan', json={'dirs': [str(empty)]})
    assert r_scan.status_code == 200
    assert r_scan.get_json()['indexed'] == 0

    # POST /export with consent allowed -> 200 + allowed verdict.
    r_export = client.post('/api/media/export', json={'scope': 'federated'})
    assert r_export.status_code == 200
    assert r_export.get_json()['allowed'] is True

    # GET /image with no url -> 400; an unfetchable url -> 404 (graceful).
    assert client.get('/api/media/image').status_code == 400
    monkeypatch.setattr('core.http_pool.pooled_get',
                        lambda url, timeout=None, **k: (_ for _ in ()).throw(OSError('down')))
    r_img = client.get('/api/media/image?url=https://example.com/x.jpg')
    assert r_img.status_code == 404


def test_media_export_route_blocks_without_consent(tmp_path, monkeypatch):
    from flask import Flask

    monkeypatch.delenv('HART_SHELL_TOKEN', raising=False)
    import security.edge_privacy as ep

    class DenyGuard:
        def check_egress(self, data, dest, context=None):
            return False, 'Scope violation'

    monkeypatch.setattr(ep, 'get_scope_guard', lambda: DenyGuard())

    tmpidx, _ = _index(tmp_path, lambda fb, p: 'sensitive caption')
    tmpidx.index_file(_make_image(tmp_path / 'priv.jpg'))
    monkeypatch.setattr(msi, 'get_index', lambda: tmpidx)

    app = Flask(__name__)
    msi.register_media_routes(app)
    client = app.test_client()

    # Even from loopback, an export the consent gate denies returns 403.
    r = client.post('/api/media/export', json={'scope': 'federated'})
    assert r.status_code == 403
    assert r.get_json()['allowed'] is False
