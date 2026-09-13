"""The context limiters must never drop the newest message.

autogen 0.2.37's MessageHistoryLimiter and MessageTokenLimiter both finish with

    if not transforms_util.is_tool_call_valid(window):
        window.pop()

is_tool_call_valid() checks only the FIRST message for role == 'tool', and
pop() removes the LAST.  Whenever the window opens on a tool result, the
newest message is thrown away.

Live 2026-09-13 11:27:21, CREATE 87400889007 flow 1 action 3: the newest
message in the group was ChatInstructor's recipe request ("Focus on the
current task at hand and create a detailed recipe ...").  The StatusVerifier's
ToolMessageHandler input was five messages -- tool, tool, Assistant,
StatusVerifier, Assistant -- and the request was not among them.  09:15-12:35
that day, 255 of 1,549 ToolMessageHandler inputs opened on a tool result, at
most 11 of them explained by the handler's own pre-steps.

hartos.helper.history_limiter / token_limiter are autogen's classes with the
newest message put back; the window is otherwise autogen's own.  Pinned here:
  1. the upstream defect, so the correction can go the day autogen fixes it
  2. the correction, on the live verifier shape and through the full chain
  3. equality with autogen whenever autogen keeps the newest
  4. the token budget still holds
  5. no agent builder constructs autogen's limiters directly

    python -m pytest tests/unit/test_context_limiters_keep_the_newest.py --noconftest -q
"""
import ast
import os
import pathlib
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest  # noqa: E402
from autogen.agentchat.contrib.capabilities import transforms as ag  # noqa: E402
from autogen.agentchat.contrib.capabilities import transforms_util  # noqa: E402
from autogen.agentchat.contrib.capabilities.transform_messages import TransformMessages  # noqa: E402

from core.constants import (  # noqa: E402
    AUTOGEN_HISTORY_LIMIT, AUTOGEN_MESSAGE_TOKEN_BUDGET,
    AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)
from hartos.helper import ToolMessageHandler, history_limiter, token_limiter  # noqa: E402

_REPO = pathlib.Path(__file__).resolve().parents[2]
_RECIPE_REQUEST = ('Focus on the current task at hand and create a detailed '
                   'recipe that includes only the necessary steps for this '
                   'action')
_TOKENS = dict(max_tokens=AUTOGEN_MESSAGE_TOKEN_BUDGET,
               max_tokens_per_message=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
               min_tokens=0)


def _words(n, word='alpha'):
    return ' '.join([word] * n)


def _msg(role, name, content, **extra):
    m = {'role': role, 'name': name, 'content': content}
    m.update(extra)
    return m


def _verifier_buffer():
    """The StatusVerifier's buffer at 11:27:21, sized so the token window
    opens on the first of two tool results -- the shape of the live dump."""
    return [
        _msg('user', 'ChatInstructor', 'Execute Action 3: check the weather'),
        _msg('assistant', 'Assistant', '', tool_calls=[{
            'id': 'call_1', 'type': 'function',
            'function': {'name': 'google_search',
                         'arguments': '{"query": "weather"}'}}]),
        _msg('tool', 'Assistant', _words(800), tool_call_id='call_1'),
        _msg('tool', 'Assistant', _words(800), tool_call_id='call_1'),
        _msg('user', 'Assistant', 'Aloha! ' + _words(400)),
        _msg('assistant', 'StatusVerifier',
             '{"status": "pending", "message": "' + _words(300) + '"}'),
        _msg('user', 'Assistant',
             'Aloha! I see we need to verify device permissions first. '
             + _words(300)),
        _msg('user', 'ChatInstructor', _RECIPE_REQUEST),
    ]


def _orphan_first_history():
    """A buffer whose first message is a tool result, longer than the window."""
    return [_msg('tool', 'Assistant', 'orphan result', tool_call_id='c0')] + [
        _msg('user' if i % 2 else 'assistant', 'Assistant', 'message %d' % i)
        for i in range(60)]


# ── 1. the upstream defect ───────────────────────────────────────────────
def test_autogen_itself_drops_the_newest_when_the_window_opens_on_a_tool():
    """If this fails, autogen has fixed it and the correction can go."""
    msgs = _verifier_buffer()
    out = ag.MessageTokenLimiter(**_TOKENS).apply_transform(msgs)
    assert out[0]['role'] == 'tool', 'fixture no longer opens on a tool result'
    assert _RECIPE_REQUEST not in [m['content'] for m in out]

    hist = _orphan_first_history()
    out = ag.MessageHistoryLimiter(
        max_messages=AUTOGEN_HISTORY_LIMIT,
        keep_first_message=True).apply_transform(hist)
    assert all(m is not hist[-1] for m in out)


# ── 2. the correction ────────────────────────────────────────────────────
def test_the_recipe_request_reaches_the_verifier():
    msgs = _verifier_buffer()
    got = token_limiter(**_TOKENS).apply_transform(msgs)
    assert got[-1]['name'] == 'ChatInstructor'
    assert got[-1]['content'] == _RECIPE_REQUEST
    # everything before it is autogen's own window
    assert got[:-1] == ag.MessageTokenLimiter(**_TOKENS).apply_transform(msgs)


