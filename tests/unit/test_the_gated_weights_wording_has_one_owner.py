"""How a gated voice is named in the key ask, and what is said when a stored
token is refused, are decided once: hartos.ai_key_vault.gated_weights_answer.

Before: the voice worker built "the {tool_name} voice" itself and Nunba's
weight download built "the {backend} voice" from its own engine names, so the
same engine was asked for as "the f5_tts voice" by one and "the f5 voice" by
the other; the refused-token answer existed only in Nunba.
"""
import json
import os
import re

import pytest

import hartos.ai_key_vault as kv
from integrations.service_tools import gpu_worker as gw

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
REPO = 'ai4bharat/indic-parler-tts'
PAGE = f'https://huggingface.co/{REPO}'
NEEDS = f'needs HEVOLVE_HF_TOKEN: {REPO} is gated; accept its terms at {PAGE}'


@pytest.fixture
def asks(monkeypatch):
    monkeypatch.delenv('HEVOLVE_HF_TOKEN', raising=False)
    monkeypatch.delenv('HF_TOKEN', raising=False)
    pending = set()
    seen = []
    vault = type('V', (), {
        'get_tool_key': lambda self, k: os.environ.get(k, ''),
        'has_pending': lambda self, k: k in pending,
    })()
    monkeypatch.setattr(kv, 'get_ai_key_vault', lambda: vault)

    def ask(desc, agent_id=None, **kw):
        seen.append(json.loads(desc))
        pending.add('HEVOLVE_HF_TOKEN')
        return 'asked'
    monkeypatch.setattr(kv, 'request_credential', ask)
    return seen


def test_a_voice_is_named_by_its_registry_id(asks):
    assert kv.gated_weights_answer(REPO, voice='f5_tts') == NEEDS
    assert asks[0]['used_by'] == 'the f5_tts voice'
    assert asks[0]['description'] == f'Needed for the f5_tts voice: {NEEDS}'


def test_a_caller_that_is_no_voice_names_itself(asks):
    """The signature callers already use: (repo, used_by)."""
    assert kv.gated_weights_answer(REPO, 'voice setup') == NEEDS
    assert asks[0]['used_by'] == 'voice setup'
    assert asks[0]['key_name'] == 'HEVOLVE_HF_TOKEN'
    assert asks[0]['label'] == 'Hugging Face access token'
    assert asks[0]['resource_type'] == 'api_key'


def test_a_caller_that_names_nobody_is_named_by_the_repo(asks):
    """Never "Needed for : needs ..." (review rv_66ba of 6ddc8dd82)."""
    assert kv.gated_weights_answer(REPO) == NEEDS
    assert asks[0]['used_by'] == f'the {REPO} weights'
    assert asks[0]['description'] == f'Needed for the {REPO} weights: {NEEDS}'


def test_a_stored_token_the_repo_refuses_asks_nobody(asks, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', 'stored')
    assert kv.gated_weights_answer(REPO, voice='indic_parler') == (
        f'{REPO} refused the stored HEVOLVE_HF_TOKEN; accept its terms at '
        f'{PAGE} with that account')
    assert asks == []


def test_the_voice_worker_asks_in_the_owners_words(asks):
    tw = gw.ToolWorker(tool_name='chatterbox_ml', tool_module='x',
                       vram_budget='tts_chatterbox_ml', output_subdir='t',
                       engine='t')

    def _start():
        raise gw.WorkerError(
            f'401 Client Error. Cannot access gated repo for url {PAGE}/'
            f'resolve/main/config.json.\nAccess to model {REPO} is restricted.')
    tw._get_or_start = _start
    assert tw.call({'text': 'a'})['error'] == NEEDS
    assert [a['used_by'] for a in asks] == ['the chatterbox_ml voice']


def test_the_voice_worker_says_a_refused_stored_token_plainly(asks, monkeypatch):
    """Was "chatterbox_ml worker startup failed: 401 Client Error ..."."""
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', 'stored')
    tw = gw.ToolWorker(tool_name='chatterbox_ml', tool_module='x',
                       vram_budget='tts_chatterbox_ml', output_subdir='t',
                       engine='t')

    def _start():
        raise gw.WorkerError(
            f'401 Client Error. Cannot access gated repo for url {PAGE}/'
            f'resolve/main/config.json.\nAccess to model {REPO} is restricted.')
    tw._get_or_start = _start
    assert tw.call({'text': 'a'})['error'] == (
        f'{REPO} refused the stored HEVOLVE_HF_TOKEN; accept its terms at '
        f'{PAGE} with that account')
    assert asks == []


def test_a_start_that_failed_for_another_reason_keeps_its_text(asks, monkeypatch):
    monkeypatch.setenv('HEVOLVE_HF_TOKEN', 'stored')
    tw = gw.ToolWorker(tool_name='chatterbox_ml', tool_module='x',
                       vram_budget='tts_chatterbox_ml', output_subdir='t',
                       engine='t')

    def _start():
        raise gw.WorkerError('CUDA out of memory')
    tw._get_or_start = _start
    assert tw.call({'text': 'a'})['error'] == (
        'chatterbox_ml worker startup failed: CUDA out of memory')
    assert asks == []


def test_nobody_else_words_the_voice():
    """Shape: an f-string that builds "the {...} voice" outside the owner."""
    owner = 'hartos/ai_key_vault.py'
    shape = re.compile(r'the \{[^}]+\} voice')
    src = open(os.path.join(ROOT, owner), encoding='utf-8').read()
    assert shape.search(src)
    for rel in ('integrations/service_tools/gpu_worker.py',
                'integrations/channels/media/tts_router.py'):
        src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
        assert not shape.search(src), rel
