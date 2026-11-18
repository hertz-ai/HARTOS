"""A voice worker whose weights are gated says it needs a key and asks once.

MEASURED 2026-11-18 (oneshot/runs/H3/gated_worker.py on HARTOS 2dbe65d24):
an Indic Parler worker start failing with Hugging Face's gated 401 answered
"indic_parler worker startup failed: 401 ...", nobody was asked for the key,
and a stored key never reached the worker: the vault keeps it as
HEVOLVE_HF_TOKEN, while huggingface_hub reads HF_TOKEN.

REVIEW of e9cebf267 (rv_b9, 2026-11-18, D:/claude_scratch/99c39a1e/rv_b9/
probes/gated_real.py): with a REAL worker child the gated 401 goes to the
child's stderr, which the parent only logged; the start failed as "died during
startup (exit=2)", so the ask was unreachable (0 asks).  The tests above fake
the start.  A startup death now carries the child's last stderr lines, and the
real-child tests at the end of this file start an actual child.
"""
import textwrap

import pytest

import hartos.ai_key_vault as kv
import integrations.service_tools.gpu_worker as gw

GATED = ("401 Client Error. Cannot access gated repo for url "
         "https://huggingface.co/ai4bharat/indic-parler-tts/resolve/main/config.json")


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.delenv('HEVOLVE_HF_TOKEN', raising=False)
    monkeypatch.delenv('HF_TOKEN', raising=False)
    pending = set()
    asks = []
    vault = type('V', (), {
        'get_tool_key': lambda self, k: __import__('os').environ.get(k, ''),
        'has_pending': lambda self, k: k in pending,
    })()
    monkeypatch.setattr(kv, 'get_ai_key_vault', lambda: vault)

    def ask(desc, agent_id=None, **kw):
        asks.append(desc)
        pending.add('HEVOLVE_HF_TOKEN')
        return 'asked'
    monkeypatch.setattr(kv, 'request_credential', ask)
    tw = gw.ToolWorker(tool_name='indic_parler', tool_module='x',
                       vram_budget='tts_indic_parler', output_subdir='t', engine='t')

    def fail(message=GATED):
        def _start():
            raise gw.WorkerError(message)
        tw._get_or_start = _start
    return tw, asks, fail


def test_a_gated_start_says_it_needs_the_key(worker):
    tw, _, fail = worker
    fail()
    err = tw.call({'text': 'vanakkam'})['error']
    assert err.startswith('needs HEVOLVE_HF_TOKEN: ai4bharat/indic-parler-tts is gated')


def test_the_owner_is_asked_once_across_two_starts(worker):
    tw, asks, fail = worker
    fail()
    tw.call({'text': 'a'})
    tw.call({'text': 'b'})
    assert len(asks) == 1


def test_with_the_key_stored_the_raw_failure_is_kept_and_nobody_is_asked(worker, monkeypatch):
    tw, asks, fail = worker
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', 'stored')
    fail()
    err = tw.call({'text': 'a'})['error']
    assert err.startswith('indic_parler worker startup failed: 401')
    assert asks == []


def test_a_failure_that_is_not_about_a_gated_repo_keeps_its_text(worker):
    tw, asks, fail = worker
    fail('CUDA out of memory')
    assert tw.call({'text': 'a'})['error'] == 'indic_parler worker startup failed: CUDA out of memory'
    assert asks == []


def test_the_worker_child_carries_the_stored_key_as_hf_token(worker, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', 'stored')
    seen = {}

    class _Stop(Exception):
        pass

    def popen(cmd, **kw):
        seen['env'] = kw.get('env') or {}
        raise _Stop()
    monkeypatch.setattr(gw.subprocess, 'Popen', popen)
    with pytest.raises(_Stop):
        gw.GPUWorker(name='t', module='x')._spawn()
    assert seen['env'].get('HF_TOKEN') == 'stored'


def test_an_hf_token_the_environment_already_has_is_left_alone(worker, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', 'stored')
    monkeypatch.setenv('HF_TOKEN', 'the-users-own')
    seen = {}

    class _Stop(Exception):
        pass

    def popen(cmd, **kw):
        seen['env'] = kw.get('env') or {}
        raise _Stop()
    monkeypatch.setattr(gw.subprocess, 'Popen', popen)
    with pytest.raises(_Stop):
        gw.GPUWorker(name='t', module='x')._spawn()
    assert seen['env'].get('HF_TOKEN') == 'the-users-own'


# ── A real worker child ──────────────────────────────────────────────
# The tool module's _load raises what huggingface_hub raises for a gated
# repo; the child prints its traceback to stderr and exits.

_GATED_LOAD = r'''
def _load():
    raise OSError(
        "401 Client Error. (Request ID: Root=1-abc)\n\nCannot access gated repo for url "
        "https://huggingface.co/ai4bharat/indic-parler-tts/resolve/main/config.json.\n"
        "Access to model ai4bharat/indic-parler-tts is restricted. You must have access "
        "to it and be authenticated to access it. Please log in.")

def _synthesize(model, req):
    return {'error': 'unreachable'}
'''

_OTHER_LOAD = '''
def _load():
    raise RuntimeError("CUDA out of memory")

def _synthesize(model, req):
    return {'error': 'unreachable'}
'''


@pytest.fixture
def real_child(tmp_path, monkeypatch):
    """A tool module on the path the child inherits, and a data folder of
    its own for the child."""
    for k in ('HARTOS_DATA_DIR', 'HOME', 'USERPROFILE'):
        monkeypatch.setenv(k, str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))

    def tool(name, body):
        (tmp_path / f'{name}.py').write_text(textwrap.dedent(body), encoding='utf-8')
        import sys
        return gw.ToolWorker(tool_name='indic_parler', tool_module=name,
                             vram_budget='no_such_budget', output_subdir='t',
                             engine='t', startup_timeout=60,
                             python_exe=sys.executable)
    return tool


def test_a_real_child_dying_on_a_gated_repo_says_it_needs_the_key(worker, real_child):
    _, asks, _ = worker
    tw = real_child('gated_weights_tool_a', _GATED_LOAD)
    err = tw.call({'text': 'vanakkam'})['error']
    assert err.startswith('needs HEVOLVE_HF_TOKEN: ai4bharat/indic-parler-tts is gated')
    assert len(asks) == 1


def test_a_real_child_dying_for_another_reason_asks_nobody(worker, real_child):
    _, asks, _ = worker
    tw = real_child('gated_weights_tool_b', _OTHER_LOAD)
    err = tw.call({'text': 'vanakkam'})['error']
    assert err.startswith('indic_parler worker startup failed:')
    assert asks == []


def test_a_respawn_does_not_carry_the_last_childs_words(real_child):
    """The tail is the CURRENT child's: words a dead earlier child left are
    not read as this child's reason."""
    import sys
    real_child('gated_weights_tool_c', _OTHER_LOAD)
    w = gw.GPUWorker(name='indic_parler', module=gw.ToolWorker._DISPATCHER,
                     startup_timeout=60, python_exe=sys.executable,
                     args=['gated_weights_tool_c'])
    stale = 'Cannot access gated repo for url https://huggingface.co/a/b'
    w._stderr_tail.append(stale)
    with pytest.raises(gw.WorkerCrash) as crash:
        w.start()
    # The exact stale line: the child's own startup line names its temp
    # folder, whose path carries this file's name (and so the word 'gated').
    assert stale not in crash.value.stderr_tail
    assert gw._gated_repo_in(crash.value.stderr_tail) == ''
    assert 'CUDA out of memory' in crash.value.stderr_tail
