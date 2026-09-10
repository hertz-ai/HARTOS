"""Every steer this module posts into the group must be pipeline text.

MEASURED LIVE 2026-09-10 14:23:10, agent 92583386981 ("English Learning
Session"), driven as its owner through the real /chat route.  The agent did
its work — the flow-0 recipe was selected, the tool ran for real:

    14:23:16  Tier-1 named attach: action 1 names ['get_chat_history'] -> 1 tools
    14:23:43  [FAB-GUARD] action 1 names tool(s) ['get_chat_history'];
              executed=['get_chat_history', 'search_long_term_memory',
              'send_message_to_user']; unrun=[]

and the user got THIS as the whole reply, HTTP 200 in 42.8s:

    "Your tool for this action has already executed and returned its result.
     Do not re-run it. Either report this action completed, or state the exact
     remaining step that still needs a tool call."

That is not the agent's lesson.  It is the module's own UNDER-REPORTED steer,
posted to the group at reuse_recipe.py:4854 and then taken as the reply.

THIS IS THE FIFTH PRODUCER OF THE SAME DEFECT.  _reuse_is_pipeline_text's own
docstring names three and says it "kept being one producer behind"; the
autonomy nudge was the fourth (fixed 7af46de6b, same session).  Patching a
fifth literal the same way would leave a sixth.

So this file has two jobs:

  1. Close the two inline steers that are still unrecognised —
     :4854 the UNDER-REPORTED steer, and :4815 f"Work on subtask: {...}",
     the BREAKDOWN steer, which is module-written text on exactly the same
     footing and one refused action away from the same exposure.

  2. TestNoSixthProducer — walk every `initiate_chat(message=...)` site in the
     module and require that any statically-known message text is recognised.
     A new inline steer then fails here on the day it is written, instead of
     on the day a user reads it.

    python -m pytest tests/unit/test_reuse_steers_are_pipeline_text.py --noconftest -q
"""
import ast
import io
import os

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'hartos', 'reuse_recipe.py')

# Verbatim from the live 14:23 reply.
UNDER_REPORT = ('Your tool for this action has already executed and returned '
                'its result. Do not re-run it. Either report this action '
                'completed, or state the exact remaining step that still '
                'needs a tool call.')
SUBTASK_PREFIX = 'Work on subtask: '


def _src():
    with io.open(MODULE, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _predicate():
    """Compile the REAL _reuse_is_pipeline_text out of the shipped file.

    Importing hartos.reuse_recipe pulls autogen -> llmlingua -> torch, which
    is an OSError at collection time on a workstation with a broken torch.
    The predicate depends only on three module constants, so those are
    compiled alongside it — this runs the same bytes production runs, not a
    restatement of them.
    """
    tree = ast.parse(_src())
    ns = {}
    wanted_fn = '_reuse_is_pipeline_text'
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith('_REUSE_'):
                    body.append(node)
                    break
        elif isinstance(node, ast.FunctionDef) and node.name == wanted_fn:
            body.append(node)
    mod = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), MODULE, 'exec'), ns)
    assert wanted_fn in ns, '%s not found in reuse_recipe.py' % wanted_fn
    return ns[wanted_fn]


