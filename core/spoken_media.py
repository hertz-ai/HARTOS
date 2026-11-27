"""core.spoken_media -- where a node's spoken media lives: the ONE folder list, reader and pruner.

A reply's voice (each TTS engine's output folder) and, since the stored face (owner 2026-11-26: "just
like audio we could store the generated video after live stream"), its face (avatarforcing's output
folder: servers/avatarforcing/stream_server.save_stream) are files on this node.  Nunba serves them at
/tts/audio/<file> and /avatar/live/<sid>.mp4 through find_spoken_media; the lifecycle's disk phase
prunes them through prune_spoken_media.

Two lists, one inside the other (review hartos-17, P1 and C1-C5):
- spoken_media_dirs(), SERVED: every `<models root>/*/output` folder (as Nunba's voice route always
  read), the named folders below, and Nunba's voice caches.  Reading never deletes, so wide is safe here.
- prunable_dirs(), PRUNED: a NAMED subset only.  acestep/output is NOT in it: media_agent
  ._keep_composition keeps compositions there for good (game memos), and a wide prune list deleted them.
  A folder missing from the subset only grows, as every folder did before this module; it never loses a
  file.  Each prunable folder and its writers, none of which keeps a file for good (each file is a cache
  of one spoken reply, made again on demand):
  * the output_subdir of every TTS engine's ToolWorker named in tts_router.ENGINE_REGISTRY (chatterbox,
    cosyvoice, f5_tts, indic_parler, kokoro, melotts, mms_tts, neutts, omnivoice, xtts): ToolWorker
    ._get_output_dir names each reply <tool>_<ms>.wav;
  * luxtts/output: luxtts_tool._get_output_dir (luxtts_tool.py:66-80), luxtts_<hash>.wav;
  * tts/output: pocket_tts_tool._get_output_dir (pocket_tts_tool.py:54-69), tts_<hash>.wav;
  * tts_memo/output: tts_router._memo_dir, <uuid>.wav; the memo deletes nothing itself, so this pruner is
    its one deleter (a hand-out refreshes a file's time, so a file in use goes last; a removed one is
    spoken again);
  * avatarforcing/output: stream_server.save_stream, <sid>.mp4 (a reply's face);
  * Nunba's piper/vibevoice/tts_cache folders: its own engines' hash-named caches.

Housekeeping before this, by reading (2026-11-27): core.platform_paths.cleanup_old_logs (logs),
core.user_file_store.gc (uploads), integrations/channels/media/files.cleanup_temp (channel media temp).
None touched these folders, so nothing capped a reply's voice either; this one cap covers voice AND face.
"""
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: A served name: a plain file name (no separator, no '..', no leading dot), a media extension.
#: A leading underscore IS a name the voice route served before this module (tts_engine's
#: multi-voice output is _tts_multi_<x>.wav), so it stays servable.
_NAME_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}')
#: The media this store holds, and the type each is served as.
MEDIA_TYPES = {'.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg', '.mp4': 'video/mp4'}
#: The share of the free disk (where the folders live) spoken media may hold, evaluated against
#: free + held so evicting does not move the target.  A share of free disk, never a fixed size
#: (owner 2026-11-25); the env override is for operators.
SPOKEN_MEDIA_PCT_OF_FREE = float(os.environ.get('HEVOLVE_SPOKEN_MEDIA_PCT_OF_FREE', '5'))
#: A .part older than this is a face that never finished being written (a crash mid-write): removed.
STALE_PART_S = 3600.0
#: An agent's idle face (integrations/service_tools/avatarforcing_idle): served, never pruned here.
IDLE_PREFIX = 'idle_'
#: Files a sweep found open (Windows refuses to delete them): logged once each, retried next tick.
_busy_logged = set()
#: The folder list is re-globbed at most every DIRS_TTL_S: a glob per served file cost ~2 ms, which is
#: why Nunba's voice route cached its own list for 30 s before it read this one.  Keyed by the home and
#: the models root, so a moved root is seen at once.
DIRS_TTL_S = 30.0
_dirs_memo = {'key': None, 'at': 0.0, 'dirs': []}


def spoken_media_dirs() -> List[Path]:
    """Every folder a reply's voice or face is written to on this node.  The system temp folder is
    NOT one: some TTS fallbacks write there, and Nunba still finds those as before, but a temp folder
    is the OS's to sweep, and a face is never served from it."""
    # The models root is model_storage's (HEVOLVE_MODEL_DIR, default ~/.hevolve/models): the same
    # resolver the engines' output folders and the TTS memo are placed by, so an operator who moves
    # the models off the home drive moves what is served and pruned with them.
    from integrations.service_tools.model_storage import get_base_dir
    home, base = Path.home(), get_base_dir()
    key, now = (str(home), str(base)), time.monotonic()
    if _dirs_memo['key'] == key and now - _dirs_memo['at'] < DIRS_TTL_S:
        return list(_dirs_memo['dirs'])
    # Served: every model's output folder (as the voice route always read) and the named prunable ones
    # (so prunable is a subset by construction, C1), then Nunba's caches.
    dirs = sorted(set(base.glob('*/output')) | {base / s for s in NAMED_PRUNABLE})
    dirs += _nunba_caches(home)
    _dirs_memo.update(key=key, at=now, dirs=dirs)
    return list(dirs)


