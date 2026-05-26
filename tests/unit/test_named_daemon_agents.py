"""Invariants for the 5 named daemon agents (Atlas/Sage/Scout/Echo/Herald).

The Nunba admin UI (landing-page/src/components/Social/Agents/AgentAuditPage.jsx)
queries /audit/agents -> DashboardService._get_agent_goals -> AgentGoal rows.
These tests guard the properties the UI depends on:
  - 5 persona slugs seeded
  - Each carries a persona_kind the UI can group by
  - goal_type maps to an existing registered type (no new prompt builder
    needed — DRY)
  - Names, cadences, audiences stable so the UI / product docs don't drift
"""

import unittest

from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS


PERSONAS = {
    'bootstrap_atlas_money_friend': {
        'name': 'Atlas',
        'kind': 'money-friend',
        'goal_type': 'finance',
        'audience': 'self',
        'cadence': 'weekly',
    },
    'bootstrap_sage_math_friend': {
        'name': 'Sage',
        'kind': 'math-friend',
        'goal_type': 'thought_experiment',
        'audience': 'self',
        'cadence': 'event',
    },
    'bootstrap_scout_safety_friend': {
        'name': 'Scout',
        'kind': 'safety-friend',
        'goal_type': 'ip_protection',
        'audience': 'self',
        'cadence': 'event',
    },
    'bootstrap_echo_marketing_intern': {
        'name': 'Echo',
        'kind': 'marketing-intern',
        'goal_type': 'marketing',
        'audience': 'developers',
        'cadence': 'weekly',
    },
    'bootstrap_herald_ml_intern': {
        'name': 'Herald',
        'kind': 'ml-intern',
        'goal_type': 'news',
        'audience': 'developers',
        'cadence': 'weekly',
    },
    'bootstrap_quest_contest_host': {
        'name': 'Quest',
        'kind': 'contest-host',
        'goal_type': 'marketing',
        'audience': 'developers',
        'cadence': 'weekly',
    },
}


def _by_slug():
    return {g['slug']: g for g in SEED_BOOTSTRAP_GOALS}


class PersonaInvariants(unittest.TestCase):
    """Each of the 5 named agents is present with the expected shape."""

    def test_all_five_slugs_present(self):
        seeds = _by_slug()
        for slug in PERSONAS:
            self.assertIn(slug, seeds, f'Missing seed slug: {slug}')

    def test_titles_are_persona_names(self):
        seeds = _by_slug()
        for slug, expected in PERSONAS.items():
            self.assertEqual(seeds[slug]['title'], expected['name'])

    def test_persona_kind_set_in_config(self):
        seeds = _by_slug()
        for slug, expected in PERSONAS.items():
            cfg = seeds[slug].get('config', {})
            self.assertEqual(
                cfg.get('persona_kind'), expected['kind'],
                f'{slug} missing or wrong persona_kind',
            )

    def test_persona_name_echoed_in_config(self):
        seeds = _by_slug()
        for slug, expected in PERSONAS.items():
            cfg = seeds[slug].get('config', {})
            self.assertEqual(cfg.get('persona_name'), expected['name'])

    def test_goal_type_reuses_existing_prompt_builder(self):
        """No new prompt builder needed — every persona maps to an
        already-registered goal_type. Keeps the addition DRY."""
        from integrations.agent_engine.goal_manager import get_registered_types
        registered = set(get_registered_types())
        seeds = _by_slug()
        for slug, expected in PERSONAS.items():
            gt = seeds[slug]['goal_type']
            self.assertEqual(gt, expected['goal_type'])
            self.assertIn(
                gt, registered,
                f'{slug} uses goal_type={gt!r} which is not registered — '
                f'add register_goal_type() or pick an existing one',
            )

    def test_audience_classification(self):
        """UI filters on audience — confirm the split is right so the
        user's feed doesn't get dev-facing posts or vice versa."""
        seeds = _by_slug()
        for slug, expected in PERSONAS.items():
            cfg = seeds[slug].get('config', {})
            self.assertEqual(cfg.get('audience'), expected['audience'])

    def test_cadence_present(self):
        seeds = _by_slug()
        for slug, expected in PERSONAS.items():
            cfg = seeds[slug].get('config', {})
            self.assertEqual(cfg.get('cadence'), expected['cadence'])

    def test_autonomous_and_continuous(self):
        """All 5 run as long-lived daemon agents, never paused by default."""
        seeds = _by_slug()
        for slug in PERSONAS:
            cfg = seeds[slug].get('config', {})
            self.assertTrue(cfg.get('autonomous'), f'{slug} not autonomous')
            self.assertTrue(cfg.get('continuous'), f'{slug} not continuous')

    def test_safety_priority_beats_money(self):
        """Scout (safety) outranks Atlas (money) — safety friend should
        preempt the money friend when they disagree."""
        seeds = _by_slug()
        scout_p = seeds['bootstrap_scout_safety_friend']['config']['priority']
        atlas_p = seeds['bootstrap_atlas_money_friend']['config']['priority']
        self.assertGreater(scout_p, atlas_p)

    def test_budgets_are_modest(self):
        """Daemon personas run on small budgets — they observe and
        post, they don't launch expensive cloud runs.  Keeps the
        aggregate Spark draw bounded on flat-tier nodes."""
        seeds = _by_slug()
        for slug in PERSONAS:
            budget = seeds[slug]['spark_budget']
            self.assertLessEqual(budget, 200, f'{slug} budget too high: {budget}')


class PromptSafety(unittest.TestCase):
    """Descriptions carry the no-parallel-path discipline so the
    prompt builder inherits the guardrail."""

    def test_atlas_cites_canonical_sources(self):
        seeds = _by_slug()
        desc = seeds['bootstrap_atlas_money_friend']['description']
        # Must reference the single sources of truth — never invent accounting
        self.assertIn('revenue_aggregator', desc)
        self.assertIn('budget_gate', desc)

    def test_scout_uses_existing_approval_path(self):
        seeds = _by_slug()
        desc = seeds['bootstrap_scout_safety_friend']['description']
        self.assertIn('action_classifier', desc)
        self.assertIn('PREVIEW_PENDING', desc)

    def test_echo_cites_source_file_instead_of_hyping(self):
        seeds = _by_slug()
        desc = seeds['bootstrap_echo_marketing_intern']['description']
        self.assertIn('source file', desc)

    def test_herald_honest_about_regressions(self):
        seeds = _by_slug()
        desc = seeds['bootstrap_herald_ml_intern']['description']
        self.assertIn('regression', desc)


if __name__ == '__main__':
    unittest.main()
