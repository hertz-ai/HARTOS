"""Module-level constants shared across HARTOS.

This file is the single source of truth for literal values that were
previously hardcoded in multiple modules. Before this file existed the
channel registry, flask integration, dynamic agent registry, test
fixtures, and example scripts each carried their own copy of
``10077`` / ``8888`` with no mechanism to keep them in sync.

Import from here instead of repeating literals:

    from core.constants import DEFAULT_USER_ID, DEFAULT_PROMPT_ID

Why these specific values:
    DEFAULT_USER_ID = 10077 — the guest/unauthenticated Hevolve user
        account used by channel adapters, test fixtures, and
        standalone entry points that haven't resolved a real user yet.
        Any real user_id comes from UserChannelBinding resolution,
        JWT auth, or the frontend session — the default only fires
        when every other source is empty.
    DEFAULT_PROMPT_ID = 8888 — the pre-registered default agent prompt
        that serves generic chat when no custom agent_id is provided.
        Tests and the channel fallback path both point here so a
        brand-new install answers chat requests out of the box.
"""

DEFAULT_USER_ID: int = 10077
DEFAULT_PROMPT_ID: int = 8888


# ──────────────────────────────────────────────────────────────────────
# HIVE_DEPTH — maximum hop count for any cross-host task / hivemind /
# federation propagation.
#
# The Hevolve topology is a strict 3-level pyramid:
#     flat (desktop)  →  regional (edge)  →  central (cloud)
# A task submitted on a flat node may hop up to regional and up to
# central (2 hops = 3 levels).  Any propagation deeper is either a bug
# (cycle) or an attempt to fan out beyond the published topology, and
# the coordinator must reject it.
#
# Single source of truth — consumed by:
#   - integrations.distributed_agent.task_coordinator.submit_goal
#     (stamps initial hop=0, rejects context['hop'] > HIVE_DEPTH)
#   - integrations.distributed_agent.worker_loop (before re-dispatching
#     a claimed task to a deeper hive layer)
#
# Keep in sync with security.key_delegation.get_node_tier() which also
# enumerates the same three tiers.
# ──────────────────────────────────────────────────────────────────────
HIVE_DEPTH: int = 3


# ISO 639-1 → language name mapping.
# Used by hart_intelligence_entry (system prompt), speculative_dispatcher
# (draft language prompt), and _persist_language (validation).
SUPPORTED_LANG_DICT = {
    "ar": "Arabic", "bg": "Bulgarian", "zh": "Chinese",
    "zh-cn": "Chinese (Simplified)", "nl": "Dutch", "fi": "Finnish",
    "fr": "French", "de": "German", "el": "Greek", "he": "Hebrew",
    "hu": "Hungarian", "is": "Icelandic", "id": "Indonesian",
    "ko": "Korean", "lv": "Latvian", "ms": "Malay", "fa": "Persian",
    "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "es": "Spanish", "sw": "Swahili", "sv": "Swedish", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese",
    "cy": "Welsh", "hi": "Hindi", "bn": "Bengali", "ta": "Tamil",
    "pa": "Punjabi", "gu": "Gujarati", "kn": "Kannada", "te": "Telugu",
    "mr": "Marathi", "ml": "Malayalam", "en": "English",
    "ja": "Japanese", "it": "Italian", "ne": "Nepali", "si": "Sinhala",
    "or": "Odia", "as": "Assamese", "sd": "Sindhi", "ks": "Kashmiri",
    "doi": "Dogri", "mni": "Manipuri", "sa": "Sanskrit", "kok": "Konkani",
    "mai": "Maithili", "brx": "Bodo", "sat": "Santali",
    # SEA Brahmi-derived scripts — added for NON_LATIN_SCRIPT_LANGS
    # membership so the sub-1B draft-skip gate recognises them.
    "km": "Khmer", "lo": "Lao", "my": "Burmese",
    # Cyrillic / Greek — weaker but non-zero 0.8B coverage; listed so
    # NON_LATIN_SCRIPT_LANGS assertion passes.
    "sr": "Serbian",
}


# Indic-language ISO 639-1 codes (Brahmi-family scripts + Urdu/Sindhi
# in Perso-Arabic).  Subset used by TTS routing (Indic Parler) and by
# NON_LATIN_SCRIPT_LANGS below.  Single source for any code that needs
# "is this an Indic language?" — previously duplicated as _INDIC_LANGS
# in tts/tts_engine.py.
INDIC_LANGS = frozenset({
    "as", "bn", "brx", "doi", "gu", "hi", "kn", "kok", "mai",
    "ml", "mni", "mr", "ne", "or", "pa", "sa", "sat", "sd", "ta",
    "te", "ur",
})


