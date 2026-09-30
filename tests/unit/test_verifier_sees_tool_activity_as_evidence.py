"""The StatusVerifier must see another seat's tool calls as reported evidence.

autogen 0.2.37 ``_append_oai_message`` (conversable_agent.py:667-668) gives
``role="assistant"`` to every message that carries ``tool_calls``, whoever
sent it.  So the StatusVerifier, which has no tools, receives the Assistant's
call as if it had made it, and continues that trajectory instead of judging
it.

Live 2026-09-14 on the installed build, two agents walked as their owners:
- 20260824301: send_message_to_user ran 3 times; the verifier answered each
  time with the same call written out as ``<tool_call>`` text;
- 12165936867: the verifier answered "Great! I found some useful
  information... <tool_call><function=subscribe_news_feed>".
Neither action ever got a verdict.  Both turns spent their 12 rounds and
the user got the leftover text as the reply.

Replaying the verifier's exact logged requests against the live llama-server
(scratchpad/verifier/replay2.txt): as logged, 4/4 answered with tool-call
text; with the same evidence told as a report from the seat that made the
call, 4/4 answered with a JSON verdict.  JSON mode did not help (2/2 still
tool-call text).

    python -m pytest tests/unit/test_verifier_sees_tool_activity_as_evidence.py --noconftest -q
"""
import ast
import json
from pathlib import Path

import pytest

_HARTOS = Path(__file__).resolve().parents[2] / 'hartos'

_CALL = {'id': 'call_1', 'type': 'function',
         'function': {'name': 'send_message_to_user',
                      'arguments': json.dumps({'text': 'A fraction is part of a whole.'})}}


def _conversation():
    """The verifier's view, as live on 2026-09-14 (verifier_005122)."""
    return [
        {'role': 'user', 'name': 'ChatInstructor',
         'content': 'Perform this action -> Action #1: Explain fractions to the user.'},
        {'role': 'assistant', 'name': 'Assistant', 'content': None,
         'tool_calls': [dict(_CALL)]},
        {'role': 'tool', 'tool_call_id': 'call_1',
         'content': 'Message sent successfully to user'},
    ]


@pytest.fixture
def seats():
    autogen = pytest.importorskip('autogen')
    flask = pytest.importorskip('flask')
    import hartos.helper as h

    def build(name, **kw):
        kw.setdefault('llm_config', False)
        kw.setdefault('code_execution_config', False)
        return autogen.AssistantAgent(name=name, **kw)

    def seen_by(seat, messages, sender):
        """What the seat's reply functions (and its LLM request) receive."""
        box = {}

        def recorder(recipient, messages=None, sender=None, config=None):
            box['messages'] = messages
            return True, 'recorded'

        seat.register_reply([autogen.Agent, None], recorder, position=0)
        seat.generate_reply(messages=messages, sender=sender)
        return box['messages']

    def shared_transform(*agents):
        chain = h.transform_messages.TransformMessages(
            transforms=[h.ToolMessageHandler()], verbose=False)
        for a in agents:
            chain.add_to_agent(a)

    with flask.Flask('judge-view-test').app_context():
        yield h, build, seen_by, shared_transform


def test_premise_autogen_hands_the_call_to_the_judge_as_its_own_turn(seats):
    """Why the fix exists: pin autogen's role rewrite."""
    h, build, _seen_by, _shared = seats
    assistant, verify = build('Assistant'), build('StatusVerifier')
    assistant.send({'content': None, 'tool_calls': [dict(_CALL)]}, verify,
                   request_reply=False, silent=True)
    got = verify._oai_messages[assistant][-1]
    assert got['role'] == 'assistant' and got.get('tool_calls'), (
        'autogen no longer rewrites a received tool call to role=assistant; '
        're-measure the verifier before keeping the judge view')


def test_the_verifier_sees_the_call_and_its_result_as_a_report(seats):
    h, build, seen_by, shared = seats
    assistant, verify = build('Assistant'), build('StatusVerifier')
    shared(verify)
    h.give_judge_view(verify)
    caller = _conversation()
    seen = seen_by(verify, caller, assistant)

    assert not any(m.get('tool_calls') for m in seen), (
        'the verifier still receives tool_calls, so it reads the call as its '
        'own turn and continues it')
    assert not any(m.get('role') == 'tool' for m in seen)
    report = '\n'.join(str(m.get('content') or '') for m in seen
                       if m.get('role') == 'user')
    assert 'send_message_to_user' in report
    assert 'A fraction is part of a whole.' in report, 'the call arguments are evidence'
    assert 'Message sent successfully to user' in report, 'the result is evidence'
    assert 'Perform this action' in report, 'the instruction is kept'
    assert caller[1].get('tool_calls'), "the conversation's own history is untouched"


def test_a_call_with_no_result_is_reported_as_such(seats):
    h, build, seen_by, shared = seats
    assistant, verify = build('Assistant'), build('StatusVerifier')
    shared(verify)
    h.give_judge_view(verify)
    seen = seen_by(verify, _conversation()[:2], assistant)
    report = '\n'.join(str(m.get('content') or '') for m in seen)
    assert 'send_message_to_user' in report
    assert not any(m.get('tool_calls') for m in seen)


@pytest.mark.parametrize('kind', ['executes_functions', 'runs_code'])
def test_a_seat_that_acts_on_calls_keeps_them(seats, tmp_path, kind):
    """generate_reply passes the transformed list to EVERY reply function,
    tool and code execution included.  A seat that executes calls or code
    must keep them, or the call would never run."""
    h, build, seen_by, shared = seats
    assistant = build('Assistant')
    if kind == 'executes_functions':
        seat = build('Helper')
        seat.register_function(function_map={'send_message_to_user': lambda text: 'ok'})
    else:
        seat = build('Executor', code_execution_config={
            'work_dir': str(tmp_path), 'use_docker': False})
    shared(seat)
    h.give_judge_view(seat)
    seen = seen_by(seat, _conversation(), assistant)
    assert any(m.get('tool_calls') for m in seen), (
        f'a seat that {kind.replace("_", " ")} lost the call it must act on')


def _verifier_vars(fn):
    """Variables bound to a StatusVerifier inside one function."""
    out = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        callee = getattr(call.func, 'attr', None) or getattr(call.func, 'id', None)
        named = any(k.arg == 'name' and isinstance(k.value, ast.Constant)
                    and k.value.value == 'StatusVerifier' for k in call.keywords)
        if named or callee == 'instantiate_status_verifier_agent':
            out.add(node.targets[0].id)
    return out


def _calls(fn, attr_or_name):
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if getattr(f, 'attr', None) == attr_or_name or getattr(f, 'id', None) == attr_or_name:
                yield node


@pytest.mark.parametrize('module', ['reuse_recipe.py', 'create_recipe.py', 'helper.py'])
def test_every_verifier_that_gets_the_shared_transform_gets_the_judge_view(module):
    src = (_HARTOS / module).read_text(encoding='utf-8')
    missing = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for var in _verifier_vars(fn):
            shared = [c.lineno for c in _calls(fn, 'add_to_agent')
                      if c.args and getattr(c.args[0], 'id', None) == var]
            if not shared:
                continue  # a factory that only builds and returns the seat
            judged = [c.lineno for c in _calls(fn, 'give_judge_view')
                      if c.args and getattr(c.args[0], 'id', None) == var]
            if not judged or min(judged) < max(shared):
                missing.append(f'{fn.name}:{var}')
    assert not missing, (
        f'{module}: StatusVerifier seats without the judge view (it must come '
        f'after the shared transform, which fills the real tool answers): {missing}')
