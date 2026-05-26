"""
MMS-TTS tool — Meta's Massively Multilingual Speech TTS (1100+ languages).

VRAM: ~1.0 GB on GPU; runs comfortably on CPU too.
Architecture: VITS (the same flow-based model VITS-MMS papers describe).
HF: facebook/mms-tts-<iso639-3>  (per-language checkpoint, ~150 MB each).

Requires: only `transformers` (already bundled in Nunba's python-embed
and HARTOS's main deps).  No new pip dep on Linux/macOS.

For non-Roman script languages (Arabic, Hindi, Mandarin, Korean, ...)
the upstream VitsTokenizer flags `is_uroman=True` and expects pre-
romanized input via the `uroman` perl package.  This tool detects
that flag and:
  - if the optional `uroman` Python wrapper (or the `UROMAN` env var
    pointing at the perl repo) is present → romanizes automatically;
  - else returns `{'error': ..., 'transient': true}` so the router
    falls through to the next engine in the language preference list.

This keeps MMS as the universal-coverage fallback without breaking
when uroman isn't installed — the language preference order picks up
Indic Parler / XTTS / MeloTTS first when they're available.

SUBPROCESS ISOLATED: same convention as f5_tts_tool / chatterbox_tool.

Public API (parent side):
  mms_tts_synthesize(text, language, voice, output_path) → JSON
  unload_mms_tts() → None
"""

from typing import Optional

import os
import sys

from integrations.service_tools.gpu_worker import ToolWorker

# ── ISO 639-1 → ISO 639-3 mapping for MMS-TTS repos ──────────────
#
# MMS-TTS uses 3-letter ISO 639-3 codes (eng / fra / hin / cmn / ...).
# Nunba and HARTOS speak ISO 639-1 (en / fr / hi / zh).  This map is
# the SINGLE bridge between the two — every language in
# core.constants.SUPPORTED_LANG_DICT that has a known mms-tts-<iso3>
# repo is listed below.  Codes deliberately NOT mapped here either
# (a) don't have a HuggingFace mms-tts checkpoint, or (b) use a
# different ISO3 than the obvious 1↔3 collation and need verification
# before we route real users through them.
#
# Source: facebook/mms-tts model collection on HuggingFace.

ISO1_TO_ISO3 = {
    # Major European
    'en': 'eng', 'es': 'spa', 'fr': 'fra', 'de': 'deu', 'it': 'ita',
    'pt': 'por', 'nl': 'nld', 'pl': 'pol', 'tr': 'tur', 'ru': 'rus',
    'cs': 'ces', 'hu': 'hun', 'sv': 'swe', 'fi': 'fin', 'el': 'ell',
    'ro': 'ron', 'bg': 'bul', 'uk': 'ukr', 'cy': 'cym', 'is': 'isl',
    # CJK + SEA
    'zh': 'cmn', 'ja': 'jpn', 'ko': 'kor', 'vi': 'vie', 'th': 'tha',
    'id': 'ind', 'ms': 'zlm', 'km': 'khm', 'lo': 'lao', 'my': 'mya',
    # Indic (subset that has explicit mms-tts checkpoints)
    'hi': 'hin', 'bn': 'ben', 'ta': 'tam', 'te': 'tel', 'mr': 'mar',
    'gu': 'guj', 'kn': 'kan', 'ml': 'mal', 'pa': 'pan', 'or': 'ory',
    'ne': 'nep', 'as': 'asm', 'sd': 'snd', 'sa': 'san', 'ur': 'urd',
    'si': 'sin',
    # Middle East / Africa
    'ar': 'ara', 'fa': 'pes', 'he': 'heb', 'sw': 'swh',
}


def _iso1_to_iso3(req_lang: Optional[str]) -> Optional[str]:
    """Return the ISO 639-3 code for a 2-letter language, or None.

    None means "MMS doesn't have a verified checkpoint for this lang
    in our mapping" — caller should treat that as 'this engine cannot
    serve this language' and fall through to the next preference.
    """
    if not req_lang:
        return ISO1_TO_ISO3.get('en')
    code = req_lang.replace('_', '-').split('-')[0].lower()
    return ISO1_TO_ISO3.get(code)


def _try_uromanize(text: str) -> Optional[str]:
    """Best-effort romanization for non-Roman script input.

    Returns the romanized string on success, None if uroman is not
    available in any supported form.  The caller treats None as a
    hard failure for the current request.

    Order of attempts:
      1. The `uroman` Python wrapper (`pip install uroman`) — pure
         Python, no perl required.  Modern, easiest path.
      2. The `UROMAN` env var pointing at the isi-nlp/uroman perl
         repo (the canonical upstream path documented by HF).
    """
    # Pure-Python wrapper first
    try:
        import uroman as _uroman_pkg  # type: ignore
        u = _uroman_pkg.Uroman()
        return u.romanize_string(text)
    except Exception:
        pass

    # Perl repo via UROMAN env var
    uroman_root = os.environ.get('UROMAN')
    if uroman_root and os.path.isdir(uroman_root):
        script = os.path.join(uroman_root, 'bin', 'uroman.pl')
        if os.path.isfile(script):
            try:
                import subprocess
                proc = subprocess.run(
                    ['perl', script],
                    input=text.encode('utf-8'),
                    capture_output=True,
                    timeout=15,
                )
                if proc.returncode == 0:
                    out = proc.stdout.decode('utf-8', errors='replace')
                    return out.rstrip('\n')
            except Exception:
                pass

    return None


