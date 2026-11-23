"""The plan writer's output budget is one number, from the slot's one split,
and capped.

RED TESTS, design under peer review (hartos-17); marked xfail(strict=True)
until the writer-cap commit, which removes the marks.

THE DEFECT (HEAD, 2026-11-24): the writer's llm_config
(hartos/gather_agentdetails.create_agents_for_user) carries no max_tokens:
it keeps only hartos.helper.get_llm_config()['config_list'], dropping the
1500 beside it.  core/llm_outbound_logger._trim_to_budget then pins every
body that names none to 2048, a number of its own (the third value).  On the
desktop (Qwen3.5-4B, 12288 per slot) the merged speech agent's first writer
call is about 5,700 prompt tokens, which leaves room, but its 10-flow plan
(about 2,150 to 2,300 tokens) is cut at 2048 (finish_reason 'length').

THE RULE THESE PIN (the coordinator's design, item 1):
  * ONE function computes the writer's max_tokens, from the same split that
    sizes the trim (_message_budget), so max_tokens + the prompt room + the
    trim's margin never exceed the slot; gather holds no number of its own.
  * The budget is min(the slot's split, the provider's max output for the
    model when known, a ceiling of about 4096): a 200K-context model never
    gets more than the ceiling.
  * The wire's hidden 2048 pin is where it lands: the writer's body goes out
    with the computed value.

THE CONTRACT NAME.  The function does not exist yet; these tests name it
``core.llm_outbound_logger.output_token_budget(per_slot, prompt_tokens,
model=None) -> int``.  If review settles another name, change BUDGET_FN.
"""
import json
import os
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

from tests.unit.test_a_cut_plan_is_never_taken_as_complete import (  # noqa: E402,F401
    SLOT, SPEC, speech_plan, wire,
)

BUDGET_FN = 'output_token_budget'
CEILING = 4096       # "a ceiling of about 4096"
PLAN_NEEDS = 2300    # the 10-flow plan's measured upper bound (Qwen3.5-4B vocab)
ONE_BUDGET = ("the writer's max_tokens is the wire's hidden 2048 pin, not one "
              "capped budget from the slot's split; fixed by the writer-cap commit")


def budget(per_slot, prompt_tokens, model=None):
    import core.llm_outbound_logger as hook
    fn = getattr(hook, BUDGET_FN, None)
    assert fn is not None, f'core.llm_outbound_logger.{BUDGET_FN} does not exist'
    return fn(per_slot, prompt_tokens, model=model)


@pytest.mark.xfail(strict=True, reason=ONE_BUDGET)
def test_the_writers_call_carries_the_computed_budget_not_2048(wire):
    """The merged speech agent's first writer call, on a 12288 local model:
    the body as the wire finally sends it."""
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS as MARGIN
    from core.token_utils import count_tokens_for_messages
    from hartos import gather_agentdetails as gad
    user, pid = 'cap_u', 1794000000301
    wire.script(f'user_proxy_{user}', (json.dumps(speech_plan('proposed_plan')), 'stop'))
    gad.user_agents.pop(f'{user}_{pid}', None)
    import flask
    try:
        with flask.Flask('writer_cap_test').app_context():
            gad.gather_info(user, SPEC, pid, autonomous=True)
    finally:
        gad.user_agents.pop(f'{user}_{pid}', None)
    assert len(wire.sent) == 1
    body = wire.sent[0]
    prompt = count_tokens_for_messages(body['messages'], body.get('model'))
    assert 5000 <= prompt <= 6500, f'not the ~5,700-token first call: {prompt}'
    sent = body.get('max_tokens')
    assert sent and sent > 2048, (
        f'the writer went out with max_tokens={sent} against a {SLOT} slot and '
        f'a {prompt}-token prompt; its 10-flow plan needs about {PLAN_NEEDS}')
    assert sent >= PLAN_NEEDS, sent
    assert sent == budget(SLOT, prompt, model=body.get('model')), (
        'the writer\'s max_tokens is not the one function\'s value')
    assert prompt + sent + MARGIN <= SLOT, (prompt, sent, MARGIN)


@pytest.mark.xfail(strict=True, reason=ONE_BUDGET)
@pytest.mark.parametrize('per_slot, prompt', [
    (12288, 5700),    # the merged speech agent's first writer call
    (12288, 3000),
    (8192, 3000),     # a spawned 8192 server (#756)
    (16384, 9000),
])
def test_one_function_splits_the_slot(per_slot, prompt):
    """max_tokens + the trim's prompt room (_message_budget, the split it
    sizes the trim by) + the trim's margin fit the slot, the prompt it was
    sized for fits that room, and the ceiling holds."""
    import core.llm_outbound_logger as hook
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS as MARGIN
    out = budget(per_slot, prompt)
    room = hook._message_budget(per_slot, out, 0)
    assert 0 < out <= CEILING, out
    assert out + room + MARGIN <= per_slot, (out, room, MARGIN, per_slot)
    assert prompt <= room, (
        f'a {prompt}-token prompt would be trimmed to make room for '
        f'max_tokens={out} (room {room})')


@pytest.mark.xfail(strict=True, reason=ONE_BUDGET)
def test_a_200k_context_model_gets_no_more_than_the_ceiling():
    out = budget(200_000, 5_700, model='claude-sonnet-5-5')
    assert out <= CEILING, f'{out} output tokens for a 200K-context model'
    assert out >= PLAN_NEEDS, out
