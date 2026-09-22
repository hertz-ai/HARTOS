"""Baseline collection must use only genuinely instrumented participants."""

import unittest
from unittest.mock import patch

from integrations.agent_engine.agent_baseline_service import AgentBaselineService
from integrations.agent_lightning import (
    recipe_assistant_agent_id, recipe_assistant_agent_ids)


class TestLightningMetrics(unittest.TestCase):
    def test_recipe_instrumentation_and_baseline_share_agent_id_builder(self):
        self.assertEqual(recipe_assistant_agent_ids('session'), [
            recipe_assistant_agent_id('create', 'session'),
            recipe_assistant_agent_id('reuse', 'session'),
        ])

    def test_collects_only_the_real_create_and_reuse_participants(self):
        session = 'user_42_prompt_7'
        create_id = f'create_recipe_assistant_{session}'
        reuse_id = f'reuse_recipe_assistant_{session}'
        seen = []

        class Store:
            def __init__(self, agent_id, backend):
                seen.append((agent_id, backend))
                self.agent_id = agent_id

            def list_spans(self, limit):
                return {
                    create_id: [{
                        'status': 'success', 'duration': 2,
                        'events': [{'type': 'reward', 'data': {'reward': 1}}],
                    }],
                    reuse_id: [{
                        'status': 'error', 'duration': 4,
                        'events': [{'type': 'reward', 'data': {'reward': -0.5}}],
                    }],
                }.get(self.agent_id, [])

        with patch('integrations.agent_lightning.is_enabled', return_value=True), \
             patch('integrations.agent_lightning.LightningStore', Store):
            metrics = AgentBaselineService._collect_lightning_metrics('7', session)

        self.assertEqual({agent_id for agent_id, _ in seen}, {create_id, reuse_id})
        self.assertEqual(metrics['execution_count'], 2)
        self.assertEqual(metrics['error_rate'], 0.5)
        self.assertEqual(metrics['per_agent'][create_id]['avg_reward'], 1.0)
        self.assertEqual(metrics['per_agent'][reuse_id]['avg_reward'], -0.5)

    def test_reward_trend_is_chronological_across_both_agent_stores(self):
        session = 'user_7_prompt_9'
        create_id = f'create_recipe_assistant_{session}'
        reuse_id = f'reuse_recipe_assistant_{session}'

        def span(agent_id, when, reward):
            return {
                'agent_id': agent_id,
                'status': 'success',
                'start_time': when,
                'events': [{
                    'type': 'reward', 'timestamp': when,
                    'data': {'reward': reward},
                }],
            }

        class Store:
            def __init__(self, agent_id, backend):
                self.agent_id = agent_id

            def list_spans(self, limit):
                # The real store returns newest-first.  CREATE and REUSE are
                # also separate lists, so concatenating them reverses and
                # groups time instead of describing improvement over time.
                values = {
                    create_id: [span(create_id, t, -1.0)
                                for t in (9, 7, 5, 3, 1)],
                    reuse_id: [span(reuse_id, t, 1.0)
                               for t in (10, 8, 6, 4, 2)],
                }
                return values.get(self.agent_id, [])

        with patch('integrations.agent_lightning.is_enabled', return_value=True), \
             patch('integrations.agent_lightning.LightningStore', Store):
            metrics = AgentBaselineService._collect_lightning_metrics('9', session)

        # Earlier samples average below later samples only when the two stores
        # are merged by timestamp.  Store order alone reported "stable".
        self.assertEqual(metrics['reward_trend'], 'improving')
