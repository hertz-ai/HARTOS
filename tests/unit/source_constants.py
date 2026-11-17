"""module_constant: a module-level constant's value, read from its source file
without importing the module.

For the harnesses that lift functions and _REUSE_* constants out of
hartos/reuse_recipe.py and run them in a namespace of their own because the
import is the problem: hartos.reuse_recipe, hartos.lifecycle_hooks and
core.constants each load torch (measured 2026-11-02: 8.8 s and 5.3 s for the
last two), which is an OSError at collection on a workstation with a broken
torch.  A lifted line that names another module's constant (SEND_TOOL, since
31ec05d14) then gets the value that module ships, not a copy in the test.
"""
import ast
import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def module_constant(name, relpath):
    """The literal value ``name`` is assigned at module level in ``relpath``
    (relative to the repository root).  AssertionError when there is no such
    literal assignment, so a renamed or computed constant fails the harness
    loudly instead of running on a guess."""
    with open(os.path.join(_ROOT, relpath), encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f'no module-level literal {name} in {relpath}')
