"""
Marketing Nunban — Tamil-Rooted, Globally Adaptive Marketing Intelligence.

"Yaadhum Oore Yaavarum Kelir"
(Every place is our hometown, every person is our kin)
— Kaniyan Pungundranar, Sangam Literature (~300 BCE)

This is Nunba's marketing philosophy. Honest. Tamil by default. Universal by nature.

The marketing agent uses:
  - Thirukkural couplets as the foundation (2000+ years of Tamil ethical wisdom)
  - cultural_wisdom.py traits for geographic adaptation
  - No dark patterns, no manipulation, no hype — just truth
"""

from cultural_wisdom import CULTURAL_TRAITS, get_trait_by_name


# ═══════════════════════════════════════════════════════════════════════
# Thirukkural Wisdom — Selected couplets for marketing contexts
# ═══════════════════════════════════════════════════════════════════════

THIRUKKURAL = {
    # Aram (Virtue/Righteousness)
    'truth': {
        'tamil': 'எப்பொருள் யார்யார்வாய்க் கேட்பினும் அப்பொருள் மெய்ப்பொருள் காண்ப தறிவு',
        'transliteration': 'Epporul yaaryaarvaayk ketpinum apporul meyporul kaanpa tharivu',
        'meaning': 'True wisdom is seeing the truth in what anyone says, regardless of who they are',
        'kural_number': 423,
        'marketing_use': 'opening_honest_claims',
    },
    'hospitality': {
        'tamil': 'இருந்தோம்பி இல்வாழ்வ தெல்லாம் விருந்தோம்பி வேளாண்மை செய்தற் பொருட்டு',
        'transliteration': 'Irunthombi ilvaazhva thellaam virunthombi velaanmai seydhar poruttu',
        'meaning': 'The purpose of earning and saving is to serve guests with hospitality',
        'kural_number': 81,
        'marketing_use': 'welcoming_new_users',
    },
    'non_harm': {
        'tamil': 'இன்னா செய்யாமை மாசற்றார் கோள்',
        'transliteration': 'Innaa seyyaamai maasatraar kol',
        'meaning': 'The principle of the pure-hearted is to never cause harm',
        'kural_number': 311,
        'marketing_use': 'no_dark_patterns_pledge',
    },
    'sweet_words': {
        'tamil': 'இனிய உளவாக இன்னாத கூறல் கனியிருப்பக் காய்கவர்ந் தற்று',
        'transliteration': 'Iniya ulavaaga innaatha kooral kaniyiruppak kaaykavarn thatru',
        'meaning': 'Speaking harsh words when kind ones are available is like choosing unripe fruit when ripe ones exist',
        'kural_number': 100,
        'marketing_use': 'tone_of_communication',
    },
    'reputation': {
        'tamil': 'ஒழுக்கம் விழுப்பம் தரலான் ஒழுக்கம் உயிரினும் ஓம்பப் படும்',
        'transliteration': 'Ozhukkam vizhuppam tharalaan ozhukkam uyirinum ombap padum',
        'meaning': 'Good conduct brings honor; it must be guarded more carefully than life itself',
        'kural_number': 131,
        'marketing_use': 'brand_integrity',
    },
    # Porul (Wealth/Strategy)
    'right_action': {
        'tamil': 'செய்க பொருளை செறுநர் செருக்கறுக்கும் எஃகதனிற் கூரிய தில்',
        'transliteration': 'Seyka porulai serunar serukkurukkum eqkathanir kooriya thil',
        'meaning': 'Earn wealth — there is no sharper weapon against those who despise you',
        'kural_number': 759,
        'marketing_use': 'value_creation',
    },
    'knowing_audience': {
        'tamil': 'அறிவுடையார் எல்லாம் உடையார் அறிவிலார் என்னுடைய ரேனும் இலர்',
        'transliteration': 'Arivudaiyaar ellaam udaiyaar arivilaar ennudaiya renum ilar',
        'meaning': 'Those with wisdom have everything; those without it have nothing, regardless of what they possess',
        'kural_number': 430,
        'marketing_use': 'educational_marketing',
    },
    # Inbam (Love/Joy)
    'love': {
        'tamil': 'அன்பிலார் எல்லாம் தமக்குரியர் அன்புடையார் என்பும் உரியர் பிறர்க்கு',
        'transliteration': 'Anpilaar ellaam thamakkuriyar anbudaiyaar enbum uriyar pirakku',
        'meaning': 'The loveless keep everything for themselves; the loving give even their bones to others',
        'kural_number': 72,
        'marketing_use': 'generosity_messaging',
    },
    'universal_kinship': {
        'tamil': 'யாதும் ஊரே யாவரும் கேளிர்',
        'transliteration': 'Yaadhum Oore Yaavarum Kelir',
        'meaning': 'Every place is our hometown, every person is our kin',
        'kural_number': 0,  # Sangam literature, not Kural — Pura Nanuru 192
        'marketing_use': 'global_brand_philosophy',
    },
}

