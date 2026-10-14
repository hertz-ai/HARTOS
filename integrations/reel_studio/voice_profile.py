"""integrations.reel_studio.voice_profile -- her voice profile, the one writer.

What her Reel Studio agent knows about how she writes and works: city,
services, caption language and code-mix, tone, the words and emoji she uses,
her hashtag bank, booking ask, brands, proof she is happy to post, logo and
logo corner, and the animated overlays her reels get (motion_overlays, a
list whose items must be contracts.MOTION_TEMPLATES; the reel_motion tool
sets it).  The fields, their types and limits come from
igkit/resources/voice_template.json (derived from instagram-agent-skill's
voice.md, MIT); anything else is refused.

Storage: ``user_dir(user_id)/voice.json``, written only by this module.
``user_dir`` is ``get_agent_data_dir()/reel_studio/<user_dir_name(user_id)>``,
the one place Reel Studio keeps a user's own files.  The user-id rule
(user_key: any non-empty text or int; 10077 and '10077' are one user) and the
folder rule (user_dir_name) are core.user_file_store's, imported, not copied
(amendment A9 d): every user the store files uploads under has a profile,
in a folder of the same name, and no id can steer the path.  The file
records its user id; a file holding another id (a hash collision) raises
PermissionError and is never shown or written over.

The file goes through security.crypto.encrypt_json_file: Fernet-encrypted
with HEVOLVE_DATA_KEY when that key is set, plaintext JSON when it is not (a
stock desktop has no key).  A plaintext profile is read, and encrypted by the
next write, once a key is set.  A write goes to a temp file that is flushed
to disk and then replaces the profile, so a crash never leaves half a
profile.  Updates for one user are serialised by a per-user lock.

A profile that exists but cannot be read (HEVOLVE_DATA_KEY missing for a
launch or rotated, or the file damaged) is never written over: load_voice,
save_voice and update_field raise VoiceProfileUnreadable and leave the file
as it is, so her whole profile is back when the key is
(security/node_integrity.py treats its key file the same way).  voice_brief
says the profile exists and could not be read.  reset_voice is the
deliberate way out once she confirms she wants to start over: it moves the
file aside, never deletes it.

Values are taken as an agent or a person writes them.  Emoji typed side by
side, or with spaces or commas between them, are stored one emoji per item
(igkit's one emoji definition, EMOJI_RE, which igkit.caption uses too), so
the humanizer allows each of them; a list item must already be one emoji.
Hashtags in text may be separated by commas, spaces or nothing ("#a#b"), by
igkit's one hashtag rule.  A yes/no is read by core.config_cache.flag_value,
the one reading in HARTOS; a store id by core.user_file_store.is_xid.

voice_brief() is what reel_skill puts in front of a skill text, so a local
model reads her profile before the instructions; it is bounded at
MAX_BRIEF_CHARS.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional

from core.config_cache import flag_value
from core.user_file_store import is_xid, user_dir_name, user_key
from integrations.agent_engine.human_voice import with_reel_hashtag_limit
from integrations.reel_studio import contracts
from integrations.reel_studio.igkit import EMOJI_RE, hashtags
from integrations.reel_studio.igkit.resources import load_json

logger = logging.getLogger(__name__)

VOICE_SCHEMA = 'hart.reel.voice.v1'
MAX_BRIEF_CHARS = 1200
#: Longest single line of the brief, so one long field cannot crowd out the rest.
_BRIEF_LINE_CHARS = 240
_FILE_NAME = 'voice.json'
#: What reset_voice renames her profile to: voice.aside.<UTC time>.<6 hex>.json.
_ASIDE_NAME = 'voice.aside.%s.%s.json'
#: Where text for a list splits: commas or new lines, but not "15,000".
_SEPARATORS = re.compile(r'\n|,(?!\d)')
_EMPTY_BRIEF = ('No voice profile saved yet. Before writing for her, ask for three of her own '
                'past captions, learn her language, tone and hashtags from them, and save each '
                'field with reel_voice after she confirms it.')
_UNREADABLE_BRIEF = ('Her voice profile is saved on this desktop but could not be read '
                     '(HEVOLVE_DATA_KEY is not set or has changed, or the file is damaged). Do '
                     'not rebuild it and do not save fields with reel_voice: saving is refused '
                     'until the key is restored, and then her whole profile is back. Write in a '
                     'plain, warm voice for now and tell her the desktop cannot read her saved '
                     'style. Only if she says that profile is gone for good and confirms she '
                     'wants to start over, reset it with reel_voice; the old file is kept aside, '
                     'not deleted.')
_NOT_EMOJI = ('%s: %r is not one emoji; give each emoji as its own item, or in text put '
              'spaces, commas or nothing between them')


class VoiceProfileUnreadable(RuntimeError):
    """Her saved profile exists but could not be read: HEVOLVE_DATA_KEY is
    missing or is not the key it was saved with, or the file is damaged.

    Nothing was written; ``path`` is the file, left as it was.  Not a
    ValueError: no value she gives can fix it.  The key has to come back, or
    she confirms she wants to start over and reset_voice moves the file aside.
    """

    def __init__(self, path: str):
        self.path = path
        super().__init__(
            'her voice profile is saved on this desktop but could not be read: '
            'HEVOLVE_DATA_KEY is not set or is not the key it was saved with, or the file '
            'is damaged. It was left untouched and nothing was saved. Restore that '
            'HEVOLVE_DATA_KEY and try again; or, only if she confirms she wants to start '
            'over, reset it (reset_voice), which moves the old file aside.')


def _field_specs() -> Mapping[str, Mapping[str, Any]]:
    """The template's fields, read-only: choices named by ``choices_from``
    come from contracts, and a label or help that states the reel hashtag
    cap gets the human-voice guide's number (its token in the template)."""
    specs = {}
    for name, raw in load_json('voice_template.json')['fields'].items():
        spec = dict(raw)
        source = spec.pop('choices_from', None)
        if source:
            spec['choices'] = tuple(getattr(contracts, source))
        elif 'choices' in spec:
            spec['choices'] = tuple(spec['choices'])
        for key in ('label', 'help'):
            if isinstance(spec.get(key), str):
                spec[key] = with_reel_hashtag_limit(spec[key])
        specs[name] = MappingProxyType(spec)
    return MappingProxyType(specs)


