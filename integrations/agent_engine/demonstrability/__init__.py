"""DemonstrationProbe framework — Package B of the ml_intern brief.

Every seeded agent claims to be best at its goal_type. This package
turns the claim into a measurable, continuously-audited delta:

    our_score  vs  (trivial_prompt, previous_version, cloud_api)

A probe runs after each agent dispatch, computes a headline score,
records it (a) in the existing _Leaderboard under benchmark key
`goal:{goal_type}` — which HiveConsensus._vote_local_probe already
reads — (b) as a per-goal append-only JSONL history for the
ContinualImprovementProver, and (c) as tensorboard scalars under the
`demonstrability/{goal_type}/*` category. No parallel storage.

Regressions beyond a configured threshold auto-trigger a
weight_tracker rollback request (when available), closing the loop
the brief describes in §3.3.

Public API:
    - register_probe(goal_type) decorator
    - get_probe(goal_type) -> DemonstrationProbe | None
    - record_result(result: ProbeResult) -> None
    - run_post_dispatch(goal_type, ctx) -> ProbeResult | None  (hook
      that agent_daemon calls after a goal dispatch completes)
    - get_dashboard_snapshot() -> dict  (surface for /api/agent-engine/
      demonstrability)
"""
from __future__ import annotations

from .base import (
    DemonstrationProbe,
    ProbeContext,
    ProbeResult,
    record_result,
    get_dashboard_snapshot,
)
from .registry import (
    register_probe,
    get_probe,
    list_probes,
)
from .scheduler import run_post_dispatch

# Importing probes/* registers them via @register_probe — side-effect is
# intentional and must NOT be lazy, otherwise the first dispatch would
# find no probe registered.
from .probes import llm_judge  # noqa: F401
from .probes import speech_therapy  # noqa: F401

__all__ = [
    'DemonstrationProbe',
    'ProbeContext',
    'ProbeResult',
    'record_result',
    'get_dashboard_snapshot',
    'register_probe',
    'get_probe',
    'list_probes',
    'run_post_dispatch',
]
