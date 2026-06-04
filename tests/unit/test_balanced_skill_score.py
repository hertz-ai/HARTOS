"""The 'balanced' agent-selection strategy has ONE source: _balanced_skill_score (#98d).

Before this refactor the identical 40/25/20/15 composite was inlined twice —
in AgentSkillRegistry.find_agents_with_skill and A2AContextExchange._score_agent.
Two copies of a scoring formula always drift. These behavioural tests drive the
REAL objects and assert both call sites route through the single helper:

  * the helper computes the documented composite (hand-checked numbers);
  * find_agents_with_skill(strategy='balanced') ranks by the helper — proven by
    monkeypatching the helper to an inverted ranking and watching the sort flip;
  * _score_agent(strategy='balanced') returns the helper's value — proven the
    same way.

If either site ever re-inlines the formula, the monkeypatch stops reaching it
and the corresponding test fails. No grep tests.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.internal_comm import internal_agent_communication as iac
from integrations.internal_comm.internal_agent_communication import (
    AgentSkill, AgentSkillRegistry, A2AContextExchange, _balanced_skill_score)


def _skill(name, proficiency, latency_ms=0.0, cost_spark=0.0):
    return AgentSkill(name=name, description='d', proficiency=proficiency,
                      avg_latency_ms=latency_ms, avg_cost_spark=cost_spark)


def test_helper_computes_documented_composite():
    # latency 30s -> norm 0.5 (half the 60s cap); cost 50 -> norm 0.5 (half 100);
    # usage_count 0 -> success_rate falls back to proficiency.
    s = _skill('x', proficiency=0.8, latency_ms=30000.0, cost_spark=50.0)
    expected = 0.40 * 0.8 + 0.25 * 0.8 + 0.20 * 0.5 + 0.15 * 0.5
    assert abs(_balanced_skill_score(s) - expected) < 1e-9


def test_helper_defaults_when_no_telemetry():
    # latency 0 and cost 0 -> both norms 0.5; usage 0 -> rate == proficiency.
    s = _skill('y', proficiency=0.6)
    expected = 0.40 * 0.6 + 0.25 * 0.6 + 0.20 * 0.5 + 0.15 * 0.5
    assert abs(_balanced_skill_score(s) - expected) < 1e-9


def test_registry_balanced_ranks_by_helper(monkeypatch):
    reg = AgentSkillRegistry()
    # Two agents whose ACCURACY order (by proficiency) is alice > bob.
    # register_agent takes skill *dicts*, not AgentSkill objects.
    reg.register_agent('alice', [{'name': 'translate', 'proficiency': 0.9}])
    reg.register_agent('bob', [{'name': 'translate', 'proficiency': 0.5}])

    # Real helper: higher proficiency wins -> alice first.
    order = [aid for aid, _ in reg.find_agents_with_skill('translate', strategy='balanced')]
    assert order == ['alice', 'bob']

    # Invert the SINGLE source: now lower proficiency must rank first. If the
    # registry had re-inlined the formula, this patch wouldn't reach it.
    monkeypatch.setattr(iac, '_balanced_skill_score', lambda skill: -skill.proficiency)
    flipped = [aid for aid, _ in reg.find_agents_with_skill('translate', strategy='balanced')]
    assert flipped == ['bob', 'alice'], "balanced sort did not route through _balanced_skill_score"


def test_score_agent_balanced_returns_helper_value(monkeypatch):
    comm = A2AContextExchange(skill_registry=MagicMock())
    skill = _skill('translate', proficiency=0.7, latency_ms=12000.0, cost_spark=20.0)
    agent_skills = {'translate': skill}

    got = comm._score_agent(agent_skills, ['translate'], 'balanced')
    assert abs(got - _balanced_skill_score(skill)) < 1e-9

    # Single-source proof: redirect the helper to a sentinel and watch the
    # composite (single required skill -> average == that one score) follow.
    monkeypatch.setattr(iac, '_balanced_skill_score', lambda s: 0.123)
    assert abs(comm._score_agent(agent_skills, ['translate'], 'balanced') - 0.123) < 1e-9
