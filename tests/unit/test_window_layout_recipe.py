"""Recipe-banked window layouts (Phase 8 / IPC §8) — behavioural.

The Recipe Pattern extended to window management: a CREATE-mode action banks its
``window.*`` steps (summon + tile + switch) into a per-(prompt_id, flow_id)
artifact so a later REUSE replays the EXACT desktop layout WITHOUT any LLM call.

These tests mock ONLY the WM-client boundary (a fake HartWmClient whose
``dispatch_verb`` records calls and returns canned gate verdicts) + the temp
PROMPTS_DIR. They register no Flask app and assert observable behaviour — the
banked file content, the replay's per-step report, and that EVERY replayed step
went through ``dispatch_verb`` (the SAME fail-closed gate a live agent verb
passes). No grep / source-shape assertions.

    python -m pytest tests/unit/test_window_layout_recipe.py -q
"""
from __future__ import annotations

import json

import pytest

import integrations.agent_engine.window_layout_recipe as wlr
from integrations.agent_engine.window_layout_recipe import (
    LayoutRecorder, bank_layout, has_layout, load_layout, replay_layout,
)


# ── A fake WM client = the gate boundary (the ONLY thing mocked) ──
class FakeWM:
    """Stands in for HartWmClient. Records every dispatch_verb call so the test
    can assert the replay/record routed through the gate, and returns a canned
    verdict per verb so honest-failure (unsupported/refused) is exercisable."""

    def __init__(self, available=True, verdicts=None):
        self.available = available
        self.calls = []                      # [(verb, args, agent_id), ...]
        self._verdicts = verdicts or {}      # verb -> result dict

    def dispatch_verb(self, verb, args, agent_id):
        self.calls.append((verb, dict(args or {}), agent_id))
        # Default: succeed for any non-destructive arrange; summon is honest-fail.
        if verb in self._verdicts:
            return dict(self._verdicts[verb])
        if verb == 'window.summon':
            return {'ok': False, 'error': 'unsupported'}
        return {'ok': True}


@pytest.fixture(autouse=True)
def _tmp_prompts(tmp_path, monkeypatch):
    """Point the layout store at a temp dir so tests never touch real prompts/."""
    monkeypatch.setattr(wlr, 'PROMPTS_DIR', str(tmp_path))
    return tmp_path


# ═══════════════════════════ bank_layout ═══════════════════════════

def test_bank_drops_non_bankable_verbs(_tmp_prompts):
    ok = bank_layout('42', 0, [
        {'verb': 'window.tile', 'args': {'layout': 'splith'}},
        {'verb': 'window.evil', 'args': {}},          # not in the allowlist
        {'verb': 'goal.dispatch', 'args': {'x': 1}},  # smuggled non-window verb
    ])
    assert ok is True
    data = load_layout('42', 0)
    verbs = [s['verb'] for s in data['steps']]
    assert verbs == ['window.tile'], "non-bankable verbs must be dropped"


def test_bank_empty_writes_nothing(_tmp_prompts):
    assert bank_layout('42', 1, []) is False
    assert bank_layout('42', 1, [{'verb': 'nope'}]) is False
    assert has_layout('42', 1) is False


def test_bank_roundtrip_persists_coordinate_and_steps(_tmp_prompts):
    steps = [
        {'verb': 'window.summon', 'args': {'manifest_id': 'files'}},
        {'verb': 'window.tile', 'args': {'layout': 'splith'}},
        {'verb': 'workspace.switch', 'args': {'workspace': 2}},
    ]
    assert bank_layout(7, 3, steps) is True
    assert has_layout(7, 3) is True
    raw = json.loads((_tmp_prompts / '7_3_window_layout.json').read_text())
    assert raw['prompt_id'] == '7' and raw['flow_id'] == 3
    assert [s['verb'] for s in raw['steps']] == [
        'window.summon', 'window.tile', 'workspace.switch']


def test_bank_coerces_uuid_prompt_id_to_str_filename(_tmp_prompts):
    """prompt_id is int (human) OR a UUID string (autonomous) — both coerce to a
    str filename, matching create_ledger_from_actions."""
    uid = 'goal-abcdef-1234'
    assert bank_layout(uid, 0, [{'verb': 'window.tile', 'args': {}}]) is True
    assert (_tmp_prompts / f'{uid}_0_window_layout.json').exists()


# ═══════════════════════════ replay_layout ═══════════════════════════

def test_replay_missing_recipe_is_honest_not_error(_tmp_prompts):
    rep = replay_layout('999', 0, wm_client=FakeWM())
    assert rep['ok'] is False and rep['total'] == 0
    assert rep['reason'] == 'no-layout'


def test_replay_routes_every_step_through_the_gate(_tmp_prompts):
    bank_layout('5', 0, [
        {'verb': 'window.tile', 'args': {'layout': 'splith'}},
        {'verb': 'workspace.switch', 'args': {'workspace': 2}},
        {'verb': 'window.focus', 'args': {'con_id': 11}},
    ])
    wm = FakeWM(available=True)
    rep = replay_layout('5', 0, agent_id='goal_x', wm_client=wm)
    assert rep['ok'] is True and rep['replayed'] == 3 and rep['total'] == 3
    # EVERY banked step was dispatched through the SAME gate, in order, with the
    # agent_id propagated (so the constitution vets destructive ops at replay).
    assert [c[0] for c in wm.calls] == [
        'window.tile', 'workspace.switch', 'window.focus']
    assert all(c[2] == 'goal_x' for c in wm.calls)


