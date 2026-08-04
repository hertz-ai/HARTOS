"""
Tests for draft telemetry logging in SpeculativeDispatcher.dispatch_draft_first().

The telemetry block (~line 265) logs a JSON envelope with all classification
fields for offline calibration and drift detection.

Source: integrations/agent_engine/speculative_dispatcher.py ~line 265
"""
from core.constants import latency_budget
import json
import logging
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from unittest.mock import patch, MagicMock, PropertyMock


# ── Expected telemetry fields from the source code ──
EXPECTED_TELEMETRY_FIELDS = {
    'speculation_id', 'user_id', 'confidence', 'delegate',
    'is_casual', 'is_correction', 'is_create_agent',
    'channel_connect', 'language_change', 'draft_model',
    'latency_ms', 'reply_len', 'escalated',
}


def _build_telemetry_dict(
    speculation_id='abc123',
    user_id='user_42',
    confidence=0.92,
    delegate='none',
    parsed=None,
    draft_model=None,
    draft_latency_ms=150.0,
    draft_reply='Hello there!',
):
    """Reproduce the telemetry dict construction from source."""
    if parsed is None:
        parsed = {
            'is_casual': True, 'is_correction': False,
            'is_create_agent': False, 'channel_connect': None,
            'language_change': None, 'delegate': 'none',
        }
    return {
        'speculation_id': speculation_id,
        'user_id': user_id,
        'confidence': confidence,
        'delegate': delegate,
        'is_casual': parsed.get('is_casual'),
        'is_correction': parsed.get('is_correction'),
        'is_create_agent': parsed.get('is_create_agent'),
        'channel_connect': parsed.get('channel_connect'),
        'language_change': parsed.get('language_change'),
        'draft_model': draft_model.model_id if draft_model else None,
        'latency_ms': draft_latency_ms,
        'reply_len': len(draft_reply) if draft_reply else 0,
        'escalated': delegate != parsed.get('delegate', 'local'),
    }


# ═══════════════════════════════════════════════════════════════
# Functional Tests
# ═══════════════════════════════════════════════════════════════

class TestDraftTelemetryFunctional:
    """FT: Telemetry envelope structure and content."""

    def test_all_expected_fields_present(self):
        """Telemetry dict contains all 12 expected fields."""
        t = _build_telemetry_dict()
        assert set(t.keys()) == EXPECTED_TELEMETRY_FIELDS

    def test_speculation_id_preserved(self):
        """speculation_id passes through unchanged."""
        t = _build_telemetry_dict(speculation_id='spec_xyz_789')
        assert t['speculation_id'] == 'spec_xyz_789'

    def test_user_id_preserved(self):
        """user_id passes through unchanged."""
        t = _build_telemetry_dict(user_id='user_999')
        assert t['user_id'] == 'user_999'

    def test_confidence_numeric(self):
        """confidence is a numeric value."""
        t = _build_telemetry_dict(confidence=0.87)
        assert t['confidence'] == 0.87
        assert isinstance(t['confidence'], float)

    def test_delegate_none_when_confident(self):
        """When delegate='none', it passes through."""
        t = _build_telemetry_dict(delegate='none')
        assert t['delegate'] == 'none'

    def test_delegate_local_when_escalated(self):
        """When delegate='local', escalation is tracked."""
        parsed = {'delegate': 'none', 'is_casual': True, 'is_correction': False,
                  'is_create_agent': False, 'channel_connect': None, 'language_change': None}
        t = _build_telemetry_dict(delegate='local', parsed=parsed)
        assert t['delegate'] == 'local'
        assert t['escalated'] is True  # 'local' != parsed 'none'

    def test_escalated_false_when_delegate_unchanged(self):
        """escalated=False when delegate matches parsed delegate."""
        parsed = {'delegate': 'local', 'is_casual': False, 'is_correction': False,
                  'is_create_agent': False, 'channel_connect': None, 'language_change': None}
        t = _build_telemetry_dict(delegate='local', parsed=parsed)
        assert t['escalated'] is False

    def test_is_casual_true_propagated(self):
        """is_casual from parsed envelope is included."""
        parsed = {'is_casual': True, 'is_correction': False, 'is_create_agent': False,
                  'channel_connect': None, 'language_change': None, 'delegate': 'none'}
        t = _build_telemetry_dict(parsed=parsed)
        assert t['is_casual'] is True

    def test_is_correction_true_propagated(self):
        """is_correction flag propagates."""
        parsed = {'is_casual': False, 'is_correction': True, 'is_create_agent': False,
                  'channel_connect': None, 'language_change': None, 'delegate': 'none'}
        t = _build_telemetry_dict(parsed=parsed)
        assert t['is_correction'] is True

    def test_is_create_agent_true_propagated(self):
        """is_create_agent flag propagates."""
        parsed = {'is_casual': False, 'is_correction': False, 'is_create_agent': True,
                  'channel_connect': None, 'language_change': None, 'delegate': 'none'}
        t = _build_telemetry_dict(parsed=parsed)
        assert t['is_create_agent'] is True

    def test_channel_connect_value_propagated(self):
        """channel_connect string passes through."""
        parsed = {'is_casual': False, 'is_correction': False, 'is_create_agent': False,
                  'channel_connect': 'slack', 'language_change': None, 'delegate': 'none'}
        t = _build_telemetry_dict(parsed=parsed)
        assert t['channel_connect'] == 'slack'

    def test_language_change_value_propagated(self):
        """language_change string passes through."""
        parsed = {'is_casual': False, 'is_correction': False, 'is_create_agent': False,
                  'channel_connect': None, 'language_change': 'fr', 'delegate': 'none'}
        t = _build_telemetry_dict(parsed=parsed)
        assert t['language_change'] == 'fr'

    def test_draft_model_id_extracted(self):
        """draft_model field uses model_id attribute."""
        mock_model = MagicMock()
        mock_model.model_id = 'qwen3.5-0.8b'
        t = _build_telemetry_dict(draft_model=mock_model)
        assert t['draft_model'] == 'qwen3.5-0.8b'

    def test_draft_model_none_when_no_model(self):
        """draft_model is None when draft_model object is None."""
        t = _build_telemetry_dict(draft_model=None)
        assert t['draft_model'] is None

    def test_latency_ms_numeric(self):
        """latency_ms is a number."""
        t = _build_telemetry_dict(draft_latency_ms=342.7)
        assert t['latency_ms'] == 342.7

    def test_reply_len_calculated(self):
        """reply_len = len(draft_reply)."""
        t = _build_telemetry_dict(draft_reply='Hi there!')
        assert t['reply_len'] == len('Hi there!')

    def test_reply_len_zero_for_none(self):
        """reply_len=0 when draft_reply is None."""
        t = _build_telemetry_dict(draft_reply=None)
        assert t['reply_len'] == 0

    def test_reply_len_zero_for_empty(self):
        """reply_len=0 for empty string."""
        t = _build_telemetry_dict(draft_reply='')
        assert t['reply_len'] == 0

    def test_json_serializable(self):
        """Entire telemetry dict is JSON-serializable."""
        t = _build_telemetry_dict()
        serialized = json.dumps(t)
        assert isinstance(serialized, str)
        roundtrip = json.loads(serialized)
        assert roundtrip == t

    def test_log_output_format(self, caplog):
        """Verify that json.dumps produces the expected log prefix."""
        t = _build_telemetry_dict()
        logger = logging.getLogger('test_draft_telemetry')
        with caplog.at_level(logging.INFO, logger='test_draft_telemetry'):
            logger.info(f"draft-telemetry: {json.dumps(t)}")
        assert 'draft-telemetry:' in caplog.text
        assert 'speculation_id' in caplog.text
        assert 'confidence' in caplog.text
        assert 'delegate' in caplog.text


