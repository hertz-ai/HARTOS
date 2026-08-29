"""Cutting the log firehose must not cost us any diagnostic information.

THE MEASUREMENT THAT FORCED THIS. On the box 2026-08-29, deduped over two
minutes of journal:

    6498 x  INFO ...dynamic_agent_registry:Discovered agent: N_N (persona: unknown)
      38 x  INFO ...dynamic_agent_registry:Scanning /var/lib/hart/data/prompts ...
      38 x  INFO ...dynamic_agent_registry:Discovered N trained agents

55 lines/second from one module. build_agent_directory() (GET /a2a/agents)
rescans on every request and something polls it ~19 times a minute, so the whole
roster was re-logged every three seconds. journald had written 6.3 GB, systemd
71.8 GB across 48h, io pressure full avg10=5.30 — on a USB stick root that then
began returning hard read errors.

THE RULE THESE TESTS ENFORCE: report the DELTA, never the roster, and lose
nothing. Every fact the old logging carried must still be emitted, and the two
facts it never carried — agents DISAPPEARING, and an explicit "still alive,
nothing changed" — must now be emitted too. Silence must never be ambiguous
between "no change" and "the scanner died".

Run:
  pytest tests/unit/test_agent_discovery_logging.py -v
"""

import logging
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.google_a2a import dynamic_agent_registry as R  # noqa: E402


@pytest.fixture(autouse=True)
def clean_module_state():
    """The reported-set record is module level (it must be — see below), so each
    test starts from a clean slate."""
    R._reported_agents.clear()
    R._reported_at.clear()
    yield
    R._reported_agents.clear()
    R._reported_at.clear()


def make_agent(agent_id, persona='analyst'):
    prompt_id, flow_id = agent_id.split('_')
    return R.TrainedAgent(
        agent_id=agent_id, prompt_id=int(prompt_id), flow_id=int(flow_id),
        persona=persona, action='do a thing', recipe=[], status='ready',
        can_perform_without_user_input='yes', fallback_action='',
        metadata={}, recipe_file='%s_recipe.json' % agent_id)


def scanner(tmp_path, agent_ids, monkeypatch, broken=()):
    """A discovery instance over a fake prompts dir holding `agent_ids`."""
    d = tmp_path / 'prompts'
    d.mkdir(exist_ok=True)
    for name in list(agent_ids) + list(broken):
        (d / ('%s_recipe.json' % name)).write_text('{}')
    # Remove recipe files for agents no longer present, so the glob shrinks.
    for f in d.glob('*_recipe.json'):
        stem = f.name[:-len('_recipe.json')]
        if stem not in list(agent_ids) + list(broken):
            f.unlink()

    disc = R.DynamicAgentDiscovery(prompts_dir=str(d))
    monkeypatch.setattr(disc, '_load_prompt_definitions', lambda: None)

    def fake_load(recipe_file):
        stem = os.path.basename(recipe_file)[:-len('_recipe.json')]
        if stem in broken:
            raise ValueError('corrupt recipe')
        return make_agent(stem)

    monkeypatch.setattr(disc, '_load_agent_from_recipe', fake_load)
    return disc


def messages(caplog, level=logging.INFO):
    return [r.getMessage() for r in caplog.records if r.levelno >= level]


# ── the first scan must be exactly as informative as before ─────────────────

def test_first_scan_still_names_every_agent_and_the_count(tmp_path, monkeypatch,
                                                          caplog):
    """Startup fidelity is untouched: the directory, every agent with its
    persona, and the total. This is the record of what the node knows."""
    caplog.set_level(logging.INFO)
    scanner(tmp_path, ['71_0', '82_0'], monkeypatch).discover_all_agents()
    msgs = messages(caplog)
    assert any('Scanning' in m and 'prompts' in m for m in msgs)
    assert any('Discovered agent: 71_0 (persona: analyst)' == m for m in msgs)
    assert any('Discovered agent: 82_0 (persona: analyst)' == m for m in msgs)
    assert any('Discovered 2 trained agents' == m for m in msgs)


# ── the firehose itself ─────────────────────────────────────────────────────

def test_an_unchanged_rescan_says_nothing(tmp_path, monkeypatch, caplog):
    """THE BUG. 19 rescans a minute re-logged all 171 agents every time."""
    scanner(tmp_path, ['71_0', '82_0'], monkeypatch).discover_all_agents()
    caplog.clear()
    caplog.set_level(logging.INFO)
    for _ in range(10):
        scanner(tmp_path, ['71_0', '82_0'], monkeypatch).discover_all_agents()
    assert messages(caplog) == [], (
        'an unchanged rescan must be silent; got %r' % messages(caplog))


