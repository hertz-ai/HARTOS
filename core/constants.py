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
    "intelligence from peer Nunba devices and Hevolve cloud nodes."
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
