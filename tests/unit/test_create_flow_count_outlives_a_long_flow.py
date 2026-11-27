"""The CREATE's flow count must outlive a flow that runs longer than its cache TTL.

LIVE (2026-11-25, installed build #2, speech agent 95568353503 built as 10202):
flow 1 ran 06:40-09:26 IST.  At 10:06:17 the flow-end step called
get_total_flows('10202_95568353503') and raised KeyError, the person got
"I couldn't finish that: '10202_95568353503'", and the CREATE could not move to
flow 2.  total_persona_actions is a 2 h TTLCache with no loader: it is written
once when the CREATE initialises and read only at a flow boundary, so
touch-on-read never refreshed it while a long flow ran.  Its sibling
recipe_for_persona restores itself through load_current_flow; the count now
restores itself from the agent's saved config the same way.
"""
import json
import sys
import time

import pytest

import hartos.create_recipe  # noqa: F401  (ensure cached)
from hartos import helper as helper_fun

cr = sys.modules['hartos.create_recipe']


def _expire(cache, key):
    """Age one entry past its TTL, as two idle hours would."""
    with cache._lock:
        cache._timestamps[key] = time.monotonic() - cache._ttl - 1


def _write_config(prompts_dir, prompt_id, n_flows):
    flows = [{'actions': [f'action {i}']} for i in range(n_flows)]
    (prompts_dir / f'{prompt_id}.json').write_text(
        json.dumps({'flows': flows}), encoding='utf-8')


@pytest.fixture
def prompts_dir(tmp_path, monkeypatch):
    # The writer's reader, get_prompt_config_json, opens
    # helper_fun.safe_prompt_path(prompt_id), under helper_fun.PROMPTS_DIR.
    monkeypatch.setattr(helper_fun, 'PROMPTS_DIR', str(tmp_path))
    return tmp_path


def test_the_count_survives_a_flow_longer_than_the_ttl(prompts_dir):
    """THE LIVE SHAPE: written at start, unread for longer than the TTL,
    then read at the flow boundary."""
    up = '777_88800011122'
    _write_config(prompts_dir, '88800011122', 10)
    cr.total_persona_actions[up] = 10
    try:
        _expire(cr.total_persona_actions, up)
        assert cr.get_total_flows(up) == 10
    finally:
        cr.total_persona_actions.pop(up, None)


def test_a_cold_read_takes_the_count_from_the_saved_config(prompts_dir):
    up = '777_88800011123'
    _write_config(prompts_dir, '88800011123', 3)
    cr.total_persona_actions.pop(up, None)
    try:
        assert cr.get_total_flows(up) == 3
    finally:
        cr.total_persona_actions.pop(up, None)


def test_an_agent_with_no_saved_config_is_still_a_miss(prompts_dir):
    """No invented count: a key with nothing on disk stays a KeyError."""
    with pytest.raises(KeyError):
        cr.get_total_flows('777_88800011124')


@pytest.mark.parametrize('key,body', [
    ('no-separator', None),
    ('777_88800011125', '{"flows": "not a list"}'),
    ('777_88800011126', '{"no_flows_here": 1}'),
])
def test_the_loader_answers_none_for_a_bad_key_or_config(prompts_dir, key, body):
    if body is not None:
        (prompts_dir / f"{key.split('_', 1)[1]}.json").write_text(
            body, encoding='utf-8')
    assert cr.load_total_flows(key) is None


def test_a_stray_entry_in_flows_counts_as_the_writer_counts_it(prompts_dir):
    """Review of 263597bda (F1): the writer, initialize_with_resume, counts
    len(get_prompt_config_json(pid)['flows']), and that reader drops what is
    not a flow (_drop_non_flow_entries).  The loader read the raw config, so a
    stray '}' or a misplaced top-level key in flows[] made a cold read one
    more than the count the writer wrote, and _has_more_flows would look for
    a flow that does not exist."""
    pid = '88800011127'
    flows = [{'actions': ['a']}, '}', {'actions': ['b']},
             {'extra_information': 'misplaced top-level key'},
             {'actions': ['c']}]
    (prompts_dir / f'{pid}.json').write_text(
        json.dumps({'flows': flows}), encoding='utf-8')
    up = f'777_{pid}'
    cr.total_persona_actions.pop(up, None)
    try:
        writers_count = len(cr.get_prompt_config_json(pid)['flows'])
        assert writers_count == 3
        assert cr.get_total_flows(up) == writers_count
    finally:
        cr.total_persona_actions.pop(up, None)


def test_a_user_id_with_an_underscore_still_names_the_agent(prompts_dir):
    """Review of 263597bda (F2): the key is '<user_id>_<prompt_id>'; the
    prompt id is after the LAST underscore, so a user id holding one does
    not take the prompt id's place."""
    pid = '88800011128'
    _write_config(prompts_dir, pid, 4)
    up = f'guest_device_7_{pid}'
    cr.total_persona_actions.pop(up, None)
    try:
        assert cr.load_total_flows(up) == 4
        assert cr.get_total_flows(up) == 4
    finally:
        cr.total_persona_actions.pop(up, None)
