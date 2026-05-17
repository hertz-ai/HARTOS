"""Unit tests for integrations.channels.media.tts_text_normalizer."""

import os
import tempfile
import unittest
from unittest.mock import patch

from integrations.channels.media import tts_text_normalizer as tn


class RulePassTest(unittest.TestCase):
    """Deterministic rule-based normalization (no LLM)."""

    def setUp(self):
        # Isolate cache for each test
        self._tmpdir = tempfile.mkdtemp()
        self._prev_env = os.environ.get('HEVOLVE_CACHE_DIR')
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop('HEVOLVE_CACHE_DIR', None)
        else:
            os.environ['HEVOLVE_CACHE_DIR'] = self._prev_env

    def test_dollar_amount_en(self):
        out = tn.rule_normalize('The item costs $200', 'en')
        self.assertIn('dollars', out)
        self.assertIn('two hundred', out)
        self.assertNotIn('$200', out)

    def test_rupee_amount_en(self):
        out = tn.rule_normalize('The fare is Rs.200', 'en')
        self.assertIn('rupees', out)
        self.assertIn('two hundred', out)

    def test_rupee_symbol_en(self):
        out = tn.rule_normalize('Price ₹500 only', 'en')
        self.assertIn('rupees', out)
        self.assertIn('five hundred', out)

    def test_rupee_amount_hi(self):
        out = tn.rule_normalize('कीमत Rs.200 है', 'hi')
        self.assertIn('रुपये', out)  # rupees in Devanagari
        # Number should be expanded in Hindi via num2words (if installed)
        # If num2words missing for hi, falls back to English words

    def test_percent_en(self):
        out = tn.rule_normalize('Growth of 12%', 'en')
        self.assertIn('percent', out)
        self.assertIn('twelve', out)
        self.assertNotIn('12%', out)

    def test_percent_decimal(self):
        out = tn.rule_normalize('Efficiency 99.5%', 'en')
        self.assertIn('percent', out)
        self.assertNotIn('%', out)

    def test_url_stripped_to_link_word(self):
        out = tn.rule_normalize('Visit https://example.com/path', 'en')
        self.assertIn('link', out)
        self.assertNotIn('https', out)
        self.assertNotIn('example.com', out)

    def test_email_spelled(self):
        out = tn.rule_normalize('Email me at x@example.com', 'en')
        self.assertIn(' at ', out)
        self.assertIn(' dot ', out)
        self.assertNotIn('@', out)

    def test_time_2_30_pm(self):
        out = tn.rule_normalize('Meeting at 2:30 PM', 'en')
        self.assertIn('two', out)
        self.assertIn('thirty', out)
        self.assertNotIn('2:30', out)

    def test_time_on_the_hour(self):
        out = tn.rule_normalize('Start at 5:00 PM', 'en')
        self.assertIn('o clock', out)

    def test_pure_indic_text_unchanged(self):
        text = 'वह एक अच्छा लड़का है'  # "He is a good boy" in Hindi
        out = tn.rule_normalize(text, 'hi')
        self.assertEqual(out, text)

    def test_tamil_currency(self):
        out = tn.rule_normalize('விலை ₹100', 'ta')
        self.assertIn('ரூபாய்', out)  # rupees in Tamil

    def test_bare_number_expanded(self):
        out = tn.rule_normalize('We have 42 items', 'en')
        self.assertIn('forty', out)
        self.assertNotIn(' 42 ', out)

    def test_number_with_comma(self):
        out = tn.rule_normalize('Budget is $1,500', 'en')
        self.assertIn('dollars', out)
        self.assertNotIn('1,500', out)
        self.assertNotIn('$', out)


class ResidualDetectorTest(unittest.TestCase):
    """_has_residual_tokens correctly identifies unspoken tokens."""

    def test_digits_are_residual(self):
        self.assertTrue(tn._has_residual_tokens('Call 911 now'))

    def test_acronym_is_residual(self):
        self.assertTrue(tn._has_residual_tokens('NASA launched a rocket'))

    def test_clean_english_not_residual(self):
        self.assertFalse(tn._has_residual_tokens('The quick brown fox'))

    def test_clean_hindi_not_residual(self):
        self.assertFalse(tn._has_residual_tokens('वह एक अच्छा लड़का है'))

    def test_short_uppercase_not_flagged(self):
        # "IT" etc. are common and TTS pronounces fine
        self.assertFalse(tn._has_residual_tokens('IT department'))


