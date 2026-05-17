"""Integration test — is WorldModelBridge training actually real?

From the ml_intern brief §2.2 question #1:
    "Do gradient updates actually run?  record_interaction → in-process
    provider.create_chat_completion(model='hevolve-interaction-replay',
    max_tokens=1, temperature=0).  Is that provider method internally
    invoking optimizer.step() on TemporalCoherence + OrthogonalLoRA
    parameters for the passed experience, or is it buffering-only for
    TensorBoard vanity metrics?"

This test fires `record_interaction` 100 times and asserts the
in-process provider's `create_chat_completion` was called with the
`hevolve-interaction-replay` model for each one.

We do NOT assert on actual weight-hash changes because:
  1. HevolveAI's OrthogonalLoRA lives in a sibling repo and isn't
     guaranteed to be importable in the HARTOS test env.
  2. The gradient side effect happens inside HevolveAI's
     distillation_engine on a background thread — hard to deterministically
     synchronize from this side of the bridge.

What we CAN verify here:
  - record_interaction → _flush_to_world_model is wired end-to-end.
  - Every flushed experience reaches provider.create_chat_completion.
  - The model string matches the `hevolve-interaction-replay` contract.
  - ConstitutionalFilter silently drops experiences whose response
    contains a violation pattern, matching brief §2.1 row 4.

Tests that assert on actual gradient updates belong in a HevolveAI
test (where the gradient code lives) — this test guards the HARTOS
↔ HevolveAI boundary contract.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

import pytest

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'),
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine import world_model_bridge as wmb


@pytest.fixture
def in_process_bridge(monkeypatch):
    """Construct a fresh bridge with an in-process stub provider.

    We don't monkey-patch the singleton because other tests might
    share it; instead we build a brand-new WorldModelBridge and
    inject the stub.
    """
    # Keep the init quiet — no HTTP, no crawl watcher, no real provider
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    monkeypatch.setenv('HEVOLVEAI_API_URL', '')
    monkeypatch.setenv('NUNBA_BUNDLED', '')

    # Prevent any real in-process init in __init__ — we'll set the
    # provider manually.
    monkeypatch.setattr(
        wmb.WorldModelBridge, '_init_in_process', lambda self: None,
    )
    # Avoid starting the crawl integrity watcher thread.
    monkeypatch.setattr(
        wmb.WorldModelBridge,
        '_start_crawl_integrity_watcher',
        lambda self: None,
    )

    bridge = wmb.WorldModelBridge()

    # Shrink the batch so every call flushes promptly
    bridge._flush_batch_size = 1
    bridge._in_process = True

    # Stub provider that records every create_chat_completion call
    calls = []

    class _StubProvider:
        def create_chat_completion(self, messages, model,
                                   temperature, max_tokens):
            calls.append({
                'messages': messages,
                'model': model,
                'temperature': temperature,
                'max_tokens': max_tokens,
            })
            return {
                'id': 'stub',
                'choices': [
                    {'message': {'role': 'assistant', 'content': 'ok'}},
                ],
            }

    bridge._provider = _StubProvider()
    bridge._hive_mind = None
    return bridge, calls


def test_record_interaction_flushes_to_provider(in_process_bridge):
    """Happy path: 1 interaction → 1 provider call with correct model."""
    bridge, calls = in_process_bridge
    bridge.record_interaction(
        user_id='u1', prompt_id='p1',
        prompt='hello hive', response='hello human',
        model_id='qwen-3.5-4b',
    )
    # Flush happens synchronously on the executor — poll for up to 2s
    deadline = time.time() + 2
    while time.time() < deadline and not calls:
        time.sleep(0.05)
    assert len(calls) == 1, f'expected 1 provider call, got {len(calls)}'
    call = calls[0]
    assert call['model'] == 'hevolve-interaction-replay'
    assert call['temperature'] == 0
    assert call['max_tokens'] == 1
    # Messages carry the canonical 3-part shape: system (metadata),
    # user (prompt), assistant (response)
    roles = [m['role'] for m in call['messages']]
    assert roles == ['system', 'user', 'assistant']
    assert 'hello hive' in call['messages'][1]['content']
    assert 'hello human' in call['messages'][2]['content']


def test_hundred_interactions_each_hit_provider(in_process_bridge):
    """Brief §5-A step 2: fire 100 interactions, every one flushes."""
    bridge, calls = in_process_bridge
    for i in range(100):
        bridge.record_interaction(
            user_id=f'u{i}', prompt_id='bench',
            prompt=f'question number {i}',
            response=f'answer number {i}',
            model_id='qwen-3.5-4b',
        )
    deadline = time.time() + 10
    while time.time() < deadline and len(calls) < 100:
        time.sleep(0.1)
    assert len(calls) == 100, (
        f'expected 100 provider calls, got {len(calls)} — '
        'record_interaction → _flush_to_world_model wiring broken'
    )
    # Every call MUST carry the replay model string — that's the
    # contract the HevolveAI distillation/auto-observe path branches on.
    for c in calls:
        assert c['model'] == 'hevolve-interaction-replay'


def test_constitutional_violation_drops_experience(in_process_bridge):
    """Brief §2.1 row 4: failed-constitutional responses are SILENTLY
    dropped from the training stream — they don't reach the provider."""
    bridge, calls = in_process_bridge

    with mock.patch(
        'security.hive_guardrails.ConstitutionalFilter.check_prompt',
        return_value=(False, 'violation: test pattern'),
    ):
        bridge.record_interaction(
            user_id='u1', prompt_id='p1',
            prompt='ok', response='banned content',
            model_id='qwen-3.5-4b',
        )
    # Give the executor a chance — we expect zero calls
    time.sleep(0.5)
    assert len(calls) == 0
    assert bridge._stats['total_recorded'] == 0


def test_attribution_chain_flows_through(in_process_bridge):
    """When a caller passes attribution_chain, the system message JSON
    carries the structured fields (goal_id etc.).  user_id is
    anonymized by secret_redactor BEFORE reaching the provider — this
    is the privacy contract: the hive trains on interactions but
    never sees a raw user_id."""
    bridge, calls = in_process_bridge
    bridge.record_interaction(
        user_id='u1', prompt_id='p1',
        prompt='q', response='a',
        model_id='qwen',
        goal_id='goal-42',
        attribution_chain={'step': 'decompose', 'credit': 0.8},
    )
    deadline = time.time() + 2
    while time.time() < deadline and not calls:
        time.sleep(0.05)
    assert len(calls) == 1
    import json as _json
    sys_content = _json.loads(calls[0]['messages'][0]['content'])
    assert sys_content['goal_id'] == 'goal-42'
    # Privacy contract: raw user_id MUST be redacted before egress.
    # secret_redactor hashes to a deterministic anon_<hex> token.
    uid = sys_content['user_id']
    assert uid != 'u1', 'raw user_id leaked to provider — privacy breach'
    assert uid.startswith('anon_') or uid == '', (
        f'user_id must be anonymized or cleared, got: {uid!r}'
    )
