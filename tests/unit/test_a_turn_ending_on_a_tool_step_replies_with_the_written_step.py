"""A turn that ends on a step whose words name a tool replies with the
answer the turn's writing step wrote, not with the tool step's report.

Measured live 2026-11-09 12:45-12:52 IST on the owner's desktop (build67 plus
the hot-swapped core/agent_tools.py), request msg_1794208521422_odljlx, agent
54 "Let's continue my lesson." as 10202:

    action 2  "Write this turn's reply to the learner yourself ..."
              Assistant: "I see! There are actually three books available
              ... The Art of Computer Programming ... isn't in your library.
              Would you like to: 1. Continue with one of the books ..."
    action 3  "Call save_data_in_memory with the key teach.<that same user id>"
              ChatInstructor: "Action 3 is NOT complete: ... report the actual
              returned result verbatim ..."           (the fabrication re-steer)
              Assistant -> save_data_in_memory -> "Saved at teach.10202: ..."
              Assistant: "Great! The progress has been saved successfully.
              The learner's data now shows: ... Page range: 1-12 ... Stage:
              quiz ..."                              (wrong: it saved 1-2)
              StatusVerifier: {"status": "completed", "action_id": 3}

[SYNTHESIS] _reuse_written_answer, bounded at action 3's dispatch, delivered
the save report as the whole reply; action 2's answer never reached the
person.  The same shape on 11-07 (msg_1794040636536_3y7gjm) delivered "I
apologize for the confusion. Let me try a different approach to save the
learner's progress."

The rule: a turn that ends on steps whose named tools are all SAVES (the
save_ entries of core.constants.BOOKKEEPING_TOOLS) replies with the longest
written answer of the step just before those saves, whatever that step is (a
writing step's lesson, or what a search or read step wrote after its result);
a save is nothing the person reads.  When that step wrote nothing, no earlier
step's text stands in (review of fffdffc96, F2/F3).  A message that carries a
tool call is the model's note to its tool and is never the reply (F1, live on
build 69).  A last step naming any other tool (google_search, a memory READ)
keeps today's recovery, because its result is the answer: the first cut took
every tool step and replied "Sure, let me look that up" (review of the
uncommitted fix, F1).  The turn starts at the newest copy of the exact seed
text _reuse_seed_message built for this request; nothing before it is ever
delivered, and a seed that fell back to the bare words leaves no start at all
(review F2).

Drives the REAL seed builder and the REAL _reuse_synthesis_turn, with
agents whose registered tools are what action_named_tools reads.

    python -m pytest tests/unit/test_a_turn_ending_on_a_tool_step_replies_with_the_written_step.py -q
"""
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

rr = pytest.importorskip('hartos.reuse_recipe')

_UP = 'livetest_turn_reply_10202_54'
WORDS = "Let's continue my lesson."
A1 = ("Call get_user_id, then get_data_by_key with the key teach.<that user "
      "id> to read this learner's saved progress.")
A2 = ("Write this turn's reply to the learner yourself, as your own chat "
      "message to them, never through a messaging tool, from their latest "
      "message and the progress action 1 already read.")
A3 = ("Call save_data_in_memory with the key teach.<that same user id> to save "
      "this learner's updated progress after the reply.")

ACTION2_REPLY = (
    "I see! There are actually three books available: How Rain Happens, A "
    "Beginner's Guide to Computers and Energy. The progress shows you were "
    "working on The Art of Computer Programming, but that book isn't in your "
    "library. Would you like to continue with one of the books in your "
    "library, upload a new book, or learn from general knowledge?")
SAVE_REPORT = (
    "Great! The progress has been saved successfully. The learner's data now "
    "shows: Book: The Art of Computer Programming, Page range: 1-12, Stage: "
    "quiz. The progress has been properly recorded in memory.")
RESTEER = ("Action 3 is NOT complete: the tool(s) save_data_in_memory did not "
           "produce a real result -- either they were never called, or they ran "
           "and returned a failure. Do not report this action as completed. "
           "@Helper call save_data_in_memory now with real arguments and report "
           "the actual returned result verbatim.")
OLD_TURN_ANSWER = "Here is the lesson from your PREVIOUS turn: page 1 of chapter 1."


