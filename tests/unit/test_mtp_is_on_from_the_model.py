"""Multi-token prediction is switched on by the MODEL, not by an env flag.

Owner, 2026-09-24: "automatic from model". Measured the day before: the
Tiel-Coder-35B-A3B-MTP preset existed, its 22.75 GB weights were on disk and
the serving llama-server (build 10330) accepts --spec-type draft-mtp, but the
flag was emitted only when HEVOLVE_LLAMA_MTP_N >= 1. That variable was set
nowhere, so the preset whose whole point is MTP loaded as a plain MoE.

The decision now has two inputs, both measured rather than typed:
  * the GGUF says it carries an MTP head -- read_gguf_facts()['mtp'], from
    the file's own nextn_predict_layers key;
  * the SERVING binary accepts `draft-mtp` -- probed once per binary and
    cached. An unknown --spec-type makes llama-server exit at startup, so an
    MTP model on an older build (7909 and 8200 are on this box) would go from
    working to dark if the file alone decided (hartos-3a, 2026-09-23).
HEVOLVE_LLAMA_MTP_N stays as the override: 0 turns it off, N sets the draft
depth. The facts reader has its own tests (test_gguf_facts_are_read_not_typed),
so here it is the injected boundary, and so is the subprocess probe.
"""
from types import SimpleNamespace

import pytest

import integrations.service_tools.model_catalog as mc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv('HEVOLVE_LLAMA_MTP_N', raising=False)
    mc._spec_type_cache.clear()
    yield
    mc._spec_type_cache.clear()


@pytest.fixture
def binary(tmp_path):
    b = tmp_path / 'llama-server.exe'
    b.write_bytes(b'x')
    return str(b)


def _facts(monkeypatch, **facts):
    monkeypatch.setattr(mc, 'read_gguf_facts', lambda path: dict(facts))


def _help(monkeypatch, text, calls=None):
    def probe(cmd, timeout=10.0, **kw):
        if calls is not None:
            calls.append(list(cmd))
        return None if text is None else SimpleNamespace(
            returncode=0, stdout=text, stderr='')
    monkeypatch.setattr('core.subprocess_safe.run_probe', probe)


_NEW = '--spec-type none,draft-simple,draft-eagle3,draft-mtp,ngram-simple'
_OLD = '--spec-type none,draft-simple,ngram-simple'


def test_an_mtp_model_on_a_capable_server_gets_mtp(monkeypatch, binary):
    _facts(monkeypatch, architecture='qwen35moe', mtp=True, mtp_layers=1)
    _help(monkeypatch, _NEW)
    assert mc.mtp_spec_args('tiel-mtp.gguf', binary) == [
        '--spec-type', 'draft-mtp', '--spec-draft-n-max', '3']


def test_a_plain_model_gets_nothing(monkeypatch, binary):
    _facts(monkeypatch, architecture='qwen35', mtp=False)
    _help(monkeypatch, _NEW)
    assert mc.mtp_spec_args('qwen-4b.gguf', binary) == []


def test_an_old_server_is_never_handed_a_flag_it_would_die_on(monkeypatch, binary):
    _facts(monkeypatch, architecture='qwen35moe', mtp=True, mtp_layers=1)
    _help(monkeypatch, _OLD)
    assert mc.mtp_spec_args('tiel-mtp.gguf', binary) == []


@pytest.mark.parametrize('why', ['no answer', 'missing binary'])
def test_an_unknown_server_is_treated_as_not_capable(monkeypatch, tmp_path, binary, why):
    _facts(monkeypatch, architecture='qwen35moe', mtp=True, mtp_layers=1)
    _help(monkeypatch, None)
    path = binary if why == 'no answer' else str(tmp_path / 'gone.exe')
    assert mc.mtp_spec_args('tiel-mtp.gguf', path) == []


def test_an_unreadable_model_gets_nothing(monkeypatch, binary):
    _facts(monkeypatch)                      # read_gguf_facts -> {}
    _help(monkeypatch, _NEW)
    assert mc.mtp_spec_args('broken.gguf', binary) == []


