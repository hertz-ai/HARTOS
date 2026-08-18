"""
Regression tests for the 2026-05-10 session's behavioral changes.

One file per session is the convention; tests are grouped by commit
hash so a future bisect can correlate a failure to its origin.

Each test class targets one shipped commit.  Failure of any test means
the corresponding commit either regressed or its assumptions broke.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════
# Commit 795bbc2 — fix(llm-watchdog): ModelPriority.PINNED → ACTIVE
# ═══════════════════════════════════════════════════════════════════

class TestWatchdogPriorityActive:
    """The stateless-probe path that registers a freshly-discovered
    llama-server must use ModelPriority.ACTIVE — PINNED doesn't exist
    on the enum and the previous AttributeError silenced the watchdog
    every 2 minutes for the entire session."""

    def test_model_priority_has_no_pinned_member(self):
        """Guard: if PINNED is later added to the enum, this test fails
        and the human reviewer should decide whether to switch the
        watchdog back to PINNED-with-pressure-evict-only semantics or
        keep ACTIVE."""
        from integrations.service_tools.model_lifecycle import ModelPriority
        assert not hasattr(ModelPriority, 'PINNED'), (
            "ModelPriority.PINNED was added to the enum — re-evaluate "
            "the stateless-probe site at model_lifecycle.py:1201 to "
            "decide whether ACTIVE is still right."
        )

    def test_stateless_probe_uses_active(self):
        """The literal at the stateless-probe registration site must
        be ACTIVE.  Detects accidental revert to PINNED."""
        import integrations.service_tools.model_lifecycle as ml
        src = open(ml.__file__, encoding='utf-8').read()
        assert 'priority=ModelPriority.ACTIVE' in src, (
            "stateless-probe registration must use ACTIVE; previous "
            "PINNED reference broke the watchdog every 2 minutes."
        )
        # Guard against a literal "PINNED" string sneaking back into
        # this file outside of comments — check for any code reference.
        assert 'ModelPriority.PINNED' not in src, (
            "ModelPriority.PINNED reference re-introduced — fix "
            "would crash watchdog with AttributeError."
        )


# ═══════════════════════════════════════════════════════════════════
# Commit 951d067 — fix(lifecycle): IN_PROGRESS → ERROR / PENDING
# ═══════════════════════════════════════════════════════════════════

class TestLifecycleTransitionsRelaxed:
    """The FSM was rejecting `in_progress → error` and `in_progress →
    pending` transitions, forcing every mid-execution failure to log
    "Invalid transition" and lose the action's error context.  Both
    edges are legitimate forward transitions and must pass validation."""

    def setup_method(self):
        # Stub heavy transitive imports the same way test_lifecycle_hooks
        # does.  Importing here keeps the stub scoped to this class.
        sys.modules.pop('lifecycle_hooks', None)
        # Ensure core.session_cache is importable (real or stub).
        try:
            import core.session_cache  # noqa: F401
        except Exception:
            pass
        from lifecycle_hooks import (validate_state_transition, ActionState,
                                     action_states, _state_lock)
        self.validate = validate_state_transition
        self.AS = ActionState
        # Reset module state for a clean per-test slate.
        with _state_lock:
            action_states.clear()

    def _set_state(self, user_prompt, action_id, state):
        """Force the action into a state without going through
        set_action_state (which itself validates)."""
        from lifecycle_hooks import action_states, _state_lock
        with _state_lock:
            action_states.setdefault(user_prompt, {})[action_id] = state

    def test_in_progress_to_error_now_allowed(self):
        self._set_state('u_p', 1, self.AS.IN_PROGRESS)
        assert self.validate('u_p', 1, self.AS.ERROR) is True

    def test_in_progress_to_pending_now_allowed(self):
        self._set_state('u_p', 1, self.AS.IN_PROGRESS)
        assert self.validate('u_p', 1, self.AS.PENDING) is True

    def test_in_progress_to_completed_still_blocked(self):
        """COMPLETED still requires going through STATUS_VERIFICATION_
        REQUESTED first — the relaxation only added ERROR + PENDING."""
        self._set_state('u_p', 1, self.AS.IN_PROGRESS)
        assert self.validate('u_p', 1, self.AS.COMPLETED) is False

    def test_in_progress_to_status_verification_still_allowed(self):
        """Sanity: the original allowed transition still works."""
        self._set_state('u_p', 1, self.AS.IN_PROGRESS)
        assert self.validate(
            'u_p', 1, self.AS.STATUS_VERIFICATION_REQUESTED) is True


# ═══════════════════════════════════════════════════════════════════
# Commit 5b6b908 — fix(coding-workspace): get_coding_workspace_dir
# ═══════════════════════════════════════════════════════════════════

class TestCodingWorkspaceDir:
    """The 17 hardcoded `work_dir="coding"` sites for AutoGen now route
    through core.platform_paths.get_coding_workspace_dir() so the
    workspace lives under user-data, not under Program Files (which
    fails with WinError 5 on non-admin Windows installs)."""

    def test_returns_path_under_user_data_dir(self, tmp_path, monkeypatch):
        # Force NUNBA_DATA_DIR to a temp path so we don't pollute the
        # real user data dir during the test.
        monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
        # Bust the platform_paths module cache so the env var takes effect.
        import core.platform_paths as pp
        pp._cached_data_dir = None
        from core.platform_paths import get_coding_workspace_dir
        path = get_coding_workspace_dir()
        assert str(tmp_path) in path
        assert path.endswith('coding')

    def test_creates_directory_idempotently(self, tmp_path, monkeypatch):
        monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
        import core.platform_paths as pp
        pp._cached_data_dir = None
        from core.platform_paths import get_coding_workspace_dir
        path1 = get_coding_workspace_dir()
        assert os.path.isdir(path1)
        # Second call must succeed (exist_ok=True semantics).
        path2 = get_coding_workspace_dir()
        assert path1 == path2

    def test_no_hardcoded_work_dir_coding_in_canonical_callers(self):
        """Regression guard: the 5 files refactored in 5b6b908 must
        NOT re-introduce the literal `"work_dir": "coding"` string."""
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        for relpath in [
            'create_recipe.py',
            'gather_agentdetails.py',
            'reuse_recipe.py',
            'helper.py',
            'hart_intelligence_entry.py',
        ]:
            p = os.path.join(repo_root, relpath)
            if not os.path.isfile(p):
                continue
            src = open(p, encoding='utf-8').read()
            # The hardcoded literal.  ` "coding"` (with the leading
            # space + quote) is what AutoGen used to receive.
            assert '"work_dir": "coding"' not in src, (
                f"Re-introduced hardcoded work_dir='coding' in {relpath}; "
                f"must use core.platform_paths.get_coding_workspace_dir()."
            )


# ═══════════════════════════════════════════════════════════════════
# Commit 0b8e524 — fix(thinking): per-event msg_id in publish_thinking_trace
# ═══════════════════════════════════════════════════════════════════

class TestPublishThinkingTraceMsgId:
    """Each publish_thinking_trace call must inject a unique msg_id
    (uuid4 hex) into the envelope so multiple thinking events sharing
    the same request_id (N steps in one chat turn) aren't collapsed
    by client-side dedup.  request_id remains the GROUPING key;
    msg_id is the dedup key."""

    def test_full_schema_envelope_has_msg_id(self):
        captured = {}

        def fake_publish(topic, payload, *args, **kwargs):
            captured['topic'] = topic
            captured['payload'] = payload

        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   return_value=fake_publish):
            from core.peer_link.crossbar_publish import publish_thinking_trace
            ok = publish_thinking_trace(
                text='test', user_id='u123', request_id='req1',
                full_schema=True)
        assert ok is True
        import json as _json
        envelope = _json.loads(captured['payload'])
        assert 'msg_id' in envelope
        # uuid4 hex is 32 chars
        assert len(envelope['msg_id']) == 32
        # request_id preserved as the grouping key
        assert envelope['request_id'] == 'req1'

    def test_short_schema_envelope_has_msg_id(self):
        captured = {}

        def fake_publish(topic, payload, *args, **kwargs):
            captured['payload'] = payload

        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   return_value=fake_publish):
            from core.peer_link.crossbar_publish import publish_thinking_trace
            publish_thinking_trace(
                text='hi', user_id='u', request_id='r',
                full_schema=False)
        import json as _json
        envelope = _json.loads(captured['payload'])
        assert 'msg_id' in envelope
        assert envelope['request_id'] == 'r'

    def test_two_emits_same_request_id_get_distinct_msg_ids(self):
        """Critical: this is the bug the fix addresses.  Two thinking
        events for one chat turn must have DIFFERENT msg_ids so client
        dedup doesn't collapse them."""
        captured = []

        def fake_publish(topic, payload, *args, **kwargs):
            captured.append(payload)

        with patch('core.safe_hartos_attr.safe_hartos_attr',
                   return_value=fake_publish):
            from core.peer_link.crossbar_publish import publish_thinking_trace
            publish_thinking_trace(text='step 1', user_id='u', request_id='r')
            publish_thinking_trace(text='step 2', user_id='u', request_id='r')
        import json as _json
        e1 = _json.loads(captured[0])
        e2 = _json.loads(captured[1])
        assert e1['request_id'] == e2['request_id'] == 'r'
        assert e1['msg_id'] != e2['msg_id']


