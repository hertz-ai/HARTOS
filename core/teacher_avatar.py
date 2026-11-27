"""A teacher avatar's image and voice, looked up by its avatar id.

Central's schema (Hevolve_Database sql/models.py) keys a character's look AND
its voice by the avatar: a ``teacher_avatar`` row carries ``image_url`` and a
``voice_id``, and ``voice_id`` names the ``voice_sample`` row whose
``voice_sample_url`` is the recording a cloning TTS engine speaks from.
Android uploads the two through MakeItTalk /upload_image/ and /upload_audio/
under one request_id, and the database links them.

ONE LOOKUP. It lived inline in the Generate_video tool (core/agent_tools.py),
the only reader of an avatar's voice until a spoken reply needed the same
answer.  It moved here unchanged, so a video and a spoken reply resolve an
avatar identically; a second copy is how the two would drift.

IDS (owner ruling 2026-09-14). ``teacher_avatar_id`` is the AVATAR id: one
avatar can front many agents, and ``prompt_id`` is the agent.  One agent can
also speak as several avatars (a story agent voices its characters), so a
voice is chosen per utterance, by that utterance's avatar id, never per agent.
"""
import logging
import threading
import time
from typing import Optional

from core.http_pool import pooled_get

logger = logging.getLogger(__name__)

#: The largest avatar id a client can carry: Android types teacher_avatar_id
#: as a Java Integer (AbstractChatActivity.java, VirtualTeacherResponse.java).
MAX_AVATAR_ID = 2 ** 31 - 1
#: The first avatar id a Nunba node hands out for an avatar made on it (Nunba
#: routes/db_routes.py LOCAL_AVATAR_ID_BASE: central's ids are in the thousands,
#: so a local id is one central never issues).  Such an id is never asked of
#: central (review rv_face2 B).
LOCAL_AVATAR_ID_BASE = 1_000_000_000
#: A default-path lookup is remembered this long (a found avatar) and this long
#: (none found), so a spoken reply's voice lookup does not ask the store, and on
#: a desktop central too, on every reply (review rv_face2 B).  An avatar that
#: lands late (an async toonify on central) can stay unseen up to the miss time,
#: plus avatarforcing_tool's own 60 s portrait-miss memo: about 2 minutes.
LOOKUP_TTL_S = 600.0
LOOKUP_MISS_TTL_S = 60.0
LOOKUPS_KEPT = 512
#: (avatar id, this node's store, central's store) -> (answer, monotonic time):
#: keyed by the stores too, so a sign-in or an operator override is never
#: answered from the other store's memory.
_lookups = {}
_lookups_lock = threading.Lock()


def avatar_id_from(value) -> Optional[int]:
    """The avatar id a request carries, or None when it carries none.

    Clients send a number or a numeric string, and Android leaves the key out
    when it has none.  Zero or a negative number never names an avatar, and
    anything that is not a plain number (a bool, a name, an object) is not an
    avatar id.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    return number if 0 < number <= MAX_AVATAR_ID else None


def lookup_avatar(avatar_id, database_url: Optional[str] = None) -> dict:
    """An avatar's image and voice sample. Never raises.

    Returns ``{'image_url', 'voice_id', 'audio_sample_url', 'openvoice'}``:
      * the image lookup failing for any reason (unreachable, an error, or
        ``null`` for an unknown or inactive id): image_url None, openvoice
        True, and no voice lookup;
      * an avatar with no voice_id: audio_sample_url and voice_id None;
      * the voice lookup failing: audio_sample_url and voice_id None.
    ``voice_id`` is ``int(voice_id)`` when truthy, else None: exactly what
    Generate_video posted before this moved here.

    With no ``database_url``, this node's database is asked first
    (get_db_url).  On a desktop that is the local Nunba store, which holds
    only the avatars made on it; a phone's agent carries CENTRAL avatar ids
    (Android and the web create them there), so when this node finds no image
    the central store is asked too (get_central_db_url), and the avatar and
    its voice come from where it was found.  A caller that names its
    ``database_url`` (Generate_video) is answered from that one store, as
    before.  On central itself the two are the same store and it is asked once.
    """
    if database_url:
        return _lookup_avatar_at(avatar_id, database_url)
    now = time.monotonic()
    from core.config_cache import get_db_url
    try:
        from core.config_cache import get_central_db_url
        central = get_central_db_url()
    except Exception:
        central = ''
    number = avatar_id_from(avatar_id)
    key = (number if number is not None else str(avatar_id), get_db_url() or '', central or '')
    with _lookups_lock:
        hit = _lookups.get(key)
    if hit is not None:
        answer, at = hit
        ttl = LOOKUP_TTL_S if answer['image_url'] is not None else LOOKUP_MISS_TTL_S
        if now - at < ttl:
            return dict(answer)
    answer = _lookup_default(avatar_id)
    with _lookups_lock:
        _lookups[key] = (dict(answer), now)
        while len(_lookups) > LOOKUPS_KEPT:
            _lookups.pop(next(iter(_lookups)))
    return answer


def _lookup_default(avatar_id) -> dict:
    """This node's store, then central for a central id this node does not hold."""
    from core.config_cache import get_db_url
    local = get_db_url() or 'https://mailer.hertzai.com'
    avatar = _lookup_avatar_at(avatar_id, local)
    if avatar['image_url'] is not None:
        return avatar
    number = avatar_id_from(avatar_id)
    if number is not None and number >= LOCAL_AVATAR_ID_BASE:
        return avatar  # made on a Nunba node: central never issued it
    try:
        from core.config_cache import get_central_db_url
        central = get_central_db_url()
    except Exception:
        central = ''
    if central and central.rstrip('/') != local.rstrip('/'):
        found = _lookup_avatar_at(avatar_id, central)
        if found['image_url'] is not None:
            return found
    return avatar