def test_the_override_can_turn_it_off(monkeypatch, binary):
    _facts(monkeypatch, architecture='qwen35moe', mtp=True, mtp_layers=1)
    _help(monkeypatch, _NEW)
    monkeypatch.setenv('HEVOLVE_LLAMA_MTP_N', '0')
    assert mc.mtp_spec_args('tiel-mtp.gguf', binary) == []


def test_the_override_sets_the_draft_depth(monkeypatch, binary):
    _facts(monkeypatch, architecture='qwen35moe', mtp=True, mtp_layers=1)
    _help(monkeypatch, _NEW)
    monkeypatch.setenv('HEVOLVE_LLAMA_MTP_N', '2')
    assert mc.mtp_spec_args('tiel-mtp.gguf', binary)[-1] == '2'


def test_the_override_cannot_force_mtp_onto_a_plain_model(monkeypatch, binary):
    """The override tunes MTP; it does not invent a head the file lacks."""
    _facts(monkeypatch, architecture='qwen35', mtp=False)
    _help(monkeypatch, _NEW)
    monkeypatch.setenv('HEVOLVE_LLAMA_MTP_N', '3')
    assert mc.mtp_spec_args('qwen-4b.gguf', binary) == []


def test_the_binary_is_probed_once(monkeypatch, binary):
    _facts(monkeypatch, architecture='qwen35moe', mtp=True, mtp_layers=1)
    calls = []
    _help(monkeypatch, _NEW, calls)
    for _ in range(3):
        mc.mtp_spec_args('tiel-mtp.gguf', binary)
    assert len(calls) == 1 and calls[0][-1] == '--help'


# -- one decision, asked at every spawn ------------------------------------
#
# The spawn paths are enumerated by the QUESTION they answer (they place a
# GGUF and so already ask moe_offload_args), and the vocabulary is guarded:
# the literal 'draft-mtp' may appear as a code string in exactly one module.
# A spawn that decides MTP for itself again -- the env-only block this
# replaced -- fails here.

import ast
import os

_HARTOS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_NUNBA = os.path.join(os.path.dirname(_HARTOS), 'Nunba-HART-Companion')

_SPAWNS = [
    os.path.join(_HARTOS, 'integrations', 'service_tools', 'llamacpp_manager.py'),
    os.path.join(_HARTOS, 'integrations', 'service_tools', 'model_lifecycle.py'),
    os.path.join(_NUNBA, 'llama', 'llama_config.py'),
]


def _string_constants(path):
    tree = ast.parse(open(path, encoding='utf-8').read())
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


@pytest.mark.parametrize('path', _SPAWNS)
def test_source_guard_every_gguf_spawn_asks_mtp_spec_args(path):
    if not os.path.isfile(path):
        pytest.skip('sibling checkout absent (HARTOS-only CI)')
    src = open(path, encoding='utf-8').read()
    assert 'moe_offload_args' in src, 'no longer a GGUF spawn path?'
    assert 'mtp_spec_args(' in src, (
        f'{os.path.basename(path)} places a GGUF but does not ask '
        f'mtp_spec_args, so an MTP model would start without MTP there')


def test_source_guard_only_the_catalog_spells_the_mtp_flag():
    roots = [os.path.join(_HARTOS, d) for d in ('integrations', 'core', 'hartos')]
    roots += [os.path.join(_NUNBA, d) for d in ('llama', 'models', 'routes')]
    offenders = []
    for root in roots:
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (
                '__pycache__', 'node_modules', 'build', 'python-embed')]
            for f in files:
                if not f.endswith('.py'):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    consts = _string_constants(p)
                except (SyntaxError, UnicodeDecodeError):
                    continue
                if 'draft-mtp' in consts and not p.endswith(
                        os.path.join('service_tools', 'model_catalog.py')):
                    offenders.append(os.path.relpath(p, os.path.dirname(_HARTOS)))
    assert offenders == [], (
        f'a second place builds the MTP flag itself: {offenders}; ask '
        f'model_catalog.mtp_spec_args instead')
