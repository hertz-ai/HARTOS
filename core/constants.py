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
# AutoGen MessageTokenLimiter budget — single source of truth.
#
# Previously hardcoded as `max_tokens=3500` in 4 sites:
#   create_recipe.py:907       — recipe-create context_handling
#   reuse_recipe.py:1245       — reuse_recipe context_handling
#   reuse_recipe.py:2279       — reuse_recipe alternate path
#   reuse_recipe.py:2896       — reuse_recipe alternate path
#
# Why 2500 (was 3500): live evidence 2026-05-20, llama_server_8082.log
# showed 477 "Context size has been exceeded" errors.  The autogen
# transform clips messages to max_tokens, but the system prompt and
# tool descriptions are appended AFTER the transform — typical
# overhead ~3000-4000 tokens.  With max_tokens=3500 the total prompt
# reaches ~7000+ tokens; under 4-slot concurrency on a 12288 n_ctx
# llama-server, overlap of 2-3 active large prompts exhausts the
# slot pool's effective context budget and llama returns the 400.
# Lowering to 2500 leaves ~6000-7000 tokens total prompt size,
# comfortable headroom for concurrent slots.  Quality impact is
# negligible: 2500 tokens is still ~3-4 messages of dense history,
# more than enough for the next-step reasoning autogen does.  If
# quality regresses, raise back to 3000; never above 3500 without
# a corresponding n_ctx bump on llama-server side.
#
# Tighter sites kept as-is:
#   reuse_recipe.py:818  select_speaker (max_tokens=3000) — speaker-
#       selection prompts are smaller by design.  Already tight.
AUTOGEN_MESSAGE_TOKEN_BUDGET: int = 2500
AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE: int = 1000  # individual message cap, unchanged
AUTOGEN_HISTORY_LIMIT: int = 50                  # message-count limit, unchanged

# ──────────────────────────────────────────────────────────────────────
# Wire-layer trim budget (llm_outbound_logger.py hard left-trim).
# Different scope from AUTOGEN_*: those soft-limit per-agent autogen
# message history; THESE hard-clip the actual bytes sent to llama-server
# so the request never exceeds (n_ctx / num_slots) - max_tokens - safety.
# The wire layer catches autogen + langchain + raw openai SDK uniformly
# (they all funnel through httpx) — the only place we can guarantee
# zero context-overflow 500s across all frameworks.
#
# Env overrides:
#   HEVOLVE_LLAMA_CTX_SIZE  — n_ctx on llama-server (must match the
#                              --ctx-size cmdline; default tracks
#                              Nunba/llama/llama_config.py:1527 = 12288)
#   HEVOLVE_LLAMA_SLOTS     — concurrent slots (n_ctx is partitioned
#                              across slots; default 1)
LLAMA_CTX_SIZE_DEFAULT: int = 12288
LLAMA_SLOTS_DEFAULT: int = 1
WIRE_TRIM_SAFETY_MARGIN_TOKENS: int = 256       # headroom under the budget
WIRE_TRIM_MARKER: str = '...[truncated head]...\n'


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
    # Indian English — Indic-accented variant.  Code preserved (NOT
    # collapsed by `_normalize_lang` the way en-US is) because Nunba's
    # TTS preference for en-IN routes to Indic Parler (ai4bharat,
    # trained on All India Radio + Indic corpora) at position 1, while
    # plain `en` keeps the chatterbox-first American/expressive ladder.
    # See `Nunba/tts/tts_engine.py:_FALLBACK_LANG_ENGINE_PREFERENCE`.
    "en-IN": "English (Indian)",
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


