"""An autonomous creation saves the plan that was approved, as it was approved.

Measured 2026-11-02 (hartos-c9), Syllable Coach 1793359802508 on the owner's
desktop, all written 30 Oct 17:00: the three staged review rounds
(prompts/1793359802508.proposed.r0/r1/r2.json) keep the creator's order, "greet
Arjun, say ball slowly, ask him to say ball" and THEN "get_chat_history to read
Arjun's latest attempt"; the saved config (1793359802508.json, 17:00:33) has the
read first and both steps reworded.  _autonomous_gather_info finalized an
approved plan by sending 'approved' to gather_info and saving whatever the model
re-emitted, at three sites: the reviewer's approval, the oscillation
force-approve and the escalation after three rounds.  The re-emission is a new
LLM answer, so the saved agent was not the plan anyone reviewed, and its reuse
read the child's attempt before asking for it.

Now each site finalizes the plan in code (status 'completed', review_required
dropped).  The escalation saves the refinement that answered the last feedback,
staged as its own round; only when nothing parses as a plan does it still ask
gather_info, as before.

    python -m pytest tests/unit/test_the_approved_plan_is_the_saved_plan.py -q
"""
import json
import os
import sys
import tempfile

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')
os.environ.setdefault('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.prompt_files import proposed_plan_filename  # noqa: E402

USER = 'u-plan'
PID = 1793359802599

ASK = 'send_message_to_user: greet Arjun, say ball slowly, ask him to say ball'
READ = "get_chat_history to read Arjun's latest attempt"
CREATOR = ['save_data_in_memory key target_word value ball', ASK, READ,
           'send_message_to_user: warm feedback on the attempt']
# What the model wrote when asked to re-emit the approved plan on 30 Oct: the
# read moved ahead of the ask and both reworded.
REWRITTEN = ['save_data_in_memory key target_word value ball',
             "get_chat_history to retrieve Arjun's latest attempt text",
             'send_message_to_user: greet Arjun, say ball slowly, and ask him to say ball',
             'send_message_to_user: warm feedback on the attempt']


def _plan(status, actions, **extra):
    plan = {'status': status, 'name': 'Syllable Coach', 'agent_name': 'syllable.coach',
            'goal': 'Arjun says ball', 'broadcast_agent': False,
            'personas': [{'name': 'Speech Companion', 'description': 'speaks to Arjun'}],
            'flows': [{'flow_name': 'Syllable exercise ball', 'persona': 'Speech Companion',
                       'actions': list(actions), 'sub_goal': 'Arjun says ball'}]}
    if status == 'proposed_plan':
        plan['review_required'] = True
    plan.update(extra)
    return json.dumps(plan)


@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


@pytest.fixture
def run(hie, tmp_path, monkeypatch):
    """Drive the real _autonomous_gather_info.  ``answers`` are gather_info's
    replies in order; any message that starts with 'approved' gets the
    model's re-emission (REWRITTEN, status completed), as on 30 Oct.
    ``verdicts`` are the reviewer's, in order."""
    monkeypatch.setattr(hie, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(hie, 'pooled_post', MagicMock(name='pooled_post'))
    state = SimpleNamespace(answers=[], verdicts=[], sent=[], reviewed=[])

    def gather_info(user_id, message, prompt_id, autonomous=False):
        state.sent.append(message)
        if str(message).startswith('approved'):
            return _plan('completed', REWRITTEN)
        # An exception in ``answers`` is the planner's model failing.
        answer = state.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setitem(sys.modules, 'hartos.gather_agentdetails',
                        SimpleNamespace(gather_info=gather_info))

    def review(plan, remaining):
        # Every plan the reviewer is shown, in order (its steps, or the plan
        # itself when it has none to read).  An exception in ``verdicts`` is
        # the reviewer's model failing.
        try:
            state.reviewed.append(_actions(plan))
        except (KeyError, IndexError, TypeError):
            state.reviewed.append(plan)
        verdict = state.verdicts.pop(0)
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict
    monkeypatch.setattr(hie, '_review_proposed_plan', review)

    def call():
        return hie._autonomous_gather_info(
            USER, 'Create an agent named "Syllable Coach"', PID)

    def go():
        call()
        with open(tmp_path / f'{PID}.json', encoding='utf-8') as f:
            return json.load(f)

    state.call, state.go = call, go
    state.saved_path = tmp_path / f'{PID}.json'
    state.staged_exists = lambda n: (tmp_path / proposed_plan_filename(PID, n)).exists()
    # ``n`` None is the plan handed to a peer reviewer (the unnumbered file).
    state.staged = lambda n: json.loads(
        (tmp_path / proposed_plan_filename(PID, n)).read_text(encoding='utf-8'))
    return state


def _actions(config):
    return config['flows'][0]['actions']


def _asked_to_approve(state):
    return [m for m in state.sent if str(m).startswith('approved')]


def test_the_reviewers_approval_saves_the_plan_it_approved(run):
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [('approved', '')]
    saved = run.go()
    assert _actions(saved) == CREATOR, (
        'the saved agent is the model re-emitting the approved plan, not the '
        'plan the reviewer approved: the read moved ahead of the ask')
    assert saved['status'] == 'completed'
    assert 'review_required' not in saved
    assert (saved['prompt_id'], saved['creator_user_id']) == (PID, USER)
    assert _asked_to_approve(run) == []


def test_the_oscillation_force_approve_saves_the_newest_plan_as_it_stands(run):
    short = CREATOR[:2]
    longer = CREATOR + ['save_data_in_memory key last_attempt value the attempt',
                        'save_data_in_memory key exercise_result value practised',
                        'send_message_to_user: say goodbye to Arjun']
    run.answers = [_plan('proposed_plan', short), _plan('proposed_plan', longer)]
    run.verdicts = [('needs_refinement', 'add the remaining steps')]
    saved = run.go()
    assert _actions(saved) == longer
    assert saved['status'] == 'completed' and 'review_required' not in saved
    assert _asked_to_approve(run) == []
    # The force-approved plan was never reviewed; it is staged as its own
    # round, like the escalation's (review of cdeca7500, F4), with its ids
    # (review of a88258c98, F-a: dropping this site's stamps failed no test).
    staged = run.staged(1)
    assert _actions(staged) == longer
    assert (staged['prompt_id'], staged['creator_user_id']) == (PID, USER)


def test_the_plan_handed_to_a_peer_reviewer_is_staged_with_its_ids(run):
    """The fourth staging, the plan the loop hands to a peer reviewer (here
    because the auto-reviewer's model is down), goes through the one writer
    too (review of a88258c98, F-b): the unnumbered file, stamped, and no
    agent saved."""
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [RuntimeError('the reviewer model is down')]
    handed = json.loads(run.call())
    assert handed['status'] == 'proposed_plan'
    assert _actions(handed['plan']) == CREATOR
    staged = run.staged(None)
    assert _actions(staged) == CREATOR
    assert (staged['prompt_id'], staged['creator_user_id']) == (PID, USER)
    assert not run.saved_path.exists()


def test_the_escalation_saves_the_refinement_that_answered_the_last_feedback(run):
    rounds = [CREATOR,
              [a.replace('warm feedback', 'kind feedback') for a in CREATOR],
              [a.replace('warm feedback', 'gentle feedback') for a in CREATOR]]
    refinement = [a.replace('warm feedback', 'short warm feedback') for a in CREATOR]
    assert refinement != rounds[2]
    run.answers = [_plan('proposed_plan', r) for r in rounds] + [
        _plan('proposed_plan', refinement)]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    saved = run.go()
    assert _actions(saved) == refinement, (
        'the escalation saved something other than the newest plan')
    assert saved['status'] == 'completed' and 'review_required' not in saved
    assert _asked_to_approve(run) == []
    # The saved plan was never reviewed; it is staged as its own round,
    # stamped like every other round (review of cdeca7500, mutant M6).
    staged = run.staged(3)
    assert _actions(staged) == refinement
    assert (staged['prompt_id'], staged['creator_user_id']) == (PID, USER)
    assert _actions(run.staged(2)) == rounds[2]


def test_a_refinement_the_model_marked_completed_is_saved_as_it_stands(run):
    """Review of cdeca7500, F1 (ran by its reviewer): when the round-3
    refinement comes back with status 'completed', the escalation still
    sent 'approved (escalated...)' to gather_info and saved the
    re-emission.  The newest plan is final whichever status the model gave
    it."""
    rounds = [CREATOR,
              [a.replace('warm feedback', 'kind feedback') for a in CREATOR],
              [a.replace('warm feedback', 'gentle feedback') for a in CREATOR]]
    refinement = [a.replace('warm feedback', 'short warm feedback') for a in CREATOR]
    run.answers = [_plan('proposed_plan', r) for r in rounds] + [
        _plan('completed', refinement)]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    saved = run.go()
    assert _actions(saved) == refinement
    assert _asked_to_approve(run) == []
    assert _actions(run.staged(3)) == refinement


def test_a_refinement_that_fails_leaves_the_rejected_plan_proposed(run):
    """Review of a88258c98, F-c: the round-3 refinement call fails (the
    model down).  The round was counted before the call, so the escalation
    saved the plan the reviewer had just rejected, staged as the "never
    reviewed" newest.  Now no agent is saved: the rejected plan goes back as
    a proposed plan for a peer reviewer, as on any other break."""
    rounds = [CREATOR,
              [a.replace('warm feedback', 'kind feedback') for a in CREATOR],
              [a.replace('warm feedback', 'gentle feedback') for a in CREATOR]]
    run.answers = [_plan('proposed_plan', r) for r in rounds] + [
        RuntimeError('the planner model is down')]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    result = run.call()
    assert not run.saved_path.exists(), 'the plan the reviewer rejected was saved'
    assert not run.staged_exists(3), 'a rejected plan was staged as never reviewed'
    handed = json.loads(result)
    assert handed['status'] == 'proposed_plan'
    assert _actions(handed['plan']) == rounds[2]
    assert _actions(run.staged(None)) == rounds[2]
    assert _asked_to_approve(run) == []


def test_a_first_answer_the_model_marked_completed_is_reviewed(run):
    """Review of a88258c98, F-d: the autonomous system message's STAGE 1
    has the first call emit a plan for review, so a 'completed' first
    answer is the model skipping review; it was saved unreviewed and
    unstaged.  It goes to the reviewer once (one reviewer call more per
    such creation), is staged as round 0, and the approved plan is
    saved."""
    run.answers = [_plan('completed', CREATOR)]
    run.verdicts = [('approved', '')]
    saved = run.go()
    assert run.reviewed == [CREATOR], 'the plan was not reviewed exactly once'
    assert _actions(saved) == CREATOR
    assert _actions(run.staged(0)) == CREATOR


def test_a_refinement_the_model_marked_completed_is_reviewed_before_rounds_end(run):
    """Review of a88258c98, F-d: a refinement marked 'completed' at round 1
    was saved unreviewed.  It is reviewed like any plan; rejected, it is
    refined again, and the plan saved is the one approved."""
    kind = [a.replace('warm feedback', 'kind feedback') for a in CREATOR]
    gentle = [a.replace('warm feedback', 'gentle feedback') for a in CREATOR]
    run.answers = [_plan('proposed_plan', CREATOR), _plan('completed', kind),
                   _plan('proposed_plan', gentle)]
    run.verdicts = [('needs_refinement', 'kinder feedback'),
                    ('needs_refinement', 'gentler feedback'), ('approved', '')]
    saved = run.go()
    assert run.reviewed == [CREATOR, kind, gentle]
    assert _actions(saved) == gentle
    assert _actions(run.staged(1)) == kind


def test_the_approved_plan_keeps_letters_outside_ascii(run):
    """The reviewer's probe: letters outside ASCII in the steps reach the
    saved file unchanged (a coach that says the word in the child's
    language)."""
    steps = ['send_message_to_user: say "பந்து" (ball) slowly and ask Arjun to say it',
             "get_chat_history to read Arjun's attempt at பந்து",
             'send_message_to_user: warm feedback, très bien']
    run.answers = [_plan('proposed_plan', steps)]
    run.verdicts = [('approved', '')]
    assert _actions(run.go()) == steps


def test_the_plan_keeps_its_own_words_true_and_false(run):
    """Review of a88258c98, F-e: every gather parse capitalised JSON's
    true/false blindly, so the plan's own words changed too ("construe"
    became "consTrue"; 406 of 2,328 saved configs on the owner's desktop
    came back with words changed).  A reply that is valid JSON is taken as
    the model wrote it (_parse_plan_reply)."""
    steps = ['send_message_to_user: ask Arjun whether "the ball is red" is true or false',
             'save_data_in_memory key how_to_construe value the answer as Arjun says it',
             "get_chat_history to read Arjun's answer",
             'send_message_to_user: warm feedback, never a falsehood']
    run.answers = [_plan('proposed_plan', steps)]
    run.verdicts = [('approved', '')]
    saved = run.go()
    assert _actions(saved) == steps
    assert saved['broadcast_agent'] is False


def test_the_interactive_reply_keeps_its_own_words_too(hie):
    """_read_gather_config, the interactive reply and the model's second
    answer (#205), reads through the same parser."""
    steps = ['send_message_to_user: ask Arjun whether the ball is red, true or false',
             'save_data_in_memory key how_to_construe value his answer']
    cfg = hie._read_gather_config(_plan('completed', steps), PID)
    assert _actions(cfg) == steps


def test_a_plan_written_as_json_keeps_its_quotes_and_dashes(run):
    """The cost of taking a valid JSON reply as written, chosen with
    hartos-c9 (review of a88258c98, F-e): curly quotes and dashes stay as
    the model wrote them; only a reply that is not valid JSON goes through
    helper.retrieve_json, which flattens them to ASCII."""
    steps = ['send_message_to_user: say “ball” slowly — then wait',
             "get_chat_history to read Arjun’s attempt"]
    run.answers = [_plan('proposed_plan', steps)]
    run.verdicts = [('approved', '')]
    assert _actions(run.go()) == steps


def test_a_reply_written_as_a_python_dict_is_still_read(run):
    """A reply that is not valid JSON is read as before: True/False and
    single quotes through helper.retrieve_json."""
    reply = repr(json.loads(_plan('proposed_plan', CREATOR)))
    assert "'review_required': True" in reply
    run.answers = [reply]
    run.verdicts = [('approved', '')]
    assert _actions(run.go()) == CREATOR


def test_only_the_one_plan_parser_capitalises_true_and_false():
    """Source guard, beside the behaviour tests above: the blind true/false
    rewrite lives in _parse_plan_reply alone, so a seventh gather site
    cannot drift back to it (hartos-c9's condition on F-e)."""
    import ast
    path = os.path.join(os.path.dirname(__file__), '..', '..', 'hart_intelligence_entry.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    sites = []
    for top in tree.body:
        for node in ast.walk(top):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'replace' and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in ('true', 'false')):
                sites.append(getattr(top, 'name', type(top).__name__))
    assert sites == ['_parse_plan_reply', '_parse_plan_reply'], sites


def test_an_escalation_with_no_plan_to_save_still_asks_gather_as_before(run):
    run.answers = [_plan('proposed_plan', CREATOR)] * 3 + ['I could not refine it.']
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    saved = run.go()
    assert len(_asked_to_approve(run)) == 1
    assert _actions(saved) == REWRITTEN
