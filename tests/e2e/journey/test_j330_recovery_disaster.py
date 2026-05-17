"""J330-J339 · Recovery / disaster.

Master key rotation, compromised node, regional outage, partial halt,
ransomware scenarios.  The "nuclear" drills nobody wants to run —
and therefore haven't been.
"""
from __future__ import annotations

import pytest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _harness_shim import harness, skip_if_missing


class TestJ330MasterKeyRotation:
    def test_old_key_grace_period_respected(self):
        skip_if_missing('security.master_key:MASTER_PUBLIC_KEY_HEX')
        pytest.skip(
            'J330 RED — master key rotation drill: new key signs '
            'next release; nodes trust both keys for grace period; '
            'protocol not journey-tested (risky to test — but even '
            'more risky to leave untested)'
        )


class TestJ331CompromisedNodeKey:
    def test_compromised_node_cert_revoked_propagates(self):
        skip_if_missing('security.key_delegation:sign_child_certificate')
        pytest.skip('J331 RED — node-cert revocation propagation '
                    'through federation journey gap')


class TestJ332RegionalOutage:
    def test_regional_node_offline_clients_fall_back_to_flat(self):
        pytest.skip('J332 RED — regional→flat failover journey gap')


class TestJ333PartialHiveHalt:
    def test_halted_region_does_not_poison_healthy_regions(self):
        skip_if_missing('security.hive_guardrails:HiveCircuitBreaker')
        pytest.skip(
            'J333 RED — if region A trips HiveCircuitBreaker but region '
            "B is healthy, B must not accept A's pre-halt deltas "
            'retroactively; journey gap'
        )


class TestJ334Ransomware:
    def test_encrypted_user_dir_recovers_from_backup(self):
        pytest.skip('J334 RED — ransomware / user-data-corruption '
                    'recovery drill journey gap')


class TestJ335ForeignMasterKey:
    def test_release_signed_by_wrong_key_refused(self):
        skip_if_missing('security.master_key:verify_release_signature')
        pytest.skip('J335 — master_key verifies at boot; E2E assertion '
                    'that a forged release triggers boot halt gap')


class TestJ336ClockSkew:
    def test_wildly_wrong_clock_warns_not_silently_misbehaves(self):
        pytest.skip('J336 RED — wrong-clock → consent timestamps / '
                    'JWT exp / cert validity all break; journey gap')


class TestJ337DataDirMoved:
    def test_user_data_dir_migrated_cleanly(self):
        pytest.skip('J337 RED — moving ~/Nunba/data to D:/Nunba '
                    '(common Windows user fix) journey untested')


class TestJ338BackupRestore:
    def test_full_backup_restore_preserves_all_pii_and_ledger(self):
        pytest.skip('J338 RED — backup/restore round-trip journey not '
                    'end-to-end tested')


class TestJ339EmergencyHalt:
    def test_ops_triggered_halt_reaches_all_nodes_under_30s(self):
        skip_if_missing('security.hive_guardrails:HiveCircuitBreaker')
        pytest.skip(
            'J339 RED — ml_intern brief §2.3 explicitly requires this '
            'drill: ops trips kill → all nodes halt within 30s → '
            'writes stop, reads still work → confirmed resume.  '
            'Acceptance criterion D of the brief; not wired.'
        )