# Script display names (with a native-script sample) for the non-Latin
# languages — used by the regional-tone prompt builder's SCRIPT:
# monoscript directive ("Reply entirely in Devanagari (हिन्दी) ...").
#
# Single source of truth — previously an inline _NON_LATIN_SCRIPTS dict
# in core/agent_personality.py, a SECOND enumeration of "which languages
# are non-Latin" that drifted: ur/as/sa/sd/bg had regional-tone entries
# AND are in NON_LATIN_SCRIPT_LANGS, but were missing from the inline
# dict, so their prompts got the Latin-script rules and the LLM emitted
# romanized text the TTS backends cannot synthesize (task #10).
#
# Keys must stay a subset of NON_LATIN_SCRIPT_LANGS (asserted below);
# agent_personality asserts the other direction at import — every
# non-Latin language it carries tone data for must have a name here.
NON_LATIN_SCRIPT_NAMES = {
    'ta': 'Tamil (தமிழ்)', 'hi': 'Devanagari (हिन्दी)', 'bn': 'Bengali (বাংলা)',
    'te': 'Telugu (తెలుగు)', 'mr': 'Devanagari (मराठी)', 'gu': 'Gujarati (ગુજરાતી)',
    'kn': 'Kannada (ಕನ್ನಡ)', 'ml': 'Malayalam (മലയാളം)', 'pa': 'Gurmukhi (ਪੰਜਾਬੀ)',
    'or': 'Odia (ଓଡ଼ିଆ)', 'ar': 'Arabic (العربية)', 'he': 'Hebrew (עברית)',
    'th': 'Thai (ไทย)', 'ko': 'Hangul (한국어)', 'ja': 'Japanese (日本語)',
    'zh': 'Chinese (中文)', 'ru': 'Cyrillic (Русский)', 'uk': 'Cyrillic (Українська)',
    'el': 'Greek (Ελληνικά)', 'ne': 'Devanagari (नेपाली)',
    # The drifted twelve (see docstring above; the import-time lockstep
    # assert in agent_personality surfaced the six scheduled-Indic ones):
    'ur': 'Perso-Arabic (اردو)', 'as': 'Bengali-Assamese (অসমীয়া)',
    'sa': 'Devanagari (संस्कृतम्)', 'sd': 'Perso-Arabic (سنڌي)',
    'bg': 'Cyrillic (Български)',
    'kok': 'Devanagari (कोंकणी)', 'mai': 'Devanagari (मैथिली)',
    'doi': 'Devanagari (डोगरी)', 'brx': 'Devanagari (बड़ो)',
    'sat': 'Ol Chiki (ᱥᱟᱱᱛᱟᱲᱤ)', 'mni': 'Meitei Mayek (ꯃꯤꯇꯩꯂꯣꯟ)',
    # In NON_LATIN_SCRIPT_LANGS with no tone entry yet — named ahead so a
    # future tone entry cannot re-open the gap:
    'fa': 'Perso-Arabic (فارسی)', 'lo': 'Lao (ລາວ)',
    'km': 'Khmer (ខ្មែរ)', 'my': 'Burmese (မြန်မာ)', 'sr': 'Cyrillic (Српски)',
}

