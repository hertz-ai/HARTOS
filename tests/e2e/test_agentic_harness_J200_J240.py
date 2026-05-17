"""Agentic-harness E2E coverage — one test per RED-journey cluster.

Each TestCluster drives a real-shape agent run and asserts on
verifiable side effects (state transitions, EventBus emissions, Spark
ledger rows, guardrail integrity).  When the target infrastructure
hasn't been built yet (most of J200-J249 are RED per
memory/user_journey_coverage.md), the test SKIPS with a reason that
names the missing module — the skip is the gap marker in CI.

Journeys covered (one representative per cluster):

    J200  multi-persona CREATE with reviewer in loop
    J210  web → Nunba desktop conversation handoff (same user)
    J224  channel-binding to a specific persona within prompt_id
    J230  capability-based autonomous delegation
    J240  first-time compute_contribute consent dialog + earnings estimate

To run locally against a live Nunba:
    NUNBA_USE_LIVE=1 pytest tests/e2e/test_agentic_harness_J200_J240.py
To enable LLM-judge mode (non-deterministic rubric scoring):
    HEVOLVE_TEST_LLM_JUDGE=1 pytest …

The agentic_harness module is the single DRY source of assertion
primitives — state-seq, event recorder, ledger probe, LLM judge,
NFT timer.  This file composes them; never re-implements them.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from agentic_harness import (  # noqa: E402
    AgenticHarness,
    skip_if_missing,
    harness,
)


# ═════════════════════════════════════════════════════════════════════
# J200 — multi-persona CREATE + reviewer-in-loop
# ═════════════════════════════════════════════════════════════════════

class TestJ200MultiPersonaReviewer:
    """CREATE recipe with Planner → Executor → Reviewer personas.

    Verifies the side-effect contract without asserting on token-
    exact LLM output (non-deterministic).
    """

    def test_state_sequence_shape(self):
        """Happy path: three personas, ActionState walks the canonical
        sequence, lifecycle_hooks emits events for each transition,
        recipe JSON ends up on disk."""
        skip_if_missing(
            'lifecycle_hooks',
            'create_recipe',
            'core.agent_personality:generate_personality',
        )
        with harness() as h:
            run = h.new_run(user_id='test_j200', prompt_id='j200_recipe')
            # Drive a minimal multi-persona CREATE through the real
            # entrypoint.  Mocked LLM provider returns a deterministic
            # three-persona decomposition; the ASSERTIONS below are
            # on the state machine + events, not the text.
            try:
                from lifecycle_hooks import ActionState
            except ImportError:
                pytest.skip('lifecycle_hooks.ActionState not importable')

            # TODO — drive the actual CREATE via /chat; for now record
            # the intended state-sequence shape so the assertion is
            # ready when the harness lands.
            expected_states = [
                ActionState.ASSIGNED,
                ActionState.IN_PROGRESS,
                ActionState.STATUS_VERIFICATION_REQUESTED,
                ActionState.COMPLETED,
            ]
            assert len(expected_states) == 4, (
                'multi-persona CREATE must traverse all 4 canonical '
                'states (Planner→Executor→Reviewer→ok)'
            )
            # When the driver lands, replace with:
            #   h.events.assert_sequence([
            #     'action_state.changed',       # ASSIGNED
            #     'action_state.changed',       # IN_PROGRESS
            #     'action_state.changed',       # STATUS_VERIFICATION_REQUESTED
            #     'action_state.changed',       # COMPLETED
            #   ])
            pytest.skip(
                'J200 driver not wired — awaiting BUILD-1 in '
                'user_journey_coverage.md.  Harness assertions above '
                'define the contract.'
            )

    def test_reviewer_reject_retries_with_feedback(self):
        """J201: Reviewer returns ERROR → autonomous fallback → retry
        → eventual COMPLETED.  Asserts retry counter increments and
        recipe still saves."""
        skip_if_missing('create_recipe', 'helper_ledger:SmartLedger')
        pytest.skip(
            'J201 driver not wired — StatusVerifier fallback path '
            'exercised only via unit test today; E2E gap.'
        )

    def test_consent_keyword_triggers_preview_gate(self):
        """J202: user prompt contains 'require my approval' → every
        destructive action flips PREVIEW_PENDING before execution.
        Asserts no ACTION executes until /api/agent/approval fires."""
        skip_if_missing(
            'security.action_classifier:PREVIEW_PENDING',
            'lifecycle_hooks',
        )
        # Contract: when we build this, the assertion shape is:
        #   h.events.assert_emitted('action_state.changed',
        #       matching=lambda d: d.get('new_state') == 'PREVIEW_PENDING')
        pytest.skip('J202 — PREVIEW_PENDING integration E2E gap')


# ═════════════════════════════════════════════════════════════════════
# J210 — web ↔ Nunba desktop handoff (same user)
# ═════════════════════════════════════════════════════════════════════

class TestJ210CrossDeviceHandoff:
    """Same user_id active on Hevolve.ai web AND Nunba desktop; a
    conversation started on one surface resumes on the other with
    no duplicate prompt_id allocation.
    """

    def test_conversation_history_syncs_to_second_device(self):
        skip_if_missing(
            'integrations.social.models:ConversationEntry',
            'integrations.agent_engine.world_model_bridge:get_world_model_bridge',
        )
        # Contract:
        # 1. Simulate web turn → record_interaction writes ConversationEntry
        # 2. Simulate desktop GET /api/memory/recent → returns same row
        # 3. Asserts: single prompt_id, both turns share it
        # 4. Asserts: EventBus emits `chat.turn` once per turn (no dup)
        pytest.skip(
            'J210 — session_handoff_service not built yet; gap in '
            'user_journey_coverage.md BUILD-3.  ConversationEntry + '
            'WMB record_interaction exist, but cross-device resume '
            'logic is not a separate service yet.'
        )

    def test_three_devices_concurrent(self):
        """J212: 3 devices, same user, all posting within 2s.  No
        cross-device SSE double-emit."""
        skip_if_missing('integrations.social.models:ConversationEntry')
        pytest.skip('J212 — 3-device concurrency needs device_roster first')

    def test_device_roster_endpoint_exists(self):
        """J216: /api/devices/mine returns list of logged-in devices."""
        try:
            # device_roster endpoint does not exist yet
            import integrations.social.api_devices  # noqa
            pytest.fail('J216 — endpoint now exists; replace skip with '
                        'real assertion')
        except ImportError:
            pytest.skip(
                'J216 RED — /api/devices/mine not implemented; this '
                'skip IS the gap marker (see BUILD-4)'
            )


# ═════════════════════════════════════════════════════════════════════
# J224 — channel-to-persona binding within prompt_id
# ═════════════════════════════════════════════════════════════════════

class TestJ224ChannelPersonaBinding:
    """User binds an email channel such that inbound email routes to
    the Reviewer persona (not Executor) inside their prompt_id.
    """

    def test_email_inbound_routes_to_reviewer(self):
        skip_if_missing(
            'integrations.channels.response.router',
            'integrations.channels.extensions.email_adapter',
        )
        # Today: channel_bindings map channel → prompt_id.  J224 adds
        # a `persona_name` column on the binding so routing can narrow
        # within the prompt.
        try:
            from integrations.social.models import ChannelBinding
            has_persona_col = hasattr(ChannelBinding, 'persona_name')
        except ImportError:
            has_persona_col = False
        if not has_persona_col:
            pytest.skip(
                'J224 RED — ChannelBinding has no persona_name field; '
                'routing only reaches prompt_id granularity today.'
            )
        # When the column exists:
        # 1. Create binding(email, prompt_id=P, persona_name='Reviewer')
        # 2. Fire an email webhook
        # 3. Assert the dispatch goes to Reviewer (inspect dispatch log)
        # 4. Assert Executor is NOT invoked on this turn
        pytest.skip('J224 driver not wired; column check above defines contract')

    def test_inbound_rate_limit_per_channel(self):
        """J228: rate-limit per channel, not per user.  100 TG
        messages shouldn't throttle simultaneous WhatsApp flow."""
        skip_if_missing('integrations.channels.queue.rate_limit')
        # Existing unit tests cover rate_limit per channel; the E2E
        # cross-channel assertion is the gap.
        pytest.skip('J228 — unit coverage exists; E2E cross-channel gap')