# ═══════════════════════════════════════════════════════════════════
# Commit 9483b00 (revised from 29ac1b9 + 0b8e524) — EventBus SSE bridge
# ═══════════════════════════════════════════════════════════════════

class TestEventBusMsgIdInjection:
    """EventBus.emit must inject msg_id into dict payloads when the
    caller didn't supply one (so future emit_event-based publishers
    get free dedup), and must NOT overwrite caller-supplied msg_ids
    (callers may use a stable id for replay-on-reconnect)."""

    def test_emit_injects_msg_id_when_absent(self):
        from core.platform.events import EventBus
        bus = EventBus()
        data = {'topic': 'chat.thinking', 'text': 'hi'}
        bus.emit('chat.thinking', data)
        assert 'msg_id' in data
        assert len(data['msg_id']) == 32  # uuid4 hex

    def test_emit_preserves_caller_msg_id(self):
        from core.platform.events import EventBus
        bus = EventBus()
        data = {'msg_id': 'caller-supplied-id', 'text': 'hi'}
        bus.emit('chat.thinking', data)
        assert data['msg_id'] == 'caller-supplied-id'

    def test_emit_skips_msg_id_for_non_dict_payload(self):
        """Non-dict payloads can't be deduped reliably — skip injection
        without raising."""
        from core.platform.events import EventBus
        bus = EventBus()
        # Should not raise.
        bus.emit('test.string', 'just a string')
        bus.emit('test.none', None)
        bus.emit('test.int', 42)


