"""The CREATE trace banker finds an action's window with THE dispatch parser.

hartos/create_recipe.py::_bank_action_recipe_from_trace opened a window on any
message CONTAINING f'Execute Action {id}:' and closed it on any message
containing 'Execute Action '.  That is a second dispatch parser, and it
disagrees with the canonical one, hartos.lifecycle_hooks.dispatch_action_id,
which honours only a LEADING marker -- because CREATE's own dispatch appends
the user's text after its marker, and that text can quote an earlier dispatch
("... ,Latest User message: Properly Execute Action 2: ...").

Reviewer's probe: for the message
    'Execute Action 5: publish the post ,Latest User message: '
    'Properly Execute Action 2: search the web'
dispatch_action_id says 5, but the banker opened action 2's newest window on
it and banked action 5's post_to_social as action 2's recipe.

These tests run the real banker (extract-and-exec, as
test_trace_action_banking does) and the real parser.
"""
import ast
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hartos.lifecycle_hooks import dispatch_action_id  # noqa: E402
from tests.unit.test_trace_action_banking import banked  # noqa: E402,F401

_QUOTING_RETRY = ('Execute Action 5: publish the post ,Latest User message: '
                  'Properly Execute Action 2: search the web')


def _call(name, call_id):
    return {'content': '', 'tool_calls': [{'id': call_id, 'function': {
        'name': name, 'arguments': '{}'}}]}


class TestBankerWindowsAgreeWithTheParser:
    def test_a_quoted_marker_does_not_open_a_window(self, banked):
        """The reviewer's probe, as an assertion."""
        assert dispatch_action_id(_QUOTING_RETRY) == 5  # the parser's verdict
        ok, data, _ = banked([
            {'content': 'Execute Action 2: search the web'},
            _call('google_search', 'a'),
            {'content': 'Execute Action 3: summarize'},
            {'content': 'Execute Action 4: draft'},
            {'content': _QUOTING_RETRY},
            _call('post_to_social', 'b'),
        ], action_id=2)
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == ['google_search'], \
            data['recipe']

    def test_a_quoted_marker_does_not_close_a_window(self, banked):
        """Text that merely mentions a dispatch is not a dispatch: the work
        after it still belongs to the action that is running."""
        ok, data, _ = banked([
            {'content': 'Execute Action 2: search the web'},
            {'content': 'Noted. The plan says Execute Action 3: comes next.',
             'name': 'Assistant'},
            _call('google_search', 'a'),
        ], action_id=2)
        assert ok is True
        assert [s['tool_name'] for s in data['recipe']] == ['google_search'], \
            data['recipe']

    def test_an_action_dispatched_only_inside_quoted_text_is_not_banked(
            self, banked):
        """Action 2 never led a message in this run, so there is nothing of
        its own to bank (the IN-RUN rule)."""
        ok, data, _ = banked([
            {'content': _QUOTING_RETRY},
            _call('post_to_social', 'b'),
        ], action_id=2)
        assert ok is False and data is None

    def test_every_leading_dispatch_shape_opens_and_closes_windows(self, banked):
        """Control: the shapes the parser accepts (retry tag, "Properly", the
        REUSE marker) still open action 2's window and end action 1's."""
        for opener in ('[retry:exec-2] Execute Action 2: again',
                       'Properly Execute Action 2: search',
                       'Perform this action -> Action #2: search'):
            ok, data, _ = banked([
                {'content': 'Execute Action 1: first'},
                _call('first_tool', 'x'),
                {'content': opener},
                _call('second_tool', 'y'),
            ], action_id=2)
            assert ok is True, opener
            assert [s['tool_name'] for s in data['recipe']] == ['second_tool'], \
                (opener, data['recipe'])
            ok, data, _ = banked([
                {'content': 'Execute Action 1: first'},
                _call('first_tool', 'x'),
                {'content': opener},
                _call('second_tool', 'y'),
            ], action_id=1)
            assert [s['tool_name'] for s in data['recipe']] == ['first_tool'], \
                (opener, data['recipe'])


# ---------------------------------------------------------------------------
# The guard that keeps it at one parser.
# ---------------------------------------------------------------------------
_MARKER_WORDS = ('Execute Action', 'Perform this action -> Action #')


def _literal_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def _marker_tests(root, rel):
    """Each `<marker literal> in <text>`, or a startswith / re call on a
    marker literal, under ``root``: a hand-rolled dispatch parser."""
    hits = []
    for node in ast.walk(root):
        if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            t = _literal_text(node.left)
            if t and any(w in t for w in _MARKER_WORDS):
                hits.append(f'{rel}:{node.lineno} containment')
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('startswith', 'search', 'match',
                                       'findall', 'finditer', 'fullmatch')):
            for a in node.args:
                t = _literal_text(a)
                if t and any(w in t for w in _MARKER_WORDS):
                    hits.append(f'{rel}:{node.lineno} {node.func.attr}')
    return hits


def test_source_guard_banker_has_no_second_dispatch_parser():
    """DRY guard, not the behaviour test (that is the class above).

    The banker must find dispatches with lifecycle_hooks.dispatch_action_id;
    any containment / prefix / regex test on a dispatch-marker literal inside
    it fails here, and so does a banker that stops calling the parser.
    """
    path = os.path.join(_ROOT, 'hartos', 'create_recipe.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == '_bank_action_recipe_from_trace')
    assert _marker_tests(fn, 'create_recipe.py') == []
    called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name)}
    assert 'dispatch_action_id' in called, \
        'the banker must read dispatches through dispatch_action_id'


def test_source_guard_catches_what_it_guards():
    """The shape detector must be able to fail."""
    tree = ast.parse("a = f'Execute Action {i}:' in c\n"
                     "b = 'Execute Action ' in c\n"
                     "d = c.startswith('Perform this action -> Action #')\n")
    kinds = sorted(h.split(' ', 1)[1] for h in _marker_tests(tree, 'probe'))
    assert kinds == ['containment', 'containment', 'startswith'], kinds
