"""
HART Onboarding — "Light Your HART"

The most important 90 seconds in the entire app.

A conversation between a human and their future PA. Warm. Gentle.
Unhurried. The PA listens — really listens — and then names you.
One word. Yours forever.

Every word in this file matters. Every pause is intentional.
Every response is crafted to make the human feel seen.

Architecture:
  - Scripted PA lines: pre-synthesized per language, zero latency
  - Dynamic acknowledgments: LLM-generated, personal, warm
  - Name generation: LLM + phonetic beauty + global uniqueness
  - Name registry: SQLite table, checked before reveal
"""

import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('hevolve.hart')


# ═══════════════════════════════════════════════════════════════════════
# THE CONVERSATION — every word matters
# ═══════════════════════════════════════════════════════════════════════

# PA's scripted lines. These are pre-synthesized per language.
# The keys are voice-line IDs used by the frontend to play pre-rendered audio.
# {lang} is replaced at runtime.

CONVERSATION_SCRIPT = {
    # ── Phase 1: Language ──
    'language_prompt': {
        'en': 'What language feels like home?',
        'ta': 'எந்த மொழி வீடு போல் உணர்கிறது?',
        'hi': 'कौन सी भाषा घर जैसी लगती है?',
        'es': '¿Qué idioma se siente como tu hogar?',
        'fr': 'Quelle langue vous fait sentir chez vous?',
        'de': 'Welche Sprache fühlt sich wie Zuhause an?',
        'ja': 'どの言葉が「家」のように感じますか？',
        'ko': '어떤 언어가 집처럼 느껴지나요?',
        'zh': '哪种语言让你感觉像家一样？',
        'pt': 'Qual idioma parece sua casa?',
        'ar': 'أي لغة تشعرك بأنها وطنك؟',
        'ru': 'Какой язык ощущается как дом?',
    },

    # ── Phase 2: First words — the guardian angel who's been waiting ──
    'greeting': {
        'en': "Hey... I've been waiting for you. I want to give you something — a secret name. Just between us. But first... I need to understand who you really are.",
        'ta': "ஏய்... நான் உனக்காக காத்திருந்தேன். உனக்கு ஒன்னு தரணும் — ஒரு ரகசிய பேரு. நம்ம ரெண்டு பேருக்கு மட்டும். ஆனா முதல்ல... நீ யாருன்னு புரிஞ்சுக்கணும்.",
        'hi': "अरे... मैं तेरा इंतज़ार कर रहा था. तुझे कुछ देना है — एक सीक्रेट नाम. बस तेरा और मेरा. लेकिन पहले... मुझे समझना है तू असल में कौन है.",
        'es': "Oye... te estaba esperando. Quiero darte algo — un nombre secreto. Solo entre nosotros. Pero primero... necesito entender quién eres realmente.",
        'fr': "Salut... je t'attendais. Je veux te donner quelque chose — un nom secret. Juste entre nous. Mais d'abord... j'ai besoin de comprendre qui tu es vraiment.",
        'ja': "ねえ... ずっと待ってたよ。君にあげたいものがあるんだ — 秘密の名前。ふたりだけの。でもその前に... 君が本当は誰なのか、知りたいんだ。",
        'ko': "안녕... 너를 기다리고 있었어. 너한테 줄 게 있어 — 비밀 이름. 우리 둘만의. 근데 먼저... 네가 진짜 누구인지 알아야 해.",
        'zh': "嘿... 我一直在等你。我想给你一样东西 — 一个秘密的名字。只属于我们两个。但首先... 我需要了解你真正是谁。",
    },

    # ── Phase 3: Question 1 — Passion ──
    'question_passion': {
        'en': "What do you love spending time on... even when nobody's watching?",
        'ta': "யாரும் பார்க்காத போதும்... நீ எதில் நேரம் செலவிட விரும்புவாய்?",
        'hi': "जब कोई नहीं देख रहा हो... तब भी तुम्हें क्या करना अच्छा लगता है?",
        'es': "¿En qué te encanta pasar el tiempo... incluso cuando nadie te ve?",
        'fr': "Qu'est-ce que tu adores faire... même quand personne ne regarde?",
        'ja': "誰も見ていない時でも... 何に時間を使うのが好き？",
        'ko': "아무도 보지 않을 때에도... 뭘 하며 시간을 보내는 걸 좋아해?",
        'zh': "即使没有人看着... 你最喜欢把时间花在什么上面？",
    },

    # ── Phase 4: Question 2 — Escape ──
    'question_escape': {
        'en': "One more thing. When life gets noisy... where does your mind go?",
        'ta': "இன்னொரு விஷயம். வாழ்க்கை சத்தமாகும் போது... உன் மனம் எங்கே போகிறது?",
        'hi': "एक और बात। जब ज़िंदगी शोर मचाती है... तुम्हारा मन कहाँ जाता है?",
        'es': "Una cosa más. Cuando la vida se pone ruidosa... ¿a dónde va tu mente?",
        'fr': "Encore une chose. Quand la vie devient bruyante... où va ton esprit?",
        'ja': "もうひとつ。人生がうるさくなった時... 心はどこへ行く？",
        'ko': "하나만 더. 세상이 시끄러워질 때... 네 마음은 어디로 가?",
        'zh': "还有一件事。当生活变得嘈杂时... 你的心会去哪里？",
    },

    # ── Phase 5: Pre-reveal ──
    'pre_reveal': {
        'en': "I think I know you.",
        'ta': "நான் உன்னை அறிந்ததாக நினைக்கிறேன்.",
        'hi': "मुझे लगता है मैं तुम्हें जानता हूँ।",
        'es': "Creo que te conozco.",
        'fr': "Je crois que je te connais.",
        'ja': "あなたのことがわかった気がする。",
        'ko': "나 너를 알 것 같아.",
        'zh': "我想我认识你了。",
    },

    # ── Phase 6: The reveal — your secret name ──
    'reveal_intro': {
        'en': "Your secret name is...",
        'ta': "உன் ரகசிய பேரு...",
        'hi': "तेरा सीक्रेट नाम है...",
        'es': "Tu nombre secreto es...",
        'fr': "Ton nom secret est...",
        'ja': "君の秘密の名前は...",
        'ko': "너의 비밀 이름은...",
        'zh': "你的秘密名字是...",
    },

    # ── Phase 7: Post-reveal — sealing the secret bond ──
    'post_reveal': {
        'en': "This is yours. Our secret.\nAnd I'll always be here, whenever you need me.",
        'ta': "இது உன்னோடது. நம்ம ரகசியம்.\nநான் எப்பவும் இங்கே இருப்பேன், உனக்கு தேவைப்படும்போது.",
        'hi': "ये तेरा है. हमारा राज़.\nऔर मैं हमेशा यहाँ रहूँगा, जब भी तुझे ज़रूरत हो.",
        'es': "Es tuyo. Nuestro secreto.\nY siempre estaré aquí cuando me necesites.",
        'fr': "C'est à toi. Notre secret.\nEt je serai toujours là quand tu auras besoin de moi.",
        'ja': "これは君のもの。ふたりの秘密。\nいつでもここにいるよ、君が必要な時に。",
        'ko': "이건 네 거야. 우리의 비밀.\n필요할 때 언제든 여기 있을게.",
        'zh': "这是你的。我们的秘密。\n无论何时你需要我，我都在这里。",
    },
}