assert set(NON_LATIN_SCRIPT_NAMES) <= NON_LATIN_SCRIPT_LANGS, (
    f"NON_LATIN_SCRIPT_NAMES has codes outside NON_LATIN_SCRIPT_LANGS: "
    f"{set(NON_LATIN_SCRIPT_NAMES) - NON_LATIN_SCRIPT_LANGS}"
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


# ──────────────────────────────────────────────────────────────────────
# Brand identity — used as the assistant's "Who am I?" sentence in
# every English chat path that doesn't already carry a per-agent
# persona.  Single source of truth so HARTOS's draft prompt and Nunba's
# fallback chat handler can never drift on the brand wording.
#
# Non-English paths get their identity through
# core.agent_personality.get_regional_tone_prompt(lang) — which carries
# the Nunba name natively in script (e.g. Tamil "நண்பா").  This
# constant is for English (and any language with no regional-tone
# entry).
#
# Call sites:
#   - integrations/agent_engine/speculative_dispatcher.py
#     (HARTOS draft prompt persona_block default)
#   - Nunba/routes/hartos_backend_adapter.py
#     (cold-boot fallback chat system prompt)
#
# Phrasing intentionally short — every byte costs draft-prompt tokens
# and Nunba is also a TTS-spoken name (the brand identity reads
# naturally aloud).  Per-site framing (privacy mention, language
# directive, etc.) is added on top by the call site, not baked here.
# ──────────────────────────────────────────────────────────────────────
NUNBA_BRAND_IDENTITY: str = (
    "You are Nunba, a friendly and helpful local AI assistant. "
    "Hevolve.ai is the web cloud version of Nunba — same intelligence, "
    "different deployment. With hive enabled, you crowdsource "
    "intelligence from peer Nunba devices and Hevolve cloud nodes. "
    "Local means the user's data stays on this device — it does not "
    "mean offline: fetching public web pages with your web tools is "
    "allowed and expected when a task needs it."
)


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


# ──────────────────────────────────────────────────────────────────────
# ENCOUNTER_TOPICS — WAMP topic namespace for the P2P encounter feature
# (BLE rotating-pubkey discovery → autonomous sighting correlation →
# avatar-only mutual-like swipe → icebreaker agent → map overlay).
#
# Full design in Claude-memory: project_encounter_icebreaker.md.
#
# Single source of truth — consumed by:
#   - integrations.social.encounter_api (publish on swipe/match)
#   - integrations.agent_engine.goal_seeding (encounter_icebreaker_agent
#     subscribes to 'match' topic)
#   - Nunba desktop wamp_router + landing-page crossbarWorker
#   - Hevolve_React_Native AutobahnConnectionManager (subscribes to
#     per-user 'sighting' and 'icebreaker' private topics)
#
# Per-user privacy scoping: 'sighting', 'swipe', 'icebreaker' are
# always prefixed with the user_id by the publisher; 'match' publishes
# TWO events (one per participant) so one user's subscription never
# leaks the other's pubkey outside the matched pair.
#
# Do NOT inline duplicate topic strings; import from here.
# ──────────────────────────────────────────────────────────────────────
ENCOUNTER_TOPIC_SIGHTING: str = 'com.hevolve.encounter.sighting'
ENCOUNTER_TOPIC_SWIPE: str = 'com.hevolve.encounter.swipe'
ENCOUNTER_TOPIC_MATCH: str = 'com.hevolve.encounter.match'
ENCOUNTER_TOPIC_ICEBREAKER: str = 'com.hevolve.encounter.icebreaker'

ENCOUNTER_TOPICS: tuple = (
    ENCOUNTER_TOPIC_SIGHTING,
    ENCOUNTER_TOPIC_SWIPE,
    ENCOUNTER_TOPIC_MATCH,
    ENCOUNTER_TOPIC_ICEBREAKER,
)

# Invariant: all encounter topics share the canonical 'com.hevolve.
# encounter.' prefix so crossbar ACL rules + log grepping are uniform.
# A topic outside this prefix would not be scoped by the existing
# WAMP router authorization (wamp_router.py _handle_publish per-topic
# authorization, Task #301), so drift here is a security regression.
_ENCOUNTER_PREFIX = 'com.hevolve.encounter.'
assert all(t.startswith(_ENCOUNTER_PREFIX) for t in ENCOUNTER_TOPICS), (
    f"ENCOUNTER_TOPICS must all share prefix {_ENCOUNTER_PREFIX!r}: "
    f"{[t for t in ENCOUNTER_TOPICS if not t.startswith(_ENCOUNTER_PREFIX)]}"
)


# ──────────────────────────────────────────────────────────────────────
# ENCOUNTER feature tunables — physical-world sighting correlation.
# A beacon is treated as a SIGHTING (autonomous pairing of the real
# person in front of the user with their rotating pubkey) only when
# ALL three conditions hold together.  Loosening any one of these
# degrades to random-pairs-in-a-crowd; tightening any one degrades to
# never-fires.  Values tuned for phone-in-hand, face-to-face scenario.
# Pocket-mode (low motion detected on both devices) relaxes compass
# tolerance to ±90° per BleSightingDetector rules.
#
# Consumed by:
#   - Hevolve_React_Native BleSightingDetector (Kotlin port of same)
#   - integrations.social.encounter_api.ENCOUNTER_SIGHTING_RULES
#   - tests.unit.test_sighting_correlation
# ──────────────────────────────────────────────────────────────────────
ENCOUNTER_SIGHTING_RSSI_PEAK_DBM: int = -55      # ~1.5m line-of-sight
ENCOUNTER_SIGHTING_MIN_DWELL_SEC: int = 3        # both parties slowed/stopped
ENCOUNTER_SIGHTING_COMPASS_TOL_DEG: int = 30     # devices facing within cone
ENCOUNTER_PUBKEY_ROTATION_SEC: int = 15 * 60     # 15 min / relaunch / geo-shift
ENCOUNTER_DISCOVERABLE_TTL_SEC: int = 4 * 60 * 60  # 4h auto-off
ENCOUNTER_DISCOVERABLE_MAX_TOGGLES_24H: int = 6
ENCOUNTER_SIGHTING_EXPIRES_SEC: int = 24 * 60 * 60  # swipe grace window
ENCOUNTER_MATCH_WINDOW_SEC: int = 5 * 60         # both sightings must be
                                                  # within this window to match
ENCOUNTER_DRAFT_MAX_CHARS: int = 220             # icebreaker length cap


# ──────────────────────────────────────────────────────────────────────
# CHAT_TOPICS — WAMP topic namespace for cross-device chat mirroring
# (U1-U8 workstream, task ledger #389).
#
# chat.new  — a new assistant or user message was persisted.  Payload
#             carries the full ChatMessage row (seq, msg_id, user_id,
#             agent_id, role, content, request_id, lang, device_id,
#             attachments, created_at).  Every device subscribed to the
#             per-user topic mirrors the row into its local view.
# chat.ack  — a subscriber ACKs receipt up to seq=N.  Used by the server
#             to decide when a message can be evicted from the hot cache
#             (the durable row stays in the DB for cursor-pull replay).
#
# Per-user scoping: publisher MUST suffix the user_id so a subscriber
# can ONLY see their own messages.  Enforced by Nunba's wamp_router
# _handle_publish per-topic authorization (Task #301).
#
# Do NOT inline duplicate topic strings; import from here.
# ──────────────────────────────────────────────────────────────────────
# CENTRAL HOST — ONE literal for the central instance's DNS name
#
# The same hostname was written out in FOUR places, in two spellings:
#   core/config_cache.py    'https://azurekong.hertzai.com:8443/db'
#   core/wamp_url.py        the WAMP router default
#   15 call sites           'ws://aws_rasa.hertzai.com:8088/ws' (hardcoded)
#   scripts/run*.sh|bat     WAMP_URL=ws://azurekong.hertzai.com:8088/ws
#
# `aws_rasa` and `azurekong` are the SAME MACHINE — verified 2026-08-18, both
# resolve to 106.51.181.24 and serve byte-identical /ws (HTTP 200, 11280 bytes)
# and /publish (405, POST-only bridge). `azurekong` is the correct name
# (steward, 2026-08-18) and is what the run scripts already export.
#
# Consumers build their own URL from this plus the port that belongs to their
# service — WAMP takes core.port_registry.get_port('crossbar'); the central DB
# keeps its own 8443. Do NOT inline the hostname; import it from here.
#
# THIS IS A DEFAULT, NOT A MANDATE. Unifying the literal must never cost a system
# the ability to run its OWN server (steward, 2026-08-18). Every consumer keeps
# its own independent override and the override always wins:
#
#   own WAMP router     WAMP_URL=ws://127.0.0.1:8088/ws
#   regional relay      WAMP_URL=ws://regional-3.lan:8088/ws
#   private central DB  HEVOLVE_CENTRAL_DB_URL=https://my-private-cloud:8443/db
#
# They are deliberately SEPARATE knobs: a node may run its own router while still
# reading the central DB, or the reverse. Do NOT collapse them into one
# "HART_HOST" — that would trade the flexibility this constant was only ever
# meant to de-duplicate. tests/unit/test_wamp_router_url_resolves.py asserts each
# consumer stays independently steerable.
# ──────────────────────────────────────────────────────────────────────
CENTRAL_HOST: str = 'azurekong.hertzai.com'

#: Legacy DNS alias for the SAME box. Recorded so a reader who greps the old
#: literal lands on this explanation instead of reintroducing it.
CENTRAL_HOST_LEGACY_ALIAS: str = 'aws_rasa.hertzai.com'


# ──────────────────────────────────────────────────────────────────────
CHAT_TOPIC_NEW: str = 'com.hertzai.hevolve.chat.new'
CHAT_TOPIC_ACK: str = 'com.hertzai.hevolve.chat.ack'

CHAT_TOPICS: tuple = (
    CHAT_TOPIC_NEW,
    CHAT_TOPIC_ACK,
)

# Invariant mirrors ENCOUNTER_TOPICS: shared prefix = uniform ACL.  The
# existing chat-reply topic 'com.hertzai.hevolve.chat.{user_id}' at
# hart_intelligence_entry.py:2174,4211 uses the same prefix — per-user
# suffixing happens at publish-time, not at constant-definition time.
_CHAT_PREFIX = 'com.hertzai.hevolve.chat.'
assert all(t.startswith(_CHAT_PREFIX) for t in CHAT_TOPICS), (
    f"CHAT_TOPICS must all share prefix {_CHAT_PREFIX!r}: "
    f"{[t for t in CHAT_TOPICS if not t.startswith(_CHAT_PREFIX)]}"
)

# Cursor-pull tunables — bound the worst-case pull size so a freshly-
# restored device doesn't stall on a 10k-message replay, and so a
# malicious cursor=0 pull can't exfiltrate the whole table.
CHAT_CURSOR_PULL_MAX_ROWS: int = 500
CHAT_CURSOR_PULL_MAX_BYTES: int = 2 * 1024 * 1024  # 2 MB body cap


# Chat-hot-path stage strings (#508) — single i18n site, consumed by
# publish_chat_stage().  Keep values ≤ 60 chars (UI bubble truncates).
CHAT_STAGE_TEXTS: dict = {
    'loading_context':  'Loading your context…',
    'loading_memory':   'Recalling our recent chat…',
    'loading_tools':    'Preparing tools…',
    'thinking':         'Thinking…',
    'generating':       'Generating a response…',
    'finalizing':       'Finalizing the answer…',
    # Generic fallback used by _with_tool_logging — callers pass
    # text=TOOL_LABELS.get(name, 'Running {name}…') for the real text.
    'tool_call':        'Running a tool…',
}
CHAT_STAGES: frozenset = frozenset(CHAT_STAGE_TEXTS)


# Chat-bubble wire contract (#508) — the `priority` + `action` values carried in
# the com.hertzai.hevolve.chat.{user_id} envelope and keyed on by the frontend
# (Demopage.handleDataReceived: priority===49 && action==='Thinking').
# CANONICAL here so the publisher (core.peer_link.crossbar_publish) and every
# consumer share ONE source — `49` / 'Thinking' / 'Status' were previously magic
# literals duplicated across both envelope shapes.  The frontend mirrors these in
# landing-page/src/constants/chatBubble.js (cross-language; keep in lockstep).
CHAT_BUBBLE_PRIORITY: int = 49
# The model's ACTUAL reasoning → rendered as Thought-process Steps (id-49 container).
CHAT_ACTION_THINKING: str = 'Thinking'
# Canned pipeline PROGRESS (publish_chat_stage / routing status) → drives the
# "analysing…" spinner ONLY, never a Step.
CHAT_ACTION_STATUS: str = 'Status'


# ──────────────────────────────────────────────────────────────────────
# RECIPE capability-mesh topics: the PROACTIVE advert layer over the
# reactive per-goal peer recipe pull (integrations/google_a2a/peer_reuse).
#
# When a node BANKS an exportable recipe it gossip-broadcasts a
# 'recipe_available' advert (peer_reuse.announce_recipe_available);
# admitted peers cache it (peer_reuse.on_recipe_available_advert) and the
# daemon consults the cache BEFORE the O(peers) discovery sweep
# (peer_reuse.consume_advert). Mirrors the RALT skill ANNOUNCE-then-PULL
# pattern (world_model_bridge.distribute_skill_packet).
#
# RECIPE_AVAILABLE_TOPIC is the flat announce topic. The dot-alias
# 'recipe.available' in core.peer_link.message_bus.TOPIC_MAP points here
# so the SAME gossip publish rides the WAMP bus the day a node-local
# recipe router ships (WAMP-ready, no new transport today).
#
# RECIPE_SEMANTIC_TOPIC partitions adverts by capability so a future
# node-local router can subscribe to just the classes it wants.
#   {semantic_class} is derived from the goal_type / goal category by a
#       RULE/HASH based normalizer (peer_reuse._semantic_class_for): it
#       is NOT ML; it lowercases + slugifies the goal_type token and
#       falls back to a stable bucket when no goal_type is known.
#   {slug} is the goal's stable identity slug (bootstrap_slug, else a
#       normalized goal_title): the SAME identity peer_reuse discovery
#       already matches on, reused so producer and consumer key adverts
#       identically (no second identity scheme).
#
# Do NOT inline duplicate topic strings; import from here.
# ──────────────────────────────────────────────────────────────────────
RECIPE_AVAILABLE_TOPIC: str = 'com.hertzai.hevolve.recipe.available'
RECIPE_SEMANTIC_TOPIC: str = 'com.hertzai.hevolve.recipe.{semantic_class}.{slug}'


# Per-tool human-readable labels for tool_call stage emits (#508).  Static
# entries cover the hardcoded tools in get_tools(is_first=True) + canonical
# provider/builtin tools.  Dynamic tool registries (skills, service_tools,
# providers, Tier-2 goal-aware) should call register_tool_label() at the
# point of Tool() construction so their tools also get a friendly label.
# Unknown tools fall back to "Running {name}…" at the emit site.
# Keep values <= 60 chars (UI bubble truncates).
# ── Upstream LLM states that are TRANSIENT, not failures ──────────────
# llama-server answers HTTP 200 with {"error": {"message": "Loading model"}}
# while weights load.  That was surfaced to the user verbatim as
# "I couldn't process that request - Loading model" (live 2026-08-18, twice
# in a row on "Hi!"), which leaks llama.cpp's internal wording, reads as a
# permanent failure, and was returned as a normal assistant turn -- so it
# was persisted into conversation history and fed back to the model as a
# prior turn.
#
# The copy below is the same wording routes/chatbot_routes.py already sends
# when the server is unreachable, so both "engine down" and "engine warming
# up" tell the user the same thing.  Detail stays in the log, not the reply.
LLM_TRANSIENT_LOADING_MARKERS: tuple = (
    'loading model',
    'model is loading',
    'model loading',
)

LLM_LOADING_REPLY: str = (
    "Starting the local AI engine for you now. "
    "Give it a few seconds and send your message again."
)

LLM_GENERIC_ERROR_REPLY: str = (
    "I ran into a problem handling that. Please try again."
)


TOOL_LABELS: dict = {
    # Memory + history
    'FULL_HISTORY':                'Searching your message history…',
    'recall_memory':               'Recalling memories…',
    'remember_memory':             'Saving to memory…',
    # Computation + web
    'Calculator':                  'Calculating…',
    'google-search':               'Searching the web…',
    # Vision / camera
    'Visual_Context_Camera':       'Looking through camera…',
    'Visual_Context_Watcher':      'Setting up a visual watcher…',
    'Request_Camera_Access':       'Requesting camera access…',
    'Image_Inference_Tool':        'Analyzing the image…',
    'Animate_Character':           'Animating your character…',
    # Media generation
    'Generate_Image':              'Generating an image…',
    'Text to image':               'Generating an image from text…',
    # Channels + invites + rooms
    'Connect_Channel':             'Connecting channel…',
    'Invite_Friend':               'Generating an invite link…',
    'Join_External_Room':          'Joining the room…',
    # Agent + planning
    'Agentic_Router':              'Planning the next steps…',
    'Create_Agent':                'Starting agent creation…',
    # System / OS
    'Shell_Command':               'Running a command…',
    'Computer_Action':              'Acting on your screen…',
    'Computer_Screenshot':         'Taking a screenshot…',
    'Request_Screen_Access':       'Requesting screen access…',
    # Cloud expert
    'Cloud_LLM':                   'Consulting the cloud expert…',
    # Navigation + UX
    'Navigate_App':                'Opening the requested page…',
    'List_Pending_Actions':        'Listing pending actions…',
    # Data extraction + user lookup
    'Data_Extraction_From_URL':    'Extracting data from URL…',
    'User_details_tool':           'Looking up user details…',
    'List_Agents':                 'Listing your agents…',
    'OpenAPI_Specification':       'Calling the OpenAPI service…',
    # Resource requests + self-improvement
    'Request_Resource':            'Requesting a resource…',
    'Suggest_Share_Worthy_Content': 'Finding share-worthy content…',
    'Observe_User_Experience':     'Recording an observation…',
    'Self_Critique_And_Enhance':   'Reflecting on past suggestions…',
    # ── reuse_recipe.py inner tools (autogen function_map names) ──
    # #509 — added here (canonical static home) instead of being
    # dynamically register_tool_label()-ed at reuse_recipe import time.
    'txt2img':                     'Generating an image…',
    'img2txt':                     'Reading the image…',
    'save_data_in_memory':         'Saving to memory…',
    'get_saved_metadata':          'Listing saved memory…',
    'get_data_by_key':             'Recalling from memory…',
    'get_user_id':                 'Looking up your user id…',
    'get_prompt_id':               'Looking up your prompt id…',
    'Generate_video':              'Generating a video…',
    'get_user_uploaded_file':      'Fetching your uploaded file…',
    'get_user_camera_inp':         'Reading from your camera…',
    'get_chat_history':            'Reading recent chat history…',
    'search_visual_history':       'Searching your visual history…',
    'register_visual_watcher':     'Registering a visual watcher…',
    'search_long_term_memory':     'Searching long-term memory…',
    'save_to_long_term_memory':    'Saving to long-term memory…',
    'create_scheduled_jobs':       'Scheduling a task…',
    'send_message_to_user':        'Sending you a message…',
    'send_presynthesized_video_to_user': 'Sending a video to you…',
    'send_message_in_seconds':     'Scheduling a message…',
    'consult_expert':              'Consulting the cloud expert…',
    'get_user_camera_inp_by_mins': 'Reading camera history…',
    'execute_windows_or_android_command': 'Running a system command…',
    'google_search':               'Searching the web…',
    'create_new_agent':            'Starting agent creation…',
    'update_persona':              'Updating role/persona in DB…',
    # ── journey_engine.py + outreach_crm_tools.py (autogen tools) ──
    # #509 — moved from inline _journey_ui_labels / _outreach_ui_labels
    # dicts at the call site so register_labeled_function picks them up
    # via TOOL_LABELS.get(name, …) without per-site dups.
    'view_journey_pipeline':       'Reviewing journey pipeline…',
    'advance_prospect_stage':      'Advancing prospect stage…',
    'run_journey_tick':            'Running journey tick…',
    'send_prospect_message':       'Sending prospect message…',
    'create_prospect':             'Creating CRM prospect…',
    'send_outreach_email':         'Sending outreach email…',
    'create_followup_sequence':    'Scheduling follow-up sequence…',
    'check_pending_followups':     'Checking pending follow-ups…',
    'move_prospect_stage':         'Moving prospect to next stage…',
    'get_pipeline_status':         'Loading pipeline status…',
    'list_sent_emails':            'Listing sent emails…',
    # ── system_introspect_tool.py — duplicate-of-truth from
    #    _INTROSPECT_LABELS pulled into the canonical dict so the
    #    static names live in one place (the module still keeps its
    #    own dict for the LangChain `labeled_tool` call sites). ──
    'get_gpu_tier':                'Checking GPU tier…',
    'list_running_models':         'Looking up active models…',
    'get_tts_status':              'Checking TTS engine status…',
    'get_tier_thresholds':         'Reading tier thresholds…',
    'get_boot_decision':           'Reading boot rationale…',
    'get_system_health':           'Checking system health…',
    'list_decisions':              'Listing recorded decisions…',
    'explain_decision':            'Explaining a system decision…',
    # ── core/agent_tools.py @log_tool_execution-decorated functions
    #    (the 28 "canonical" core tools shared by create_recipe +
    #    reuse_recipe via register_core_tools).  These are *autogen
    #    function_map* names — snake_case — and are SEPARATE names from
    #    their similarly-named LangChain Tool() literals above (e.g.,
    #    `text_2_image` (autogen) vs `Generate_Image` / `Text to image`
    #    (LangChain).  Same verb, same UX, registered both ways.  A
    #    follow-up task will canonicalize the names; for now we list
    #    them all so the lookup never misses. ──
    'text_2_image':                'Generating an image…',
    'data_extraction_from_url':    'Extracting data from URL…',
    'device_control':              'Acting on your device…',
    'get_user_details':            'Looking up user details…',
    'observe_user_experience':     'Recording an observation…',
    'request_resource':            'Requesting a resource…',
    'self_critique_and_enhance':   'Reflecting on past suggestions…',
    'suggest_share_worthy_content': 'Finding share-worthy content…',
    # ── integrations/channels/agent_tools.py @log_tool_execution funcs
    #    (channel-adapter tools shared by both create + reuse). ──
    'register_channel':            'Registering a channel…',
    'send_to_channel':             'Sending to a channel…',
    'send_install_link':           'Sending an install link…',
    'list_channels':               'Listing your channels…',
    'get_channel_context':         'Reading channel context…',
    'reconnect_channel':           'Reconnecting channel…',
    'disconnect_channel':          'Disconnecting channel…',
    # ── integrations/providers/agent_tools.py labeled_tool() literals
    #    that the prior pass missed (casing drift from Generate_video). ──
    'Generate_Video':              'Generating a video…',
    'List_AI_Providers':           'Listing AI providers…',
    'Provider_Leaderboard':        'Loading provider leaderboard…',
    # ── AP2 (Agent Protocol 2) — agentic commerce payment tools.
    #    Registered dynamically via get_ap2_tools_for_autogen() in
    #    create_recipe.py:1761 + reuse_recipe.py:2494 in both flows.
    'request_payment':             'Requesting payment authorization…',
    'authorize_payment':           'Authorizing payment…',
    'process_payment':             'Processing payment via gateway…',
}


def register_tool_label(name: str, label: str) -> None:
    """Register a UI label for a tool name.  Used by dynamic tool registries
    (integrations.skills, integrations.service_tools, integrations.providers,
    Tier-2 goal-aware tool packs) to supply human-readable status text next
    to where they construct Tool() objects.  Idempotent — overwrites prior
    entry for the same name so a registry can refine its labels over time.

    Tools that don't register a label fall back to generic_tool_label() at the
    emit site (core.tool_logging._emit_tool_call_stage).
    """
    if not name or not label:
        return
    TOOL_LABELS[str(name)] = str(label)[:60]


def generic_tool_label(name: str) -> str:
    """The single 'Running {name}…' fallback label for a tool with no registered
    TOOL_LABELS entry.  Canonical home (#116): labeled_tool.generic_label,
    labeled_autogen_function.generic_autogen_label, and the tool_logging emit
    site all delegate here instead of pasting the template."""
    return f'Running {name}…'


# ──────────────────────────────────────────────────────────────────────
# LATENCY BUDGETS — the ONE place a performance ceiling is written down.
#
# CLAUDE.md's review checklist names the hot-path budgets (chat 1.5s,
# draft 300ms, cache <1ms) but NOTHING enforced them, while six test
# files each carried their own unrelated inline literal (`< 1.0`,
# `< 0.5`, `< 2.0`, `< 6.0`, `< 5.0`, `< 0.4`) with no shared definition
# and no way to tell a deliberate ceiling from a number someone typed.
#
# These are CEILINGS, not targets: a test asserts the measured value is
# UNDER the budget. Keys are stable; a caller imports the name, never a
# literal, so tightening a budget is one edit and every enforcement
# point moves with it.
#
# Adding a budget is cheap and expected. Changing one is a product
# decision — the number is the contract, so state WHY in the comment.
# ──────────────────────────────────────────────────────────────────────
LATENCY_BUDGETS = {
    # ── Hot path (CLAUDE.md review checklist) ──
    # A user chat turn's non-LLM overhead. core.user_context enforces the
    # same 1.5s as its own DEFAULT_BUDGET_SECONDS wall-clock fetch cap.
    'chat_turn_overhead_s': 1.5,
    # Draft/speculative classify: must stay an order under the main model
    # or the speculation costs more than it saves.
    'draft_classify_s': 0.3,
    # An in-process cache lookup. Anything slower is not a cache.
    'cache_lookup_ms': 1.0,

    # ── Shell / desktop responsiveness ──
    # The metrics poll must never block the shell's paint loop.
    'shell_metrics_poll_s': 0.4,
    # Cross-process authority check (ai_sensing gate) on the toast path.
    'sense_gate_s': 0.5,

    # ── Failure detection (fast-fail is a feature) ──
    # A crashed GPU worker must be noticed before the user retries.
    'gpu_worker_crash_detect_s': 2.0,
    # Startup failure of a model server, incl. its retry window.
    'gpu_worker_startup_fail_s': 6.0,
    # Dedup/coordination decisions are pure-compute; sub-second or the
    # coordinator becomes the bottleneck it exists to remove.
    'coordinator_dedup_s': 0.5,

    # ── Never-block guarantees (an async call that blocks is a hang) ──
    # A fire-and-forget capture must RETURN, not wait for its own work.
    'async_dispatch_return_s': 1.0,
    # Telemetry object construction, 10k iterations. Telemetry that costs
    # measurable time stops being telemetry and becomes the workload.
    'telemetry_build_10k_ms': 1000.0,
    # Cold import of the Flask entry module. Not "fast", but bounded: the
    # learning pipeline must stay in the background rather than being
    # dragged onto the import path.
    'entry_module_import_s': 5.0,
    # First cross-device state sync after a cold start. Bounded so a new
    # device feels joined rather than pending.
    'multidevice_cold_sync_s': 5.0,

    # ── Boot, enforced IN THE VM on a real booted node (task #29) ──
    # Every budget above is enforced by a PYTHON suite. Nothing enforced
    # anything on the booted OS, which is how userspace startup reached
    # 6min36s with no test failing on it.
    #
    # `boot_userspace_s` is a REGRESSION CEILING, not the product target.
    # Measured across nixosTest desktop nodes on 2026-08-02:
    #   3min30s, 3min56s, 4min22s, 4min50s, 6min48s  (210s - 408s)
    # The spread itself says the pole is load-dependent, and no single unit
    # explains it: hart-sandbox-firstboot, the last unit before "Startup
    # finished", takes ~2.7s and only STARTS at t=280s. So the ceiling is set
    # above the observed max deliberately — a budget nobody can pass gets
    # disabled, and then nothing is measured at all. It catches a
    # catastrophic regression today; tightening it needs `systemd-analyze
    # blame` to name the pole, which the VM test now captures on EVERY run
    # (pass or fail) precisely so the next tightening is evidence-led.
    'boot_userspace_s': 600.0,
    # Kernel-side boot. Observed 6.5s-12.5s, so this one is already near
    # where it should be and is a genuine gate rather than a placeholder.
    'boot_kernel_s': 30.0,
    # /status must answer fast even on a busy node: it is what every health
    # check and the shell's own boot-wait poll. A slow /status is
    # indistinguishable from a hung one to the caller.
    'status_endpoint_ms': 2000.0,
}


def latency_budget(name: str) -> float:
    """The ceiling for ``name``. Raises on an unknown key ON PURPOSE.

    A typo'd budget name must fail the test loudly rather than silently
    returning a default that asserts nothing — an enforcement point that
    quietly stops enforcing is worse than no enforcement at all.
    """
    try:
        return LATENCY_BUDGETS[name]
    except KeyError:
        raise KeyError(
            f"unknown latency budget {name!r}; known: "
            f"{sorted(LATENCY_BUDGETS)}") from None


# Every budget must be a POSITIVE, FINITE number — a zero or negative
# ceiling can never be satisfied, and an infinite one silently disables
# the check. Loud at import, like the language-registry invariants above.
assert all(isinstance(v, (int, float)) and 0 < v < 3600
           for v in LATENCY_BUDGETS.values()), (
    "LATENCY_BUDGETS values must be positive finite seconds/ms: "
    f"{ {k: v for k, v in LATENCY_BUDGETS.items() if not (isinstance(v, (int, float)) and 0 < v < 3600)} }"
)


#: Role name used when a prompt config declares NO personas.
#:
#: create_agents_for_role branches on `len(personas) > 1`, so the else covers
#: BOTH one persona and NONE — and the empty case used to index personas[0] and
#: raise IndexError, 500-ing the whole /chat request.
#:
#: Empty is normal on the hardware HART must run on. A 0.8B model on a CPU-only
#: box routinely returns truncated or malformed persona JSON, so the config ends
#: up with no 'personas' key. No personas simply means there is no role to
#: choose between — a single-role agent — which is a state to name, not to crash
#: on. 'assistant' matches the speaker name the rest of reuse_recipe already
#: uses for the default agent.
DEFAULT_SINGLE_ROLE = 'assistant'


#: Values of ``AgentGoal.created_by`` that name a PROCESS, not a user.
#:
#: `created_by` is a provenance field: it records what produced the goal.
#: For human-created goals that happens to be a user id, which is why
#: core.event_attribution.goal_owner_user_id reads it as an ownership
#: fallback.  For machine-seeded goals it is a daemon name, and treating
#: one as an identity produces a user id that cannot exist.
#:
#: Measured live 2026-08-16 across 105 active goals: owner_id 0/105 and
#: user_id 0/105 were populated, so `created_by` decided every case —
#: 'error_advice' x52 and 'system_bootstrap' x32 against just 6 real
#: uuids.  /api/social/users/system_bootstrap returns 404.  Events
#: stamped with those labels passed the P3a SSE guard (a non-empty
#: user_id) and were then delivered to nobody, with nothing logged.
#:
#: Derived by reading the producers, never guessed.  Each entry is a
#: literal written at exactly one site:
#:   error_advice           core/error_advice.py
#:   system_bootstrap       integrations/agent_engine/goal_seeding.py
#:   auto_remediation       integrations/agent_engine/goal_seeding.py
#:   intelligence_milestone integrations/agent_engine/agent_daemon.py
#:   self_healing_dispatcher integrations/agent_engine/self_healing_dispatcher.py
#:   revenue_aggregator     integrations/agent_engine/revenue_aggregator.py
#:   system_daemon          integrations/social/dashboard_service.py
#:
#: tests/unit/test_goal_owner_machine_authors.py AST-scans the tree and
#: fails if a new bare-label `created_by=` literal appears unregistered,
#: so this set cannot silently fall behind the producers.
MACHINE_GOAL_AUTHORS = frozenset({
    'error_advice',
    'system_bootstrap',
    'auto_remediation',
    'intelligence_milestone',
    'self_healing_dispatcher',
    'revenue_aggregator',
    'system_daemon',
})


# ── HTTP payload policy (ONE source, two consumers) ──────────────────────
# hart_intelligence_entry sets Flask's MAX_CONTENT_LENGTH from this, and
# core.serve passes it to Hypercorn's AsyncioWSGIMiddleware as max_body_size.
# Before 2026-08-21 the transport side was never set, so the middleware's
# library default of 2**16 (64 KB) silently rejected every POST body larger
# than that with an empty 400 — measured live: a 50 KB multipart reached the
# Flask handler, a 200 KB one never did.  A 5-second voice recording is
# ~150 KB; batch /voice/transcribe and any real upload were unreachable
# while the app-level policy said 2 MB was fine.
import os as _os
MAX_PAYLOAD_BYTES = int(_os.environ.get('HEVOLVE_MAX_PAYLOAD_BYTES', 2 * 1024 * 1024))
