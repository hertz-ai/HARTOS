"""A refusal that tells the agent to finish the step without the tool's
result lets the step finish.

WHAT WENT WRONG (review of e0b313bdd, CRITICAL)
  e0b313bdd made text_2_image answer a core.tool_refusal refusal when the
  image service delivers nothing: "Carry on without the image ... and finish
  the step".  Every completion gate reads a refusal as a failed call
  (core.constants.tool_reply_failed), so for the step "text_2_image: make a
  picture ..." given that refusal and a written answer,
  _reuse_completion_evidence found no receipt and _reuse_fabricated_tools
  called text_2_image never run.  The step could never finish, which is the
  needs_help pause central's two image goals were already in.

THE RULE (one predicate, core.constants.tool_reply_is_step_work, read by the
receipt readers in place of `not tool_reply_failed`)
  - a refusal built with core.tool_refusal.RETRY_FINISH_WITHOUT is the step's
    receipt: the tool did all it could and said the step may finish without
    it;
  - any other refusal or failure is still no receipt;
  - CREATE's trace banker keeps tool_reply_failed: such a call is not a step
    to replay.

    python -m pytest tests/unit/test_a_carry_on_refusal_finishes_its_step.py -q
"""
from unittest import mock

import pytest

from core.constants import tool_reply_failed
from core.tool_refusal import RETRY_LATER, refuse, refusal_has_hint

ACTION = 'text_2_image: make a picture of a warm kitchen for the post'


def _image_refusal():
    """What the real text_2_image tool answers when no image came back."""
    from tests.unit.test_a_memory_or_image_step_can_move_on import _tools
    helper = mock.Mock()
    helper.txt2img.return_value = ''
    return _tools(helper_fun=helper)['text_2_image']('a warm kitchen')


class _Task:
    def __init__(self, text):
        self._text = text
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return self._text


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


class _GC:
    def __init__(self, messages, agents):
        self.messages = messages
        self.agents = agents


def _step(body, written=True):
    msgs = [
        {'role': 'user', 'name': 'ChatInstructor',
         'content': f'Perform this action -> Action #1: {ACTION}'},
        {'role': 'assistant', 'name': 'Helper', 'content': None,
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'text_2_image',
                                      'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'Assistant', 'content': body,
         'tool_responses': [{'tool_call_id': 'c1', 'role': 'tool',
                             'content': body}]},
    ]
    if written:
        msgs.append({'role': 'assistant', 'name': 'Assistant',
                     'content': 'Image prompt: a warm kitchen at dawn, steam '
                                'over a pot. Step done without the image.'})
    agents = [_Agent('Helper', ['text_2_image']),
              _Agent('Assistant', ['text_2_image'])]
    return _GC(msgs, agents), agents


@pytest.fixture()
def reuse(monkeypatch):
    from hartos import reuse_recipe
    monkeypatch.setattr(reuse_recipe, '_reuse_person_turn', lambda *_a: False)
    monkeypatch.setitem(reuse_recipe.user_tasks, 'u_carry_on', _Task(ACTION))
    return reuse_recipe


def test_the_image_refusal_is_a_hinted_failed_call():
    out = _image_refusal()
    assert tool_reply_failed(out) and refusal_has_hint(out), out


def test_the_image_refusal_is_the_steps_receipt(reuse):
    gc, _agents = _step(_image_refusal())
    ev = reuse._reuse_completion_evidence('u_carry_on', 1, gc)
    assert ev and ev.get('kind') == 'tool_receipt', ev


def test_the_image_refusal_is_not_a_fabricated_step(reuse):
    gc, agents = _step(_image_refusal(), written=False)
    assert reuse._reuse_fabricated_tools('u_carry_on', 1, gc, agents) == []


def test_another_refusal_is_still_no_receipt(reuse):
    """The rule is that one retry mode, not every refusal."""
    other = refuse('text_2_image', 'the prompt was empty',
                   'Pass the words of the picture', retry=RETRY_LATER)
    gc, agents = _step(other)
    assert reuse._reuse_completion_evidence('u_carry_on', 1, gc) is None
    assert reuse._reuse_fabricated_tools('u_carry_on', 1, gc, agents) == [
        'text_2_image']


def test_create_reads_the_same_receipt():
    from hartos.lifecycle_hooks import _work_results
    body = _image_refusal()
    msg = {'role': 'tool', 'content': body,
           'tool_responses': [{'tool_call_id': 'c1', 'content': body}]}
    assert list(_work_results(msg, {'c1': 'text_2_image'}, ACTION.lower(),
                              {'text_2_image'}, [msg])) == ['text_2_image']


def test_the_langchain_image_tool_refuses_in_words(monkeypatch):
    """Review F4: hart_intelligence_entry.parse_text_to_image returned None
    when the image service answered anything but 200, and the exception text
    when it raised."""
    import hart_intelligence_entry as hie
    monkeypatch.setattr(hie, 'publish_async', lambda *a, **k: None)
    monkeypatch.setattr(hie.requests, 'request',
                        lambda *a, **k: mock.Mock(status_code=503))
    out = hie.parse_text_to_image('a lightbulb')
    assert isinstance(out, str) and tool_reply_failed(out), repr(out)
    assert refusal_has_hint(out) and 'without the image' in out, out

    def _boom(*a, **k):
        raise ConnectionError('http://img.internal:5459 refused')
    monkeypatch.setattr(hie.requests, 'request', _boom)
    out = hie.parse_text_to_image('a lightbulb')
    assert refusal_has_hint(out) and 'img.internal' not in out, out
    assert 'ConnectionError' in out

    monkeypatch.setattr(hie.requests, 'request', lambda *a, **k: mock.Mock(
        status_code=200, json=lambda: {'img_url': 'https://img.example/b.png'}))
    assert hie.parse_text_to_image('a lightbulb') == 'https://img.example/b.png'