# ISO 639-1 codes where sub-1B LLMs (the Qwen3.5-0.8B-class draft
# model) produce Latin-transliterated output ("Vanakkam" instead of
# native Tamil script) due to weak Unicode-script tokenizer coverage.
#
# Single source of truth — consumed by:
#   - integrations.agent_engine.speculative_dispatcher
#     (dispatch_draft_first skip-gate at runtime)
#   - integrations.service_tools.model_lifecycle
#     (on_lang_change subscriber — evicts draft on switch TO these)
#
# Derived from INDIC_LANGS plus the other non-Latin script families.
# Do NOT inline a duplicate frozenset anywhere else — import this.
NON_LATIN_SCRIPT_LANGS = INDIC_LANGS | frozenset({
    # CJK
    "zh", "ja", "ko",
    # RTL (Arabic / Hebrew / Persian)
    "ar", "he", "fa",
    # Southeast Asian Brahmi-derived
    "th", "lo", "km", "my",
    # Cyrillic + Greek (historically included by HIE's inline
    # _NON_LATIN_LANGS; kept here for parity + weaker 0.8B coverage)
    "ru", "uk", "bg", "sr", "el",
})

# Invariant: every code in NON_LATIN_SCRIPT_LANGS must be a registered
# language in SUPPORTED_LANG_DICT.  Fails loud at import time on drift,
# so adding a code to the set without registering its display name is
# a build-time error, not a runtime mystery.
assert NON_LATIN_SCRIPT_LANGS <= set(SUPPORTED_LANG_DICT), (
    f"NON_LATIN_SCRIPT_LANGS has codes not in SUPPORTED_LANG_DICT: "
    f"{NON_LATIN_SCRIPT_LANGS - set(SUPPORTED_LANG_DICT)}"
)


# ──────────────────────────────────────────────────────────────────────
# GREETINGS — canonical, localized "first-run handshake" phrase per
# language.  Used by the TTS first-run handshake smoke test
# (tts/tts_handshake.py) to synthesize a phrase the user actually hears
# before the "Voice engine ready" banner flips.
#
# Single source of truth — replaces two historical parallel paths:
#   1. tts/verified_synth._TEST_PHRASES  (synthesis probe)
#   2. the "ready to use" string that the React card heuristically
#      matched to flip isComplete before any audio had been produced.
#
# Contract:
#   * Keys are ISO 639-1 codes that appear in SUPPORTED_LANG_DICT.
#   * Values are phrases long enough to produce > MIN_AUDIO_BYTES
#     (~0.5s at 22kHz mono) on CPU synth in under 30 seconds.
#   * English 'en' is the fallback when a requested lang is missing.
#
# Scope — only the languages that TTS backends actually ship support
# for today.  Do NOT bulk-add entries without verifying the engine
# can synth them; a missing entry falls back to English, which is
# preferable to synthesizing garbage.
# ──────────────────────────────────────────────────────────────────────
GREETINGS = {
    # Core — every Nunba install can hit these via at least one engine.
    "en": "Hey, I'm Nunba. Can you hear me?",
    "ta": "வணக்கம், நான் நண்பா. என்னுடைய குரல் கேட்கிறதா?",
    "hi": "नमस्ते, मैं नन्बा हूँ। क्या आप मुझे सुन सकते हैं?",
    # Indic Parler cohort — its 21-language allowlist, minus the
    # scripts we haven't hand-verified greetings for.
    # Transliteration intent: the brand "Nunba" reads aloud as "Nan-baa"
    # (rhymes with "Numba" the JIT lib).  Indic scripts use "न + न" /
    # "ன + ன" / equivalent so TTS synth renders the intended phonetics.
    "bn": "হ্যালো, আমি নন্বা। আপনি কি আমাকে শুনতে পাচ্ছেন?",
    "te": "హలో, నేను నన్బా. మీరు నన్ను వినగలరా?",
    "ml": "ഹലോ, ഞാൻ നൻബ. എനിക്കു നിങ്ങൾ കേൾക്കാനാകുമോ?",
    "kn": "ಹಲೋ, ನಾನು ನನ್ಬಾ. ನೀವು ನನ್ನನ್ನು ಕೇಳಬಹುದೆ?",
    "mr": "नमस्कार, मी नन्बा. तुम्ही मला ऐकू शकता का?",
    "gu": "નમસ્તે, હું નન્બા છું. શું તમે મને સાંભળી શકો છો?",
    "pa": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ, ਮੈਂ ਨਨਬਾ ਹਾਂ। ਕੀ ਤੁਸੀਂ ਮੈਨੂੰ ਸੁਣ ਸਕਦੇ ਹੋ?",
    "ur": "ہیلو، میں نَنبا ہوں۔ کیا آپ مجھے سن سکتے ہیں؟",
    # Chatterbox Multilingual + CosyVoice3 cohort
    "zh": "你好,我是 Nunba。你能听到我吗?",
    "ja": "こんにちは、私はNunbaです。聞こえますか?",
    "ko": "안녕하세요, 저는 Nunba입니다. 제 목소리가 들리시나요?",
    "fr": "Bonjour, je suis Nunba. Vous m'entendez ?",
    "es": "Hola, soy Nunba. ¿Me escuchas?",
    "de": "Hallo, ich bin Nunba. Kannst du mich hören?",
    "it": "Ciao, sono Nunba. Mi senti?",
    "ru": "Привет, я Nunba. Вы меня слышите?",
    "pt": "Olá, eu sou o Nunba. Você consegue me ouvir?",
}


