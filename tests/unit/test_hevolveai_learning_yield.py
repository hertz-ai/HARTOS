"""#687 — hevolveai background learning must yield to Nunba user chat.

Live 2026-08-23 (boot 11:33): the child's synthetic distillation loop ran
108 teacher queries in 40 min against the SAME llama :8080 that serves
user chat; at 11:53 a real user turn was starved into the Nunba
bare-llama fallback (zero dispatcher lines).  The engine's own pause
machinery (learning_llm_provider._pause_all_background_learning) fires
only around hevolveai's OWN /v1/chat/completions — Nunba turns are
invisible to it.

Fix under test: the supervisor polls the canonical
dispatch.should_yield_to_user gate (#505) and posts
/v1/learning/pause|resume to the child on TRANSITIONS only.

    python -m pytest tests/unit/test_hevolveai_learning_yield.py --noconftest -q
"""
from pathlib import Path

from integrations.agent_engine.hevolveai_supervisor import run_learning_yield_loop

_SUP_SRC = (Path(__file__).resolve().parents[2] / 'integrations' /
            'agent_engine' / 'hevolveai_supervisor.py').read_text(encoding='utf-8')


def _drive(gate_values, post_effects=None):
    """Run the loop over a scripted gate sequence; record posts.

    post_effects: optional list aligned with post CALLS — an Exception
    instance means that call raises.
    """
    gates = iter(gate_values)
    posts, effects = [], list(post_effects or [])

    def gate():
        return next(gates)

    def post(action):
        posts.append(action)
        if effects:
            eff = effects.pop(0)
            if isinstance(eff, Exception):
                raise eff

    remaining = {'n': len(gate_values)}

    def stop():
        return remaining['n'] <= 0

    def sleep():
        remaining['n'] -= 1

    run_learning_yield_loop(gate, post, sleep, stop)
    return posts


def test_yield_loop_posts_on_transitions_only():
    posts = _drive([False, True, True, True, False, False])
    assert posts == ['pause', 'resume'], (
        "the poller must post only on gate TRANSITIONS — one pause when the "
        "user becomes active, one resume when they go idle; per-tick posts "
        "would hammer the child exactly like the loop it is throttling")


def test_pause_post_failure_retries_next_tick():
    # child still booting: first pause POST raises; the poller must retry
    # on the next tick instead of believing the child is paused.
    posts = _drive([True, True, False],
                   post_effects=[ConnectionError('booting'), None, None])
    assert posts == ['pause', 'pause', 'resume']


def test_gate_error_fails_open():
    gates = iter([RuntimeError('gate boom'), False])
    posts = []

    def gate():
        v = next(gates)
        if isinstance(v, Exception):
            raise v
        return v

    remaining = {'n': 2}
    run_learning_yield_loop(
        gate, posts.append, lambda: remaining.__setitem__('n', remaining['n'] - 1),
        lambda: remaining['n'] <= 0)
    assert posts == [], "a raising gate must fail OPEN (treat as not-yielding)"


def test_supervisor_start_wires_poller():
    """start() must arm the poller on BOTH exits — spawned child AND the
    operator-managed-port branch (that server grinds the shared llama
    just the same)."""
    assert _SUP_SRC.count('_start_learning_yield_poller()') >= 2, (
        "supervisor.start() does not arm the learning-yield poller on "
        "both exits — the synthetic loop keeps competing with user chat")