class _Task:
    def __init__(self, texts):
        self.actions = [{'action': t} for t in texts]
        self.current_action = 1
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return self.actions[idx]


class _Agent:
    def __init__(self, name, tools):
        self.name = name
        self._function_map = {t: (lambda **k: None) for t in tools}
        self.llm_config = {'tools': [{'function': {'name': t}} for t in tools]}
        self._oai_messages = {}


class _Chat:
    def __init__(self, messages, agents):
        self.messages = list(messages)
        self.agents = agents


class _Manager:
    _oai_messages = {}


class _Recorder:
    """Stands in for chat_instructor; captures a steer instead of posting it."""

    def __init__(self):
        self.messages = []

    def initiate_chat(self, recipient=None, message=None, **kw):
        self.messages.append(message)


@pytest.fixture(autouse=True)
def _fresh_turn_starts(monkeypatch):
    """Each test starts with no turn recorded: the module's turn-start state
    leaked between tests before (review F9)."""
    monkeypatch.setattr(rr, '_reuse_turn_start', rr.TTLCache(
        ttl_seconds=7200, max_size=500, name='test_reuse_turn_start'))


@pytest.fixture
def turn(monkeypatch):
    """The session as the live turn left it: a 3-action recipe, the seed the
    REAL producer built for this request, and the gate reporting nothing
    outstanding (live: unrun=[] for action 3)."""
    texts = [A1, A2, A3]
    task = _Task(texts)
    monkeypatch.setitem(rr.user_tasks, _UP, task)
    monkeypatch.setitem(rr.recipes, _UP, {'actions': [
        {'action': t, 'recipe': [{'steps': f'step {i}', 'tool_name': ''}]}
        for i, t in enumerate(texts, 1)]})
    monkeypatch.setattr(rr, 'current_app',
                        types.SimpleNamespace(logger=mock.MagicMock()))
    monkeypatch.setattr(rr, '_reuse_outstanding_tools', lambda *a, **k: [],
                        raising=True)
    seed = rr._reuse_seed_message(_UP, WORDS)
    task.current_action = 4          # every action advanced, as live
    agents = [_Agent('Assistant', ['get_user_id', 'get_data_by_key',
                                   'save_data_in_memory'])]
    return types.SimpleNamespace(seed=seed, task=task, agents=agents)


def _dispatch(n, text):
    return {'role': 'user', 'name': 'ChatInstructor',
            'content': f'Perform this action -> Action #{n}:{text}'}


def _verdict(n):
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': f'{{"status": "completed", "action_id": {n}}}'}


def _assistant(text):
    return {'role': 'user', 'name': 'Assistant', 'content': text}


def _live_log(seed, action2_reply=ACTION2_REPLY):
    """The 12:52:11 group log, in order, after an earlier turn's lines."""
    log = [
        _dispatch(2, A2),
        _assistant(OLD_TURN_ANSWER),                       # an earlier turn
        _verdict(2),
        {'role': 'user', 'name': 'ChatInstructor', 'content': seed},
        {'role': 'assistant', 'name': 'Assistant', 'content': '',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'get_user_id', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'Helper', 'content': '10202',
         'tool_call_id': 'c1'},
        _verdict(1),
        _dispatch(2, A2),
    ]
    if action2_reply:
        log.append(_assistant(action2_reply))
    log += [
        _verdict(2),
        _dispatch(3, A3),
        _verdict(3),
        {'role': 'user', 'name': 'ChatInstructor', 'content': RESTEER},
        {'role': 'assistant', 'name': 'Assistant',
         'content': "I can see the learner's saved progress. Let me save it:",
         'tool_calls': [{'id': 'c2', 'type': 'function',
                         'function': {'name': 'save_data_in_memory',
                                      'arguments': '{"key": "teach.10202"}'}}]},
        {'role': 'tool', 'name': 'Helper',
         'content': 'Saved at teach.10202: {"stage": "check_question"}',
         'tool_call_id': 'c2'},
        _assistant(SAVE_REPORT),
        _verdict(3),
    ]
    return log


def _synthesise(turn, log):
    chat = _Chat(log, turn.agents)
    rec = _Recorder()
    rr._reuse_synthesis_turn(_UP, chat, _Manager(), rec)
    return (chat.messages[-1].get('content') or ''), rec