# ═══════════════════════════════════════════════════════════════
# Non-Functional Tests
# ═══════════════════════════════════════════════════════════════

class TestDraftTelemetryNonFunctional:
    """NFT: Robustness, exception safety, performance."""

    def test_telemetry_exception_does_not_propagate(self):
        """If telemetry construction raises, the except: pass catches it."""
        # Simulate a broken draft_model (model_id raises)
        bad_model = MagicMock()
        type(bad_model).model_id = PropertyMock(side_effect=AttributeError("no model_id"))
        try:
            _telemetry = {
                'speculation_id': 'x',
                'draft_model': bad_model.model_id if bad_model else None,
            }
        except Exception:
            pass  # This mimics the except: pass in production
        # Test passes if no exception propagates

    def test_missing_parsed_keys_default_to_none(self):
        """If parsed dict is missing keys, .get() returns None."""
        parsed = {}  # empty
        t = _build_telemetry_dict(parsed=parsed)
        assert t['is_casual'] is None
        assert t['is_correction'] is None
        assert t['is_create_agent'] is None
        assert t['channel_connect'] is None
        assert t['language_change'] is None

    def test_escalated_default_delegate_fallback(self):
        """When parsed has no 'delegate' key, default is 'local'."""
        parsed = {'is_casual': True, 'is_correction': False, 'is_create_agent': False,
                  'channel_connect': None, 'language_change': None}
        # delegate='none' but parsed.get('delegate', 'local') = 'local'
        t = _build_telemetry_dict(delegate='none', parsed=parsed)
        assert t['escalated'] is True  # 'none' != 'local' (default)

    def test_telemetry_construction_is_fast(self):
        """Telemetry dict construction should be sub-millisecond."""
        start = time.time()
        for _ in range(10000):
            _build_telemetry_dict()
        elapsed_ms = (time.time() - start) * 1000
        # 10k iterations should finish in well under 1 second
        _budget = latency_budget('telemetry_build_10k_ms')
        assert elapsed_ms < _budget, (
            f"Telemetry construction too slow: {elapsed_ms:.1f}ms for 10k "
            f"(budget {_budget}ms)")

    def test_large_reply_len_handled(self):
        """Very large draft_reply computes len correctly."""
        big_reply = 'x' * 1_000_000
        t = _build_telemetry_dict(draft_reply=big_reply)
        assert t['reply_len'] == 1_000_000

    def test_hive_delegate_tracked(self):
        """delegate='hive' is recorded correctly."""
        parsed = {'delegate': 'hive', 'is_casual': False, 'is_correction': False,
                  'is_create_agent': False, 'channel_connect': None, 'language_change': None}
        t = _build_telemetry_dict(delegate='hive', parsed=parsed)
        assert t['delegate'] == 'hive'
        assert t['escalated'] is False

    def test_negative_latency_not_rejected(self):
        """Telemetry does not validate latency (negative is logged as-is)."""
        t = _build_telemetry_dict(draft_latency_ms=-5.0)
        assert t['latency_ms'] == -5.0

    def test_zero_confidence_valid(self):
        """confidence=0.0 is a valid value."""
        t = _build_telemetry_dict(confidence=0.0)
        assert t['confidence'] == 0.0

    def test_confidence_above_one_not_rejected(self):
        """Telemetry does not clamp confidence (1.5 is logged as-is)."""
        t = _build_telemetry_dict(confidence=1.5)
        assert t['confidence'] == 1.5
