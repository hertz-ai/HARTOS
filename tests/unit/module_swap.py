"""swap_modules: put sys.modules entries in place, and restore ONLY those.

THE one implementation for a test that stands a fake in for a module, or
hides one (value None, so the import raises ImportError and the fallback
path runs).  tests/conftest.py hidden_modules(*names) is
swap_modules({name: None}) and delegates here.

Why not patch.dict('sys.modules', {...}): its exit CLEARS sys.modules and
restores the snapshot it took on entry, so EVERY module first imported
inside the block is evicted -- one such block around a dispatch call
evicted ~3200 modules.  Two failures measured from that one cause:

  * C extensions.  The extension stays initialised in the process while
    its Python layer re-executes, so the next `import torch` died with
    "function '_has_torch_function' already has a docstring" (or a
    tensor_numpy.cpp INTERNAL ASSERT) in whatever unrelated test touched
    torch next -- the test_distributed_bridge torch failures on CI.

  * Stale package attributes.  The evicted module's object stays on its
    package, so a later `from integrations.vlm import qwen3vl_backend`
    returns the stale object, the test patches THAT, and the code under
    test -- whose call-time import loads a fresh copy -- runs unpatched.
    Measured 2026-09-27: after tests/unit/test_vlm_local_loop.py,
    test_vlm_loop_feeds_back_action_output sent real requests to
    127.0.0.1:8080, and a test patching a stale subprocess_safe passed
    with the code it guarded disabled.

tests/unit/test_one_sys_modules_swap.py refuses a new patch.dict on
sys.modules; the existing users are frozen there until migrated.
"""
import contextlib
import sys

_MISSING = object()


@contextlib.contextmanager
def swap_modules(replacements):
    """``replacements``: {module name: module object, or None to make the
    import fail}.  Only those keys are changed and only those restored."""
    saved = {k: sys.modules.get(k, _MISSING) for k in replacements}
    sys.modules.update(replacements)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