#: What a caller may ask for.  Each route names its kind, so the voice route (no owner check: a
#: voice is served by its file name alone) can never hand out a stored face, which only the
#: owner-checked /avatar/live route serves.
KINDS = ('audio', 'video')


#: Writers with no ToolWorker object, by the folder their own resolver gives (C3): read as data, never
#: by calling the resolver (each one makes its folder; readers create nothing, owner 2026-11-24).
NAMED_PRUNABLE = (
    'luxtts/output',         # luxtts_tool._get_tts_dir() / 'output'      (luxtts_tool.py:66-80)
    'tts/output',            # pocket_tts_tool._get_tts_dir() / 'output'  (pocket_tts_tool.py:54-69)
    'tts_memo/output',       # tts_router._memo_dir(), get_base_dir() / 'tts_memo' / 'output'
    'avatarforcing/output',  # stream_server.OUTPUT_DIR (save_stream)
)
_engine_subdirs_memo = {'subdirs': None}


def _engine_output_subdirs() -> List[str]:
    """The output_subdir string of every TTS engine's ToolWorker in tts_router.ENGINE_REGISTRY, read
    as data (C2: gpu_worker._get_output_dir would make the folder).  Tool modules import nothing heavy
    at top level; an engine whose module will not import is left out (its folder is then not pruned,
    the safe direction).  Read once per process: the registry is static."""
    if _engine_subdirs_memo['subdirs'] is not None:
        return list(_engine_subdirs_memo['subdirs'])
    import importlib
    out = set()
    try:
        from integrations.channels.media.tts_router import ENGINE_REGISTRY
    except Exception as e:
        logger.warning(f"spoken media: no TTS engine registry ({type(e).__name__}: {e}); only the named "
                       f"folders are pruned")
        return []
    for spec in ENGINE_REGISTRY.values():
        mod_name, attr = getattr(spec, 'tool_module', None), getattr(spec, 'tool_worker_attr', None)
        if not (mod_name and attr):
            continue
        try:
            sub = getattr(getattr(importlib.import_module(mod_name), attr, None), 'output_subdir', None)
        except Exception:
            continue
        if isinstance(sub, str) and sub.endswith('/output') and '..' not in sub and not sub.startswith('/'):
            out.add(sub)
    _engine_subdirs_memo['subdirs'] = sorted(out)
    return sorted(out)


def _nunba_caches(home: Path) -> List[Path]:
    return [home / '.nunba' / 'piper' / 'cache',
            home / '.nunba' / 'vibevoice' / 'cache',
            home / '.nunba' / 'tts_cache' / 'presynth',
            home / '.nunba' / 'tts_cache']


def prunable_dirs() -> List[Path]:
    """The folders prune_spoken_media may empty: a NAMED subset of spoken_media_dirs() (see the module
    note).  Builds paths only: it creates no folder and imports no model library."""
    from integrations.service_tools.model_storage import get_base_dir
    base = get_base_dir()
    subdirs = set(NAMED_PRUNABLE) | set(_engine_output_subdirs())
    return sorted(base / s for s in subdirs) + _nunba_caches(Path.home())


def find_spoken_media(filename, *, kind: str) -> Optional[Path]:
    """The stored voice (``kind='audio'``) or face (``kind='video'``) named ``filename``, or None.
    Plain media names only: a path, a '..', a '.part' (a face still being written), another kind
    or any other type of file is never served."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    if not isinstance(filename, str) or '..' in filename or not _NAME_RE.fullmatch(filename):
        return None
    if not MEDIA_TYPES.get(Path(filename).suffix.lower(), '').startswith(kind + '/'):
        return None
    for d in spoken_media_dirs():
        p = d / filename
        if p.is_file():
            return p
    return None


def media_type(path) -> str:
    """The type a stored file is served as."""
    return MEDIA_TYPES.get(Path(path).suffix.lower(), 'application/octet-stream')


def _audited(owner_path: Path) -> bool:
    """Whether a face's ``.owner`` record says it is on the audit trail (avatarforcing_tool.settle_faces)."""
    try:
        import json
        return bool(json.loads(owner_path.read_text(encoding='utf-8')).get('audited'))
    except (OSError, ValueError, AttributeError):
        return False


