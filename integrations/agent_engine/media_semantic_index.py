"""
Local Semantic Media Search + Dynamic Image Cache for HART OS.

Everything here is LOCAL by default. Captioning, embedding, indexing and
the web-image cache never leave the device. The ONLY path that lets a
caption / embedding / thumbnail / path leave the perimeter is
``export_captions`` which is gated by the EXISTING consent flow
(``security.edge_privacy.ScopeGuard.check_egress`` + ``PrivacyScope``).
There is no parallel consent path.

REUSES (calls, never rewrites):
  - Vision captioner: ``integrations.vision.lightweight_backend.get_vision_backend``
    (the same describe() path the VisionService uses for camera/screen).
  - Vector store: chromadb collection "media_captions" with chromadb's own
    default embedding function (the "existing chromadb embedding").
  - Data dir: ``core.platform_paths.get_data_dir`` (the canonical user data root).
  - HTTP: ``core.http_pool.pooled_get`` (pooled, always with a timeout).
  - Foreground/idle gate: ``integrations.agent_engine.dispatch.should_yield_to_user``
    (the one canonical gate every background daemon yields on), with a
    ``core.foreground`` fallback.
  - Consent/egress: ``security.edge_privacy.get_scope_guard().check_egress``
    (the federated_aggregator.broadcast_delta pattern).

Storage layout (all under <data>/data/media_index/):
  - catalog.json.gz   gzip-compressed catalog {path: {caption, mtime, hash, ...}}
  - chroma/           chromadb persistent dir holding caption embeddings
  - imgcache/         dynamic web-image LRU cache (bytes + gz index)

Nothing heavy is imported at module load. chromadb, cv2, PIL, flask and the
vision backend are all imported lazily inside the functions that need them, so
importing this module on a degraded boot is cheap and never crashes.
"""

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.media_index')

# ─── Constants ──────────────────────────────────────────────────────────────

COLLECTION_NAME = 'media_captions'
_INDEX_DIRNAME = 'media_index'

# Aligned with the existing shell media indexer extension sets
# (integrations/agent_engine/shell_system_apis.py shell_media_scan). Only image
# + video are captionable; music has no frame to describe.
IMAGE_EXTS = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif',
    '.webp', '.bmp', '.tiff', '.tif',
})
VIDEO_EXTS = frozenset({
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm', '.flv', '.m4v', '.ts',
})

_CAPTION_PROMPT = 'Describe this image in one short factual sentence.'

# Idle indexer cadence (seconds). Conservative so the indexer is invisible.
_IDLE_BATCH_SIZE = 8
_IDLE_YIELD_SLEEP = 30.0     # user active / system hot -> back off
_IDLE_EMPTY_SLEEP = 300.0    # nothing new to index -> sleep long
_IDLE_WORKED_SLEEP = 5.0     # did a batch -> brief pause, then re-check the gate

# Image cache defaults
_IMG_CACHE_MAX_BYTES = 64 * 1024 * 1024   # 64 MB
_IMG_FETCH_TIMEOUT = 8.0


# ─── Path helpers ───────────────────────────────────────────────────────────

def _default_base_dir() -> str:
    """Canonical local index dir under the user data root.

    Reuses core.platform_paths so it lands in the same place as every other
    HARTOS/Nunba data file (~/Documents/Nunba/data/media_index on Windows).
    """
    try:
        from core.platform_paths import get_data_dir
        return os.path.join(get_data_dir(), 'data', _INDEX_DIRNAME)
    except Exception:
        return os.path.join(os.path.expanduser('~'), '.hartos', _INDEX_DIRNAME)


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass


def default_media_dirs() -> List[str]:
    """Default user media directories to index (Pictures / Videos).

    Matches the existing shell_media_scan default so the two indexers see the
    same files instead of inventing a parallel directory concept.
    """
    home = os.path.expanduser('~')
    return [
        os.path.join(home, 'Pictures'),
        os.path.join(home, 'Videos'),
    ]


