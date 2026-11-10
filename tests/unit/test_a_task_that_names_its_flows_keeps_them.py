"""A task that names its own flows is planned as those flows, one per flow.

Measured 2026-11-03: the owner's "Speech therapy Agent Flows" note (seven
named flows; Flow 1 "Name calling Response Improvement") went into the
desktop's autonomous CREATE verbatim (msg_1793687224401_create), and round 0
was already ONE flow 'main' of 9 steps with the seven flows folded into one
sentence of extra_information.  The plan author's prompt gave an exact shape
with a single flow and rules for flows[0] only; the reviewer saw the plan cut
at 6000 characters; the oscillation guard counted flows[0] only.  The owner:
the speech agent must start by getting the child's attention (Flow 1).

Real gather_agentdetails.create_agents_for_user and the real
hart_intelligence_entry._review_proposed_plan (the LLM call recorded at
pooled_post).  The guard's count is pinned in test_plan_oscillation.py.

    python -m pytest tests/unit/test_a_task_that_names_its_flows_keeps_them.py -q
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

FLOW_NAMES = ['Name calling response', 'Family member association', 'Flash cards',
              'Reverse flash cards', 'Syllable count', 'Echolalia answer only',
              'Concept of time']
NOTE = ('Speech therapy Agent Flows. '
        + ' '.join(f'Flow {i}: {n}.' for i, n in enumerate(FLOW_NAMES, 1)))


def _seven_flow_plan():
    """A plan the size the desktop's real plans run to: nine steps a flow,
    each a tool call of about 90 characters."""
    step = ('send_message_to_user: say the word slowly in the language spoken at home, '
            'then wait for the attempt')
    return {'status': 'proposed_plan', 'name': 'Speech Therapy', 'agent_name': 'speech.coach',
            'goal': 'The child responds to their name, then to words', 'broadcast_agent': False,
            'personas': [{'name': 'Executor', 'description': 'speaks to the child'}],
            'flows': [{'flow_name': n, 'persona': 'Executor', 'sub_goal': n,
                       'actions': [f'{step} ({n} {k})' for k in range(9)]} for n in FLOW_NAMES],
            'extra_information': '', 'review_required': True}


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def reviewer(hie, monkeypatch):
    """The reviewer's LLM call: records the prompt and answers ``verdict``."""
    seen = SimpleNamespace(prompts=[], verdict='{"status":"approved"}')

    def pooled_post(url, json=None, **_k):
        seen.prompts.append(json['messages'][0]['content'])
        content = seen.verdict
        return SimpleNamespace(json=lambda: {'choices': [{'message': {'content': content}}]})

    monkeypatch.setattr(hie, 'pooled_post', pooled_post)
    return seen


def _plan_in(prompt):
    head, rest = prompt.split('Plan to review (JSON):\n', 1)
    return rest.split('\n\nRespond with ONLY a JSON object', 1)[0]


def test_the_reviewer_reads_a_seven_flow_plan_whole(hie, reviewer):
    plan = _seven_flow_plan()
    assert hie._review_proposed_plan(plan, 3) == ('approved', None)
    seen = json.loads(_plan_in(reviewer.prompts[0]))         # not cut mid-JSON
    assert [f['flow_name'] for f in seen['flows']] == FLOW_NAMES
    assert all(len(f['actions']) == 9 for f in seen['flows'])


def test_the_flow_the_reviewer_names_travels_with_its_feedback(hie, reviewer):
    reviewer.verdict = json.dumps({'status': 'needs_refinement',
                                   'flow_name': 'Name calling response',
                                   'feedback': 'step 3 must wait for the child'})
    assert hie._review_proposed_plan(_seven_flow_plan(), 3) == (
        'needs_refinement', "In flow 'Name calling response': step 3 must wait for the child")
    assert '"flow_name"' in reviewer.prompts[0].split('Respond with ONLY a JSON object', 1)[1]

    reviewer.verdict = json.dumps({'status': 'needs_refinement', 'feedback': 'tighten step 2'})
    assert hie._review_proposed_plan(_seven_flow_plan(), 3) == ('needs_refinement',
                                                                'tighten step 2')


def test_the_plan_author_keeps_each_named_flow():
    """The words the plan author is given: a task that names its flows gets
    one flow per named flow, in order, never folded into one; the step rules
    and the refinement apply per flow."""
    from hartos import gather_agentdetails as ga
    assistant, _proxy = ga.create_agents_for_user('u-flows', autonomous=True,
                                                  initial_description=NOTE)
    words = assistant.system_message
    assert NOTE in words
    assert 'ONE entry per named flow, in the order given' in words
    assert 'never fold\nthe named flows into one flow or into extra_information' in words
    # Each flow keeps the party the task gives it (owner, 2026-11-03: "each
    # flow also has a persona association"); REUSE picks a flow by persona.
    assert 'Each flow names\nits own persona' in words
    assert '"personas" lists every\npersona a flow names' in words
    assert 'never the agent itself' in words
    # REUSE moves a person to their next flow when this one's sub_goal is met
    # (owner 2026-11-03, "When its goal is met"), so it must be checkable.
    assert "Each flow's sub_goal is a result one turn can show" in words
    assert 'RULES for the actions[] of EACH flow' in words
    assert 'RULES for flows[0].actions[]' not in words
    assert 'apply the feedback to the actions[] of the flow it names' in words
