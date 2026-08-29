"""#719 — the wire-trim budget must charge EVERY schema block, and must say so
when the trim fails to reach the budget.

Measured on the live 400 (2026-08-29 16:21:36,561, autogen.create, n_ctx 8192):

    messages   2 msgs      3,860 chars  (~965 tok)
    functions  5 entries   2,918 chars  (~729 tok)   <- NEVER CHARGED
    tools     70 entries  39,959 chars (~9,989 tok)  <- charged correctly
    max_tokens 1500 ; server rejected at 11,236 prompt tokens

The `[TRIM]` line one second earlier read "est tokens 920->795, budget 351".
That reconciles exactly: 12288 - 1500 - 256 - 10,181 = 351, so the tool block
WAS counted.  The defect is what happens next: 795 > 351, the trim did not
reach the budget (the system message alone exceeds it, and truncating the LAST
message cannot shrink the SYSTEM message), and the request was sent regardless.
`_apply_trim_to_request` treats "we truncated something" as success.

Two testable consequences:
  1. `functions` is never charged  -> the budget is overstated by its cost.
  2. Nothing verifies the post-trim total  -> a doomed request goes out silently.
"""
import logging

import core.llm_outbound_logger as lol


def _mk_tool(name: str, filler: str = 'x' * 200) -> dict:
    return {'type': 'function',
            'function': {'name': name, 'description': filler,
                         'parameters': {'type': 'object', 'properties': {}}}}


def _mk_function(name: str, filler: str = 'x' * 200) -> dict:
    return {'name': name, 'description': filler,
            'parameters': {'type': 'object', 'properties': {}}}


def test_functions_block_is_charged_to_the_budget():
    """A body carrying only `functions` must not be costed at zero.

    autogen sends BOTH `functions` (legacy OpenAI) and `tools`; the live 400
    carried 5 functions worth ~729 tokens that nothing charged.
    """
    body = {'model': 'llama', 'messages': [{'role': 'user', 'content': 'hi'}],
            'functions': [_mk_function(f'f{i}') for i in range(20)]}
    assert lol._schema_tokens(body, 'llama') > 0, (
        'the functions block costs real prompt tokens and must be charged')


def test_tools_block_is_still_charged():
    """Regression guard for the 2026-08-07 fix — do not lose it."""
    body = {'model': 'llama', 'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [_mk_tool(f't{i}') for i in range(20)]}
    assert lol._schema_tokens(body, 'llama') > 0


def test_both_blocks_are_charged_together():
    """autogen sends both; the cost is the sum, not either one."""
    fns = [_mk_function(f'f{i}') for i in range(10)]
    tls = [_mk_tool(f't{i}') for i in range(10)]
    base = {'model': 'llama', 'messages': [{'role': 'user', 'content': 'hi'}]}
    only_fn = lol._schema_tokens({**base, 'functions': fns}, 'llama')
    only_tl = lol._schema_tokens({**base, 'tools': tls}, 'llama')
    both = lol._schema_tokens({**base, 'functions': fns, 'tools': tls}, 'llama')
    assert both >= only_fn + only_tl - 4, (
        f'both blocks must be charged: {both} < {only_fn} + {only_tl}')


def test_schema_free_body_costs_nothing():
    """No schema block -> no charge.  Guards against over-counting."""
    body = {'model': 'llama', 'messages': [{'role': 'user', 'content': 'hi'}]}
    assert lol._schema_tokens(body, 'llama') == 0


def test_trim_that_cannot_reach_budget_says_so(caplog):
    """The live failure mode: trim runs, cannot reach budget, sends anyway.

    Reproduces the shape of the 2026-08-29 400 — a system message far larger
    than the budget left after the tool schema is charged.  Truncating the
    LAST message cannot shrink the SYSTEM message, so the trim is doomed.
    Before this fix nothing reported that; the only signal was the eventual
    HTTP 400 from llama-server.
    """
    body = {
        'model': 'llama',
        'max_tokens': 1500,
        'messages': [
            {'role': 'system', 'content': 'S ' * 4000},   # far over any budget
            {'role': 'user', 'content': 'U ' * 200},
        ],
        'tools': [_mk_tool(f't{i}', 'd' * 400) for i in range(60)],
    }
    with caplog.at_level(logging.ERROR, logger=lol.logger.name):
        trimmed, n_dropped, n_trunc, est_before, est_after, budget = \
            lol._trim_to_budget(body)

    # The trim genuinely cannot reach the budget in this shape.
    assert est_after > budget, (
        'test premise broken: this body was supposed to be untrimmable to '
        f'budget (est_after={est_after}, budget={budget})')

    # ...and that fact must be stated, not swallowed.
    blob = ' '.join(r.getMessage() for r in caplog.records).lower()
    assert 'over budget' in blob or 'could not' in blob or 'still' in blob, (
        'a trim that fails to reach its budget must be reported at ERROR; '
        f'captured instead: {blob[:400]!r}')
