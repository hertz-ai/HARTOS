"""Python programs kept in string constants compile.

A program inside a string literal is invisible to py_compile, ruff and every
import: nothing parses it until the child that runs it, or the repo it is
pasted into.  Each is compiled here with SyntaxWarning as an error (an invalid
escape is a warning today and an error in a later Python).

Found by the question "which constants hold Python that a child runs or a file
receives" (python -c arguments, written-out sources, code handed to the coding
agent to embed), in HARTOS:
  integrations/agent_engine/hevolveai_supervisor.py
      _TORCH_PROBE_SNIPPET   python -c, the torch probe
      _ARMOR_INSTALL_SNIPPET prefix of the HevolveAI boot program (python -c)
  integrations/agent_engine/hive_sdk_spec.py
      the *_SNIPPET blocks the coding agent embeds in every repo it writes
Nunba's equivalents are in its tests/test_generated_source_compiles.py.
"""
import warnings

import pytest

from integrations.agent_engine import hevolveai_supervisor as sup
from integrations.agent_engine import hive_sdk_spec as sdk

SOURCES = {
    'hevolveai_supervisor._TORCH_PROBE_SNIPPET': sup._TORCH_PROBE_SNIPPET,
    'hevolveai_supervisor._ARMOR_INSTALL_SNIPPET': sup._ARMOR_INSTALL_SNIPPET,
    'hive_sdk_spec.MASTER_KEY_VERIFICATION_SNIPPET':
        sdk.MASTER_KEY_VERIFICATION_SNIPPET,
    'hive_sdk_spec.GUARDRAIL_HASH_SNIPPET': sdk.GUARDRAIL_HASH_SNIPPET,
    'hive_sdk_spec.WORLD_MODEL_BRIDGE_SNIPPET': sdk.WORLD_MODEL_BRIDGE_SNIPPET,
    'hive_sdk_spec.NODE_IDENTITY_SNIPPET': sdk.NODE_IDENTITY_SNIPPET,
    'hive_sdk_spec.TRUEFLOW_CODE_QUALITY_SNIPPET':
        sdk.TRUEFLOW_CODE_QUALITY_SNIPPET,
}


def _compile(name, source):
    with warnings.catch_warnings():
        warnings.simplefilter('error', SyntaxWarning)
        compile(source, name, 'exec')


@pytest.mark.parametrize('name', sorted(SOURCES))
def test_the_program_compiles_with_no_syntax_warning(name):
    _compile(name, SOURCES[name])


def test_the_hevolveai_boot_program_compiles():
    """The program the supervisor actually spawns: the armor prefix plus the
    uvicorn boot, as _build_cmd builds it with no repo checkout."""
    class _Bare:
        python_exe = 'python'
        repo_root = None
        repo_python = None

    cmd = sup._Supervisor._build_cmd(_Bare())
    assert cmd[1] == '-c'
    _compile('hevolveai boot', cmd[2])


def test_the_embedded_snippets_compile_together():
    """The coding agent pastes the snippets into one module, after an
    `import os` (they use os.environ without importing it)."""
    _compile('hive sdk embed', 'import os\n' + '\n'.join(
        SOURCES[n] for n in sorted(SOURCES) if n.startswith('hive_sdk_spec.')))
