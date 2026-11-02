"""
task_delegation_bridge must not push its own root to the front of sys.path.

In an installed build the module lives in <site-packages>/integrations/
internal_comm/, so its "repo root" is site-packages itself.  It used to run
``sys.path.insert(0, _ROOT)`` unconditionally at import.  In the frozen Nunba
app (Python 3.11) that root is python-embed's site-packages (Python 3.12), so
the insert put cp312 packages ahead of the app's own: ``import uuid_utils``
bound the cp312 build, langchain_core failed to import, and /chat failed
with "CustomGPT() takes no arguments" (Nunba logs, 2026-10-01, with
python-embed at sys.path[0] and again at [5]).
"""
import importlib
import os
import sys

import pytest

MODULE = 'integrations.internal_comm.task_delegation_bridge'


@pytest.fixture
def bridge_module():
    mod = importlib.import_module(MODULE)
    saved = list(sys.path)
    yield mod
    sys.path[:] = saved


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def test_root_already_on_path_is_not_moved_to_the_front(bridge_module):
    root = bridge_module._ROOT
    # Installed layout: the root (site-packages) is already on sys.path, last.
    sys.path[:] = [p for p in sys.path if _norm(p) != _norm(root)] + [root]
    front = sys.path[0]

    importlib.reload(bridge_module)

    assert sys.path[0] == front
    assert sum(_norm(p) == _norm(root) for p in sys.path) == 1


def test_same_root_spelled_differently_is_not_added_again(bridge_module):
    root = bridge_module._ROOT
    variant = root.swapcase() if os.name == 'nt' else root + os.sep
    sys.path[:] = [p for p in sys.path if _norm(p) != _norm(root)] + [variant]

    importlib.reload(bridge_module)

    assert sum(_norm(p) == _norm(root) for p in sys.path) == 1


def test_bare_source_checkout_still_gets_the_root(bridge_module):
    root = bridge_module._ROOT
    sys.path[:] = [p for p in sys.path if _norm(p) != _norm(root)]
    # The module and its imports are already loaded, so reload only re-runs
    # the path logic at the top of the module.
    importlib.reload(bridge_module)

    assert _norm(sys.path[0]) == _norm(root)
