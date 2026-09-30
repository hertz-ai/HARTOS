"""Agent Lightning must learn from lifecycle evidence, never a bare reply."""

import unittest

from integrations.agent_lightning.rewards import RewardType
from integrations.agent_lightning.wrapper import AgentLightningWrapper
from integrations.agent_lightning import wrapper as wrapper_module


class _Tracer:
    def __init__(self):
        self.events = []

    def start_span(self, span_type, context=None):
        self.events.append(('start', span_type, context))
        return 'span-1'

    def emit_prompt(self, *args, **kwargs):
        self.events.append(('prompt', args, kwargs))

    def emit_response(self, *args, **kwargs):
        self.events.append(('response', args, kwargs))

    def emit_reward(self, *args, **kwargs):
        self.events.append(('reward', args, kwargs))

    def end_span(self, *args, **kwargs):
        self.events.append(('end', args, kwargs))


class _Rewards:
    def __init__(self):
        self.calls = []

    def calculate_reward(self, reward_type, context):
        self.calls.append((reward_type, context))
        return 1.0


class TestVerifiedTaskOutcomes(unittest.TestCase):
    def _wrapper(self):
        wrapper = AgentLightningWrapper.__new__(AgentLightningWrapper)
        wrapper.tracer = _Tracer()
        wrapper.reward_calculator = _Rewards()
        wrapper.execution_count = 0
        wrapper.current_span_id = None
        return wrapper

    def test_reply_is_traced_but_cannot_award_task_completion(self):
        wrapper = self._wrapper()
        reply = wrapper._wrap_generate_reply(lambda: 'attempted result')

        self.assertEqual(reply(), 'attempted result')
        self.assertEqual(wrapper.reward_calculator.calls, [])
        self.assertTrue(any(event[0] == 'response' for event in wrapper.tracer.events))

    def test_verified_outcome_awards_completion_with_receipt_context(self):
        wrapper = self._wrapper()
        receipt = {'kind': 'tool_receipt', 'message_index': 3}

        wrapper.record_verified_task_outcome(True, {'evidence': receipt})

        self.assertEqual(len(wrapper.reward_calculator.calls), 1)
        reward_type, context = wrapper.reward_calculator.calls[0]
        self.assertEqual(reward_type, RewardType.TASK_COMPLETION)
        self.assertTrue(context['success'])
        self.assertEqual(context['evidence'], receipt)
        self.assertTrue(any(event[0] == 'reward' for event in wrapper.tracer.events))

    def test_groupchat_owner_gets_credit_without_rewarding_inactive_flow(self):
        class Agent:
            pass

        active_agent = Agent()
        inactive_agent = Agent()
        active = self._wrapper()
        inactive = self._wrapper()
        wrapper_module._agent_wrappers[active_agent] = active
        wrapper_module._agent_wrappers[inactive_agent] = inactive

        recorded = wrapper_module.record_verified_outcome_for_agents(
            [active_agent], True, {'evidence': {'kind': 'tool_receipt'}})

        self.assertTrue(recorded)
        self.assertEqual(len(active.reward_calculator.calls), 1)
        self.assertEqual(inactive.reward_calculator.calls, [])

    def test_an_unweakreferenceable_participant_does_not_abort_the_scan(self):
        """A GroupChat may hold a participant that cannot be weak-referenced.

        WeakKeyDictionary.get raises TypeError on those.  Before the guard
        that killed the whole loop, so a real instrumented agent standing
        LATER in group_chat.agents silently lost its credit and the caller
        saw the same False it gets when Lightning is simply off.
        """
        class Agent:
            pass

        real_agent = Agent()
        real = self._wrapper()
        wrapper_module._agent_wrappers[real_agent] = real

        # object() is not weak-referenceable; it stands first in the list.
        recorded = wrapper_module.record_verified_outcome_for_agents(
            [object(), real_agent], True, {'evidence': {'kind': 'tool_receipt'}})

        self.assertTrue(
            recorded,
            'the scan stopped at a participant it could not look up')
        self.assertEqual(len(real.reward_calculator.calls), 1)

    def test_no_instrumented_participant_reports_false(self):
        """False must stay reachable -- the lifecycle logs on it."""
        class Agent:
            pass

        self.assertFalse(
            wrapper_module.record_verified_outcome_for_agents(
                [Agent()], True, {'evidence': {}}))
        self.assertFalse(
            wrapper_module.record_verified_outcome_for_agents(None, True))
