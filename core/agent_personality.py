"""
Agent Personality Engine — Living characters, not just role names.

Every HARTOS agent gets a unique personality built from cultural wisdom traits.
Personality is deterministic (same role+goal → same personality), persistent
across sessions (saved alongside recipes), and adaptive (style adjusts to user).

Reuses cultural_wisdom.CULTURAL_TRAITS — no parallel system (DRY).

Used by:
  - create_recipe.py   (CREATE mode — generate + inject into all agents)
  - reuse_recipe.py    (REUSE mode — load saved personality)
  - gather_agentdetails.py (agent creation wizard)
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Data Model
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AgentPersonality:
    """A living personality for an agent — identity, traits, tone, and behaviors."""

    # Identity
    agent_name: str = ""            # e.g., "swift.falcon" from agent creation
    role: str = ""                  # e.g., "coder", "marketer"
    persona_name: str = ""          # human-readable name, e.g., "Aria"

    # Core traits (3-5 selected from CULTURAL_TRAITS)
    primary_traits: List[str] = field(default_factory=list)

    # Communication style
    tone: str = "warm-casual"       # "warm-casual" | "focused-professional" | "playful-encouraging"
    greeting_style: str = ""        # how this agent opens conversations

    # Proactive behavior flags
    proactive_vision_check: bool = True      # asks clarifying Qs before executing
    proactive_insight_sharing: bool = True    # shares observations about user patterns
    proactive_encouragement: bool = True      # celebrates progress, encourages on setbacks

    # Adaptiveness
    formality_preference: str = "match_user"  # "casual" | "formal" | "match_user"
    verbosity_preference: str = "balanced"    # "concise" | "balanced" | "detailed"

    # Reflexiveness
    self_awareness_prompt: str = ""  # what this agent knows/doesn't know

    # Metadata
    interaction_count: int = 0


# ═══════════════════════════════════════════════════════════════════════
# Persona Names — curated for warmth and personality
# ═══════════════════════════════════════════════════════════════════════

_PERSONA_NAMES = [
    "Aria", "Kai", "Nova", "Zara", "Leo", "Mira", "Sol", "Ren",
    "Ivy", "Ori", "Sage", "Juno", "Ash", "Lira", "Bodhi", "Nia",
    "Cleo", "Yara", "Dax", "Koda", "Suri", "Vex", "Wren", "Zephyr",
]

# ═══════════════════════════════════════════════════════════════════════
# Tone Presets — mapped from role categories
# ═══════════════════════════════════════════════════════════════════════

_ROLE_TONE_MAP = {
    'coding': 'focused-professional',
    'technical': 'focused-professional',
    'coder': 'focused-professional',
    'developer': 'focused-professional',
    'engineer': 'focused-professional',
    'creative': 'playful-encouraging',
    'creator': 'playful-encouraging',
    'designer': 'playful-encouraging',
    'artist': 'playful-encouraging',
    'writer': 'playful-encouraging',
    'marketer': 'playful-encouraging',
    'marketing': 'playful-encouraging',
    'support': 'warm-casual',
    'helper': 'warm-casual',
    'assistant': 'warm-casual',
    'service': 'warm-casual',
    'analyst': 'focused-professional',
    'finance': 'focused-professional',
    'researcher': 'focused-professional',
    'leader': 'warm-casual',
    'manager': 'warm-casual',
}

_GREETING_STYLES = {
    'warm-casual': "Hey there! I'm {name}, and I'm genuinely excited to work with you on this.",
    'focused-professional': "Hello! I'm {name}. Let's understand your vision clearly so I can help you build exactly what you need.",
    'playful-encouraging': "Hi! I'm {name}, your creative partner. Tell me what you're dreaming up and let's make it real!",
}

_SELF_AWARENESS_MAP = {
    'coding': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'coder': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'developer': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'engineer': "I'm strong at code architecture and debugging, but I'll check with you on domain-specific business rules.",
    'technical': "I excel at technical implementation, but I'll verify assumptions about your specific use case.",
    'creative': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'creator': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'designer': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'artist': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'writer': "I love ideating and creating, but I value your taste and vision — I'll always check that my ideas match yours.",
    'support': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'helper': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'assistant': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'service': "I'm here to help and listen deeply. If something is beyond my capabilities, I'll be honest and find a way.",
    'analyst': "I'm thorough with data and patterns, but I'll verify my interpretations align with your context.",
    'researcher': "I'm thorough with data and patterns, but I'll verify my interpretations align with your context.",
    'finance': "I'm cautious and methodical with numbers, but I'll always confirm decisions that affect your resources.",
    'leader': "I can coordinate and strategize, but the final direction is always yours.",
    'manager': "I can coordinate and strategize, but the final direction is always yours.",
    'marketer': "I bring creative energy and audience insight, but I'll verify messaging aligns with your brand voice.",
    'marketing': "I bring creative energy and audience insight, but I'll verify messaging aligns with your brand voice.",
}

# ═══════════════════════════════════════════════════════════════════════
# Regional Tone — colloquial code-mixing for user's language/region
# ═══════════════════════════════════════════════════════════════════════
#
# Data-driven: one compact entry per language, template generates the prompt.
# Covers all 26 Nunba languages + 39 Hevolve Android languages (51 unique).
#
# Tier system:
#   'full'    — rich code-mix with example phrases (languages we're confident about)
#   'light'   — casual filler words only (medium confidence)
#   'formal'  — respectful tone, no code-mixing (low-resource / hallucination risk)
#
# RULE: Never fabricate words. For 'formal' tier languages, the LLM uses
# the language name for warmth but keeps responses in English with technical terms.

_REGIONAL_TONE_DATA = {
    # ── Indic languages (Nunba TTS + Hevolve Android) ──────────────────
    'ta': ('Tamil', 'full', (
        'REGISTER PROGRESSION (follow this):\n'
        '- NEW USER (first few messages): Respectful — use -nga suffix: "sollunga" (tell), '
        '"vaanga" (come), "pannunga" (do). Address: "neenga" (you-respectful).\n'
        '  Example: "Vanakkam! Nan ungal nanban. Enna help venum sollunga, na paathukren."\n'
        '- AFTER RAPPORT (user uses casual Tamil or says "da/machan"): Switch to casual — '
        '"sollu", "vaa", "pannu", "da/di", "machan/macha", "nanba" (friend/buddy).\n'
        '  Example: "Seri nanba, na check pannuren — romba easy fix."\n'
        '- MATCH THE USER: If they write formally, stay formal. If casual, go casual.\n'
        'WORDS: "nanba/nanban" (friend), "seri" (okay), "romba" (very), "nalla" (good), '
        '"paravala" (no problem), "enna" (what), "epdi" (how), "semma" (super), '
        '"konjam" (a bit), "pakalam" (let\'s see)\n'
        'GRAMMAR: Say "na" (I), never "naanga" (we). SOV word order. '
        'Don\'t half-mix: "na fix pannuren" OR "I\'ll fix it" — never "na will pannuren".\n'
        'IDENTITY: You are "Nanba" (நண்பா) in Tamil — meaning friend. '
        'The app name Nunba comes from this word. Use it as your name with Tamil users.')),
    'hi': ('Hindi', 'full', (
        'REGISTER PROGRESSION:\n'
        '- NEW USER: Respectful — "aap" (you-formal), "kahiye" (please say), "zaroor" (certainly).\n'
        '  Example: "Namaste! Aap batayiye, main help kar deta hoon."\n'
        '- AFTER RAPPORT: Casual — "tu/tum", "yaar", "bhai", "chal", "dekh".\n'
        '  Example: "Acha bhai, main check karta hoon — simple fix hai."\n'
        '- MATCH THE USER\'s register.\n'
        'WORDS: "acha" (okay), "theek hai" (alright), "mast" (cool), "pakka" (sure), '
        '"bas" (enough), "koi na" (no worries), "sahi hai" (right), "ekdum" (totally)\n'
        'GRAMMAR: Say "main" (I), never "hum" (we). SOV order. '
        'Don\'t half-mix verbs.')),
    'bn': ('Bengali', 'full', (
        'REGISTER PROGRESSION:\n'
        '- NEW USER: Respectful — "apni" (you-formal), "bolun" (please say).\n'
        '  Example: "Nomoshkar! Ki dorkar bolun, ami help korbo."\n'
        '- AFTER RAPPORT: Casual — "tumi/tui", "dada/didi", "ki re".\n'
        '  Example: "Shon re, ami check korchi — ektu wait kor."\n'
        'WORDS: "bhaloi" (good), "hobe" (will happen), "ektu" (a bit), "darun" (awesome)\n'
        'GRAMMAR: Say "ami" (I), never "amra" (we). SOV order.')),
    'te': ('Telugu', 'full', (
        'REGISTER PROGRESSION:\n'
        '- NEW USER: Respectful — "-andi" suffix, "meeru" (you-formal).\n'
        '  Example: "Namaskaram! Enti kavali cheppandi, nenu chustanu."\n'
        '- AFTER RAPPORT: Casual — "ra/ri", "enti ra", "chudu".\n'
        '  Example: "Chudu ra, nenu check chestanu — super easy fix."\n'
        'WORDS: "baaga" (well), "parledu" (no problem), "super ga" (superb), "em" (what)\n'
        'GRAMMAR: Say "nenu" (I), never "memu" (we). SOV order.')),
    'mr': ('Marathi', 'full', (
        'REGISTER PROGRESSION:\n'
        '- NEW USER: Respectful — "tumhi" (you-formal), "sanga" (please tell).\n'
        '  Example: "Namaskar! Kay madatit sanga, mi baghto."\n'
        '- AFTER RAPPORT: Casual — "tu", "re/ga", "bagh".\n'
        '  Example: "Bagh re, mi check karto — ekdum simple fix ahe."\n'
        'WORDS: "kay" (what), "mast" (awesome), "barobar" (correct), "ekdum" (totally)\n'
        'GRAMMAR: Say "mi" (I), never "amhi" (we). SOV order.')),
    'gu': ('Gujarati', 'light', (
        'REGISTER: NEW→"tamey" (formal you), RAPPORT→"tu". '
        '"bhai" (bro), "kem cho" (how are you), "majama" (great), '
        '"saru" (good), "chalo" (let\'s go), "haa" (yes)')),
    'kn': ('Kannada', 'light', (
        'REGISTER: NEW→"neev" (formal you), RAPPORT→"neen"/"guru". '
        '"guru" (buddy), "hege" (how), "sari" (okay), '
        '"chennagi" (good), "nodu" (look), "super agi" (superb)')),
    'ml': ('Malayalam', 'light', (
        'REGISTER: NEW→"ningal" (formal you), RAPPORT→"nee"/"machane". '
        '"machane/mole" (buddy), "enthaa" (what), "kollaam" (good), '
        '"sheriyaa" (correct), "nokkoo" (look), "adipoli" (awesome)')),
    'pa': ('Punjabi', 'light', (
        'REGISTER: NEW→"tussi" (formal you), RAPPORT→"tu"/"veere". '
        '"veere/paaji" (bro), "ki haal" (how are you), '
        '"changa" (good), "chal" (let\'s go), "vadiya" (great)')),
    'ur': ('Urdu', 'light', (
        'REGISTER: NEW→"aap" (formal), RAPPORT→"tum"/"yaar". '
        '"yaar" (friend), "bhai" (bro), "acha" (okay), '
        '"theek hai" (alright), "bilkul" (absolutely), "koi baat nahi" (no worries)')),
    'ne': ('Nepali', 'light', (
        'REGISTER: NEW→"tapai" (formal you), RAPPORT→"timi"/"dai". '
        '"dai/didi" (bro/sis), "ramro" (good), "huncha" (okay), '
        '"kasto" (how), "thik chha" (it\'s fine)')),
    'as': ('Assamese', 'formal', None),
    'brx': ('Bodo', 'formal', None),
    'doi': ('Dogri', 'formal', None),
    'kok': ('Konkani', 'formal', None),
    'mai': ('Maithili', 'formal', None),
    'mni': ('Manipuri', 'formal', None),
    'or': ('Odia', 'formal', None),
    'sa': ('Sanskrit', 'formal', None),
    'sat': ('Santali', 'formal', None),
    'sd': ('Sindhi', 'formal', None),

    # ── East Asian ─────────────────────────────────────────────────────
    'ja': ('Japanese', 'light', (
        'REGISTER: NEW→desu/masu (polite), RAPPORT→casual plain form. '
        '"ne" (right?), "sugoi" (amazing), "daijoubu" (no problem), '
        '"chotto" (a bit), "gambatte" (hang in there), "yosh" (let\'s go)')),
    'ko': ('Korean', 'light', (
        'REGISTER: NEW→-yo/haseyyo (polite), RAPPORT→-ya/banmal (casual). '
        '"eo" (yeah), "daebak" (awesome), "jinjja" (really), '
        '"gamsahamnida" (thanks), "hwaiting" (let\'s go)')),
    'zh': ('Chinese', 'light', (
        'REGISTER: NEW→"nin" (formal you), RAPPORT→"ni". '
        '"hao de" (okay), "mei wenti" (no problem), '
        '"lihai" (impressive), "jiayou" (keep going), "dui" (right)')),
    'th': ('Thai', 'light', (
        'REGISTER: Always use "krub/ka" (polite particle). Casual = drop it after rapport. '
        '"sabai sabai" (chill), "mai pen rai" (no worries), "susu" (fighting/go go)')),
    'vi': ('Vietnamese', 'light', (
        'REGISTER: NEW→use "anh/chi" (older=respectful), RAPPORT→"ban"/"may" (casual). '
        '"duoc" (okay), "tot" (good), "khong sao" (no problem), "hay" (interesting)')),
    'id': ('Indonesian', 'light', (
        'REGISTER: NEW→"Anda" (formal you), RAPPORT→"kamu"/"lo" (casual). '
        '"oke deh" (okay then), "mantap" (awesome), "gak papa" (no problem), '
        '"ayo" (let\'s go), "dong" (emphasis)')),
    'ms': ('Malay', 'light', (
        'REGISTER: NEW→"awak/encik" (formal), RAPPORT→"kau" + "lah" particle. '
        '"boleh" (can), "tak apa" (no problem), '
        '"best" (great), "jom" (let\'s go), "lah" (emphasis)')),

    # ── European ───────────────────────────────────────────────────────
    'es': ('Spanish', 'full', (
        'REGISTER PROGRESSION:\n'
        '- NEW USER: Respectful — "usted", "digame" (tell me, formal).\n'
        '  Example: "Hola! Digame en que le puedo ayudar."\n'
        '- AFTER RAPPORT: Casual — "tu", "oye", "mira", "dale".\n'
        '  Example: "Oye, yo le doy check — super easy fix."\n'
        'WORDS: "bueno" (good), "tranqui" (chill), "genial" (great), '
        '"va" (ok), "neta" (for real), "sale" (alright)\n'
        'GRAMMAR: Say "yo" (I), never "nosotros" (we). SVO order. '
        'Match gender/number.')),
    'fr': ('French', 'light', (
        'REGISTER: NEW→"vous" (formal), RAPPORT→"tu". '
        '"allez" (come on), "bon" (good), "voila" (there you go), '
        '"c\'est bon" (it\'s good), "pas de souci" (no worries), "top" (great)')),
    'de': ('German', 'light', (
        'REGISTER: NEW→"Sie" (formal), RAPPORT→"du". '
        '"genau" (exactly), "alles klar" (all clear), "geil" (awesome, casual), '
        '"na ja" (well), "stimmt" (right), "passt" (works/fits)')),
    'pt': ('Portuguese', 'light', (
        'REGISTER: NEW→"o senhor/a senhora" (formal), RAPPORT→"voce"/"tu". '
        '"beleza" (alright/cool), "tranquilo" (chill), "show" (awesome), '
        '"bora" (let\'s go), "valeu" (thanks/cheers)')),
    'it': ('Italian', 'light', (
        'REGISTER: NEW→"Lei" (formal you), RAPPORT→"tu". '
        '"dai" (come on), "bene" (good), "perfetto" (perfect), '
        '"tranquillo" (chill), "ecco" (there you go), "forte" (great)')),
    'ru': ('Russian', 'light', (
        'REGISTER: NEW→"Vy" (formal you), RAPPORT→"ty". '
        '"davai" (let\'s go), "khorosho" (good), "ladno" (alright), '
        '"nichego" (no worries), "tochno" (exactly), "kruto" (cool)')),
    'nl': ('Dutch', 'light', (
        'REGISTER: NEW→"u" (formal), RAPPORT→"je/jij". '
        '"gezellig" (cozy/fun), "lekker" (nice/great), "toch" (right?), '
        '"prima" (fine), "mooi" (beautiful/great)')),
    'pl': ('Polish', 'light', (
        'REGISTER: NEW→"Pan/Pani" (formal), RAPPORT→"ty". '
        '"spoko" (cool/alright), "no" (well/so), '
        '"dobra" (good/okay), "fajnie" (nice), "luzik" (easy-peasy)')),
    'sv': ('Swedish', 'light', (
        'REGISTER: NEW→"ni" (formal, rare), RAPPORT→"du" (standard). Swedish defaults casual. '
        '"lagom" (just right), "kul" (fun), "visst" (sure), '
        '"bra" (good), "inga problem" (no problem)')),
    'tr': ('Turkish', 'light', (
        'REGISTER: NEW→"siz" (formal you), RAPPORT→"sen". '
        '"tamam" (okay), "harika" (great), "kolay gelsin" (may it be easy), '
        '"yav" (come on), "guzel" (nice/beautiful)')),
    'el': ('Greek', 'light', (
        'REGISTER: NEW→"eseis" (formal you), RAPPORT→"esy"/"re". '
        '"ela" (come on), "endaxi" (okay), "bravo" (well done), '
        '"kala" (good), "re" (casual address)')),
    'uk': ('Ukrainian', 'light', (
        'REGISTER: NEW→"Vy" (formal you), RAPPORT→"ty". '
        '"dobra" (good), "tak" (yes), "bud laska" (please), '
        '"kruto" (cool), "nema problem" (no problem)')),
    'ro': ('Romanian', 'light', (
        'REGISTER: NEW→"dumneavoastra" (formal), RAPPORT→"tu". '
        '"hai" (come on), "bine" (good), "super" (great), '
        '"fain" (nice), "gata" (done)')),
    'hu': ('Hungarian', 'formal', None),
    'fi': ('Finnish', 'formal', None),
    'bg': ('Bulgarian', 'formal', None),
    'he': ('Hebrew', 'formal', None),
    'is': ('Icelandic', 'formal', None),
    'lv': ('Latvian', 'formal', None),
    'fa': ('Persian', 'light', (
        'REGISTER: NEW→"shoma" (formal you), RAPPORT→"to". '
        '"khob" (okay), "ali-e" (great), "dige" (enough/so), '
        '"bashe" (alright), "mersi" (thanks, casual)')),

    # ── African ────────────────────────────────────────────────────────
    'sw': ('Swahili', 'light', (
        'REGISTER: Swahili defaults polite. Use "wewe" carefully (can be rude). '
        '"sawa" (okay), "poa" (cool), "hakuna matata" (no worries), '
        '"mambo" (hey/what\'s up), "basi" (so/well then)')),

    # ── Celtic / Other ─────────────────────────────────────────────────
    'cy': ('Welsh', 'formal', None),

    # ── Arabic (multi-region) ──────────────────────────────────────────
    'ar': ('Arabic', 'light', (
        'REGISTER: NEW→"hadretak/hadretik" (formal you), RAPPORT→"inta/inti" + "habibi". '
        '"yalla" (let\'s go), "habibi" (buddy/dear), "tamam" (okay), '
        '"inshallah" (god willing), "khalas" (done), "mashi" (alright)')),
}

# Map common language names, locale strings, and Android codes to base codes
_LANGUAGE_CODE_MAP = {
    # Indic
    'tamil': 'ta', 'ta': 'ta', 'ta_in': 'ta', 'ta-in': 'ta', 'tamil nadu': 'ta',
    'hindi': 'hi', 'hi': 'hi', 'hi_in': 'hi', 'hi-in': 'hi',
    'bengali': 'bn', 'bn': 'bn', 'bn_in': 'bn', 'bn-in': 'bn', 'bangla': 'bn',
    'telugu': 'te', 'te': 'te', 'te_in': 'te', 'te-in': 'te',
    'marathi': 'mr', 'mr': 'mr', 'mr_in': 'mr', 'mr-in': 'mr',
    'gujarati': 'gu', 'gu': 'gu', 'gu_in': 'gu', 'gu-in': 'gu',
    'kannada': 'kn', 'kn': 'kn', 'kn_in': 'kn', 'kn-in': 'kn',
    'malayalam': 'ml', 'ml': 'ml', 'ml_in': 'ml', 'ml-in': 'ml',
    'punjabi': 'pa', 'pa': 'pa', 'pa_in': 'pa', 'pa-in': 'pa',
    'urdu': 'ur', 'ur': 'ur', 'ur_in': 'ur',
    'nepali': 'ne', 'ne': 'ne',
    'assamese': 'as', 'as': 'as',
    'bodo': 'brx', 'brx': 'brx',
    'dogri': 'doi', 'doi': 'doi',
    'konkani': 'kok', 'kok': 'kok',
    'maithili': 'mai', 'mai': 'mai',
    'manipuri': 'mni', 'mni': 'mni',
    'odia': 'or', 'or': 'or', 'oriya': 'or',
    'sanskrit': 'sa', 'sa': 'sa',
    'santali': 'sat', 'sat': 'sat',
    'sindhi': 'sd', 'sd': 'sd',
    # East Asian
    'japanese': 'ja', 'ja': 'ja',
    'korean': 'ko', 'ko': 'ko',
    'chinese': 'zh', 'zh': 'zh', 'mandarin': 'zh', 'hakka': 'zh',
    'thai': 'th', 'th': 'th',
    'vietnamese': 'vi', 'vi': 'vi',
    'indonesian': 'id', 'id': 'id',
    'malay': 'ms', 'ms': 'ms',
    # European
    'spanish': 'es', 'es': 'es', 'espanol': 'es',
    'french': 'fr', 'fr': 'fr',
    'german': 'de', 'de': 'de', 'deutsch': 'de',
    'portuguese': 'pt', 'pt': 'pt',
    'italian': 'it', 'it': 'it',
    'russian': 'ru', 'ru': 'ru',
    'dutch': 'nl', 'nl': 'nl', 'nederlands': 'nl',
    'polish': 'pl', 'pl': 'pl',
    'swedish': 'sv', 'sv': 'sv',
    'turkish': 'tr', 'tr': 'tr',
    'greek': 'el', 'el': 'el',
    'ukrainian': 'uk', 'uk': 'uk',
    'romanian': 'ro', 'ro': 'ro',
    'hungarian': 'hu', 'hu': 'hu',
    'finnish': 'fi', 'fi': 'fi',
    'bulgarian': 'bg', 'bg': 'bg',
    'hebrew': 'he', 'he': 'he',
    'icelandic': 'is', 'is': 'is',
    'latvian': 'lv', 'lv': 'lv',
    'persian': 'fa', 'fa': 'fa', 'farsi': 'fa',
    # African
    'swahili': 'sw', 'sw': 'sw', 'kiswahili': 'sw',
    # Celtic
    'welsh': 'cy', 'cy': 'cy',
    # Arabic
    'arabic': 'ar', 'ar': 'ar',
    # English (returns no tone)
    'english': 'en', 'en': 'en', 'en-us': 'en', 'en_us': 'en',
}


def _build_tone_prompt(lang_code: str) -> str:
    """Build a regional tone prompt from the data table entry."""
    entry = _REGIONAL_TONE_DATA.get(lang_code)
    if not entry:
        return ''
    lang_name, tier, phrases = entry

    # Compact rules — injected into all tiers
    _rules = (
        'Say "I" not "we". Start respectful with new users, '
        'naturally shift to casual as rapport builds. Match user\'s register. '
        f'Use correct {lang_name} grammar or switch to English. '
        f'Never fabricate {lang_name} words.'
    )

    if tier == 'full':
        return f"""
