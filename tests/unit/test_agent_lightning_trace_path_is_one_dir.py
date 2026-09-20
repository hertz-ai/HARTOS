"""A span the tracer writes must be a span the store can read.

Agent Lightning has exactly two components that touch the traces directory:
`LightningTracer._save_span` (the only writer) and `LightningStore` (the only
reader, constructed in production solely by
`agent_baseline_service.AgentBaselineService._collect_lightning_metrics`).
They resolved the SAME config key two different ways:

  tracer.py  rewrote a './'-prefixed or empty value to
             `core.platform_paths.get_agent_data_dir()/lightning_traces`,
             because an installed build's CWD is C:\\Program Files and is
             not writable.
  store.py   took `AGENT_LIGHTNING_CONFIG['traces_path']` literally, so it
             used the CWD-relative default.

`AGENT_LIGHTNING_TRACES_PATH` is set nowhere in either repo, so the default
is the shipped state, and Nunba's `routes/hartos_backend_adapter.py` turns
Agent Lightning ON by default -- meaning the reader looked in a directory the
writer never wrote to, and Phase 3 baseline aggregation always saw zero spans
while reporting success.

These tests pin the invariant behaviourally: write through the real tracer,
read through the real store, assert the span comes back.  They must not assert
any particular directory -- only that the two agree -- so that changing where
traces live stays a one-line change in `config.get_traces_path`.

Measured by the Phase 3 review agent on 2026-09-20 (finding 1): with the
shipped default, `store.list_spans()` returned 0 and
`_collect_lightning_metrics` returned {} while the tracer had written to
~/Documents/Nunba/data/agent_data/lightning_traces.
"""
import os

import pytest

from integrations.agent_lightning import config as al_config
from integrations.agent_lightning.store import LightningStore
from integrations.agent_lightning.tracer import LightningTracer


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point the platform data dir at tmp_path and put CWD somewhere else.

    The two must differ, or a CWD-relative bug would accidentally pass.
    """
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    cwd = tmp_path / 'elsewhere'
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    import core.platform_paths
    monkeypatch.setattr(
        core.platform_paths, 'get_agent_data_dir', lambda: str(data_dir))
    # The default is the CWD-relative value; that is the shipped state.
    monkeypatch.setitem(
        al_config.AGENT_LIGHTNING_CONFIG,
        'traces_path', './agent_data/lightning_traces')
    monkeypatch.setitem(
        al_config.AGENT_LIGHTNING_CONFIG, 'store_backend', 'json')
    return data_dir, cwd


def _write_one_span(agent_id):
    """Drive the real tracer end to end; returns the span_id it persisted."""
    tracer = LightningTracer(agent_id)
    span_id = tracer.start_span('task_execution', {'probe': True})
    tracer.end_span(span_id, 'success', {'ok': True})
    return span_id


def test_the_store_reads_back_a_span_the_tracer_wrote(isolated_data_dir):
    span_id = _write_one_span('probe_agent')

    store = LightningStore('probe_agent')
    found = [s for s in store.list_spans(limit=50)
             if s.get('span_id') == span_id]

    assert found, (
        'the store found no span the tracer had just written; the writer and '
        'the reader are resolving traces_path differently'
    )


def test_writer_and_reader_agree_on_the_directory(isolated_data_dir):
    """The store's directory is where the tracer's file actually landed."""
    span_id = _write_one_span('probe_agent_2')
    store = LightningStore('probe_agent_2')

    assert os.path.isfile(
        os.path.join(store.storage_path, f'{span_id}.json')
    ), f'span file is not under the store path {store.storage_path}'


def test_an_explicit_absolute_path_is_still_honoured(tmp_path, monkeypatch):
    """The override must keep working -- the fix is for './' defaults only."""
    explicit = tmp_path / 'operator_chosen'
    monkeypatch.setitem(
        al_config.AGENT_LIGHTNING_CONFIG, 'traces_path', str(explicit))
    monkeypatch.setitem(
        al_config.AGENT_LIGHTNING_CONFIG, 'store_backend', 'json')

    span_id = _write_one_span('probe_agent_3')
    store = LightningStore('probe_agent_3')

    assert os.path.abspath(store.storage_path) == os.path.abspath(str(explicit))
    assert os.path.isfile(os.path.join(str(explicit), f'{span_id}.json'))


def test_the_resolver_never_returns_a_relative_path(monkeypatch):
    """A relative path is what made this CWD-dependent in the first place."""
    for value in ('./agent_data/lightning_traces', '', 'agent_data/x', None):
        monkeypatch.setitem(
            al_config.AGENT_LIGHTNING_CONFIG, 'traces_path', value)
        resolved = al_config.get_traces_path()
        assert os.path.isabs(resolved), f'{value!r} resolved to {resolved!r}'

