"""Parallel-path fix (data integrity): create_recipe.py wrote agent/recipe
configs with a non-atomic ``with open(path, 'w'): json.dump(...)`` — a crash or
power-loss mid-write left a truncated/corrupt config that ``get_prompt_config_json``
then KeyError-crashed on (the exact "flows" corruption class). The config-save
sites now route through the canonical ``core.file_cache.atomic_json_write``
(temp file -> fsync -> os.replace).
"""
import json
from pathlib import Path

from core.file_cache import atomic_json_write


def test_atomic_write_produces_valid_readable_json(tmp_path):
    p = tmp_path / 'cfg.json'
    data = {'flows': [{'x': 1}], 'name': 'héllo', 'n': 42}
    atomic_json_write(str(p), data, indent=4)
    assert json.loads(p.read_text(encoding='utf-8')) == data


def test_create_recipe_config_saves_are_atomic():
    src = (Path(__file__).resolve().parents[2] / 'hartos/create_recipe.py').read_text(encoding='utf-8')
    assert 'atomic_json_write(file_path, data' in src, \
        "recipe config saves must use the canonical atomic write"
    assert "with open(file_path, 'w') as f:" not in src, \
        "the raw non-atomic config write must be gone (crash = corrupt config)"
