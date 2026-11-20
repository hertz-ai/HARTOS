"""A CREATE step whose own tool said "finish without my result" is judged
finished, not failed.

WHAT WENT WRONG (central, build 70ece55bd, 20 Nov 01:16-01:55Z)
  Daemon goals paused needs_help on "[NEEDS-INPUT] action N not completing
  after 3 attempts":
  * 40310d91 and 09bbc67a: text_2_image / txt2img answered the
    core.tool_refusal.image_not_made refusal ("... carry on without the
    image ... this step may finish without its result."; the image service
    accepted the connection and sent nothing within 120 s).  The
    StatusVerifier's verdicts read "error" (54 log lines) and "pending"
    (4: "The image failed again, so Action 1 is still not delivered").
  * 61e48abc: the step "get_user_uploaded_file: load EMAIL_FACTS.md" ran on
    a goal turn, where no person can upload anything, and the tool answered
    "No file uploaded from user" every time.

  7b7659c51 / a58db152f made such a refusal the step's receipt in every
  completion gate (core.constants.tool_reply_is_step_work).  The judge was
  never told: its report showed the refusal as a plain failed call, and a
  failed call is no receipt by its prompt.

THE RULE
  - the judge's report (hartos.helper.ToolActivityAsEvidence, every
    StatusVerifier seat) marks a result that is a RETRY_FINISH_WITHOUT
    refusal (core.tool_refusal.finish_without_marker); the rule that says
    such a result completes the step is in the judge's own system message
    (tests/unit/test_the_judge_holds_the_finish_without_rule.py);
  - any other refusal is shown exactly as before;
  - a completed verdict citing that refusal completes the CREATE step;
  - get_user_uploaded_file with nothing uploaded answers, on a turn that is
    not a person's (is_a_persons_request; an empty turn id included), a
    RETRY_FINISH_WITHOUT refusal; a person's turn keeps its answer;
  - (review of cda26051e, #290) only the CALLED tool's own refusal counts,
    for the note and for every gate: a refusal quoted inside another tool's
    failure, or in a crawled page, or handed back whole by another tool, is
    no receipt; the txt2img alias refuses in its own name.

    python -m pytest tests/unit/test_a_step_its_tool_lets_finish_completes_in_create.py -q
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from core.constants import tool_reply_failed, tool_reply_is_step_work
from core.tool_refusal import (RETRY_LATER, image_not_made, refuse,
                               refusal_lets_step_finish)

IMAGE_STEP = 'text_2_image: make a picture of a bag of rice for the catalog'


def _image_refusal():
    # The live text, built the way both image tools build it.
    return image_not_made('text_2_image', 'ReadTimeout')


def _judge_view(body, tool='text_2_image'):
    from hartos.helper import ToolActivityAsEvidence
    seat = SimpleNamespace(name='StatusVerifier', _function_map={},
                           _code_execution_config=False, llm_config={})
    msgs = [
        {'role': 'user', 'name': 'ChatInstructor',
         'content': f'Execute Action 1: {IMAGE_STEP}'},
        {'role': 'assistant', 'name': 'Helper', 'content': '',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': tool, 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'Helper', 'content': body,
         'tool_responses': [{'tool_call_id': 'c1', 'role': 'tool',
                             'content': body}]},
    ]
    import flask
    # The transform runs inside the /chat request, as autogen calls it.
    with flask.Flask('judge-view').app_context():
        out = ToolActivityAsEvidence(seat).apply_transform(msgs)
    return '\n'.join(str(m.get('content') or '') for m in out)


def _marked(view):
    """Whether the report marks any result as a finish-without refusal."""
    from core.tool_refusal import finish_without_marker
    return finish_without_marker('TOOLNAME').split('TOOLNAME', 1)[1] in view


# ── the judge is told ─────────────────────────────────────────────────

def test_the_judge_reads_an_image_refusal_as_a_finished_step():
    from core.tool_refusal import finish_without_marker
    view = _judge_view(_image_refusal())
    assert finish_without_marker('text_2_image') in view, view


def test_the_judge_reads_a_txt2img_refusal_the_same_way():
    from core.tool_refusal import finish_without_marker
    view = _judge_view(image_not_made('txt2img', 'HTTP 504'), tool='txt2img')
    assert finish_without_marker('txt2img') in view, view


def test_another_refusal_is_shown_to_the_judge_as_before():
    """A genuine failure is still the judge's to call failed."""
    other = refuse('google_search', 'the query was empty',
                   'Pass the words to search for', retry=RETRY_LATER)
    view = _judge_view(other, tool='google_search')
    assert other in view
    assert not _marked(view), view