def _lookup_avatar_at(avatar_id, database_url: str) -> dict:
    """lookup_avatar against one store (the rules above)."""
    avatar = {'image_url': None, 'voice_id': None,
              'audio_sample_url': None, 'openvoice': False}
    try:
        res = pooled_get(f"{database_url}/get_image_by_id/{avatar_id}").json()
        avatar['image_url'] = res["image_url"]
        voice_id = res.get('voice_id')
    except Exception:
        avatar['openvoice'] = True
        return avatar
    if voice_id is not None:
        try:
            sample = pooled_get(
                f"{database_url}/get_voice_sample_id/{voice_id}").json()
            avatar['audio_sample_url'] = sample.get("voice_sample_url")
            avatar['voice_id'] = int(voice_id) if voice_id else None
        except Exception:
            avatar['audio_sample_url'] = None
            avatar['voice_id'] = None
    return avatar


def image_reference(avatar_id) -> Optional[str]:
    """Local path of an avatar's portrait, for a talking-avatar model.

    The image half of voice_reference: the same lookup, and the same two
    resolvers for the URL (this node's ``/uploads/...``, or an ``http(s)``
    URL downloaded once and cached).  None when the avatar has no image or
    it cannot be had.
    """
    url = lookup_avatar(avatar_id).get('image_url')
    if not url or not isinstance(url, str):
        return None
    if url.startswith('/uploads/'):
        from integrations.learning.book_pipeline import resolve_upload_url
        path = resolve_upload_url(url)
        return str(path) if path else None
    if url.startswith(('http://', 'https://')):
        from integrations.agent_engine.video_orchestrator import download_asset
        return download_asset(url, 'images')
    logger.info(f"avatar {avatar_id}: image URL {url!r} is neither an "
                f"/uploads/ path nor http(s); no face for it")
    return None


def voice_reference(avatar_id) -> Optional[str]:
    """Local path of an avatar's recorded voice, for a cloning TTS engine.

    None when the avatar has no voice, or its recording cannot be had.  The
    URL becomes a file through the helpers that already do that:
      * this node's own ``/uploads/...`` URL: book_pipeline.resolve_upload_url,
        the one strict resolver (no traversal, no drive letters, no UNC);
      * an ``http(s)`` URL, as central's rows carry:
        video_orchestrator.download_asset, which downloads once and then
        serves its cache.
    """
    url = lookup_avatar(avatar_id).get('audio_sample_url')
    if not url or not isinstance(url, str):
        return None
    if url.startswith('/uploads/'):
        from integrations.learning.book_pipeline import resolve_upload_url
        path = resolve_upload_url(url)
        return str(path) if path else None
    if url.startswith(('http://', 'https://')):
        from integrations.agent_engine.video_orchestrator import download_asset
        return download_asset(url, 'audio')
    logger.info(f"avatar {avatar_id}: voice sample URL {url!r} is neither "
                f"an /uploads/ path nor http(s); speaking without it")
    return None
