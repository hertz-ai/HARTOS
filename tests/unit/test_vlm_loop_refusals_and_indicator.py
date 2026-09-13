"""VLM loop: each goal gets its own action budget, a refused action is reported
as refused, and the AI-control ribbon is up while the loop drives the desktop.

Live 2026-09-13 (~/.nunba/audit/vlm_actions_20260913.jsonl): the
process-wide SessionGuard hit its 100-action cap at 19:05:24, 1 h 45 min
after boot, and every desktop action after that -- 2,506 today -- was refused
with "session-cap reached (100 actions)".  safety.py documents the guard as
reset "by the loop when it terminates a goal"; the loop never called the
reset.  Each refusal was then recorded ok:True with an empty result, so
neither the VLM nor the calling agent learned why.  Agent 89088690384 got
"Not able to perform this action now please try later" and then saved and
announced 118.5 GB free (the real figure was 24.2 GB).  Nothing on the
in-process tier asks Nunba to show its AI-control ribbon either: zero
"Ribbon indicator shown" lines all day.

The real run_local_agentic_loop and the real execute_action (safety guard
included) run here; only the screenshot, the VLM and the part that would
touch this machine are replaced.

    python -m pytest tests/unit/test_vlm_loop_refusals_and_indicator.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

CAP = 'session-cap reached (100 actions)'


class _ShellBackend:
    """Stands in for Qwen3VLBackend; always asks for one shell command."""

    def __init__(self):
        self.prompts = []

    def route_task(self, _instruction):
        return 'single_shot'

    def try_taskbar_pre_check(self, *_a, **_k):
        return None

    def detect_grounding_bias(self, *_a, **_k):
        return None

    def _call_api(self, messages):
        text = ''
        for part in (messages[0].get('content') or []):
            if isinstance(part, dict) and part.get('type') == 'text':
                text = part.get('text', '')
        self.prompts.append(text)
        return ('{"Reasoning": "read the free space", "Next Action": "shell", '
                '"command": "Get-PSDrive C", "Status": "IN_PROGRESS"}')


@pytest.fixture
def loop(monkeypatch):
    lct = pytest.importorskip('integrations.vlm.local_computer_tool')
    from integrations.vlm import local_loop, qwen3vl_backend, safety

    backend = _ShellBackend()
    ran = []

    def _fake_inprocess(action):
        ran.append(action)
        return {'output': 'Free 24.2 GB'}

    monkeypatch.setattr(qwen3vl_backend, 'get_qwen3vl_backend',
                        lambda: backend, raising=False)
    monkeypatch.setattr(lct, 'take_screenshot', lambda _tier: 'ZmFrZQ==',
                        raising=False)
    monkeypatch.setattr(lct, '_execute_inprocess', _fake_inprocess)
    monkeypatch.setattr(lct, '_emit_audit', lambda *a, **k: None)
    monkeypatch.setattr(lct, '_check_reasoning_mismatch', lambda action: None)
    monkeypatch.setenv('HEVOLVE_VLM_UNIFIED', '1')
    monkeypatch.setenv('HEVOLVE_VLM_LOOP_SAFETY', '1')
    monkeypatch.setenv('HEVOLVE_VLM_LOOP_VERIFY', '0')
    guard = safety.get_session_guard()
    guard.reset()
    yield local_loop, backend, ran, guard, lct
    guard.reset()


def _run(local_loop, uid):
    return local_loop.run_local_agentic_loop(
        {'instruction_to_vlm_agent': 'report the free disk space',
         'enhanced_instruction': 'report the free disk space',
         'user_id': uid, 'prompt_id': 'p-vlm', 'max_ETA_in_seconds': 60},
        tier='inprocess')


def _refuse_everything(monkeypatch, lct):
    monkeypatch.setattr(lct, '_check_safety',
                        lambda window_meta, action=None: CAP)


def test_a_new_goal_is_not_refused_by_an_earlier_goals_actions(loop):
    local_loop, _backend, ran, guard, _lct = loop
    # The live state from 19:05:24 on: the cap already spent by earlier goals.
    guard.action_count = guard.config.max_actions_per_session
    _run(local_loop, 'u-budget')
    assert ran, ("every action of a new goal was refused by the cap that "
                 "earlier goals had used up")


def test_a_refused_action_is_recorded_as_failed_with_its_reason(
        loop, monkeypatch):
    local_loop, _backend, ran, _guard, lct = loop
    _refuse_everything(monkeypatch, lct)
    out = _run(local_loop, 'u-refused')
    actions = [r['content'] for r in out['extracted_responses']
               if r.get('type') == 'action']
    assert actions, 'the loop recorded no actions'
    assert not ran, 'a refused action reached the machine'
    for a in actions:
        assert a['ok'] is False, f'refused action recorded as ok: {a}'
        assert CAP in a['result'], f'the refusal reason was dropped: {a}'


def test_the_model_is_told_its_action_was_refused(loop, monkeypatch):
    local_loop, backend, _ran, _guard, lct = loop
    _refuse_everything(monkeypatch, lct)
    _run(local_loop, 'u-told')
    assert len(backend.prompts) >= 2, 'the loop never asked the model twice'
    assert CAP in backend.prompts[1], (
        "iteration 2 was not told iteration 1's action was refused:\n"
        + backend.prompts[1][:800])


def test_repeated_refusals_end_the_run(loop, monkeypatch):
    local_loop, _backend, _ran, _guard, lct = loop
    _refuse_everything(monkeypatch, lct)
    out = _run(local_loop, 'u-bail')
    assert out['exit_reason'] == 'action_error', (
        "three refusals in a row must end the run honestly, not spend the "
        f"whole budget: exit_reason={out['exit_reason']!r}")


def test_the_ai_control_ribbon_is_up_while_the_loop_acts(loop, monkeypatch):
    local_loop, _backend, _ran, _guard, lct = loop
    import core.config_cache as cc
    import core.http_pool as hp
    events = []
    monkeypatch.setattr(cc, 'is_bundled', lambda: True)
    monkeypatch.setattr(hp, 'pooled_get',
                        lambda url, **kw: events.append(url.rsplit('/', 2)[-2]
                                                        + '/' + url.rsplit('/', 1)[-1]))

    def _act(action):
        events.append('ACTION')
        return {'output': 'Free 24.2 GB'}

    monkeypatch.setattr(lct, '_execute_inprocess', _act)
    _run(local_loop, 'u-ribbon')
    assert 'indicator/show' in events, (
        'the loop never asked Nunba to show the AI-control ribbon')
    assert 'ACTION' in events, 'no action ran'
    assert events.index('indicator/show') < events.index('ACTION'), (
        f'the ribbon was not up before the first action: {events}')
    assert events[-1] == 'indicator/hide', (
        f'the ribbon was left up after the loop ended: {events}')


def test_no_ribbon_call_outside_nunba(loop, monkeypatch):
    """Standalone HARTOS has no ribbon: no call, no 4 s refused connect."""
    local_loop, _backend, _ran, _guard, _lct = loop
    import core.config_cache as cc
    import core.http_pool as hp
    calls = []
    monkeypatch.setattr(cc, 'is_bundled', lambda: False)
    monkeypatch.setattr(hp, 'pooled_get', lambda url, **kw: calls.append(url))
    _run(local_loop, 'u-standalone')
    assert not any('/indicator/' in u for u in calls), calls
