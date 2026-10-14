"""integrations.reel_studio.tools -- her seven reel_* agent tools.

``register_reel_studio_tools(helper, assistant, user_id, prompt_id=,
request_id_getter=, with_schema=)`` builds seven closures with her user id,
her agent's prompt id and a getter for the current turn's request id bound
in, so none of them is a tool argument, and registers them the way the
other Tier-2 families are (helper: the LLM schema; assistant: execution).
With ``with_schema=False`` (an agent that is not her Reel Studio agent) only
execution is registered, so the tools are not in the model's schema but
core.agent_tool_menu.discover_and_attach finds them by the first line of
their docstrings when the agent asks request_tools for them.

The seven (REEL_TOOL_NAMES):

* ``reel_make``: a reel from the clips she attached, delivered to her phone
  (pipeline.make_reel).  It reads this turn's clips (else her newest unused
  batch, or the clip ids named), her style (the arguments, over her agent
  record's words through style.parse_style_hints, over her voice profile),
  her newest logo (kept in her profile as logo_xid), her song (a key from
  reel_trending_songs, her attached song audio, else the top trending song
  in her language), a caption (else one from her profile) and at most five
  hashtags (else from her hashtag bank).
* ``reel_trending_songs``: trending songs in her language
  (integrations.trends.trending_songs_client).
* ``reel_check_caption``: igkit's caption lint, how machine-written it
  reads, and a cleaned-up version, with her own emoji and words kept.
* ``reel_score_hooks``: weak-hook flags (verdict and weakest check only; no
  score: it does not predict performance).
* ``reel_beat_sheet``: a voice-over script timed at her speaking rate.
* ``reel_skill``: an adapted skill text with her voice brief in front.
* ``reel_voice``: read or change her voice profile.

Replies are JSON, except reel_skill's text.  A call that did not do its work
answers in the executor's failure form, starting "Error:"
(core.constants.TOOL_ERROR_REPLY_PREFIXES): for reel_make that is every
status but 'ok' -- needs_clips, busy, no_space, quota, not_enough_footage,
clip_unsupported, render_failed, qa_failed -- with the result's JSON on the
next line.  core.constants.tool_reply_failed is the one rule the CREATE
recipe banker, the REUSE fabrication gate and the completion gate read, so
a reel that was not made is never banked or verified as made.

Importing this module is cheap: no cv2, numpy, ffmpeg or autogen.  The first
registration in a process sweeps jobs a restart interrupted
(jobs.sweep_once).
"""
import json
import logging
import os
import re
from typing import Annotated, Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.constants import TOOL_ERROR_REPLY_PREFIXES

logger = logging.getLogger(__name__)

REEL_TOOL_NAMES = ('reel_make', 'reel_trending_songs', 'reel_check_caption', 'reel_score_hooks',
                   'reel_beat_sheet', 'reel_skill', 'reel_voice')

#: The executor's failure form (core.constants.tool_reply_failed reads it).
_FAILED = TOOL_ERROR_REPLY_PREFIXES[0]
#: Longest on-screen hook text taken from the agent (the renderer wraps it).
HOOK_TEXT_MAX_CHARS = 120
#: Trending songs looked through for a song key the agent names.
SONG_LOOKUP_LIMIT = 100
DEFAULT_SONG_LIMIT = 5

#: open_with values that ask for the final-look teaser (A1), and those that
#: ask for the plain story; anything else is read as her words.
_OPEN_TEASER = frozenset({'final_look', 'final', 'finished_look', 'result', 'end_result',
                          'reveal', 'teaser', 'after', 'hook'})
_OPEN_STORY = frozenset({'story', 'bare_face', 'before', 'start', 'beginning', 'process',
                         'chronological', 'no', 'none', 'off'})

# ── replies ───────────────────────────────────────────────────────────────