class TestEventBusSSEDenylist:
    """The denylist defaults to empty per user instruction
    ("allow all unless some events need not be published to SSE").
    The _topic_targets_sse helper returns True for any topic not in
    the denylist."""

    def test_default_denylist_holds_only_the_internal_bus_prefix(self):
        from core.platform.events import _SSE_DENYLIST_PREFIXES
        # The instruction was "allow all UNLESS some events need not be
        # published to SSE" -- `bus.*` is exactly that exception, and it was
        # added on live evidence: the EventBus auto-bridge re-published every
        # `bus.<topic>` alongside the canonical SSE leg, so one publish became
        # two envelopes with divergent dedup keys and the SPA played TTS audio
        # TWICE on a single chat turn (2026-05-10 20:28:29). Assert the narrow
        # exception explicitly, so a BROAD denylist still fails this guard.
        assert _SSE_DENYLIST_PREFIXES == ('bus.',)

    def test_topic_targets_sse_returns_true_by_default(self):
        from core.platform.events import _topic_targets_sse
        assert _topic_targets_sse('chat.thinking') is True
        assert _topic_targets_sse('theme.changed') is True
        assert _topic_targets_sse('memory.item_added') is True
        assert _topic_targets_sse('any.future.topic.name') is True


# ═══════════════════════════════════════════════════════════════════
# Commit afd795b — feat(privacy): auto_grant_with_notice
# ═══════════════════════════════════════════════════════════════════

