"""JSON trace storage must not mix agents sharing its directory."""

import json

from integrations.agent_lightning.config import AGENT_LIGHTNING_CONFIG
from integrations.agent_lightning.store import LightningStore


def test_json_store_filters_spans_by_agent_id(tmp_path, monkeypatch):
    monkeypatch.setitem(AGENT_LIGHTNING_CONFIG, 'traces_path', str(tmp_path))
    (tmp_path / 'create.json').write_text(json.dumps({
        'span_id': 'create', 'agent_id': 'create_recipe_assistant_one',
        'span_type': 'generate_reply', 'status': 'success', 'start_time': 1,
        'events': [],
    }))
    (tmp_path / 'reuse.json').write_text(json.dumps({
        'span_id': 'reuse', 'agent_id': 'reuse_recipe_assistant_two',
        'span_type': 'generate_reply', 'status': 'success', 'start_time': 2,
        'events': [],
    }))

    spans = LightningStore('create_recipe_assistant_one', backend='json').list_spans()

    assert [span['span_id'] for span in spans] == ['create']