def _ok_reply(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _error(tool: str, message: str) -> str:
    return '%s %s: %s' % (_FAILED, tool, ' '.join(str(message).split()))


def _failure_reply(result: Mapping[str, Any]) -> str:
    """A reel_make result that made no reel, in the executor's failure form:
    one line naming the status and what to do, then the result's JSON."""
    head = '%s %s: %s' % (_FAILED, result.get('status'),
                          ' '.join(str(result.get('message') or '').split()))
    return head + '\n' + _ok_reply(result)


def _describe(func: Callable) -> str:
    """The first line of the docstring: the one description of a tool, read
    by the LLM schema and by discover_and_attach alike."""
    return (func.__doc__ or '').strip().split('\n')[0].strip() or func.__name__


# ── reading values the model sends ────────────────────────────────────────

def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ('' if value is None else str(value).strip())


def _int(value) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    match = re.search(r'\d+', str(value))
    return int(match.group()) if match else None


def _flag(value) -> Optional[bool]:
    """'' or None: not given; else core.config_cache.flag_value."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    from core.config_cache import flag_value
    return flag_value(value, True)


def _opening(value) -> Optional[bool]:
    """open_with -> hook_first: True (teaser), False (the story), None (her words)."""
    text = _text(value)
    if not text or text.lower() == 'auto':
        return None
    key = re.sub(r'[\s-]+', '_', text.lower())
    if key in _OPEN_TEASER:
        return True
    if key in _OPEN_STORY:
        return False
    from integrations.reel_studio.style import parse_style_hints
    return parse_style_hints(text).get('hook_first')


# ── her things ────────────────────────────────────────────────────────────

class _Turn:
    """What every closure of one agent knows: whose, which agent, which turn."""

    def __init__(self, user_id, prompt_id, request_id_getter):
        self.user_id = str(user_id).strip()
        self.prompt_id = None if prompt_id is None else (str(prompt_id).strip() or None)
        self._getter = request_id_getter

    def request_id(self) -> Optional[str]:
        if self._getter is None:
            return None
        try:
            rid = self._getter()
        except Exception as e:
            logger.warning('reel tools: request id getter failed: %s', e)
            return None
        return None if rid is None else (str(rid).strip() or None)


def _profile(user_id) -> Tuple[Dict[str, Any], Optional[str]]:
    """(her voice profile, a note when it could not be read)."""
    from integrations.reel_studio import voice_profile
    try:
        return voice_profile.load_voice(user_id), None
    except voice_profile.VoiceProfileUnreadable:
        return {}, 'voice_profile_unreadable'
    except (ValueError, PermissionError) as e:
        logger.warning('reel tools: voice profile of %s not read: %s', user_id, e)
        return {}, 'voice_profile_unreadable'


def _style(turn: _Turn, profile: Mapping, args: Optional[Mapping] = None) -> Dict[str, Any]:
    from integrations.reel_studio.style import agent_record_text, build_style, parse_style_hints
    hints = parse_style_hints(agent_record_text(turn.prompt_id))
    return build_style(args=args, hints=hints, profile=profile)


def _store():
    from core.user_file_store import get_user_file_store
    return get_user_file_store()


def _clips(turn: _Turn, rid: Optional[str], named: str) -> List[Dict[str, Any]]:
    """The clips to use: the ids named (hers, purpose reel_source), else this
    turn's, else her newest unused batch for this agent."""
    from core import chat_attachments
    from core.user_file_store import is_xid
    from integrations.reel_studio.pipeline import MAX_CLIPS
    if named:
        store, out, seen = _store(), [], set()
        for ref in re.split(r'[\s,;]+', named.lower()):
            if not is_xid(ref) or ref in seen:
                continue
            seen.add(ref)
            try:
                held = store.resolve(turn.user_id, ref, box='in')
            except Exception as e:
                logger.warning('reel tools: clip %s not resolved: %s', ref[:12], e)
                held = None
            if held is not None and held.purpose == 'reel_source':
                out.append(held.to_dict())
        return out[:MAX_CLIPS]
    files = chat_attachments.latest_inbound(turn.user_id, turn.prompt_id, request_id=rid) \
        if rid else []
    if not files:
        files = chat_attachments.latest_inbound(turn.user_id, turn.prompt_id)
    return files[:MAX_CLIPS]


def _song_audio(turn: _Turn, rid: Optional[str]) -> Optional[Dict[str, Any]]:
    from core import chat_attachments
    files = chat_attachments.latest_inbound(turn.user_id, turn.prompt_id, request_id=rid,
                                            purpose='song_audio') if rid else []
    if not files:
        files = chat_attachments.latest_inbound(turn.user_id, turn.prompt_id,
                                                purpose='song_audio')
    return files[-1] if files else None


def _logo_path(turn: _Turn, profile: Mapping) -> Optional[str]:
    """Her newest uploaded logo (saved to her profile as logo_xid when it is
    new), else the one her profile names, else None."""
    from integrations.reel_studio import voice_profile
    store = _store()
    try:
        logos = store.query_inbox(turn.user_id, purpose='logo', unconsumed_only=False)
    except Exception as e:
        logger.warning('reel tools: logos of %s not read: %s', turn.user_id, e)
        logos = []
    newest = logos[-1] if logos else None
    saved = profile.get('logo_xid')
    if newest is not None:
        if newest.xid != saved:
            try:
                voice_profile.update_field(turn.user_id, 'logo_xid', newest.xid)
            except Exception as e:      # the reel still gets the logo
                logger.warning('reel tools: logo not saved to the profile of %s: %s',
                               turn.user_id, e)
        return newest.path
    if saved:
        try:
            held = store.resolve(turn.user_id, saved, box='in')
        except Exception:
            held = None
        return held.path if held is not None else None
    return None


def _trending(langs=None, limit: int = DEFAULT_SONG_LIMIT) -> Dict[str, Any]:
    from integrations.trends import trending_songs_client
    try:
        doc = trending_songs_client.get_trending_songs(langs=langs, limit=limit)
    except Exception as e:      # documented never to raise; held to it here too
        logger.warning('reel tools: trending songs unavailable: %s', e)
        doc = None
    return doc if isinstance(doc, dict) else {'songs': [], 'stale': True, 'reason': 'error'}


def _song_from(entry: Mapping) -> Dict[str, Any]:
    start = entry.get('suggested_start_ms')
    return {'song_key': entry.get('song_key'), 'title': entry.get('title') or '',
            'artist': entry.get('artist') or '', 'lang': entry.get('lang'),
            'start_ms': start if isinstance(start, int) and not isinstance(start, bool) else 0}


def _song(song_key: str, language: Optional[str],
          audio: Optional[Mapping]) -> Optional[Dict[str, Any]]:
    """The song the reel suggests: the key named (from the trending list),
    her attached audio, else the top trending song in her language."""
    song = None
    if song_key:
        hit = next((s for s in _trending(None, SONG_LOOKUP_LIMIT).get('songs') or []
                    if isinstance(s, Mapping) and s.get('song_key') == song_key), None)
        song = _song_from(hit) if hit else {'song_key': song_key, 'title': '', 'artist': '',
                                            'lang': None, 'start_ms': 0}
    if audio is not None:
        song = song or {'song_key': None, 'artist': '', 'lang': language, 'start_ms': 0,
                        'title': os.path.splitext(str(audio.get('name') or ''))[0]}
        song.update(audio_path=audio.get('path'), audio_xid=audio.get('xid'))
        return song
    if song is None:
        top = [s for s in _trending(language, 1).get('songs') or [] if isinstance(s, Mapping)]
        song = _song_from(top[0]) if top else None
    return song


def _caption_from_profile(profile: Mapping) -> str:
    """A plain caption from her own profile, never invented: what she does,
    where, and her ask.  '' when her profile has none of them."""
    services = _text(profile.get('services')).rstrip('.')
    city, cta = _text(profile.get('city')), _text(profile.get('cta'))
    first = services or ('Bridal makeup' if city else '')
    line = (first + (' in %s' % city if city else '')).strip()
    parts = [line + '.' if line else '', cta]
    return ' '.join(p for p in parts if p)


def _hashtags(text: str, profile: Mapping) -> List[str]:
    """At most five '#tags' from what the agent wrote (commas, spaces, with
    or without '#'), else from her hashtag bank; repeats dropped."""
    from integrations.reel_studio.contracts import HASHTAG_LIMIT
    from integrations.reel_studio.igkit import hashtags as find_tags, is_tag_char
    tags: List[str] = []
    seen = set()
    for piece in re.split(r'[\s,;]+', text or ''):
        found = [t for t, _, _ in find_tags(piece)]
        if not found and piece and all(is_tag_char(c) for c in piece):
            found = ['#' + piece]
        for tag in found:
            if tag.lower() not in seen:
                seen.add(tag.lower())
                tags.append(tag)
    if not tags:
        tags = [t for t in profile.get('hashtag_bank') or () if isinstance(t, str)]
    return tags[:HASHTAG_LIMIT]


# ── the tools ─────────────────────────────────────────────────────────────

def _reel_make(turn: _Turn, target_seconds, open_with, hook_text, caption, hashtags, song_key,
               use_logo, clips) -> str:
    from integrations.reel_studio import pipeline
    rid = turn.request_id()
    files = _clips(turn, rid, _text(clips))
    if not files:
        return _failure_reply({'status': 'needs_clips', 'message': pipeline.NEEDS_CLIPS_MESSAGE})
    profile, note = _profile(turn.user_id)
    target = _int(target_seconds)
    try:
        style = _style(turn, profile, {
            'target_s': target if target and target > 0 else None,
            'hook_first': _opening(open_with),
            'use_logo': _flag(use_logo),
            'hook_text': _text(hook_text)[:HOOK_TEXT_MAX_CHARS] or None,
            'song_key': _text(song_key) or None,
        })
    except ValueError as e:
        return _error('reel_make', 'bad style: %s' % e)
    result = pipeline.make_reel(
        user_id=turn.user_id, prompt_id=turn.prompt_id, request_id=rid, clips=files,
        style=style, song=_song(_text(song_key), style['language'], _song_audio(turn, rid)),
        logo_path=_logo_path(turn, profile) if style['use_logo'] else None,
        caption=_text(caption) or _caption_from_profile(profile),
        hashtags=_hashtags(_text(hashtags), profile))
    if result.get('status') != 'ok':
        return _failure_reply(result)
    if note:
        result['notes'] = list(result.get('notes') or []) + [note]
    return _ok_reply(result)


def _reel_voice(turn: _Turn, action, field, value) -> str:
    from integrations.reel_studio import voice_profile
    act = _text(action).lower() or 'get'
    name = _text(field)
    try:
        if act == 'get':
            return _ok_reply({'profile': voice_profile.load_voice(turn.user_id)})
        if act == 'fields':
            return _ok_reply({'fields': {
                f: {k: (list(v) if isinstance(v, tuple) else v) for k, v in spec.items()}
                for f, spec in voice_profile.FIELD_SPECS.items()}})
        if act == 'set':
            return _ok_reply({'profile': voice_profile.update_field(turn.user_id, name, value)})
        if act == 'clear':
            return _ok_reply({'profile': voice_profile.update_field(turn.user_id, name, None)})
        if act == 'reset':
            return _ok_reply({'reset': voice_profile.reset_voice(turn.user_id) is not None})
    except voice_profile.VoiceProfileUnreadable as e:
        return _error('reel_voice', str(e))
    except (ValueError, PermissionError) as e:
        return _error('reel_voice', '%s: %s' % (name or act, e))
    return _error('reel_voice', 'action must be get, set, clear, fields or reset, not %r' % act)


def build_reel_tools(user_id, *, prompt_id,
                     request_id_getter: Optional[Callable[[], Any]] = None
                     ) -> List[Tuple[str, Callable[..., str]]]:
    """The seven (name, closure) pairs for one user's agent, in REEL_TOOL_NAMES order."""
    turn = _Turn(user_id, prompt_id, request_id_getter)

    def reel_make(
        target_seconds: Annotated[int, 'Seconds, 5-30; 0 = her instructions'] = 0,
        open_with: Annotated[str, "final_look (1 s teaser first), story, or '' = her instructions"] = '',
        hook_text: Annotated[str, 'On-screen text for 2 s, max 6 words'] = '',
        caption: Annotated[str, "Caption; '' = from her profile"] = '',
        hashtags: Annotated[str, "Up to 5 #tags; '' = her bank"] = '',
        song_key: Annotated[str, "From reel_trending_songs; '' = auto"] = '',
        use_logo: Annotated[str, "yes, no, or '' = her instructions"] = '',
        clips: Annotated[str, "Clip ids; '' = attached clips"] = '',
    ) -> str:
        """Make an Instagram reel video from her attached makeup clips and send it to her phone.

        Plays her clips in the order she filmed them and ends on the
        finished look.  Takes minutes; the reel also arrives on her phone
        by itself.
        """
        return _reel_make(turn, target_seconds, open_with, hook_text, caption, hashtags,
                          song_key, use_logo, clips)

    def reel_trending_songs(
        language: Annotated[str, "ta, hi, pa, en; '' = hers"] = '',
        limit: Annotated[int, 'Max songs'] = DEFAULT_SONG_LIMIT,
    ) -> str:
        """Trending Instagram reel songs in her language, with start times."""
        profile, _note = _profile(turn.user_id)
        language = _text(language).lower() or _style(turn, profile)['language']
        return _ok_reply(_trending(language or None, _int(limit) or DEFAULT_SONG_LIMIT))

    def reel_check_caption(
        caption: Annotated[str, 'Caption'],
        keywords: Annotated[str, "Search terms, comma-separated; '' = hers"] = '',
    ) -> str:
        """Check an Instagram caption: lint, machine-text score and a cleaned-up version.

        Her own emoji and words (voice profile) are never flagged or replaced.
        """
        from integrations.reel_studio.igkit.caption import lint_caption
        from integrations.reel_studio.igkit.humanize import detect_machine_text, humanize_text
        profile, _note = _profile(turn.user_id)
        text = caption if isinstance(caption, str) else _text(caption)
        terms = [k.strip() for k in re.split(r'[\n,]', _text(keywords)) if k.strip()] \
            or list(profile.get('search_terms') or ()) or None
        keep = {'keep_emoji': tuple(profile.get('emoji') or ()),
                'keep_words': tuple(profile.get('words_used') or ())}
        return _ok_reply({'lint': lint_caption(text, keywords=terms),
                          'detection': detect_machine_text(text, **keep),
                          'humanized': humanize_text(text, **keep)})

    def reel_score_hooks(
        hooks: Annotated[str, 'One hook per line'],
    ) -> str:
        """Flag weak reel hooks and each one's weakest check; it does not predict performance."""
        from integrations.reel_studio.igkit.hooks import score_hooks
        return _ok_reply(score_hooks(hooks if isinstance(hooks, str) else _text(hooks)))

    def reel_beat_sheet(
        script: Annotated[str, 'One beat per line'],
        target_seconds: Annotated[int, 'Reel seconds; 0 = none'] = 0,
    ) -> str:
        """Time a reel voice-over script into beats at her speaking rate."""
        from integrations.reel_studio.igkit.hooks import DEFAULT_WPM, beat_sheet
        profile, _note = _profile(turn.user_id)
        target = _int(target_seconds)
        try:
            return _ok_reply(beat_sheet(script if isinstance(script, str) else _text(script),
                                        wpm=profile.get('wpm') or DEFAULT_WPM,
                                        target=target if target and target > 0 else None))
        except ValueError as e:
            return _error('reel_beat_sheet', e)

    def reel_skill(
        name: Annotated[str, 'ig_reel, ig_caption or ig_human'],
    ) -> str:
        """Read a Reel Studio writing skill with her voice profile in front."""
        from integrations.reel_studio import voice_profile
        from integrations.reel_studio.igkit.skills import SKILL_NAMES, get_skill_text
        try:
            text = get_skill_text(_text(name))
        except KeyError:
            return _error('reel_skill', 'no skill %r; the skills are %s'
                          % (_text(name), ', '.join(SKILL_NAMES)))
        try:
            brief = voice_profile.voice_brief(turn.user_id)
        except (ValueError, PermissionError) as e:
            logger.warning('reel tools: voice brief of %s not read: %s', turn.user_id, e)
            brief = ''
        return (brief + '\n\n' + text) if brief else text

    def reel_voice(
        action: Annotated[str, 'get|set|clear|fields|reset'] = 'get',
        field: Annotated[str, 'Field to set or clear'] = '',
        value: Annotated[str, 'New value'] = '',
    ) -> str:
        """Read or change her Instagram voice profile: language, tone, emoji, hashtags, logo corner.

        reset only after she confirms she wants to start her profile over.
        """
        return _reel_voice(turn, action, field, value)

    return [('reel_make', reel_make), ('reel_trending_songs', reel_trending_songs),
            ('reel_check_caption', reel_check_caption), ('reel_score_hooks', reel_score_hooks),
            ('reel_beat_sheet', reel_beat_sheet), ('reel_skill', reel_skill),
            ('reel_voice', reel_voice)]


def _first_registration() -> None:
    """Once per process: sweep jobs a restart interrupted (jobs.sweep_once).
    The store's own once-per-process retention pass is the device door's
    (core.peer_link.file_channel.install); make_reel runs one after each reel."""
    from integrations.reel_studio import jobs
    jobs.sweep_once()


def register_reel_studio_tools(helper, assistant, user_id: str, *, prompt_id,
                               request_id_getter: Optional[Callable[[], Any]] = None,
                               with_schema: bool = True) -> List[str]:
    """Register the seven reel tools for ``user_id``'s agent ``prompt_id``.

    ``request_id_getter()`` returns the current turn's request id, read each
    time reel_make runs.  ``with_schema``: True for her Reel Studio agent
    (``helper.register_for_llm`` plus ``assistant.register_for_execution``);
    False registers execution on ``assistant`` only.  Returns the names
    registered, in REEL_TOOL_NAMES order.
    """
    _first_registration()
    tools = build_reel_tools(user_id, prompt_id=prompt_id, request_id_getter=request_id_getter)
    for name, func in tools:
        if with_schema:
            helper.register_for_llm(name=name, description=_describe(func))(func)
        assistant.register_for_execution(name=name)(func)
    names = [name for name, _func in tools]
    logger.info('Registered %d reel tools for user %s (%s)', len(names), user_id,
                'schema + execution' if with_schema else 'execution only')
    return names


__all__ = ['REEL_TOOL_NAMES', 'HOOK_TEXT_MAX_CHARS', 'build_reel_tools',
           'register_reel_studio_tools']
