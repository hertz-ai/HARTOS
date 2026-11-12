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
OWNER = '10202'

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

    # The creator's approval card (person_ask) and the per-person hold store.
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path / 'data'))
    from integrations.agent_engine import person_ask
    asks = SimpleNamespace(sent=[], state={'status': 'open'}, filed=True)

    def ask(user_id, agent_id, component, *, prompt_id=None, background=False, **_k):
        asks.sent.append((user_id, component, background))
        if not asks.filed:      # the daemon tick's database lock (hartos-16)
            return {'ok': False, 'ask_id': None, 'refused': 'not_filed', 'card': False}
        return {'ok': True, 'ask_id': f'ask-{len(asks.sent)}', 'refused': None, 'card': True}
    monkeypatch.setattr(person_ask, 'ask', ask)
    monkeypatch.setattr(person_ask, 'ask_state', lambda ask_id, user_id: dict(asks.state))
    monkeypatch.setattr(person_ask, 'person_to_ask',
                        lambda user_id, request_id=None: OWNER if str(user_id).startswith('daemon')
                        else str(user_id))
    state.asks = asks

    def call(user=USER, pid=PID, background=False):
        return hie._autonomous_gather_info(
            user, 'Create an agent named "Syllable Coach"', pid, background=background)

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


LONGER = CREATOR + ['save_data_in_memory key last_attempt value the attempt',
                    'save_data_in_memory key exercise_result value practised',
                    'send_message_to_user: say goodbye to Arjun']
ROUNDS = [CREATOR,
          [a.replace('warm feedback', 'kind feedback') for a in CREATOR],
          [a.replace('warm feedback', 'gentle feedback') for a in CREATOR]]
REFINEMENT = [a.replace('warm feedback', 'short warm feedback') for a in CREATOR]
APPROVE = {'status': 'answered', 'answer': {'decision': 'approve'}}


def _oscillate(run):
    run.answers = [_plan('proposed_plan', CREATOR[:2]), _plan('proposed_plan', LONGER)]
    run.verdicts = [('needs_refinement', 'add the remaining steps')]


def _held(run, pid=PID):
    """The plan waiting on its creator's card, and its hold."""
    from core.prompt_files import staged_plan
    staged = staged_plan(str(run.saved_path.parent), pid) or {}
    return staged, staged.get('hold')


def _approve_and_build(run, expected):
    """The creator approves on the card; their next create turn builds the
    staged plan itself, with no planner call (no re-emission)."""
    from core.prompt_files import held_plan
    sent_before = list(run.sent)
    run.asks.state = APPROVE
    saved = run.go()
    assert _actions(saved) == expected
    assert saved['status'] == 'completed' and 'review_required' not in saved
    assert 'hold' not in saved
    assert run.sent == sent_before, 'the approved plan was re-asked of the planner'
    assert _held(run)[1] is None and held_plan(USER) is None
    return saved


def test_an_oscillating_plan_waits_for_its_creators_approval(run):
    """The oscillation guard saved the newest plan as it stood, approved by
    nobody (live 11-07, agent 1794031134533).  Now it waits on one card."""
    _oscillate(run)
    reply = run.call()
    assert 'waits for your approval' in reply and 'Syllable Coach' in reply
    assert not run.saved_path.exists(), 'an unapproved plan was saved'
    staged, hold = _held(run)
    assert _actions(staged) == LONGER and hold['ask_id'] == 'ask-1'
    assert [(who, card['type'], bg) for who, card, bg in run.asks.sent] == [
        (USER, 'approval', False)]
    assert run.asks.sent[0][1]['action'] == f'approve_plan:{PID}'
    # Never reviewed: still staged as its own round, with its ids.
    assert _actions(run.staged(1)) == LONGER
    assert (run.staged(1)['prompt_id'], run.staged(1)['creator_user_id']) == (PID, USER)
    _approve_and_build(run, LONGER)


