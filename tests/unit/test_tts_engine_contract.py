"""Every TTS engine row honours the engine interface (tts_router.engine_contract_errors).

The nearest Python has to a Java interface that fails the build: each code-shipped
ENGINE_REGISTRY row is checked against its real tool module -- the synthesis function exists
and takes (text, language=, voice=, output_path=), and the engine's live state can be read (a
ToolWorker with is_alive/stop, or is_loaded() for an in-process engine).  A row or a module that
breaks it fails here, not at runtime as an error answer.  Whether the router runs a row is a
separate matter: omnivoice and neutts_air honour the interface and still have no router branch
until the registry dispatch lands after the demo (test_tts_router_realtime.py pins that).
"""
import os
import sys
import types
from dataclasses import replace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.unit.module_swap import swap_modules  # noqa: E402

from integrations.channels.media import tts_router as tr  # noqa: E402

KNOWN_GAPS = {
    # Piper is spoken in-process by Nunba (tts/piper_tts.py); HARTOS has no tool module for it,
    # so the router puts it on its ladder and cannot run it.  strict: fixing it turns this red
    # until the entry is removed.
    'piper': 'no HARTOS tool module: Nunba speaks Piper itself, the router cannot',
}


@pytest.mark.parametrize('engine_id', sorted(tr._SEED_SPECS))
def test_every_engine_row_honours_the_interface(engine_id):
    if engine_id in KNOWN_GAPS:
        pytest.xfail(KNOWN_GAPS[engine_id])
    assert tr.engine_contract_errors(tr._SEED_SPECS[engine_id]) == []


def test_the_known_gaps_are_still_gaps():
    """A gap that was fixed must leave KNOWN_GAPS (strict, as xfail(strict=True) would be)."""
    for engine_id in KNOWN_GAPS:
        assert tr.engine_contract_errors(tr._SEED_SPECS[engine_id]), \
            f'{engine_id} honours the interface now: remove it from KNOWN_GAPS'


# ── the checker itself catches each kind of break ────────────────────────────

class _Worker:
    def is_alive(self):
        return False

    def stop(self):
        pass


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _spec(**kw):
    fields = {'engine_id': 'fake_engine', 'tool_module': 'fake_tts_tool', **kw}
    return replace(tr._SEED_SPECS['f5_tts'], **fields)


def _good(text, language='en', voice=None, output_path=None):
    return '{}'


def test_a_conforming_module_passes():
    with swap_modules({'fake_tts_tool': _module('fake_tts_tool', synth=_good, _tool=_Worker())}):
        assert tr.engine_contract_errors(_spec(tool_function='synth', tool_worker_attr='_tool')) == []


def test_a_missing_function_is_caught():
    with swap_modules({'fake_tts_tool': _module('fake_tts_tool', _tool=_Worker())}):
        errors = tr.engine_contract_errors(_spec(tool_function='synth', tool_worker_attr='_tool'))
    assert errors and 'no function' in errors[0]


def test_a_function_that_cannot_take_the_call_is_caught():
    def positional_only(text, voice_name, out):  # pocket_tts's own shape: not the interface
        return '{}'
    with swap_modules({'fake_tts_tool': _module('fake_tts_tool', synth=positional_only,
                                                           _tool=_Worker())}):
        errors = tr.engine_contract_errors(_spec(tool_function='synth', tool_worker_attr='_tool'))
    assert errors and 'cannot be called' in errors[0]


def test_a_worker_that_is_not_a_toolworker_is_caught():
    with swap_modules({'fake_tts_tool': _module('fake_tts_tool', synth=_good, _tool=object())}):
        errors = tr.engine_contract_errors(_spec(tool_function='synth', tool_worker_attr='_tool'))
    assert errors and 'not a ToolWorker' in errors[0]


def test_an_in_process_engine_without_is_loaded_is_caught():
    with swap_modules({'fake_tts_tool': _module('fake_tts_tool', synth=_good)}):
        errors = tr.engine_contract_errors(_spec(tool_function='synth', tool_worker_attr=None))
    assert errors and 'is_loaded' in errors[0]


def test_a_module_that_does_not_import_is_caught():
    errors = tr.engine_contract_errors(_spec(tool_module='no_such_tts_tool_module',
                                             tool_function='synth'))
    assert errors and 'does not import' in errors[0]