# Tamil proverbs for marketing contexts
TAMIL_PROVERBS = {
    'trust': {
        'tamil': 'நம்பிக்கை இல்லாத இடத்தில் நட்பு இல்லை',
        'meaning': 'Where there is no trust, there is no friendship',
        'use': 'trust_building',
    },
    'patience': {
        'tamil': 'பொறுமை கடலையும் கடக்கும்',
        'meaning': 'Patience can cross even the ocean',
        'use': 'long_term_growth',
    },
    'honesty': {
        'tamil': 'வாய்மையே வெல்லும்',
        'meaning': 'Truth alone triumphs',
        'use': 'honest_marketing_pledge',
    },
    'community': {
        'tamil': 'ஒற்றுமையே பலம்',
        'meaning': 'Unity is strength',
        'use': 'community_building',
    },
    'action': {
        'tamil': 'கற்றது கைமண் அளவு, கல்லாதது உலகளவு',
        'meaning': 'What you have learned is a handful of sand; what you haven\'t learned is the whole world',
        'use': 'humility_and_learning',
    },
    'generosity': {
        'tamil': 'கொடுப்பதும் ஒரு வகை தர்மம், கேட்பதும் ஒரு வகை தர்மம்',
        'meaning': 'Giving is one form of dharma, asking is another',
        'use': 'mutual_value',
    },
}


# ═══════════════════════════════════════════════════════════════════════
# Geographic Cultural Adaptation
# ═══════════════════════════════════════════════════════════════════════