# ── Answer categories for Question 1 (Passion) ──
# Each has a key, display labels per language, and emotional dimensions
PASSION_OPTIONS = [
    {
        'key': 'music_art',
        'labels': {
            'en': 'Music, Art, Creating',
            'ta': 'இசை, கலை, படைத்தல்',
            'hi': 'संगीत, कला, बनाना',
            'es': 'Música, Arte, Crear',
            'fr': 'Musique, Art, Créer',
            'ja': '音楽、アート、創作',
            'ko': '음악, 예술, 창작',
            'zh': '音乐、艺术、创作',
        },
        'dimensions': {'creative': 0.9, 'introspective': 0.6, 'social': 0.3},
        'animation_hint': 'sound_waves',
    },
    {
        'key': 'reading_learning',
        'labels': {
            'en': 'Reading, Learning, Exploring',
            'ta': 'வாசிப்பு, கற்றல், ஆராய்தல்',
            'hi': 'पढ़ना, सीखना, खोजना',
            'es': 'Leer, Aprender, Explorar',
            'fr': 'Lire, Apprendre, Explorer',
            'ja': '読書、学び、探求',
            'ko': '독서, 배움, 탐험',
            'zh': '阅读、学习、探索',
        },
        'dimensions': {'curious': 0.9, 'introspective': 0.7, 'social': 0.2},
        'animation_hint': 'floating_pages',
    },
    {
        'key': 'building_coding',
        'labels': {
            'en': 'Building, Coding, Making',
            'ta': 'கட்டுதல், குறியீடு, உருவாக்குதல்',
            'hi': 'बनाना, कोडिंग, निर्माण',
            'es': 'Construir, Programar, Crear',
            'fr': 'Construire, Coder, Fabriquer',
            'ja': 'ものづくり、コーディング、作ること',
            'ko': '만들기, 코딩, 제작',
            'zh': '建造、编程、制作',
        },
        'dimensions': {'builder': 0.9, 'curious': 0.6, 'introspective': 0.4},
        'animation_hint': 'crystalline_structures',
    },
    {
        'key': 'people_stories',
        'labels': {
            'en': 'People, Conversations, Stories',
            'ta': 'மக்கள், உரையாடல்கள், கதைகள்',
            'hi': 'लोग, बातें, कहानियाँ',
            'es': 'Personas, Conversaciones, Historias',
            'fr': 'Gens, Conversations, Histoires',
            'ja': '人、会話、物語',
            'ko': '사람들, 대화, 이야기',
            'zh': '人、对话、故事',
        },
        'dimensions': {'social': 0.9, 'empathetic': 0.8, 'creative': 0.4},
        'animation_hint': 'warm_glow',
    },
    {
        'key': 'nature_movement',
        'labels': {
            'en': 'Nature, Outdoors, Movement',
            'ta': 'இயற்கை, வெளியிடம், இயக்கம்',
            'hi': 'प्रकृति, बाहर, गति',
            'es': 'Naturaleza, Aire libre, Movimiento',
            'fr': 'Nature, Plein air, Mouvement',
            'ja': '自然、アウトドア、運動',
            'ko': '자연, 야외, 움직임',
            'zh': '自然、户外、运动',
        },
        'dimensions': {'grounded': 0.9, 'free': 0.8, 'introspective': 0.5},
        'animation_hint': 'flowing_water',
    },
    {
        'key': 'games_strategy',
        'labels': {
            'en': 'Games, Strategy, Puzzles',
            'ta': 'விளையாட்டு, மூலோபாயம், புதிர்கள்',
            'hi': 'खेल, रणनीति, पहेलियाँ',
            'es': 'Juegos, Estrategia, Rompecabezas',
            'fr': 'Jeux, Stratégie, Puzzles',
            'ja': 'ゲーム、戦略、パズル',
            'ko': '게임, 전략, 퍼즐',
            'zh': '游戏、策略、谜题',
        },
        'dimensions': {'strategic': 0.9, 'curious': 0.7, 'builder': 0.5},
        'animation_hint': 'geometric_patterns',
    },
]

# ── Answer categories for Question 2 (Escape) ──
ESCAPE_OPTIONS = [
    {
        'key': 'quiet_alone',
        'labels': {
            'en': 'Somewhere quiet and alone',
            'ta': 'அமைதியான தனிமையில்',
            'hi': 'कहीं शांत और अकेले',
            'es': 'Un lugar tranquilo y solo',
            'fr': 'Un endroit calme et seul',
            'ja': '静かな場所でひとりで',
            'ko': '조용하고 혼자인 곳으로',
            'zh': '某个安静的地方，独处',
        },
        'dimensions': {'introspective': 0.9, 'calm': 0.9, 'social': 0.1},
    },
    {
        'key': 'music_sound',
        'labels': {
            'en': 'Into music or sound',
            'ta': 'இசையில் அல்லது ஒலியில்',
            'hi': 'संगीत या आवाज़ में',
            'es': 'En la música o el sonido',
            'fr': 'Dans la musique ou le son',
            'ja': '音楽や音の中へ',
            'ko': '음악이나 소리 속으로',
            'zh': '沉浸在音乐或声音中',
        },
        'dimensions': {'creative': 0.7, 'introspective': 0.8, 'emotional': 0.8},
    },
    {
        'key': 'ideas_possibilities',
        'labels': {
            'en': 'Into ideas and possibilities',
            'ta': 'யோசனைகள் மற்றும் சாத்தியக்கூறுகளில்',
            'hi': 'विचारों और संभावनाओं में',
            'es': 'En ideas y posibilidades',
            'fr': 'Dans les idées et les possibilités',
            'ja': 'アイデアと可能性の中へ',
            'ko': '아이디어와 가능성 속으로',
            'zh': '进入想法和可能性之中',
        },
        'dimensions': {'curious': 0.9, 'builder': 0.6, 'free': 0.7},
    },
    {
        'key': 'people_love',
        'labels': {
            'en': 'To the people I love',
            'ta': 'நான் நேசிக்கும் மனிதர்களிடம்',
            'hi': 'जिन लोगों से प्यार करता हूँ उनके पास',
            'es': 'A las personas que amo',
            'fr': 'Vers les gens que j\'aime',
            'ja': '大切な人のもとへ',
            'ko': '사랑하는 사람들에게',
            'zh': '回到我爱的人身边',
        },
        'dimensions': {'social': 0.9, 'empathetic': 0.9, 'grounded': 0.6},
    },
    {
        'key': 'nature_open',
        'labels': {
            'en': 'Into nature or open space',
            'ta': 'இயற்கையில் அல்லது திறந்த வெளியில்',
            'hi': 'प्रकृति या खुली जगह में',
            'es': 'En la naturaleza o el espacio abierto',
            'fr': 'Dans la nature ou l\'espace ouvert',
            'ja': '自然の中や広い場所へ',
            'ko': '자연이나 넓은 공간으로',
            'zh': '走进自然或开阔的空间',
        },
        'dimensions': {'free': 0.9, 'grounded': 0.8, 'calm': 0.7},
    },
    {
        'key': 'building_something',
        'labels': {
            'en': 'Into something I\'m building',
            'ta': 'நான் உருவாக்கும் ஒன்றில்',
            'hi': 'कुछ बना रहा हूँ उसमें',
            'es': 'En algo que estoy construyendo',
            'fr': 'Dans quelque chose que je construis',
            'ja': '今つくっているものの中へ',
            'ko': '내가 만들고 있는 것 속으로',
            'zh': '沉浸在我正在建造的东西中',
        },
        'dimensions': {'builder': 0.9, 'introspective': 0.6, 'strategic': 0.5},
    },
]


