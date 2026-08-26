"""The supervisor is the C166 producer: it re-seeds the child's
drain-on-restart goal queue.

hevolveai's qwen_distillation_engine keeps its goal queue in memory BY
DESIGN (C166: "every producer re-seeds -- daemons re-POST /v1/goals/seed
on their own cadence") -- but no producer existed in any repo, so every
child restart (2026-08-26 sawtooth: one ~45min) silently wiped the queue
and the engine fell back to synthetic self-queries (#687).
run_goal_seed_loop is that missing producer.  Same injected-callback
style as test_commit_ceiling_loop -- no threads, no DB, no HTTP.
"""
from integrations.agent_engine.hevolveai_supervisor import run_goal_seed_loop

GOALS = [('grow the newsletter: draft 3 subject lines', 2),
         ('summarize yesterday\'s support threads', 1)]


def _stop_after(n_ticks):
    ticks = {'n': 0}

    def stop():
        ticks['n'] += 1
        return ticks['n'] > n_ticks

    return stop


def test_posts_every_active_goal_each_tick():
    """Re-posting every tick is the design: dedup lives child-side, and a
    consumed goal must be re-fed while its row stays active."""
    posted = []
    run_goal_seed_loop(lambda: list(GOALS), lambda t, p: posted.append((t, p)),
                       sleep=lambda: None, stop=_stop_after(2))
    assert posted == GOALS + GOALS


def test_child_down_abandons_tick_then_refills_after_restart():
    """First failing POST abandons the whole tick (no 5s-timeout march
    through the rest); the next tick re-seeds everything -- this IS the
    restart-refill behavior the sawtooth kills need."""
    calls = []
    state = {'up': False}

    def post(text, priority):
        calls.append(text)
        if not state['up']:
            raise ConnectionError('child booting')

    def sleep():
        state['up'] = True  # child comes up between tick 1 and tick 2

    run_goal_seed_loop(lambda: list(GOALS), post, sleep, _stop_after(2))
    # tick 1: first POST raises, second goal never attempted; tick 2: both.
    assert calls == [GOALS[0][0], GOALS[0][0], GOALS[1][0]]


def test_raising_get_goals_survives_to_next_tick():
    calls = {'n': 0}

    def get_goals():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('db busy')
        return list(GOALS)

    posted = []
    run_goal_seed_loop(get_goals, lambda t, p: posted.append(t),
                       sleep=lambda: None, stop=_stop_after(2))
    assert posted == [g[0] for g in GOALS]


def test_stopped_loop_does_no_work():
    posted = []
    run_goal_seed_loop(lambda: list(GOALS), lambda t, p: posted.append(t),
                       sleep=lambda: None, stop=lambda: True)
    assert posted == []