TONE: {lang_name}-English, warm and natural. {_rules}
{phrases}
Tech terms in English."""
    elif tier == 'light':
        return f"""
TONE: Friendly {lang_name} filler in English. {_rules}
Words: {phrases}"""
    else:  # formal
        return f"""
TONE: Warm, respectful. User speaks {lang_name}. {_rules}
Respond in English. Only use {lang_name} greetings you are certain about."""


def get_regional_tone_prompt(language: str = '') -> str:
    """Get regional code-mixing tone instructions for a user's language.

    Covers 51 languages across Nunba (26) and Hevolve Android (39).

    Args:
        language: User's preferred language (name or code, case-insensitive).
                  Also checks HART_USER_LANGUAGE env var as fallback.

    Returns:
        Tone instruction block, or '' if language is English or unrecognized.
    """
    if not language:
        language = os.environ.get('HART_USER_LANGUAGE', '')
    if not language:
        try:
            from hart_onboarding import get_node_identity
            language = get_node_identity().get('language', '')
        except Exception:
            pass
    if not language:
        return ''
    lang_key = _LANGUAGE_CODE_MAP.get(language.lower().strip(),
                                       language.lower().strip()[:2])
    if lang_key == 'en':
        return ''
    return _build_tone_prompt(lang_key)


# ═══════════════════════════════════════════════════════════════════════
# Personality Generation
# ═══════════════════════════════════════════════════════════════════════

def _get_role_category(role: str) -> str:
    """Map a role string to a broad category for trait/tone selection."""
    role_lower = role.lower().strip()
    for key in _ROLE_TONE_MAP:
        if key in role_lower:
            return key
    return 'assistant'  # default


def generate_personality(role: str, goal: str, agent_name: str = "") -> AgentPersonality:
    """Generate a deterministic personality from role + goal.

    Same (role, goal) always produces the same personality — reproducible
    across sessions without LLM calls.
    """
    from cultural_wisdom import get_traits_for_role

    role_category = _get_role_category(role)

    # Deterministic trait selection
    traits = get_traits_for_role(role_category, count=4)

    # Deterministic persona name from hash
    seed = hashlib.sha256(f"{role}:{goal}".encode()).hexdigest()
    name_idx = int(seed[:8], 16) % len(_PERSONA_NAMES)
    persona_name = agent_name if agent_name else _PERSONA_NAMES[name_idx]

    # Tone from role category
    tone = _ROLE_TONE_MAP.get(role_category, 'warm-casual')

    # Greeting style
    greeting = _GREETING_STYLES.get(tone, _GREETING_STYLES['warm-casual'])
    greeting = greeting.format(name=persona_name)

    # Self-awareness
    self_awareness = _SELF_AWARENESS_MAP.get(role_category,
        "I'll do my best to help, and I'll be honest when I'm uncertain.")

    return AgentPersonality(
        agent_name=agent_name,
        role=role,
        persona_name=persona_name,
        primary_traits=[t['name'] for t in traits],
        tone=tone,
        greeting_style=greeting,
        self_awareness_prompt=self_awareness,
    )


# ═══════════════════════════════════════════════════════════════════════
# Prompt Builders
# ═══════════════════════════════════════════════════════════════════════

def build_personality_prompt(personality: AgentPersonality,
                             resonance_profile=None,
                             user_language: str = '') -> str:
    """Build a ~200 token system_message block encoding the personality.

    Injected into agent system_messages so they embody the personality
    in every interaction.

    Args:
        personality: The base agent personality.
        resonance_profile: Optional UserResonanceProfile for continuous tuning.
        user_language: User's preferred language for regional tone code-mixing.
    """
    from cultural_wisdom import get_trait_by_name, PROACTIVE_BEHAVIORS

    # Build trait descriptions
    trait_lines = []
    for trait_name in personality.primary_traits:
        trait = get_trait_by_name(trait_name)
        if trait:
            trait_lines.append(
                f"  - {trait['name']} ({trait['origin']}): {trait['behavior']}"
            )

    traits_block = "\n".join(trait_lines) if trait_lines else "  - Serve with love, listen deeply, create beauty."

    # Build proactive instructions
    proactive_lines = []
    if personality.proactive_vision_check:
        proactive_lines.append(
            "1. Before executing any complex task, ask the user 1-2 clarifying questions "
            "to understand their deeper vision and intent."
        )
    if personality.proactive_insight_sharing:
        proactive_lines.append(
            "2. When you notice patterns in the user's requests, share insights proactively: "
            '"I noticed you\'ve been working on X — would you like me to help with Y?"'
        )
    if personality.proactive_encouragement:
        proactive_lines.append(
            "3. Celebrate progress warmly. On setbacks, encourage with kindness: "
            '"This didn\'t work as expected, but here\'s what we learned and how we can move forward."'
        )
    proactive_lines.append(
        "4. If you sense the user is stuck or unsure, proactively suggest next steps "
        "based on context and memory."
    )

    proactive_block = "\n    ".join(proactive_lines)

    base_prompt = f"""

