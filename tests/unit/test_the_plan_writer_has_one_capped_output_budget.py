"""The plan writer's output budget is one number, from the slot's one split,
and capped.

THE DEFECT (2026-11-24, before the writer-cap commit): the writer's
llm_config (hartos/gather_agentdetails.create_agents_for_user) carries no
max_tokens: it keeps only hartos.helper.get_llm_config()['config_list'],
dropping the 1500 beside it.  core/llm_outbound_logger._trim_to_budget then
pinned every body that names none to 2048, a number of its own (the third
value).  On the desktop (Qwen3.5-4B, 12288 per slot) the merged speech
agent's first writer call is about 5,700 prompt tokens, which leaves room,
but its 10-flow plan (about 2,150 to 2,300 tokens) was cut at 2048
(finish_reason 'length').

THE RULE THESE PIN (the coordinator's design, item 1, as reviewed):
  * ONE function, core.llm_outbound_logger.output_token_budget(per_slot,
    prompt_tokens), computes the max_tokens of a request that names none,
    from the same split that sizes the trim (_message_budget): what the
    prompt leaves of the slot beside the trim's margin, so max_tokens + the
    prompt + the margin is the slot; gather holds no number of its own.
  * Never above OUTPUT_TOKEN_CEILING (4096): a 200K-context server never
    gives a reply more than that.  Never below OUTPUT_TOKEN_FLOOR (2048, the
    old pin): a prompt too long for the slot keeps the room and the output
    it had before, and the trim shortens it to that room.
  * The wire's hidden 2048 pin is where it lands: the writer's body goes out
    with the computed value.
  * Local servers only.  The provider's own output cap belongs to the path
    that knows the provider: a hosted body never reaches the local trim
    (core.claude_native.MAX_TOKENS_WHEN_UNSET applies there), so core reads
    no provider table (review of 72b5db708).  The 200K case is supplied by
    the real caller, the wire, for a local server whose slot is that large.
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

CEILING = 4096       # "a ceiling of about 4096"
FLOOR = 2048         # the old pin, now the least a reply is given
PLAN_NEEDS = 2300    # the 10-flow plan's measured upper bound (Qwen3.5-4B vocab)


def budget(per_slot, prompt_tokens):
    from core.llm_outbound_logger import output_token_budget
    return output_token_budget(per_slot, prompt_tokens)


def _first_writer_call(wire, user, pid):
    """The merged speech agent's first writer call, as the wire sends it."""
    from hartos import gather_agentdetails as gad
    wire.script(f'user_proxy_{user}', (json.dumps(speech_plan('proposed_plan')), 'stop'))
    gad.user_agents.pop(f'{user}_{pid}', None)
    import flask
    try:
        with flask.Flask('writer_cap_test').app_context():
            gad.gather_info(user, SPEC, pid, autonomous=True)
    finally:
        gad.user_agents.pop(f'{user}_{pid}', None)
    assert len(wire.sent) == 1
    return wire.sent[0]


def test_the_writers_call_carries_the_computed_budget_not_2048(wire):
    """On a 12288 local model: the body as the wire finally sends it."""
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS as MARGIN
    from core.token_utils import count_tokens_for_messages
    body = _first_writer_call(wire, 'cap_u', 1794000000301)
    prompt = count_tokens_for_messages(body['messages'], body.get('model'))
    assert 5000 <= prompt <= 6500, f'not the ~5,700-token first call: {prompt}'
    sent = body.get('max_tokens')
    assert sent and sent > 2048, (
        f'the writer went out with max_tokens={sent} against a {SLOT} slot and '
        f'a {prompt}-token prompt; its 10-flow plan needs about {PLAN_NEEDS}')
    assert sent >= PLAN_NEEDS, sent
    assert sent == budget(SLOT, prompt), (
        'the writer\'s max_tokens is not the one function\'s value')
    assert prompt + sent + MARGIN == SLOT, (prompt, sent, MARGIN)


@pytest.mark.parametrize('per_slot, prompt, expected', [
    (12288, 5700, 3772),   # the merged speech agent's first writer call
    (12288, 3000, 4096),   # the ceiling
    (8192, 3000, 2376),    # a spawned 8192 server (#756)
    (16384, 3000, 4096),
    (16384, 9000, 4096),
    (12288, 7000, 2472),   # just above the floor
])
def test_one_function_splits_the_slot(per_slot, prompt, expected):
    """The value is the split, pinned: what the prompt leaves of the slot
    beside the trim's margin, never above the ceiling.  max_tokens + the
    trim's prompt room (_message_budget) + the margin is the slot, and the
    prompt it was sized for fits that room."""
    import core.llm_outbound_logger as hook
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS as MARGIN
    out = budget(per_slot, prompt)
    assert out == expected, out
    room = hook._message_budget(per_slot, out, 0)
    assert out + room + MARGIN == per_slot, (out, room, MARGIN, per_slot)
    assert prompt <= room, (
        f'a {prompt}-token prompt would be trimmed to make room for '
        f'max_tokens={out} (room {room})')


@pytest.mark.parametrize('per_slot, prompt, room', [
    (12288, 9000, 7424),   # a long interview: the room the old pin left
    (8192, 6000, 3328),
])
def test_a_prompt_too_long_for_the_slot_keeps_the_old_room(per_slot, prompt, room):
    """A prompt that leaves less than the floor: the reply gets the floor,
    the old pin, and the trim shortens the prompt to the room the old pin
    left, never to less."""
    import core.llm_outbound_logger as hook
    out = budget(per_slot, prompt)
    assert out == FLOOR, out
    assert hook._message_budget(per_slot, out, 0) == room


def test_a_200k_slot_gives_the_writer_no_more_than_the_ceiling(wire, monkeypatch):
    """The real caller, the wire, on a local server with a 200K slot: the
    writer's first call goes out with the ceiling, enough for the plan."""
    monkeypatch.setenv('HEVOLVE_LLAMA_CTX_SIZE', '200000')
    body = _first_writer_call(wire, 'cap_u200', 1794000000302)
    assert body.get('max_tokens') == CEILING, body.get('max_tokens')
    assert CEILING >= PLAN_NEEDS


def test_a_trimmed_request_is_given_what_its_kept_prompt_leaves():
    """A body over the slot is trimmed to the room the floor leaves; its
    reply is then given what the prompt the trim KEPT leaves, so a request
    whose middle turns were dropped can write a whole plan in their place."""
    import core.llm_outbound_logger as hook
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS as MARGIN
    from core.token_utils import count_tokens_for_messages
    from unittest.mock import patch
    msgs = [{'role': 'system', 'content': 'You write plans.'},
            {'role': 'user', 'name': 'user_proxy_t', 'content': 'the task ' * 40}]
    for n in range(12):
        msgs.append({'role': 'assistant', 'content': f'plan {n} ' + 'step ' * 1200})
        msgs.append({'role': 'user', 'name': 'user_proxy_t', 'content': f'answer {n}'})
    body = {'model': 'llama', 'messages': msgs}
    with patch.object(hook, '_get_budget_per_slot', lambda: SLOT):
        out, n_dropped, _cut, before, after, room = hook._trim_to_budget(body)
    assert before > SLOT and n_dropped, (before, n_dropped)
    assert room == SLOT - FLOOR - MARGIN, room
    kept = count_tokens_for_messages(out['messages'], 'llama')
    assert kept == after and after <= room, (kept, after, room)
    assert out['max_tokens'] == budget(SLOT, after) >= FLOOR, out['max_tokens']
    assert after + out['max_tokens'] + MARGIN <= SLOT
