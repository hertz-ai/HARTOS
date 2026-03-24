"""
test_threadlocal.py - Tests for threadlocal.py

Tests the thread-local data store used by every /chat request.
Each test verifies a specific isolation guarantee or data lifecycle:

FT: Set/get for all fields (request_id, user_id, token counts, intents,
    creation signals, agentic routing, model config, task source).
NFT: Thread isolation (two threads see different values), default values
     when unset, clear operations, concurrent access safety.
"""
import os
import sys
import threading

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from threadlocal import ThreadLocalData


# ============================================================
# Basic set/get — each field must round-trip correctly
# ============================================================

class TestBasicSetGet:
    """Every field must survive set→get without corruption."""

    def test_request_id(self):
        tld = ThreadLocalData()
        tld.set_request_id('req-123')
        assert tld.get_request_id() == 'req-123'

    def test_user_id(self):
        tld = ThreadLocalData()
        tld.set_user_id('user-456')
        assert tld.get_user_id() == 'user-456'

    def test_prompt_id(self):
        tld = ThreadLocalData()
        tld.set_prompt_id('prompt-789')
        assert tld.get_prompt_id() == 'prompt-789'

    def test_reqid_list(self):
        tld = ThreadLocalData()
        tld.set_reqid_list([1, 2, 3])
        assert tld.get_reqid_list() == [1, 2, 3]

    def test_global_intent(self):
        tld = ThreadLocalData()
        tld.set_global_intent('FINAL_ANSWER')
        assert tld.get_global_intent() == 'FINAL_ANSWER'

    def test_task_source(self):
        tld = ThreadLocalData()
        tld.set_task_source('hive')
        assert tld.get_task_source() == 'hive'


# ============================================================
# Default values — unset fields must return safe defaults
# ============================================================

class TestDefaults:
    """Unset fields must return None or safe defaults — not crash."""

    def test_request_id_default_none(self):
        tld = ThreadLocalData()
        assert tld.get_request_id() is None

    def test_user_id_default_none(self):
        tld = ThreadLocalData()
        assert tld.get_user_id() is None

    def test_reqid_list_default_empty(self):
        tld = ThreadLocalData()
        assert tld.get_reqid_list() == []

    def test_task_source_default_own(self):
        """Default source is 'own' — user-initiated, not daemon."""
        tld = ThreadLocalData()
        assert tld.get_task_source() == 'own'

    def test_creation_requested_default_false(self):
        tld = ThreadLocalData()
        assert tld.get_creation_requested() is False

    def test_agentic_requested_default_false(self):
        tld = ThreadLocalData()
        assert tld.get_agentic_requested() is False

    def test_model_config_override_default_none(self):
        tld = ThreadLocalData()
        assert tld.get_model_config_override() is None


# ============================================================
# Token counts — used for billing/budget tracking
# ============================================================

class TestTokenCounts:
    """Token counts drive the Spark budget system — wrong counts = wrong billing."""

    def test_set_and_get_req_tokens(self):
        tld = ThreadLocalData()
        tld.set_req_token_count(100)
        assert tld.get_req_token_count() == 100

    def test_update_req_tokens_increments(self):
        tld = ThreadLocalData()
        tld.set_req_token_count(100)
        tld.update_req_token_count(50)
        assert tld.get_req_token_count() == 150

    def test_set_and_get_res_tokens(self):
        tld = ThreadLocalData()
        tld.set_res_token_count(200)
        assert tld.get_res_token_count() == 200

    def test_update_res_tokens_increments(self):
        tld = ThreadLocalData()
        tld.set_res_token_count(200)
        tld.update_res_token_count(100)
        assert tld.get_res_token_count() == 300


# ============================================================
# Intent recognition — drives response routing
# ============================================================

class TestIntents:
    """Recognized intents determine whether response is chat, action, or creation."""

    def test_set_and_get_intents(self):
        tld = ThreadLocalData()
        tld.set_recognize_intents()
        assert tld.get_recognize_intents() == []

    def test_update_intents_appends(self):
        tld = ThreadLocalData()
        tld.set_recognize_intents()
        tld.update_recognize_intents('FINAL_ANSWER')
        tld.update_recognize_intents('CREATE_AGENT')
        intents = tld.get_recognize_intents()
        assert 'FINAL_ANSWER' in intents
        assert 'CREATE_AGENT' in intents

    def test_update_intents_auto_initializes(self):
        """update_recognize_intents must work even without prior set_recognize_intents."""
        tld = ThreadLocalData()
        tld.update_recognize_intents('TEST')
        assert 'TEST' in tld.get_recognize_intents()