def test_the_plan_whose_review_could_not_finish_waits_on_the_card(run):
    """The plan the loop handed to a "peer reviewer" (the auto-reviewer's
    model down) was returned with a free-text "approved" request nothing
    read; it waits on the card like the others (hartos-16, 11-07)."""
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [RuntimeError('the reviewer model is down')]
    reply = run.call()
    assert 'waits for your approval' in reply and 'approve it on the card' in reply
    assert 'Reply with "approved"' not in reply
    staged, hold = _held(run)
    assert _actions(staged) == CREATOR and hold['why'] == 'the review could not finish'
    assert (staged['prompt_id'], staged['creator_user_id']) == (PID, USER)
    assert not run.saved_path.exists()
    _approve_and_build(run, CREATOR)


def test_three_rounds_without_agreement_hold_the_refinement_that_answered_them(run):
    assert REFINEMENT != ROUNDS[2]
    run.answers = [_plan('proposed_plan', r) for r in ROUNDS] + [
        _plan('proposed_plan', REFINEMENT)]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    run.call()
    assert not run.saved_path.exists()
    assert _actions(_held(run)[0]) == REFINEMENT
    assert _asked_to_approve(run) == []
    # Never reviewed: staged as its own round (review of cdeca7500, M6).
    assert _actions(run.staged(3)) == REFINEMENT
    assert (run.staged(3)['prompt_id'], run.staged(3)['creator_user_id']) == (PID, USER)
    assert _actions(run.staged(2)) == ROUNDS[2]
    _approve_and_build(run, REFINEMENT)


def test_a_refinement_the_model_marked_completed_waits_like_any_other(run):
    """Review of cdeca7500, F1: a round-3 refinement marked 'completed' is
    the newest plan whatever status the model gave it; it waits on the card."""
    run.answers = [_plan('proposed_plan', r) for r in ROUNDS] + [
        _plan('completed', REFINEMENT)]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    run.call()
    assert not run.saved_path.exists()
    assert _actions(_held(run)[0]) == REFINEMENT
    assert _asked_to_approve(run) == []
    _approve_and_build(run, REFINEMENT)


def _model_down():
    """What the real gather_info returns when its model fails: a sentence,
    not an exception (review of 7e130b0df, must-fix 1, P2a)."""
    from core.agent_tools import is_user_facing_error, user_facing_error
    sentence = user_facing_error(RuntimeError('llama-server :8080 refused'))
    assert is_user_facing_error(sentence)
    return sentence


@pytest.mark.parametrize('failure', ['returned', 'raised'])
def test_a_refinement_that_fails_leaves_the_rejected_plan_waiting(run, failure):
    """Review of a88258c98, F-c, and of 7e130b0df, must-fix 1: a failed
    round-3 refinement, returned as the real gather_info returns it or
    raised, never counts as a round, never saves the plan the reviewer had
    just rejected, and never stages it as never reviewed."""
    down = _model_down() if failure == 'returned' else RuntimeError('the planner model is down')
    run.answers = [_plan('proposed_plan', r) for r in ROUNDS] + [down]
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    reply = run.call()
    assert not run.saved_path.exists(), 'the plan the reviewer rejected was saved'
    assert not run.staged_exists(3), 'a rejected plan was staged as never reviewed'
    assert 'waits for your approval' in reply
    assert _actions(_held(run)[0]) == ROUNDS[2]
    assert _asked_to_approve(run) == []
    assert 'Agent details gathered' not in reply


class _ModelProxy:
    """autogen.UserProxyAgent stand-in for the REAL gather_info: send() is the
    model round trip, and an exception in the script is the endpoint failing
    inside it (the shape of the review of 7e130b0df, P2a)."""

    def __init__(self, script):
        self.script, self.sent = list(script), []
        self.chat_messages = {'assistant': []}

    def send(self, message, recipient, request_reply=True):
        self.sent.append(message)
        nxt = self.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        self.chat_messages['assistant'].append({'role': 'assistant', 'content': nxt})