def _media_kind(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in VIDEO_EXTS:
        return 'video'
    return None


def _fingerprint(path: str) -> Optional[Tuple[str, float, int]]:
    """Cheap content fingerprint: sha1(size + head 64KB). Fast for big videos.

    Returns (hash, mtime, size) or None if the file is unreadable.
    """
    try:
        st = os.stat(path)
        h = hashlib.sha1()
        h.update(str(st.st_size).encode('utf-8'))
        with open(path, 'rb') as f:
            h.update(f.read(65536))
        return h.hexdigest(), float(st.st_mtime), int(st.st_size)
    except (OSError, ValueError):
        return None


def _doc_id(path: str) -> str:
    return 'media_' + hashlib.sha1(os.path.abspath(path).encode('utf-8')).hexdigest()


# ─── Compressed catalog (the local, gzip-compressed source of truth) ────────

class MediaCaptionCatalog:
    """gzip-compressed JSON catalog of indexed media.

    The catalog is the deterministic source of truth (filename/path search +
    idempotency) AND satisfies the "captions stored compressed locally"
    requirement: the whole store is persisted gzip-compressed and written
    atomically (temp file + os.replace) so a crash mid-write never corrupts it.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or _default_base_dir()
        self.path = os.path.join(self.base_dir, 'catalog.json.gz')
        self._lock = threading.RLock()
        self._data: Dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                with gzip.open(self.path, 'rt', encoding='utf-8') as f:
                    self._data = json.load(f) or {}
            except (OSError, json.JSONDecodeError, EOFError):
                self._data = {}
            self._loaded = True

    def save(self) -> bool:
        """Atomically persist the gzip catalog. Best-effort, never raises."""
        with self._lock:
            _ensure_dir(self.base_dir)
            tmp = self.path + '.tmp'
            try:
                with gzip.open(tmp, 'wt', encoding='utf-8') as f:
                    json.dump(self._data, f)
                os.replace(tmp, self.path)
                return True
            except OSError as e:
                logger.debug('catalog save failed: %s', e)
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return False

    def get(self, path: str) -> Optional[dict]:
        self._load()
        with self._lock:
            return self._data.get(os.path.abspath(path))

    def put(self, path: str, record: dict) -> None:
        self._load()
        with self._lock:
            self._data[os.path.abspath(path)] = record

    def items(self) -> List[Tuple[str, dict]]:
        self._load()
        with self._lock:
            return list(self._data.items())

    def __len__(self) -> int:
        self._load()
        with self._lock:
            return len(self._data)

    def is_current(self, path: str, content_hash: str) -> bool:
        """True if this exact content is already indexed (idempotency check)."""
        rec = self.get(path)
        return bool(rec and rec.get('hash') == content_hash)

    def stats(self) -> dict:
        self._load()
        with self._lock:
            total = len(self._data)
            captioned = sum(1 for r in self._data.values() if r.get('caption'))
            embedded = sum(1 for r in self._data.values() if r.get('embedded'))
            kinds: Dict[str, int] = {}
            for r in self._data.values():
                k = r.get('kind', 'unknown')
                kinds[k] = kinds.get(k, 0) + 1
        return {
            'total': total,
            'captioned': captioned,
            'embedded': embedded,
            'by_kind': kinds,
        }


# ─── Captioner (reuses the existing vision describe path) ───────────────────

def _extract_video_frame(path: str) -> Optional[bytes]:
    """Grab a representative (middle) frame from a video as JPEG bytes.

    Degrades to None when OpenCV is unavailable or the file cannot be read.
    """
    try:
        import cv2
    except Exception:
        logger.debug('cv2 unavailable - video captioning skipped')
        return None
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total // 2))
        ok, frame = cap.read()
        if (not ok or frame is None) and total > 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            return None
        ok, buf = cv2.imencode('.jpg', frame)
        if not ok:
            return None
        return buf.tobytes()
    except Exception as e:
        logger.debug('video frame extract failed for %s: %s', path, e)
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def _read_frame_bytes(path: str, kind: str) -> Optional[bytes]:
    """Return JPEG/PNG bytes to caption: the image itself, or a sampled frame."""
    if kind == 'image':
        try:
            with open(path, 'rb') as f:
                return f.read()
        except OSError:
            return None
    if kind == 'video':
        return _extract_video_frame(path)
    return None


class _DefaultCaptioner:
    """Lazy, cached wrapper over the existing vision backend describe() path.

    Selecting + starting a backend can load a model, so we do it once and
    reuse it. When no backend is available (or it is offline) describe()
    returns None and the indexer records the file without a caption - the
    deterministic search still finds it. Never raises.
    """

    def __init__(self):
        self._backend = None
        self._tried = False
        self._lock = threading.Lock()

    def _get_backend(self):
        if self._tried:
            return self._backend
        with self._lock:
            if self._tried:
                return self._backend
            self._tried = True
            try:
                from integrations.vision.lightweight_backend import get_vision_backend
                backend = get_vision_backend()
                if backend is None or backend.name == 'none':
                    self._backend = None
                else:
                    try:
                        backend.start()
                    except Exception:
                        pass
                    self._backend = backend
            except Exception as e:
                logger.debug('vision backend unavailable: %s', e)
                self._backend = None
            return self._backend

    def __call__(self, frame_bytes: bytes, prompt: str) -> Optional[str]:
        backend = self._get_backend()
        if backend is None:
            return None
        try:
            text = backend.describe(frame_bytes, prompt)
        except Exception as e:
            logger.debug('describe failed: %s', e)
            return None
        if not text:
            return None
        return text.strip()


# ─── chromadb vector store adapter (graceful, version tolerant) ─────────────

def _build_collection(base_dir: str):
    """Open (or create) the persistent "media_captions" collection.

    Uses chromadb's own default embedding function (the existing chromadb
    embedding). Returns the collection, or None when chromadb / its embedding
    model is unavailable - in which case semantic search simply degrades to
    deterministic-only. Never raises.
    """
    chroma_dir = os.path.join(base_dir, 'chroma')
    _ensure_dir(chroma_dir)
    try:
        import chromadb
    except Exception as e:
        logger.debug('chromadb import failed: %s', e)
        return None
    # Modern API (>=0.4): PersistentClient. Fall back to the legacy
    # Client(Settings(...)) shape used by chromadb 0.3.x.
    try:
        if hasattr(chromadb, 'PersistentClient'):
            client = chromadb.PersistentClient(path=chroma_dir)
        else:
            from chromadb.config import Settings
            client = chromadb.Client(Settings(
                chroma_db_impl='duckdb+parquet',
                persist_directory=chroma_dir,
            ))
        return client.get_or_create_collection(name=COLLECTION_NAME)
    except Exception as e:
        logger.debug('chroma collection open failed: %s', e)
        return None


# ─── Foreground / idle gate (reuses the one canonical daemon-yield gate) ────

def _should_yield() -> bool:
    """True when a user-facing request is active or the system is hot.

    Reuses the single canonical gate (dispatch.should_yield_to_user); falls
    back to core.foreground. Fail-open False so a missing gate never blocks
    indexing (same contract as every other daemon)."""
    try:
        from integrations.agent_engine.dispatch import should_yield_to_user
        return bool(should_yield_to_user())
    except Exception:
        try:
            from core.foreground import foreground_active
            return bool(foreground_active())
        except Exception:
            return False


# ─── The index ──────────────────────────────────────────────────────────────

class MediaSemanticIndex:
    """Local caption index + deterministic-then-semantic search.

    All boundaries are injectable for testing:
      - ``captioner``: callable(frame_bytes, prompt) -> Optional[str]
      - ``collection_factory``: callable() -> chromadb-like collection or None
      - ``catalog``: a MediaCaptionCatalog instance
    """

    def __init__(self, base_dir: Optional[str] = None,
                 captioner: Optional[Callable[[bytes, str], Optional[str]]] = None,
                 collection_factory: Optional[Callable[[], object]] = None,
                 catalog: Optional[MediaCaptionCatalog] = None):
        self.base_dir = base_dir or _default_base_dir()
        self._catalog = catalog or MediaCaptionCatalog(self.base_dir)
        self._captioner = captioner or _DefaultCaptioner()
        self._collection_factory = collection_factory
        self._collection = None
        self._collection_tried = False
        self._lock = threading.Lock()

    # -- collection (lazy) --

    def _collection_or_none(self):
        if self._collection_tried:
            return self._collection
        with self._lock:
            if self._collection_tried:
                return self._collection
            self._collection_tried = True
            try:
                if self._collection_factory is not None:
                    self._collection = self._collection_factory()
                else:
                    self._collection = _build_collection(self.base_dir)
            except Exception as e:
                logger.debug('collection factory failed: %s', e)
                self._collection = None
            return self._collection

    # -- indexing --

    def index_file(self, path: str) -> str:
        """Caption + embed + store one media file. Idempotent.

        Returns one of:
          'skipped'    already indexed at this content hash
          'indexed'    captioned + (best-effort) embedded + stored
          'no_caption' file recorded but model offline / no frame; no embedding
          'unsupported' not an image/video
          'missing'    file unreadable
        """
        kind = _media_kind(path)
        if kind is None:
            return 'unsupported'
        fp = _fingerprint(path)
        if fp is None:
            return 'missing'
        content_hash, mtime, size = fp

        # Idempotency: identical content already indexed -> skip (no recaption).
        if self._catalog.is_current(path, content_hash):
            return 'skipped'

        frame = _read_frame_bytes(path, kind)
        caption = None
        if frame:
            try:
                caption = self._captioner(frame, _CAPTION_PROMPT)
            except Exception as e:
                logger.debug('captioner raised for %s: %s', path, e)
                caption = None

        record = {
            'path': os.path.abspath(path),
            'name': os.path.basename(path),
            'kind': kind,
            'caption': caption,
            'mtime': mtime,
            'size': size,
            'hash': content_hash,
            'indexed_at': time.time(),
            'embedded': False,
        }

        status = 'no_caption'
        if caption:
            embedded = self._embed(path, caption, content_hash, kind)
            record['embedded'] = embedded
            status = 'indexed'

        self._catalog.put(path, record)
        self._catalog.save()
        return status

    def _embed(self, path: str, caption: str, content_hash: str, kind: str) -> bool:
        """Store the caption embedding in chroma (chroma auto-embeds the doc).

        Best-effort: returns False (and leaves the catalog entry usable for
        deterministic search) when the vector store is unavailable."""
        col = self._collection_or_none()
        if col is None:
            return False
        doc_id = _doc_id(path)
        meta = {'path': os.path.abspath(path), 'name': os.path.basename(path),
                'kind': kind, 'hash': content_hash}
        try:
            if hasattr(col, 'upsert'):
                col.upsert(ids=[doc_id], documents=[caption], metadatas=[meta])
            else:
                try:
                    col.delete(ids=[doc_id])
                except Exception:
                    pass
                col.add(ids=[doc_id], documents=[caption], metadatas=[meta])
            return True
        except Exception as e:
            logger.debug('embed failed for %s: %s', path, e)
            return False

    # -- idle batch (yields to the foreground) --

    def idle_index_batch(self, batch_size: int = _IDLE_BATCH_SIZE,
                         dirs: Optional[List[str]] = None,
                         yield_check: Optional[Callable[[], bool]] = None) -> int:
        """Index up to ``batch_size`` NEW/changed files, yielding to the user.

        Yields FIRST: if the foreground gate says the user is active (or the
        system is hot) this returns 0 immediately and touches nothing - idle
        work never competes with a live interaction. Returns the number of
        files actually captioned/recorded this call."""
        yc = yield_check or _should_yield
        try:
            if yc():
                return 0
        except Exception:
            pass

        search_dirs = dirs or default_media_dirs()
        done = 0
        for fpath in self._iter_media_files(search_dirs):
            if done >= batch_size:
                break
            # Re-check the gate between files so a user who starts interacting
            # mid-batch reclaims the machine within one file.
            try:
                if yc():
                    break
            except Exception:
                pass
            fp = _fingerprint(fpath)
            if fp is None:
                continue
            if self._catalog.is_current(fpath, fp[0]):
                continue
            status = self.index_file(fpath)
            if status in ('indexed', 'no_caption'):
                done += 1
        return done

    def _iter_media_files(self, dirs: List[str]):
        for directory in dirs:
            if not directory or not os.path.isdir(directory):
                continue
            for root, subdirs, files in os.walk(directory):
                subdirs[:] = [d for d in subdirs if not d.startswith('.')]
                for fname in files:
                    if _media_kind(fname) is not None:
                        yield os.path.join(root, fname)

    # -- search (deterministic first, then semantic) --

    def search(self, query: str, limit: int = 20) -> List[dict]:
        """Deterministic filename/path hits FIRST, then semantic caption hits.

        Always returns the deterministic hits even when the vector store is
        empty or the embedding model is offline. Never raises."""
        q = (query or '').strip()
        if not q:
            return []
        ql = q.lower()

        seen = set()
        results: List[dict] = []

        # 1. Deterministic: exact then prefix on filename and full path.
        exact, prefix = [], []
        for path, rec in self._catalog.items():
            name = (rec.get('name') or os.path.basename(path)).lower()
            pl = path.lower()
            if name == ql or pl == ql:
                exact.append((path, rec))
            elif name.startswith(ql) or pl.startswith(ql) or ql in name:
                prefix.append((path, rec))

        for match, bucket in (('exact', exact), ('prefix', prefix)):
            for path, rec in bucket:
                if path in seen:
                    continue
                seen.add(path)
                results.append(self._result(path, rec, match, score=1.0))
                if len(results) >= limit:
                    return results

        # 2. Semantic: embed the query, similarity over caption embeddings.
        col = self._collection_or_none()
        if col is not None:
            try:
                # Embedding the query lazily downloads chromadb's embedding model
                # on a brand-new store. If nothing is embedded yet there are no
                # semantic hits to find, so skip the query and never block a user
                # search on that one-time fetch (deterministic hits already stand).
                count = None
                if hasattr(col, 'count'):
                    try:
                        count = col.count()
                    except Exception:
                        count = None
                if count == 0:
                    return results
                n = max(1, limit - len(results))
                res = col.query(query_texts=[q], n_results=n)
                ids = (res.get('ids') or [[]])[0]
                metas = (res.get('metadatas') or [[]])[0]
                docs = (res.get('documents') or [[]])[0]
                dists = (res.get('distances') or [[]])[0]
                for i in range(len(ids)):
                    meta = metas[i] if i < len(metas) else {}
                    path = (meta or {}).get('path')
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    rec = self._catalog.get(path) or {}
                    if not rec.get('caption') and i < len(docs):
                        rec = dict(rec, caption=docs[i])
                    dist = dists[i] if i < len(dists) and dists[i] is not None else None
                    score = max(0.0, 1.0 - float(dist)) if dist is not None else 0.5
                    results.append(self._result(path, rec, 'semantic', score=score))
                    if len(results) >= limit:
                        break
            except Exception as e:
                logger.debug('semantic query failed (degrading to deterministic): %s', e)

        return results

    @staticmethod
    def _result(path: str, rec: dict, match: str, score: float) -> dict:
        return {
            'path': path,
            'name': rec.get('name') or os.path.basename(path),
            'kind': rec.get('kind'),
            'caption': rec.get('caption'),
            'match': match,
            'score': round(float(score), 4),
        }

    # -- consent-gated export (the ONLY egress path) --

    def export_captions(self, destination_scope: str = 'federated',
                        paths: Optional[List[str]] = None,
                        context: Optional[dict] = None) -> dict:
        """Gate an export of captions/embeddings through the EXISTING consent flow.

        Local index/caption/cache need NO consent. Anything LEAVING the
        perimeter does: this reuses security.edge_privacy.ScopeGuard.check_egress
        (the federated_aggregator.broadcast_delta pattern). Default is
        local-only; the caller gets a blocked verdict unless the scope check
        passes. This function does NOT transmit - it returns the vetted payload
        so the existing federation/sync transport can carry it (a follow-up).
        """
        items = self._catalog.items()
        if paths is not None:
            wanted = {os.path.abspath(p) for p in paths}
            items = [(p, r) for p, r in items if p in wanted]

        captions = [
            {'path': p, 'name': r.get('name'), 'caption': r.get('caption')}
            for p, r in items if r.get('caption')
        ]
        payload = {'captions': captions}
        try:
            from security.edge_privacy import get_scope_guard, PrivacyScope
            try:
                dest = PrivacyScope(destination_scope)
            except ValueError:
                dest = PrivacyScope.EDGE_ONLY
            guard = get_scope_guard()
            # ScopeGuard._extract_text only scans TOP-LEVEL string fields, so the
            # caption / filename / path text nested inside the 'captions' list
            # would slip past the DLP + secret scanners. Surface every caption,
            # name and path as one flat top-level string (non-underscore key) so
            # check_egress actually inspects the sensitive text for PII / secrets
            # before anything derived from a private photo leaves the device.
            scan_text = '\n'.join(
                str(v) for c in captions
                for v in (c.get('caption'), c.get('name'), c.get('path')) if v
            )
            tagged = dict(payload, scan_text=scan_text, _privacy_scope=dest)
            allowed, reason = guard.check_egress(
                tagged, dest, context=context or {'source': 'media_caption_export'})
        except ImportError:
            # No edge-privacy module -> fail CLOSED: never leak without the gate.
            return {'allowed': False,
                    'reason': 'edge_privacy unavailable - export blocked',
                    'count': 0}
        if not allowed:
            return {'allowed': False, 'reason': reason, 'count': 0}
        return {'allowed': True, 'reason': reason,
                'count': len(captions), 'payload': payload}

    def status(self) -> dict:
        st = self._catalog.stats()
        st['semantic_available'] = self._collection_or_none() is not None
        st['base_dir'] = self.base_dir
        return st


# ─── Module singletons ──────────────────────────────────────────────────────

_index_singleton: Optional[MediaSemanticIndex] = None
_index_lock = threading.Lock()


def get_index() -> MediaSemanticIndex:
    global _index_singleton
    if _index_singleton is None:
        with _index_lock:
            if _index_singleton is None:
                _index_singleton = MediaSemanticIndex()
    return _index_singleton


# ─── Idle indexer registration (no agent_daemon edit needed) ────────────────

_idle_thread: Optional[threading.Thread] = None
_idle_started = False
_idle_lock = threading.Lock()


def register_idle_indexer(dirs: Optional[List[str]] = None) -> bool:
    """Start the low-priority idle media indexer on a daemon thread.

    Self-contained: it reuses the canonical foreground/idle gate
    (should_yield_to_user) and never runs while the user is interacting, so it
    needs no edit to agent_daemon. Idempotent - calling twice is a no-op.
    Returns True if this call started the thread."""
    global _idle_thread, _idle_started
    with _idle_lock:
        if _idle_started and _idle_thread is not None and _idle_thread.is_alive():
            return False
        _idle_started = True

        def _loop():
            logger.info('media idle indexer started')
            while True:
                try:
                    if _should_yield():
                        time.sleep(_IDLE_YIELD_SLEEP)
                        continue
                    worked = get_index().idle_index_batch(
                        batch_size=_IDLE_BATCH_SIZE, dirs=dirs)
                    if worked == 0:
                        time.sleep(_IDLE_EMPTY_SLEEP)
                    else:
                        time.sleep(_IDLE_WORKED_SLEEP)
                except Exception as e:
                    logger.debug('idle indexer tick error: %s', e)
                    time.sleep(_IDLE_EMPTY_SLEEP)

        _idle_thread = threading.Thread(
            target=_loop, daemon=True, name='media-idle-indexer')
        _idle_thread.start()
        return True


# ─── Dynamic web-image cache (local, compressed index, size-bounded LRU) ────

class ImageCache:
    """Fetch-once, serve-from-cache LRU for images the UI pulls from news/web.

    LOCAL only. Bytes are stored content-addressed (image formats are already
    entropy-coded so re-gzipping them wastes CPU); the cache INDEX is gzip
    -compressed on disk. Bounded by total bytes with least-recently-accessed
    eviction. Fetches use the pooled HTTP client with an explicit timeout and
    degrade to None when offline. Never raises."""

    def __init__(self, base_dir: Optional[str] = None,
                 max_bytes: int = _IMG_CACHE_MAX_BYTES):
        root = base_dir or _default_base_dir()
        self.dir = os.path.join(root, 'imgcache')
        self.index_path = os.path.join(self.dir, 'index.json.gz')
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._index: Dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                with gzip.open(self.index_path, 'rt', encoding='utf-8') as f:
                    self._index = json.load(f) or {}
            except (OSError, json.JSONDecodeError, EOFError):
                self._index = {}
            self._loaded = True

    def _save(self) -> None:
        _ensure_dir(self.dir)
        tmp = self.index_path + '.tmp'
        try:
            with gzip.open(tmp, 'wt', encoding='utf-8') as f:
                json.dump(self._index, f)
            os.replace(tmp, self.index_path)
        except OSError as e:
            logger.debug('imgcache index save failed: %s', e)

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha1(url.encode('utf-8')).hexdigest()

    @staticmethod
    def _ext_from(url: str, content_type: str) -> str:
        ct = (content_type or '').lower()
        for needle, ext in (('jpeg', '.jpg'), ('jpg', '.jpg'), ('png', '.png'),
                            ('webp', '.webp'), ('gif', '.gif'), ('bmp', '.bmp')):
            if needle in ct:
                return ext
        base = os.path.splitext(url.split('?')[0])[1].lower()
        return base if base in IMAGE_EXTS else '.img'

    def get_path(self, url: str, timeout: float = _IMG_FETCH_TIMEOUT) -> Optional[str]:
        """Return a local file path for ``url``, fetching once if needed.

        Cache hit -> path immediately. Miss -> fetch via pooled_get (timeout),
        store, return path. Offline / non-200 -> None (graceful)."""
        if not url:
            return None
        self._load()
        key = self._key(url)
        with self._lock:
            rec = self._index.get(key)
            if rec:
                fpath = os.path.join(self.dir, rec['file'])
                if os.path.isfile(fpath):
                    rec['last_access'] = time.time()
                    self._save()
                    return fpath
                # File vanished under us - drop the stale index entry.
                self._index.pop(key, None)

        data, content_type = self._fetch(url, timeout)
        if data is None:
            return None

        fname = key + self._ext_from(url, content_type)
        fpath = os.path.join(self.dir, fname)
        _ensure_dir(self.dir)
        try:
            with open(fpath, 'wb') as f:
                f.write(data)
        except OSError as e:
            logger.debug('imgcache write failed: %s', e)
            return None

        with self._lock:
            self._index[key] = {
                'url': url, 'file': fname, 'size': len(data),
                'fetched_at': time.time(), 'last_access': time.time(),
            }
            self._evict_locked()
            self._save()
        return fpath

    def get_bytes(self, url: str, timeout: float = _IMG_FETCH_TIMEOUT) -> Optional[bytes]:
        fpath = self.get_path(url, timeout)
        if not fpath:
            return None
        try:
            with open(fpath, 'rb') as f:
                return f.read()
        except OSError:
            return None

    @staticmethod
    def _fetch(url: str, timeout: float) -> Tuple[Optional[bytes], str]:
        try:
            from core.http_pool import pooled_get
            resp = pooled_get(url, timeout=timeout)
            if getattr(resp, 'status_code', 0) != 200:
                return None, ''
            content_type = ''
            try:
                content_type = resp.headers.get('Content-Type', '')
            except Exception:
                pass
            return resp.content, content_type
        except Exception as e:
            logger.debug('image fetch failed (offline?) %s: %s', url, e)
            return None, ''

    def _evict_locked(self) -> None:
        total = sum(r.get('size', 0) for r in self._index.values())
        if total <= self.max_bytes:
            return
        # Least-recently-accessed first.
        ordered = sorted(self._index.items(),
                         key=lambda kv: kv[1].get('last_access', 0))
        for key, rec in ordered:
            if total <= self.max_bytes:
                break
            try:
                os.remove(os.path.join(self.dir, rec['file']))
            except OSError:
                pass
            total -= rec.get('size', 0)
            self._index.pop(key, None)

    def stats(self) -> dict:
        self._load()
        with self._lock:
            return {
                'entries': len(self._index),
                'bytes': sum(r.get('size', 0) for r in self._index.values()),
                'max_bytes': self.max_bytes,
                'dir': self.dir,
            }


_img_cache_singleton: Optional[ImageCache] = None
_img_cache_lock = threading.Lock()


def get_image_cache() -> ImageCache:
    global _img_cache_singleton
    if _img_cache_singleton is None:
        with _img_cache_lock:
            if _img_cache_singleton is None:
                _img_cache_singleton = ImageCache()
    return _img_cache_singleton


# ─── Flask routes ───────────────────────────────────────────────────────────

def register_media_routes(app) -> None:
    """Register the local media search + image cache endpoints.

    Every endpoint serves the local desktop shell only. The shell wiring into
    liquid_ui_service is a follow-up since that file is held by another
    workflow.

      GET  /api/media/search?q=<query>&limit=<n>   deterministic + semantic
      GET  /api/media/index/status                 catalog + cache stats
      POST /api/media/index   {path | paths:[...]} index file(s) now
      POST /api/media/index/scan {dirs?, batch?}   run one idle batch now
      GET  /api/media/image?url=<url>              fetch-once cached web image
      POST /api/media/export  {scope, paths?}      consent-gated egress (blocked
                                                   unless ScopeGuard allows)
    """
    from flask import jsonify, request, send_file
    # Reuse the canonical local-shell auth + audit helpers from the same package
    # instead of minting a parallel local-only check. Loopback OR a matching
    # X-Shell-Token is required. Every route below is local-only because the
    # catalog (private-photo captions + absolute paths) must never be readable
    # by a non-local client, and the image route must not become an open
    # fetch-any-URL proxy for the network.
    from integrations.agent_engine.shell_system_apis import (
        _require_system_auth, _audit_system_op)

    @app.route('/api/media/search', methods=['GET'])
    @_require_system_auth
    def media_search():
        q = request.args.get('q', '')
        try:
            limit = max(1, min(100, int(request.args.get('limit', 20))))
        except (TypeError, ValueError):
            limit = 20
        results = get_index().search(q, limit=limit)
        return jsonify({'query': q, 'count': len(results), 'results': results})

    @app.route('/api/media/index/status', methods=['GET'])
    @_require_system_auth
    def media_index_status():
        return jsonify({
            'index': get_index().status(),
            'image_cache': get_image_cache().stats(),
        })

    @app.route('/api/media/index', methods=['POST'])
    @_require_system_auth
    def media_index():
        data = request.get_json(silent=True) or {}
        paths = data.get('paths')
        if not paths and data.get('path'):
            paths = [data['path']]
        if not paths:
            return jsonify({'error': 'path or paths required'}), 400
        idx = get_index()
        out = {p: idx.index_file(p) for p in paths}
        return jsonify({'results': out})

    @app.route('/api/media/index/scan', methods=['POST'])
    @_require_system_auth
    def media_index_scan():
        data = request.get_json(silent=True) or {}
        dirs = data.get('dirs')
        try:
            batch = max(1, min(100, int(data.get('batch', _IDLE_BATCH_SIZE))))
        except (TypeError, ValueError):
            batch = _IDLE_BATCH_SIZE
        n = get_index().idle_index_batch(batch_size=batch, dirs=dirs)
        return jsonify({'indexed': n})

    @app.route('/api/media/image', methods=['GET'])
    @_require_system_auth
    def media_image():
        url = request.args.get('url', '')
        if not url:
            return jsonify({'error': 'url required'}), 400
        path = get_image_cache().get_path(url)
        if not path:
            return jsonify({'error': 'unavailable', 'url': url}), 404
        try:
            return send_file(path)
        except Exception:
            return jsonify({'error': 'unavailable', 'url': url}), 404

    @app.route('/api/media/export', methods=['POST'])
    @_require_system_auth
    def media_export():
        data = request.get_json(silent=True) or {}
        scope = data.get('scope', 'federated')
        paths = data.get('paths')
        verdict = get_index().export_captions(destination_scope=scope, paths=paths)
        # Record the egress decision in the immutable audit log (allow OR block).
        _audit_system_op('media_caption_export',
                         {'allowed': verdict.get('allowed'),
                          'count': verdict.get('count'), 'scope': scope})
        code = 200 if verdict.get('allowed') else 403
        return jsonify(verdict), code

    logger.info('media search + image cache routes registered')


# Backwards-friendly alias (some callers expect register_*_routes naming).
register_media_search_routes = register_media_routes
