"""The VLM tier resolver must speak the CANONICAL intelligence_preference set.

`dispatch_inference` predated the canonical vocabulary
{local_only, auto, hive_preferred} with its own {local_only, hybrid, hive}.
Canonical 'hive_preferred' therefore matched NO branch and fell through to the
local-first ordering — a user who chose Hive still had local tried first. Both
vocabularies must now route identically; these pin that, and that mapping the
legacy spellings did not change any existing caller's behaviour.
"""
import pytest

try:
    from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
except Exception:  # minimal env without the vlm stack
    Qwen3VLBackend = None

pytestmark = pytest.mark.skipif(
    Qwen3VLBackend is None, reason="vlm stack not importable")

REQ = {'method': 'point_and_act', 'screenshot_b64': 'x', 'task': 't'}


def _tier(monkeypatch, pref):
    """Run the resolver with every tier stubbed; return which tier it picked."""
    b = Qwen3VLBackend()
    monkeypatch.setattr(b, '_is_local_vlm_available', lambda: True)
    monkeypatch.setattr(b, '_dispatch_local', lambda *a, **k: {'r': 'local'})
    monkeypatch.setattr(b, '_dispatch_paired_peer', lambda *a, **k: {'r': 'peer'})
    monkeypatch.setattr(b, '_dispatch_hive', lambda *a, **k: {'r': 'hive'})
    monkeypatch.setattr(b, '_dispatch_cloud', lambda *a, **k: {'r': 'cloud'})
    out = b.dispatch_inference(dict(REQ),
                               peer_dispatch=lambda *a, **k: {},
                               intelligence_preference=pref)
    return out['tier']


def test_canonical_hive_preferred_tries_peer_first(monkeypatch):
    assert _tier(monkeypatch, 'hive_preferred') == 'paired_peer', (
        "canonical 'hive_preferred' matched no branch and silently degraded "
        "to the local-first ordering")


def test_legacy_hive_still_tries_peer_first(monkeypatch):
    assert _tier(monkeypatch, 'hive') == 'paired_peer'


def test_canonical_auto_is_local_first(monkeypatch):
    assert _tier(monkeypatch, 'auto') == 'local'


def test_legacy_hybrid_behaves_as_auto(monkeypatch):
    assert _tier(monkeypatch, 'hybrid') == 'local'


def test_local_only_stays_local(monkeypatch):
    assert _tier(monkeypatch, 'local_only') == 'local'