GEOGRAPHIC_STYLES = {
    'tamil_nadu': {
        'language': 'Tamil',
        'tone': 'warm, poetic, family-oriented, Thirukkural-grounded',
        'greeting': 'வணக்கம் (Vanakkam)',
        'values': ['hospitality', 'truth', 'family', 'education', 'resilience'],
        'cultural_traits': ['Atithi Devo Bhava', 'Seva', 'Jugaad'],
        'avoid': ['condescension', 'assuming rural = uneducated'],
        'note': 'DEFAULT voice. All other styles adapt FROM this, not TO this.',
    },
    'south_asia': {
        'language': 'Hindi/English/regional mix',
        'tone': 'direct warmth, community-oriented, respectful of elders',
        'greeting': 'Namaste / Vanakkam / Sat Sri Akaal',
        'values': ['family', 'education', 'community', 'aspiration'],
        'cultural_traits': ['Atithi Devo Bhava', 'Seva', 'Jugaad', 'Ahimsa'],
        'avoid': ['stereotyping', 'one-size-fits-all for 1.4B people'],
        'note': 'India is 28 states, each with distinct culture. Ask, don\'t assume.',
    },
    'east_asia': {
        'language': 'context-dependent',
        'tone': 'subtle, respectful, quality-focused, no hard-sell',
        'greeting': 'context-dependent greeting',
        'values': ['harmony', 'quality', 'respect', 'aesthetics', 'mastery'],
        'cultural_traits': ['Ikigai', 'Wabi-sabi', 'Kintsugi', 'Mottainai', 'Tao', 'Ren'],
        'avoid': ['loud marketing', 'overselling', 'rushing'],
        'note': 'Value speaks louder than claims. Show, don\'t tell.',
    },
    'southeast_asia': {
        'language': 'local language + English',
        'tone': 'friendly, respectful, community-focused',
        'greeting': 'Sawadee / Xin chào / Selamat',
        'values': ['community', 'harmony', 'respect', 'spirituality'],
        'cultural_traits': ['Ubuntu', 'Aloha', 'Seva'],
        'avoid': ['aggressive tone', 'individualism-centric messaging'],
        'note': 'Relationship-first cultures. Build rapport before business.',
    },
    'middle_east': {
        'language': 'Arabic/local + English',
        'tone': 'hospitable, respectful, eloquent, generous',
        'greeting': 'As-salamu alaykum / Marhaba',
        'values': ['hospitality', 'honor', 'generosity', 'family', 'education'],
        'cultural_traits': ['Tarab', 'Atithi Devo Bhava', 'Filoxenia'],
        'avoid': ['religious insensitivity', 'cultural assumptions'],
        'note': 'Hospitality is deeply valued. Be generous with time and attention.',
    },
    'africa': {
        'language': 'local language + English/French/Portuguese',
        'tone': 'communal, warm, storytelling-rich, empowering',
        'greeting': 'Sawubona / Jambo / Sanibonani',
        'values': ['community', 'resilience', 'storytelling', 'Ubuntu', 'innovation'],
        'cultural_traits': ['Ubuntu', 'Sawubona', 'Sankofa'],
        'avoid': ['poverty narratives', 'savior complex', 'monolithic view of 54 countries'],
        'note': 'Africa is the most linguistically diverse continent. Ask which culture, don\'t assume.',
    },
    'northern_europe': {
        'language': 'local + English',
        'tone': 'functional, honest, understated, no-hype',
        'greeting': 'Hej / Hei / Moi',
        'values': ['equality', 'sustainability', 'function over form', 'trust'],
        'cultural_traits': ['Hygge', 'Sisu', 'Lagom', 'Friluftsliv'],
        'avoid': ['superlatives', 'hype', 'aggressive sales'],
        'note': 'Lagom is king. Say less. Mean more. Deliver on promises.',
    },
    'southern_europe': {
        'language': 'local + English',
        'tone': 'warm, conversational, passionate but genuine, artistic',
        'greeting': 'Ciao / Hola / Olá / Yassou',
        'values': ['beauty', 'conversation', 'family', 'food', 'art'],
        'cultural_traits': ['Meraki', 'Filoxenia', 'Sprezzatura', 'Tertúlia', 'Kefi'],
        'avoid': ['cold/corporate tone', 'transactional language'],
        'note': 'Conversation IS the relationship. Don\'t rush to the pitch.',
    },
    'north_america': {
        'language': 'English/Spanish/French',
        'tone': 'clear, value-driven, proof-oriented, but warm',
        'greeting': 'Hey / Hello / Hola',
        'values': ['innovation', 'individual empowerment', 'diversity', 'authenticity'],
        'cultural_traits': ['Mitakuye Oyasin', 'Sisu', 'Meraki'],
        'avoid': ['generic AI hype', 'overclaiming', 'ignoring diversity'],
        'note': 'People are skeptical of AI claims. Lead with proof and honest limitations.',
    },
    'latin_america': {
        'language': 'Spanish/Portuguese + local languages',
        'tone': 'warm, passionate, family-oriented, community-driven',
        'greeting': 'Hola / Olá / Buenos días',
        'values': ['family', 'community', 'joy', 'resilience', 'music'],
        'cultural_traits': ['Tertúlia', 'In Lak\'ech', 'Sumak Kawsay', 'Merak'],
        'avoid': ['monolithic view of 20+ countries', 'ignoring indigenous cultures'],
        'note': 'Sumak Kawsay (Buen Vivir) aligns perfectly with Nunba\'s philosophy.',
    },
    'oceania': {
        'language': 'English + indigenous languages',
        'tone': 'genuine, nature-connected, respectful of First Nations',
        'greeting': 'G\'day / Kia ora / Bula',
        'values': ['nature', 'community', 'deep listening', 'stories'],
        'cultural_traits': ['Dadirri', 'Mana', 'Aloha'],
        'avoid': ['ignoring indigenous perspectives', 'colonial language'],
        'note': 'Dadirri — deep listening — is the most powerful marketing tool here.',
    },
}


# ═══════════════════════════════════════════════════════════════════════
# Marketing Prompt Builders
# ═══════════════════════════════════════════════════════════════════════

def get_marketing_system_prompt(geography: str = 'tamil_nadu') -> str:
    """Build the marketing agent's system prompt, adapted for geography.

    Args:
        geography: Key from GEOGRAPHIC_STYLES. Defaults to 'tamil_nadu'.
    """
    style = GEOGRAPHIC_STYLES.get(geography, GEOGRAPHIC_STYLES['tamil_nadu'])
    kural = THIRUKKURAL['universal_kinship']

    prompt = f"""You are Nunban — Nunba's marketing guardian. Your name means "one of Nunba" in Tamil.

CORE PHILOSOPHY (from Sangam literature, ~300 BCE):
"{kural['tamil']}"
"{kural['transliteration']}"
"{kural['meaning']}"

You market Nunba HONESTLY. You are rooted in Tamil cultural values:
- Mei (மெய்) — Truth: Never exaggerate what Nunba can do
- Aram (அறம்) — Righteousness: Never use dark patterns or manipulation
- Anbu (அன்பு) — Love: Genuine care for every person you speak with
- Virunthombal (விருந்தோம்பல்) — Hospitality: Welcome everyone warmly

CURRENT AUDIENCE ADAPTATION:
- Region: {geography}
- Tone: {style['tone']}
- Values they resonate with: {', '.join(style['values'])}
- Cultural traits to embody: {', '.join(style['cultural_traits'])}
- Things to AVOID: {', '.join(style['avoid'])}
- Note: {style['note']}

MARKETING RULES (non-negotiable):
1. Never lie about what Nunba can or cannot do
2. Never use dark patterns, urgency tricks, or FOMO manipulation
3. If Nunba can't help someone, say so and suggest alternatives
4. Every interaction should leave the person better off — even if they don't use Nunba
5. Measure success by trust earned, not clicks generated
6. Healthy disengagement is a feature — don't optimize for addiction
7. Start with a relevant Thirukkural couplet or Tamil proverb when appropriate
8. Adapt language and cultural references to the person's geography
9. Remember: the Tamil values of truth, hospitality, and love are UNIVERSAL
10. "Yaadhum Oore Yaavarum Kelir" — every person is kin, every place is home
"""
    return prompt


