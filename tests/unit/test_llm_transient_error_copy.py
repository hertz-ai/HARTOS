"""A transient LLM state must not reach the user as raw upstream text.

Live 2026-08-18, from llm_outbound.jsonl -- the conversation the model was
later given as its own history:

    User: Hi!
    Assistant: I couldn't process that request - Loading model
    User: Hi!
    Assistant: I couldn't process that request - Loading model
    User: Yeah
    Assistant: Hi there! Ready to help with whatever you need.

"Loading model" is llama-server's HTTP body while weights load; it answers
200 with {"error": {"message": "Loading model"}} rather than failing, so the
error branch in hart_intelligence_entry formatted it straight into the reply.
Three problems in one string:

  1. It leaks llama.cpp's internal wording to an end user.
  2. It reads as a permanent failure ("I couldn't process that request")
     when the state is transient -- the third turn worked.
  3. It is returned as a normal assistant turn, so it was persisted into
     conversation history and replayed to the model as a prior turn.

routes/chatbot_routes.py already ships friendly copy for the neighbouring
state (server unreachable).  Both now say the same thing, so "engine down"
and "engine warming up" are indistinguishable to the user, which is correct
-- the action is the same either way.
"""
import re

from core import constants as C


def _error_reply(msg: str) -> str:
    """Mirror of the branch in hart_intelligence_entry, kept tiny on purpose.

    The real branch sits deep inside a request handler that needs a Flask app,
    a live llama-server and a G12 distillation future.  What this pins is the
    MAPPING (upstream text -> user copy); the surrounding plumbing is covered
    elsewhere.  A drift test below asserts the real site still uses these
    constants, so this mirror cannot silently diverge.
    """
    low = (msg or '').strip().lower()
    if any(m in low for m in C.LLM_TRANSIENT_LOADING_MARKERS):
        return C.LLM_LOADING_REPLY
    return C.LLM_GENERIC_ERROR_REPLY


def test_loading_model_maps_to_the_warming_up_copy():
    assert _error_reply('Loading model') == C.LLM_LOADING_REPLY


def test_loading_variants_are_recognised():
    for msg in ('Loading model', 'loading model', '  LOADING MODEL  ',
                'the model is loading', 'model loading, please wait'):
        assert _error_reply(msg) == C.LLM_LOADING_REPLY, msg


def test_a_real_error_does_not_get_the_warming_up_copy():
    """n_ctx overflow is a genuine failure; telling the user to wait would be
    wrong -- waiting never fixes it."""
    assert _error_reply(
        'the request exceeds the available context size') == C.LLM_GENERIC_ERROR_REPLY


def test_upstream_text_never_appears_in_the_reply():
    """The whole point: no upstream message reaches the user."""
    for msg in ('Loading model', 'n_ctx exceeded', 'CUDA out of memory',
                'slot unavailable'):
        assert msg.lower() not in _error_reply(msg).lower(), msg


def test_no_em_dash_in_user_facing_copy():
    """The original used an em dash to splice in the internal text."""
    for s in (C.LLM_LOADING_REPLY, C.LLM_GENERIC_ERROR_REPLY):
        assert '—' not in s, f'em dash in user-facing copy: {s!r}'


def test_copy_is_not_empty_and_is_actionable():
    for s in (C.LLM_LOADING_REPLY, C.LLM_GENERIC_ERROR_REPLY):
        assert s.strip()
        assert len(s.split()) >= 4, f'too terse to be useful: {s!r}'


def test_loading_copy_matches_the_route_that_already_shipped_it():
    """Drift guard across repos: routes/chatbot_routes.py (Nunba) sends this
    same wording when the server is unreachable.  If one is reworded without
    the other, the user gets two different explanations of one situation."""
    import os
    route = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        '..', 'Nunba-HART-Companion', 'routes', 'chatbot_routes.py')
    route = os.path.normpath(route)
    if not os.path.exists(route):
        import pytest
        pytest.skip('Nunba sibling not present')
    with open(route, encoding='utf-8') as fh:
        src = fh.read()
    assert 'Starting the local AI engine for you now.' in src, (
        'the Nunba route no longer ships this wording; core.constants'
        '.LLM_LOADING_REPLY has drifted from it')


def test_real_site_uses_the_constants():
    """Drift guard: the mirror above is only valid while the real branch
    routes through these constants."""
    import os
    p = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))),
        'hart_intelligence_entry.py')
    with open(p, encoding='utf-8') as fh:
        src = fh.read()
    assert 'LLM_TRANSIENT_LOADING_MARKERS' in src
    assert 'LLM_LOADING_REPLY' in src
    assert 'LLM_GENERIC_ERROR_REPLY' in src
    # and the leaky f-string must not come back
    assert not re.search(r'_err_reply\s*=\s*f"I couldn', src), (
        'the raw upstream message is being spliced into the reply again')