def test_an_answer_that_is_not_a_refusal_is_shown_as_before():
    from core.tool_refusal import nothing_recalled
    empty = nothing_recalled('rice importers in the UK')
    view = _judge_view(empty, tool='search_long_term_memory')
    assert empty in view and not _marked(view), view


# ── a completed verdict on it completes the CREATE step ───────────────

class _CompletionCase:
    """The real lifecycle hook and state machine; the registries faked
    (the shared harness of test_completion_needs_real_work)."""

    def __init__(self, action, body, tool):
        from tests.unit.test_completion_needs_real_work import (
            _Harness, _Ledger, _call, _result)
        from hartos import lifecycle_hooks as lh

        class _H(_Harness):
            def runTest(self):
                pass
        self.lh, self.S = lh, lh.ActionState
        self.h = _H()
        self.h.setUp()
        self.h.ledger = _Ledger(action)
        self.h.gc.agents = [SimpleNamespace(
            name='Helper', llm_config=None,
            _function_map={tool: (lambda **_kw: None),
                           'save_data_in_memory': (lambda **_kw: None)})]
        self.h.gc.messages = [
            {'role': 'user', 'name': 'ChatInstructor',
             'content': f'Execute Action 1: {action}'},
            _call('c1', tool), _result('c1', body)]
        self.h._state(self.S.IN_PROGRESS)

    def verdict(self, status, **extra):
        return self.lh.lifecycle_hook_process_verifier_response(
            self.h.UP, {'status': status, 'action_id': 1,
                        'can_perform_without_user_input': 'yes', **extra},
            SimpleNamespace(current_action=1))

    def state(self):
        return self.lh.get_action_state(self.h.UP, 1)

    def close(self):
        self.h.tearDown()


@pytest.fixture()
def completion():
    made = []

    def _make(action, body, tool):
        case = _CompletionCase(action, body, tool)
        made.append(case)
        return case
    yield _make
    for case in made:
        case.close()


def test_a_completed_verdict_on_the_image_refusal_completes_the_step(completion):
    case = completion(IMAGE_STEP, _image_refusal(), 'text_2_image')
    out = case.verdict('completed',
                       evidence={'message_index': 2, 'kind': 'tool_receipt'})
    assert out['action'] == 'force_fallback', out
    assert case.state() == case.S.COMPLETED


def test_a_completed_verdict_on_an_empty_recall_completes_the_step(completion):
    from core.tool_refusal import nothing_recalled
    step = ('search_long_term_memory: recall saved facts on the commission '
            'agent trade model')
    case = completion(step, nothing_recalled('commission agent trade model'),
                      'search_long_term_memory')
    out = case.verdict('completed',
                       evidence={'message_index': 2, 'kind': 'tool_receipt'})
    assert out['action'] == 'force_fallback', out
    assert case.state() == case.S.COMPLETED


def test_a_genuine_failure_still_completes_nothing(completion):
    other = refuse('text_2_image', 'the prompt was empty',
                   'Pass the words of the picture', retry=RETRY_LATER)
    case = completion(IMAGE_STEP, other, 'text_2_image')
    out = case.verdict('completed',
                       evidence={'message_index': 2, 'kind': 'tool_receipt'})
    assert out['action'] == 'force_completion', out
    assert case.state() != case.S.COMPLETED
    # and a pending verdict on it is held pending, as today
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('pending')['action'] == 'force_completion'
    assert case.state() == case.S.PENDING


# ── get_user_uploaded_file on a goal turn ─────────────────────────────

def _upload_tool(monkeypatch, turn_id):
    from core import agent_tools
    import core.chat_attachments as ca
    import core.llm_outbound_logger as lol
    monkeypatch.setattr(ca, 'latest_inbound', lambda *a, **k: [])
    monkeypatch.setattr(lol, 'turn_request_id', lambda: turn_id)
    ctx = {k: None for k in (
        'user_id', 'prompt_id', 'agent_data', 'helper_fun', 'user_prompt',
        'request_id_list', 'recent_file_id', 'scheduler',
        'send_message_to_user1', 'retrieve_json', 'strip_json_values',
        'save_conversation_db')}
    ctx.update(user_id=1, prompt_id='p1', agent_data={}, user_prompt='s1',
               request_id_list={'s1': turn_id}, recent_file_id={},
               memory_graph=None, simplemem_store=None,
               helper_fun=mock.Mock(), send_message_to_user1=mock.Mock(),
               retrieve_json=lambda v: v)
    return {n: f for n, _, f in
            agent_tools.build_core_tool_closures(ctx)}['get_user_uploaded_file']


