"""An agent written in a language other than English can be read back.

Live, 13 Nov 2026 09:06 IST, Nunba build 75 on the Windows desktop: a speech agent
whose steps speak Tamil to the child ('அர்ஜுன், யாரு இது? அம்மா? அப்பா?') was saved,
and from then on every /chat for it failed with UnicodeDecodeError in
create_recipe.get_prompt_config_json: the agent's files are written as UTF-8, but
read back with open() and no encoding, which on Windows means cp1252.  Nunba showed
the person "Your local AI is busy" for 20 minutes.

Every text read of an agent file (config, recipe, action) names its encoding.  The
platform default is pinned to cp1252 here, so these tests mean the same on every OS.
"""
from __future__ import annotations

import ast
import builtins
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

TAMIL = 'அர்ஜுன், யாரு இது? அம்மா? அப்பா?'
CONFIG = {
    'name': 'Arjun Tamil Speech Practice', 'goal': 'Help Arjun name his family members',
    'personas': [{'name': 'Child', 'description': 'Arjun'}],
    'flows': [{'flow_name': 'Family member association', 'persona': 'Child',
               'actions': [f"send_message_to_user to Arjun: '{TAMIL}'"],
               'sub_goal': 'Arjun names his mother'}],
}
FILES = ('hartos/create_recipe.py', 'hartos/reuse_recipe.py', 'hart_intelligence_entry.py')


@pytest.fixture
def agent_file(tmp_path):
    path = tmp_path / '4242.json'
    # as the create loop saves it: UTF-8, the Tamil written as itself
    path.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding='utf-8')
    return str(path)


@pytest.fixture
def windows_default(monkeypatch):
    """open() with no encoding reads in the platform's default, cp1252 on the desktop."""
    real = builtins.open

    def platform_default(file, mode='r', *args, **kwargs):
        if 'b' not in mode and not args and kwargs.get('encoding') is None:
            kwargs['encoding'] = 'cp1252'
        return real(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', platform_default)


def test_the_create_path_reads_a_tamil_agent(agent_file, windows_default, monkeypatch):
    from hartos import create_recipe as cr
    monkeypatch.setattr(cr.helper_fun, 'safe_prompt_path', lambda *a, **k: agent_file)
    config = cr.get_prompt_config_json(4242)
    assert TAMIL in config['flows'][0]['actions'][0]


def test_the_reuse_path_reads_a_tamil_agent(agent_file, windows_default, monkeypatch):
    from hartos import reuse_recipe as rr
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *a, **k: agent_file)
    assert rr._reuse_flow_persona(4242, 0) == 'Child'


def _text_reads_without_encoding(rel):
    """Builtin open() calls in read text mode that name no encoding: (line, source)."""
    src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
    found = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'open'):
            continue
        mode = node.args[1] if len(node.args) > 1 else next(
            (k.value for k in node.keywords if k.arg == 'mode'), None)
        if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
            continue  # a computed mode: not judged here
        text = 'r' if mode is None else mode.value
        if 'b' in text or not set(text) & {'r', '+'} or set(text) & {'w', 'a', 'x'}:
            continue
        if any(k.arg == 'encoding' for k in node.keywords) or len(node.args) > 3:
            continue
        found.append((node.lineno, ast.get_source_segment(src, node)))
    return found


@pytest.mark.parametrize('rel', FILES)
def test_every_agent_file_read_names_its_encoding(rel):
    # hart_intelligence_entry's chat route reads the same agent file on every turn
    # (prompts/<id>.json); importing it here would start the whole server
    assert _text_reads_without_encoding(rel) == []