@pytest.fixture
def real_gather(run, hie, monkeypatch):
    """The real hartos.gather_agentdetails.gather_info, its autogen agents
    faked, so a model failure comes back the way it does live: a sentence.
    ``run`` put a stub in sys.modules under the planner's name; it is taken
    out so the real module is the one imported (and put back after)."""
    import importlib
    monkeypatch.delitem(sys.modules, 'hartos.gather_agentdetails')
    gad = importlib.import_module('hartos.gather_agentdetails')
    assert hasattr(gad, 'user_agents'), 'the planner stub, not the planner'
    monkeypatch.setattr(gad, 'autogen', SimpleNamespace(AssistantAgent=object,
                                                        UserProxyAgent=object))
    monkeypatch.setitem(sys.modules, 'hartos.create_recipe',
                        SimpleNamespace(_push_thinking=lambda *a, **k: None))

    def go(script):
        proxy = _ModelProxy(script)
        monkeypatch.setitem(gad.user_agents, f'{USER}_{PID}', (object(), proxy))
        with hie.app.app_context():
            reply = hie._autonomous_gather_info(USER, 'Create an agent named "Syllable Coach"', PID)
        return reply, proxy
    run.go_real = go
    return run


@pytest.mark.parametrize('down_for', [1, 10])
def test_the_real_planner_failing_on_round_three_saves_nothing(real_gather, down_for):
    """Review of 7e130b0df, P2a/P2b, with the real gather_info: its model
    refusing the round-3 refinement returned "I couldn't finish that: ...",
    which was counted as round 3 and let the escalation save the plan the
    reviewer had just rejected ("Agent details gathered autonomously")."""
    run = real_gather
    run.verdicts = [('needs_refinement', 'tighten the feedback step')] * 3
    script = [_plan('proposed_plan', r) for r in ROUNDS] + (
        [ConnectionRefusedError('llama-server :8080 refused')] * down_for)
    reply, proxy = run.go_real(script)
    assert not run.saved_path.exists(), 'the plan the reviewer rejected was saved'
    assert 'Agent details gathered' not in reply and 'waits for your approval' in reply
    assert _actions(_held(run)[0]) == ROUNDS[2]
    assert len(proxy.sent) == 4, 'the loop kept calling a model that is down'


def test_a_self_marked_completed_plan_that_was_rejected_is_never_saved(run):
    """Review of 7e130b0df, must-fix 2 (P1a/P1b): the model marks its first
    plan 'completed', the reviewer rejects it, the refinement fails.  The
    relabel lived only in a local, so `response` still said 'completed' and
    the final parse saved the rejected plan while the log said "not saved"."""
    run.answers = [_plan('completed', CREATOR), _model_down()]
    run.verdicts = [('needs_refinement', 'reorder the steps')]
    reply = run.call()
    assert not run.saved_path.exists(), 'the rejected plan was saved'
    assert 'waits for your approval' in reply
    staged = _held(run)[0]
    assert _actions(staged) == CREATOR and staged['status'] == 'proposed_plan'


# -- the card's answers --------------------------------------------------------

@pytest.mark.parametrize('answer', [
    {'status': 'answered', 'answer': {'decision': 'deny'}},
    {'status': 'dismissed', 'answer': None}])
def test_deny_drops_the_plan_and_names_it(run, answer):
    from core.prompt_files import held_plan
    _oscillate(run)
    run.call()
    run.asks.state = answer
    reply = run.call()
    assert reply.startswith('Dropped the plan for Syllable Coach')
    assert not run.saved_path.exists()
    assert _held(run)[1] is None and held_plan(USER) is None


def test_while_the_card_is_open_the_creator_is_told_and_asked_nothing_more(run):
    """'Later' on a client closes its card and sends nothing, so the ask
    stays open: two more turns say it waits, with no second card."""
    _oscillate(run)
    run.call()
    for _ in range(2):
        reply = run.call()
        assert 'waiting on the card' in reply and 'Syllable Coach' in reply
    assert len(run.asks.sent) == 1 and not run.saved_path.exists()


