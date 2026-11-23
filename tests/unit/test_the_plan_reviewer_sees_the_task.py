"""The plan reviewer is shown the task the plan was written for.

THE DEFECT (2026-11-24, before the writer-cap commit):
hart_intelligence_entry._review_proposed_plan
builds its request from the quality gates and the plan alone
(``review_prompt``: "Plan to review (JSON):" + the plan).  It never sees the
task, so a plan that kept 7 of the 10 flows the task names passes every gate
it can check: nothing in the request says three flows are missing.  On a
desktop turn the request goes out through pooled_post (requests, not httpx),
so the wire trim never sizes it either: the request has to fit the model's
context on its own.

THE FIX: _review_proposed_plan(plan, rounds, task=) shows the task before
the plan, and on that path leaves it out when the request would not fit the
slot whole (core.llm_outbound_logger.prompt_token_room).

Driven through the real _autonomous_gather_info on the real writer (the
wire of test_a_cut_plan_is_never_taken_as_complete); the reviewer's model
call, pooled_post to GPT_API, is the boundary that is scripted and read.
"""
import json
import os
import re
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
if 'HEVOLVE_CACHE_DIR' not in os.environ:
    os.environ['HEVOLVE_CACHE_DIR'] = tempfile.mkdtemp()

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

from tests.unit.test_a_cut_plan_is_never_taken_as_complete import (  # noqa: E402,F401
    SLOT, SPEC, auto, hie, speech_plan, wire,
)


def _review_text(request):
    return '\n'.join(str(m.get('content')) for m in request.get('messages') or [])


def test_a_plan_missing_three_of_ten_flows_is_reviewed_against_the_task(auto):
    auto.wire.script(auto.seat, (json.dumps(speech_plan('proposed_plan', n_flows=7)), 'stop'))
    auto.call()
    assert auto.reviews, 'the plan was never reviewed'
    text = _review_text(auto.reviews[0])
    # The plan reviewed is the 7-flow one: its JSON names seven flows (the
    # task names its flows in prose, never as "flow_name"), wherever in the
    # request the task is put.
    named = re.findall(r'"flow_name":\s*"(Flow \d+)', text)
    assert len(named) == 7 and '"Flow 7 Concepts"' in text, (
        f'not the 7-flow plan: {named}')
    assert SPEC in text, (
        'the reviewer was shown the 7-flow plan without the task that names '
        '10 flows: ' + text[:160])


def test_the_reviewers_request_with_the_task_fits_its_context(auto):
    """The whole 10-flow plan and its task, reviewed on the 12288 local
    model: the request carries the task, and its prompt, its max_tokens and
    the wire's margin fit the slot (nothing trims a pooled_post request)."""
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS as MARGIN
    from core.token_utils import count_tokens_for_messages
    auto.wire.script(auto.seat, (json.dumps(speech_plan('proposed_plan')), 'stop'))
    auto.call()
    assert auto.reviews, 'the plan was never reviewed'
    request = auto.reviews[0]
    prompt = count_tokens_for_messages(request['messages'], request.get('model'))
    out = int(request.get('max_tokens') or 0)
    assert prompt + out + MARGIN <= SLOT, (
        f"the reviewer's request is {prompt} prompt + {out} output + {MARGIN} "
        f"margin tokens against a {SLOT} slot")
    assert SPEC in _review_text(request), (
        f"the reviewer's request ({prompt} tokens) does not carry the task")
