"""The hevolveai child is told to ground the spoken WORD, not just the sound.

WHY: hevolveai's audio-ingest branch encodes the waveform and calls encode_text
on the transcript only when HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING is set (its
C265). That call is what stashes the per-token sequence its concept grounder
names concepts from; without it every concept is named "token_0" and nothing
consolidates or recalls (hevolveai Master 11.331).

HARTOS already delivers the transcript (world_model_bridge sets `text` on the
/v1/sensor/ingest body), and the 153-entry child environment did not carry the
flag, so the words died at the child's door. This is the env-contract half; the
consumer half lives in the hevolveai repo.

Behavioural, in the same shape as test_supervisor_child_llm_url.py: call the
real _build_env with a fake self and assert the env it returns. Covers: the
default is ON, an operator override is preserved, and the flag rides the same
dict the child is actually spawned with.
"""
import os
import sys
import types
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from integrations.agent_engine.hevolveai_supervisor import _Supervisor  # noqa: E402


def _fake_self():
    return types.SimpleNamespace(
        api_url='http://127.0.0.1:8000', port=8000, pythonpath='')


def test_transcript_grounding_defaults_on():
    """Absent from the parent env -> the child still gets it ON.

    This is the whole point: the flag was absent from production's child
    environment, so spoken words never reached grounding.
    """
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING', None)
        env = _Supervisor._build_env(_fake_self())
    assert env['HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING'] == '1'


def test_operator_can_still_turn_it_off():
    """setdefault, not assignment: an explicit 0 survives.

    Same contract as HEVOLVE_DEVICE, so an operator can disable transcript
    grounding without editing HARTOS.
    """
    with patch.dict(os.environ,
                    {'HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING': '0'},
                    clear=False):
        env = _Supervisor._build_env(_fake_self())
    assert env['HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING'] == '0'


def test_flag_rides_the_env_the_child_is_spawned_with():
    """The value must be on the dict handed to Popen, not merely computed.

    _build_popen is the single site that assembles the spawn kwargs, so
    asserting there proves the flag reaches the actual child.
    """
    sup = _Supervisor.__new__(_Supervisor)
    sup.api_url, sup.port, sup.pythonpath = 'http://127.0.0.1:8000', 8000, ''
    sup.repo_root, sup.repo_python = None, None
    sup.python_exe = sys.executable
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING', None)
        _cmd, kw = _Supervisor._build_popen(sup)
    assert kw['env']['HEVOLVE_AUDIO_TRANSCRIPT_GROUNDING'] == '1'