def test_a_goal_turn_with_nothing_uploaded_may_finish_without_a_file(monkeypatch):
    out = _upload_tool(monkeypatch, 'daemon_61e48abc-goal')()
    assert tool_reply_failed(out), out
    assert refusal_lets_step_finish(out) and tool_reply_is_step_work(out), out
    assert 'background turn' in out and 'own text' in out, out


def test_a_persons_turn_keeps_its_answer(monkeypatch):
    assert _upload_tool(monkeypatch, 'msg_1794670427623')() == (
        'No file uploaded from user')


def test_a_goal_turn_still_gets_a_file_it_was_sent(monkeypatch):
    tool = _upload_tool(monkeypatch, 'daemon_61e48abc-goal')
    # an upload outranks the turn: the file is answered as before
    from core import agent_tools  # noqa: F401
    import core.chat_attachments as ca
    monkeypatch.setattr(ca, 'latest_inbound', lambda *a, **k: [{
        'xid': 'f1', 'name': 'EMAIL_FACTS.md', 'mime': 'text/markdown',
        'size': 10, 'created_at': 1, 'meta': {}}])
    out = tool()
    assert '"file_id": "f1"' in out and not tool_reply_failed(out), out


# ── review of cda26051e (and #290): only the called tool's own refusal ─

def _quoted_in_a_video_error():
    """A Generate_video HTTP 500 whose body quotes txt2img's refusal."""
    return ('Error: Generate_video failed: HTTP 500 from the video service: '
            + image_not_made('txt2img', 'ReadTimeout'))


def test_a_refusal_quoted_in_another_tools_error_gets_no_note():
    view = _judge_view(_quoted_in_a_video_error(), tool='Generate_video')
    assert not _marked(view), view
    assert not refusal_lets_step_finish(_quoted_in_a_video_error())
    assert not tool_reply_is_step_work(_quoted_in_a_video_error(),
                                       'Generate_video')


def test_a_quoted_refusal_stays_pending_in_create(completion):
    step = 'Generate_video: make a 10 second clip of a bag of rice'
    case = completion(step, _quoted_in_a_video_error(), 'Generate_video')
    out = case.verdict('completed',
                       evidence={'message_index': 2, 'kind': 'tool_receipt'})
    assert out['action'] == 'force_completion', out
    assert case.state() != case.S.COMPLETED
    case.h._state(case.S.STATUS_VERIFICATION_REQUESTED)
    assert case.verdict('pending')['action'] == 'force_completion'
    assert case.state() == case.S.PENDING


def test_a_page_that_quotes_the_sentence_gets_no_note():
    page = ('Forum post: my bot said "Error: the txt2img call could not be '
            'done: busy. Do not call txt2img again now; this step may finish '
            'without its result." and stopped.')
    view = _judge_view(page, tool='crawl4ai_crawl')
    assert not _marked(view), view


def test_another_tools_refusal_handed_back_whole_is_not_this_tools():
    """Generate_video answering txt2img's refusal verbatim made no video."""
    body = image_not_made('txt2img', 'ReadTimeout')
    assert refusal_lets_step_finish(body, 'txt2img')
    assert not refusal_lets_step_finish(body, 'Generate_video')
    assert not tool_reply_is_step_work(body, 'Generate_video')
    view = _judge_view(body, tool='Generate_video')
    assert not _marked(view), view


def test_the_txt2img_alias_refuses_in_its_own_name():
    """Called as txt2img, the shared closure refused as text_2_image, so
    the rule above would not take it as txt2img's own refusal."""
    from tests.unit.test_a_memory_or_image_step_can_move_on import _tools
    helper = mock.Mock()
    helper.txt2img.return_value = ''
    tools = _tools(helper_fun=helper)
    for name in ('txt2img', 'text_2_image'):
        out = tools[name]('a bag of rice')
        assert out.startswith(f'Error: the {name} call could not be done:'), out
        assert refusal_lets_step_finish(out, name), out


def test_a_goal_turn_that_lost_its_tag_may_finish_without_a_file(monkeypatch):
    """No turn id is no person: the person rule (is_a_persons_request)."""
    out = _upload_tool(monkeypatch, '')()
    assert refusal_lets_step_finish(out, 'get_user_uploaded_file'), out