def _remove(p: Path, counts: Dict[str, int]) -> bool:
    try:
        p.unlink()
        _busy_logged.discard(str(p))
        return True
    except FileNotFoundError:
        return False
    except PermissionError:  # open (being played or written): next tick
        counts['busy'] += 1
        if str(p) not in _busy_logged:
            _busy_logged.add(str(p))
            logger.info(f"spoken media: {p.name} is open; kept until a later sweep")
        return False
    except OSError as e:
        logger.debug(f"spoken media: {p.name} not removed: {e}")
        return False


def prune_spoken_media(*, pct_of_free: Optional[float] = None, now: Optional[float] = None,
                       free_bytes: Optional[Callable[[str], int]] = None) -> Dict[str, int]:
    """Keep the spoken-media folders within ``pct_of_free`` % of the disk they live on, OLDEST FIRST
    by modification time (NTFS often keeps no access time, so this is oldest-first, not LRU), and
    remove stale .part files.  A voice and a face are removed by the same rule.  Returns counts."""
    from core.platform_paths import free_bytes as _platform_free_bytes
    pct = SPOKEN_MEDIA_PCT_OF_FREE if pct_of_free is None else float(pct_of_free)
    now = time.time() if now is None else now
    counts = {'files': 0, 'bytes': 0, 'busy': 0, 'stale_parts': 0}
    files, stale = [], []
    waiting = 0  # faces not yet on the audit trail: held, not prunable yet
    for d in prunable_dirs():  # never spoken_media_dirs(): kept compositions live in a served folder
        try:
            entries = list(d.iterdir()) if d.is_dir() else []
        except OSError:
            continue
        for p in entries:
            try:
                if not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            if p.name.startswith(IDLE_PREFIX):
                continue  # an agent's idle face: one per avatar, remade only at a 60 s GPU cost
            if p.suffix == '.owner':  # a face's owner and audit record (avatarforcing_tool)
                if (_audited(p) and now - st.st_mtime > STALE_PART_S
                        and not p.with_suffix('.mp4').exists()
                        and not p.with_name(p.stem + '.mp4.part').exists()):
                    stale.append(p)  # audited, and its face was stopped or pruned
                continue
            if p.suffix == '.mp4' and p.with_suffix('.owner').exists() and not _audited(
                    p.with_suffix('.owner')):
                waiting += st.st_size  # still counted against the share (review rv_face2 D6)
                continue  # a face is never pruned before it is on the audit trail (MUST 4)
            if p.name.endswith('.part'):
                if now - st.st_mtime > STALE_PART_S:
                    stale.append(p)
                continue
            if p.suffix.lower() in MEDIA_TYPES:
                # One file on disk under two names (a replay's hard link) is keyed by its inode; any
                # other by its path, since a volume that reports no inode (st_ino 0, a network share)
                # would key every file alike and nothing would ever be freed (review rv_face2 D).
                key = (st.st_dev, st.st_ino) if st.st_nlink > 1 and st.st_ino else str(p)
                files.append((st.st_mtime, st.st_size, p, key))
    for p in stale:
        if _remove(p, counts):
            counts['stale_parts'] += 1
    # A replayed face is a hard link to one already kept: one file on disk, counted once, and freed
    # only when its last name goes (review rv_face2).
    names_left = {}
    sizes = {}
    for _m, size, _p, ino in files:
        names_left[ino] = names_left.get(ino, 0) + 1
        sizes[ino] = size
    held = sum(sizes.values()) + waiting
    if waiting:
        logger.warning(f"spoken media: {waiting >> 10} KB of faces are waiting for their audit line "
                       f"(avatarforcing_tool.settle_faces) and cannot be pruned yet")
    # The disk the stored faces and voices live on: the models root (HEVOLVE_MODEL_DIR may move it off
    # the home drive), else the home (review rv_lowvram nit).
    from integrations.service_tools.model_storage import get_base_dir
    root = get_base_dir()
    free = (free_bytes or _platform_free_bytes)(str(root if root.exists() else Path.home()))
    cap = (free + held) * pct / 100.0
    for _mtime, size, p, ino in sorted(files, key=lambda f: (f[0], str(f[2]))):
        if held <= cap:
            break
        if _remove(p, counts):
            names_left[ino] -= 1
            counts['files'] += 1
            if names_left[ino] <= 0:
                held -= size
                counts['bytes'] += size
            owner = p.with_suffix('.owner')
            if p.suffix == '.mp4' and owner.exists():
                _remove(owner, counts)  # a pruned face takes its owner record with it
    if counts['files'] or counts['stale_parts']:
        logger.info(f"spoken media: removed {counts['files']} file(s), {counts['bytes'] >> 20} MB, and "
                    f"{counts['stale_parts']} unfinished face(s); cap {pct:g}% of free disk")
    return counts