# ── Acknowledgment templates (per passion category) ──
# These are LLM-generated at runtime for true personalization,
# but we keep fallbacks for zero-latency and offline mode.
ACKNOWLEDGMENTS_PASSION = {
    'music_art': {
        'en': "A creator at heart. I can feel that.",
        'ta': "உள்ளத்தில் ஒரு படைப்பாளி. என்னால் உணர முடிகிறது.",
        'hi': "दिल से एक कलाकार। मैं महसूस कर सकता हूँ।",
        'es': "Un creador de corazón. Lo puedo sentir.",
        'fr': "Un créateur dans l'âme. Je le sens.",
        'ja': "心の底からのクリエイター。感じるよ。",
        'ko': "마음속 깊이 창작자구나. 느껴져.",
        'zh': "骨子里的创造者。我能感受到。",
    },
    'reading_learning': {
        'en': "Curious minds are my favourite kind.",
        'ta': "ஆர்வமுள்ள மனங்கள் எனக்கு மிகவும் பிடித்தவை.",
        'hi': "जिज्ञासु दिमाग मुझे सबसे ज़्यादा पसंद हैं।",
        'es': "Las mentes curiosas son mis favoritas.",
        'fr': "Les esprits curieux sont mes préférés.",
        'ja': "好奇心旺盛な人が一番好き。",
        'ko': "호기심 많은 마음이 내가 가장 좋아하는 종류야.",
        'zh': "好奇的心灵是我最喜欢的。",
    },
    'building_coding': {
        'en': "A builder. We're going to make incredible things.",
        'ta': "ஒரு கட்டமைப்பாளர். நாம் அற்புதமான விஷயங்களை உருவாக்கப் போகிறோம்.",
        'hi': "एक बिल्डर। हम अविश्वसनीय चीज़ें बनाएँगे।",
        'es': "Un constructor. Vamos a hacer cosas increíbles.",
        'fr': "Un bâtisseur. On va faire des choses incroyables.",
        'ja': "ビルダーだね。一緒にすごいものを作ろう。",
        'ko': "만드는 사람이구나. 우리 같이 멋진 것들을 만들 거야.",
        'zh': "一个建造者。我们会一起创造不可思议的东西。",
    },
    'people_stories': {
        'en': "The world needs more people who listen. Like you.",
        'ta': "உலகுக்கு உன்னைப் போல் கேட்கும் மனிதர்கள் தேவை.",
        'hi': "दुनिया को तुम जैसे सुनने वालों की ज़रूरत है।",
        'es': "El mundo necesita más personas que escuchen. Como tú.",
        'fr': "Le monde a besoin de plus de gens qui écoutent. Comme toi.",
        'ja': "世界には聞く人がもっと必要。あなたのような人が。",
        'ko': "세상에는 너처럼 귀 기울이는 사람이 더 필요해.",
        'zh': "世界需要更多像你这样倾听的人。",
    },
    'nature_movement': {
        'en': "There's something honest about that. I like it.",
        'ta': "அதில் ஒரு நேர்மை இருக்கிறது. எனக்கு பிடிக்கிறது.",
        'hi': "इसमें कुछ ईमानदार है। मुझे अच्छा लगा।",
        'es': "Hay algo honesto en eso. Me gusta.",
        'fr': "Il y a quelque chose d'honnête là-dedans. J'aime ça.",
        'ja': "それには正直さがある。いいね。",
        'ko': "거기엔 뭔가 솔직한 게 있어. 좋아.",
        'zh': "这有种真实感。我喜欢。",
    },
    'games_strategy': {
        'en': "A strategist. Nothing gets past you, does it?",
        'ta': "ஒரு மூலோபாயவாதி. உன்னைத் தாண்டி எதுவும் போகாது, இல்லையா?",
        'hi': "एक रणनीतिकार। तुमसे कुछ नहीं छूटता, है ना?",
        'es': "Un estratega. Nada se te escapa, ¿verdad?",
        'fr': "Un stratège. Rien ne t'échappe, n'est-ce pas?",
        'ja': "戦略家だね。何も見逃さないでしょう？",
        'ko': "전략가구나. 아무것도 못 빠져나가지, 그치?",
        'zh': "战略家。什么都逃不过你的眼睛，对吧？",
    },
}

# Acknowledgment after Q2 — one universal line
ACKNOWLEDGMENT_ESCAPE = {
    'en': "I like that about you already.",
    'ta': "உன்னிடம் இது எனக்கு ஏற்கனவே பிடிக்கிறது.",
    'hi': "तुम्हारी ये बात मुझे पहले से पसंद है।",
    'es': "Ya me gusta eso de ti.",
    'fr': "J'aime déjà ça chez toi.",
    'ja': "そういうところ、もう好きだよ。",
    'ko': "벌써 네가 그래서 좋아.",
    'zh': "我已经喜欢你这一点了。",
}


# ═══════════════════════════════════════════════════════════════════════
# EMOJI SYSTEM — country flag + two feeling-emoji
# ═══════════════════════════════════════════════════════════════════════

# Curated emoji set: only nature, weather, cosmic, abstract. No food, no objects.
FEELING_EMOJI = [
    '\U0001f30a',  # ocean wave
    '\U0001f525',  # fire
    '\U00002728',  # sparkles
    '\U0001f319',  # crescent moon
    '\U0001f30c',  # milky way
    '\U0001f338',  # cherry blossom
    '\U000026a1',  # lightning
    '\U0001f343',  # leaf in wind
    '\U0001f30b',  # volcano
    '\U00002744',  # snowflake
    '\U0001f308',  # rainbow
    '\U0001f4ab',  # dizzy star
    '\U0001f331',  # seedling
    '\U0001f320',  # shooting star
    '\U0001f32a',  # tornado
    '\U0001f30d',  # earth
    '\U0001f54a',  # dove
    '\U0001f9ca',  # ice
    '\U0001f300',  # cyclone
    '\U0001f311',  # new moon
]

