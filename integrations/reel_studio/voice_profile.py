"""integrations.reel_studio.voice_profile -- her voice profile, the one writer.

What her Reel Studio agent knows about how she writes and works: city,
services, caption language and code-mix, tone, the words and emoji she uses,
her hashtag bank, booking ask, brands, proof she is happy to post, logo and
logo corner.  The fields, their types and limits come from
igkit/resources/voice_template.json (derived from instagram-agent-skill's
voice.md, MIT); anything else is refused.

Storage: ``get_agent_data_dir()/reel_studio/<user_id>/voice.json``, one file
per user, written only by this module.  The file is encrypted at rest
through security.crypto.encrypt_json_file (Fernet with HEVOLVE_DATA_KEY;
that helper writes plaintext JSON when no key is configured) and replaced
atomically, so a crash never leaves half a profile.  Updates for one user
are serialised by a per-user lock.

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

from integrations.reel_studio import contracts
from integrations.reel_studio.igkit import is_tag_char
from integrations.reel_studio.igkit.resources import load_json

logger = logging.getLogger(__name__)

VOICE_SCHEMA = 'hart.reel.voice.v1'
MAX_BRIEF_CHARS = 1200
#: Longest single line of the brief, so one long field cannot crowd out the rest.
_BRIEF_LINE_CHARS = 240
_FILE_NAME = 'voice.json'
_SAFE_USER_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_XID = re.compile(r'[0-9a-f]{64}')
_TRUE = frozenset({'true', 'yes', 'y', '1', 'on'})
_FALSE = frozenset({'false', 'no', 'n', '0', 'off'})
_EMPTY_BRIEF = ('No voice profile saved yet. Before writing for her, ask for three of her own '
                'past captions, learn her language, tone and hashtags from them, and save each '
                'field with reel_voice after she confirms it.')


def _field_specs() -> Mapping[str, Mapping[str, Any]]:
    specs = {}
    for name, raw in load_json('voice_template.json')['fields'].items():
        spec = dict(raw)
        source = spec.pop('choices_from', None)
        if source:
            spec['choices'] = tuple(getattr(contracts, source))
        elif 'choices' in spec:
            spec['choices'] = tuple(spec['choices'])
        specs[name] = MappingProxyType(spec)
    return MappingProxyType(specs)


#: Every field her profile may hold, in brief order: name -> read-only spec
#: (``type`` text|list|hashtags|emoji|choice|bool|int|xid, its limits,
#: ``label`` and ``help``).
FIELD_SPECS = _field_specs()

_locks_guard = threading.Lock()
_user_locks: Dict[str, threading.Lock] = {}


# ── paths and locks ─────────────────────────────────────────────────────────

def _user_key(user_id) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
        raise ValueError('unsafe user id %r' % (user_id,))
    key = str(user_id).strip()
    if not _SAFE_USER_ID.fullmatch(key):
        raise ValueError('unsafe user id %r: letters, digits, _ and - only' % (user_id,))
    return key


def _profile_path(key: str) -> str:
    from core.platform_paths import get_agent_data_dir
    return os.path.join(get_agent_data_dir(), 'reel_studio', key, _FILE_NAME)


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


def _items(value, field: str, spec: Mapping[str, Any]) -> List[str]:
    """A list value from a list, or from text split per the spec."""
    if isinstance(value, str):
        split = spec.get('split', 'commas')
        if split == 'none':
            value = [value]
        elif split == 'lines':
            value = value.splitlines()
        else:                              # commas or new lines; not "15,000"
            value = re.split(r'\n|,(?!\d)', value)
    if not isinstance(value, (list, tuple)):
        raise ValueError('%s must be a list or text, got %r' % (field, type(value).__name__))
    return list(value)


def _hashtag(item: str, field: str, max_len: int) -> str:
    tag = item if item.startswith('#') else '#' + item
    if len(tag) < 2 or len(tag) > max_len or not all(is_tag_char(c) for c in tag[1:]):
        raise ValueError('%s: %r is not one hashtag (letters, digits and _ only, at most %d '
                         'characters)' % (field, item, max_len))
    return tag


def _emoji(item: str, field: str, max_len: int) -> str:
    if len(item) > max_len or any(unicodedata.category(c)[0] in 'LN' for c in item):
        raise ValueError('%s: %r is not an emoji' % (field, item))
    return item


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
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        if not key:
            return None
        if key in _TRUE:
            return True
        if key in _FALSE:
            return False
    raise ValueError('%s must be yes or no, got %r' % (field, value))


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
    if not (isinstance(value, str) and _XID.fullmatch(value.strip())):
        raise ValueError('%s must be a store id (64 lowercase hex), got %r' % (field, value))
    return value.strip()


def _coerce(field: str, value) -> Any:
    """The stored form of ``value`` for ``field``; None clears the field.

    Text from the agent is accepted for every type: "170" for a number,
    "yes" for a flag, "#a, #b" for a list.  Raises ValueError for an unknown
    field or a value its spec refuses.
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
    path = _profile_path(key)
    if not os.path.exists(path):
        return {}
    from security.crypto import decrypt_json_file
    doc = decrypt_json_file(path)
    if not isinstance(doc, Mapping) or not isinstance(doc.get('fields'), Mapping):
        logger.warning('voice profile for %s could not be read (wrong data key or damaged '
                       'file); treating it as empty', key)
        return {}
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
    doc = {'schema': VOICE_SCHEMA, 'updated_at': time.time(), 'fields': dict(fields)}
    try:
        encrypt_json_file(tmp, doc)
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
    are dropped with a log line.  Raises ValueError for an unsafe user id.
    """
    key = _user_key(user_id)
    with _lock_for(key):
        return _read(key)


def save_voice(user_id, data: Mapping[str, Any]) -> None:
    """Replace her whole profile with ``data``.

    Every key must be a field of the template and every value valid for it
    (text forms are accepted, as in update_field); empty values are left
    out.  Nothing is written if anything is refused (ValueError).
    """
    key = _user_key(user_id)
    fields = _clean(data)
    with _lock_for(key):
        _write(key, fields)


def update_field(user_id, field: str, value: Any) -> Dict[str, Any]:
    """Set one field (None or an empty value clears it) and return her
    whole profile.  Raises ValueError for an unknown field or a value the
    field refuses, and then changes nothing."""
    key = _user_key(user_id)
    clean = _coerce(field, value)
    with _lock_for(key):
        fields = _read(key)
        if clean is None:
            fields.pop(field, None)
        else:
            fields[field] = clean
        _write(key, {f: fields[f] for f in FIELD_SPECS if f in fields})
        return dict(fields)


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
    profile it says so and how to start one."""
    fields = load_voice(user_id)
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
