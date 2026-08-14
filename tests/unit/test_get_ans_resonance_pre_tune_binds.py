"""``get_ans`` must pre-tune resonance from the USER MESSAGE, and it must bind.

Root cause (observed live 2026-08-11 21:46:59 in gui_app.log/server.log):

    hart_intelligence_entry - ERROR - get_ans: swallowed Exception
    UnboundLocalError: cannot access local variable 'prompt' where it is not
    associated with a value

``get_ans`` is declared at hart_intelligence_entry.py:7274 as::

    def get_ans(casual_conv, req_tool, user_id, query, custom_prompt,
                preferred_lang):

There is NO ``prompt`` parameter — the user's message is ``query``.  But
:7351 READ ``prompt``::

    _res_profile = pre_tune_from_input(_res_profile, prompt)

while the only assignment to ``prompt`` is 119 lines LATER at :7470::

    prompt = ConversationalChatAgent.create_prompt(...)

Because ``prompt`` is assigned anywhere in the function body, Python marks it
local for the WHOLE function, so the earlier read is unbound and raises.

Two distinct defects in one line, which is why a plain "make it bind" fix would
still be wrong:

1. **Unbound read.**  The enclosing try/except (:7347-7354) swallows it, so the
   turn still answers — the failure is SILENT.  Consequence: ``pre_tune_from_input``
   never ran and ``build_resonance_prompt`` on the next line never ran either, so
   ``_resonance_block`` was permanently ''.  The resonance feature was dead on
   this path from the moment the line was written.
2. **Wrong concept.**  ``prompt`` at :7470 is the LangChain *prompt template*
   object.  ``pre_tune_from_input(profile, user_message: str)`` wants the user's
   message text and does ``user_message.lower()``.  Passing the template would be
   a bug even if it were bound.  This is the one-name-two-vocabularies shape:
   "prompt" means the template to the agent builder and the user's utterance to
   whoever wrote :7351.

The feature's own docstring (core/resonance_tuner.py:627) states the contract
these tests defend:

    "extract signals from the current user message and apply them to the profile
     BEFORE the LLM generates its response.  This fixes the one-turn-behind
     problem where 'speak respectfully' only takes effect on the NEXT response
     instead of the current one."

So "speak respectfully" landing on the CURRENT turn has never worked.

Source-level guard rather than behavioural: ``get_ans`` is ~400 lines inside the
Flask app module and importing it drags the whole HARTOS boot path.  These pin
the shape that regressed, same mechanical approach as
tests/unit/test_urllib_outbound_gating.py's siblings and
Nunba's tests/test_lang_constants.py.
"""
import ast
from pathlib import Path

import pytest

ENTRY = (Path(__file__).resolve().parent.parent.parent
         / 'hart_intelligence_entry.py')


@pytest.fixture(scope='module')
def get_ans_fn():
    tree = ast.parse(ENTRY.read_text(encoding='utf-8', errors='replace'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'get_ans':
            return node
    pytest.fail('get_ans not found in hart_intelligence_entry.py — renamed?')


def _param_names(fn):
    a = fn.args
    names = [p.arg for p in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return names


def _pre_tune_calls(fn):
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, 'id', None) or getattr(f, 'attr', None)
            if name == 'pre_tune_from_input':
                out.append(node)
    return out


def test_pre_tune_is_still_called(get_ans_fn):
    """Guard the guard: if the call is deleted, the rest is vacuous."""
    assert _pre_tune_calls(get_ans_fn), (
        'get_ans no longer calls pre_tune_from_input — the resonance pre-tune '
        'was removed rather than fixed; delete these tests deliberately if that '
        'was intended.')


def test_pre_tune_second_arg_is_a_bound_parameter(get_ans_fn):
    """RED before the fix: the arg was `prompt`, not a parameter at all.

    This is the general form of the defect — reading a name that only becomes
    local further down the function.  Restricting the argument to a formal
    parameter makes the unbound-read class impossible here by construction.
    """
    params = _param_names(get_ans_fn)
    for call in _pre_tune_calls(get_ans_fn):
        assert len(call.args) >= 2, (
            'pre_tune_from_input called without a user_message argument')
        second = call.args[1]
        assert isinstance(second, ast.Name), (
            'pre_tune_from_input\'s user_message must be a plain parameter '
            f'name, got {type(second).__name__}')
        assert second.id in params, (
            f"pre_tune_from_input receives '{second.id}', which is NOT a "
            f'parameter of get_ans ({params}). If it is assigned later in the '
            'body, Python treats it as local for the whole function and this '
            'read raises UnboundLocalError — silently, because the enclosing '
            'try/except swallows it, leaving resonance permanently disabled.')


def test_pre_tune_receives_the_user_message_not_the_prompt_template(get_ans_fn):
    """Pin INTENT, not just bindability.

    ``query`` is get_ans's user-message parameter.  ``prompt`` is the LangChain
    template built at :7470 — a different concept whose ``.lower()`` would be
    meaningless to the signal extractor.  Binding the wrong name would satisfy
    the test above while still being wrong, so pin the right one.
    """
    for call in _pre_tune_calls(get_ans_fn):
        second = call.args[1]
        assert getattr(second, 'id', None) == 'query', (
            f"pre_tune_from_input must receive `query` (the user's message); "
            f"got `{getattr(second, 'id', None)}`. core/resonance_tuner.py:627 "
            'declares the parameter as `user_message: str` and calls '
            '`user_message.lower()`.')


def test_no_read_of_prompt_before_it_is_assigned(get_ans_fn):
    """Direct regression pin for the exact line that shipped.

    Compares source order: the first Load of ``prompt`` must not precede the
    first Store of ``prompt``.  Catches re-introduction anywhere in the body,
    not just at the resonance site.
    """
    first_store = None
    first_load = None
    for node in ast.walk(get_ans_fn):
        if isinstance(node, ast.Name) and node.id == 'prompt':
            line = node.lineno
            if isinstance(node.ctx, ast.Store):
                if first_store is None or line < first_store:
                    first_store = line
            elif isinstance(node.ctx, ast.Load):
                if first_load is None or line < first_load:
                    first_load = line

    if first_load is None or first_store is None:
        return  # `prompt` is not both read and assigned — nothing to police

    assert first_load > first_store, (
        f'get_ans reads local `prompt` at line {first_load} but first assigns '
        f'it at line {first_store}. Every call raises UnboundLocalError at the '
        'read; if a try/except wraps it the failure is silent and whatever that '
        'block was meant to do never happens.')
