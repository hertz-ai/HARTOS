"""A person's place in an agent's steps must survive a rebuild or reinstall.

WHAT WENT WRONG (desktop, 2026-11-11, speech agent 1793366231447)
  SmartLedger saved to the bare relative folder "agent_data", and Nunba moves
  its working folder to the program's own folder at start (app.py chdir to
  the exe folder).  So a person's steps were written inside the program:
  C:\\Program Files (x86)\\HevolveAI\\Nunba\\agent_data for the installed app
  (1,806 ledgers), Nunba-HART-Companion\\build\\Nunba\\agent_data for a build
  run from its folder.  All 24 REUSE sessions logged on 11-11 between 01:15
  and 07:30 IST, both proof accounts included, were gone the next morning:
  each rebuild replaced the folder they lived in.  The resume scan read the
  same relative folder, so after a rebuild or reinstall it found nothing and
  the agent started the person over and asked again.

  Every other reader already used the node's agent-data folder
  (core.cache_loaders.AGENT_DATA_DIR: ~/Documents/Nunba/data/agent_data on a
  desktop): load_current_flow, the dashboard, the zombie reaper's walk.  So
  even without a rebuild, the current-flow loader read a folder REUSE never
  wrote and put the person back on flow 0.

  And the ledger registry's cache-miss loader called
  get_or_create_ledger(user_id, prompt_id) positionally: agent_id=user_id,
  session_id=prompt_id, a key nothing saves, so a miss handed back a new EMPTY
  ledger and cached it for 8 hours.

THE RULE
  A ledger with no folder named lives in the folder the node's readers read
  (core.cache_loaders.AGENT_DATA_DIR).  Ledgers a node saved in its program
  folder are copied there once; nothing is deleted or overwritten.  A
  registry miss reopens the person's own in-flight session, or nothing.

Run:
  pytest tests/unit/test_a_persons_steps_outlive_the_program_folder.py -q --noconftest
"""

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "agent-ledger-opensource"))
sys.path.insert(0, REPO)

from agent_ledger.core import (  # noqa: E402
    SmartLedger, TaskStatus, create_ledger_from_actions,
)

PROMPT = 777000111
CHILD = "d0000000-child-0000-0000-000000000001"
ACTIONS = [
    {"action_id": 1, "action": "Ask the child to say Amma"},
    {"action_id": 2, "action": "Praise the try and record it"},
]


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """The node's agent-data folder, and a program folder the process runs
    in (Nunba's working folder is the exe's folder)."""
    import core.cache_loaders as cl
    data = tmp_path / "Documents" / "Nunba" / "data" / "agent_data"
    data.mkdir(parents=True)
    monkeypatch.setattr(cl, "AGENT_DATA_DIR", str(data))
    program = tmp_path / "Program Files" / "Nunba-build-72"
    program.mkdir(parents=True)
    monkeypatch.chdir(program)
    return {"data": data, "program": program, "root": tmp_path}


def _ledger_file(folder, ledger):
    return folder / f"ledger_{ledger.agent_id}_{ledger.session_id}.json"


def _start(user=CHILD, flow_id=0):
    """What REUSE does on a person's turn: build the ledger from the recipe's
    actions (reuse_recipe.py, create_ledger_from_actions) and start step 1."""
    ledger = create_ledger_from_actions(user_id=user, prompt_id=PROMPT,
                                        actions=ACTIONS, flow_id=flow_id)
    ledger.update_task_status("action_1", TaskStatus.IN_PROGRESS)
    return ledger


def test_a_persons_steps_are_saved_in_the_data_folder_not_the_program(folders):
    ledger = _start()
    assert _ledger_file(folders["data"], ledger).exists(), (
        "the person's steps were not saved in the node's agent-data folder, the "
        "one load_current_flow, the dashboard and the reaper read")
    assert not _ledger_file(folders["program"] / "agent_data", ledger).exists(), (
        "the person's steps were saved inside the program folder, which a "
        "rebuild or reinstall replaces")


def test_the_person_resumes_their_step_after_a_rebuild(folders, monkeypatch):
    first = _start()
    rebuilt = folders["root"] / "Program Files" / "Nunba-build-73"
    rebuilt.mkdir(parents=True)
    monkeypatch.chdir(rebuilt)

    again = create_ledger_from_actions(user_id=CHILD, prompt_id=PROMPT,
                                       actions=ACTIONS)
    assert again.session_id == first.session_id, (
        "after the program moved to a new folder the person got a fresh "
        f"session ({again.session_id}) instead of their own ({first.session_id}): "
        "the agent starts them over and asks again")
    assert again.get_task("action_1").status == TaskStatus.IN_PROGRESS


