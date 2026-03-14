"""Append closing line to each language audio + stitch all into one combined file."""
import asyncio
import os
import sys

AUDIO_DIR = os.path.join(os.path.dirname(__file__), 'hartos_speech_audio')

# Closing line in each language — "I'm your privacy-first local Nanban (friend in Tamil)"
CLOSING = {
    "english":    ("I'm your privacy-first local Nanban — your friend.", "en-US-GuyNeural"),
    "hindi":      ("मैं आपका प्राइवेसी-फर्स्ट लोकल नण्बन हूँ — आपका दोस्त।", "hi-IN-MadhurNeural"),
    "tamil":      ("நான் உங்கள் privacy-first local நண்பன் — உங்கள் நண்பன்.", "ta-IN-ValluvarNeural"),
    "telugu":     ("నేను మీ privacy-first local నణ్బన్ — మీ స్నేహితుడు.", "te-IN-MohanNeural"),
    "spanish":    ("Soy tu Nanban local, privacidad primero — tu amigo.", "es-ES-AlvaroNeural"),
    "french":     ("Je suis votre Nanban local, confidentialite d'abord — votre ami.", "fr-FR-HenriNeural"),
    "arabic":     ("أنا نانبان المحلي الخاص بك — صديقك الذي يحمي خصوصيتك أولاً.", "ar-SA-HamedNeural"),
    "mandarin":   ("我是你的隐私优先本地 Nanban — 你的朋友。", "zh-CN-YunxiNeural"),
    "japanese":   ("私はあなたのプライバシー優先のローカル ナンバン — あなたの友達です。", "ja-JP-KeitaNeural"),
    "korean":     ("저는 당신의 프라이버시 우선 로컬 난반 — 당신의 친구입니다.", "ko-KR-InJoonNeural"),
    "german":     ("Ich bin dein lokaler Nanban, Datenschutz zuerst — dein Freund.", "de-DE-ConradNeural"),
    "portuguese": ("Sou seu Nanban local, privacidade em primeiro lugar — seu amigo.", "pt-BR-AntonioNeural"),
    "russian":    ("Я ваш локальный Нанбан — ваш друг, конфиденциальность прежде всего.", "ru-RU-DmitryNeural"),
    "swahili":    ("Mimi ni Nanban wako wa ndani, faragha kwanza — rafiki yako.", "sw-KE-RafikiNeural"),
    "bengali":    ("আমি তোমার প্রাইভেসি-ফার্স্ট লোকাল নণ্বন — তোমার বন্ধু।", "bn-IN-BashkarNeural"),
    "urdu":       ("میں آپ کا پرائیویسی فرسٹ لوکل نانبان ہوں — آپ کا دوست۔", "ur-PK-AsadNeural"),
    "italian":    ("Sono il tuo Nanban locale, privacy prima di tutto — il tuo amico.", "it-IT-DiegoNeural"),
    "turkish":    ("Ben senin gizlilik oncelikli yerel Nanban'inim — arkadasinim.", "tr-TR-AhmetNeural"),
}


async def generate_clip(text, voice, path):
    import edge_tts
    c = edge_tts.Communicate(text, voice)
    await c.save(path)


async def main():
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    from pydub import AudioSegment

    tmp_dir = os.path.join(AUDIO_DIR, '_tmp')
    os.makedirs(tmp_dir, exist_ok=True)

    combined = AudioSegment.empty()
    pause = AudioSegment.silent(duration=800)  # 0.8s pause between languages

    for lang, (text, voice) in CLOSING.items():
        speech_file = os.path.join(AUDIO_DIR, f'hartos_speech_{lang}.mp3')
        closing_file = os.path.join(tmp_dir, f'closing_{lang}.mp3')

        if not os.path.exists(speech_file):
            print(f"  [{lang}] SKIP — speech file not found")
            continue

        # Generate closing line
        print(f"  [{lang}] Generating closing line...")
        await generate_clip(text, voice, closing_file)

        # Stitch: original + pause + closing
        original = AudioSegment.from_mp3(speech_file)
        closing = AudioSegment.from_mp3(closing_file)
        stitched = original + pause + closing

        # Overwrite original with stitched version
        stitched.export(speech_file, format='mp3')
        size_kb = os.path.getsize(speech_file) / 1024
        print(f"  [{lang}] Stitched ({size_kb:.0f} KB)")

        # Add to combined
        combined = combined + stitched + pause

    # Export combined all-languages file
    combined_path = os.path.join(AUDIO_DIR, 'hartos_speech_all_languages.mp3')
    combined.export(combined_path, format='mp3')
    size_mb = os.path.getsize(combined_path) / (1024 * 1024)
    print(f"\nCombined all-languages: {size_mb:.1f} MB")

    # Cleanup tmp
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"Done. Files at: {AUDIO_DIR}")


if __name__ == '__main__':
    asyncio.run(main())
