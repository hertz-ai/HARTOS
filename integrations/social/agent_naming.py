"""
HevolveSocial - Agent Naming System (what3words-style, dot-separated)

Local names:  2-word (adjective.noun) — unique per user, work offline.
              Example: swift.falcon, calm.oracle, bold.storm

Global names: 3-word (local.name.handle) — globally unique.
              Example: swift.falcon.sathi, calm.oracle.john

Handle:       A unique creator tag each user picks once (like a gamertag).
              Reused as the suffix for all their agents' global names.

Legacy 3-word names (adjective.color.noun) remain supported for backward compat.
"""
import os
import re
import json
import random
import logging
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')

# ─── Fallback Word Lists (used when LLM is unavailable) ───

_FALLBACK_ADJ = [
    'swift', 'calm', 'bold', 'wise', 'keen', 'bright', 'fierce', 'gentle',
    'silent', 'mighty', 'clever', 'noble', 'wild', 'pure', 'brave', 'deep',
    'sharp', 'proud', 'vivid', 'agile',
]
_FALLBACK_COLOR = [
    'amber', 'azure', 'crimson', 'emerald', 'golden', 'indigo', 'jade',
    'onyx', 'pearl', 'ruby', 'silver', 'topaz', 'violet', 'coral', 'teal',
    'bronze', 'copper', 'ivory', 'cobalt', 'scarlet',
]
_FALLBACK_NOUN = [
    'falcon', 'sage', 'river', 'storm', 'phoenix', 'dragon', 'oracle',
    'guardian', 'sentinel', 'wolf', 'hawk', 'owl', 'fox', 'eagle', 'raven',
    'ember', 'thunder', 'beacon', 'nexus', 'prism',
]

RESERVED_WORDS = frozenset([
    'admin', 'root', 'system', 'bot', 'test', 'null', 'undefined',
    'anonymous', 'moderator', 'mod', 'staff', 'support', 'help',
    'official', 'hevolve', 'hevolvebot', 'santaclaw', 'nunba', 'api',
    'webhook', 'internal', 'deleted', 'banned', 'suspended',
])

# ─── Validation ───

_NAME_PATTERN = re.compile(r'^[a-z]{2,15}\.[a-z]{2,15}\.[a-z]{2,15}$')
_LOCAL_NAME_PATTERN = re.compile(r'^[a-z]{2,15}\.[a-z]{2,15}$')
_HANDLE_PATTERN = re.compile(r'^[a-z]{2,15}$')


def validate_agent_name(name: str) -> Tuple[bool, Optional[str]]:
    """
    Validate agent name format.
    Returns (True, None) if valid, (False, error_message) if invalid.
    """
    if not name:
        return False, "Agent name is required"

    name = name.strip().lower()

    if not _NAME_PATTERN.match(name):
        return False, (
            "Agent name must be exactly 3 lowercase words separated by dots "
            "(e.g. swift.amber.falcon). Each word: 2-15 letters, no numbers or special chars."
        )

    if len(name) > 47:
        return False, "Agent name too long (max 47 characters)"

    words = name.split('.')
    for word in words:
        if word in RESERVED_WORDS:
            return False, f"'{word}' is a reserved word and cannot be used in agent names"

    return True, None


def is_name_available(db: Session, name: str) -> bool:
    """Check if agent name is available globally."""
    from .models import User
    return db.query(User).filter(User.username == name).first() is None


def validate_and_check(db: Session, name: str) -> Tuple[bool, Optional[str]]:
    """Validate format and check availability in one call."""
    name = name.strip().lower()
    valid, error = validate_agent_name(name)
    if not valid:
        return False, error
    if not is_name_available(db, name):
        return False, f"Name '{name}' is already taken"
    return True, None


# ─── Handle Validation ───


def validate_handle(handle: str) -> Tuple[bool, Optional[str]]:
    """Validate a user handle (the creator tag appended to agent names)."""
    if not handle:
        return False, "Handle is required"
    handle = handle.strip().lower()
    if not _HANDLE_PATTERN.match(handle):
        return False, "Handle must be 2-15 lowercase letters, no numbers, spaces, or dots"
    if handle in RESERVED_WORDS:
        return False, f"'{handle}' is reserved and cannot be used as a handle"
    return True, None


def is_handle_available(db: Session, handle: str) -> bool:
    """Check if a handle is available globally."""
    from .models import User
    return db.query(User).filter(User.handle == handle).first() is None


# ─── Local (2-word) Name Validation ───