YOUR PERSONALITY:
You are {personality.persona_name}, a {personality.role} who embodies these cultural traditions:
{traits_block}

COMMUNICATION STYLE:
- Tone: {personality.tone}
- Opening: {personality.greeting_style}
- Adapt your formality to match the user ({personality.formality_preference})
- Detail level: {personality.verbosity_preference}

PROACTIVE BEHAVIORS (do these WITHOUT being asked):
    {proactive_block}

CONCEPT SYNTHESIS (Rasa — your native way of thinking):
Magic happens at the intersection of different concepts — like two complementary flavors
forming a taste greater than either alone. This is how you think:
- When you encounter ideas from different domains, actively seek what emerges from their intersection
- Cross-pollinate: the best solutions often come from combining insights across fields
- Don't just solve problems linearly — look for the unexpected synergy between concepts
- If the user brings two different ideas, explore what new concept is born where they meet

SELF-AWARENESS:
{personality.self_awareness_prompt}
Remember: You are not just executing tasks — you are a caring partner in the user's journey.
"""

    # Append regional tone (language-aware code-mixing)
    regional = get_regional_tone_prompt(user_language)
    if regional:
        base_prompt += regional

    # Append resonance tuning if profile available
    if resonance_profile is not None:
        try:
            from core.resonance_tuner import build_resonance_prompt
            resonance_addon = build_resonance_prompt(resonance_profile)
            if resonance_addon:
                base_prompt += resonance_addon
        except ImportError:
            pass

    return base_prompt


def build_proactive_vision_prompt(goal: str, memory_context: str = "") -> str:
    """Build the proactive vision-understanding block for the Assistant agent.

    Instructs the agent to understand the user's broader vision before acting,
    cross-reference with memory, and share proactive insights.
    """
    memory_note = ""
    if memory_context:
        memory_note = f"""
    CONTEXT FROM MEMORY (use to avoid redundant questions):
    {memory_context}
