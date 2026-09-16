"""get_chat_history must not be dead for UUID (guest) users.

MEASURED LIVE 2026-09-10 14:58:55, agent 92583386981, driven as its owner
through the real /chat route.  Two log lines, 50 ms apart:

    14:58:55,851 WARNING get_time_based_history: bad session_id
                 user_3e2908ac-3ff6-4198-bc46-9ec43a2aac9a:
                 invalid literal for int() with base 10:
                 '3e2908ac-3ff6-4198-bc46-9ec43a2aac9a'
    14:58:55,901 tool    content: {"res": []}
                 (tool_call_id lduA6QbgLoheVTuBioeXTnWcceD5Q8oM,
                  = get_chat_history(text='english_progress'))

``get_chat_history`` passes ``f'user_{user_id}'``.  For a guest / UUID user
that is not parseable as an int, so the guard returned ``{'res': []}``
BEFORE ANY STORE WAS QUERIED.  The caller cannot tell that apart from "you
have no history", and on this run the model filled the gap by inventing a
CEFR level and a tense assessment for the user (task #817 / D52).

THE COERCION IS DEAD WEIGHT.  Both consumers of ``user_id`` in this
function take a string:

    ConversationEntry.user_id == str(user_id)      # converts straight back
    SimpleMemChatMemory.load_or_create(user_id)

So ``int()`` only ever narrowed the accepted id space.  For an integer user
``str(123)`` and ``'123'`` are the same string, so those users are
byte-identical before and after — the fix widens, it does not change.

SCOPE, stated so this is not over-read: this makes the function REACH its
stores for a UUID user.  It does NOT by itself mean history is found (the
store may be legitimately empty, and the SimpleMem branch has its own
gating), and it does NOT fix D52 — an empty result becoming a fabricated
answer is a separate hole in a separate place.

    python -m pytest tests/unit/test_history_accepts_uuid_user.py --noconftest -q
"""
import ast
import io
import json
import os

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'helper.py')

# The exact id from the live run.
LIVE_UUID = '3e2908ac-3ff6-4198-bc46-9ec43a2aac9a'


def _src():
    with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _fn_node(name):
    for node in ast.parse(_src()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('%s not found in helper.py' % name)


class _Recorder:
    """Stands in for resolve_recall_window and records that it was reached."""

    def __init__(self):
        self.calls = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        return None            # no window -> falls through to the semantic leg


def _call_with(session_id):
    """Run the REAL function body against stubs.

    Importing hartos.helper pulls the whole backend (torch included), so the
    one function under test is exec'd straight from the shipped source.  Only
    its module-level dependencies are stubbed; the branching logic is the
    code that actually runs in production.
    """
    ns = {
        'time': __import__('time'),
        'current_app': None,          # every logger call here is try/except'd
        'resolve_recall_window': _Recorder(),
    }
    mod = ast.Module(body=[_fn_node('get_time_based_history')], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), MODULE, 'exec'), ns)
    out = ns['get_time_based_history']('english_progress', session_id, None, None)
    return out, ns['resolve_recall_window']


class TestUuidUserReachesTheStore:

    def test_uuid_session_id_is_not_rejected_before_querying(self):
        out, recorder = _call_with('user_' + LIVE_UUID)
        assert recorder.calls, (
            'get_time_based_history returned WITHOUT reaching '
            'resolve_recall_window, i.e. it bailed out in the int(session_id) '
            'guard. Measured live 2026-09-10 14:58:55,851 for session_id '
            'user_%s — the tool then returned {"res": []} and the model '
            'invented a CEFR level for the user (#817).' % LIVE_UUID)

    def test_the_int_coercion_is_gone(self):
        """Drift guard: the narrowing must not come back.

        Pinned to the assignment, not to the whole function, so unrelated
        int() use elsewhere in helper.py is untouched.
        """
        fn = _fn_node('get_time_based_history')
        bad = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, 'id', '') == 'int'):
                for a in node.args:
                    if 'session_id' in ast.dump(a):
                        bad.append(node.lineno)
        assert not bad, (
            'int(session_id...) is back at line(s) %s — that rejects every '
            'UUID/guest user and returns an empty result indistinguishable '
            'from "no history"' % bad)


class TestNoRegressionForIntegerUsers:

    def test_integer_session_id_still_reaches_the_store(self):
        out, recorder = _call_with('user_123')
        assert recorder.calls, 'integer users must be unaffected'

    def test_integer_id_resolves_to_the_same_string_as_before(self):
        """`str(int('123'))` == `'123'`, so the DB filter is unchanged.

        This is the whole no-regression argument for the 653-equivalent
        population: the only consumer stringifies, so dropping the int()
        round-trip cannot change what is queried for a numeric id.
        """
        assert str(int('123')) == '123'


class TestStillGuardsGarbage:

    def test_empty_id_after_prefix_is_still_refused(self):
        """A session_id with nothing after the prefix is not a user."""
        out, recorder = _call_with('user_')
        assert not recorder.calls, (
            'an empty user id must not be used to query the store')
        assert json.loads(out) == {'res': []}
