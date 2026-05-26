"""J290-J299 · Localization depth.

J03-J14 in PRODUCT_MAP cover the chat-language matrix.  This cluster
covers DEEPER i18n: RTL layout, CJK width, locale numerals, lang
switch mid-task, UTF-8 edge cases.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ290RTL:
    def test_chat_layout_flips_for_arabic(self):
        pytest.skip('J290 RED — RTL (Arabic / Hebrew) layout flip untested')

    def test_mixed_direction_paragraphs_render_correctly(self):
        pytest.skip('J290b RED — bidi text (English name inside Arabic '
                    'sentence) rendering untested')


class TestJ291CJKWidth:
    def test_cjk_characters_not_truncated_in_previews(self):
        pytest.skip('J291 RED — Chinese/Japanese/Korean double-width '
                    'characters in preview fields not asserted')


class TestJ292LocaleNumerals:
    def test_hindi_devanagari_numerals_accepted(self):
        pytest.skip('J292 RED — non-ASCII numeral input (१२३ = 123) '
                    'journey untested')

    def test_arabic_eastern_numerals_accepted(self):
        pytest.skip('J292b RED — Arabic-Indic numerals (١٢٣) untested')


class TestJ293LocaleDateTime:
    def test_24hr_vs_12hr_respects_locale(self):
        pytest.skip('J293 RED — date/time formatting per locale gap')


class TestJ294LangSwitchMidTask:
    def test_user_lang_change_mid_chat_flushes_draft_model(self):
        skip_if_missing('core.user_lang:set_preferred_lang')
        pytest.skip(
            'J294 RED — switching language mid-conversation should '
            'evict the draft LLM for that lang; model_lifecycle '
            'subscriber exists but journey not tested'
        )


class TestJ295UTF8EdgeCases:
    def test_emoji_modifier_sequences_roundtrip(self):
        pytest.skip('J295 RED — complex emoji sequences (family with '
                    'skin tone modifiers) round-trip untested')

    def test_zalgo_text_sanitized_not_crashed(self):
        pytest.skip('J295b RED — zalgo / combining-mark spam input '
                    'safety untested')


class TestJ296FontFallback:
    def test_missing_glyph_falls_back_gracefully(self):
        pytest.skip('J296 RED — rare-script font fallback untested')


class TestJ297PluralRules:
    def test_ru_polish_complex_plural_rules_applied(self):
        pytest.skip('J297 RED — ICU plural rules for Slavic languages '
                    '("2 files" has different form than "5 files") '
                    'untested')


class TestJ298InputMethod:
    def test_ime_composition_not_submitted_prematurely(self):
        pytest.skip('J298 RED — CJK/Indic IME composition state not '
                    'asserted to be non-submitting for Enter key')


class TestJ299TranslationParity:
    def test_every_ui_string_has_translation_for_core_languages(self):
        pytest.skip('J299 RED — invariant: every user-facing string '
                    'exists in all 12 tier-1 languages; not tested')