#: Every field her profile may hold, in brief order: name -> read-only spec
#: (``type`` text|list|hashtags|emoji|choice|bool|int|xid, its limits,
#: ``label`` and ``help``).
FIELD_SPECS = _field_specs()

_locks_guard = threading.Lock()
_user_locks: Dict[str, threading.Lock] = {}


# ── paths and locks ─────────────────────────────────────────────────────────

def _user_dir(key: str) -> str:
    from core.platform_paths import get_agent_data_dir
    return os.path.join(get_agent_data_dir(), 'reel_studio', user_dir_name(key))


def user_dir(user_id) -> str:
    """Her Reel Studio folder,
    ``get_agent_data_dir()/reel_studio/<user_dir_name(user id)>``.

    The one place Reel Studio keeps a user's own files: her voice profile is
    ``voice.json`` in it, and any other per-user Reel Studio file belongs
    here too rather than in a copy of the rule.  The folder has the name of
    her core.user_file_store folder (user_dir_name, imported), so an id never
    becomes a path.  Raises ValueError for a user id the store refuses
    (user_key).  Nothing is created.
    """
    return _user_dir(user_key(user_id))


def _profile_path(key: str) -> str:
    return os.path.join(_user_dir(key), _FILE_NAME)


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _user_locks.setdefault(key, threading.Lock())


# ── values ──────────────────────────────────────────────────────────────────

def _no_controls(text: str, field: str, multiline: bool) -> str:
    allowed = '\n' if multiline else ''
    if any(unicodedata.category(c) == 'Cc' and c not in allowed for c in text):
        raise ValueError('%s holds control characters' % field)
    return text