def test_the_record_is_shared_across_fresh_instances(tmp_path, monkeypatch,
                                                     caplog):
    """LOAD-BEARING. build_agent_directory() constructs a NEW
    DynamicAgentDiscovery on every GET /a2a/agents, so if the already-reported
    set lived on the instance it would be empty each time and the whole roster
    would be re-logged forever — a fix that changes nothing. Each call below is a
    separate instance, exactly like the hot path."""
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    caplog.clear()
    caplog.set_level(logging.INFO)
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    assert messages(caplog) == []


# ── nothing informative may be lost ─────────────────────────────────────────

def test_a_new_agent_is_still_announced_by_name(tmp_path, monkeypatch, caplog):
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    caplog.clear()
    caplog.set_level(logging.INFO)
    scanner(tmp_path, ['71_0', '99_0'], monkeypatch).discover_all_agents()
    msgs = messages(caplog)
    assert any('Discovered agent: 99_0 (persona: analyst)' == m for m in msgs)
    assert any('Discovered 2 trained agents (+1 -0)' == m for m in msgs)
    assert not any('71_0' in m for m in msgs), 'the unchanged agent was re-logged'


def test_a_vanished_agent_is_reported_which_it_never_was_before(tmp_path,
                                                                monkeypatch,
                                                                caplog):
    """NEW information. In a wall of 171 identical lines every three seconds, a
    recipe disappearing was invisible. Now it is one line."""
    scanner(tmp_path, ['71_0', '82_0'], monkeypatch).discover_all_agents()
    caplog.clear()
    caplog.set_level(logging.INFO)
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    msgs = messages(caplog)
    assert any('Agent no longer present: 82_0' == m for m in msgs)
    assert any('Discovered 1 trained agents (+0 -1)' == m for m in msgs)


def test_a_broken_recipe_still_warns(tmp_path, monkeypatch, caplog):
    """The warning path is deliberately untouched — a recipe that fails to load
    is precisely what this log exists to surface, and it is rare."""
    caplog.set_level(logging.INFO)
    scanner(tmp_path, ['71_0'], monkeypatch, broken=['66_0']).discover_all_agents()
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any('Failed to load agent from' in m and '66_0' in m
               for m in warnings)


def test_a_broken_recipe_warns_on_every_rescan(tmp_path, monkeypatch, caplog):
    """Deliberate: the per-agent INFO is deduped, the failure warning is NOT.
    A persistently broken recipe should keep saying so."""
    scanner(tmp_path, ['71_0'], monkeypatch, broken=['66_0']).discover_all_agents()
    caplog.clear()
    caplog.set_level(logging.WARNING)
    scanner(tmp_path, ['71_0'], monkeypatch, broken=['66_0']).discover_all_agents()
    assert any('Failed to load agent from' in r.getMessage()
               for r in caplog.records)


# ── silence must not be ambiguous ───────────────────────────────────────────

def test_an_unchanged_scan_eventually_says_it_is_still_alive(tmp_path,
                                                             monkeypatch,
                                                             caplog):
    """Silence has to mean "no change", never "the scanner died". After the
    heartbeat interval an unchanged scan states the count again."""
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    # Pretend the last thing we said was longer ago than the heartbeat.
    for key in list(R._reported_at):
        R._reported_at[key] -= (R.UNCHANGED_HEARTBEAT_SECONDS + 1)
    caplog.clear()
    caplog.set_level(logging.INFO)
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    assert any('Discovered 1 trained agents (unchanged)' == m
               for m in messages(caplog))


def test_the_heartbeat_does_not_fire_every_scan(tmp_path, monkeypatch, caplog):
    """It is a heartbeat, not a second firehose."""
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    for key in list(R._reported_at):
        R._reported_at[key] -= (R.UNCHANGED_HEARTBEAT_SECONDS + 1)
    scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()   # beats once
    caplog.clear()
    caplog.set_level(logging.INFO)
    for _ in range(5):
        scanner(tmp_path, ['71_0'], monkeypatch).discover_all_agents()
    assert messages(caplog) == []


def test_the_return_value_is_unchanged(tmp_path, monkeypatch):
    """Callers count on the count. Logging changes must not touch behaviour."""
    disc = scanner(tmp_path, ['71_0', '82_0', '99_0'], monkeypatch)
    assert disc.discover_all_agents() == 3
    again = scanner(tmp_path, ['71_0', '82_0', '99_0'], monkeypatch)
    assert again.discover_all_agents() == 3
    assert set(again.discovered_agents) == {'71_0', '82_0', '99_0'}