# Fallback phrase when the requested language isn't in GREETINGS.
# Kept as a named constant (not a magic literal) so call sites read
# clearly and tests can refer to it by name.
GREETING_FALLBACK_LANG: str = "en"


# Every GREETINGS key MUST be a registered language.  Mirrors the
# NON_LATIN_SCRIPT_LANGS invariant above — a missing display name for
# a greeting-supported lang is a build-time error, not a runtime
# "None" appearing in a banner.
assert set(GREETINGS) <= set(SUPPORTED_LANG_DICT), (
    f"GREETINGS has codes not in SUPPORTED_LANG_DICT: "
    f"{set(GREETINGS) - set(SUPPORTED_LANG_DICT)}"
)
assert GREETING_FALLBACK_LANG in GREETINGS, (
    f"GREETING_FALLBACK_LANG={GREETING_FALLBACK_LANG!r} is not in GREETINGS"
)


# ──────────────────────────────────────────────────────────────────────
# VISION INTENT — keywords that indicate the user is asking Nunba to
# use the camera / describe the scene / see them / read a screen.
# When the draft 0.8B classifier flags a turn as `is_casual=True`, the
# dispatcher short-circuits to the draft reply without loading the
# LangChain tool registry — which means the `Visual_Context_Camera`
# tool never runs even though the user clearly needs vision.
#
# This set is consulted in hart_intelligence_entry.dispatch path as a
# safety net: if the message matches any pattern, we force the full
# LangChain path so `parse_visual_context` is reachable.
#
# Single source of truth — do NOT inline a parallel regex anywhere.
# Keep lower-cased; callers must lowercase the prompt before matching.
# ──────────────────────────────────────────────────────────────────────
import re as _vis_re

VISION_INTENT_KEYWORDS: tuple = (
    # direct camera / vision verbs
    "see me", "see my", "see what", "look at me", "look at my",
    "looking at me", "watch me", "watch my",
    # describe-scene phrases
    "what do you see", "what can you see", "what am i doing",
    "what am i wearing", "what am i holding", "what's in front of",
    "what is in front of", "what is on my", "what's on my",
    "describe me", "describe what", "describe the scene",
    "describe my", "describe this",
    # camera-specific
    "through my camera", "on my camera", "on the camera",
    "via camera", "using camera", "use the camera", "use my camera",
    "turn on camera", "turn on the camera", "start camera",
    # visual modality
    "can you see", "do you see", "are you seeing",
    "visual context", "visual question", "video call",
    # screen / ocr / read-what-is-shown
    "read the screen", "read my screen", "what's on screen",
    "what is on screen", "what does the screen show",
)

# Word-boundary regex compiled once.  Matching uses `search` so partial
# phrasings ("can you see what i'm wearing") hit.
_VISION_PATTERN_SRC: str = r"\b(?:" + "|".join(
    _vis_re.escape(kw) for kw in VISION_INTENT_KEYWORDS
) + r")\b"
VISION_INTENT_PATTERN = _vis_re.compile(_VISION_PATTERN_SRC, _vis_re.IGNORECASE)


def prompt_needs_vision(prompt: str) -> bool:
    """Return True if the prompt clearly requests a vision / camera
    capability (i.e. should route through the LangChain tool path so
    Visual_Context_Camera can fire) even if the draft classifier
    flagged the turn casual.

    Cheap regex match — safe to call on every draft fall-through path.
    """
    if not prompt:
        return False
    try:
        return bool(VISION_INTENT_PATTERN.search(prompt))
    except Exception:
        return False
