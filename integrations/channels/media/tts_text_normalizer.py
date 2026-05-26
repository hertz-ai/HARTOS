"""
TTS Text Normalizer — expand numbers, currency, units, URLs to spoken form.

Why this exists:
  Modern diffusion-token TTS (OmniVoice, F5, CosyVoice, Indic-Parler) cannot
  pronounce tokens like "Rs.200", "12.5%", "2:30 PM", "https://x.com",
  "Dr.", "kg". They either skip them or produce garbage.  Text MUST be
  normalized to its spoken form BEFORE hitting the synthesizer.

Single converging path:
  Called ONCE from tts_router.synthesize() right after language detection.
  Every TTS engine in the registry benefits — we do not duplicate this
  logic per-engine.

Two-stage strategy:
  1) Rule pass (fast, offline, deterministic) — num2words + regex for
     currency, percent, time, URLs, emails.  <1 ms for short utterances.
  2) LLM fallback (slow, online) — only if rule pass leaves residual
     unspeakable tokens.  Calls model_bus_service.infer() with the local
     0.8B model.  2 s timeout → falls back to rule output.

Cache:
  (sha256(text), lang) → normalized_text, persisted at
  ~/.hevolve/cache/tts_normalize/ so repeated phrases never re-run
  num2words or hit the LLM.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Configuration ─────────────────────────────────────────────────────

# num2words built-in language support (as of 0.5.x).  Missing Indic langs
# (hi, ta, bn, ur, pa, or, as, ml, mr — partially covered; sa never) will
# fall through to LLM normalization when num2words raises NotImplementedError.
NUM2WORDS_LANGS = frozenset({
    'ar', 'az', 'be', 'bg', 'ca', 'cs', 'cy', 'da', 'de', 'el', 'en',
    'eo', 'es', 'et', 'fa', 'fi', 'fr', 'he', 'hr', 'hu', 'hy', 'id',
    'is', 'it', 'ja', 'kn', 'ko', 'kz', 'lt', 'lv', 'nl', 'no', 'pl',
    'pt', 'ro', 'ru', 'sk', 'sl', 'sr', 'sv', 'te', 'th', 'tr', 'uk',
    'vi', 'zh',
    # Added in recent forks — check at call-time since versions vary
    'hi', 'ta', 'gu', 'mr',
})

# Currency symbol → language-keyed spoken form.  Symbols that make it past
# the regex rule pass get replaced with the target-language word; unknown
# languages fall back to English.
CURRENCY_WORDS: dict[str, dict[str, str]] = {
    '$':  {'en': 'dollars', 'hi': 'डॉलर', 'ta': 'டாலர்', 'default': 'dollars'},
    '₹':  {'en': 'rupees',  'hi': 'रुपये', 'ta': 'ரூபாய்', 'default': 'rupees'},
    'Rs': {'en': 'rupees',  'hi': 'रुपये', 'ta': 'ரூபாய்', 'default': 'rupees'},
    '€':  {'en': 'euros',   'hi': 'यूरो',  'ta': 'யூரோ',   'default': 'euros'},
    '£':  {'en': 'pounds',  'hi': 'पाउंड', 'ta': 'பவுண்ட்', 'default': 'pounds'},
    '¥':  {'en': 'yen',     'default': 'yen'},
    '₩':  {'en': 'won',     'default': 'won'},
    '₽':  {'en': 'rubles',  'default': 'rubles'},
}

# Percent and per-mille
PERCENT_WORDS: dict[str, str] = {
    'en': 'percent', 'hi': 'प्रतिशत', 'ta': 'சதவீதம்',
    'bn': 'শতাংশ', 'te': 'శాతం', 'ml': 'ശതമാനം', 'kn': 'ಶೇಕಡಾ',
    'mr': 'टक्के', 'gu': 'ટકા', 'pa': 'ਪ੍ਰਤੀਸ਼ਤ', 'ur': 'فیصد',
    'default': 'percent',
}

# "at" and "dot" words for email/URL spelling
AT_DOT_WORDS: dict[str, tuple[str, str]] = {
    'en': ('at', 'dot'),
    'hi': ('ऐट', 'डॉट'),
    'ta': ('அட்', 'டாட்'),
    'default': ('at', 'dot'),
}

LINK_WORDS: dict[str, str] = {
    'en': 'link', 'hi': 'लिंक', 'ta': 'இணைப்பு',
    'bn': 'লিঙ্ক', 'te': 'లింక్',
    'default': 'link',
}

# Residual-token detector: any Latin digit or uppercase-only 3+ char
# acronym after the rule pass triggers LLM fallback.  Indic scripts in
# this regex set are excluded — their own digits stay unchanged (TTS
# pronounces Devanagari / Tamil / Bengali digits natively).
_RESIDUAL_PATTERN = re.compile(
    r'[0-9]|(?:\b[A-Z]{3,}\b)'
)

_LLM_TIMEOUT_SEC = 2.0
_CACHE_TTL_DAYS = 30


# ─── Cache ─────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    base = Path(
        os.environ.get('HEVOLVE_CACHE_DIR')
        or (Path.home() / '.hevolve' / 'cache' / 'tts_normalize')
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cache_key(text: str, lang: str) -> str:
    h = hashlib.sha256(f'{lang}\x00{text}'.encode('utf-8')).hexdigest()
    return h[:32]


def _cache_get(text: str, lang: str) -> Optional[str]:
    try:
        p = _cache_dir() / f'{_cache_key(text, lang)}.json'
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > _CACHE_TTL_DAYS * 86400:
            p.unlink(missing_ok=True)
            return None
        return json.loads(p.read_text(encoding='utf-8')).get('normalized')
    except Exception:
        return None


def _cache_put(text: str, lang: str, normalized: str) -> None:
    try:
        p = _cache_dir() / f'{_cache_key(text, lang)}.json'
        tmp = p.with_suffix('.json.tmp')
        tmp.write_text(
            json.dumps({
                'lang': lang,
                'text': text[:200],  # truncated preview only; full text in hash
                'normalized': normalized,
                'ts': time.time(),
            }, ensure_ascii=False),
            encoding='utf-8',
        )
        tmp.replace(p)
    except Exception as e:
        logger.debug(f'tts_normalize cache write failed: {e}')


# ─── Rule pass ─────────────────────────────────────────────────────────

def _currency_word(symbol: str, lang: str) -> str:
    # Normalize "Rs." / "Rs " / "Rs" to the registry key "Rs"
    key = symbol.rstrip('. ').strip()
    entry = CURRENCY_WORDS.get(key) or CURRENCY_WORDS.get(symbol) or {}
    return entry.get(lang) or entry.get('default') or symbol


def _percent_word(lang: str) -> str:
    return PERCENT_WORDS.get(lang) or PERCENT_WORDS['default']


def _link_word(lang: str) -> str:
    return LINK_WORDS.get(lang) or LINK_WORDS['default']


def _at_dot(lang: str) -> tuple[str, str]:
    return AT_DOT_WORDS.get(lang) or AT_DOT_WORDS['default']


def _num_to_words(n: float, lang: str) -> Optional[str]:
    """Convert number to spoken words in target language via num2words.
    Returns None if unsupported.
    """
    if lang not in NUM2WORDS_LANGS:
        return None
    try:
        from num2words import num2words  # type: ignore
    except ImportError:
        return None
    try:
        # num2words doesn't always error on unsupported langs; try and catch
        return num2words(n, lang=lang)
    except (NotImplementedError, KeyError, ValueError):
        return None
    except Exception as e:
        logger.debug(f'num2words failed for {lang}: {e}')
        return None


def _expand_currency_number(match: re.Match, lang: str) -> str:
    """Replace <symbol><amount> with <amount-words> <currency-word>.
    E.g. Rs.200 → "two hundred rupees" (en) or "दो सौ रुपये" (hi).
    """
    symbol = match.group('sym')
    amount_str = match.group('amt').replace(',', '')
    try:
        amount = float(amount_str) if '.' in amount_str else int(amount_str)
    except ValueError:
        return match.group(0)
    words = _num_to_words(amount, lang)
    if words is None:
        words = _num_to_words(amount, 'en') or str(amount)
    return f'{words} {_currency_word(symbol, lang)}'


def _expand_standalone_number(match: re.Match, lang: str) -> str:
    """Replace bare numbers with words.  Falls through to original on failure."""
    raw = match.group(0).replace(',', '')
    try:
        n = float(raw) if '.' in raw else int(raw)
    except ValueError:
        return match.group(0)
    return _num_to_words(n, lang) or _num_to_words(n, 'en') or raw


def _expand_percent(match: re.Match, lang: str) -> str:
    """12.5% → 'twelve point five percent' (or target-lang equivalent)."""
    num_part = match.group('num').replace(',', '')
    try:
        n = float(num_part) if '.' in num_part else int(num_part)
    except ValueError:
        return match.group(0)
    words = _num_to_words(n, lang) or _num_to_words(n, 'en') or num_part
    return f'{words} {_percent_word(lang)}'


def _expand_url(match: re.Match, lang: str) -> str:
    return _link_word(lang)


def _expand_email(match: re.Match, lang: str) -> str:
    at, dot = _at_dot(lang)
    user = match.group('user')
    domain = match.group('domain')
    # Spell the domain dots too ("example.com" → "example dot com")
    domain_spoken = domain.replace('.', f' {dot} ')
    return f'{user} {at} {domain_spoken}'


def _expand_time(match: re.Match, lang: str) -> str:
    """2:30 PM → 'two thirty PM' (EN only — other langs fall through)."""
    if lang != 'en':
        return match.group(0)
    h = int(match.group('h'))
    m = int(match.group('m'))
    ampm = match.group('ampm') or ''
    h_words = _num_to_words(h, 'en') or str(h)
    if m == 0:
        return f'{h_words} o clock{" " + ampm if ampm else ""}'
    m_words = _num_to_words(m, 'en') or str(m)
    return f'{h_words} {m_words}{" " + ampm if ampm else ""}'


def rule_normalize(text: str, lang: str) -> str:
    """Apply all regex-based normalizations.  Deterministic, <1 ms."""
    out = text

    # Emails FIRST (before URL matcher eats the @-host)
    out = re.sub(
        r'(?P<user>[\w.+\-]+)@(?P<domain>[\w.\-]+\.[A-Za-z]{2,})',
        lambda m: _expand_email(m, lang),
        out,
    )

    # URLs
    out = re.sub(
        r'https?://\S+',
        lambda m: _expand_url(m, lang),
        out,
    )

    # Currency: $100 / ₹200 / Rs.200 / €50.25 — optional dot/comma in amount
    out = re.sub(
        r'(?P<sym>\$|₹|€|£|¥|₩|₽|Rs\.?)\s?(?P<amt>[\d,]+(?:\.\d+)?)',
        lambda m: _expand_currency_number(m, lang),
        out,
    )

    # Percent
    out = re.sub(
        r'(?P<num>[\d,]+(?:\.\d+)?)\s?%',
        lambda m: _expand_percent(m, lang),
        out,
    )

    # Time HH:MM with optional AM/PM
    out = re.sub(
        r'\b(?P<h>\d{1,2}):(?P<m>\d{2})\s?(?P<ampm>AM|PM|am|pm)?\b',
        lambda m: _expand_time(m, lang),
        out,
    )

    # Standalone numbers LAST — catches any residual bare digits.
    # Only target ASCII digits; Indic digits (০-৯ ०-९ etc.) stay intact.
    out = re.sub(
        r'\b\d[\d,]*(?:\.\d+)?\b',
        lambda m: _expand_standalone_number(m, lang),
        out,
    )

    return out


# ─── LLM fallback ──────────────────────────────────────────────────────

def _has_residual_tokens(text: str) -> bool:
    """True if rule-normalized text still contains digits or unspoken
    acronyms that TTS would stumble on.
    """
    return bool(_RESIDUAL_PATTERN.search(text))


def _get_model_bus():
    """Resolve the local ModelBus singleton.

    Tries legacy module-level accessors first (used by several other
    call sites), then falls back to constructing ModelBusService lazily.
    Returns None if the model_bus module is absent or construction fails
    — caller MUST handle None gracefully.
    """
    try:
        from integrations.agent_engine import model_bus_service as _mbs
    except ImportError:
        return None
    # Prefer whatever accessor the rest of the codebase agrees on
    for attr in ('get_model_bus', 'get_model_bus_service'):
        fn = getattr(_mbs, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception as e:
                logger.debug(f'{attr}() failed: {e}')
    # Fallback: construct directly — cheap if already a singleton internally
    try:
        return _mbs.ModelBusService()
    except Exception as e:
        logger.debug(f'ModelBusService() failed: {e}')
        return None


def _llm_normalize(text: str, lang: str) -> Optional[str]:
    """Ask the local 0.8B model to expand remaining unspeakable tokens.
    Returns None on timeout, error, or backend absence.
    """
    bus = _get_model_bus()
    if bus is None:
        return None

    prompt = (
        f'Normalize the following text for text-to-speech in {lang}.\n'
        f'Expand numbers, currency, units, dates, and abbreviations into '
        f'their spoken form in {lang}.  Keep all other content unchanged. '
        f'Return ONLY the normalized text, no commentary.\n\n'
        f'Text: {text}\n'
        f'Normalized:'
    )

    try:
        t0 = time.time()
        result = bus.infer(
            model_type='llm',
            prompt=prompt,
            options={'max_tokens': min(512, len(text) * 3), 'temperature': 0.0},
        )
        if (time.time() - t0) > _LLM_TIMEOUT_SEC:
            logger.debug(f'tts_normalize LLM call exceeded {_LLM_TIMEOUT_SEC}s — using rule output')
            return None
        if isinstance(result, dict) and 'response' in result:
            response = result['response'].strip()
            # Guard: LLM sometimes echoes the prompt; strip "Normalized:" prefix
            if response.lower().startswith('normalized:'):
                response = response[len('normalized:'):].strip()
            return response or None
        return None
    except Exception as e:
        logger.debug(f'tts_normalize LLM fallback failed: {e}')
        return None


# ─── Public API ────────────────────────────────────────────────────────

def normalize_for_tts(
    text: str,
    lang: str = 'en',
    use_llm: bool = True,
) -> str:
    """Normalize text so TTS can pronounce everything.

    Pipeline:
      1. Cache lookup → return if hit
      2. Rule pass (num2words + regex)
      3. If residual digits / acronyms remain AND use_llm → LLM pass
      4. Cache the final result

    Args:
        text: Raw text (may contain currency, numbers, URLs, acronyms)
        lang: Target ISO 639-1 language code
        use_llm: If False, skip LLM fallback (faster, but residual tokens
                 may remain).  Use when caller is latency-sensitive.

    Returns:
        Normalized text safe for any TTS engine.
    """
    if not text or not text.strip():
        return text

    lang = (lang or 'en').lower().split('-')[0].split('_')[0]

    cached = _cache_get(text, lang)
    if cached is not None:
        return cached

    normalized = rule_normalize(text, lang)

    if use_llm and _has_residual_tokens(normalized):
        llm_out = _llm_normalize(normalized, lang)
        if llm_out:
            normalized = llm_out

    _cache_put(text, lang, normalized)
    return normalized
