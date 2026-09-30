"""A rate-limited REUSE round ends the turn instead of retrying at once.

Measured on central 2026-09-13 (task #91): 429s per minute went from 1-4 to
28 and then 20, and in the busiest minute reuse_recipe logged 41 of them as
"WE have some indexx error here: Error code: 429 ... rate_limit_exceeded".
The REUSE loop's blanket except swallowed the rate-limit error and started
the next round at once, so a turn spent its whole round allowance on
back-to-back 429s against a shared, rate-limited endpoint.

Importing reuse_recipe hangs in a bare pytest env (see
test_trace_action_banking), so this reads the handler's source. The except
that logs "indexx error" must re-raise a 429 before anything else, and must
not break: get_agent_response's outer handler turns the re-raised error into
user_facing_error(e), "I couldn't finish that: Error code: 429 ...", which the
hive worker releases for a paced retry, while a break would fall through to
returning the last message as if it were an answer.

Run:
  pytest tests/unit/test_reuse_rate_limit_ends_the_turn.py -q
"""
import ast
import os

_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'hartos',
                    'reuse_recipe.py')


def _tree():
    with open(_SRC, encoding='utf-8') as fh:
        return ast.parse(fh.read())


def _indexx_handler(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.ExceptHandler)
                and 'WE have some indexx error here' in ast.unparse(node)):
            return node
    raise AssertionError('the REUSE loop handler was not found')


def test_the_handler_re_raises_a_rate_limit_before_anything_else():
    first = _indexx_handler(_tree()).body[0]
    assert isinstance(first, ast.If), (
        'the 429 check must be the first thing the handler does')
    cond = ast.unparse(first.test)
    assert 'status_code' in cond and '429' in cond, cond
    assert any(isinstance(n, ast.Raise) for n in ast.walk(first)), (
        'a rate-limit error must be re-raised, not retried')


def test_a_rate_limit_does_not_break_out_as_if_answered():
    first = _indexx_handler(_tree()).body[0]
    assert not any(isinstance(n, ast.Break) for n in ast.walk(first)), (
        'a break returns the last message as the reply, so the rate-limited '
        'turn would be recorded as work done')


def test_the_outer_handler_returns_the_failed_turn_reply():
    fn = next(n for n in ast.walk(_tree())
              if isinstance(n, ast.FunctionDef) and n.name == 'get_agent_response')
    outer = [n for n in fn.body if isinstance(n, ast.Try)][-1]
    handlers = ' '.join(ast.unparse(h) for h in outer.handlers)
    assert 'user_facing_error(e)' in handlers, handlers[:300]
