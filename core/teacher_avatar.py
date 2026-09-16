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
from typing import Optional

from core.http_pool import pooled_get

logger = logging.getLogger(__name__)

#: The largest avatar id a client can carry: Android types teacher_avatar_id
#: as a Java Integer (AbstractChatActivity.java, VirtualTeacherResponse.java).
MAX_AVATAR_ID = 2 ** 31 - 1


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
    """
    if not database_url:
        from core.config_cache import get_db_url
        database_url = get_db_url() or 'https://mailer.hertzai.com'
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