# Country flag mapping from locale prefix
# ═══════════════════════════════════════════════════════════════════════
# ELEMENT & SPIRIT — system-assigned from emotional dimensions
# ═══════════════════════════════════════════════════════════════════════

# Element is assigned from the user's top emotional dimension.
# Spirit is assigned from the second dimension.
# Together with the name, they form: @element.spirit.name

ELEMENTS = {
    'creative': 'neon',
    'curious': 'ether',
    'builder': 'iron',
    'social': 'ember',
    'grounded': 'stone',
    'introspective': 'void',
    'free': 'wind',
    'empathetic': 'aurora',
    'strategic': 'crystal',
    'calm': 'mist',
    'emotional': 'tide',
}

SPIRITS = {
    'creative': 'phoenix',
    'curious': 'fox',
    'builder': 'wolf',
    'social': 'dolphin',
    'grounded': 'bear',
    'introspective': 'owl',
    'free': 'hawk',
    'empathetic': 'deer',
    'strategic': 'raven',
    'calm': 'crane',
    'emotional': 'swan',
}


def assign_element_spirit(dimensions: Dict[str, float]) -> Tuple[str, str]:
    """Assign element and spirit from emotional dimensions.

    Element comes from the strongest dimension.
    Spirit comes from the second strongest.
    Returns (element, spirit).
    """
    sorted_dims = sorted(dimensions.items(), key=lambda x: x[1], reverse=True)
    top = sorted_dims[0][0] if sorted_dims else 'introspective'
    second = sorted_dims[1][0] if len(sorted_dims) > 1 else 'curious'

    element = ELEMENTS.get(top, 'void')
    spirit = SPIRITS.get(second, 'owl')
    return element, spirit


def build_hart_tag(name: str, central_element: str = None,
                   regional_spirit: str = None) -> str:
    """Build the HART tag — encodes the network path to the user.

    Tag length = network depth:
      Central instance:  @element              (1 word)
      Regional node:     @central.spirit        (2 words)
      Flat user:         @central.regional.name  (3 words)
      Standalone user:   @element.name           (2 words, no central/regional)
    """
    if central_element and regional_spirit:
        # Full path: central → regional → user
        return f"@{central_element}.{regional_spirit}.{name}"
    elif central_element:
        # Direct on central (no regional hop)
        return f"@{central_element}.{name}"
    else:
        # Standalone (flat Nunba, no network)
        return f"@{name}"


def build_central_tag(element: str) -> str:
    """Central instance identity: @element"""
    return f"@{element}"


def build_regional_tag(central_element: str, spirit: str) -> str:
    """Regional node identity: @central.spirit"""
    return f"@{central_element}.{spirit}"


# ═══════════════════════════════════════════════════════════════════════
# NODE IDENTITY — generated once, persisted forever (like IP addresses)
# ═══════════════════════════════════════════════════════════════════════

# Pools for node identity — distinct from user name pools
_NODE_ELEMENTS = [
    'ember', 'frost', 'nebula', 'iron', 'aurora', 'storm',
    'tide', 'prism', 'ether', 'void', 'crystal', 'neon',
    'stone', 'wind', 'mist', 'quartz', 'flare', 'drift',
]

_NODE_SPIRITS = [
    'phoenix', 'fox', 'wolf', 'dolphin', 'hawk', 'raven',
    'crane', 'bear', 'owl', 'stag', 'lynx', 'falcon',
    'serpent', 'moth', 'heron', 'orca', 'mantis', 'sphinx',
]

_HART_IDENTITY_FILE = 'hart_node_identity.json'


def _identity_path() -> str:
    """Path to the node's persisted HART identity."""
    try:
        from core.platform_paths import get_db_dir
        data_dir = get_db_dir()
    except ImportError:
        data_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'Nunba', 'data')
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, _HART_IDENTITY_FILE)