def test_an_answer_no_longer_kept_is_asked_again_never_built(run):
    _oscillate(run)
    run.call()
    run.asks.state = {'status': 'expired', 'answer': None}
    reply = run.call()
    assert 'waits for your approval' in reply
    assert len(run.asks.sent) == 2 and _held(run)[1]['ask_id'] == 'ask-2'
    assert not run.saved_path.exists()


def test_an_ask_that_could_not_be_filed_still_holds_and_asks_again(run):
    run.asks.filed = False
    _oscillate(run)
    run.call()
    assert not run.saved_path.exists() and _held(run)[1]['ask_id'] is None
    run.asks.filed = True
    run.call()
    assert len(run.asks.sent) == 2 and _held(run)[1]['ask_id'] == 'ask-2'
    assert not run.saved_path.exists()


# -- whose hold, under which id ------------------------------------------------

def test_a_persons_next_create_with_a_new_id_finds_their_waiting_plan(run, hie):
    _oscillate(run)
    run.call()
    assert hie._creation_prompt_id(USER, 999) == PID
    assert hie._creation_prompt_id('u-someone-else', 999) == 999
    run.asks.state = {'status': 'answered', 'answer': {'decision': 'deny'}}
    run.call()
    assert hie._creation_prompt_id(USER, 999) == 999


def test_a_hold_whose_plan_no_longer_waits_is_cleared_not_followed(run, hie):
    from core.prompt_files import held_plan, proposed_plan_filename
    _oscillate(run)
    run.call()
    os.remove(run.saved_path.parent / proposed_plan_filename(PID))
    assert hie._creation_prompt_id(USER, 999) == 999 and held_plan(USER) is None


def test_a_background_hold_is_kept_by_its_goal_never_by_the_persona(run, hie):
    """One daemon persona serves many goals, and the card goes to the owner:
    neither the persona nor the owner gets a person hold (hartos-16)."""
    from core.prompt_files import held_plan
    _oscillate(run)
    run.call(user='daemon-persona', background=True)
    assert [(who, bg) for who, _c, bg in run.asks.sent] == [(OWNER, True)]
    assert held_plan('daemon-persona') is None and held_plan(OWNER) is None
    assert hie._creation_prompt_id(OWNER, 555) == 555
    assert _held(run)[1]['asked'] == OWNER


def test_goal_b_on_the_same_persona_runs_fresh_beside_goal_as_hold(run):
    other = PID + 1
    _oscillate(run)
    run.call(user='daemon-persona', background=True)
    run.answers = [_plan('proposed_plan', CREATOR)]
    run.verdicts = [('approved', '')]
    run.call(user='daemon-persona', pid=other, background=True)
    assert (run.saved_path.parent / f'{other}.json').exists()
    assert not run.saved_path.exists() and _held(run)[1]['ask_id'] == 'ask-1'


def test_the_background_create_keeps_its_goals_own_id(hie):
    """The daemon/API create (its goal's deterministic id) is never routed
    through the person hold: a source guard on its one call."""
    import inspect
    import re
    src = inspect.getsource(hie)
    assert re.search(r"_autonomous_gather_info\(user_id, prompt, prompt_id,\s*background=True\)", src)
    assert not re.search(r"\bprompt_id = _creation_prompt_id\(", src)


def test_flows_written_as_text_never_read_as_an_oscillation():
    """Review of 494d5a4e1, Y2: flows the model wrote as JSON text counted 0
    actions, so a format slip read as a swing and would cost a card."""
    from core.plan_oscillation import is_plan_oscillating, plan_action_count
    as_list = {'flows': [{'actions': ['a'] * 9}]}
    as_text = {'flows': json.dumps(as_list['flows'])}
    assert plan_action_count(as_text) is None
    assert not is_plan_oscillating(plan_action_count(as_list), plan_action_count(as_text))


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