def test_the_current_flow_loader_reads_where_the_steps_are_saved(folders):
    from core.cache_loaders import load_current_flow
    _start(flow_id=1)
    assert load_current_flow(f"{CHILD}_{PROMPT}") == 1, (
        "the person is on flow 1, but the loader read a folder REUSE never "
        "wrote and put them back on flow 0")


def test_the_reapers_index_reads_the_folder_the_steps_are_saved_in(folders):
    ledger = _start()
    assert ledger.ledger_key in SmartLedger.keys_in_progress(), (
        "a step in progress was saved where the zombie reaper's index does not "
        "look, so a dead turn's step is never released")


def test_a_registry_miss_reopens_the_persons_own_session(folders):
    from hartos.lifecycle_hooks import _load_ledger_on_miss
    ledger = _start()
    reopened = _load_ledger_on_miss(f"{CHILD}_{PROMPT}")
    assert reopened is not None, "a miss lost the person's in-flight session"
    assert (str(reopened.agent_id), reopened.session_id) == (
        str(PROMPT), ledger.session_id), (
        f"a miss opened ledger {reopened.agent_id}:{reopened.session_id}, not the "
        f"person's own {PROMPT}:{ledger.session_id}")
    assert reopened.get_task("action_1").status == TaskStatus.IN_PROGRESS


def test_a_registry_miss_with_nothing_in_flight_returns_nothing(folders):
    from hartos.lifecycle_hooks import _load_ledger_on_miss
    assert _load_ledger_on_miss(f"{CHILD}_{PROMPT}") is None, (
        "a miss with no session in flight handed back a new empty ledger, which "
        "the registry then serves as the person's for 8 hours")


OLD_BUILD = [
    {"action_id": 1, "action": "Show the child a picture of a cat"},
    {"action_id": 2, "action": "Ask the child to name it"},
    {"action_id": 3, "action": "Say well done"},
]


def _finish(ledger):
    for task_id in list(ledger.tasks):
        ledger.update_task_status(task_id, TaskStatus.IN_PROGRESS)
        ledger.update_task_status(task_id, TaskStatus.COMPLETED)


def test_a_registry_miss_never_opens_an_older_builds_session(folders):
    """Review of b48cd6693, must-fix 1 (its probe): an earlier build's session
    is left in progress, the agent is rebuilt, the current build's session is
    done.  A miss must not reopen the earlier build's session (lifecycle syncs
    would then write this turn's states into another build's steps)."""
    import time as _time
    from hartos.lifecycle_hooks import _load_ledger_on_miss
    old = create_ledger_from_actions(user_id=CHILD, prompt_id=PROMPT,
                                     actions=OLD_BUILD)
    old.update_task_status("action_1", TaskStatus.IN_PROGRESS)
    _time.sleep(0.01)
    current = create_ledger_from_actions(user_id=CHILD, prompt_id=PROMPT,
                                         actions=ACTIONS)
    assert current.session_id != old.session_id
    _finish(current)
    reopened = _load_ledger_on_miss(f"{CHILD}_{PROMPT}")
    assert reopened is None, (
        f"a miss reopened {getattr(reopened, 'session_id', None)}, the earlier "
        f"build's session, while the current build's ({current.session_id}) is done")


def test_a_registry_miss_never_takes_another_persons_session(folders):
    """Another person's session of the same agent is in progress, this person
    has none: the miss gives this person nothing (review of b48cd6693: the
    mutant that drops the person from the scan survived)."""
    from hartos.lifecycle_hooks import _load_ledger_on_miss
    _start(user="d0000000-other-0000-0000-000000000002")
    assert _load_ledger_on_miss(f"{CHILD}_{PROMPT}") is None, (
        "a miss handed this person another person's session")


