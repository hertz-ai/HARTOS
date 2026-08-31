"""#721 - reuse_recipe must NOT stringize its annotations, or tool registration dies.

LIVE-PROVEN 2026-08-29 20:23 from a real /chat reuse turn (agent 52612946585):
agent reuse loaded its recipe (all 15 actions), instantiated the assistant, then
CRASHED registering its tools:

    reuse_recipe.py:1322  @helper.register_for_llm(
    autogen/function_utils.py:136 -> autogen/_pydantic.py:25  TypeAdapter(t).json_schema()
    pydantic.errors.PydanticUserError:
      TypeAdapter[ForwardRef("Annotated[str, 'Target persona/role name to deliver
      the message to']")] is not fully defined

16 such frames in one log window - every tool, every turn.  Cause: the file had
`from __future__ import annotations` (PEP 563), so autogen's register_for_llm
received a ForwardRef STRING instead of a real Annotated[...] object, and
pydantic cannot resolve it.

CONTROL GROUP: create_recipe.py / helper.py / hart_intelligence_entry.py have no
future import and register tools fine.  reuse_recipe was the only module with it
and the only one that threw.

WHY IT WAS THERE, AND WHY DELETING IT ALONE IS WRONG: the blanket PEP 563 was
also deferring the heavy autogen import - `autogen` here is a lazy proxy
(reuse_recipe.py:34 lazy_module("autogen")), and evaluating any module-scope
annotation that touches autogen.X forces the real import.  My first attempt
quoted only the two annotated globals and was REFUTED by the repo's own guard
(tests/unit/test_lazy_autogen_import.py failed: autogen ended up in
sys.modules).  The complete set was found empirically, by swapping the proxy for
one that raises on __getattr__ and importing until the traceback went silent:

    user_agents      global annotation
    role_agents      global annotation
    create_agents_for_user   RETURN annotation
    get_agent_response       6 PARAMETER annotations

Nine annotation positions across four statements.  All are quoted individually,
so laziness is preserved while every OTHER annotation in the file - crucially the
Annotated[...] tool parameters inside create_agents_for_user - stays a real
object that register_for_llm can schematize.

These two tests pin both halves of that bargain.
"""
import ast
import io
import os

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'hartos/reuse_recipe.py')


def _read(path=_SRC):
    with io.open(path, encoding='utf-8') as f:
        return f.read()


def _future_annotations_present(src):
    for node in ast.parse(src).body:
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            if any(a.name == 'annotations' for a in node.names):
                return True
    return False


def _unquoted_autogen_annotations(src):
    """Module-scope annotations that touch autogen.* and are NOT string literals.

    Each one forces the lazy autogen proxy to resolve at import time.
    """
    tree = ast.parse(src)
    out = []

    def check(ann, label, lineno):
        if ann is None or isinstance(ann, ast.Constant):
            return
        seg = ast.get_source_segment(src, ann) or ''
        if 'autogen.' in seg:
            out.append((lineno, label, seg[:60]))

    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            check(node.annotation, 'global', node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                        + [a.vararg, a.kwarg]):
                if arg is not None:
                    check(arg.annotation, f'{node.name}({arg.arg})', node.lineno)
            check(node.returns, f'{node.name}(return)', node.lineno)
    return out


def test_no_future_annotations_import():
    """PEP 563 here breaks autogen tool registration for the whole reuse path."""
    assert not _future_annotations_present(_read()), (
        "reuse_recipe.py must not use `from __future__ import annotations`: it "
        "stringizes the Annotated[...] tool parameters, and autogen's "
        "register_for_llm resolves those through pydantic TypeAdapter, which "
        "raises PydanticUserError on a ForwardRef. Agent reuse then dies at tool "
        "registration on every turn (live-proven 2026-08-29). Quote the "
        "individual autogen annotations instead - see the other test here.")


def test_every_module_scope_autogen_annotation_is_quoted():
    """The other half: laziness must survive without the blanket import.

    tests/unit/test_lazy_autogen_import.py asserts the OUTCOME (autogen absent
    from sys.modules after import). This asserts the CAUSE, so a newly-added
    unquoted annotation is caught at the line that introduced it rather than as
    a mysterious import-time regression.
    """
    bad = _unquoted_autogen_annotations(_read())
    assert not bad, (
        "these module-scope annotations touch the lazy autogen proxy and will "
        "force the heavy import at module load - quote them as strings:\n"
        + "\n".join(f"  line {ln}: {label} -> {seg}" for ln, label, seg in bad))