def test_the_turn_replies_with_the_writing_steps_answer(turn):
    """THE DEFECT, replayed.  RED before the fix: the tail is the save report."""
    tail, rec = _synthesise(turn, _live_log(turn.seed))
    assert tail == ACTION2_REPLY, (
        "the reply is not action 2's answer; the person would get "
        f"{tail[:90]!r} (live 2026-11-09 12:52, the save report)")
    assert rec.messages == [], 'no synthesis round is needed: the answer exists'


def test_the_tool_steps_report_to_the_gate_is_never_the_reply(turn):
    """The line written in answer to the re-steer is addressed to the
    harness, and it misreports the save (1-12 vs the saved 1-2)."""
    tail, _rec = _synthesise(turn, _live_log(turn.seed))
    assert 'saved successfully' not in tail


def test_never_reaches_into_an_earlier_turn(turn):
    """With no written answer in THIS turn, nothing before this request's seed
    is delivered (the group log keeps earlier turns: clear_history=False);
    today's recovery decides instead."""
    tail, _rec = _synthesise(turn, _live_log(turn.seed, action2_reply=None))
    assert 'PREVIOUS' not in tail


def test_a_turn_ending_on_a_writing_step_keeps_todays_recovery(monkeypatch, turn):
    """When the last step names no tool, its own written answer is the reply,
    exactly as before; an earlier step's text is not."""
    texts = [A1, A2]
    turn.task.actions = [{'action': t} for t in texts]
    turn.task.current_action = 3
    log = _live_log(turn.seed)
    log = log[:log.index(_dispatch(3, A3))]          # the turn ends on action 2
    log.append(_verdict(2))
    tail, _rec = _synthesise(turn, log)
    assert tail == ACTION2_REPLY


def test_two_writing_steps_reply_with_the_last_one(monkeypatch, turn):
    """Both steps name no tool: the last step's own answer is the reply, never
    the earlier step's draft (today's recovery, untouched by this rule)."""
    w1 = 'Write a short draft of the lesson for the learner.'
    w2 = 'Write the final lesson for the learner yourself.'
    turn.task.actions = [{'action': w1}, {'action': w2}]
    turn.task.current_action = 3
    log = [
        {'role': 'user', 'name': 'ChatInstructor', 'content': turn.seed},
        _assistant('DRAFT: rain forms when water vapour cools.'),
        _verdict(1),
        _dispatch(2, w2),
        _assistant('FINAL: Rain forms when water vapour in clouds cools, '
                   'condenses into droplets and falls.'),
        _verdict(2),
    ]
    tail, _rec = _synthesise(turn, log)
    assert tail.startswith('FINAL:'), tail


def test_a_writing_step_that_wrote_nothing_keeps_todays_steer(monkeypatch, turn):
    """The rule is scoped to a turn that ends on a step naming a tool.  A turn
    ending on a writing step that wrote nothing is decided as before: nothing
    in its own window, so the synthesis steer asks for the answer.  It does
    not reach back to an earlier step's text."""
    w1 = 'Write a short draft of the lesson for the learner.'
    w2 = 'Write the final lesson for the learner yourself.'
    turn.task.actions = [{'action': w1}, {'action': w2}]
    turn.task.current_action = 3
    log = [
        {'role': 'user', 'name': 'ChatInstructor', 'content': turn.seed},
        _assistant('DRAFT: rain forms when water vapour cools.'),
        _verdict(1),
        _dispatch(2, w2),
        _verdict(2),
    ]
    tail, rec = _synthesise(turn, log)
    assert rec.messages, 'the synthesis steer must still be posted'
    assert not tail.startswith('DRAFT:'), tail