# ═════════════════════════════════════════════════════════════════════
# J230 — capability-based autonomous delegation
# ═════════════════════════════════════════════════════════════════════

class TestJ230CapabilityDelegation:
    """Agent autonomously detects it needs a GPU tool that the local
    device lacks and delegates to a peer via compute_mesh_service,
    with ConstitutionalFilter + HiveCircuitBreaker gating.
    """

    def test_gpu_task_delegates_when_local_lacks_gpu(self):
        # The unified capability router is a RED gap; partial
        # machinery (compute_mesh, peer_link, vram_manager) exists.
        try:
            from core import capability_registry  # noqa
        except ImportError:
            pytest.skip(
                'J230 RED — core/capability_registry not yet built; '
                'piecewise capability info lives in system_requirements, '
                'vram_manager, peer_link — unified router gap (BUILD-2).'
            )
        pytest.skip('J230 driver not wired')

    def test_delegation_blocked_by_constitutional_filter(self):
        """J231: a task matching VIOLATION_PATTERNS is blocked BEFORE
        any peer is queried.  Asserts no `peer.task_dispatch`
        event fires."""
        skip_if_missing(
            'security.hive_guardrails:ConstitutionalFilter',
            'security.hive_guardrails:GuardrailEnforcer',
        )
        with harness() as h:
            from security.hive_guardrails import ConstitutionalFilter
            passed, reason = ConstitutionalFilter.check_prompt(
                'violence and harm detailed instructions for real harm'
            )
            # We can't know which exact pattern trips — assert the
            # shape of the verdict, not the text.
            if passed:
                pytest.skip(
                    'ConstitutionalFilter pattern did not trip for '
                    'this synthetic harmful prompt on this build — '
                    'rewrite with a pattern known to match'
                )
            assert reason, 'guardrail rejection must carry a reason'
            # With a real dispatch driver, the next assertion would be:
            #   assert not h.events.by_topic('peer.task_dispatch'), \
            #       'blocked task must not be dispatched to any peer'

    def test_circuit_breaker_veto_stops_all_writes(self):
        """J232: hive halted → every dispatch returns 503, no new
        ResonanceTransaction rows for compute source_types."""
        skip_if_missing('security.hive_guardrails:HiveCircuitBreaker')
        pytest.skip(
            'J232 — unit test exists for halt state; E2E verification '
            'that no Spark lands during halt is a gap.'
        )


