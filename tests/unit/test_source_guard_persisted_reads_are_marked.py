"""Guard: every tool that reads stored memory is classified (#104).

The group chat's write-back leaves out the results of tools marked
reads_persisted_state, so a read of stored state is not stored again. The mark
is opt-in: a reader registered without it re-stores what it reads, bounded only
by MEMORY_ITEM_MAX_CHARS, which is the growth #104 ended (Guardian
Convergence's graph reached 28.6M chars on central, 2026-09-14).

This finds, in the two tool builders, every REGISTERED tool whose body reads
MemoryGraph or SimpleMem, directly or by calling something in the same builder
that does (get_data_by_key reads through _read_saved), and fails unless the
tool is either marked or listed in _STORED_ON_PURPOSE with a reason. Calls are
followed broadly on purpose: a new tool that returns another reader's result
verbatim is caught too, at the cost of one reasoned entry for a tool that only
writes through a reader. A read is recognised by the name the store goes by
in the builders (memory_graph or graph, simplemem_store), so a tool that reads
through another name for the store (mg = memory_graph) is not seen: keep the
builders' names. Source structure is the subject here, so the guard reads the
source.

    python -m pytest tests/unit/test_source_guard_persisted_reads_are_marked.py --noconftest -q
"""
import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BUILDERS = {
    'core/agent_tools.py': 'build_core_tool_closures',
    'integrations/channels/memory/agent_memory_tools.py': 'create_memory_tools',
}
_GRAPH_READS = {'recall', 'backtrace', 'backtrace_semantic',
                'get_session_memories', 'context_recall'}
_GRAPH_NAMES = {'memory_graph', 'graph'}

# Tools that read stored memory, directly or through another reader, but whose
# RESULT is not a read of stored state: new work, or the report of an action.
# The write-back stores their results; marking them would drop that from
# memory. Each entry carries its reason.
_STORED_ON_PURPOSE = {
    'self_critique_and_enhance': (
        'Self_Critique_And_Enhance and self_critique_and_enhance return a short '
        'critique (5 suggestions and 5 observations cut to 100 chars each) and '
        'register a new insight; the result is new work and cannot re-inflate '
        'on the next recall'),
    'generate_receipt': (
        'reads the saved receipt template and logo path, and returns a receipt '
        "rendered from them and the call's fields: new output for the customer"),
    'set_receipt_logo': (
        'copies the logo and saves its path through save_data_in_memory, whose '
        'save check reads it back; it returns a one-line confirmation, not the '
        'stored value'),
}


def _builder(path, name):
    tree = ast.parse((_ROOT / path).read_text(encoding='utf-8'))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _reads_directly(fn):
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        name = owner.id if isinstance(owner, ast.Name) else None
        if name in _GRAPH_NAMES and node.func.attr in _GRAPH_READS:
            return True
        if name == 'simplemem_store' and node.func.attr == 'search':
            return True
    return False


def _nested(builder):
    return {fn.name: fn for fn in ast.walk(builder)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and fn is not builder}


def _called_names(fn):
    return {n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _memory_readers(builder):
    """Nested functions that read stored memory, directly or through another
    function defined in the same builder."""
    fns = _nested(builder)
    readers = {name for name, fn in fns.items() if _reads_directly(fn)}
    grew = True
    while grew:
        grew = False
        for name, fn in fns.items():
            if name not in readers and _called_names(fn) & readers:
                readers.add(name)
                grew = True
    return readers


def _fn_name(expr):
    if isinstance(expr, ast.Name):
        return expr.id
    if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
            and expr.func.id == 'reads_persisted_state' and expr.args
            and isinstance(expr.args[0], ast.Name)):
        return expr.args[0].id
    return None


def _registered(builder):
    """The functions the builder registers as tools: the third element of each
    tools.append((name, description, fn)), and the first element of each
    (fn, description) value in a returned dict."""
    names = set()
    for node in ast.walk(builder):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'append' and node.args
                and isinstance(node.args[0], ast.Tuple)
                and len(node.args[0].elts) == 3):
            names.add(_fn_name(node.args[0].elts[2]))
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for v in node.value.values:
                if isinstance(v, ast.Tuple) and len(v.elts) == 2:
                    names.add(_fn_name(v.elts[0]))
    names.discard(None)
    return names


def _readers(builder):
    fns = _nested(builder)
    return {name: fns[name].lineno
            for name in _registered(builder) & _memory_readers(builder)}


def _marked(builder):
    return {node.args[0].id for node in ast.walk(builder)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'reads_persisted_state'
            and node.args and isinstance(node.args[0], ast.Name)}


def test_source_guard_every_memory_reading_tool_is_classified():
    unclassified = []
    for path, builder_name in _BUILDERS.items():
        builder = _builder(path, builder_name)
        marked = _marked(builder)
        for name, lineno in sorted(_readers(builder).items()):
            if name not in marked and name not in _STORED_ON_PURPOSE:
                unclassified.append(f'{path}:{lineno} {name}')
    assert not unclassified, (
        'tools that read stored memory but are neither marked '
        'reads_persisted_state (a verbatim read, not stored again) nor listed '
        'in _STORED_ON_PURPOSE (new work or an action report, stored): '
        + ', '.join(unclassified))


def test_source_guard_a_tool_stored_on_purpose_is_never_marked():
    core = _builder('core/agent_tools.py', 'build_core_tool_closures')
    assert not set(_STORED_ON_PURPOSE) & _marked(core), (
        'a tool whose result is new work, marked reads_persisted_state, would '
        'drop that work from the write-back')
    assert set(_STORED_ON_PURPOSE) <= set(_readers(core)), (
        'a listed tool no longer reads memory; drop its entry')


def test_source_guard_sees_the_readers_it_exists_for():
    """A guard that finds no readers passes on nothing, so pin the known ones,
    including one that reads through a helper and one helper that is not a
    tool."""
    core_builder = _builder('core/agent_tools.py', 'build_core_tool_closures')
    core = _readers(core_builder)
    graph_tools = _readers(_builder(
        'integrations/channels/memory/agent_memory_tools.py', 'create_memory_tools'))
    assert {'get_data_by_key', 'search_long_term_memory'} <= set(core)
    assert '_read_saved' in _memory_readers(core_builder)
    assert '_read_saved' not in core, 'an unregistered helper is not a tool'
    assert {'recall_memory', 'backtrace_memory', 'get_memory_context'} <= set(graph_tools)
