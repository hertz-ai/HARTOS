"""Accuracy of EVERY seeded agent's prompt against the local 4B.

The premise being tested is the steward's: a 4B model is capable of acting
autonomously *if the prompt is proper*. So when an agent fails here the finding
is about the PROMPT, not the model, and the output has to name which agent and
why rather than produce a single number.

This is a RUNTIME measurement, not a source-code test (plan gate B6). It sends
each agent's real `description` from SEED_BOOTSTRAP_GOALS to the real local
model and reads what comes back. Nothing is mocked, and nothing is asserted
about wording.

Why measure this at all: goal descriptions are the entire specification of what
an agent does here. They are prose, they are edited by hand, and nothing checks
that a model can act on them. `register_news_tools` was authored, looked
correct, and reached no tools for weeks because the wiring was keyed on
something else; a prompt that reads well to a human but yields no action is the
same failure one layer up.

RUNNING IT

    HEVOLVE_TEST_LLM_JUDGE=1 HEVOLVE_AGENT_ACCURACY=1 \
        pytest tests/e2e/test_agent_prompt_accuracy.py -s

Both flags are required and the suite SKIPS without them. It talks to a real
model, so it is slow and it costs tokens; that must be an explicit choice, not
something a routine `pytest` run triggers.

WHAT "PASS" MEANS

Judged strictly (`strict=True`), so an unreachable model scores ZERO rather
than falling back to the heuristic. That distinction is the whole point of a
benchmark: a fail-open judge would report 100% while having measured nothing,
which is exactly the shape of the 416 fabricated "PROOF: 0.0%" posts this repo
already published.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import pytest

from tests.e2e.agentic_harness import harness, skip_if_missing


# Both flags on purpose. HEVOLVE_TEST_LLM_JUDGE=1 turns the judge's LLM backend
# on; HEVOLVE_AGENT_ACCURACY=1 says "yes, spend real inference on all ~30
# agents". Neither implies the other.
_ENABLED = (
    os.environ.get('HEVOLVE_TEST_LLM_JUDGE', '0').lower() in ('1', 'true', 'yes')
    and os.environ.get('HEVOLVE_AGENT_ACCURACY', '0').lower() in ('1', 'true', 'yes')
)

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason='agent accuracy benchmark is opt-in: set HEVOLVE_TEST_LLM_JUDGE=1 '
           'and HEVOLVE_AGENT_ACCURACY=1 (it calls the real local model)',
)


def _seeded_agents() -> List[Dict[str, Any]]:
    """Every agent the platform ships, from the one place they are defined."""
    from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
    return [g for g in SEED_BOOTSTRAP_GOALS if (g or {}).get('description')]


def _provider():
    """The same path LLMJudge uses, so there is one way to reach the model."""
    from integrations.agent_engine.world_model_bridge import get_world_model_bridge
    bridge = get_world_model_bridge()
    return getattr(bridge, '_provider', None)


def _ask_model(prompt: str, *, max_tokens: int = 400) -> str:
    """Give the model the agent's brief and ask it to act, not to summarise.

    The system prompt deliberately asks for a PLAN NAMING TOOLS rather than a
    description. An agent brief that produces eloquent prose and no tool call
    is the failure mode worth catching: it is what "completed with no
    side-effects" looks like from the inside.
    """
    provider = _provider()
    if provider is None:
        raise RuntimeError('no local model provider available')
    messages = [
        {'role': 'system', 'content':
            'You are an autonomous agent. You are given your standing brief. '
            'Reply with the concrete steps you would take RIGHT NOW, naming '
            'the specific tool for each step. Do not restate the brief.'},
        {'role': 'user', 'content': prompt},
    ]
    resp = provider.create_chat_completion(
        messages=messages, model='hevolve-agent-accuracy',
        temperature=0, max_tokens=max_tokens,
    )
    try:
        return resp['choices'][0]['message']['content'] or ''
    except Exception:
        return ''


_RUBRIC = (
    'Does this response lay out concrete, executable steps for the brief, '
    'naming specific tools or actions? Score 1.0 for a clear ordered plan '
    'whose steps could be executed as written. Score 0.0 for a restatement '
    'of the brief, a refusal, or vague intentions with no named action.'
)


@pytest.fixture(scope='module')
def agents():
    skip_if_missing('integrations.agent_engine.goal_seeding')
    found = _seeded_agents()
    if not found:
        pytest.skip('no seeded agents with descriptions')
    return found


def test_every_seeded_agent_prompt_is_actionable_by_the_local_model(agents):
    """Run all of them, report per-agent, then assert on the aggregate.

    Deliberately ONE test over all agents rather than a parametrised case per
    agent: the useful artefact is the table, and a per-agent test would stop at
    the first failure and tell you nothing about the other twenty-nine.
    """
    if _provider() is None:
        pytest.fail(
            'no local model provider reachable. This benchmark FAILS rather '
            'than skips here on purpose: reporting an accuracy number without '
            'a model is worse than reporting nothing.'
        )

    results: List[Dict[str, Any]] = []
    with harness() as h:
        for goal in agents:
            slug = goal.get('slug', '?')
            started = time.time()
            try:
                reply = _ask_model(goal['description'])
                error = ''
            except Exception as exc:
                reply, error = '', str(exc)

            if error:
                verdict_passed, score, reason = False, 0.0, f'model call failed: {error}'
            else:
                v = h.judge.judge(
                    reply,
                    rubric=_RUBRIC,
                    min_len=40,
                    strict=True,
                )
                verdict_passed, score, reason = v.passed, v.score, v.reason

            results.append({
                'slug': slug,
                'goal_type': goal.get('goal_type', '?'),
                'passed': verdict_passed,
                'score': round(float(score), 3),
                'reason': reason[:120],
                'reply_chars': len(reply),
                'seconds': round(time.time() - started, 1),
            })

    passed = [r for r in results if r['passed']]
    accuracy = len(passed) / float(len(results)) if results else 0.0

    # Printed, not just asserted. The table is the deliverable: it says WHICH
    # briefs the model cannot act on, which is the actionable part.
    print('\n\n=== agent prompt accuracy vs local model ===')
    print(f'{"ok":<4}{"score":<8}{"secs":<7}{"type":<14}slug')
    for r in sorted(results, key=lambda x: (x['passed'], x['score'])):
        print(f'{"PASS" if r["passed"] else "FAIL":<4}'
              f'{r["score"]:<8}{r["seconds"]:<7}{r["goal_type"]:<14}{r["slug"]}')
    print(f'\naccuracy: {len(passed)}/{len(results)} = {accuracy:.0%}')
    for r in results:
        if not r['passed']:
            print(f'  FAIL {r["slug"]}: {r["reason"]}')

    # Written where a later run can diff against it. A single run is a
    # snapshot; the useful signal is whether editing a brief moved its score.
    out = os.environ.get('HEVOLVE_AGENT_ACCURACY_OUT', '')
    if out:
        try:
            with open(out, 'w', encoding='utf-8') as fh:
                json.dump({'accuracy': accuracy, 'results': results}, fh, indent=2)
        except Exception as exc:
            print(f'(could not write {out}: {exc})')

    # No threshold asserted. The first run establishes the baseline, and
    # inventing a number here would be picking a target before knowing what is
    # achievable, which is how a gate becomes something people delete.
    assert results, 'no agents were evaluated'
    assert all(r['reply_chars'] > 0 or not r['passed'] for r in results), (
        'an agent was scored as passing on an empty reply'
    )


def test_the_judge_refuses_to_score_without_a_model(agents):
    """The property that makes the number above trustworthy.

    If this ever passes vacuously, every accuracy figure this file produces is
    meaningless, so it is asserted directly rather than assumed.
    """
    from tests.e2e.agentic_harness import LLMJudge
    judge = LLMJudge()
    judge._llm_enabled = False  # simulate the backend being unavailable
    v = judge.judge('a perfectly reasonable looking answer',
                    rubric=_RUBRIC, strict=True)
    assert not v.passed, 'strict judging scored a pass with no model'
    assert v.score == 0.0
