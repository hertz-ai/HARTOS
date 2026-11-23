"""A hive task held on its person's open approval card costs nothing per tick.

Review of 6182b632a (Medium, measured): that commit stopped the settle gate
pausing a goal whose hive work waits on its person, but every daemon tick
still re-dispatched the goal, the re-dispatch released the held task
(_release_held_tasks), and the worker claimed it and ran a whole /chat turn
(worker_loop._tick -> _execute_task -> local_chat_dispatch) only to hold it
again.  Over 7200 s with the card never answered: 121 turns and 363 full
coordinator-ledger saves, the goal still active.  The parent 58419ffc2 gave
30 turns and 91 saves, then paused.

Now the re-dispatch keeps a task held for its person while the ask it waits
on is still open (dispatch.goal_plan_card_state), so nothing is
claimed, no turn runs and nothing is saved until the person answers.  A goal
that waits is still not paused, a real stall still pauses, and an answer
brings the task back on the tick it comes in.  A wait this node cannot
prove (a card filed on a peer, a REUSE step waiting on a chat reply), or a
card no longer open, is treated as on the parent: released, counted,
paused at 1800 s (reviews of 4ecd81b1d and 5d578624a).

Behavioural: the real dispatch.dispatch_goal_distributed,
DistributedTaskCoordinator over a real SmartLedger (in memory) and
InMemoryTaskLock, DistributedWorkerLoop._tick, the settle gate
(agent_daemon._settle_dispatched_goal) and the real person_ask store.
Replaced at the boundaries only: the /chat turn (local_chat_dispatch,
playing hart_intelligence_entry._held_plan_turn over the real staged-plan
file and person_ask), the ask's card push and bell row, the goal row and DB
session, the node identity, the yield / provider / guardrail gates and the
ledger's clock.

    python -m pytest tests/unit/test_a_hive_task_waiting_on_an_open_card_costs_nothing_per_tick.py -q
"""
import contextlib
import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')

from agent_ledger import core as ledger_core  # noqa: E402
from agent_ledger.core import SmartLedger, TaskStatus  # noqa: E402
from core.agent_tools import user_facing_error  # noqa: E402
from core.constants import WAITING_FOR_PERSON_REPLY_PREFIX  # noqa: E402
from core.prompt_files import proposed_plan_filename, staged_plan  # noqa: E402
from integrations.agent_engine import agent_daemon, dispatch, person_ask  # noqa: E402
from integrations.distributed_agent import task_coordinator as tc  # noqa: E402
from integrations.distributed_agent.coordinator_backends import (  # noqa: E402
    InMemoryTaskLock)
from integrations.distributed_agent.worker_loop import (  # noqa: E402
    DistributedWorkerLoop)
from tests.unit.test_a_hive_goal_waiting_on_its_person_is_not_a_stall import (  # noqa: E402
    GOAL, NODE, PROMPT, TASK, TICK_S, _back, _clock, _Db, _Mem)
from tests.unit.test_speculative_turn_is_judged_by_its_reply import Goal  # noqa: E402

PERSON = 'owner-10202'
PID = dispatch.prompt_id_for_goal(GOAL)
#: What _held_plan_turn tells a background turn while the card is open.
WAITING = (f'{WAITING_FOR_PERSON_REPLY_PREFIX} Your plan for textile sourcing '
           f'is waiting on the card: approve it to build it, or deny it to '
           f'drop it.')
BUILT = 'Built the approved plan and listed 12 textile suppliers.'
#: What it tells a background turn once the card is denied (no card open).
DROPPED = (f'{WAITING_FOR_PERSON_REPLY_PREFIX} Dropped the plan for textile '
           f'sourcing. Tell me what you want and I will plan it again.')
#: The parent's counts over 7200 s (58419ffc2, measured by the reviewer).
PARENT_TURNS, PARENT_SAVES = 30, 91