class LLMFallbackTest(unittest.TestCase):
    """LLM fallback behavior when rule pass leaves residual tokens."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        os.environ.pop('HEVOLVE_CACHE_DIR', None)

    def test_llm_called_when_residual_present(self):
        mock_bus = type('FakeBus', (), {
            'infer': lambda self, model_type, prompt, options:
                {'response': 'Call nine one one now'}
        })()

        with patch.object(tn, '_get_model_bus', return_value=mock_bus):
            out = tn.normalize_for_tts('Call 911 now', 'en', use_llm=True)
            # Rule pass should have already converted 911 → words, but if not
            # LLM fills in.  Either way, no bare digit remains.
            self.assertFalse(any(c.isdigit() for c in out))

    def test_llm_skipped_when_no_residual(self):
        calls = []

        def fake_get_bus():
            calls.append(1)
            raise RuntimeError('LLM should not be called')

        with patch.object(tn, '_get_model_bus', side_effect=fake_get_bus):
            out = tn.normalize_for_tts('Plain English text', 'en', use_llm=True)
            self.assertEqual(len(calls), 0)
            self.assertEqual(out, 'Plain English text')

    def test_use_llm_false_skips_even_with_residual(self):
        with patch.object(tn, '_get_model_bus',
                          side_effect=RuntimeError('should not be called')):
            # Pure acronym — rule pass can't fix, LLM disabled — should still
            # return without raising
            out = tn.normalize_for_tts('NASA built a rocket', 'en', use_llm=False)
            self.assertIn('NASA', out)  # left as-is

    def test_llm_timeout_falls_back_to_rule_output(self):
        import time as _time

        class SlowBus:
            def infer(self, model_type, prompt, options):
                _time.sleep(2.5)  # exceeds _LLM_TIMEOUT_SEC
                return {'response': 'should not be used'}

        with patch.object(tn, '_get_model_bus', return_value=SlowBus()):
            out = tn.normalize_for_tts('Call 911 now', 'en', use_llm=True)
            # Rule pass already expanded 911 → 'nine hundred eleven'; LLM
            # output must NOT be used.
            self.assertNotIn('should not be used', out)


class CachingTest(unittest.TestCase):
    """Cache hits skip both rule and LLM passes."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        os.environ.pop('HEVOLVE_CACHE_DIR', None)

    def test_cache_roundtrip(self):
        # First call populates cache
        first = tn.normalize_for_tts('Price $100', 'en', use_llm=False)

        # Second call: patch rule_normalize to raise — if cache works, call succeeds
        with patch.object(tn, 'rule_normalize', side_effect=RuntimeError('no cache')):
            second = tn.normalize_for_tts('Price $100', 'en', use_llm=False)

        self.assertEqual(first, second)

    def test_different_lang_separate_cache_entry(self):
        en = tn.normalize_for_tts('Cost $50', 'en', use_llm=False)
        hi = tn.normalize_for_tts('Cost $50', 'hi', use_llm=False)
        self.assertNotEqual(en, hi)


class PublicAPITest(unittest.TestCase):
    """End-to-end normalize_for_tts shape."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        os.environ['HEVOLVE_CACHE_DIR'] = self._tmpdir

    def tearDown(self):
        os.environ.pop('HEVOLVE_CACHE_DIR', None)

    def test_empty_text_passthrough(self):
        self.assertEqual(tn.normalize_for_tts('', 'en'), '')
        self.assertEqual(tn.normalize_for_tts('   ', 'en'), '   ')

    def test_lang_normalization(self):
        # en-US, en_GB, EN — all map to 'en'
        a = tn.normalize_for_tts('$100', 'en-US', use_llm=False)
        b = tn.normalize_for_tts('$100', 'en_GB', use_llm=False)
        c = tn.normalize_for_tts('$100', 'EN',    use_llm=False)
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_none_lang_defaults_to_en(self):
        out = tn.normalize_for_tts('$100', None, use_llm=False)  # type: ignore
        self.assertIn('dollars', out)


if __name__ == '__main__':
    unittest.main()
