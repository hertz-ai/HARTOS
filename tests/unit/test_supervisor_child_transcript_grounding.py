"""The hevolveai child grounds the spoken WORD with no environment knob.

HISTORY: hevolveai's audio-ingest branch used to call encode_text on the
transcript only when HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING was set (its C265), and
this supervisor exported that flag. hevolveai a951136 (C265b) graduated it: the
child now grounds any delivered transcript unconditionally and reads no such
variable. The export became a dead knob whose comment promised an operator that
=0 turns grounding off, which no longer holds (review 2026-09-22, F4).

What this file now pins: the supervisor does NOT hand the child that variable,
so nobody reintroduces a switch the child ignores. The two cwd tests below are
unrelated to the flag and unchanged.
"""
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine.hevolveai_supervisor import _Supervisor  # noqa: E402


def _fake_self():
    return types.SimpleNamespace(
        api_url='http://127.0.0.1:8000', port=8000, pythonpath='')


def test_the_graduated_grounding_flag_is_not_exported():
    """The child no longer reads HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING (hevolveai
    a951136). Exporting it would advertise an off switch that does nothing."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING', None)
        env = _Supervisor._build_env(_fake_self())
    assert 'HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING' not in env, (
        'the supervisor re-exported a flag the child ignores')


def test_installed_child_uses_canonical_writable_data_cwd():
    """The bundled Python child must not inherit Program Files as its cwd."""
    sup = _Supervisor.__new__(_Supervisor)
    sup.repo_root, sup.repo_python = None, None
    with tempfile.TemporaryDirectory() as tmp, \
            patch('core.platform_paths.get_data_dir', return_value=tmp):
        assert sup._child_working_dir() == str(Path(tmp).resolve())


def test_repo_child_keeps_checkout_cwd():
    """The installed-mode fix must preserve run_server.py repo semantics."""
    sup = _Supervisor.__new__(_Supervisor)
    sup.repo_root = Path('C:/work/hevolveai')
    sup.repo_python = 'C:/Python310/python.exe'
    with patch('core.platform_paths.get_data_dir') as get_data_dir:
        assert sup._child_working_dir() == str(sup.repo_root)
    get_data_dir.assert_not_called()