class _Row:
    """The goal row as the dispatcher's lookup reads it."""

    def __init__(self, goal):
        self._goal = goal

    def filter_by(self, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._goal


class _Session:
    def __init__(self, goal):
        self._goal = goal

    def query(self, *a, **k):
        return _Row(self._goal)


class _PlanTurn:
    """The /chat turn of a goal whose agent is not built yet: its plan did
    not settle in review, so it waits on its creator's approval card.  Plays
    hart_intelligence_entry._held_plan_turn / _hold_for_the_creator over the
    real stores: the staged plan file and person_ask."""

    def __init__(self, prompts_dir, fail=False):
        self.prompts_dir = prompts_dir
        self.fail = fail
        self.turns = 0

    def _write(self, plan):
        with open(os.path.join(self.prompts_dir,
                               proposed_plan_filename(PID)), 'w') as f:
            json.dump(plan, f)

    def __call__(self, prompt, user_id, prompt_id, daemon_id=None, **k):
        assert str(prompt_id) == PID
        self.turns += 1
        if self.fail:
            return 'ok', user_facing_error(RuntimeError('Error code: 429'))
        staged = staged_plan(self.prompts_dir, prompt_id)
        hold = (staged or {}).get('hold')
        plan = {'name': 'textile sourcing', 'status': 'proposed_plan',
                'prompt_id': prompt_id}
        if isinstance(hold, dict):
            state = person_ask.ask_state(hold['ask_id'], hold['asked'])
            if state['status'] == 'open':
                return 'ok', WAITING
            decision = (state['answer'] or {}).get('decision')
            if decision == 'approve':
                self._write(plan)                  # the hold ends
                return 'ok', BUILT
            if decision == 'deny':
                self._write(plan)                  # the hold ends, nothing built
                return 'ok', DROPPED
        out = person_ask.ask(
            PERSON, prompt_id,
            {'type': 'approval', 'action': f'approve_plan:{prompt_id}',
             'title': 'Approve the plan?',
             'description': 'The plan did not settle in review. Approve it?'},
            prompt_id=prompt_id, background=True)
        assert out['ok'], out
        self._write(dict(plan, hold={'ask_id': out['ask_id'], 'asked': PERSON,
                                     'why': 'the reviewer answered no verdict'}))
        return 'ok', WAITING


class _Run:
    """A simulated clock of daemon ticks over one goal handed to the hive:
    the dispatcher's handoff, the settle gate, then the worker's tick."""

    def __init__(self, goal, tmp_path, chat):
        self.goal = goal
        self.chat = chat
        self.led = SmartLedger(agent_id='coord', session_id='s', backend=_Mem())
        self.saves = 0
        real_save = self.led.save

        def _counted_save():
            self.saves += 1
            return real_save()
        self.led.save = _counted_save
        self.co = tc.DistributedTaskCoordinator(
            ledger=self.led, task_lock=InMemoryTaskLock(),
            verifier=MagicMock(), baseline=MagicMock())
        self.sim = _clock()
        self.tmp = tmp_path
        self.worker = DistributedWorkerLoop()
        self.worker._node_id = NODE
        self.worker._capabilities = []
        self.tick = -1

    @contextlib.contextmanager
    def boundaries(self):
        goal, co = self.goal, self.co

        @contextlib.contextmanager
        def _db_session(*a, **k):
            yield _Session(goal)

        patches = [
            patch.object(tc, 'datetime', self.sim),
            patch.object(ledger_core, 'datetime', self.sim),
            patch('core.platform_paths.get_recipe_prompts_dir',
                  return_value=str(self.tmp)),
            patch('core.platform_paths.get_agent_data_dir',
                  return_value=str(self.tmp)),
            patch('integrations.distributed_agent.requesters.this_node_id',
                  return_value=NODE),
            patch('integrations.social.models.db_session', _db_session),
            patch('integrations.social.services.NotificationService.create'),
            patch('integrations.social.services.NotificationService.mark_read'),
            patch('integrations.social.realtime.on_notification'),
            patch('integrations.agent_engine.liquid_ui_service.push_agent_ui',
                  return_value=True),
            patch('integrations.agent_engine.liquid_ui_service.withdraw_agent_ui'),
            patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                  side_effect=lambda prompt, *a, **k: (True, '', prompt)),
            patch('security.hive_guardrails.GuardrailEnforcer.after_response',
                  return_value=(True, '')),
            patch('integrations.agent_engine.world_model_bridge.'
                  'get_world_model_bridge', return_value=MagicMock()),
            patch.object(dispatch, '_get_distributed_coordinator',
                         return_value=co),
            patch.object(dispatch, '_decompose_goal',
                         side_effect=lambda prompt, gid, gt, uid: [{
                             'task_id': f'{gid}_task_0', 'description': prompt,
                             'capabilities': []}]),
            patch.object(dispatch, 'goal_turn_held', return_value=''),
            patch.object(dispatch, 'goal_turn_model',
                         side_effect=lambda gt, mc=None: (mc, '')),
            patch.object(dispatch, 'revenue_expert_config', return_value=None),
            patch.object(dispatch, 'local_chat_dispatch', self.chat),
            patch.object(self.worker, '_get_coordinator', return_value=co),
            patch.object(self.worker, '_dispatch_would_defer', return_value=None),
            patch.object(self.worker, '_held_for', return_value=''),
            patch.object(self.worker, '_goal_not_running', return_value=None),
            patch.object(self.worker, '_expert_goal_type', return_value=None),
            patch.object(self.co, '_notify_goal_contribution'),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield

    def ticks(self, n, worker=None):
        """``n`` more daemon ticks; ``worker(run)`` is the worker's part of
        each (default: the real worker tick).  Returns the tick the goal
        stopped being active at, or None."""
        worker = worker or (lambda run: run.worker._tick())
        with self.boundaries():
            for _ in range(n):
                self.tick += 1
                handed = dispatch.dispatch_goal_distributed(
                    PROMPT, PERSON, GOAL, 'marketing')
                assert handed == GOAL, f'tick {self.tick}: nothing handed off'
                agent_daemon._settle_dispatched_goal(_Db(), self.goal, GOAL,
                                                     handed_to_hive=True)
                if self.goal.status != 'active':
                    return self.tick
                worker(self)
                _back(self.goal.config_json, TICK_S)
                self.sim.offset += TICK_S
        return None

    def answer(self, decision='approve'):
        hold = staged_plan(str(self.tmp), PID)['hold']
        with self.boundaries():
            body, status = person_ask.answer_ask(
                hold['ask_id'], {'decision': decision}, PERSON)
        assert status == 200, body

    def task(self):
        return self.led.get_task(TASK)


@pytest.fixture
def goal(monkeypatch):
    monkeypatch.setenv('HEVOLVE_GOAL_VERIFY_TIMEOUT_S', '1800')
    g = Goal(gid=GOAL)
    g.title = 'Tamil Nadu textile supplier sourcing'
    return g


@pytest.fixture(autouse=True)
def _no_asks_from_other_tests():
    with patch.dict(person_ask._asks, clear=True):
        for said in (getattr(agent_daemon, '_hive_wait_said', None),
                     getattr(tc, '_unreadable_said', None)):
            if isinstance(said, dict):
                said.clear()
        yield


def test_an_unanswered_card_costs_no_turn_and_no_ledger_save_per_tick(
        goal, tmp_path, caplog):
    """FAILS BEFORE THE FIX: 121 turns and 363 full ledger saves in 7200 s.
    Now the first turn files the card and holds the task, and nothing runs
    or is saved again while the card is open; the goal is not paused."""
    chat = _PlanTurn(str(tmp_path))
    run = _Run(goal, tmp_path, chat)
    with caplog.at_level(logging.INFO):
        stopped = run.ticks(7200 // TICK_S + 1)

    cfg = goal.config_json
    assert stopped is None and goal.status == 'active', (
        cfg.get('pause_cause'), cfg.get('pause_reason'))
    assert 'pause_cause' not in cfg
    assert run.task().status == TaskStatus.BLOCKED
    print(f'turns={chat.turns} saves={run.saves}')
    assert chat.turns <= PARENT_TURNS and run.saves <= PARENT_SAVES, (
        chat.turns, run.saves)
    # The submission, the claim and the hold; nothing per tick after that.
    assert (chat.turns, run.saves) == (1, 3), (chat.turns, run.saves)
    said = [r.getMessage() for r in caplog.records
            if r.module == 'agent_daemon' and GOAL in r.getMessage()
            and 'waits on a person' in r.getMessage()]
    assert len(said) == 1, said


@pytest.mark.parametrize('decision', ['approve', 'deny'])
def test_the_task_runs_again_once_the_person_answers(goal, tmp_path, decision):
    """Held while the card is open; the tick after the answer releases it and
    the worker's next turn acts on the answer: an approval builds the plan,
    a denial drops it (that turn waits again, with no card open, so the
    next tick runs the goal again and it plans afresh)."""
    chat = _PlanTurn(str(tmp_path))
    run = _Run(goal, tmp_path, chat)
    assert run.ticks(30) is None
    assert chat.turns == 1 and run.task().status == TaskStatus.BLOCKED

    run.answer(decision)
    assert run.ticks(1) is None
    assert chat.turns == 2
    if decision == 'approve':
        assert run.task().status == TaskStatus.COMPLETED
        assert run.task().result == BUILT
    else:
        assert run.task().error_message == DROPPED
        assert run.ticks(1) is None
        assert chat.turns == 3, 'a dropped plan kept its task held'
        assert run.task().status == TaskStatus.BLOCKED     # its new card
    assert goal.status == 'active'


class _ChatWait:
    """A turn that waits on its person with nothing this node can read."""

    def __init__(self, reply):
        self.reply = reply
        self.turns = 0

    def __call__(self, prompt, user_id, prompt_id, daemon_id=None, **k):
        self.turns += 1
        return 'ok', self.reply


@pytest.mark.parametrize('case', ['card_on_a_peer', 'reuse_step_waits_on_chat'])
def test_a_wait_this_node_cannot_prove_is_no_worse_than_the_parent(
        goal, tmp_path, case):
    """FAILS ON 4ecd81b1d (review of it, measured): 121 turns and 363 saves
    in 7200 s and never paused.  The card filed on a peer (its plan and its
    open ask live in the peer's prompts folder), or a REUSE step waiting on
    the person's chat reply (waiting_for_person_reply, no card): this node
    cannot show the person is still being asked, so the task is released
    every tick and the wait counts, as on the parent 58419ffc2: 30 turns,
    91 saves, paused as ledger_grounding at tick 30."""
    from core.agent_tools import waiting_for_person_reply
    if case == 'card_on_a_peer':
        peer = tmp_path / 'peer_prompts'
        peer.mkdir()
        chat = _PlanTurn(str(peer))
    else:
        chat = _ChatWait(waiting_for_person_reply(3))
    run = _Run(goal, tmp_path, chat)
    stopped = run.ticks(7200 // TICK_S + 1)
    print(f'{case}: turns={chat.turns} saves={run.saves} paused at tick {stopped}')
    assert goal.status == 'paused', 'a wait this node cannot prove never paused'
    assert goal.config_json['pause_cause'] == 'ledger_grounding'
    assert stopped is not None and stopped <= 1800 // TICK_S, stopped
    assert chat.turns <= PARENT_TURNS and run.saves <= PARENT_SAVES, (
        chat.turns, run.saves)


def test_an_answer_after_the_re_dispatch_builds_on_that_tick(goal, tmp_path):
    """FAILS ON 5d578624a (review of it): built at tick 11, where both
    parents built at tick 10.  The card is approved after tick 10's
    re-dispatch kept the task held, before that tick's worker: the answer
    itself hands the task back (person_ask -> dispatch
    .release_holds_answered), so tick 10's worker builds the plan."""
    chat = _PlanTurn(str(tmp_path))
    run = _Run(goal, tmp_path, chat)
    assert run.ticks(10) is None
    assert run.task().status == TaskStatus.BLOCKED
    saves = run.saves

    def _approved_then_worker(run):
        run.answer('approve')
        run.worker._tick()
    assert run.ticks(1, worker=_approved_then_worker) is None
    assert run.tick == 10
    assert run.task().status == TaskStatus.COMPLETED, 'built a tick late'
    assert run.task().result == BUILT
    assert chat.turns == 2
    # The release moved from the next dispatch to the answer: one save for
    # it, one for the claim, one for the result.
    assert run.saves - saves == 3, run.saves - saves


class _DroppedButStillHeld(_PlanTurn):
    """Dismissed, but the turn could not rewrite the staged plan (_end's
    write failed, e.g. disk full): the hold stays with its dismissed ask
    and every turn answers "Dropped ..." again."""

    def __call__(self, prompt, user_id, prompt_id, daemon_id=None, **k):
        hold = (staged_plan(self.prompts_dir, prompt_id) or {}).get('hold')
        if isinstance(hold, dict) and person_ask.ask_state(
                hold['ask_id'], hold['asked'])['status'] == 'dismissed':
            self.turns += 1
            return 'ok', DROPPED
        return super().__call__(prompt, user_id, prompt_id, daemon_id, **k)


def test_a_dismissed_card_whose_hold_cannot_end_still_pauses(goal, tmp_path):
    """FAILS ON 5d578624a (review of it): 121 turns and 363 saves, never
    paused; 58419ffc2 paused at tick 30 with 30 turns and 91 saves in all.
    Only an open card holds: a dismissed one is counted as on the parent,
    from the last tick the card was open, and the whole run costs no more
    than the parent's."""
    chat = _DroppedButStillHeld(str(tmp_path))
    run = _Run(goal, tmp_path, chat)
    assert run.ticks(5) is None
    hold = staged_plan(str(tmp_path), PID)['hold']
    with run.boundaries():
        body, status = person_ask.dismiss_ask(hold['ask_id'], PERSON)
    assert status == 200, body
    stopped = run.ticks(121)
    print(f'dismissed, hold kept: turns={chat.turns} saves={run.saves} '
          f'paused at tick {stopped}')
    assert goal.status == 'paused', 'a dismissed card held the goal forever'
    assert goal.config_json['pause_cause'] == 'ledger_grounding'
    assert stopped is not None and stopped <= 4 + 1800 // TICK_S + 1, stopped
    assert chat.turns <= PARENT_TURNS and run.saves <= PARENT_SAVES, (
        chat.turns, run.saves)


def test_an_unreadable_card_is_warned_once_and_counts(goal, tmp_path, caplog):
    """Nit of the review of 5d578624a: a card store that cannot be read was
    one WARNING per tick.  Now one per goal per change, and the wait counts
    as on the parent (nothing proves the person is still asked)."""
    chat = _PlanTurn(str(tmp_path))
    run = _Run(goal, tmp_path, chat)
    assert run.ticks(2) is None
    with patch.object(dispatch, 'goal_plan_card_state',
                      side_effect=OSError('store down')), \
            caplog.at_level(logging.WARNING):
        stopped = run.ticks(60)
    said = [r for r in caplog.records
            if 'could not read whether its person answered' in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in said]
    assert stopped is not None and goal.status == 'paused'


@pytest.mark.parametrize('left', ['pending', 'in_progress', 'failing'])
def test_a_stall_after_the_answer_still_pauses(goal, tmp_path, left):
    """The reviewer's probe: held for 20 ticks, then the person answers and
    the task is left unclaimed, left in progress, or fails every turn.  The
    wait ended with the answer (only an open card holds), so the goal pauses
    as ledger_grounding 30 ticks after the last tick its card was open
    (tick 19), give or take a tick."""
    chat = _PlanTurn(str(tmp_path))
    run = _Run(goal, tmp_path, chat)
    assert run.ticks(20) is None
    run.answer()

    def _after(run):
        if left == 'in_progress' and run.task().status == TaskStatus.PENDING:
            assert run.co.claim_next_task(NODE, []) is not None
        elif left == 'failing':
            chat.fail = True
            run.worker._tick()
    stopped = run.ticks(60, worker=_after)
    print(f'{left}: paused at tick {stopped}')
    assert stopped is not None and goal.status == 'paused'
    assert goal.config_json['pause_cause'] == 'ledger_grounding'
    assert 19 + 30 <= stopped <= 20 + 30 + 1, stopped
