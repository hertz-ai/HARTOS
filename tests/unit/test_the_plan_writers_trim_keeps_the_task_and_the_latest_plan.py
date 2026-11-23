"""When a person's creation outgrows the slot, the writer's request keeps
the system prompt, the task and the LATEST plan, and drops the turns in
between first.

THE DEFECT (2026-11-24, before the writer-cap commit): the wire trim
(core/llm_outbound_logger._trim_to_budget) drops the leftmost unprotected
message first.  Its protected set (protected_messages) is the newest user
turn, the task turn (_task_turn), the newest tool result and the current
order.  In the writer's two-seat chat every person turn comes from
'user_proxy_<id>', so _task_turn is the NEWEST of that seat's turns, not the
first: the request the creation began with is the first message dropped.
The interactive writer (AGENT_CREATOR_SYSTEM_MESSAGE) holds the task nowhere
else.  The latest plan is protected by nothing either; it survives today
only because the newer turns are short.

THE FIX: the writer names its task and its latest plan for each of its calls
(core.llm_outbound_logger.protect_turns, from
gather_agentdetails.get_agent_response), and protected_messages keeps them;
the reply is then given what the prompt the trim kept leaves
(test_the_plan_writer_has_one_capped_output_budget).

THE SCENARIO, built on the 346 walk's shape (a person's interview with the
interactive writer, answered turn by turn; tests/unit/
test_the_plan_writer_answers_every_turn.py), with the merged speech agent's
10-flow task: 346's own messages total about 3k tokens, under every budget,
so they cannot exercise a trim.  Turn 1 is the task; the writer answers each
of turns 1 to 4 with the whole plan for review; turn 4 is a correction, and
the turn-4 plan carries it; turn 5 gets a question; turn 6 is the answer.
The turn-6 request, as the real wire sends it, is what is read.
"""
import json
import os
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
if 'HEVOLVE_CACHE_DIR' not in os.environ:
    os.environ['HEVOLVE_CACHE_DIR'] = tempfile.mkdtemp()

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

from tests.unit.test_a_cut_plan_is_never_taken_as_complete import (  # noqa: E402,F401
    SLOT, SPEC, speech_plan, wire,
)

USER, PID = 'trim_u', '1794000000401'
CORRECTION = 'call <child_name> by name twice before any praise, never once'
PERSON = [
    SPEC,
    'Put Flow 8, the parent giving the child details, before Flow 1.',
    'In Flow 5, start every new word at one syllable.',
    'Correction for Flow 1: ' + CORRECTION + '.',
    'Is the therapist review weekly?',
    'Weekly.',
]
def _review(plan):
    return json.dumps({'status': 'pending', 'review_details': json.dumps(plan)})


def _replies():
    plans = []
    for n in range(4):
        plan = speech_plan('completed')
        plan['name'] = f'Speech Therapy Agent v{n + 1}'
        if n == 3:  # the turn-4 plan carries the correction
            plan['flows'][0]['actions'].insert(2, CORRECTION)
        plans.append(_review(plan))
    question = json.dumps({'status': 'pending',
                           'question': 'Weekly or monthly therapist review?'})
    return plans + [question, question]


def test_a_correction_in_turn_4_reaches_the_turn_6_request(wire):
    import flask
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS as MARGIN
    from core.token_utils import count_tokens_for_messages
    from hartos import gather_agentdetails as gad
    replies = _replies()
    wire.script(f'user_proxy_{USER}', *[(r, 'stop') for r in replies])
    gad.user_agents.pop(f'{USER}_{PID}', None)
    try:
        with flask.Flask('writer_trim_test').app_context():
            for text in PERSON:
                gad.gather_info(USER, text, PID)
            system = gad.user_agents[f'{USER}_{PID}'][0].system_message
    finally:
        gad.user_agents.pop(f'{USER}_{PID}', None)
    assert len(wire.sent) == 6, len(wire.sent)
    body = wire.sent[5]
    msgs = body['messages']
    contents = [m.get('content') for m in msgs]
    total = count_tokens_for_messages(msgs, body.get('model'))
    # Non-vacuous: the history outgrew the slot, so something was dropped.
    assert replies[0] not in contents, 'nothing was trimmed: the scenario is too small'
    assert msgs[0]['role'] == 'system' and msgs[0]['content'].startswith(system), (
        'the system prompt was cut')
    assert contents[-1] == PERSON[5], 'the newest turn is not last'
    assert SPEC in contents, (
        f'the task the creation began with was dropped from the turn-6 request '
        f'({len(msgs)} messages, {total} tokens): '
        f'{[str(c)[:30] for c in contents]}')
    assert replies[3] in contents, 'the latest plan, which carries the turn-4 correction, was dropped'
    assert any(CORRECTION in str(c) for c in contents)
    assert total + int(body.get('max_tokens') or 0) + MARGIN <= SLOT, (
        total, body.get('max_tokens'))