class TestConsentAutoGrantWithNotice:
    """Privacy-first principle: nothing fails in the name of privacy.
    First call → silent auto-grant + emit notice + return True so the
    caller proceeds.  Subsequent calls → silent (consent on file).
    Prior explicit revoke → return False (re-grant requires fresh
    user action via settings UI)."""

    def setup_method(self):
        # In-memory SQLite + UserConsent table
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from integrations.social.models import Base
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_cloud_egress_in_consent_types(self):
        from integrations.social.consent_service import CONSENT_TYPES
        assert 'cloud_egress' in CONSENT_TYPES

    def test_first_call_auto_grants_returns_true(self):
        from integrations.social.consent_service import ConsentService
        db = self.Session()
        try:
            granted = ConsentService.auto_grant_with_notice(
                db, 'u_first', 'cloud_egress', scope='vision',
                reason='test reason')
            assert granted is True
            # And subsequent check confirms the grant landed
            assert ConsentService.check_consent(
                db, 'u_first', 'cloud_egress', scope='vision') is True
        finally:
            db.close()

    def test_second_call_silent_returns_true(self):
        from integrations.social.consent_service import ConsentService
        db = self.Session()
        try:
            ConsentService.auto_grant_with_notice(
                db, 'u_second', 'cloud_egress', scope='vision')
            # Second call should also return True without erroring on
            # the "row already exists" path.
            granted2 = ConsentService.auto_grant_with_notice(
                db, 'u_second', 'cloud_egress', scope='vision')
            assert granted2 is True
        finally:
            db.close()

    def test_returns_false_after_explicit_revoke(self):
        """Privacy invariant: if user explicitly revoked, re-grant
        requires fresh user action — auto-grant must NOT silently
        re-grant after a revoke."""
        from integrations.social.consent_service import ConsentService
        db = self.Session()
        try:
            ConsentService.auto_grant_with_notice(
                db, 'u_revoker', 'cloud_egress', scope='vision')
            ConsentService.revoke_consent(
                db, 'u_revoker', 'cloud_egress', scope='vision')
            # Now auto_grant_with_notice MUST refuse.
            granted = ConsentService.auto_grant_with_notice(
                db, 'u_revoker', 'cloud_egress', scope='vision',
                reason='attempted re-grant')
            assert granted is False
        finally:
            db.close()


# ═══════════════════════════════════════════════════════════════════
# Commit f840cd93 (Nunba — frontend) — verified via static src grep
# Commit a7addd49 (Nunba) — served_by + node_tier in /chat response
# These are tested in the Nunba repo's own test suite; the HARTOS
# side has nothing to assert beyond the wire-contract tests above.
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# Commit e673d9f — revert(draft): drop history dedup
# ═══════════════════════════════════════════════════════════════════

class TestDraftHistoryNoDedup:
    """User principle: dedup of repeated text dropped legitimate
    repeated turns ("hi" then "hi" again).  The dedup block is removed.
    This test guards against re-introducing it via copy-paste."""

    def test_no_dedup_block_in_dispatch_draft_first(self):
        import integrations.agent_engine.speculative_dispatcher as sd
        src = open(sd.__file__, encoding='utf-8').read()
        # The previous dedup compared content equality — guard against
        # the specific anti-pattern returning.
        assert '== prompt.strip()' not in src, (
            "Draft-history dedup re-introduced.  Users genuinely "
            "repeat themselves — this exact-match heuristic silently "
            "drops legit turns."
        )