def _text(value, field: str, max_len: int, multiline: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        raise ValueError('%s must be text, got %r' % (field, type(value).__name__))
    if multiline:
        lines = [ln.rstrip() for ln in value.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
        text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    else:
        text = ' '.join(value.split())
    if not text:
        return None
    if len(text) > max_len:
        raise ValueError('%s is %d characters; at most %d' % (field, len(text), max_len))
    return _no_controls(text, field, multiline)


def _emoji_units(text: str, field: str) -> List[str]:
    """Each emoji of ``text``, where she put commas, spaces, new lines or
    nothing between them ("✨💄" is two).  Raises ValueError naming the first
    piece that is not all emoji."""
    units: List[str] = []
    for piece in re.split(r'[\s,]+', text):
        found = EMOJI_RE.findall(piece)
        if ''.join(found) != piece:
            raise ValueError(_NOT_EMOJI % (field, piece))
        units.extend(found)
    return units


def _tag_units(piece: str) -> List[str]:
    """Hashtags she wrote one after another in one piece of text ("#a #b",
    "#a#b") as one item each; anything else stays one item, for _hashtag to
    take or refuse."""
    tags = [tag for tag, _, _ in hashtags(piece)]
    if len(tags) > 1 and ''.join(tags) == ''.join(piece.split()):
        return tags
    return [piece]


def _items(value, field: str, spec: Mapping[str, Any]) -> List[str]:
    """A list value from a list (each item as given), or from text split
    per the spec.  Emoji and hashtags in text also split where she put
    spaces or nothing between them."""
    if isinstance(value, str):
        split = spec.get('split', 'commas')
        if spec['type'] == 'emoji':
            value = _emoji_units(value, field)
        elif spec['type'] == 'hashtags':
            value = [tag for piece in re.split(r'[\n,]', value) for tag in _tag_units(piece)]
        elif split == 'none':
            value = [value]
        elif split == 'lines':
            value = value.splitlines()
        else:
            value = _SEPARATORS.split(value)
    if not isinstance(value, (list, tuple)):
        raise ValueError('%s must be a list or text, got %r' % (field, type(value).__name__))
    return list(value)


def _hashtag(item: str, field: str, max_len: int) -> str:
    """One hashtag, as igkit.hashtags reads it, with its '#' added if
    missing."""
    tag = item if item.startswith('#') else '#' + item
    if len(tag) > max_len or [t for t, _, _ in hashtags(tag)] != [tag]:
        raise ValueError('%s: %r is not one hashtag (letters, digits and _ only, at most %d '
                         'characters); separate tags with commas or spaces'
                         % (field, item, max_len))
    return tag


def _emoji(item: str, field: str, max_len: int) -> str:
    """Exactly one emoji, as igkit.EMOJI_RE reads it (a keycap, a flag, or
    an emoji with its selector, skin tone and joined parts)."""
    if len(item) > max_len or not EMOJI_RE.fullmatch(item):
        raise ValueError(_NOT_EMOJI % (field, item))
    return item


def _list_choice(item: str, field: str, choices) -> str:
    """One item of a list whose items are ``choices`` (motion_overlays):
    case, spaces and hyphens do not matter ("End card" is end_card)."""
    key = re.sub(r'[\s-]+', '_', item.strip().lower())
    if key not in choices:
        raise ValueError('%s: %r is not one of %s' % (field, item, ', '.join(choices)))
    return key


def _list(value, field: str, spec: Mapping[str, Any]) -> Optional[List[str]]:
    kind, max_len = spec['type'], spec['item_max_len']
    out: List[str] = []
    seen = set()
    for raw in _items(value, field, spec):
        if not isinstance(raw, str):
            raise ValueError('%s items must be text, got %r' % (field, raw))
        if kind == 'hashtags':
            item = raw.strip()
            item = _hashtag(item, field, max_len) if item else None
        elif kind == 'emoji':
            item = raw.strip()
            item = _emoji(item, field, max_len) if item else None
        else:
            item = _text(raw, field, max_len, multiline=bool(spec.get('multiline')))
            if item is not None and spec.get('choices'):
                item = _list_choice(item, field, spec['choices'])
        if item is None or item.casefold() in seen:
            continue
        seen.add(item.casefold())
        out.append(item)
    if len(out) > spec['max_items']:
        raise ValueError('%s has %d items; at most %d' % (field, len(out), spec['max_items']))
    return out or None


def _choice(value, field: str, choices) -> Optional[str]:
    if not isinstance(value, str):
        raise ValueError('%s must be one of %s' % (field, ', '.join(choices)))
    key = value.strip().lower()
    if not key:
        return None
    if key not in choices:
        raise ValueError('%s must be one of %s, got %r' % (field, ', '.join(choices), value))
    return key


def _bool(value, field: str) -> Optional[bool]:
    """A yes/no as core.config_cache.flag_value reads one (a bool, a number's
    truth, or yes/no/true/false/on/off/1/0 text); blank text clears the
    field, and anything else is refused."""
    if isinstance(value, str) and not value.strip():
        return None
    flag = flag_value(value, None)
    if flag is None:
        raise ValueError('%s must be yes or no, got %r' % (field, value))
    return flag


def _int(value, field: str, lo: int, hi: int) -> Optional[int]:
    if isinstance(value, str):
        if not value.strip():
            return None
        if not value.strip().isdigit():
            raise ValueError('%s must be a whole number from %d to %d, got %r'
                             % (field, lo, hi, value))
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError('%s must be a whole number from %d to %d, got %r' % (field, lo, hi, value))
    return value


def _xid(value, field: str) -> Optional[str]:
    if isinstance(value, str) and not value.strip():
        return None
    if not (isinstance(value, str) and is_xid(value.strip())):
        raise ValueError('%s must be a store id (64 lowercase hex), got %r' % (field, value))
    return value.strip()


def _coerce(field: str, value) -> Any:
    """The stored form of ``value`` for ``field``; None clears the field.

    Text from the agent is accepted for every type: "170" for a number,
    "yes" (or 1) for a flag, "#a, #b" or "#a #b" for hashtags, "✨💄" or
    "✨ 💄" for emoji.  Raises ValueError for an unknown field or a value
    its spec refuses.
    """
    spec = FIELD_SPECS.get(field) if isinstance(field, str) else None
    if spec is None:
        raise ValueError('%r is not a voice profile field; the fields are: %s'
                         % (field, ', '.join(FIELD_SPECS)))
    if value is None:
        return None
    kind = spec['type']
    if kind == 'text':
        return _text(value, field, spec['max_len'], multiline=bool(spec.get('multiline')))
    if kind in ('list', 'hashtags', 'emoji'):
        return _list(value, field, spec)
    if kind == 'choice':
        return _choice(value, field, spec['choices'])
    if kind == 'bool':
        return _bool(value, field)
    if kind == 'int':
        return _int(value, field, spec['min'], spec['max'])
    if kind == 'xid':
        return _xid(value, field)
    raise ValueError('field %r has an unknown type %r in voice_template.json' % (field, kind))


def _clean(data: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError('a voice profile is a mapping of field -> value')
    unknown = [k for k in data if k not in FIELD_SPECS]
    if unknown:
        raise ValueError('not voice profile fields: %s; the fields are: %s'
                         % (', '.join(map(repr, unknown)), ', '.join(FIELD_SPECS)))
    out = {}
    for field in FIELD_SPECS:              # template order
        if field in data:
            value = _coerce(field, data[field])
            if value is not None:
                out[field] = value
    return out


# ── storage ─────────────────────────────────────────────────────────────────

def _read(key: str) -> Dict[str, Any]:
    """Her stored fields, {} when she has no profile file.

    Raises VoiceProfileUnreadable for a file that exists but does not
    decrypt or parse, and PermissionError for a file another user id wrote;
    callers hold the user's lock and write nothing after either.
    """
    path = _profile_path(key)
    if not os.path.exists(path):
        return {}
    from security.crypto import decrypt_json_file
    doc = decrypt_json_file(path)
    if not isinstance(doc, Mapping) or not isinstance(doc.get('fields'), Mapping):
        logger.error('voice profile %s exists but could not be read (HEVOLVE_DATA_KEY missing '
                     'or not the key it was saved with, or the file is damaged); left untouched '
                     'and nothing will be written over it', path)
        raise VoiceProfileUnreadable(path)
    owner = doc.get('user_id')
    if owner is not None and owner != key:
        # sha256(user id)[:16] collided: never show one user another's profile.
        raise PermissionError('voice profile %s belongs to another user' % path)
    out = {}
    for field, value in doc['fields'].items():
        if field not in FIELD_SPECS:
            logger.info('voice profile for %s: dropped field %r the template no longer has',
                        key, field)
            continue
        try:
            value = _coerce(field, value)
        except ValueError as exc:
            logger.warning('voice profile for %s: dropped invalid %s: %s', key, field, exc)
            continue
        if value is not None:
            out[field] = value
    return out


def _write(key: str, fields: Mapping[str, Any]) -> None:
    from security.crypto import encrypt_json_file
    path = _profile_path(key)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, '.%s.%s.tmp' % (_FILE_NAME, uuid.uuid4().hex))
    doc = {'schema': VOICE_SCHEMA, 'user_id': key, 'updated_at': time.time(),
           'fields': dict(fields)}
    try:
        encrypt_json_file(tmp, doc)
        # encrypt_json_file closes without an fsync: put the new profile on
        # disk before it replaces the old one, as core.file_cache's
        # atomic_json_write does for plain JSON.
        with open(tmp, 'rb+') as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as exc:
            logger.debug('voice profile: temp file %s not removed: %s', tmp, exc)
        raise


# ── API ─────────────────────────────────────────────────────────────────────

def load_voice(user_id) -> Dict[str, Any]:
    """Her saved profile as ``{field: value}`` ({} when she has none).

    Fields the template no longer has, and values it no longer accepts,
    are dropped with a log line.  Raises ValueError for a user id that is
    not non-empty text or an int, VoiceProfileUnreadable when her profile
    exists but cannot be read, and PermissionError for another user's file.
    """
    key = user_key(user_id)
    with _lock_for(key):
        return _read(key)


def save_voice(user_id, data: Mapping[str, Any]) -> None:
    """Replace her whole profile with ``data``.

    Every key must be a field of the template and every value valid for it
    (text forms are accepted, as in update_field); empty values are left
    out.  Nothing is written if anything is refused (ValueError), or if her
    saved profile exists but cannot be read (VoiceProfileUnreadable): a
    profile the key would bring back is never replaced.
    """
    key = user_key(user_id)
    fields = _clean(data)
    with _lock_for(key):
        _read(key)
        _write(key, fields)


def update_field(user_id, field: str, value: Any) -> Dict[str, Any]:
    """Set one field (None or an empty value clears it) and return her
    whole profile.  Raises ValueError for an unknown field or a value the
    field refuses, and VoiceProfileUnreadable when her saved profile exists
    but cannot be read; either way nothing changes."""
    key = user_key(user_id)
    clean = _coerce(field, value)
    with _lock_for(key):
        fields = _read(key)
        if clean is None:
            fields.pop(field, None)
        else:
            fields[field] = clean
        _write(key, {f: fields[f] for f in FIELD_SPECS if f in fields})
        return dict(fields)


def reset_voice(user_id) -> Optional[str]:
    """Start her profile over: move her saved profile aside and return the
    path it now has, or None when she has no profile.

    For a profile that cannot be read (VoiceProfileUnreadable) once she has
    confirmed she wants to start again; a readable profile is set aside the
    same way.  The file is renamed, never rewritten or deleted, to
    ``voice.aside.<UTC time>.<6 hex>.json`` in her folder, still encrypted
    with the key it was saved with, so it can be put back by hand if that
    key returns.  Saving works again afterwards.  Raises ValueError for a
    bad user id, and PermissionError, moving nothing, for a readable file
    another user id wrote.
    """
    key = user_key(user_id)
    with _lock_for(key):
        path = _profile_path(key)
        if not os.path.exists(path):
            return None
        try:
            _read(key)                     # another user's file raises PermissionError
        except VoiceProfileUnreadable as exc:
            logger.info('voice profile %s cannot be read; setting it aside', exc.path)
        aside = os.path.join(os.path.dirname(path), _ASIDE_NAME % (
            time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()), uuid.uuid4().hex[:6]))
        os.replace(path, aside)
        logger.warning('voice profile for %s set aside as %s; she starts over', key, aside)
        return aside


def _brief_value(value: Any, kind: str) -> str:
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, (list, tuple)):
        return (' ' if kind in ('hashtags', 'emoji') else '; ').join(value)
    return str(value)


