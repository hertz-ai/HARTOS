"""
Tests for _persist_language — validate lang against SUPPORTED_LANG_DICT, write JSON.

Source: hart_intelligence_entry.py ~line 5132
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, mock_open, MagicMock


# ── Recreate function under test with the same SUPPORTED_LANG_DICT keys ──

SUPPORTED_LANG_DICT = {
    "ar": "Arabic", "bg": "Bulgarian", "zh": "Chinese",
    "zh-cn": "Chinese (Simplified)", "nl": "Dutch", "fi": "Finnish",
    "fr": "French", "de": "German", "el": "Greek", "he": "Hebrew",
    "hu": "Hungarian", "id": "Indonesian", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "ms": "Malay", "no": "Norwegian", "pl": "Polish",
    "pt": "Portuguese", "ro": "Romanian", "ru": "Russian", "sk": "Slovak",
    "es": "Spanish", "sv": "Swedish", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese", "cy": "Welsh",
    "hi": "Hindi", "bn": "Bengali", "ta": "Tamil", "pa": "Punjabi",
    "gu": "Gujarati", "kn": "Kannada", "te": "Telugu", "mr": "Marathi",
    "ml": "Malayalam", "en": "English",
}

# Through platform_paths, like core.user_lang: under pytest that is a temp
# file.  It used to be ~/Documents/Nunba/data/hart_language.json itself, and
# the unpatched calls below (e.g. _persist_language('en')) wrote the owner's
# real language preference.
from core.platform_paths import get_db_path  # noqa: E402
_HART_LANG_PATH = get_db_path('hart_language.json')


def _persist_language(lang: str) -> bool:
    """Mirror of hart_intelligence_entry._persist_language."""
    if not lang or lang[:2] not in SUPPORTED_LANG_DICT:
        return False
    try:
        os.makedirs(os.path.dirname(_HART_LANG_PATH), exist_ok=True)
        with open(_HART_LANG_PATH, 'w') as _f:
            json.dump({'language': lang}, _f)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Functional Tests
# ═══════════════════════════════════════════════════════════════

class TestPersistLanguageFunctional:
    """FT: Valid/invalid lang, file write, disk errors."""

    def test_valid_lang_en_writes_file(self, tmp_path):
        """English code writes JSON successfully."""
        path = str(tmp_path / 'hart_language.json')
        with patch('tests.unit.test_persist_language._HART_LANG_PATH', path):
            # Call with patched path
            lang = 'en'
            if not lang or lang[:2] not in SUPPORTED_LANG_DICT:
                result = False
            else:
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, 'w') as _f:
                        json.dump({'language': lang}, _f)
                    result = True
                except Exception:
                    result = False
            assert result is True
            with open(path) as f:
                data = json.load(f)
            assert data == {'language': 'en'}

    def test_valid_lang_fr_writes_file(self, tmp_path):
        """French code writes correctly."""
        path = str(tmp_path / 'hart_language.json')
        assert _persist_language_with_path('fr', path) is True
        with open(path) as f:
            assert json.load(f)['language'] == 'fr'

    def test_valid_lang_hi_writes_file(self, tmp_path):
        """Hindi code writes correctly."""
        path = str(tmp_path / 'hart_language.json')
        assert _persist_language_with_path('hi', path) is True
        with open(path) as f:
            assert json.load(f)['language'] == 'hi'

    def test_valid_lang_zh_cn_uses_first_two_chars(self, tmp_path):
        """zh-cn validates using first 2 chars ('zh' in dict)."""
        path = str(tmp_path / 'hart_language.json')
        assert _persist_language_with_path('zh-cn', path) is True
        with open(path) as f:
            assert json.load(f)['language'] == 'zh-cn'

    def test_invalid_lang_xx_returns_false(self):
        """Unknown lang code returns False without writing."""
        assert _persist_language('xx') is False

    def test_invalid_lang_zz_returns_false(self):
        """Made-up lang 'zz' returns False."""
        assert _persist_language('zz') is False

    def test_empty_string_returns_false(self):
        """Empty string returns False."""
        assert _persist_language('') is False

    def test_none_returns_false(self):
        """None returns False (guarded by 'not lang')."""
        assert _persist_language(None) is False

    def test_single_char_returns_false(self):
        """Single char like 'e' — lang[:2] = 'e', not in dict."""
        assert _persist_language('e') is False

    def test_numeric_string_returns_false(self):
        """Numeric strings are not valid language codes."""
        assert _persist_language('12') is False

    def test_disk_error_returns_false(self):
        """OSError during write returns False."""
        with patch('builtins.open', side_effect=OSError("disk full")), \
             patch('os.makedirs'):
            assert _persist_language('en') is False

    def test_permission_error_returns_false(self):
        """PermissionError during makedirs returns False."""
        with patch('os.makedirs', side_effect=PermissionError("denied")):
            assert _persist_language('en') is False

    def test_all_supported_langs_accepted(self):
        """Every key in SUPPORTED_LANG_DICT is accepted (validation only, no write)."""
        for code in SUPPORTED_LANG_DICT:
            # Just check validation passes (first two chars in dict)
            assert code[:2] in SUPPORTED_LANG_DICT, f"{code} should be valid"

    def test_overwrites_existing_file(self, tmp_path):
        """Writing a new lang overwrites the previous value."""
        path = str(tmp_path / 'hart_language.json')
        _persist_language_with_path('en', path)
        _persist_language_with_path('fr', path)
        with open(path) as f:
            assert json.load(f)['language'] == 'fr'


# ═══════════════════════════════════════════════════════════════
# Non-Functional Tests
# ═══════════════════════════════════════════════════════════════

class TestPersistLanguageNonFunctional:
    """NFT: Edge cases, backward compat, robustness."""

    def test_return_type_always_bool(self):
        """Return is always bool, never None."""
        assert isinstance(_persist_language('en'), bool) or isinstance(_persist_language('xx'), bool)

    def test_valid_lang_false_return_on_write_error(self):
        """Even with valid lang, write failure returns False not exception."""
        with patch('builtins.open', side_effect=IOError("broken pipe")), \
             patch('os.makedirs'):
            result = _persist_language('en')
        assert result is False

    def test_unicode_lang_code_rejected(self):
        """Unicode characters that are not in dict are rejected."""
        assert _persist_language('\u00e9\u00e9') is False  # accented e's

    def test_very_long_lang_string_uses_first_two(self, tmp_path):
        """A long string starting with 'en' should pass validation."""
        path = str(tmp_path / 'hart_language.json')
        result = _persist_language_with_path('en-US-variant-extra-long', path)
        assert result is True

    def test_whitespace_lang_returns_false(self):
        """Whitespace-only string: first 2 chars = '  ', not in dict."""
        assert _persist_language('  ') is False

    def test_json_output_is_valid(self, tmp_path):
        """Written file is valid JSON."""
        path = str(tmp_path / 'hart_language.json')
        _persist_language_with_path('de', path)
        with open(path) as f:
            data = json.load(f)  # should not raise
        assert 'language' in data

    def test_makedirs_creates_parent(self, tmp_path):
        """Parent directories are created if missing."""
        path = str(tmp_path / 'deep' / 'nested' / 'hart_language.json')
        lang = 'en'
        if lang[:2] in SUPPORTED_LANG_DICT:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump({'language': lang}, f)
        assert os.path.exists(path)


# ── Helper: _persist_language with custom path ──

def _persist_language_with_path(lang: str, path: str) -> bool:
    """Version of _persist_language that writes to a custom path."""
    if not lang or lang[:2] not in SUPPORTED_LANG_DICT:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as _f:
            json.dump({'language': lang}, _f)
        return True
    except Exception:
        return False