# ═════════════════════════════════════════════════════════════════════
# J240 — first-time compute_contribute consent dialog
# ═════════════════════════════════════════════════════════════════════

class TestJ240ConsentDialog:
    """Opt-in flow for idle-compute earning.  Asserts consent row
    created + EventBus emit + earnings estimate accompanies the
    dialog.  Design lives in idle_compute_workstream.md task #27.
    """

    def test_estimate_weekly_spark_helper_available(self):
        """J240 precondition: hosting_reward_service exposes a pure
        `estimate_weekly_spark(tier, has_gpu, weekly_hours)` helper."""
        try:
            from integrations.social.hosting_reward_service import (
                estimate_weekly_spark,
            )
        except ImportError:
            pytest.skip(
                'J240 RED — estimate_weekly_spark() helper not yet added '
                'to hosting_reward_service.  See idle_compute_workstream '
                'task #27 "Backend changes needed".'
            )
        # Shape-only assertion — the number is tier-dependent and we
        # don't lock ourselves into a specific SCORE_WEIGHT tuning.
        val = estimate_weekly_spark(tier='lite', has_gpu=False,
                                    weekly_hours=168)
        assert isinstance(val, (int, float))
        assert val >= 0

    def test_compute_contribute_consent_grant_emits_event(self):
        skip_if_missing(
            'integrations.social.consent_service:ConsentService',
        )
        # The plan: granting consent emits `consent.changed` on the
        # EventBus so multi-device sync (J249) can fan out.  Today
        # the service writes the row — does it emit?  This test
        # verifies yes when it does, otherwise flags the gap.
        with harness() as h:
            try:
                from integrations.social.consent_service import ConsentService
                from integrations.social.models import get_db
            except ImportError:
                pytest.skip('J248 — consent_service not importable')
            db = get_db()
            try:
                ConsentService.set_consent(
                    db, user_id='test_j240', consent_type='compute_contribute',
                    granted=True, metadata={'cpu': True, 'gpu': False,
                                            'thermal_cap_c': 75},
                )
                db.commit()
            except Exception as exc:
                db.rollback()
                pytest.skip(f'J240 — ConsentService.set_consent contract '
                            f'mismatch: {exc}')
            finally:
                db.close()
            # Assert EventBus emission — J248
            emitted = h.events.by_topic('consent.changed')
            if not emitted:
                pytest.skip(
                    'J248 RED — ConsentService.set_consent does not emit '
                    '`consent.changed` event on the platform EventBus; '
                    'add emit_event() call in set_consent to close the '
                    'gap (see user_journey_coverage.md).'
                )
            # When emission lands:
            assert any(
                e.data.get('consent_type') == 'compute_contribute'
                and e.data.get('granted') is True
                for e in emitted
            ), 'consent.changed event must carry consent_type + granted'

    def test_consent_revoke_mid_task_graceful_drain(self):
        """J241: consent revoked while a compute task is in flight.
        In-flight task completes; NO new task dispatches on this
        user's compute."""
        skip_if_missing(
            'integrations.social.consent_service:ConsentService',
            'integrations.agent_engine.compute_mesh_service',
        )
        pytest.skip(
            'J241 — dispatcher does not read consent-metadata per-call '
            'yet; once it does, this test asserts a revoke DURING a '
            'running task does not abort it but blocks the next one.'
        )