def voice_brief(user_id) -> str:
    """Her profile as a short text for the top of a skill, at most
    MAX_BRIEF_CHARS.  Fields follow the template order; a field marked
    ``brief: false`` (the logo id, sample captions) is left out, a long line
    is shortened, and fields that no longer fit are dropped.  With no
    profile it says so and how to start one; with a profile that exists but
    cannot be read it says that, not to rebuild it, and to reset it only
    after she confirms.

    Raises, as load_voice does, ValueError for a user id that is not
    non-empty text or an int and PermissionError for another user's file.
    """
    try:
        fields = load_voice(user_id)
    except VoiceProfileUnreadable:
        return _UNREADABLE_BRIEF[:MAX_BRIEF_CHARS]
    if not fields:
        return _EMPTY_BRIEF[:MAX_BRIEF_CHARS]
    lines = ['Her voice profile (follow it; never contradict it):']
    used = len(lines[0])
    for field, spec in FIELD_SPECS.items():
        if field not in fields or spec.get('brief') is False:
            continue
        line = '- %s: %s' % (spec.get('label', field), _brief_value(fields[field], spec['type']))
        if len(line) > _BRIEF_LINE_CHARS:
            line = line[:_BRIEF_LINE_CHARS - 3] + '...'
        if used + 1 + len(line) > MAX_BRIEF_CHARS:
            break
        lines.append(line)
        used += 1 + len(line)
    return '\n'.join(lines)