def test_a_miss_reads_only_the_persons_own_steps(folders, monkeypatch):
    """Review of b48cd6693, must-fix 2: a miss parsed every ledger in the
    folder (3,074 on the owner's desktop; 19 s cold, 1.8 s warm on 3,000)
    while the registry's lock was held.  It reads this person's ledgers of
    this agent only."""
    import agent_ledger.core as alc
    from hartos.lifecycle_hooks import _load_ledger_on_miss
    for n in range(12):
        _start(user=f"d0000000-other-0000-0000-{n:012d}")
        SmartLedger(str(PROMPT + 1 + n), f"{CHILD}_{PROMPT + 1 + n}_1").save()
    mine = _start()
    parsed = []
    real_load = alc.json.load

    def _counting_load(fh, *a, **kw):
        parsed.append(os.path.basename(getattr(fh, 'name', '?')))
        return real_load(fh, *a, **kw)

    monkeypatch.setattr(alc.json, 'load', _counting_load)
    reopened = _load_ledger_on_miss(f"{CHILD}_{PROMPT}")
    assert reopened is not None and reopened.session_id == mine.session_id
    others = [p for p in parsed if p.startswith('ledger_')
              and p != _ledger_file(folders["data"], mine).name]
    assert not others, f"a miss parsed {len(others)} other ledgers: {sorted(others)[:4]}"


def test_a_miss_with_nothing_in_flight_is_not_read_again_at_once(folders, monkeypatch):
    """Lifecycle hooks read the registry on every state change, and a session
    that owns no ledger misses every time: the second miss within a short
    window does not read the folder again; a registered ledger is served at
    once all the same."""
    from agent_ledger.core import SmartLedger as _SL
    import hartos.lifecycle_hooks as lh
    reads = []
    real = _SL.list_grouped_by_recipe_hierarchy.__func__

    def _counting(cls, *a, **kw):
        reads.append(1)
        return real(cls, *a, **kw)

    monkeypatch.setattr(_SL, 'list_grouped_by_recipe_hierarchy', classmethod(_counting))
    key = f"{CHILD}_{PROMPT}"
    lh._ledger_registry.pop(key, None)
    assert lh.get_registered_ledger(key) is None
    assert lh.get_registered_ledger(key) is None
    assert len(reads) == 1, f"the folder was read {len(reads)} times for two misses"
    ledger = _start()
    lh.register_ledger_for_session(key, ledger)
    try:
        assert lh.get_registered_ledger(key) is ledger
        # The registration forgot the remembered miss: with the entry gone
        # (evicted), the next read reopens the session from disk at once.
        lh._ledger_registry.pop(key, None)
        reopened = lh.get_registered_ledger(key)
        assert reopened is not None and reopened.session_id == ledger.session_id, (
            "after a registration the old miss was still remembered, so the "
            "person's session in progress was not reopened")
    finally:
        lh._ledger_registry.pop(key, None)


def test_the_program_folders_old_steps_are_copied_once_never_overwritten(folders, monkeypatch):
    import agent_ledger.core as alc
    old = folders["program"] / "agent_data"
    # Saved by an earlier build, in the program folder (explicit folder: the
    # old default).
    kept = SmartLedger(str(PROMPT), f"{CHILD}_{PROMPT}_1", ledger_dir=str(old))
    kept.add_task(alc.Task(task_id="action_1", description="say Amma",
                           task_type=alc.TaskType.PRE_ASSIGNED,
                           status=TaskStatus.PENDING))
    kept.save()
    same_name = SmartLedger(str(PROMPT), f"{CHILD}_{PROMPT}_2", ledger_dir=str(old))
    same_name.save()
    already = folders["data"] / _ledger_file(old, same_name).name
    already.write_text(json.dumps({"tasks": {}, "mine": "data folder copy"}),
                       encoding="utf-8")

    create_ledger_from_actions(user_id="someone-else", prompt_id=PROMPT,
                               actions=ACTIONS)

    copied = folders["data"] / _ledger_file(old, kept).name
    assert copied.exists(), "the steps saved in the program folder were not brought over"
    assert json.loads(already.read_text(encoding="utf-8")).get("mine") == "data folder copy", (
        "a ledger already in the data folder was overwritten by the old one")
    assert _ledger_file(old, kept).exists(), "the old folder's ledger was deleted"

    # Once: a later start (a restart: the process's memo is new) does not copy
    # a file the data folder has since let go of.
    copied.unlink()
    monkeypatch.setattr(alc, "_OLD_FOLDERS_COPIED", set())
    create_ledger_from_actions(user_id="someone-else-2", prompt_id=PROMPT,
                               actions=ACTIONS)
    assert not copied.exists(), "the old folder was copied again after a restart"
