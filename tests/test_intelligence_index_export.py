"""The delta must carry intelligence_index from HiveMind's REAL stats shape.

Live incident 2026-08-25 (build 15 desktop): every layer worked — provider
loaded in-process, HiveMind constructed and captured, deltas accepted by
central — yet census showed intelligence_index=null for all nodes.  Root
cause: HiveMind.get_stats() (hevolveai hive_mind.py:3588) nests the
BootstrappedIntelligence stats under the key 'intelligence'
({'intelligence': {'intelligence_index': ..., 'growth_rate': ...}}), while
extract_local_delta's _real_metric read only TOP-LEVEL keys of
hivemind_stats.  Producer and consumer disagreed by one nesting level, so
the exported field was structurally unreachable.

This test feeds the delta builder the REAL producer shape and asserts the
index reaches hivemind_state.  Proven RED before the _real_metric fix.
"""
import types
import sys
import pytest


REAL_SHAPE_HIVEMIND_STATS = {
    # top-level keys as HiveMind.get_stats() actually returns them
    "num_agents": 1,
    "common_dim": 256,
    "fusion_method": "attention",
    "distributed": False,
    "intelligence": {
        "intelligence_index": 3.7,
        "growth_rate": 1.2,
        "num_agents": 1,
    },
}


def test_intelligence_index_reaches_delta(monkeypatch):
    from integrations.agent_engine import federated_aggregator as fa

    agg = fa.FederatedAggregator.__new__(fa.FederatedAggregator)
    agg._event_counters = {}
    import threading
    agg._event_counters_lock = threading.Lock()

    bridge = types.SimpleNamespace(
        get_stats=lambda: {"total_recorded": 5},
        get_learning_stats=lambda: {
            "learning": {},
            "hivemind": REAL_SHAPE_HIVEMIND_STATS,
            "bridge": {"total_recorded": 5},
        },
    )
    monkeypatch.setattr(
        "integrations.agent_engine.world_model_bridge.get_world_model_bridge",
        lambda: bridge,
    )

    delta = agg.extract_local_delta()
    assert delta is not None
    hm = delta.get("hivemind_state") or {}
    assert hm.get("intelligence_index") == 3.7, (
        "intelligence_index did not survive the producer's nested shape: "
        f"hivemind_state={hm}"
    )
    assert hm.get("growth_rate") == 1.2
