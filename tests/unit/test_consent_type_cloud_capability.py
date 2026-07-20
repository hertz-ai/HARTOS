"""#review (week review): 'cloud_capability' must be a REGISTERED consent type.

It is the de-facto consent type that the write path (consent_api.grant_consent,
which does not validate) stores, and that tools.py (T2 browser research),
encounter_api, icebreaker_service, and room_presence_service all read.  But it
was missing from consent_service.CONSENT_TYPES, so check_consent ->
_validate_consent_type raised ValueError on the very first line — swallowed by
the caller's broad except → consent ALWAYS denied → the entire T2 read/post
subsystem was dead-on-arrival.

Behavioral: drive the real _validate_consent_type (the gate that raised) and
assert it now accepts 'cloud_capability', still rejects unknown types, and
still accepts the legacy 'cloud_egress' (the fix is additive).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from integrations.social import consent_service as cs  # noqa: E402


def test_cloud_capability_is_registered():
    assert 'cloud_capability' in cs.CONSENT_TYPES


def test_validate_accepts_cloud_capability():
    # Before the fix this raised ValueError (-> swallowed -> consent denied).
    cs._validate_consent_type('cloud_capability')  # must not raise


def test_validate_still_rejects_unknown_type():
    with pytest.raises(ValueError):
        cs._validate_consent_type('definitely_not_a_real_consent_type')


def test_legacy_cloud_egress_still_valid():
    # Additive fix — must not have removed the existing type.
    cs._validate_consent_type('cloud_egress')