# ============================================================
# Agent creation signals — LangChain→autogen bridge
# ============================================================

class TestCreationSignals:
    """Creation signals tell hart_intelligence_entry to switch from LangChain to autogen."""

    def test_set_creation_requested(self):
        tld = ThreadLocalData()
        tld.set_creation_requested(description='Build a bot', autonomous=True)
        assert tld.get_creation_requested() is True
        assert tld.get_creation_description() == 'Build a bot'
        assert tld.get_creation_autonomous() is True

    def test_clear_creation_flags(self):
        tld = ThreadLocalData()
        tld.set_creation_requested(description='test', autonomous=True)
        tld.clear_creation_flags()
        assert tld.get_creation_requested() is False
        assert tld.get_creation_description() is None
        assert tld.get_creation_autonomous() is False


# ============================================================
# Agentic routing — LangChain→autogen plan execution
# ============================================================

class TestAgenticRouting:
    """Agentic routing signals trigger the Plan Mode→Execute flow."""

    def test_set_agentic_routing(self):
        tld = ThreadLocalData()
        tld.set_agentic_routing(
            task_description='Deploy the app',
            plan_steps=['step1', 'step2'],
            matched_agent_id='agent_42')
        assert tld.get_agentic_requested() is True
        assert tld.get_agentic_task_description() == 'Deploy the app'
        assert tld.get_agentic_plan_steps() == ['step1', 'step2']
        assert tld.get_agentic_matched_agent_id() == 'agent_42'

    def test_clear_agentic_flags(self):
        tld = ThreadLocalData()
        tld.set_agentic_routing(task_description='test')
        tld.clear_agentic_flags()
        assert tld.get_agentic_requested() is False
        assert tld.get_agentic_plan_steps() == []


# ============================================================
# Model config override — speculative execution
# ============================================================

class TestModelConfigOverride:
    """Per-request model override — daemon uses different model than user chat."""

    def test_set_and_get(self):
        tld = ThreadLocalData()
        config = [{'model': 'gpt-4o', 'api_key': 'test'}]
        tld.set_model_config_override(config)
        assert tld.get_model_config_override() == config

    def test_clear(self):
        tld = ThreadLocalData()
        tld.set_model_config_override([{'model': 'test'}])
        tld.clear_model_config_override()
        assert tld.get_model_config_override() is None


# ============================================================
# Thread isolation — CRITICAL: each thread sees its own data
# ============================================================

class TestThreadIsolation:
    """Two concurrent requests must NOT see each other's data."""

    def test_different_threads_isolated(self):
        tld = ThreadLocalData()
        results = {}

        def worker(thread_id, user_id):
            tld.set_user_id(user_id)
            tld.set_request_id(f'req-{thread_id}')
            # Small sleep to increase chance of interleaving
            import time
            time.sleep(0.01)
            results[thread_id] = {
                'user_id': tld.get_user_id(),
                'request_id': tld.get_request_id(),
            }

        t1 = threading.Thread(target=worker, args=(1, 'alice'))
        t2 = threading.Thread(target=worker, args=(2, 'bob'))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Each thread must see its OWN data, not the other's
        assert results[1]['user_id'] == 'alice'
        assert results[1]['request_id'] == 'req-1'
        assert results[2]['user_id'] == 'bob'
        assert results[2]['request_id'] == 'req-2'

    def test_ten_threads_isolated(self):
        """Stress test: 10 concurrent requests must all be isolated."""
        tld = ThreadLocalData()
        results = {}
        errors = []

        def worker(tid):
            try:
                tld.set_user_id(f'user-{tid}')
                tld.set_request_id(f'req-{tid}')
                tld.set_task_source(f'source-{tid}')
                import time
                time.sleep(0.005)
                uid = tld.get_user_id()
                rid = tld.get_request_id()
                src = tld.get_task_source()
                if uid != f'user-{tid}' or rid != f'req-{tid}' or src != f'source-{tid}':
                    errors.append(f'Thread {tid}: expected user-{tid}/req-{tid}/source-{tid}, '
                                  f'got {uid}/{rid}/{src}')
                results[tid] = True
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread isolation failures: {errors}"
        assert len(results) == 10