def test_a_single_tool_step_keeps_its_own_summary(monkeypatch, turn):
    """A one-step turn whose step names a tool and writes its result (search,
    then summarise) has no earlier writing step: its own text still stands."""
    summary = 'Tokyo has about 14 million people (2023 census).'
    action = 'Use google_search to find the population of Tokyo and summarise it.'
    turn.task.actions = [{'action': action}]
    turn.task.current_action = 2
    turn.agents.append(_Agent('Helper', ['google_search']))
    seed = turn.seed
    log = [
        {'role': 'user', 'name': 'ChatInstructor', 'content': seed},
        {'role': 'assistant', 'name': 'Assistant', 'content': '',
         'tool_calls': [{'id': 'g1', 'type': 'function',
                         'function': {'name': 'google_search', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'Helper', 'content': 'Tokyo: 14,000,000',
         'tool_call_id': 'g1'},
        _assistant(summary),
        _verdict(1),
    ]
    tail, _rec = _synthesise(turn, log)
    assert tail == summary


# --- The review of the first cut (REJECTED, 11-09): each probe it ran is pinned
# here, with the shape that made it fail.

@pytest.fixture
def make(monkeypatch):
    """A session with its own steps and registered tools (the reviewer's env)."""
    monkeypatch.setattr(rr, 'current_app',
                        types.SimpleNamespace(logger=mock.MagicMock()))
    monkeypatch.setattr(rr, '_reuse_outstanding_tools', lambda *a, **k: [])

    def _make(up, texts, tools):
        task = _Task(texts)
        monkeypatch.setitem(rr.user_tasks, up, task)
        monkeypatch.setitem(rr.recipes, up, {'actions': [
            {'action': t, 'recipe': [{'steps': f'step {i}', 'tool_name': ''}]}
            for i, t in enumerate(texts, 1)]})
        return task, [_Agent('Assistant', tools)]
    return _make


def _call(cid, fn):
    return {'role': 'assistant', 'name': 'Assistant', 'content': '',
            'tool_calls': [{'id': cid, 'type': 'function',
                            'function': {'name': fn, 'arguments': '{}'}}]}


def _result(cid, text):
    return {'role': 'tool', 'name': 'Helper', 'content': text,
            'tool_call_id': cid}


def _seed_msg(seed):
    return {'role': 'user', 'name': 'ChatInstructor', 'content': seed}


def _pending(n):
    return {'role': 'user', 'name': 'StatusVerifier',
            'content': f'{{"status": "pending", "action_id": {n}}}'}


def _run(up, log, agents):
    chat = _Chat(log, agents)
    rec = _Recorder()
    rr._reuse_synthesis_turn(up, chat, _Manager(), rec)
    return (chat.messages[-1].get('content') or ''), rec


SAVE = "Call save_data_in_memory to save the learner's progress."
WRITE = "Write this turn's reply to the learner yourself."
SAVED_REPORT = 'Great! The progress has been saved successfully.'


def test_a_last_step_whose_tool_gives_the_answer_keeps_that_answer(make):
    """F1 (Critical): the search result IS the reply.  The first cut replied
    with step 1's "let me look that up" for every turn ending on a tool step
    (133 of 1014 desktop flows end on one after a step naming none)."""
    up = 'turnreply_f1'
    a1 = "Acknowledge the learner's question and tell them you will look it up."
    a2 = ("Use google_search to find the population of Tokyo and tell the "
          "learner the number.")
    task, agents = make(up, [a1, a2], ['google_search'])
    seed = rr._reuse_seed_message(up, 'How many people live in Tokyo?')
    task.current_action = 3
    answer = 'Tokyo has about 14 million people.'
    log = [_seed_msg(seed), _assistant('Sure, let me look that up for you.'),
           _verdict(1), _dispatch(2, a2), _call('g1', 'google_search'),
           _result('g1', 'Tokyo: 14,000,000'), _assistant(answer), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == answer, f'delivered {tail!r}'


def test_a_last_step_that_reads_memory_keeps_todays_recovery(make):
    """A READ is not a save: get_data_by_key's result is what the step answers
    from, so the step's own answer stands.  Only the save_ entries of
    BOOKKEEPING_TOOLS end a turn on nothing the person reads."""
    up = 'turnreply_read'
    a1 = "Greet the learner and say you will check their saved progress."
    a2 = ("Call get_data_by_key with the key teach.<user id> and tell the "
          "learner which page they reached.")
    task, agents = make(up, [a1, a2], ['get_data_by_key'])
    seed = rr._reuse_seed_message(up, 'Where was I?')
    task.current_action = 3
    answer = 'You reached page 12 of How Rain Happens.'
    log = [_seed_msg(seed), _assistant('Hi! Let me check your progress.'),
           _verdict(1), _dispatch(2, a2), _call('r1', 'get_data_by_key'),
           _result('r1', '{"page": 12}'), _assistant(answer), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == answer, f'delivered {tail!r}'


def test_a_seed_that_fell_back_never_delivers_the_previous_turn(make, monkeypatch):
    """F2: the seed builder failed on this turn (bare words went out), so no
    turn start is recorded; the previous turn's start must not be used."""
    up = 'turnreply_f2'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed1 = rr._reuse_seed_message(up, 'Teach me about rain.')

    def boom(*a, **k):
        raise KeyError('recipes entry gone')
    monkeypatch.setattr(rr, '_build_reuse_action_message', boom)
    task.current_action = 1
    seed2 = rr._reuse_seed_message(up, 'Now teach me about snow.')
    assert seed2 == 'Now teach me about snow.'          # the fallback happened
    task.current_action = 3
    log = [_seed_msg(seed1), _assistant('OLD: Rain forms when water vapour cools.'),
           _verdict(1), _dispatch(2, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved at teach.1'), _assistant('Saved.'), _verdict(2),
           {'role': 'user', 'name': 'User', 'content': seed2},
           _assistant('NEW: Snow forms when ice crystals grow in cold clouds.'),
           _verdict(1), _dispatch(2, SAVE), _call('s2', 'save_data_in_memory'),
           _result('s2', 'Saved at teach.1'), _assistant(SAVED_REPORT),
           _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert not tail.startswith('OLD:'), f'delivered the previous turn: {tail!r}'


@pytest.mark.parametrize('words', ['ok', 'teach me more about clouds'])
def test_the_turn_starts_at_this_requests_seed(make, words):
    """F5 and P4b: the turn starts at the exact seed text, not at a later
    re-dispatch of the seeded step that carries the same words (short words
    like 'ok' sit inside 'book'), and not at a report quoting them."""
    up = 'turnreply_f5'
    a1 = "Write this turn's reply to the learner about their book yourself."
    task, agents = make(up, [a1, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, words)
    task.current_action = 3
    reply = 'REPLY: Chapter 2 of your book starts with how clouds form.'
    log = [_seed_msg(seed), _assistant(reply), _pending(1),
           _dispatch(1, f'{a1} ({words})'), _verdict(1), _dispatch(2, SAVE),
           _call('s1', 'save_data_in_memory'), _result('s1', 'Saved'),
           _assistant(f'You asked "{words}". {SAVED_REPORT}'), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == reply, f'delivered {tail!r}'


def test_the_reply_is_the_lesson_not_the_note_after_it(make):
    """F4: the writing step wrote the lesson and then a one-line note; the
    longest answer in its window is the reply, as
    lifecycle_hooks._derive_written_answer chooses."""
    up = 'turnreply_f4'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 3
    reply = ('Page 3 explains evaporation: the sun warms water, it rises as '
             'vapour, cools and forms clouds. Ready for a question on it?')
    log = [_seed_msg(seed), _assistant(reply),
           _assistant('I have written the reply to the learner.'),
           _verdict(1), _dispatch(2, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == reply, f'delivered {tail!r}'


def test_an_earlier_longer_draft_is_never_the_reply(make):
    """Only the LATEST writing step's window is read: a longer draft from an
    earlier writing step never beats the final answer."""
    up = 'turnreply_draft'
    w1 = 'Write a long draft of the lesson for the learner.'
    w2 = 'Write the final lesson for the learner yourself.'
    task, agents = make(up, [w1, w2, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me about rain.')
    task.current_action = 4
    final = 'FINAL: Rain is water vapour that cools, condenses and falls.'
    draft = 'DRAFT: ' + ('Rain forms when warm moist air rises, cools and '
                         'condenses into droplets that grow until they fall. ') * 3
    log = [_seed_msg(seed), _assistant(draft), _verdict(1), _dispatch(2, w2),
           _assistant(final), _verdict(2), _dispatch(3, SAVE),
           _call('s1', 'save_data_in_memory'), _result('s1', 'Saved'),
           _assistant(SAVED_REPORT), _verdict(3)]
    tail, _rec = _run(up, log, agents)
    assert tail == final, f'delivered {tail!r}'


def test_other_seats_and_pipeline_text_are_never_picked(make):
    """P6: under the writing step only other seats and the pipeline's own text:
    the turn picker returns nothing."""
    up = 'turnreply_p6'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 3
    bad = [
        {'role': 'user', 'name': 'Helper', 'content': 'HELPER prose.'},
        {'role': 'user', 'name': 'StatusVerifier',
         'content': '{"message2userfinal": "VERIFIER voice"}'},
        {'role': 'user', 'name': 'Assistant',
         'content': rr._REUSE_SYNTHESIS_STEER},          # seat name lost
        {'role': 'user', 'name': 'Assistant',
         'content': '@StatusVerifier ROUTED please verify.'},
        {'role': 'user', 'name': 'Assistant',
         'content': '{"status": "completed", "action_id": 1}'},
    ]
    log = [_seed_msg(seed), *bad, _verdict(1), _dispatch(2, SAVE),
           _call('s1', 'save_data_in_memory'), _result('s1', 'Saved'),
           _assistant(SAVED_REPORT), _verdict(2)]
    assert rr._reuse_turn_written_reply(up, _Chat(log, agents)) is None


def test_the_log_names_the_picker(make):
    """F8: the synthesis log says which picker chose the reply."""
    up = 'turnreply_f8'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 3
    log = [_seed_msg(seed), _assistant('REPLY: page 3 is about evaporation.'),
           _verdict(1), _dispatch(2, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(2)]
    _run(up, log, agents)
    lines = [str(c.args[0]) for c in rr.current_app.logger.info.call_args_list
             if c.args]
    assert any('already wrote the answer' in line and 'picker=turn' in line
               for line in lines), lines


# --- Probe P9 of the 11-10 proof: the save step's own line is the turn's last
# message and no verdict follows it, so _reuse_needs_synthesis is False and the
# first commit of this fix never asked the picker.

def test_a_save_report_with_no_verdict_after_it_is_not_the_reply(make):
    up = 'turnreply_p9'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 3
    reply = 'REPLY: page 3 is about evaporation.'
    log = [_seed_msg(seed), _assistant(reply), _verdict(1), _dispatch(2, SAVE),
           _call('s1', 'save_data_in_memory'), _result('s1', 'Saved'),
           _assistant(SAVED_REPORT)]
    tail, _rec = _run(up, log, agents)
    assert tail == reply, f'delivered {tail!r}'
    lines = [str(c.args[0]) for c in rr.current_app.logger.info.call_args_list
             if c.args]
    assert any("save step's own line" in line and 'picker=turn' in line
               for line in lines), lines


def test_a_read_step_answer_with_no_verdict_after_it_stays_the_reply(make):
    """A READ's answer is the reply: the P9 path only acts after a save."""
    up = 'turnreply_p9_read'
    a2 = ("Call get_data_by_key with the key teach.<user id> and tell the "
          "learner which page they reached.")
    task, agents = make(up, [WRITE, a2], ['get_data_by_key'])
    seed = rr._reuse_seed_message(up, 'Where was I?')
    task.current_action = 3
    answer = 'You reached page 12 of How Rain Happens.'
    log = [_seed_msg(seed), _assistant('REPLY: welcome back.'), _verdict(1),
           _dispatch(2, a2), _call('r1', 'get_data_by_key'),
           _result('r1', '{"page": 12}'), _assistant(answer)]
    tail, _rec = _run(up, log, agents)
    assert tail == answer, f'delivered {tail!r}'


def test_a_turn_ending_on_its_writing_step_is_left_alone(make):
    """No save step at the end: nothing is appended, the answer stands."""
    up = 'turnreply_p9_write_last'
    task, agents = make(up, [SAVE, WRITE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 3
    reply = 'REPLY: page 3 is about evaporation.'
    log = [_seed_msg(seed), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(1),
           _dispatch(2, WRITE), _assistant(reply)]
    chat = _Chat(log, agents)
    rr._reuse_synthesis_turn(up, chat, _Manager(), _Recorder())
    assert len(chat.messages) == len(log)
    assert chat.messages[-1]['content'] == reply


# --- The review of fffdffc96 (REJECTED, 11-10): the shapes it ran.

def _note(cid, fn, text):
    """An Assistant message whose words ride a tool call (build 69's shape)."""
    return {'role': 'assistant', 'name': 'Assistant', 'content': text,
            'tool_calls': [{'id': cid, 'type': 'function',
                            'function': {'name': fn, 'arguments': '{}'}}]}


NOTE = ("I need to save Arjun's attempt text to memory first. Since this is a "
        "first visit and there's no prior attempt data, I'll save the text as "
        "the last_attempt value before writing anything to the learner.")


def test_a_note_riding_a_tool_call_is_never_the_reply(make):
    """F1: the writing step's window holds the model's note to its save call
    (longer than the answer) and then the answer.  The answer is the reply,
    as it is when no save step follows."""
    up = 'turnreply_r_f1'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 3
    answer = 'Page 2: rain forms when vapour cools.'
    assert len(NOTE) > len(answer)
    log = [_seed_msg(seed), _note('n1', 'save_data_in_memory', NOTE),
           _result('n1', 'Saved'), _assistant(answer), _verdict(1),
           _dispatch(2, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == answer, f'delivered {tail!r}'


@pytest.mark.parametrize('key', ['tool_calls', 'function_call'])
def test_words_riding_a_call_are_not_an_answer(key):
    """The one reply predicate refuses them, so every picker (the turn's, the
    action's, the post-loop walk) and the synthesis gate read them alike."""
    call = ([{'id': 'x', 'type': 'function',
              'function': {'name': 'save_data_in_memory', 'arguments': '{}'}}]
            if key == 'tool_calls'
            else {'name': 'save_data_in_memory', 'arguments': '{}'})
    msg = {'role': 'assistant', 'name': 'Assistant', 'content': NOTE, key: call}
    assert rr._reuse_message_is_user_answer(msg) is False
    assert rr._reuse_message_is_user_answer(
        {'role': 'assistant', 'name': 'Assistant', 'content': NOTE}) is True


def test_an_answer_key_beside_a_call_is_judged_by_the_key():
    """Below the answer key, as the seats are: a message that carries
    message2userfinal is judged by what that key holds."""
    msg = {'role': 'assistant', 'name': 'Assistant',
           'content': ('{"message2userfinal": "Page 2: rain forms when '
                       'vapour cools."}'),
           'tool_calls': [{'id': 'x', 'type': 'function',
                           'function': {'name': 'save_data_in_memory',
                                        'arguments': '{}'}}]}
    assert rr._reuse_message_is_user_answer(msg) is True


def test_the_last_actions_note_is_not_delivered(make):
    """Build 69, msg_1794336534220_sim, picker=action: the last action's
    window holds only the note riding its call, the result and the verdict.
    The note is not the reply; the synthesis steer asks for one."""
    up = 'turnreply_r_action'
    a1 = "Call get_chat_history and tell the learner what they said last time."
    task, agents = make(up, [a1], ['get_chat_history'])
    seed = rr._reuse_seed_message(up, 'What did I say?')
    task.current_action = 2
    log = [_seed_msg(seed), _note('h1', 'get_chat_history', NOTE),
           _result('h1', '[]'), _verdict(1)]
    tail, rec = _run(up, log, agents)
    assert tail != NOTE
    assert rec.messages, 'the synthesis steer must be posted'


def test_a_search_step_before_the_save_gives_the_reply(make):
    """F2: a tool step whose result is the answer sits between the writing
    step and the save; its answer is the reply, not the writing step's
    opening line."""
    up = 'turnreply_r_f2'
    a1 = "Acknowledge the learner's question and tell them you will look it up."
    a2 = ("Use google_search to find the population of Tokyo and tell the "
          "learner the number.")
    task, agents = make(up, [a1, a2, SAVE],
                        ['google_search', 'save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'How many people live in Tokyo?')
    task.current_action = 4
    answer = 'Tokyo has about 14 million people.'
    log = [_seed_msg(seed), _assistant('Sure, let me look that up for you.'),
           _verdict(1), _dispatch(2, a2), _call('g1', 'google_search'),
           _result('g1', 'Tokyo: 14,000,000'), _assistant(answer), _verdict(2),
           _dispatch(3, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(3)]
    tail, _rec = _run(up, log, agents)
    assert tail == answer, f'delivered {tail!r}'


def test_a_read_step_before_the_save_gives_the_reply(make):
    """F3: no writing step at all (52 of the 89 save-ending desktop flows):
    the read step's answer is the reply, not the save line."""
    up = 'turnreply_r_f3'
    a1 = ("Call get_data_by_key with the key teach.<user id> and tell the "
          "learner which page they reached.")
    task, agents = make(up, [a1, SAVE],
                        ['get_data_by_key', 'save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Where was I?')
    task.current_action = 3
    answer = 'You reached page 12 of How Rain Happens.'
    log = [_seed_msg(seed), _call('r1', 'get_data_by_key'),
           _result('r1', '{"page": 12}'), _assistant(answer), _verdict(1),
           _dispatch(2, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == answer, f'delivered {tail!r}'


def test_a_step_before_the_saves_that_wrote_nothing_is_not_passed_over(make):
    """When the step before the saves wrote nothing (its send delivered the
    lesson), no earlier step's line stands in for it."""
    up = 'turnreply_r_pass'
    a2 = "Call send_message_to_user with this turn's lesson for the learner."
    task, agents = make(up, [WRITE, a2, SAVE],
                        ['send_message_to_user', 'save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 4
    early = 'Sure, let me get your lesson ready.'
    log = [_seed_msg(seed), _assistant(early), _verdict(1), _dispatch(2, a2),
           _call('m1', 'send_message_to_user'),
           _result('m1', 'Message sent successfully'), _verdict(2),
           _dispatch(3, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(3)]
    assert rr._reuse_turn_written_reply(up, _Chat(log, agents)) is None
    tail, _rec = _run(up, log, agents)
    assert tail != early, f'delivered {tail!r}'


def test_two_saves_at_the_end_reply_with_the_step_before_them(make):
    up = 'turnreply_r_two_saves'
    save2 = "Call save_to_long_term_memory to keep today's summary."
    task, agents = make(up, [WRITE, SAVE, save2],
                        ['save_data_in_memory', 'save_to_long_term_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 4
    reply = 'REPLY: page 3 is about evaporation.'
    log = [_seed_msg(seed), _assistant(reply), _verdict(1), _dispatch(2, SAVE),
           _call('s1', 'save_data_in_memory'), _result('s1', 'Saved'),
           _assistant(SAVED_REPORT), _verdict(2), _dispatch(3, save2),
           _call('s2', 'save_to_long_term_memory'), _result('s2', 'Saved'),
           _assistant('Summary stored for next time.'), _verdict(3)]
    tail, _rec = _run(up, log, agents)
    assert tail == reply, f'delivered {tail!r}'


def test_a_tie_goes_to_the_newer_answer(make):
    """Two answers of equal length in the step's window: the newer is the
    reply (the reviewer's tie_older mutant passed every earlier test)."""
    up = 'turnreply_r_tie'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed = rr._reuse_seed_message(up, 'Teach me the next page.')
    task.current_action = 3
    older = 'OLDER: page 3 is evaporation.'
    newer = 'NEWER: page 3 is evaporation.'
    assert len(older) == len(newer)
    log = [_seed_msg(seed), _assistant(older), _assistant(newer), _verdict(1),
           _dispatch(2, SAVE), _call('s1', 'save_data_in_memory'),
           _result('s1', 'Saved'), _assistant(SAVED_REPORT), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == newer, f'delivered {tail!r}'


def test_the_turn_starts_at_the_newest_copy_of_its_seed(make):
    """The same words twice build the same seed.  The previous turn ended on
    the same step, so only the newest seed keeps its longer lesson out (the
    reviewer's oldest_seed mutant passed every earlier test)."""
    up = 'turnreply_r_seed'
    task, agents = make(up, [WRITE, SAVE], ['save_data_in_memory'])
    seed1 = rr._reuse_seed_message(up, 'Teach me the next page.')
    old = 'OLD: ' + 'the previous lesson about clouds, ' * 6
    task.current_action = 1
    seed2 = rr._reuse_seed_message(up, 'Teach me the next page.')
    assert seed1 == seed2
    task.current_action = 3
    new = 'NEW: page 4 is rain.'
    log = [_seed_msg(seed1), _assistant(old), _seed_msg(seed2),
           _assistant(new), _verdict(1), _dispatch(2, SAVE),
           _call('s1', 'save_data_in_memory'), _result('s1', 'Saved'),
           _assistant(SAVED_REPORT), _verdict(2)]
    tail, _rec = _run(up, log, agents)
    assert tail == new, f'delivered {tail!r}'