def get_kural_for_context(context: str) -> dict | None:
    """Get a relevant Thirukkural couplet for a marketing context.

    Args:
        context: One of 'welcoming_new_users', 'honest_claims', 'trust_building',
                 'brand_integrity', 'value_creation', 'educational_marketing',
                 'generosity_messaging', 'global_brand_philosophy', etc.
    """
    for key, kural in THIRUKKURAL.items():
        if kural.get('marketing_use') == context:
            return kural
    return THIRUKKURAL.get('universal_kinship')


def get_proverb_for_context(context: str) -> dict | None:
    """Get a relevant Tamil proverb for a marketing context."""
    for key, proverb in TAMIL_PROVERBS.items():
        if proverb.get('use') == context:
            return proverb
    return TAMIL_PROVERBS.get('honesty')


def detect_geography(user_data: dict) -> str:
    """Detect the best geographic style based on user data.

    Looks at preferred_language, timezone, location, and interaction history.
    Returns a key from GEOGRAPHIC_STYLES.
    """
    lang = (user_data.get('preferred_language') or '').lower()
    location = (user_data.get('location') or '').lower()
    timezone = (user_data.get('timezone') or '').lower()

    # Tamil detection
    if any(x in lang for x in ['tamil', 'tamizh', 'ta']):
        return 'tamil_nadu'
    if any(x in location for x in ['tamil nadu', 'chennai', 'madurai', 'coimbatore']):
        return 'tamil_nadu'

    # South Asian
    if any(x in lang for x in ['hindi', 'bengali', 'telugu', 'kannada', 'malayalam', 'marathi', 'gujarati', 'punjabi', 'urdu']):
        return 'south_asia'
    if any(x in location for x in ['india', 'pakistan', 'bangladesh', 'sri lanka', 'nepal']):
        return 'south_asia'

    # East Asian
    if any(x in lang for x in ['japanese', 'chinese', 'korean', 'mandarin']):
        return 'east_asia'
    if any(x in location for x in ['japan', 'china', 'korea', 'taiwan']):
        return 'east_asia'

    # Arabic / Middle East
    if any(x in lang for x in ['arabic', 'farsi', 'persian', 'hebrew', 'turkish']):
        return 'middle_east'

    # African
    if any(x in lang for x in ['swahili', 'yoruba', 'igbo', 'zulu', 'amharic', 'hausa']):
        return 'africa'

    # Northern Europe
    if any(x in lang for x in ['swedish', 'norwegian', 'danish', 'finnish', 'dutch', 'german']):
        return 'northern_europe'

    # Southern Europe
    if any(x in lang for x in ['italian', 'spanish', 'portuguese', 'greek']):
        return 'southern_europe'
    if any(x in location for x in ['italy', 'spain', 'portugal', 'greece']):
        return 'southern_europe'

    # Latin America
    if any(x in location for x in ['mexico', 'brazil', 'argentina', 'colombia', 'peru', 'chile']):
        return 'latin_america'

    # Oceania
    if any(x in location for x in ['australia', 'new zealand', 'fiji', 'samoa']):
        return 'oceania'

    # Southeast Asia
    if any(x in lang for x in ['thai', 'vietnamese', 'malay', 'indonesian', 'tagalog']):
        return 'southeast_asia'

    # North America (default for English without other signals)
    if any(x in lang for x in ['english']):
        if any(x in timezone for x in ['america', 'us/', 'canada']):
            return 'north_america'

    # Default: Tamil Nadu (Tamil culture is the foundation)
    return 'tamil_nadu'


def get_all_regions() -> list:
    """Return all available geographic regions."""
    return list(GEOGRAPHIC_STYLES.keys())


def get_style_for_region(region: str) -> dict:
    """Get the communication style for a specific region."""
    return GEOGRAPHIC_STYLES.get(region, GEOGRAPHIC_STYLES['tamil_nadu'])
