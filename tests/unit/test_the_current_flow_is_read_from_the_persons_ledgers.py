"""The current-flow loader reads the person's ledgers of that agent, no more.

load_current_flow (core/cache_loaders.py) restores recipe_for_persona's
flow on a cache miss: on the first turn after a restart, and after an
idle eviction.  It asked list_grouped_by_recipe_hierarchy for EVERY ledger
in the folder (1,902 files, 49 MB on the owner's desktop on 22 Nov 2026,
measured by hevolve-66e6), then used only one agent's sessions for one
person.  _find_resumable_session already reads ledger_<agent>_<user>_*.json
only (17b251c11); this loader asks with the same pattern.

The ledgers are real files in a temporary folder and the real loader and
folder reader run; only json.load is wrapped, to see which files are read.
"""

import json

import pytest

AGENT = 54
PERSON = 10202
OTHER = 20303


def _ledger(folder, agent, session, flow_status):
    tasks = {}
    for flow, action, status in flow_status:
        tid = f"action_{action}"
        tasks[tid] = {"task_id": tid, "status": status,
                      "recipe_prompt_id": str(agent),
                      "recipe_flow_id": flow, "recipe_action_id": action}
    path = folder / f"ledger_{agent}_{session}.json"
    path.write_text(json.dumps({"agent_id": str(agent), "session_id": session,
                                "tasks": tasks}))
    return path.name


@pytest.fixture
def folder(tmp_path, monkeypatch):
    import core.cache_loaders as cache_loaders
    monkeypatch.setattr(cache_loaders, 'AGENT_DATA_DIR', str(tmp_path))
    return tmp_path


@pytest.fixture
def files_read(monkeypatch):
    import agent_ledger.core as ledger_core
    read = []
    real_load = ledger_core.json.load

    def _recording_load(fh, *args, **kwargs):
        read.append(fh.name.replace('\\', '/').rsplit('/', 1)[-1])
        return real_load(fh, *args, **kwargs)

    monkeypatch.setattr(ledger_core.json, 'load', _recording_load)
    return read


def test_a_miss_reads_only_the_persons_ledgers_of_that_agent(folder, files_read):
    mine = {
        _ledger(folder, AGENT, f"{PERSON}_{AGENT}_1795300000000",
                [(0, 1, "completed"), (1, 1, "in_progress")]),
        _ledger(folder, AGENT, f"{PERSON}_{AGENT}",
                [(0, 1, "completed")]),
    }
    _ledger(folder, AGENT, f"{OTHER}_{AGENT}_1795300000001",
            [(3, 1, "in_progress")])
    for other_agent in range(100, 140):
        _ledger(folder, other_agent, f"{PERSON}_{other_agent}_1795300000002",
                [(0, 1, "in_progress")])

    from core.cache_loaders import load_current_flow
    assert load_current_flow(f"{PERSON}_{AGENT}") == 1
    assert set(files_read) == mine


def test_a_person_with_no_ledger_of_that_agent_reads_nothing(folder, files_read):
    _ledger(folder, AGENT, f"{OTHER}_{AGENT}_1795300000001",
            [(3, 1, "in_progress")])
    _ledger(folder, 77, f"{PERSON}_77_1795300000002", [(2, 1, "in_progress")])

    from core.cache_loaders import load_current_flow
    assert load_current_flow(f"{PERSON}_{AGENT}") == 0
    assert files_read == []


def test_a_person_whose_id_starts_another_persons_is_not_mixed_up(folder):
    """Pin: person 1020 must not read person 10202's flow (the pattern
    ends the person's id at '_')."""
    _ledger(folder, AGENT, f"{PERSON}_{AGENT}_1795300000000",
            [(4, 1, "in_progress")])

    from core.cache_loaders import load_current_flow
    assert load_current_flow(f"1020_{AGENT}") == 0
    assert load_current_flow(f"{PERSON}_{AGENT}") == 4