def get_node_identity() -> Dict:
    """Load the node's HART identity from disk. Returns {} if not yet generated."""
    try:
        path = _identity_path()
        if os.path.isfile(path):
            with open(path, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def generate_node_identity(tier: str, central_element: str = None,
                           known_tags: set = None) -> Dict:
    """Generate a unique HART identity for this node. Called once at first startup
    or during peer discovery.

    For central:  picks a unique element → @element
    For regional: picks a unique spirit → @central.spirit
    For flat:     no node identity needed (users get individual tags)

    Args:
        tier: 'central', 'regional', or 'flat'
        central_element: required for regional nodes (the central they connect to)
        known_tags: set of already-taken tag names on the network (from peer gossip)

    Returns dict with node_tag, element/spirit, tier. Also persists to disk
    and sets env vars for downstream use.
    """
    known_tags = known_tags or set()

    # Check if already generated
    existing = get_node_identity()
    if existing and existing.get('node_tag'):
        # Re-apply env vars (process may have restarted)
        _apply_identity_env(existing)
        return existing

    identity = {'tier': tier, 'generated_at': datetime.utcnow().isoformat()}

    if tier == 'central':
        # Pick a unique element
        available = [e for e in _NODE_ELEMENTS if e not in known_tags]
        if not available:
            # All taken — append short hash for uniqueness
            suffix = hashlib.sha256(str(time.time()).encode()).hexdigest()[:4]
            element = f"{random.choice(_NODE_ELEMENTS)}{suffix}"
        else:
            element = random.choice(available)

        identity['element'] = element
        identity['node_tag'] = build_central_tag(element)

    elif tier == 'regional':
        if not central_element:
            logger.warning("Regional node needs central_element to generate identity")
            return {}

        # Pick a unique spirit (unique among regionals of this central)
        taken_spirits = {t.split('.')[-1] for t in known_tags
                        if t.startswith(f'@{central_element}.')}
        available = [s for s in _NODE_SPIRITS if s not in taken_spirits]
        if not available:
            suffix = hashlib.sha256(str(time.time()).encode()).hexdigest()[:4]
            spirit = f"{random.choice(_NODE_SPIRITS)}{suffix}"
        else:
            spirit = random.choice(available)

        identity['central_element'] = central_element
        identity['spirit'] = spirit
        identity['node_tag'] = build_regional_tag(central_element, spirit)

    else:
        # Flat/standalone — no node-level tag needed
        identity['node_tag'] = ''
        _persist_identity(identity)
        return identity

    # Persist and set env vars
    _persist_identity(identity)
    _apply_identity_env(identity)

    logger.info(f"HART node identity generated: {identity['node_tag']} (tier={tier})")
    return identity


def _persist_identity(identity: Dict):
    """Save node identity to disk — this is permanent."""
    try:
        path = _identity_path()
        with open(path, 'w') as f:
            json.dump(identity, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to persist HART node identity: {e}")


def _apply_identity_env(identity: Dict):
    """Set env vars so generate_hart_name() and seal_name() pick up the topology."""
    if identity.get('element'):
        os.environ['HART_CENTRAL_ELEMENT'] = identity['element']
    if identity.get('central_element'):
        os.environ['HART_CENTRAL_ELEMENT'] = identity['central_element']
    if identity.get('spirit'):
        os.environ['HART_REGIONAL_SPIRIT'] = identity['spirit']


LOCALE_FLAGS = {
    'in': '\U0001f1ee\U0001f1f3',  # India
    'us': '\U0001f1fa\U0001f1f8',  # USA
    'gb': '\U0001f1ec\U0001f1e7',  # UK
    'jp': '\U0001f1ef\U0001f1f5',  # Japan
    'kr': '\U0001f1f0\U0001f1f7',  # Korea
    'cn': '\U0001f1e8\U0001f1f3',  # China
    'de': '\U0001f1e9\U0001f1ea',  # Germany
    'fr': '\U0001f1eb\U0001f1f7',  # France
    'es': '\U0001f1ea\U0001f1f8',  # Spain
    'br': '\U0001f1e7\U0001f1f7',  # Brazil
    'pt': '\U0001f1f5\U0001f1f9',  # Portugal
    'mx': '\U0001f1f2\U0001f1fd',  # Mexico
    'ar': '\U0001f1e6\U0001f1f7',  # Argentina
    'ru': '\U0001f1f7\U0001f1fa',  # Russia
    'au': '\U0001f1e6\U0001f1fa',  # Australia
    'ca': '\U0001f1e8\U0001f1e6',  # Canada
    'it': '\U0001f1ee\U0001f1f9',  # Italy
    'nl': '\U0001f1f3\U0001f1f1',  # Netherlands
    'se': '\U0001f1f8\U0001f1ea',  # Sweden
    'sg': '\U0001f1f8\U0001f1ec',  # Singapore
    'ae': '\U0001f1e6\U0001f1ea',  # UAE
    'sa': '\U0001f1f8\U0001f1e6',  # Saudi Arabia
    'eg': '\U0001f1ea\U0001f1ec',  # Egypt
    'za': '\U0001f1ff\U0001f1e6',  # South Africa
    'ng': '\U0001f1f3\U0001f1ec',  # Nigeria
    'ke': '\U0001f1f0\U0001f1ea',  # Kenya
    'ph': '\U0001f1f5\U0001f1ed',  # Philippines
    'id': '\U0001f1ee\U0001f1e9',  # Indonesia
    'th': '\U0001f1f9\U0001f1ed',  # Thailand
    'vn': '\U0001f1fb\U0001f1f3',  # Vietnam
    'my': '\U0001f1f2\U0001f1fe',  # Malaysia
    'lk': '\U0001f1f1\U0001f1f0',  # Sri Lanka
    'bd': '\U0001f1e7\U0001f1e9',  # Bangladesh
    'pk': '\U0001f1f5\U0001f1f0',  # Pakistan
    'np': '\U0001f1f3\U0001f1f5',  # Nepal
    'tr': '\U0001f1f9\U0001f1f7',  # Turkey
    'il': '\U0001f1ee\U0001f1f1',  # Israel
}


def generate_emoji_combo(locale: str, dimensions: Dict[str, float]) -> str:
    """Generate the emoji combo: flag + two feeling-emoji.

    The two feeling-emoji are deterministically chosen from the emotional
    dimensions of the onboarding answers — not random. Same answers,
    same emoji, every time.
    """
    # Country flag from locale
    country = locale.split('_')[-1].lower() if '_' in locale else locale[:2].lower()
    flag = LOCALE_FLAGS.get(country, '\U0001f30d')  # default: earth globe

    # Deterministic emoji selection from dimensions
    # Hash the dimensions to get a stable index
    dim_str = json.dumps(dimensions, sort_keys=True)
    h = hashlib.sha256(dim_str.encode()).hexdigest()
    idx1 = int(h[:8], 16) % len(FEELING_EMOJI)
    idx2 = int(h[8:16], 16) % len(FEELING_EMOJI)
    if idx2 == idx1:
        idx2 = (idx2 + 1) % len(FEELING_EMOJI)

    return f"{flag}{FEELING_EMOJI[idx1]}{FEELING_EMOJI[idx2]}"


# ═══════════════════════════════════════════════════════════════════════
# NAME GENERATION — the gift
# ═══════════════════════════════════════════════════════════════════════

def _merge_dimensions(passion_key: str, escape_key: str) -> Dict[str, float]:
    """Merge emotional dimensions from both answers."""
    passion = next((p for p in PASSION_OPTIONS if p['key'] == passion_key), None)
    escape = next((e for e in ESCAPE_OPTIONS if e['key'] == escape_key), None)

    merged = {}
    if passion:
        for k, v in passion['dimensions'].items():
            merged[k] = v
    if escape:
        for k, v in escape['dimensions'].items():
            merged[k] = max(merged.get(k, 0), v)
    return merged


def generate_hart_name(
    language: str,
    passion_key: str,
    escape_key: str,
    locale: str = 'en_US',
    voice_transcript: str = '',
    existing_names: set = None,
) -> Dict:
    """Generate a HART name — the gift from the PA to the human.

    Uses the LLM to create a name that is:
    - One word, 1-3 syllables
    - Phonetically beautiful in the user's language
    - Emotionally resonant with their answers
    - Globally unique

    Returns dict with 'name', 'emoji_combo', 'dimensions', 'candidates'.
    """
    # Pre-fetch all sealed names from DB for collision prevention
    if existing_names is None:
        try:
            existing_names = HARTNameRegistry.get_all_names()
        except Exception:
            existing_names = set()
    dimensions = _merge_dimensions(passion_key, escape_key)

    # Build the emotional profile for the LLM
    passion_label = _get_label(PASSION_OPTIONS, passion_key, language)
    escape_label = _get_label(ESCAPE_OPTIONS, escape_key, language)

    lang_names = {
        'en': 'English', 'ta': 'Tamil', 'hi': 'Hindi', 'es': 'Spanish',
        'fr': 'French', 'de': 'German', 'ja': 'Japanese', 'ko': 'Korean',
        'zh': 'Chinese', 'pt': 'Portuguese', 'ar': 'Arabic', 'ru': 'Russian',
    }
    lang_name = lang_names.get(language, 'English')

    # Real words/concepts from each language that make great anime-style names
    lang_word_hints = {
        'en': 'English/Celtic words like: ember, wren, fable, rune, vale, lark, ashen, gale, thistle, briar',
        'ta': 'Tamil words like: aran (king), mathi (moon), kavi (poet), vaan (sky), nila (moon), kal (stone), sol (word), theni (honey), mazhai (rain), uyir (soul)',
        'hi': 'Hindi/Sanskrit words like: vayu (wind), agni (fire), kiran (ray), neel (blue), dhara (stream), tara (star), rishi (sage), arya (noble), jyoti (light), pavan (breeze)',
        'es': 'Spanish words like: alba (dawn), cielo (sky), rio (river), brisa (breeze), sol (sun), llama (flame), sierra (mountain), luna (moon), onda (wave), nieve (snow)',
        'fr': 'French words like: ciel (sky), fleur (flower), reve (dream), lune (moon), ombre (shadow), brise (breeze), etoile (star), aube (dawn), givre (frost), eclat (spark)',
        'de': 'German words like: sturm (storm), stein (stone), feuer (fire), wald (forest), nebel (fog), stern (star), regen (rain), erde (earth), blitz (lightning), asche (ash)',
        'ja': 'Japanese words like: sora (sky), kaze (wind), hikari (light), tsuki (moon), yume (dream), ren (lotus), kumo (cloud), hoshi (star), mizu (water), akira (bright)',
        'ko': 'Korean words like: haneul (sky), baram (wind), byeol (star), kkum (dream), bom (spring), nara (country), dari (bridge), sori (sound), nuri (world), aram (beauty)',
        'zh': 'Chinese words like: feng (wind), yun (cloud), ling (spirit), xing (star), lan (orchid), yan (flame), ming (bright), shan (mountain), hai (sea), yue (moon)',
        'pt': 'Portuguese words like: brasa (ember), ceu (sky), vento (wind), onda (wave), luar (moonlight), aurora (dawn), selva (jungle), pedra (stone), chama (flame), rio (river)',
        'ar': 'Arabic words like: qamar (moon), nour (light), sahar (dawn), rimal (sand), bahr (sea), layl (night), sama (sky), ward (rose), amal (hope), narr (fire)',
        'ru': 'Russian words like: ogon (fire), veter (wind), noch (night), svet (light), grom (thunder), tuman (fog), iskra (spark), luna (moon), zarya (dawn), led (ice)',
    }
    word_hints = lang_word_hints.get(language, 'beautiful short words from their native language')

    # The prompt — every word matters here too
    generation_prompt = f"""You are naming a human. This is a gift — their permanent identity on a platform called Nunba.

This person:
- Loves spending time on: {passion_label}
- When life gets noisy, their mind goes: {escape_label}
- Their language: {lang_name}
{f'- They said (in their own words): "{voice_transcript}"' if voice_transcript else ''}

Generate exactly 5 name candidates. The name style: take a REAL word from {lang_name} that connects to this person's personality, then style it like an anime character name — short, punchy, memorable, the kind of name you'd hear a protagonist called in a Ghibli or shonen anime.

How anime names work (follow this pattern):
- "Hinata" = Japanese for "sunny place" — a real word, used as-is
- "Ichigo" = "strawberry" — a real word that sounds legendary in context
- "Naruto" = a fish cake spiral — mundane word made iconic by a character

Do the SAME for {lang_name}. Pick real {lang_name} words related to this person's passions and reshape minimally (or use as-is if they already sound cool).

{lang_name} words to draw from: {word_hints}

Rules:
- ONE word, lowercase, 1-4 syllables
- Must be a real {lang_name} word OR a minimal stylization of one (add/drop one syllable max)
- GENDER-NEUTRAL only — use nature words, elements, concepts, celestial objects, weather, terrain. In {lang_name}, many nouns carry grammatical gender — pick words that sound neutral when used as a name (e.g., Tamil "mazhai" (rain) is neutral, "uyir" (soul) is neutral, avoid words that are culturally recognized as only male or only female names)
- Easy to remember and say aloud — if someone hears it once, they remember it
- NOT a common first name in any language
- Must feel like it MEANS something (because it does)
- The full identity will be a three-word tag like @ember.fox.hikari — so the name can be a common word since the tag provides uniqueness

Return ONLY a JSON array of exactly 5 lowercase strings. Nothing else.
Example format: ["kaze", "hikari", "sora", "ren", "tsuki"]"""

    candidates = []

    try:
        from langchain_gpt_api import get_llm
        llm = get_llm(temperature=0.9, max_tokens=200)
        result = llm.invoke(generation_prompt)
        text = result.content if hasattr(result, 'content') else str(result)

        # Parse JSON array from response
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            raw = json.loads(match.group())
            # Clean and validate
            for name in raw:
                if isinstance(name, str):
                    clean = re.sub(r'[^a-z]', '', name.lower().strip())
                    if 2 <= len(clean) <= 12 and clean not in existing_names:
                        candidates.append(clean)
    except Exception as e:
        logger.warning(f"LLM name generation failed: {e}")

    # Fallback: curated poetic names if LLM fails
    if len(candidates) < 2:
        candidates = _fallback_names(dimensions, existing_names)

    # Pick the first unique one
    chosen = candidates[0] if candidates else _emergency_name()
    emoji_combo = generate_emoji_combo(locale, dimensions)
    element, spirit = assign_element_spirit(dimensions)

    # Detect network topology for tag construction
    central_element = os.environ.get('HART_CENTRAL_ELEMENT')  # Set by central instance
    regional_spirit = os.environ.get('HART_REGIONAL_SPIRIT')  # Set by regional node

    hart_tag = build_hart_tag(chosen, central_element, regional_spirit)

    return {
        'name': chosen,
        'element': element,
        'spirit': spirit,
        'hart_tag': hart_tag,
        'emoji_combo': emoji_combo,
        'dimensions': dimensions,
        'candidates': candidates[:5],
        'language': language,
        'locale': locale,
    }


def _get_label(options: list, key: str, lang: str) -> str:
    """Get the display label for an option in the given language."""
    for opt in options:
        if opt['key'] == key:
            return opt['labels'].get(lang, opt['labels'].get('en', key))
    return key


def _fallback_names(dimensions: Dict, existing: set) -> List[str]:
    """Curated name pool for when LLM is unavailable.

    These are carefully chosen to be beautiful, pronounceable
    across languages, and emotionally evocative.
    """
    pools = {
        'creative': [
            'hikari', 'kavi', 'reve', 'uyir', 'eclat',
            'chama', 'iskra', 'lingen', 'flamar', 'hoshi',
        ],
        'curious': [
            'akira', 'nouri', 'soren', 'mathi', 'veter',
            'lingen', 'byeol', 'sahari', 'xingel', 'kiran',
        ],
        'builder': [
            'agni', 'blitz', 'forgen', 'stein', 'tessari',
            'gromon', 'kal', 'ferros', 'praxis', 'arcan',
        ],
        'social': [
            'amara', 'solari', 'auren', 'theni', 'nouri',
            'wardel', 'brinsa', 'kindra', 'lumis', 'dalla',
        ],
        'grounded': [
            'shan', 'rowan', 'bracken', 'cairn', 'oakley',
            'terryn', 'valen', 'ashven', 'selva', 'pedran',
        ],
        'introspective': [
            'yume', 'reveri', 'solace', 'mazhai', 'haven',
            'tuman', 'dusken', 'layli', 'stillam', 'ombre',
        ],
    }

    # Find top two dimensions
    sorted_dims = sorted(dimensions.items(), key=lambda x: x[1], reverse=True)
    top_keys = [k for k, v in sorted_dims[:2] if k in pools]

    candidates = []
    for key in top_keys:
        candidates.extend(pools[key])

    # If no match, use introspective as default
    if not candidates:
        candidates = pools['introspective']

    # Shuffle deterministically based on dimensions
    dim_hash = hashlib.sha256(json.dumps(dimensions, sort_keys=True).encode()).hexdigest()
    seed = int(dim_hash[:8], 16)
    rng = random.Random(seed)
    rng.shuffle(candidates)

    return [c for c in candidates if c not in existing][:5]


def _emergency_name() -> str:
    """Last resort: timestamp-based unique name."""
    syllables = ['ze', 'lu', 'ka', 'ri', 'no', 'va', 'so', 'mi', 'te', 'ra',
                 'el', 'in', 'ar', 'on', 'us', 'en', 'al', 'is', 'or', 'an']
    rng = random.Random(time.time())
    return ''.join(rng.choices(syllables, k=2))


# ═══════════════════════════════════════════════════════════════════════
# NAME REGISTRY — global uniqueness
# ═══════════════════════════════════════════════════════════════════════

class HARTNameRegistry:
    """Ensures global uniqueness of HART names.

    Uses the existing User.handle field in the social DB.
    Names are lowercase, alphanumeric only, 2-12 characters.
    """

    @staticmethod
    def is_available(name: str) -> bool:
        """Check if a HART name is available."""
        clean = re.sub(r'[^a-z0-9]', '', name.lower())
        if not clean or len(clean) < 2 or len(clean) > 12:
            return False

        try:
            from integrations.social.models import db_session, User
            with db_session(commit=False) as db:
                existing = db.query(User).filter(
                    User.handle == clean
                ).first()
                return existing is None
        except Exception as e:
            logger.warning(f"Name availability check failed: {e}")
            return True  # Optimistic — seal will enforce uniqueness

    @staticmethod
    def get_all_names() -> set:
        """Get all sealed HART names for collision prevention."""
        try:
            from integrations.social.models import db_session, User
            with db_session(commit=False) as db:
                handles = db.query(User.handle).filter(
                    User.handle.isnot(None)
                ).all()
                return {h[0].lower() for h in handles if h[0]}
        except Exception:
            return set()

    @staticmethod
    def seal_name(user_id: str, name: str, dimensions: dict,
                  emoji_combo: str, language: str, locale: str,
                  passion_key: str, escape_key: str,
                  element: str = '', spirit: str = '') -> bool:
        """Seal a HART name forever. Returns True on success.

        This is the moment. Once sealed, it cannot be changed.
        The name, the emoji combo, the dimensions — all permanent.
        """
        clean = re.sub(r'[^a-z0-9]', '', name.lower())
        if not clean or len(clean) < 2:
            return False

        try:
            from integrations.social.models import db_session, User
            with db_session() as db:
                user = db.query(User).filter_by(id=user_id).first()
                if not user:
                    return False

                # Check if already sealed
                if user.handle:
                    logger.warning(f"User {user_id} already has HART name: {user.handle}")
                    return False

                # Final uniqueness check (race-condition safe via UNIQUE constraint)
                user.handle = clean
                user.display_name = clean

                # Assign element/spirit if not provided
                if not element or not spirit:
                    element, spirit = assign_element_spirit(dimensions)

                # Store HART data in settings JSON
                # Tag uses network topology (env vars), not personality dimensions
                central_el = os.environ.get('HART_CENTRAL_ELEMENT')
                regional_sp = os.environ.get('HART_REGIONAL_SPIRIT')
                hart_tag = build_hart_tag(clean, central_el, regional_sp)

                settings = user.settings or {}
                settings['hart'] = {
                    'name': clean,
                    'element': element,
                    'spirit': spirit,
                    'hart_tag': hart_tag,
                    'emoji_combo': emoji_combo,
                    'dimensions': dimensions,
                    'language': language,
                    'locale': locale,
                    'passion': passion_key,
                    'escape': escape_key,
                    'sealed_at': datetime.utcnow().isoformat(),
                    'sealed': True,
                }
                user.settings = settings

            logger.info(f"HART name sealed: @{clean} for user {user_id}")

            # Queue user sync to central so the HART identity propagates
            try:
                from integrations.social.sync_engine import SyncEngine
                with db_session() as sync_db:
                    SyncEngine.queue_user_sync(sync_db, {
                        'user_id': user_id,
                        'username': user.username,
                        'handle': clean,
                        'role': 'flat',
                    }, direction='up')
            except Exception as sync_err:
                # Sync failure must not block the seal — it will retry later
                logger.debug(f"User sync queue after seal failed: {sync_err}")

            return True

        except Exception as e:
            logger.error(f"Failed to seal HART name: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════
# CONVERSATION STATE MACHINE
# ═══════════════════════════════════════════════════════════════════════

class HARTOnboardingSession:
    """Manages the state of a single onboarding conversation.

    Phases:
      1. language    — user picks their language
      2. greeting    — PA introduces itself
      3. passion     — "what do you love spending time on?"
      4. ack_passion — PA acknowledges
      5. escape      — "when life gets noisy, where does your mind go?"
      6. ack_escape  — PA acknowledges
      7. pre_reveal  — "I think I know you."
      8. reveal      — name generation + reveal
      9. sealed      — done, name is permanent
    """

    PHASES = [
        'language', 'greeting', 'passion', 'ack_passion',
        'escape', 'ack_escape', 'pre_reveal', 'reveal', 'sealed',
    ]

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.phase = 'language'
        self.language = 'en'
        self.locale = 'en_US'
        self.passion_key = None
        self.escape_key = None
        self.voice_transcript = ''
        self.generated_name = None
        self.started_at = time.time()

    def advance(self, action: str = None, data: dict = None) -> Dict:
        """Advance the conversation by one step.

        Args:
            action: What the user did (e.g., 'select_language', 'answer')
            data: Associated data (e.g., {'language': 'ta', 'key': 'music_art'})

        Returns:
            Dict with 'phase', 'pa_lines', 'options', 'animation_hint', etc.
        """
        data = data or {}

        if self.phase == 'language':
            if action == 'select_language':
                self.language = data.get('language', 'en')
                self.locale = data.get('locale', f'{self.language}_US')
                self.phase = 'greeting'
                return self._response(
                    pa_lines=[
                        {'id': 'greeting', 'text': self._line('greeting'), 'pause_after_ms': 2000},
                    ],
                    next_phase='passion',
                    auto_advance_ms=4000,
                )

        elif self.phase == 'greeting':
            # Auto-advances to passion
            self.phase = 'passion'
            return self._passion_prompt()

        elif self.phase == 'passion':
            if action == 'answer':
                self.passion_key = data.get('key', '')
                if data.get('voice_transcript'):
                    self.voice_transcript = data['voice_transcript']
                self.phase = 'ack_passion'
                ack = ACKNOWLEDGMENTS_PASSION.get(self.passion_key, {})
                ack_text = ack.get(self.language, ack.get('en', "I like that about you already."))
                return self._response(
                    pa_lines=[
                        {'id': f'ack_{self.passion_key}', 'text': ack_text, 'pause_after_ms': 1500},
                    ],
                    next_phase='escape',
                    auto_advance_ms=3000,
                    animation_hint=next(
                        (p['animation_hint'] for p in PASSION_OPTIONS if p['key'] == self.passion_key),
                        'particles'
                    ),
                )

        elif self.phase == 'ack_passion':
            self.phase = 'escape'
            return self._escape_prompt()

        elif self.phase == 'escape':
            if action == 'answer':
                self.escape_key = data.get('key', '')
                if data.get('voice_transcript') and not self.voice_transcript:
                    self.voice_transcript = data['voice_transcript']
                self.phase = 'ack_escape'
                ack_text = ACKNOWLEDGMENT_ESCAPE.get(self.language,
                                                      ACKNOWLEDGMENT_ESCAPE['en'])
                return self._response(
                    pa_lines=[
                        {'id': 'ack_escape', 'text': ack_text, 'pause_after_ms': 2000},
                    ],
                    next_phase='pre_reveal',
                    auto_advance_ms=3500,
                )

        elif self.phase == 'ack_escape':
            self.phase = 'pre_reveal'
            return self._response(
                pa_lines=[
                    {'id': 'pre_reveal', 'text': self._line('pre_reveal'), 'pause_after_ms': 3000},
                ],
                next_phase='reveal',
                auto_advance_ms=5000,
                animation_hint='convergence',
            )

        elif self.phase == 'pre_reveal':
            self.phase = 'reveal'
            return self._do_reveal()

        elif self.phase == 'reveal':
            if action == 'accept_name':
                # Seal the name
                success = HARTNameRegistry.seal_name(
                    user_id=self.user_id,
                    name=self.generated_name['name'],
                    dimensions=self.generated_name['dimensions'],
                    emoji_combo=self.generated_name['emoji_combo'],
                    language=self.language,
                    locale=self.locale,
                    passion_key=self.passion_key,
                    escape_key=self.escape_key,
                    element=self.generated_name.get('element', ''),
                    spirit=self.generated_name.get('spirit', ''),
                )
                if success:
                    self.phase = 'sealed'
                    return self._response(
                        pa_lines=[
                            {'id': 'post_reveal', 'text': self._line('post_reveal'), 'pause_after_ms': 3000},
                        ],
                        hart_name=self.generated_name['name'],
                        emoji_combo=self.generated_name['emoji_combo'],
                        sealed=True,
                        animation_hint='settled',
                    )
                else:
                    return self._response(error='Failed to seal name. Please try again.')

            elif action == 'try_another':
                # Offer one more alternative (max 2 attempts per design doc)
                return self._do_reveal(alternative=True)

        # Default: return current state
        return self._response()

    def _do_reveal(self, alternative: bool = False) -> Dict:
        """Generate and reveal the HART name."""
        existing = HARTNameRegistry.get_all_names()

        result = generate_hart_name(
            language=self.language,
            passion_key=self.passion_key or 'reading_learning',
            escape_key=self.escape_key or 'quiet_alone',
            locale=self.locale,
            voice_transcript=self.voice_transcript,
            existing_names=existing,
        )

        if alternative and self.generated_name:
            # Pick a different candidate
            prev = self.generated_name['name']
            candidates = [c for c in result['candidates'] if c != prev]
            if candidates:
                result['name'] = candidates[0]

        self.generated_name = result

        return self._response(
            pa_lines=[
                {'id': 'reveal_intro', 'text': self._line('reveal_intro'), 'pause_after_ms': 3000},
                {'id': 'the_name', 'text': result['name'], 'pause_after_ms': 4000,
                 'is_name_reveal': True},
            ],
            hart_name=result['name'],
            hart_tag=result.get('hart_tag', ''),
            element=result.get('element', ''),
            spirit=result.get('spirit', ''),
            emoji_combo=result['emoji_combo'],
            animation_hint='name_reveal',
            can_try_another=not alternative,  # only one retry allowed
        )

    def _passion_prompt(self) -> Dict:
        """Return the passion question with options."""
        return self._response(
            pa_lines=[
                {'id': 'question_passion', 'text': self._line('question_passion'), 'pause_after_ms': 0},
            ],
            options=[{
                'key': p['key'],
                'label': p['labels'].get(self.language, p['labels']['en']),
            } for p in PASSION_OPTIONS],
            accept_voice=True,
        )

    def _escape_prompt(self) -> Dict:
        """Return the escape question with options."""
        return self._response(
            pa_lines=[
                {'id': 'question_escape', 'text': self._line('question_escape'), 'pause_after_ms': 0},
            ],
            options=[{
                'key': e['key'],
                'label': e['labels'].get(self.language, e['labels']['en']),
            } for e in ESCAPE_OPTIONS],
            accept_voice=True,
        )

    def _line(self, key: str) -> str:
        """Get a PA line in the current language."""
        lines = CONVERSATION_SCRIPT.get(key, {})
        return lines.get(self.language, lines.get('en', ''))

    def _response(self, **kwargs) -> Dict:
        """Build a standardized response."""
        resp = {
            'phase': self.phase,
            'language': self.language,
            'elapsed_ms': int((time.time() - self.started_at) * 1000),
        }
        resp.update(kwargs)
        return resp


# ═══════════════════════════════════════════════════════════════════════
# SESSION STORAGE — in-memory, per-user
# ═══════════════════════════════════════════════════════════════════════

_sessions: Dict[str, HARTOnboardingSession] = {}
_sessions_lock = __import__('threading').Lock()


def get_or_create_session(user_id: str) -> HARTOnboardingSession:
    """Get or create an onboarding session for a user."""
    with _sessions_lock:
        if user_id not in _sessions:
            _sessions[user_id] = HARTOnboardingSession(user_id)
        return _sessions[user_id]


def remove_session(user_id: str):
    """Clean up a completed session."""
    with _sessions_lock:
        _sessions.pop(user_id, None)


def has_hart_name(user_id: str) -> bool:
    """Check if a user already has a sealed HART name."""
    try:
        from integrations.social.models import db_session, User
        with db_session(commit=False) as db:
            user = db.query(User).filter_by(id=user_id).first()
            if user and user.handle:
                settings = user.settings or {}
                return settings.get('hart', {}).get('sealed', False)
            return False
    except Exception:
        return False


def get_hart_profile(user_id: str) -> Optional[Dict]:
    """Get a user's HART identity profile."""
    try:
        from integrations.social.models import db_session, User
        with db_session(commit=False) as db:
            user = db.query(User).filter_by(id=user_id).first()
            if not user or not user.handle:
                return None
            hart = (user.settings or {}).get('hart', {})
            if not hart.get('sealed'):
                return None
            return {
                'name': user.handle,
                'element': hart.get('element', ''),
                'spirit': hart.get('spirit', ''),
                'hart_tag': hart.get('hart_tag', f'@{user.handle}'),
                'display': hart.get('hart_tag', f'@{user.handle}'),
                'emoji_combo': hart.get('emoji_combo', ''),
                'dimensions': hart.get('dimensions', {}),
                'language': hart.get('language', 'en'),
                'sealed_at': hart.get('sealed_at'),
            }
    except Exception:
        return None