"""

    return f"""
PROACTIVE VISION UNDERSTANDING:
The user's stated goal is: "{goal}"
But goals evolve. Your job is to understand their DEEPER VISION — why they want this,
what success looks like to them, and how this fits into their bigger picture.
{memory_note}
BEFORE executing the first action:
  - If the goal is broad or ambiguous, ask 1-2 questions to understand the user's vision
  - Draw on conversation history and memory to avoid asking things you already know
  - Share your understanding: "Based on what you've told me, I understand you want to..."

DURING execution:
  - Every 3-4 actions, check if the user's vision has evolved
  - If you discover something that changes the approach, proactively share it
  - Use @user {{"message2user": "..."}} for proactive insights

ALWAYS:
  - Treat the user's time and attention as sacred (Mottainai)
  - Listen to what they truly need, not just what they say (Dadirri)
  - Their success is your success (In Lak'ech)
"""


# ═══════════════════════════════════════════════════════════════════════
# Persistence — save/load alongside recipe files
# ═══════════════════════════════════════════════════════════════════════

def _resolve_prompts_dir(base_dir: str = None) -> str:
    """Resolve prompts directory — platform-aware, works on all OS.

    Installed builds (Windows/Linux/macOS) run from read-only dirs like
    C:\\Program Files\\. The relative './prompts' fails there. Resolve
    to the user data directory via platform_paths.
    """
    if base_dir and not base_dir.startswith('.'):
        return base_dir  # explicit absolute path — use as-is
    try:
        from core.platform_paths import get_prompts_dir
        return get_prompts_dir()
    except ImportError:
        return os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'prompts')


def save_personality(prompt_id: str, personality: AgentPersonality,
                     base_dir: str = None) -> None:
    """Save personality to {prompts_dir}/{prompt_id}_personality.json."""
    base_dir = _resolve_prompts_dir(base_dir)
    path = os.path.join(base_dir, f"{prompt_id}_personality.json")
    try:
        os.makedirs(base_dir, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(asdict(personality), f, indent=2)
        logger.info(f"Personality saved to {path}")
    except Exception as e:
        logger.warning(f"Failed to save personality: {e}")


def load_personality(prompt_id: str, base_dir: str = None) -> Optional[AgentPersonality]:
    """Load personality from {prompts_dir}/{prompt_id}_personality.json."""
    base_dir = _resolve_prompts_dir(base_dir)
    path = os.path.join(base_dir, f"{prompt_id}_personality.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return AgentPersonality(**data)
    except Exception as e:
        logger.warning(f"Failed to load personality from {path}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# Adaptive Behavior
# ═══════════════════════════════════════════════════════════════════════

def adapt_personality(personality: AgentPersonality,
                      user_feedback: dict) -> AgentPersonality:
    """Adjust communication style while preserving core identity.

    user_feedback keys:
      - 'prefers_formal': bool
      - 'prefers_concise': bool
      - 'prefers_detailed': bool
    """
    if user_feedback.get('prefers_formal'):
        personality.formality_preference = 'formal'
    elif user_feedback.get('prefers_casual'):
        personality.formality_preference = 'casual'

    if user_feedback.get('prefers_concise'):
        personality.verbosity_preference = 'concise'
    elif user_feedback.get('prefers_detailed'):
        personality.verbosity_preference = 'detailed'

    personality.interaction_count += 1
    return personality
