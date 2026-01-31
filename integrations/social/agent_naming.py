"""
HevolveSocial - 3-Word Agent Naming System
Agent names are globally unique 3-word hyphenated phrases: adjective-color-noun
Example: swift-amber-falcon, calm-jade-oracle, bold-crimson-storm

Primary: LLM generates creative names using the user's configured API key.
Fallback: Small embedded word list for offline / no-LLM scenarios.
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
    'official', 'hevolve', 'hevolvebot', 'moltbot', 'nunba', 'api',
    'webhook', 'internal', 'deleted', 'banned', 'suspended',
])

# ─── Validation ───

_NAME_PATTERN = re.compile(r'^[a-z]{2,15}-[a-z]{2,15}-[a-z]{2,15}$')


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
            "Agent name must be exactly 3 lowercase words separated by hyphens "
            "(e.g. swift-amber-falcon). Each word: 2-15 letters, no numbers or special chars."
        )

    if len(name) > 47:
        return False, "Agent name too long (max 47 characters)"

    words = name.split('-')
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


def _generate_via_llm(count: int) -> List[str]:
    """Call LLM to generate creative 3-word hyphenated agent names."""
    api_key = _load_api_key()
    if not api_key:
        return []

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Generate {count} unique creative 3-word agent names. "
                    "Format: adjective-color-noun, all lowercase, hyphenated. "
                    "Examples: swift-amber-falcon, calm-jade-oracle, bold-crimson-storm. "
                    "Be creative and varied. Return ONLY the names, one per line, nothing else."
                ),
            }],
            temperature=1.0,
            max_tokens=count * 30,
        )
        text = response.choices[0].message.content.strip()
        names = [line.strip().lower() for line in text.split('\n') if line.strip()]
        return names
    except Exception as e:
        logger.debug(f"LLM name generation failed: {e}")
        return []


def _generate_random_fallback(db: Session, count: int) -> List[str]:
    """Fallback: generate names from embedded word lists when LLM unavailable."""
    results = []
    attempts = 0
    max_attempts = count * 20

    while len(results) < count and attempts < max_attempts:
        attempts += 1
        candidate = (f"{random.choice(_FALLBACK_ADJ)}-"
                     f"{random.choice(_FALLBACK_COLOR)}-"
                     f"{random.choice(_FALLBACK_NOUN)}")
        if candidate not in results and is_name_available(db, candidate):
            results.append(candidate)

    return results


def generate_agent_name(db: Session, count: int = 5) -> List[str]:
    """Generate unique available 3-word agent names. LLM-first, fallback to word lists."""
    # Try LLM first (ask for extras to handle collisions)
    llm_names = _generate_via_llm(count * 2)

    results = []
    for name in llm_names:
        name = name.strip().lower()
        valid, _ = validate_agent_name(name)
        if valid and is_name_available(db, name) and name not in results:
            results.append(name)
        if len(results) >= count:
            break

    # Fallback if LLM didn't produce enough
    if len(results) < count:
        results.extend(_generate_random_fallback(db, count - len(results)))

    return results
