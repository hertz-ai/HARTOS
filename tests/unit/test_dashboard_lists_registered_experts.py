"""The agent dashboard lists the expert agents the A2A skill registry holds.

Regression: DashboardService._get_expert_agents called
``AgentSkillRegistry.get_instance()`` and read ``._agents``.  Neither
exists (the singleton is the module-level ``skill_registry`` and its map
is ``.agents``); the AttributeError was swallowed, so the live dashboard
(GET /api/social/dashboard/agents) returned 825 rows and ZERO
``expert_agent`` rows while 96 experts were registered for delegation.

Contract pinned here:
  * an expert appears iff it is in the ExpertAgentRegistry catalog AND
    registered in the skill registry (registration is what makes it
    delegatable by ``a2a_context.delegate_task``);
  * non-expert skill-registry agents (assistant/helper/marketing_<uid>)
    are NOT labelled ``expert_agent``;
  * the row carries the catalog name and the registry's skill names.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.expert_agents.registry import ExpertAgentRegistry
from integrations.internal_comm import internal_agent_communication as iac
from integrations.social.dashboard_service import DashboardService


@pytest.fixture
def fresh_registry(monkeypatch):
    """Swap the process singleton for an empty one (no cross-test state)."""
    reg = iac.AgentSkillRegistry()
    monkeypatch.setattr(iac, 'skill_registry', reg)
    return reg


def _one_expert():
    catalog = ExpertAgentRegistry().agents
    agent_id = sorted(catalog)[0]
    return agent_id, catalog[agent_id]


def test_registered_expert_is_listed_with_catalog_name_and_skills(fresh_registry):
    agent_id, expert = _one_expert()
    fresh_registry.register_agent(agent_id, [
        {'name': 'livetest_dash_skill_a', 'proficiency': 0.8},
        {'name': 'livetest_dash_skill_b', 'proficiency': 0.6},
    ])

    rows = DashboardService._get_expert_agents()

    assert len(rows) == 1
    row = rows[0]
    assert row['id'] == f'expert_{agent_id}'
    assert row['type'] == 'expert_agent'
    assert row['name'] == expert.name
    assert row['status'] == 'available'
    assert sorted(row['skills']) == ['livetest_dash_skill_a',
                                     'livetest_dash_skill_b']
    assert row['metrics']['accuracy'] == pytest.approx(0.7)


def test_non_expert_skill_registry_agents_are_not_experts(fresh_registry):
    fresh_registry.register_agent('livetest_dash_assistant', [
        {'name': 'task_coordination', 'proficiency': 0.95}])
    fresh_registry.register_agent('marketing_livetest_dash', [
        {'name': 'marketing', 'proficiency': 0.9}])

    assert DashboardService._get_expert_agents() == []


def test_catalog_expert_not_registered_is_not_available(fresh_registry):
    # Empty registry: nothing is delegatable, so nothing is 'available'.
    assert DashboardService._get_expert_agents() == []


def test_all_registered_experts_reach_the_dashboard(fresh_registry):
    """End to end through get_dashboard with the real registration path."""
    catalog = ExpertAgentRegistry().agents
    # Same skill shape register_all_experts writes (it is idempotent via a
    # module flag, so it is not re-callable against a fresh registry).
    for agent_id, expert in catalog.items():
        fresh_registry.register_agent(agent_id, [
            {'name': c.name, 'description': c.description,
             'proficiency': expert.reliability}
            for c in expert.capabilities])
    fresh_registry.register_agent('livetest_dash_helper', [
        {'name': 'tool_execution', 'proficiency': 1.0}])

    db = _EmptyDB()
    data = DashboardService.get_dashboard(db)

    experts = [a for a in data['agents'] if a['type'] == 'expert_agent']
    assert len(experts) == len(catalog)
    assert data['summary']['by_type'].get('expert_agent') == len(catalog)
    assert 'expert_livetest_dash_helper' not in {a['id'] for a in experts}


class _EmptyDB:
    """Session stand-in whose tables are empty (goal/user queries -> [])."""

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        return []