def test_replay_surfaces_honest_failure_not_masked(_tmp_prompts):
    """A banked window.summon that the Tier-2 shim cannot confirm (no real
    toplevel mapped) returns unsupported — the replay reports it, never a
    phantom success (§8, §1.4)."""
    bank_layout('6', 0, [
        {'verb': 'window.tile', 'args': {'layout': 'splith'}},
        {'verb': 'window.summon', 'args': {'manifest_id': 'x'}},
    ])
    rep = replay_layout('6', 0, wm_client=FakeWM())
    assert rep['ok'] is False               # not every step succeeded
    assert rep['replayed'] == 1 and rep['total'] == 2
    summon = [r for r in rep['results'] if r['verb'] == 'window.summon'][0]
    assert summon['ok'] is False and summon.get('error') == 'unsupported'


def test_replay_refused_destructive_step_is_reported(_tmp_prompts):
    """A banked window.close the constitution refuses at replay time is reported
    as a refusal — the gate still runs on REUSE (§8)."""
    bank_layout('8', 0, [{'verb': 'window.close', 'args': {'con_id': 3}}])
    wm = FakeWM(verdicts={'window.close':
                          {'ok': False, 'error': 'refused-by-constitution'}})
    rep = replay_layout('8', 0, wm_client=wm)
    assert rep['ok'] is False and rep['replayed'] == 0
    assert rep['results'][0]['error'] == 'refused-by-constitution'
    assert wm.calls and wm.calls[0][0] == 'window.close'  # gate WAS consulted


def test_replay_no_compositor_is_noop_not_phantom(_tmp_prompts):
    """Cage Tier-3 (no native windows): replay must NOT pretend it ran — it's a
    logged no-op and the layout is preserved for a future tier."""
    bank_layout('9', 0, [{'verb': 'window.tile', 'args': {'layout': 'splith'}}])
    wm = FakeWM(available=False)
    rep = replay_layout('9', 0, wm_client=wm)
    assert rep['ok'] is False and rep['available'] is False
    assert rep['reason'] == 'no-compositor'
    assert wm.calls == [], "must not dispatch when no compositor is present"
    assert has_layout('9', 0) is True, "layout kept for a future tier"


# ═══════════════════════ LayoutRecorder (CREATE) ═══════════════════════

def test_recorder_banks_only_allowed_steps_then_replays(_tmp_prompts):
    """CREATE banks summon+tile+switch via the SAME gate; the banked recipe
    replays the EXACT layout. A gate-refused / unsupported step is NOT banked, so
    REUSE never replays a step that never really happened."""
    create_wm = FakeWM(
        available=True,
        verdicts={
            'window.summon': {'ok': False, 'error': 'unsupported'},  # honest-fail
            'window.tile': {'ok': True},
            'workspace.switch': {'ok': True},
        })
    rec = LayoutRecorder(agent_id='goal_abc', wm_client=create_wm)
    rec.dispatch('window.summon', {'manifest_id': 'files'})   # NOT banked (failed)
    rec.dispatch('window.tile', {'layout': 'splith'})         # banked
    rec.dispatch('workspace.switch', {'workspace': 2})        # banked
    rec.dispatch('goal.dispatch', {'x': 1})                   # NOT bankable verb

    # Only the two real, allowed, bankable ops were recorded.
    assert [s['verb'] for s in rec.steps] == ['window.tile', 'workspace.switch']

    assert rec.flush(11, 0) is True
    data = load_layout(11, 0)
    assert [s['verb'] for s in data['steps']] == ['window.tile', 'workspace.switch']

    # REUSE: replay the banked layout through a fresh gate, no LLM involved.
    reuse_wm = FakeWM(available=True)
    rep = replay_layout(11, 0, agent_id='goal_abc', wm_client=reuse_wm)
    assert rep['ok'] is True and rep['replayed'] == 2
    assert [c[0] for c in reuse_wm.calls] == ['window.tile', 'workspace.switch']


def test_recorder_no_compositor_records_and_banks_nothing(_tmp_prompts):
    """On cage Tier-3 the recorder dispatches nothing and banks nothing — there
    was no real window op to replay."""
    rec = LayoutRecorder(agent_id='g', wm_client=FakeWM(available=False))
    r = rec.dispatch('window.tile', {'layout': 'splith'})
    assert r['ok'] is False and r.get('available') is False
    assert rec.steps == []
    assert rec.flush(12, 0) is False
    assert has_layout(12, 0) is False


def test_recorder_dispatch_never_raises(_tmp_prompts):
    """A wm_client whose dispatch blows up must not break the agent flow."""
    class Boom:
        available = True

        def dispatch_verb(self, *a, **k):
            raise RuntimeError("backend exploded")

    rec = LayoutRecorder(agent_id='g', wm_client=Boom())
    r = rec.dispatch('window.tile', {'layout': 'splith'})   # must swallow
    assert r['ok'] is False and 'dispatch raised' in r.get('error', '')
    assert rec.steps == []