def _load():
    """Load the default English MMS-TTS checkpoint on the best device.

    The model+tokenizer pair is per-language, so we cache them in a
    dict keyed by ISO 639-3 code and lazily load on first request for
    each language.  On English the load is a no-op since `_State`
    already initialized it.
    """
    from transformers import VitsTokenizer, VitsModel

    try:
        import torch
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
    except Exception:
        device = 'cpu'

    repo = 'facebook/mms-tts-eng'
    tokenizer = VitsTokenizer.from_pretrained(repo)
    model = VitsModel.from_pretrained(repo)
    if device == 'cuda':
        try:
            model = model.to('cuda')
        except Exception:
            device = 'cpu'

    class _State:
        def __init__(self_):
            self_.device = device
            # iso3 → (tokenizer, model)
            self_.cache = {'eng': (tokenizer, model)}

    return _State()


def _synthesize(state, req: dict) -> dict:
    text = req.get('text', '')
    if not text or not text.strip():
        return {'error': 'Text is required'}

    output_path = req.get('output_path')
    if not output_path:
        return {'error': 'output_path is required'}

    iso3 = _iso1_to_iso3(req.get('language', 'en'))
    if not iso3:
        return {
            'error': (
                f"MMS-TTS has no mapped checkpoint for language "
                f"'{req.get('language')}'"
            ),
            'transient': True,
        }

    # Lazy-load the per-language model
    if iso3 not in state.cache:
        from transformers import VitsTokenizer, VitsModel
        repo = f'facebook/mms-tts-{iso3}'
        try:
            tokenizer = VitsTokenizer.from_pretrained(repo)
            model = VitsModel.from_pretrained(repo)
            if state.device == 'cuda':
                try:
                    model = model.to('cuda')
                except Exception:
                    pass
            state.cache[iso3] = (tokenizer, model)
        except Exception as e:
            return {
                'error': f'mms-tts-{iso3} load failed: {e}',
                'transient': True,
            }

    tokenizer, model = state.cache[iso3]

    # Romanize input text on demand for non-Roman script languages.
    # The VitsTokenizer.is_uroman flag tells us whether the model was
    # trained on romanized text.  If True and the input contains
    # non-ASCII, route through uroman first.
    if getattr(tokenizer, 'is_uroman', False):
        is_ascii = all(ord(c) < 128 for c in text)
        if not is_ascii:
            roman = _try_uromanize(text)
            if roman is None:
                return {
                    'error': (
                        f"mms-tts-{iso3} requires uroman for non-Roman "
                        f"input; install `pip install uroman` or set "
                        f"UROMAN env var to the isi-nlp/uroman repo path"
                    ),
                    'transient': True,
                }
            text = roman

    inputs = tokenizer(text=text, return_tensors='pt')
    if state.device == 'cuda':
        try:
            inputs = {k: v.to('cuda') for k, v in inputs.items()}
        except Exception:
            pass

    import torch
    with torch.no_grad():
        outputs = model(**inputs)
    waveform = outputs.waveform[0].detach().cpu().numpy()
    sr = int(model.config.sampling_rate)

    # Write WAV via soundfile (already a transitive of the bigger TTS
    # engines so it's reliably present).
    import soundfile as _sf
    _sf.write(output_path, waveform, sr)

    duration = round(len(waveform) / sr, 2)

    return {
        'path': output_path,
        'duration': duration,
        'sample_rate': sr,
        'engine': 'mms-tts',
        'device': state.device,
        'language': req.get('language', 'en'),
        'iso3': iso3,
        'voice': 'default',
    }


# ── Parent-side: one ToolWorker instance ─────────────────────────

_tool = ToolWorker(
    tool_name='mms_tts',
    tool_module='integrations.service_tools.mms_tts_tool',
    vram_budget='tts_mms_tts',
    output_subdir='mms_tts/output',
    engine='mms-tts',
    startup_timeout=120.0,   # first-time per-language download is ~150 MB
    request_timeout=90.0,
)


def mms_tts_synthesize(
    text: str,
    language: str = 'en',
    voice: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """Synthesize speech using MMS-TTS (Meta's 1100+ language VITS).

    Returns JSON. On subprocess crash or unsupported language the
    response contains `transient: true` so the caller can fall back.
    """
    return _tool.synthesize(
        text=text,
        language=language,
        voice=voice,
        output_path=output_path,
    )


def unload_mms_tts():
    """Stop the MMS-TTS worker subprocess and free its VRAM."""
    _tool.stop()


class MMSTTSTool:
    """Register MMS-TTS as an in-process service tool."""

    @classmethod
    def register_functions(cls):
        from .registry import ServiceToolInfo, service_tool_registry
        tool_info = ServiceToolInfo(
            name="mms_tts",
            description=(
                "MMS-TTS: Meta's Massively Multilingual Speech TTS. "
                "1100+ languages via per-language VITS checkpoints, "
                "~1 GB VRAM, no voice cloning. "
                "Non-Roman scripts need uroman (perl or "
                "`pip install uroman`).  Uses transformers — no extra pip dep."
            ),
            base_url="inprocess://mms_tts",
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": "Synthesize with MMS-TTS (1100+ languages, GPU/CPU).",
                    "params_schema": {
                        "text": {"type": "string"},
                        "language": {"type": "string"},
                    },
                },
            },
            tags=["tts", "speech", "multilingual", "mms", "vits"],
            timeout=60,
        )
        tool_info.is_healthy = True
        service_tool_registry._tools["mms_tts"] = tool_info
        return True


# NOTE: no `if __name__ == '__main__':` block — gpu_worker dispatcher
# resolves `_load` / `_synthesize` by convention.
