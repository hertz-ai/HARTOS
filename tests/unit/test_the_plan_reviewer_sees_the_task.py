"""The plan reviewer is shown the task the plan was written for.

RED TESTS, design under peer review (hartos-17); marked xfail(strict=True)
until the writer-cap commit, which removes the marks.

THE DEFECT (HEAD, 2026-11-24): hart_intelligence_entry._review_proposed_plan
builds its request from the quality gates and the plan alone
(``review_prompt``: "Plan to review (JSON):" + the plan).  It never sees the
task, so a plan that kept 7 of the 10 flows the task names passes every gate
it can check: nothing in the request says three flows are missing.  On a
desktop turn the request goes out through pooled_post (requests, not httpx),
so the wire trim never sizes it either: the request has to fit the model's
context on its own.

Driven through the real _autonomous_gather_info on the real writer (the
wire of test_a_cut_plan_is_never_taken_as_complete); the reviewer's model
call, pooled_post to GPT_API, is the boundary that is scripted and read.
"""
import json
import os
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

import pytest  # noqa: E402

pytest.importorskip('autogen', reason='autogen not installed')

from tests.unit.test_a_cut_plan_is_never_taken_as_complete import (  # noqa: E402,F401
    SLOT, SPEC, auto, hie, speech_plan, wire,
)

SEES = ("the plan reviewer's request carries the plan but not the task; "
        "fixed by the writer-cap commit")


def _review_text(request):
    return '\n'.join(str(m.get('content')) for m in request.get('messages') or [])


@pytest.mark.xfail(strict=True, reason=SEES)
def test_a_plan_missing_three_of_ten_flows_is_reviewed_against_the_task(auto):
    auto.wire.script(auto.seat, (json.dumps(speech_plan('proposed_plan', n_flows=7)), 'stop'))
    auto.call()
    assert auto.reviews, 'the plan was never reviewed'
    text = _review_text(auto.reviews[0])
    assert '"Flow 7 Concepts"' in text and 'Flow 8 Parent' not in text.split(
        'Plan to review')[-1], 'not the 7-flow plan'
    assert SPEC in text, (
        'the reviewer was shown the 7-flow plan without the task that names '
        '10 flows: ' + text[:160])


@pytest.mark.xfail(strict=True, reason=SEES)
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