def _module_string_constants(tree):
    """Module-scope NAME -> str, so a promoted steer is still resolvable."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    try:
                        v = ast.literal_eval(node.value)
                    except Exception:
                        continue
                    if isinstance(v, str):
                        out[t.id] = v
    return out


def _steer_texts():
    """Statically-resolvable message texts of every initiate_chat call site.

    Resolves three shapes: a plain literal, the leading constant run of an
    f-string, and a reference to a module-scope string constant (possibly
    concatenated with a runtime value, as the subtask steer is).

    RESOLVING THE NAME CASE IS LOAD-BEARING, and this file learned it the
    hard way on its own first green run.  When both inline literals were
    promoted to constants, a literals-only sweep found ZERO sites — so
    test_every_static_steer_is_recognised passed over an empty set, i.e.
    vacuously, on a file where a sixth producer could be added freely.
    test_the_enumeration_actually_found_sites caught that; this is the fix it
    demanded.  A guard that stops seeing its subject is not a guard.
    """
    tree = ast.parse(_src())
    consts = _module_string_constants(tree)
    out = []

    def resolve(node):
        try:
            v = ast.literal_eval(node)
            if isinstance(v, str):
                return v
        except Exception:
            pass
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        if isinstance(node, ast.JoinedStr):
            lead = ''
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    lead += part.value
                else:
                    break
            return lead or None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            # `_CONST + runtime_value` — the constant half is the marker.
            return resolve(node.left) or resolve(node.right)
        return None

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if getattr(n.func, 'attr', getattr(n.func, 'id', '')) != 'initiate_chat':
            continue
        msg = None
        for kw in n.keywords:
            if kw.arg == 'message':
                msg = kw.value
        if msg is None:
            continue
        text = resolve(msg)
        if text and text.strip():
            out.append((n.lineno, text))
    return out


class TestTheTwoInlineSteers:
    """The exact texts, so the fix cannot be a near-miss."""

    def test_under_report_steer_is_pipeline_text(self):
        assert _predicate()(UNDER_REPORT), (
            'the UNDER-REPORTED steer (reuse_recipe.py:4854) is text this '
            'module wrote to steer the group; unrecognised, it reaches the '
            'user as the agent answer — measured live 2026-09-10 14:23:10 on '
            'agent 92583386981, whose tool HAD run (FAB-GUARD unrun=[])')

    def test_subtask_steer_is_pipeline_text(self):
        assert _predicate()(SUBTASK_PREFIX + 'summarise the log file'), (
            'the BREAKDOWN steer (reuse_recipe.py:4815) is module-written '
            'text on the same footing as the four already closed')


class TestNoSixthProducer:
    """The durable half: a new inline steer must fail HERE, not in chat.

    _reuse_is_pipeline_text's docstring records that it "kept being one
    producer behind" — three producers found one at a time, then the autonomy
    nudge, then this one.  Enumerating the call sites is what turns that into
    a closed set.
    """

    def test_every_static_steer_is_recognised(self):
        pred = _predicate()
        unrecognised = [(ln, t) for ln, t in _steer_texts() if not pred(t)]
        assert not unrecognised, (
            'these initiate_chat messages are written by this module but are '
            'not recognised as pipeline text, so each can be delivered to the '
            'user as the agent answer:\n' +
            '\n'.join('  reuse_recipe.py:%d  %r' % (ln, t[:120])
                      for ln, t in unrecognised))

    def test_the_enumeration_actually_found_sites(self):
        """Guard the guard: an empty sweep would pass vacuously."""
        found = _steer_texts()
        assert len(found) >= 2, (
            'expected at least the two inline steers; found %d — the AST '
            'sweep has stopped matching and the guard above is now vacuous'
            % len(found))


class TestPrecisionNoRegression:
    """Refuse the module's own markers, never the agent's sentiment."""

    def test_honest_failure_report_still_reaches_the_user(self):
        pred = _predicate()
        honest = ('Since no specific text was provided in your input, I '
                  'could not summarize anything.')
        assert not pred(honest)

    def test_a_real_lesson_is_not_pipeline_text(self):
        pred = _predicate()
        answer = ('You are at B1. Today we work on the present perfect: '
                  '"I have lived here for two years."')
        assert not pred(answer)

    def test_a_report_that_merely_mentions_a_tool_is_not_pipeline_text(self):
        """Nearest legitimate neighbour of the under-report steer."""
        pred = _predicate()
        assert not pred('I ran the search tool and found three results.')

    def test_empty_and_none_are_not_pipeline_text(self):
        pred = _predicate()
        assert not pred('')
        assert not pred(None)


class TestOneSourceOfTruth:
    """Each steer text lives in exactly one place, like its four siblings."""

    def _module_constants(self):
        names = {}
        for node in ast.parse(_src()).body:          # module scope only
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        try:
                            v = ast.literal_eval(node.value)
                        except Exception:
                            continue
                        if isinstance(v, str):
                            names[t.id] = v
        return names

    def test_under_report_steer_is_a_named_module_constant(self):
        vals = self._module_constants().values()
        assert any(UNDER_REPORT == v for v in vals), (
            'the steer is still an inline literal at the call site; promote '
            'it to a module constant so the producer and the predicate cannot '
            'drift apart')

    def test_each_steer_text_is_held_once(self):
        nodes = [n for n in ast.walk(ast.parse(_src()))
                 if isinstance(n, ast.Constant) and n.value == UNDER_REPORT]
        assert len(nodes) == 1, (
            'the under-report steer text is held in %d places — a surviving '
            'inline copy will drift from the constant the predicate tests'
            % len(nodes))