def test_history_limiter_keeps_the_newest_behind_an_orphan_first_message():
    hist = _orphan_first_history()
    got = history_limiter(max_messages=AUTOGEN_HISTORY_LIMIT,
                          keep_first_message=True).apply_transform(hist)
    assert got[-1] is hist[-1]
    assert got == [hist[0]] + hist[-(AUTOGEN_HISTORY_LIMIT - 1):]


def _verifier_request(history, tokens):
    """What the verifier's LLM call is asked to answer, after the whole chain
    every CREATE seat carries.  ToolMessageHandler merges consecutive
    same-role messages, so the request arrives as the TAIL of the last one
    (behind the Assistant's 'Aloha! ...'), not as a message of its own."""
    flask = pytest.importorskip('flask')
    chain = TransformMessages(
        transforms=[history, tokens, ToolMessageHandler()], verbose=False)
    with flask.Flask('limiter-chain').app_context():
        out = chain._transform_messages(_verifier_buffer())
    return str(out[-1].get('content') or '')


def test_autogens_chain_withholds_the_recipe_request():
    """The live 11:27:21 outcome, end to end, on autogen's own limiters."""
    last = _verifier_request(
        ag.MessageHistoryLimiter(max_messages=AUTOGEN_HISTORY_LIMIT,
                                 keep_first_message=True),
        ag.MessageTokenLimiter(**_TOKENS))
    assert _RECIPE_REQUEST not in last


def test_the_verifier_chain_ends_on_the_recipe_request():
    last = _verifier_request(
        history_limiter(max_messages=AUTOGEN_HISTORY_LIMIT,
                        keep_first_message=True),
        token_limiter(**_TOKENS))
    assert last.rstrip().endswith(_RECIPE_REQUEST), (
        'the verifier is asked to answer %r' % last[-120:])


# ── 3. equality with autogen, 4. the budget ──────────────────────────────
def _random_conversation(rng):
    out = []
    for i in range(rng.randint(1, 80)):
        role = rng.choice(('user', 'assistant', 'tool'))
        m = _msg(role, rng.choice(('Assistant', 'Helper', 'StatusVerifier',
                                   'ChatInstructor')),
                 _words(rng.randint(0, 1200), rng.choice(('alpha', 'beta'))))
        if role == 'tool':
            m['tool_call_id'] = 'c%d' % i
        if rng.random() < 0.1:
            m['content'] = None
        out.append(m)
    return out


def _pairs():
    """(ours, autogen's) for every configuration a HARTOS chain uses."""
    return (
        (history_limiter(max_messages=AUTOGEN_HISTORY_LIMIT, keep_first_message=True),
         ag.MessageHistoryLimiter(max_messages=AUTOGEN_HISTORY_LIMIT, keep_first_message=True)),
        (history_limiter(max_messages=50, keep_first_message=True),
         ag.MessageHistoryLimiter(max_messages=50, keep_first_message=True)),
        (history_limiter(max_messages=5),
         ag.MessageHistoryLimiter(max_messages=5)),
        (token_limiter(**_TOKENS), ag.MessageTokenLimiter(**_TOKENS)),
        (token_limiter(max_tokens=3500, max_tokens_per_message=1000, min_tokens=0),
         ag.MessageTokenLimiter(max_tokens=3500, max_tokens_per_message=1000, min_tokens=0)),
        (token_limiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=300),
         ag.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=300)),
    )


@pytest.mark.parametrize('seed', range(150))
def test_identical_to_autogen_except_the_newest_is_kept(seed):
    msgs = _random_conversation(random.Random(seed))
    for ours, theirs in _pairs():
        got = ours.apply_transform(msgs)
        want = theirs.apply_transform(msgs)
        popped = want is not msgs and (not want or want[0].get('role') == 'tool')
        if not popped:
            assert got == want
            continue
        assert got[:-1] == want
        assert (got[-1]['role'], got[-1].get('name')) == (
            msgs[-1]['role'], msgs[-1].get('name'))


@pytest.mark.parametrize('seed', range(150))
def test_the_token_budget_still_holds(seed):
    msgs = _random_conversation(random.Random(seed))
    got = token_limiter(**_TOKENS).apply_transform(msgs)
    total = sum(transforms_util.count_text_tokens(m['content']) for m in got
                if transforms_util.is_content_right_type(m.get('content')))
    assert total <= AUTOGEN_MESSAGE_TOKEN_BUDGET


# ── 5. one limiter for every builder ─────────────────────────────────────
def test_no_agent_builder_constructs_autogens_limiters_directly():
    """Every file that names autogen's limiters may only subclass them."""
    offenders = []
    for top in ('hartos', 'core', 'integrations'):
        for path in (_REPO / top).rglob('*.py'):
            src = path.read_text(encoding='utf-8', errors='replace')
            if 'MessageHistoryLimiter' not in src and 'MessageTokenLimiter' not in src:
                continue
            for node in ast.walk(ast.parse(src)):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ('MessageHistoryLimiter',
                                               'MessageTokenLimiter')):
                    offenders.append('%s:%d' % (
                        path.relative_to(_REPO).as_posix(), node.lineno))
    assert not offenders, (
        "autogen's limiters drop the newest message when the window opens on "
        "a tool result; build them with hartos.helper.history_limiter / "
        "token_limiter instead: %s" % offenders)
