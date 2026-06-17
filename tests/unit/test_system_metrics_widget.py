"""The desktop system widget must read the metrics API's ACTUAL shape.

Live floor boot (2026-06-16, WSL/QEMU, software-GL) showed Memory stuck at 0%:
the widget (hartSessionUI.js) read a flat `memory_percent`, but
/api/shell/system/metrics returns NESTED `ram.percent`. This pins the API shape
(behavioural) + the JS read (a labelled source-guard alongside it).

    python -m pytest tests/unit/test_system_metrics_widget.py --noconftest -p no:capture -q
"""
from pathlib import Path

from integrations.agent_engine.liquid_ui_service import LiquidUIService


def test_metrics_endpoint_returns_nested_ram_percent():
    # Behavioural: the REAL route + psutil; the widget's memory bar reads this.
    client = LiquidUIService(a2ui_enabled=True)._create_flask_app().test_client()
    r = client.get('/api/shell/system/metrics')
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data.get('ram'), dict), "metrics must expose ram{}"
    assert 'percent' in data['ram'], \
        "metrics ram must carry .percent — the key the memory bar reads"


def test_source_guard_widget_reads_ram_percent():
    # Labelled source-guard (paired with the behavioural test above): the system
    # widget must read m.ram.percent, not the flat memory_percent floor-boot
    # proved is always 0.
    js = Path('integrations/agent_engine/static/hartSessionUI.js').read_text(
        encoding='utf-8')
    assert 'm.ram.percent' in js.replace(' ', ''), \
        "system widget must read m.ram.percent (the metrics API shape)"
