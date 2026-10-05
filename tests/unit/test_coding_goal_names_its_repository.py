"""A maintenance goal names what it works on; one that names nothing is not sent.

Coding goals name a repository; self-heal goals name a failure.

MEASURED 2026-10-05 on the live desktop (read-only DB, logs, workspace):

  * active goal ce3ff57e "Audit and Embed Hive Intelligence in All Repos"
    (seed bootstrap_hive_embedding_audit, config repo_url '') is dispatched
    by the coding daemon, last at 10:40:55 UTC;
  * _build_coding_prompt told it "You are working on the GitHub repository
    (branch main) ... Clone the repo, analyze the codebase, and make
    improvements", with an empty repository, and its plan included "Create
    and push commits to the repository";
  * the agent searched the web for "list repositories coding agent git
    hertz-ai/HARTOS" and cloned github.com/kotthoff/hartos (10-04 05:36) and
    github.com/aden-hive/hive (09-24 02:19) into the coding workspace:
    strangers' repositories, found by name.  No commit and no push happened
    (each reflog holds only its clone).

"Repositories created by the coding agent" has no source anywhere in the code
(no registry of them), and no code fills an empty repo_url.  The builder now
declines a coding goal that names no repository, the way the SEO builder
declines one with no repo ("paused until configured"), and the coding daemon
then leaves the agent for the next goal (that half is pinned by
test_coding_daemon_declined_goal_keeps_its_agent).

Same day, same shape: active goal 50f19807, the seed bootstrap_exception_watcher
(goal_type self_heal, config {mode: watch, continuous: true}), is dispatched
as "Exception: Unknown / Module: unknown / Occurrences: 0 ... Write a minimal
fix"; its banked recipe (59272372353_0_recipe.json) is the autonomous-gather
stub "Respond to user".  The watching itself runs in code
(agent_daemon -> SelfHealingDispatcher.check_and_dispatch, and
ExceptionWatcher.process_exceptions), whether or not this goal is sent.  The
self-heal builder now declines a goal that names no failure.
"""
import logging
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from integrations.agent_engine.goal_manager import GoalManager  # noqa: E402
from integrations.agent_engine.goal_seeding import (  # noqa: E402
    LOOPHOLE_REMEDIATION_MAP, SEED_BOOTSTRAP_GOALS)
from tests.unit.test_coding_daemon_declined_goal_keeps_its_agent import (  # noqa: E402
    _Goal, _fix_goal, _run_tick)


def _seed_goal(slug):
    seed = next(s for s in SEED_BOOTSTRAP_GOALS if s['slug'] == slug)
    return {'id': f'g-{slug}', 'goal_type': seed['goal_type'],
            'title': seed['title'], 'description': seed['description'],
            'config_json': dict(seed['config'])}


def test_the_live_seeds_name_no_repository_and_are_not_sent():
    for slug in ('bootstrap_hive_embedding_audit', 'bootstrap_coding_health'):
        assert GoalManager.build_prompt(_seed_goal(slug)) is None, slug


def test_the_remediation_templates_with_no_repository_are_not_sent():
    blank = [(k, t) for k, t in LOOPHOLE_REMEDIATION_MAP.items()
             if t['goal_type'] == 'coding'
             and not (t['config'].get('repo_url') or t['config'].get('repo_path'))]
    assert blank, 'no repo-less coding template left to check'
    for key, t in blank:
        goal = {'id': f'g-{key}', 'goal_type': 'coding', 'title': t['title'],
                'description': t['description'], 'config_json': dict(t['config'])}
        assert GoalManager.build_prompt(goal) is None, key


def test_a_goal_that_names_a_repository_url_is_sent():
    prompt = GoalManager.build_prompt({
        'id': 'g-url', 'goal_type': 'coding', 'title': 'Fix auth bug',
        'description': 'Authentication broken',
        'config_json': {'repo_url': 'owner/repo', 'repo_branch': 'main'}})
    assert prompt and 'owner/repo' in prompt


def test_a_goal_that_names_a_local_repository_is_sent_to_it():
    prompt = GoalManager.build_prompt({
        'id': 'g-path', 'goal_type': 'coding', 'title': 'Fix auth bug',
        'description': 'Authentication broken',
        'config_json': {'repo_path': 'C:/work/app'}})
    assert prompt and 'C:/work/app' in prompt
    assert 'GitHub repository  (' not in prompt, prompt[:400]
    assert 'Clone the repo' not in prompt, 'a local repository is not cloned'


def test_the_daemon_sends_the_named_goal_and_not_the_blank_one():
    blank = _Goal('blank', 'coding', {'repo_url': '', 'repo_branch': 'main',
                                      'mode': 'audit'})
    named = _Goal('named', 'coding', {'repo_url': 'owner/repo',
                                      'repo_branch': 'main'})
    dispatch = _run_tick([blank, named], [{'user_id': 'agent-1', 'username': 'a1'}])
    assert [c.args[2] for c in dispatch.call_args_list] == ['named']
    assert blank.last_dispatched_at is None


def test_the_decline_is_said_once_per_goal(caplog):
    goal = {'id': 'g-once', 'goal_type': 'coding', 'title': 'Blank repo goal',
            'description': 'd', 'config_json': {'repo_url': ''}}
    with caplog.at_level(logging.INFO):
        GoalManager.build_prompt(goal)
        GoalManager.build_prompt(goal)
    said = [r for r in caplog.records if 'names no repository' in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in said]


# ── self-heal goals name a failure ─────────────────────────────────────────

def test_the_exception_monitor_seed_names_no_failure_and_is_not_sent():
    seed = next(s for s in SEED_BOOTSTRAP_GOALS
                if s['slug'] == 'bootstrap_exception_watcher')
    goal = {'id': 'g-watch', 'goal_type': seed['goal_type'], 'title': seed['title'],
            'description': seed['description'], 'config_json': dict(seed['config'])}
    assert GoalManager.build_prompt(goal) is None


def test_a_self_heal_goal_that_names_its_failure_is_sent():
    prompt = GoalManager.build_prompt({
        'id': 'g-fix', 'goal_type': 'self_heal', 'title': 'Fix KeyError',
        'description': 'd',
        'config_json': {'exc_type': 'KeyError', 'source_module': 'mod_a',
                        'source_function': 'fn', 'occurrence_count': 3,
                        'sample_traceback': 'Traceback (most recent call last): ...'}})
    assert prompt and 'KeyError' in prompt and 'mod_a' in prompt


def test_a_self_heal_goal_named_by_its_category_alone_is_sent():
    """error_advice files tts.probe goals: a category and a backend."""
    prompt = GoalManager.build_prompt({
        'id': 'g-cat', 'goal_type': 'self_heal', 'title': 'Self-heal: tts.probe',
        'description': 'd',
        'config_json': {'category': 'tts.probe',
                        'context': {'backend': 'cosyvoice3'}}})
    assert prompt and 'repair_backend_venv' in prompt


def test_the_daemon_leaves_the_monitor_seed_and_sends_a_real_fix():
    monitor = _Goal('monitor', 'self_heal', {'mode': 'watch', 'continuous': True})
    fix = _fix_goal()
    dispatch = _run_tick([monitor, fix], [{'user_id': 'agent-1', 'username': 'a1'}])
    assert [c.args[2] for c in dispatch.call_args_list] == ['fix']
    assert monitor.last_dispatched_at is None
