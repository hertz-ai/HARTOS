"""Every model record carries every field the lifecycle reads from it.

Owner, 2026-11-24: "make the model record all fiels required".

``ModelLifecycleManager.request_swap`` skips language models when it picks a
model to evict, and it asked each record for ``model_type``.  ``ModelState``
never had that field, so as soon as the scan reached a GPU record that was
not named ``llm-*`` the swap raised AttributeError, and no other model could
be made room for.  Found 2026-11-24 through
Nunba tests/test_model_resilience.py::TestThreadSafety::test_concurrent_swap_requests
(4 of 4 workers raised).

``model_type`` is now a field every record must be given when it is made,
filled at each place a record is made from what the model is: the catalog
row's type, or the runtime tool's own declaration when it has no row.
"""
import ast
import dataclasses
import inspect
import threading
import time
from unittest.mock import MagicMock

import pytest

from integrations.service_tools import model_lifecycle
from integrations.service_tools.model_lifecycle import (
    ModelDevice, ModelLifecycleManager, ModelPriority, ModelState,
)


def test_a_record_cannot_be_made_without_saying_what_kind_of_model_it_is():
    with pytest.raises(TypeError):
        ModelState(name='whisper')
    assert ModelState(name='whisper', model_type='stt').model_type == 'stt'


def test_every_field_the_lifecycle_reads_from_a_record_is_declared():
    """A static read of model_lifecycle.py: every ``<record>.<attr>`` it reads
    in a comprehension or loop over the registry is a ModelState field."""
    fields = {f.name for f in dataclasses.fields(ModelState)}
    methods = {n for n, _ in inspect.getmembers(ModelState, inspect.isfunction)}
    tree = ast.parse(inspect.getsource(model_lifecycle))
    record_names = {'s', 'state', 'ms', 'model_state', 'victim', 'candidate'}
    missing = sorted({
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id in record_names
        and n.attr not in fields and n.attr not in methods
    })
    assert missing == []


def test_a_swap_can_pick_a_model_that_is_not_a_language_model():
    mlm = ModelLifecycleManager()
    mlm._do_unload = MagicMock()
    for i in range(4):
        mlm._models[f'voice{i}'] = ModelState(
            name=f'voice{i}', model_type='tts', device=ModelDevice.GPU,
            priority=ModelPriority.EVICTABLE,
            last_access_time=time.time() - i * 100)
    errors = []

    def worker(tid):
        try:
            mlm.request_swap(f'needed_{tid}')
        except Exception as e:  # the defect: AttributeError on model_type
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


@pytest.mark.parametrize('tool, kind', [
    ('whisper', 'stt'),            # no catalog row: the tool declares it
    ('tts_audio_suite', 'tts'),    # no catalog row: the tool declares it
    ('minicpm', 'vlm'),            # the catalog row 'vlm-minicpm-v2' says it
    ('wan2gp', 'video_gen'),       # the catalog row 'video_gen-wan2gp' says it
])
def test_a_started_tool_is_recorded_as_its_kind(tool, kind):
    mlm = ModelLifecycleManager()
    mlm._on_tool_started(tool, device='gpu')
    assert mlm._models[tool].model_type == kind


def test_the_language_model_record_says_llm():
    """The record made when a live llama-server is adopted."""
    mlm = ModelLifecycleManager()
    mlm._record_llm_alive()
    assert mlm._models['llm'].model_type == 'llm'
