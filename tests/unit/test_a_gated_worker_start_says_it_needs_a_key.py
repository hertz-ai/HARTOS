"""A voice worker whose weights are gated says it needs a key and asks once.

MEASURED 2026-11-18 (oneshot/runs/H3/gated_worker.py on HARTOS 2dbe65d24):
an Indic Parler worker start failing with Hugging Face's gated 401 answered
"indic_parler worker startup failed: 401 ...", nobody was asked for the key,
and a stored key never reached the worker: the vault keeps it as
HEVOLVE_HF_TOKEN, while huggingface_hub reads HF_TOKEN.
"""
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