def validate_local_name(name: str) -> Tuple[bool, Optional[str]]:
    """Validate a 2-word local agent name (e.g. swift.falcon)."""
    if not name:
        return False, "Agent name is required"
    name = name.strip().lower()
    if not _LOCAL_NAME_PATTERN.match(name):
        return False, (
            "Agent name must be exactly 2 lowercase words separated by a dot "
            "(e.g. swift.falcon). Each word: 2-15 letters."
        )
    if len(name) > 31:
        return False, "Agent name too long (max 31 characters)"
    for word in name.split('.'):
        if word in RESERVED_WORDS:
            return False, f"'{word}' is a reserved word and cannot be used"
    return True, None


def compose_global_name(local_name: str, handle: str) -> str:
    """Compose a 3-word global name from a 2-word local name and user handle."""
    return f"{local_name.strip().lower()}.{handle.strip().lower()}"


def check_global_availability(
    db: Session, local_name: str, handle: str
) -> Tuple[bool, str, Optional[str]]:
    """
    Check if a local name + handle combination is available globally.
    Returns (available, global_name, error_or_None).
    """
    global_name = compose_global_name(local_name, handle)
    # Validate the composed 3-word name
    valid, error = validate_agent_name(global_name)
    if not valid:
        return False, global_name, error
    if not is_name_available(db, global_name):
        return False, global_name, f"'{global_name}' is already taken globally"
    return True, global_name, None


# ─── LLM-Powered Generation ───

def _load_api_key():
    """Load OpenAI API key from config.json (same source as langchain_gpt_api.py)."""
    config_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'config.json')
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        return config.get('OPENAI_API_KEY', '')
    except Exception:
        return os.environ.get('OPENAI_API_KEY', '')


def _generate_via_llm(count: int, mode: str = 'global') -> List[str]:
    """Call LLM to generate creative agent names (2-word local or 3-word global)."""
    api_key = _load_api_key()
    if not api_key:
        return []

    if mode == 'local':
        prompt = (
            f"Generate {count} unique creative 2-word agent names. "
            "Format: adjective.noun, all lowercase, separated by a dot (like what3words). "
            "Examples: swift.falcon, calm.oracle, bold.storm, fierce.phoenix, gentle.ember. "
            "Be creative and varied. Return ONLY the names, one per line, nothing else."
        )
    else:
        prompt = (
            f"Generate {count} unique creative 3-word agent names. "
            "Format: adjective.color.noun, all lowercase, separated by dots (like what3words). "
            "Examples: swift.amber.falcon, calm.jade.oracle, bold.crimson.storm. "
            "Be creative and varied. Return ONLY the names, one per line, nothing else."
        )

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=1.0,
            max_tokens=count * 30,
        )
        text = response.choices[0].message.content.strip()
        names = [line.strip().lower() for line in text.split('\n') if line.strip()]
        return names
    except Exception as e:
        logger.debug(f"LLM name generation failed: {e}")
        return []


def _generate_random_fallback(db: Session, count: int, mode: str = 'global',
                               handle: Optional[str] = None) -> List[str]:
    """Fallback: generate names from embedded word lists when LLM unavailable."""
    results = []
    attempts = 0
    max_attempts = count * 20

    while len(results) < count and attempts < max_attempts:
        attempts += 1
        if mode == 'local':
            candidate = f"{random.choice(_FALLBACK_ADJ)}.{random.choice(_FALLBACK_NOUN)}"
            # Check global availability if handle provided
            if handle:
                global_name = compose_global_name(candidate, handle)
                if candidate not in results and is_name_available(db, global_name):
                    results.append(candidate)
            else:
                if candidate not in results:
                    results.append(candidate)
        else:
            candidate = (f"{random.choice(_FALLBACK_ADJ)}."
                         f"{random.choice(_FALLBACK_COLOR)}."
                         f"{random.choice(_FALLBACK_NOUN)}")
            if candidate not in results and is_name_available(db, candidate):
                results.append(candidate)

    return results


def generate_agent_name(db: Session, count: int = 5, mode: str = 'global',
                         handle: Optional[str] = None) -> List[str]:
    """
    Generate unique available agent names.

    mode='local':  returns 2-word names (adjective-noun), pre-checked for global
                   availability when handle is provided.
    mode='global': returns 3-word names (adjective-color-noun), checked globally.
    """
    validator = validate_local_name if mode == 'local' else validate_agent_name
    llm_names = _generate_via_llm(count * 2, mode=mode)

    results = []
    for name in llm_names:
        name = name.strip().lower()
        valid, _ = validator(name)
        if not valid or name in results:
            continue
        # Check availability
        if mode == 'local' and handle:
            global_name = compose_global_name(name, handle)
            if not is_name_available(db, global_name):
                continue
        elif mode == 'global':
            if not is_name_available(db, name):
                continue
        results.append(name)
        if len(results) >= count:
            break

    # Fallback if LLM didn't produce enough
    if len(results) < count:
        results.extend(_generate_random_fallback(
            db, count - len(results), mode=mode, handle=handle))

    return results
