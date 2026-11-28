"""
core/agent_tools.py — Canonical AutoGen tool definitions (single source of truth).

Every core tool is defined ONCE here. Both create_recipe.py and reuse_recipe.py
call build_core_tool_closures() + register_core_tools() instead of duplicating
function bodies and registration lines.

Pattern mirrors:
  - integrations/service_tools/media_agent.py   → register_media_tools()
  - integrations/channels/memory/agent_memory_tools.py → register_autogen_tools()
  - integrations/agent_engine/marketing_tools.py → register_marketing_tools()
"""
import bisect
import html
from html.entities import html5 as _HTML5_ENTITIES, name2codepoint as _HTML4_NAMES
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional, Tuple

import requests
from json_repair import repair_json

from core.http_pool import pooled_get, pooled_post
from core.tool_traits import reads_persisted_state
from integrations.service_tools.model_catalog import ModelType

tool_logger = logging.getLogger('tool_execution')


def _bounded_observation(text, hint):
    """``text`` bounded to TOOL_OBSERVATION_MAX_CHARS for a tool result, the
    cut marked with ``hint`` (#104). The result goes to the model and, through
    the group chat's write-back, into memory."""
    from core.constants import TOOL_OBSERVATION_MAX_CHARS
    from core.token_utils import bound_text
    return bound_text(text, TOOL_OBSERVATION_MAX_CHARS, f'\n...[cut; {hint}]')


def _one_page(value, offset, next_call):
    """One page of a long text for a tool result, read the way book pages
    are (#104): TOOL_OBSERVATION_MAX_CHARS from ``offset``, and when more
    follows, a note naming the call that reads the next page (``next_call``,
    e.g. "get_data_by_key with ", followed by "offset=<n>")."""
    from core.constants import TOOL_OBSERVATION_MAX_CHARS as page_chars
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    if start and start >= len(value):
        return f'...[offset {start} is past the end of the value ({len(value)} chars)]'
    page = value[start:start + page_chars]
    end = start + len(page)
    if end >= len(value):
        return page
    return (f'{page}\n...[chars {start}-{end} of {len(value)}; call '
            f'{next_call}offset={end} for the rest]')


#: What a list of the files a person gave the agent says about itself.
_GIVEN_FILES_NOTE = (
    'The files this person gave you, newest first. file_id is the id to save '
    'or to pass to a tool that takes a file; purpose is what they sent it as; '
    'sent_for is the ask_id of the file_request it answered, or the chat '
    'message it came with.')


def _given_files_page(files, offset, at):
    """One page of get_user_uploaded_file's list of the files a person gave
    the agent (StoredFile dicts, newest first): whole files only, as many as
    fit TOOL_OBSERVATION_MAX_CHARS with at least one, and while more follow,
    a note naming the offset of the next page.  ``offset`` counts files.
    _one_page cuts by characters and would split a file's entry (review of
    the 2026-11-26 design, hartos-17).  ``at`` writes a time for the model."""
    from core.constants import TOOL_OBSERVATION_MAX_CHARS as page_chars
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    if start >= len(files):
        return (f'...[offset {start} is past the last of the {len(files)} files; '
                f'call get_user_uploaded_file with offset=0 for the first page]')
    rows = [{'file_id': f['xid'], 'name': f['name'], 'mime': f['mime'],
             'size': f['size'], 'purpose': f['purpose'], 'sent_at': at(f['created_at']),
             'sent_for': f.get('request_id')} for f in files]

    def page(n):
        end = start + n
        note = _GIVEN_FILES_NOTE
        if end < len(rows):
            note += (f' {len(rows) - end} more: call get_user_uploaded_file with '
                     f'offset={end}.')
        return json.dumps({'count': len(rows), 'files': rows[start:end], 'note': note})

    shown = 1
    while start + shown < len(rows) and len(page(shown + 1)) <= page_chars:
        shown += 1
    return page(shown)


def _bounded_recall(contents, max_items, skip_oversize=True):
    """Recalled memories joined for a tool result, within one budget.

    Both legs of search_long_term_memory use it. On the MemoryGraph leg
    (``skip_oversize``) a row longer than MEMORY_ITEM_MAX_CHARS is skipped,
    not cut: every graph write is bounded to that since #104, so a longer row
    predates the bound. Rows like it (whole data stores, written back and
    recalled again) grew Guardian Convergence's graph to 28.6M chars and one
    recall to 3,386,616, and they rank high on any query because they hold so
    many terms. SimpleMem's item is an answer, not a stored row, so it is cut.
    """
    from core.constants import MEMORY_ITEM_MAX_CHARS, TOOL_OBSERVATION_MAX_CHARS
    from core.token_utils import bound_text
    picked, used, skipped = [], 0, 0
    for c in contents:
        if not isinstance(c, str) or not c.strip():
            continue
        if skip_oversize and len(c) > MEMORY_ITEM_MAX_CHARS:
            skipped += 1
            continue
        room = TOOL_OBSERVATION_MAX_CHARS - used
        if len(picked) >= max_items or room < 40:
            break
        piece = bound_text(c, room)
        picked.append(piece)
        used += len(piece)
    if skipped:
        tool_logger.info(f'[RECALL-BOUND] skipped {skipped} memory row(s) '
                         f'over {MEMORY_ITEM_MAX_CHARS} chars')
    return '\n'.join(picked)


# ---------------------------------------------------------------------------
# Generic registration helper
# ---------------------------------------------------------------------------

_INTERNAL_ERROR_MARKERS = ('context size', 'server_error', 'error code: 500',
                           'traceback', 'connection', 'timed out', 'timeout',
                           'memory slot')


def user_facing_error(e):
    """ONE user-facing string for an internal failure (#716).

    Three sites (reuse get_agent_response, create get_response_group,
    gather_agentdetails) returned raw exception text as the reply, so
    TTS spoke lines like 'Context size has been exceeded' to the user
    (observed live 2026-08-31, probe run 4).  Full tracebacks are
    already logged at every call site - the reply only needs to be
    honest and speakable.
    """
    text = str(e)
    low = text.lower()
    if any(m in low for m in _INTERNAL_ERROR_MARKERS) or len(text) > 200:
        return _SNAG_REPLY
    return f"{_COULD_NOT_FINISH_PREFIX}{text[:160]}"


# The two shapes user_facing_error() produces, named once so the code that has
# to RECOGNISE a failed turn reads the same strings the code that writes them
# does.  Changing the wording here changes both.
_SNAG_REPLY = ("I hit an internal snag finishing that - please try "
               "again in a moment.")
_COULD_NOT_FINISH_PREFIX = "I couldn't finish that: "
#: What a conversation that only grows (the plan writer's) answers when the
#: model refuses it as longer than its context (llama.cpp: "request (N
#: tokens) exceeds the available context size").  Sent again it is refused
#: again, so it says so instead of _SNAG_REPLY's "try again", and the gather
#: turn reads it as the overflow it is (hart_intelligence_entry
#: ._read_gather_config).  A failed turn by its prefix.
CONTEXT_FULL_REPLY = (f"{_COULD_NOT_FINISH_PREFIX}this conversation has grown "
                      f"longer than the model can read at once.")
#: What the plan writer answers when its model stopped at its output limit
#: (finish_reason 'length'): the reply was cut off, so its text is not kept
#: (hartos.gather_agentdetails.get_agent_response).  The gather sites ask the
#: writer once more; a second cut is this sentence to the person, and nothing
#: is saved.  A cut plan repaired into valid JSON was saved as complete with
#: flows missing (2026-11-24, the merged speech agent: 10 flows asked, the
#: plan cut at 2048 tokens).  A failed turn by its prefix.
PLAN_TOO_LONG_REPLY = (f"{_COULD_NOT_FINISH_PREFIX}the plan was too long to "
                       f"write in one reply, so nothing was saved. Ask for "
                       f"fewer flows or shorter steps, then send it again.")


def is_user_facing_error(reply) -> bool:
    """True when ``reply`` is a failed turn dressed as an answer.

    A turn that fails does not raise to its caller: user_facing_error() turns
    the exception into a polite, speakable sentence and the pipeline returns
    it as the reply, and hart_intelligence_entry does the same with
    LLM_LOADING_REPLY / LLM_GENERIC_ERROR_REPLY, and create_recipe with
    BUILD_INCOMPLETE_REPLY when an agent build ends without its recipe.  That
    is right for a person reading it and wrong for any caller that has to
    decide whether WORK was done.  Measured on central 2026-09-13: the
    distributed worker submitted "I couldn't finish that: Error code: 429 -
    ... rate_limit_exceeded ..." as a hive task's result, and the coordinator
    marked the task completed; later the same day Hive Model Trainer's task
    was completed with BUILD_INCOMPLETE_REPLY.

    Recognises exactly the strings this codebase emits for a failure, by
    reference to where they are defined, so rewording one cannot silently stop
    this check from matching it.
    """
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    if text == _SNAG_REPLY or text.startswith(_COULD_NOT_FINISH_PREFIX):
        return True
    from core.constants import (
        BUILD_INCOMPLETE_REPLY, LLM_GENERIC_ERROR_REPLY, LLM_LOADING_REPLY)
    # The whole sentence as a prefix: what follows it cannot turn a failed
    # build into work, and nothing shorter is matched.
    if text.startswith(BUILD_INCOMPLETE_REPLY.strip()):
        return True
    return text in (LLM_LOADING_REPLY.strip(), LLM_GENERIC_ERROR_REPLY.strip())


def is_help_pause(reply) -> bool:
    """True when ``reply`` says the turn's action was handed to a person or an
    expert (create_recipe._ask_for_help on an autonomous run).

    Neither a result nor a failure: the action is held, the goal is parked or
    handed to the expert, and the reply is the notice.  A caller deciding
    whether work was done (the hive worker) must not record it as a
    completion, and must not release it for a retry either, since that would
    run a paused goal.  Recognised by reference to the prefixes in
    core.constants so rewording one cannot silently stop this check.
    """
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    from core.constants import HELP_EXPERT_REPLY_PREFIX, HELP_PAUSED_REPLY_PREFIX
    return text.startswith((HELP_PAUSED_REPLY_PREFIX, HELP_EXPERT_REPLY_PREFIX))


def user_pause_reply(action_id=None) -> str:
    """The sentence a background turn answers when it stops for the user.

    Owner ruling 2026-10-04: yield-to-user applies to every background agent
    (the VLM loop, autogen group chats, daemon goals), pause all, resume
    idempotently when the user is actually idle.  Written here, beside the
    predicate that recognises it, so a reworded sentence cannot stop being
    recognised.
    """
    from core.constants import PAUSED_FOR_USER_REPLY_PREFIX
    step = f' step {action_id}' if action_id else ''
    return (f"{PAUSED_FOR_USER_REPLY_PREFIX} the owner is using this computer, "
            f"so{step} stopped between steps; it resumes when they are idle.")


def is_user_pause(reply) -> bool:
    """True when ``reply`` is user_pause_reply: a background turn stopped for
    the user.  Neither a result nor a failure nor a parked goal.  The daemon
    counts no failure and keeps the goal active; the hive worker releases the
    task for a later claim; nobody records it as work done.  Recognised by
    the prefix in core.constants, never by matching the sentence."""
    if not isinstance(reply, str):
        return False
    from core.constants import PAUSED_FOR_USER_REPLY_PREFIX
    return reply.strip().startswith(PAUSED_FOR_USER_REPLY_PREFIX)


def held_for_another_turn_reply(kept=False) -> str:
    """The sentence a turn answers when it did not start because another turn
    of the same conversation is still running (core.constants
    .HELD_FOR_ANOTHER_TURN_REASON).  It asks for the same message again: the
    held turn read nothing, and its message is in no history (review of
    0ad43fef4).  Written here, beside the predicate that recognises it.

    ``kept``: the person's message was kept for the conversation's next turn
    (hartos.helper.admit_turn), which reads it once the running turn ends and
    sends its answer as a new message, so the sentence says that and asks for
    nothing.  Owner 2026-11-26 on "Please send the same message again": "it
    shd be automatically associated and understood in n context".  Same
    marker either way, so every reader of a hold reads this one as held."""
    from core.constants import (HELD_FOR_ANOTHER_TURN_REASON,
                                HELD_FOR_ANOTHER_TURN_REPLY_PREFIX)
    if kept:
        return (f"{HELD_FOR_ANOTHER_TURN_REPLY_PREFIX} {HELD_FOR_ANOTHER_TURN_REASON}. "
                f"I have this message too; my answer to it comes as a new message "
                f"once that one is done.")
    return (f"{HELD_FOR_ANOTHER_TURN_REPLY_PREFIX} {HELD_FOR_ANOTHER_TURN_REASON}, "
            f"so this message was not read. Please send the same message again "
            f"once that answer arrives.")


def kept_messages_read_after_an_unfinished_turn_reply() -> str:
    """What the person is told, before anything else, when the messages they
    sent while the agent was answering are read after that answer's turn
    ended without finishing (hartos.helper._run_the_waiting_turn found its
    thread gone; review rv_303 round 2 S1).  Owner 2026-11-28: the agent
    says what it is going to do before doing it, "rather than leaving the
    user at search of what is happening".  Not a held line: the kept
    messages are being read."""
    return ("Sorry, I could not finish my answer to your earlier message. "
            "Here is my answer to what you said since.")


#: Why messages kept while the agent was answering could not be read when
#: their turn came, by the status their replayed /chat ended with, for a
#: refusal that carries no words of its own (kept_messages_not_read_reply).
_NOT_READ_BECAUSE = {
    401: "this device's sign-in is no longer accepted here. Please sign in "
         "again and send it again.",
    403: "this device's sign-in is no longer accepted here. Please sign in "
         "again and send it again.",
    413: "it was too long to read in one go. Please send it again in shorter "
         "parts.",
    429: "too many messages came in at once. Please send it again in a minute.",
    503: "something it needs is busy right now. Please send it again in a minute.",
}


def kept_messages_not_read_reply(status, reason=None):
    """What the person is told when the messages they sent while the agent
    was answering could not be read when their turn came: the replayed /chat
    was refused by the API gate (a phone un-paired or still waiting for the
    owner, a token expired while they waited) or ended busy, rate limited or
    too long (review rv_303 rounds 3 and 4).  Owner 2026-11-09: every
    refusal says what was really wrong, so ``reason``, the refusal's own
    person-readable ``message``, is said as given; otherwise the status's
    plain reason.  None for a status that is none of these, whatever else
    the reply carries."""
    because = _NOT_READ_BECAUSE.get(status)
    if because is None:
        return None
    if isinstance(reason, str) and reason.strip():
        return f"Sorry, I couldn't read what you sent while I was busy. {reason.strip()}"
    return f"Sorry, I couldn't read what you sent while I was busy: {because}"


def still_answering_an_earlier_message_reply() -> str:
    """What the person is told, once, when the turn their newer messages
    wait for is still working at the age a person's turn is waited on
    (hartos.helper._run_the_waiting_turn; review rv_303 round 2 S1).  It
    promises no "now": that turn is still running, and its answer comes
    first."""
    return ("I'm still working on your earlier message. I'll get to what you "
            "said next as soon as it's done.")


def is_held_for_another_turn(reply) -> bool:
    """True when ``reply`` is held_for_another_turn_reply: the turn did not
    run.  Neither a result nor a failure: the daemon keeps the goal (and an
    escalation to the expert) for its next tick with no strike, and the hive
    worker defers the task.  Recognised by the prefix in core.constants."""
    if not isinstance(reply, str):
        return False
    from core.constants import HELD_FOR_ANOTHER_TURN_REPLY_PREFIX
    return reply.strip().startswith(HELD_FOR_ANOTHER_TURN_REPLY_PREFIX)


def held_reply_as_the_person_reads_it(reply):
    """``reply`` as the person reads and hears it: a held reply without its
    marker (HELD_FOR_ANOTHER_TURN_REPLY_PREFIX), which is for the readers
    that hold the turn (the daemon, the hive worker, the resumed build's
    reply) and never for the person (review of ba153f51d: "Held for another
    turn:" was shown and spoken).  Any other reply is returned unchanged."""
    if not is_held_for_another_turn(reply):
        return reply
    from core.constants import HELD_FOR_ANOTHER_TURN_REPLY_PREFIX
    said = reply.strip()[len(HELD_FOR_ANOTHER_TURN_REPLY_PREFIX):].strip()
    return said[:1].upper() + said[1:]


def is_a_persons_request(request_id) -> bool:
    """Whether ``request_id`` is a person's request: dispatch
    .is_genuine_user_request, the sole authority on "is this a person".  A
    goal turn (daemon_<goal>), a peer agent's A2A turn (daemon_a2a_<task>),
    '' and an unreadable classifier are not.  The one copy of the rule:
    REUSE's camera window (_reuse_request_is_a_person) reads it too."""
    try:
        from integrations.agent_engine.dispatch import is_genuine_user_request
        return bool(is_genuine_user_request(str(request_id or '')))
    except Exception:
        return False


def camera_turn_id() -> str:
    """The id of the turn a camera or screen tool runs in: what
    with_llm_context bound for the running chat_agent / recipe() turn
    (core.llm_outbound_logger.turn_request_id), '' when none runs.  Never
    request_id_list, which holds the session's LAST /chat id: a scheduled
    run read the person's id there for as long as the schedule kept the
    cached entry fresh, a goal turn gained the camera when the person's
    message arrived mid-turn, and the person's own turn lost it when a goal
    turn started (review of efd223acf, findings 1-3)."""
    from core.llm_outbound_logger import turn_request_id
    return str(turn_request_id() or '')


def turn_may_see_the_camera() -> bool:
    """True only inside a person's own turn: the one turn that may read what
    that person's camera and screen show (the live frame, the caption window,
    the caption search).  The running turn's own id, by the person rule
    (is_a_persons_request).  A scheduled run, an agent-to-agent relay and a
    worker thread run in no chat_agent / recipe() turn, so their id is ''.

    Review of HARTOS 23ccd0eb3 (critical): once that commit made these tools
    read Nunba's stamps, a peer's A2A turn got the owner's camera and screen
    captions through them."""
    return is_a_persons_request(camera_turn_id())


#: How far ahead of a visual scheduled task its person's camera is wanted:
#: the task (reuse_recipe.call_visual_task) works from the captions of the
#: last 5 minutes.  A visual task that fires at least this often keeps the
#: camera always on: that is how an agent asks for "always on".
VISUAL_TASK_LEAD_S = 300


def camera_wanted(user_id) -> bool:
    """True while an agent needs ``user_id``'s camera, by what HARTOS already
    keeps (owner 2026-11-21: "if none of the agents background or foreground
    agent needs it then it can be stopped and started by the agent which
    needs captions or vision or always on ... decided either at agent
    creation time or at reuse time by agents"):

      - at creation time, a visual scheduled task of that person
        (reuse_recipe.create_schedule's ``visual_task_<user>_<prompt>_<n>``
        jobs) is running, or runs within VISUAL_TASK_LEAD_S;
      - at reuse time, a visual turn of that person that holds the camera
        runs, or a camera tool of theirs waits for a frame
        (reuse_recipe.camera_held_for, the one read of the visual-turn
        marks; a turn the 2 s poll started reads captions and holds no
        camera), or a visual watcher of theirs has not expired
        (hart_intelligence_entry._active_watchers).

    VisionService asks it whether to tell that person's page to free or take
    the camera.  It reads modules already loaded and imports none: a process
    without REUSE or the entry module holds no such need."""
    uid = str(user_id)
    rr = sys.modules.get('hartos.reuse_recipe')
    if rr is not None:
        held = getattr(rr, 'camera_held_for', None)
        if held is not None and held(uid):
            return True
        scheduler = getattr(rr, 'scheduler', None)
        if scheduler is not None:
            now = datetime.now(timezone.utc)  # next_run_time is tz-aware
            prefix = f'visual_task_{uid}_'
            # APScheduler counts each job's running instances per executor.
            # 3.x (pinned 3.11.2) has no public read of that count, so its
            # private names are read through getattr: a version that renames
            # them counts no job as running, and the due time still holds.
            executors = list((getattr(scheduler, '_executors', None) or {}).values())
            for job in scheduler.get_jobs():
                if not str(job.id).startswith(prefix):
                    continue
                if any((getattr(ex, '_instances', None) or {}).get(job.id)
                       for ex in executors):
                    return True
                due = job.next_run_time
                # astimezone: a time with no zone is local time, and
                # subtracting it from an aware one raised (review of
                # 1fef56008).
                if due is not None and (due.astimezone(timezone.utc)
                                        - now).total_seconds() <= VISUAL_TASK_LEAD_S:
                    return True
    hie = sys.modules.get('hart_intelligence_entry')
    if hie is not None:
        wall = time.time()
        for who, watchers in list(getattr(hie, '_active_watchers', {}).items()):
            if str(who) == uid and any(
                    w.get('modality') in ('visual', 'both')
                    and wall < w.get('expires_at', 0) for w in watchers):
                return True
    return False


def recall_runs_on_a_background_turn() -> bool:
    """Whether a recall runs on a goal or peer turn: dispatch
    .is_current_request_autonomous, asked about the turn's own id.  That is
    camera_turn_id (what with_llm_context binds for recipe() / chat_agent())
    when one is bound, else the thread-local request id /chat set for its own
    thread, which the owner reads itself.  LangChain chat binds no turn
    context: get_ans runs its tools on the /chat thread, after /chat set that
    id and before teardown_request clears it (review of 641d7e289, M2).

    The bound id first, never llm_outbound_logger._get_request_id: that reads
    the thread-local first, which names the outer /chat request when a turn
    with its own id runs on that thread (review of efd223acf, finding 5).

    A person's turn is not a background turn, and neither is a run with no id
    from either source: it may be a person's own scheduled (/time_agent) or
    camera (/visual_agent) agent.  The one rule free_text_recall and
    get_chat_history read.  It used to carry its own copy of the owner's test
    (a non-empty id that is not a person's) and of its thread-local read."""
    from integrations.agent_engine.dispatch import is_current_request_autonomous
    return is_current_request_autonomous(camera_turn_id())


def free_text_recall(graph, query, top_k, mode='hybrid', since=None, until=None):
    """The MemoryGraph nodes a free-text recall tool answers from.

    search_long_term_memory and recall_memory both call it, and both answer
    nothing_recalled_on_this_turn when it returns none, so the step carries
    on.  Some-words matching (partial_match), as both asked for.

    On a goal or peer turn (recall_runs_on_a_background_turn) it leaves out 'conversation' rows: in the graph they are that
    turn's own prompt, replies and loop steers.  Live on central 2026-11-20
    01:07-01:16Z, goal 74416785's 28 recalls of 'saved facts' returned
    exactly those, and the step paused needs_help after 3 attempts
    (tests/unit/test_a_goal_recall_reads_saved_facts_not_its_own_talk.py).
    A person's turn, and a run with no id from either source, keep them.
    """
    # Unlike get_user_uploaded_file, no id here is not "no person": an
    # id-less run may be a person's own scheduled (/time_agent) or camera
    # (/visual_agent) agent, and the conversation it recalls is hers.  An
    # upload needs a person present; a recall of her own talk does not.
    own_talk = ('conversation',) if recall_runs_on_a_background_turn() else ()
    return graph.recall(query, mode=mode, top_k=top_k, since=since,
                        until=until, partial_match=True,
                        exclude_types=own_talk)


def nothing_recalled_on_this_turn(query) -> str:
    """What search_long_term_memory and recall_memory answer when nothing
    saved matches ``query``: core.tool_refusal.nothing_recalled, and on this
    node's own goal turn whose goal names a folder that may be opened, also
    what that folder holds and to open the file that may hold the answer
    first (integrations.agent_engine.goal_files.goal_files_hint).  Any other
    turn gets nothing_recalled alone, as before.

    Live on central 2026-11-20 14:35Z: the rice buyer goal recalled nothing
    for its owner rules, never opened EMAIL_FACTS.md in its own files_root,
    and paused needs_help after 3 tries
    (tests/unit/test_memory_miss_names_the_goals_files.py)."""
    from core.tool_refusal import nothing_recalled
    answer = nothing_recalled(query)
    # The callers answer recall_failed ("the store could not be read") on
    # any exception, so a hint that raised would turn this read into one.
    try:
        from integrations.agent_engine.goal_files import goal_files_hint
        hint = goal_files_hint()
    except Exception as e:
        tool_logger.warning("goal files not named on a memory miss: %s",
                            type(e).__name__)
        hint = None
    return f"{answer} {hint}" if hint else answer


def camera_refusal(tool_name: str, turn_id=None) -> str:
    """What a camera or screen tool answers on a turn that is not the
    person's own: a core.tool_refusal refusal (the reply gates read it as a
    failed call, so no step is credited with it) naming what the turn is and
    what to do.  A person's own turn is never refused, so it is never told it
    is a background one (review of efd223acf, finding 4).

    ``turn_id``: the turn's id when the caller read it somewhere other than
    the bound turn context (camera_refusal_on_a_background_turn, for the
    LangChain chat path); left out, the bound id is named as before."""
    from core.tool_refusal import RETRY_LATER, refuse
    rid = str(turn_id or '') or camera_turn_id()
    if not rid:
        what = ("this run answers no person's message (a scheduled run, or "
                "work started without one)")
    elif rid.startswith('daemon_a2a_'):
        what = "this turn is another agent's request"
    elif rid.startswith('daemon_'):
        what = "this turn works on a goal in the background"
    else:
        what = "this turn could not be confirmed as the person's own message"
    return refuse(tool_name,
                  f"{what}, and only the person's own turn may look at what "
                  f"their camera or screen shows",
                  "Do this step without what the camera or screen shows",
                  retry=RETRY_LATER)


def camera_refusal_on_a_background_turn(tool_name: str):
    """The refusal a chat-path camera tool gives on a goal or peer turn, None
    on every other turn: Visual_Context_Camera (hart_intelligence_entry
    .parse_visual_context) and the watcher tool (_handle_visual_watcher_tool,
    which REUSE's register_visual_watcher calls too).

    Those two run on the LangChain path, which binds no turn id, so
    turn_may_see_the_camera would refuse a person there as well.  The check
    is recall_runs_on_a_background_turn, the one that already tells the cases
    apart on that path: the bound id first, else the request id /chat set on
    its thread.  Without it a goal or peer turn that reached these tools
    could register a watcher that keeps the person's camera on for up to two
    hours and speaks to them, or wake the camera, send its frame to the model
    and file the owner's camera ask (review of 47f2252e5, medium).

    A person's turn, and a run with no id from either source, is not refused
    here, as that check reads them.  The refusal names the id the check read."""
    if not recall_runs_on_a_background_turn():
        return None
    from hartos.threadlocal import thread_local_data
    return camera_refusal(
        tool_name, camera_turn_id() or thread_local_data.get_request_id())


def waiting_for_person_reply(action_id) -> str:
    """The sentence a background turn answers when the session's current
    REUSE step is held for the person's reply (#161): the step asked them in
    their own turn, and this turn neither ran it nor asked again.  Written
    here, beside the predicate that recognises it, so a reworded sentence
    cannot stop being recognised."""
    from core.constants import WAITING_FOR_PERSON_REPLY_PREFIX
    return (f"{WAITING_FOR_PERSON_REPLY_PREFIX} step {action_id} asked them a "
            f"question and waits for their reply; this turn did not run it or "
            f"ask again.")


def is_waiting_for_person(reply) -> bool:
    """True when ``reply`` is waiting_for_person_reply: the turn did not run
    its step because the step waits for the person.  Neither a result nor a
    failure nor a parked goal: the daemon counts no strike and keeps the goal
    active (dispatch treats it as a transient defer), the hive worker holds
    the task (HeldForHelp), nobody records it as work.  Recognised by the
    prefix in core.constants."""
    if not isinstance(reply, str):
        return False
    from core.constants import WAITING_FOR_PERSON_REPLY_PREFIX
    return reply.strip().startswith(WAITING_FOR_PERSON_REPLY_PREFIX)


def is_action_error_reply(reply) -> bool:
    """True when ``reply`` is the CREATE pipeline's structured error envelope,
    {"status": "error", "action": ..., "action_id": ..., "message": ...} — the
    format create_recipe's prompt tells an agent to return when an action
    failed and self-heal did not work.  Like a help pause, an error is not a
    result: a caller deciding whether work was done must not record it as one.
    Measured 2026-09-15: the daemon counted these as successful dispatches, so
    a continuous goal whose every run ended in this envelope re-ran every
    5 minutes for five months (53,949 copilot sessions)."""
    if not isinstance(reply, str):
        return False
    text = reply.strip()
    if not text:
        return False
    if text.startswith('{'):
        try:
            import json
            d = json.loads(text)
            if isinstance(d, dict):
                return str(d.get('status', '')).lower() == 'error'
        except ValueError:
            pass
    # The envelope with prose around it: both protocol keys present.
    import re
    return '"action_id"' in text and re.search(r'"status"\s*:\s*"error"', text) is not None


def is_build_stop_reply(reply) -> bool:
    """True when a build's reply says it stopped and why: a pause for the
    person, a turn held for another, an error the person reads, a help pause,
    or the CREATE error envelope.  Such a reply is shown as written, is never
    counted as work, and is never hidden behind a progress notice.  One list
    for every site that asks (review of HARTOS cfa9e29ee: a second copy left
    out the error envelope and showed a failed build as "being completed")."""
    return (is_user_pause(reply) or is_held_for_another_turn(reply)
            or is_user_facing_error(reply) or is_help_pause(reply)
            or is_action_error_reply(reply))


# The tool menu, schema fitting, registration and runtime attach live in
# core.agent_tool_menu (split out when this module crossed the 3000-line
# god-module ratchet).  Re-exported so every `from core.agent_tools import X`
# and every `core.agent_tools.X` attribute read keeps working unchanged.
from core.agent_tool_menu import (  # noqa: E402,F401 -- re-export
    CODING_TOOL_NEEDS, CREATE_ADVERTISED_TOOLS, CREATE_LEG_EXTRA_TOOLS,
    MAIN_LEG_CORE_TOOLS, REQUEST_TOOLS_DESCRIPTION, attach_for_names,
    attach_for_tags, withheld_coding_tools,
    create_helper_keep, defer_helper_schema, discover_and_attach,
    filter_service_tools, fit_schema_to_ctx, helper_tool_names,
    main_leg_core_tools, main_leg_tool_menu, register_core_tools,
    register_dual, register_request_tools, registered_tool_menu,
    _NEED_STOPWORDS, _attach_tool, _join_tool_menu, _need_names_tool,
    _schema_or_none,
)


# ---------------------------------------------------------------------------
# Core tool closure factory
# ---------------------------------------------------------------------------

# Neutral fallback receipt (#752) used until the user accepts a custom template.
# The accept-once flow overrides it via save_data_in_memory('receipt_template').
# {var} placeholders are filled by the existing TemplateEngine.
_DEFAULT_RECEIPT_TEMPLATE = (
    "RECEIPT\n"
    "{business_name}\n"
    "Date: {date}\n"
    "Received from: {client_name}\n"
    "For: {service}\n"
    "Event timing: {event_timing}\n"
    "Amount: {currency} {amount}\n"
    "Advance received: {currency} {advance}\n"
    "Balance due: {currency} {balance}\n"
    "{notes}\n"
    "Thank you for your business."
)


from core.game_sound_memo import (  # noqa: E402
    GAME_STATE_DURATIONS,
    GAME_STATES,
    ended_composition,
    forget_game_state_record,
    game_state_key,
    game_sound_action,
    game_state_match,
    game_state_record,
    game_state_sound,
    holds_composition,
    record_verdict,
    rejected_take,
    set_game_state_sound,
    set_game_state_sound_at,
    submit_in_flight,
    submit_note,
    take_variant,
    task_labels,
    without_submit_note,
)


#: How long a timed-out submit is assumed to still be queued server-side
#: before this client will submit the same state again -- owned by the memo
#: module, which answers "composing" with it for the node's route as well.
from core.game_sound_memo import SUBMIT_COOLDOWN_S  # noqa: E402,F401

#: A task older than this is taken as lost, not slow.  AceStep keeps its job
#: store in memory, so a restart forgets every id, and it answers a forgotten
#: id exactly as it answers a queued one (hartos-3a F2).  The slowest job
#: MEASURED on a shared GPU averaged 907 s; twice that is past any real job.
TASK_STALE_S = 1800


def offer_sound_for_review(user_id, prompt_id, game_id, state, record):
    """Put a newly composed game sound in front of the person, to hear.

    Creation is meant to be liquid: the reviewer hears the piece and
    answers it on the surface they are already looking at, rather than
    being told a URL.  This rides the existing agent-to-UI channel
    (LiquidUIService.agent_ui_update), which is allow-listed, audited and
    delivered to web, phone and desktop alike.

    Best-effort by design: a node without that service, or a hive the
    human has halted, must not stop a sound being composed and memoized.
    Returns True when the card itself was accepted for delivery -- and the
    person is told on their other surfaces either way, because a node with
    no screen attached is precisely when the phone matters most.
    """
    shown = False
    try:
        from core.platform.registry import get_registry
        service = get_registry().get('LiquidUIService')
        if service is not None:
            # The audio FIRST, as the declared 'media' component every
            # client already renders (props: type, src, alt, controls).  It
            # used to ride inside the approval card as an undeclared prop,
            # which no client reads -- so the card invited someone to "have
            # a listen" and gave them nothing to listen to.  'media_type'
            # as well as 'type' because the component's own type key is
            # 'media' and the clients read the modality from media_type.
            if record.get('url'):
                service.agent_ui_update(user_id, {
                    'type': 'media',
                    'agent_id': str(prompt_id),
                    'media_type': 'audio',
                    'src': record.get('url'),
                    'controls': True,
                    'alt': f'{state} sound for {game_id}',
                    'title': f'{state} sound for {game_id}',
                }, user_id=user_id)
            shown = bool(service.agent_ui_update(user_id, {
                'type': 'approval',
                'agent_id': str(prompt_id),
                'action': game_sound_action(game_id, state),
                'description': (
                    f"New {state} sound for {game_id}. Have a listen: keep "
                    f"it, or say what is wrong and I will compose another."
                ),
                'options': ['Keep it', 'Compose another'],
            }, user_id=user_id))
    except Exception as e:
        # never at the cost of the composition that just succeeded
        tool_logger.debug(f'game sound: no card on screen ({e})')
    if not shown:
        # Only when the card did NOT reach a screen.  A game has fourteen
        # states, so notifying regardless meant one game cost the person
        # fourteen phone pushes and fourteen unread rows -- for sounds they
        # were already being shown one by one.  'shown' was computed and
        # thrown away; it is the whole signal for whether they need telling
        # somewhere else.
        _tell_the_person_elsewhere(user_id, prompt_id, game_id, state, record)
    return shown


def _tell_the_person_elsewhere(user_id, prompt_id, game_id, state, record):
    """Reach the person who is not looking at the screen it was offered on.

    The card above lands where they are logged in; a sound composed while
    they are away from that screen would otherwise wait unheard.  This is
    the same pair the consent ask already uses
    (integrations/social/device_routing_service): a notification record,
    which every surface of theirs shows, and an FCM push to the phone.
    Both are best-effort and both no-op cleanly on a node with no push
    credential.
    """
    message = f"A new {state} sound for {game_id} is ready for you to hear."
    try:
        from integrations.social.services import NotificationService
        from integrations.social.models import db_session
        with db_session() as db:
            # target_type/target_id are the schema's own way of saying what
            # a notification is ABOUT, and the client routes on them.  Without
            # them the row is inert: it tells the person a sound is ready and
            # gives them no way to reach it.
            NotificationService.create(
                db, str(user_id), 'agent_game_sound_review',
                source_user_id=str(prompt_id), message=message,
                target_type='agent', target_id=str(prompt_id),
            )
    except Exception as e:
        tool_logger.debug(f'game sound: no notification record ({e})')
    try:
        from core.fcm_sync import send_fcm_push
        send_fcm_push(
            str(user_id),
            'A new game sound',
            message,
            data={
                'type': 'game_sound_review',
                'agent_id': str(prompt_id),
                'game_id': str(game_id),
                'state': str(state),
                'url': str(record.get('url') or ''),
                'topic_reply': f'com.hertzai.pupit.{user_id}',
            },
        )
    except Exception as e:
        tool_logger.debug(f'game sound: no push to the phone ({e})')


def answer_game_sound_card(agent_id, action, approved, reason, approver_id):
    """A person's answer to the card offer_sound_for_review raised.

    /api/agent/approval hands it here with the caller's VERIFIED identity
    (never a body field).  The agent's creator answers for the agent;
    anyone else's answer is their own choice, written in their own store,
    and the agent's take stays as it is for everyone else (owner
    2026-10-17).  The verdict itself is core.game_sound_memo.record_verdict,
    as for the agent's own approve_game_sound.  Returns (body, status).

    The card is answered outside any chat turn, so it reads and writes the
    store files directly (core.cache_loaders, atomic) and then has every
    pipeline cache drop its copy: a live session still holding the old
    store kept serving it and wrote it back over this verdict on its next
    save (review of c48e90601, F2).
    """
    from core.cache_loaders import (
        forget_cached_store, load_agent_data, save_agent_data)
    from core.game_sound_memo import parse_game_sound_action, record_verdict
    verdict = 'approved' if approved else 'rejected'
    if not approver_id:
        from integrations.social.auth import log_refused_credential
        log_refused_credential()
        return {'status': 'error', 'action': action, 'applied': False,
                'reason': 'sign in to answer this'}, 401
    game_id, state = parse_game_sound_action(action)
    pid = int(agent_id)
    agent_store = load_agent_data(pid) or {}
    games = agent_store.setdefault('games', {})
    for_everyone = is_agent_creator(approver_id, pid)
    if for_everyone:
        store_id, store = pid, agent_store
        record, matched, key = record_verdict(
            games, game_id, state, approved, reason)
    else:
        store_id = person_store_for(approver_id, pid)
        store = load_agent_data(store_id) or {}
        record, matched, key = record_verdict(
            games, game_id, state, approved, reason,
            own=store.setdefault('games', {}))
    saved = bool(record) and save_agent_data(store_id, store)
    if saved:
        forget_cached_store(store_id)
    elif record:
        tool_logger.warning(
            f'game sound card: agent={agent_id} {verdict} {game_id}/{state} '
            f'was not saved; nothing changed')
    tool_logger.info(
        f'game sound card: agent={agent_id} {verdict} {game_id}/{state} at '
        f'memo {key} for {"everyone" if for_everyone else "the answerer only"}')
    return {'status': verdict, 'action': action, 'applied': saved,
            'key': key, 'matched': matched,
            'scope': 'agent' if for_everyone else 'mine'}, 200


def _coding_workdir(working_dir):
    """The directory a coding tool was handed, else HEVOLVE_CODING_WORKDIR.

    Empty means "resolve it": vlm_adapter.resolve_task_workspace then takes
    the agent's goal repo_path, else the user-data coding workspace, never
    the process cwd (the install folder on the desktop).  One rule for
    every coding tool an agent is given.
    """
    return working_dir or os.environ.get('HEVOLVE_CODING_WORKDIR', '')


def _overlay(shared, own):
    """``own`` laid over ``shared``, key by key down nested dicts, as a new
    dict; neither argument is changed.  Where both hold a dict the two
    merge, otherwise ``own`` wins."""
    view = dict(shared)
    for key, value in own.items():
        if isinstance(value, dict) and isinstance(view.get(key), dict):
            view[key] = _overlay(view[key], value)
        else:
            view[key] = value
    return view


def person_store_id(user_prompt):
    """The store id of one person's memory for one agent: the session key
    itself when it is safe as a file name (core.cache_loaders.is_safe_store_id)
    and lower case, else a digest of it.  The id becomes a file name in
    hartos.helper, which joins it unchecked, and a user id can come from a
    request body, so '../x' must never reach a path.  Lower case only, since
    Windows and macOS file names ignore case and two ids differing by case
    would share a file.  Never in the wire trim's `elided` namespace either:
    its sweep deletes those files after a day, and a made-up id there could
    name another user's elided item (review of 7121264b1)."""
    from core.cache_loaders import is_safe_store_id
    from core.llm_outbound_logger import ELIDED_ITEM_PREFIX
    key = str(user_prompt)
    if (is_safe_store_id(key) and key == key.lower()
            and not key.startswith(ELIDED_ITEM_PREFIX)):
        return key
    import hashlib
    return 'p' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


def person_store_for(user_id, prompt_id):
    """person_store_id for a caller that holds the two ids rather than the
    session key, such as an HTTP route: the same "{user_id}_{prompt_id}"
    key the recipe pipelines build, so both reach one file."""
    return person_store_id(f'{user_id}_{prompt_id}')


def is_agent_creator(user_id, prompt_id):
    """This person's agent: whose prompts/<id>.json says it is through the
    one ownership rule, core.prompt_files.agent_account (the account that
    adopted it, else the one that built it, creator_user_id: the ownership
    ruling of 2026-10-08).  The one test of who may save a template or
    example for everyone (save_data_in_memory for_everyone), of whose
    choice of a game's sounds is the agent's (owner 2026-10-17: anyone
    else's choice is their own), and of whose own memory a resumed CREATE
    restores the saved-key snapshot into.  No recorded owner: nobody.
    (The /prompts listing's "no creator means listed for everyone" is a
    visibility rule, not this one.)"""
    from core.cache_loaders import load_agent_config
    from core.prompt_files import agent_account
    owner = agent_account(load_agent_config(prompt_id))
    return owner != '' and owner == str(user_id)


def memory_view(agent_data, prompt_id, user_prompt):
    """One person's memory for one agent, as that person's tools read it.

    Owner ruling 2026-10-17: an agent's memory belongs to the person AND the
    agent, kept under the session key ``user_prompt`` ("{user_id}_{prompt_id}",
    the key the pipelines, ledgers and memory graphs already use).  The
    memory tools write there, except a template or example the agent's
    creator saves for everyone while building it (save_data_in_memory
    for_everyone), which goes to the agent's own store under ``prompt_id``.
    That store (those templates, game sounds, anything saved before the
    ruling) stays readable to everyone, under each person's own entries.
    One store per agent let a sweep's save of ``teach`` erase the creator's
    progress on the owner's desktop on 2026-10-14
    (docs/internal/verification/USER_JOURNEYS_LIVE_2026-10-13.md 11.1).
    """
    shared = agent_data.get(prompt_id) or {}
    own = agent_data.get(person_store_id(user_prompt)) or {}
    return _overlay(shared if isinstance(shared, dict) else {},
                    own if isinstance(own, dict) else {})


def _saved_text_refusal(key, keys, store, value) -> Optional[str]:
    """The refusal of a save that would replace the text saved at ``keys`` in
    ``store`` with a flag, with an object that only wraps that same text, or
    with an object (or a list of objects) that holds the key's own name beside
    other values; None for every other save.

    A saved text keeps its kind (#282, #295).  Live on the speech agent
    95568353503 (CREATE on the local 4B, 25-26 Nov 2026): the child said
    "தமிழ்" and the model saved home_language true; the parent said "Her name
    is Zoey. At home she speaks Tamil." and it saved child_name {"child_name":
    "Zoey", "home_language": "Tamil"}.  REUSE then read a flag and an object
    where a language and a name had been, and asked the parent again.  Again
    at 20:35 IST on the repaired store: child_name {"value": "Zoey"}, one
    entry holding exactly the saved text.  Over the desktop's 463 saves of
    11-11 to 11-26 this refuses 7, and over every session's 9,121 memory-graph
    saves 35, every one a wrong write.  Text replaced by any other object, a
    list or a number stays allowed: an object that adds anything to the saved
    text (a draft gaining its content, a target word its syllable count, a tip
    its word) is a real upgrade, and a list may grow from one item.

    Never splits the object into keys: an invented record that holds its own
    key (last_attempt {"child_name": "Liam", "home_language": "English", ...})
    would then overwrite the real values beside it.  For the same reason the
    reply offers a call only for a key that holds nothing or a bare flag (the
    shape this repairs), with the model's own value; a key that already holds
    a value is named with it and left alone, and a value that is only its own
    key's name or a blank the form left unfilled ("<home_language>",
    unfilled_text_placeholder: a blank template) is never offered.  Reads
    only: the store is not touched."""
    node = store
    for part in keys[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
    saved = node.get(keys[-1]) if isinstance(node, dict) else None
    if not isinstance(saved, str) or not saved.strip():
        return None
    leaf = keys[-1]
    said = json.dumps(saved, ensure_ascii=False)
    if isinstance(value, bool):
        problem = (f"{key} holds the text {said}, and {json.dumps(value)} would "
                   f"replace it with a flag")
        fix = ("Save the value itself as text under this key, or save the flag "
               "under a key of its own")
    elif (isinstance(value, dict) and len(value) == 1
          and next(iter(value.values())) == saved):
        problem = (f"{key} holds the text {said}, and the value you sent only wraps "
                   f"that same text: {json.dumps(value, ensure_ascii=False)}")
        fix = ("The text is already saved under this key, so this value adds "
               "nothing: save anything new under a key of its own")
    else:
        wrapped = None
        if isinstance(value, dict) and leaf in value:
            wrapped = value
        elif isinstance(value, list):
            wrapped = next((v for v in value if isinstance(v, dict) and leaf in v),
                           None)
        if wrapped is None:
            return None
        prefix = '.'.join(keys[:-1]) + '.' if len(keys) > 1 else ''
        calls, held = [], []
        for k, v in wrapped.items():
            if (v == k or v in (None, '', [], {})
                    or (isinstance(v, str) and unfilled_text_placeholder(v))):
                continue
            here = node.get(k)
            if here in (None, '', [], {}) or isinstance(here, bool):
                calls.append(f'save_data_in_memory(key="{prefix}{k}", '
                             f'value={json.dumps(v, ensure_ascii=False)})')
            elif here != v:
                held.append(f'{prefix}{k} already holds '
                            f'{json.dumps(here, ensure_ascii=False)}')
        beside = ', '.join(str(k) for k in wrapped if k != leaf) or 'nothing else'
        problem = (f"{key} holds the text {said}, and the value you sent holds "
                   f"{leaf} again inside it, beside {beside}")
        parts = []
        if calls:
            parts.append(f"Save each value under its own key: {'; '.join(calls[:8])}")
        if held:
            parts.append(f"{'; '.join(held[:8])}: each stays as it is unless the "
                         f"person says otherwise")
        fix = ('. '.join(parts) if parts else
               "It holds no new value, only blanks or what is saved already: "
               "save nothing here")
    from core.tool_refusal import refuse
    return refuse('save_data_in_memory', problem, fix)


def unfilled_key_placeholder(key) -> Optional[str]:
    """The first ``<...>`` still left in a memory key, or None.

    A placeholder is a '<', then something other than spaces, then the next
    '>': the shape a saved step uses for "fill this in" ("teach.<that same
    user id>").  It is found anywhere in the key, because the model copies
    it in more than one shape: whole ('teach.<user_id>'), holding a dot
    ('teach.<user.id>', twice on 10-10, each read answered by a loose match
    as if it were the record) and glued to text ('<experiment_id>_state',
    agent 29023956042's saved step).  A lone '<' or '>' and an empty '<>'
    are not placeholders.  This reads the tool's own argument, not the
    model's words.  Measured 2026-11-09 by the change's reviewer: all 48
    logged placeholder calls have this shape, and no stored key outside the
    incident's own record does.

    Measured live 2026-11-07 14:12 IST (request msg_1794040636536_3y7gjm,
    agent 54): save_data_in_memory(key='teach.<user_id>') stored the
    learner's progress under the literal part '<user_id>', not teach.10202,
    and the fabrication gate credited the save because the tool reported
    success.  Read shape: get_data_by_key('teach.<d68c9dee-...>') on
    2026-10-14 23:10, the real id in brackets.
    """
    text = str(key or '')
    start = text.find('<')
    while start != -1:
        end = text.find('>', start + 1)
        if end == -1:
            return None
        inner = text[start + 1:end]
        if '<' in inner:                 # the last '<' before it opens the pair
            start = text.rfind('<', start + 1, end)
            continue
        if inner.strip():
            return text[start:end + 1]
        start = text.find('<', end + 1)
    return None


# A fill-in mark left in a message for a person: an identifier joined by '_'
# inside <> or [] ('<parent_name>', '[REVENUE_AMOUNT]'), not glued to a word
# before it (my_list[item_index], std::vector<my_type> are code) and, in [],
# not the text of a markdown link ([click_here](url)).
_ANGLE_BLANK = r'<[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+>'
_SQUARE_BLANK = r'\[[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\]'
_TEXT_PLACEHOLDER = re.compile(rf'(?<!\w)(?:{_ANGLE_BLANK}|{_SQUARE_BLANK}(?!\())')
#: The same two shapes in an email's subject line, which is never code nor
#: markdown: glued to a word ("Hi<first_name>", "x[A_B]") or before a "(",
#: each is still a blank the reader sees (review rv_fc0c).
_SUBJECT_PLACEHOLDER = re.compile(f'{_ANGLE_BLANK}|{_SQUARE_BLANK}')


#: A template blank in braces: {POSTAL ADDRESS}, {{first_name}},
#: {product_keyword}, {SAVE10}.  Only a word inside braces, so CSS
#: ({color: red}) is not one.  A name may hold dots and hyphens after its
#: first letter, as a template writes a field: {{ user.first_name }},
#: {{first-name}} (review rv_c092 #2: both were sent).
_BRACE_PLACEHOLDER = re.compile(r'\{\{?\s*[A-Za-z_][A-Za-z0-9_ .-]*\s*\}?\}')
#: An HTML email is read as its reader's HTML parser builds it: libxml2,
#: through lxml (_read_email), the parser the outreach reviews judge with,
#: so no hand-written tag rule reads a shape the parser reads otherwise
#: (three rejections of hand-written rules: rv_f32b rounds 1-2, rv_1a81).
#: What no reader is shown: the head, its title, styles and scripts.
_UNSHOWN_ELEMENTS = frozenset({'head', 'title', 'style', 'script'})
#: The elements a line of the reader's text starts and ends at: the HTML
#: standard's blocks, list items, table parts, a line break and a rule.
_LINE_ELEMENTS = frozenset(
    'address article aside blockquote body br caption center dd details dialog '
    'dir div dl dt fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 '
    'header hgroup hr html legend li listing main menu nav ol optgroup option p '
    'pre section summary table tbody td tfoot th thead tr ul xmp'.split())
#: A conditional comment, which Outlook reads as HTML ('<!--[if mso]> ...
#: <![endif]-->'), its own markers cut.
_CONDITIONAL = re.compile(r'\s*\[if\b', re.IGNORECASE)
_CONDITIONAL_OPEN = re.compile(r'^\s*\[if[^\]]*\]>', re.IGNORECASE)
_CONDITIONAL_CLOSE = re.compile(r'<!\[endif\]\s*$', re.IGNORECASE)
#: The underscore shape's [] form in capitals ("[DISCOUNT_CODE]",
#: "[FIRST_NAME]"): still a blank inside a code block, which the reader sees
#: as it is (review rv_fqa: "<code>[DISCOUNT_CODE]</code>" was sent).  A
#: code block's other brackets ("[user_id]", "<K_V>") are code, as is one
#: glued to a word (os.environ[API_KEY]) or a markdown link's text.
#: Lowercase blanks in code ("<code>[booking_link]</code>") are not told
#: apart from code ("[user_id]") and are still sent (parked).  A word in
#: capitals ("<code>[NAME]</code>") is a blank there too, by the owner's
#: rule (_owner_blank_in); "arr[N]" and "[1, 2]" stay code.
_CODE_BLANK = re.compile(
    r'(?<!\w)\[(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+'
    r'|(?=(?:[^\[\]A-Z\n]{0,60}[A-Z]){2})[A-Z][A-Z0-9]*(?:[ _-][A-Z0-9]+)*)\](?!\()')
#: The same blank in an email's prose, glued to a word or not: code's place
#: is a code block (review rv_52c3: "Dear[FIRST_NAME]," and "Order
#: 12[ORDER_ID]" were sent).  Lowercase ("my_list[item_index]") stays code.
_PROSE_CAPS_BLANK = re.compile(r'\[[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\](?!\()')
#: What a generic's brackets hold: identifiers, "::", dots, commas.
_CODE_LIKE = re.compile(r'[A-Za-z_][\w:.]*(?:\s*,\s*[A-Za-z_][\w:.\[\]]*)*')
#: The owner's rule for an outreach email (2026-11-23, "Block them"; review
#: rv_52c3 of 02d098595: "[NAME]", "[UPDATE]", "[First Name]", "%FIRSTNAME%"
#: were sent), read by _owner_blank_in.
#: A word in capitals in square brackets, two capitals at least ("[A]" and
#: "[Q3]" are none), spaces inside the brackets or not.  The look for two
#: capitals stays inside the pair (a run of "[" with none closed is read
#: once, not once per "[").
_SQUARE_CAPS = re.compile(
    r'\[\s*(?=(?:[^\[\]A-Z\n]{0,60}[A-Z]){2})[A-Z][A-Z0-9]*(?:[ _-][A-Z0-9]+)*\s*\](?!\()')
_CAPS_WORDS = re.compile(r'(?=(?:[^A-Z]*[A-Z]){2})[A-Z][A-Z0-9]*(?:[ _-][A-Z0-9]+)*')
_IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+')
#: A square pair that may hold a field name (three characters at least:
#: "[zip]"), and an angle pair whose words spaces pad ("< first_name>"; an
#: unpadded one is _placeholder_shaped's).
_SQUARE_PAIR = re.compile(r'\[([^\[\]\n]{3,60})\](?!\()')
_ANGLE_PADDED = re.compile(r'<(\s[^<>\n]{0,59}|[^<>\n]{0,59}\s)>')
#: A merge tag: "%FIRSTNAME%", "%first_name%", Mailchimp's "*|FNAME|*"; not
#: a link's percent-encoding ("caf%C3%A9s%C3%A0": a '%' and two hex digits
#: before the tag, or two hex digits after it).
_MERGE_TAG = re.compile(
    r'(?<!%[0-9A-Fa-f]{2})%[A-Za-z][A-Za-z0-9_]{0,40}%(?![0-9A-Fa-f]{2})'
    r'|\*\|[A-Za-z][\w:]{0,40}\|\*')
#: Common field names a template leaves in brackets (_field_name), in lower
#: case with single spaces: each alone or after one of _FIELD_LEADS
#: ("[Company]", "[Your Company]", "[Client City]").
_FIELD_NAMES = frozenset((
    'firstname', 'lastname', 'fullname', 'fname', 'lname', 'surname', 'username',
    'company', 'business', 'organization', 'organisation', 'clinic', 'school',
    'hospital', 'practice', 'brand', 'agency', 'position', 'role', 'designation',
    'department', 'phone', 'mobile', 'whatsapp', 'city', 'town', 'state', 'country',
    'region', 'location', 'zip', 'zipcode', 'zip code', 'postcode', 'postal code',
    'pincode', 'pin code', 'website', 'time', 'day', 'price', 'amount', 'cost', 'fee',
    'product', 'service', 'industry', 'signature', 'recipient', 'sender',
    'promo code', 'discount code', 'coupon code', 'referral code'))
_FIELD_LEADS = frozenset(
    'your my our their client customer recipient prospect contact sender user '
    'company business clinic target'.split())
#: The words a field name ends with, whatever up to three words come first
#: ("[Name]", "[Clinic Name]", "[Event Date]", "[Booking Link]").
_FIELD_ENDINGS = ('name', 'email', 'email address', 'address', 'number', 'date',
                  'link', 'url', 'title')
#: A word every field name holds, in lower case whatever its spelling
#: ("FirstName", "E-Mail"): what a pair must hold before _field_name reads
#: it in words.
_FIELD_WORD = re.compile('|'.join(sorted(
    {word for phrase in (*_FIELD_NAMES, *_FIELD_ENDINGS) for word in phrase.split()}
    | {'insert', 'mail'}, key=len, reverse=True)))


def _field_name(inner: str) -> bool:
    """Whether ``inner``, what a pair of brackets holds, is a common field
    name (owner, 2026-11-23: "[First Name]"): read in words ("FirstName",
    "first_name", "first-name" and "First Name" alike, "e-mail" as
    "email", "Child's Name" as "child name", a last "here" dropped), it is
    one of _FIELD_NAMES alone or after one of _FIELD_LEADS, ends with one
    of _FIELD_ENDINGS, or starts "insert" ("[insert testimonial here]")."""
    if not _FIELD_WORD.search(inner.lower()):
        return False
    words = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', inner)
    words = re.sub(r'(?i)\be[\s_.-]?mail\b', 'email', words)
    words = re.sub(r"['’]s\b", '', words)
    words = re.sub(r'[\s_.-]+', ' ', words).strip().lower().split()
    if words[-1:] == ['here']:
        words = words[:-1]
    if not words or len(words) > 5 or not all(word.isalpha() for word in words):
        return False
    if words[0] == 'insert':
        return len(words) > 1
    phrase = ' '.join(words)
    if phrase in _FIELD_NAMES or (words[0] in _FIELD_LEADS
                                  and ' '.join(words[1:]) in _FIELD_NAMES):
        return True
    return any(phrase == end or (phrase.endswith(' ' + end)
                                 and len(words) - len(end.split()) <= 3)
               for end in _FIELD_ENDINGS)


def _owner_blank_in(text: str, prose: bool = True) -> Optional[str]:
    """The first blank of the owner's rule (2026-11-23, "Block them") in
    ``text``, as its reader sees it, or None: a word in capitals in square
    brackets ("[NAME]", "[PDF]", "[UPDATE]"), a merge tag ("%FIRSTNAME%",
    "*|FNAME|*"), and a common field name in brackets ("[First Name]",
    "[Your Name]"); "[1]", "[sic]", "[2023]", "[A]" and "[Webinar]" are
    none.  Spaces inside the brackets hide none ("< first_name>", "[
    FIRST_NAME ]"; "<first name>" is _placeholder_shaped's).  In prose a
    word in capitals glued to the word before is one too ("Name:[NAME]",
    "Dear[NAME]"), while lowercase glued is code ("user[name]").
    ``prose=False``: text no reader reads as words (attribute values,
    styles, scripts), held to the capitals unglued and the merge tags
    only, so a style sheet's "[name]" selector is none.  Each kind's
    first is found, and the first of those named; what a pair holds is
    judged once per email, so a long one of brackets reads fast."""
    def glued(at):
        before = text[at - 1:at]
        return before.isalnum() or before == '_'
    merge_tag = _MERGE_TAG.search(text)
    firsts = [(merge_tag.start(), merge_tag.group(0))] if merge_tag else []
    for m in _SQUARE_CAPS.finditer(text):
        if prose or not glued(m.start()):
            firsts.append((m.start(), m.group(0)))
            break
    if not prose:
        return min(firsts)[1] if firsts else None
    judged = {}
    for m in _SQUARE_PAIR.finditer(text):
        held = m.group(1)
        if held not in judged:
            inner = held.strip()
            judged[held] = bool(_field_name(inner)
                                or (inner != held and _IDENTIFIER.fullmatch(inner)))
        inner = held.strip()
        if judged[held] and not (glued(m.start()) and inner == inner.lower()
                                 and ' ' not in inner):
            firsts.append((m.start(), m.group(0)))   # lowercase glued is code: "user[name]"
            break
    judged = {}
    for m in _ANGLE_PADDED.finditer(text):
        held = m.group(1)
        if held not in judged:
            inner = held.strip()
            judged[held] = bool(inner and (_CAPS_WORDS.fullmatch(inner)
                                           or _IDENTIFIER.fullmatch(inner)
                                           or _field_name(inner)))
        after = text[m.end():m.end() + 1]
        if judged[held] and not (glued(m.start()) or after.isalnum()
                                 or (text[m.start() - 1:m.start()] == '<' and after == '>')):
            firsts.append((m.start(), m.group(0)))   # not code ("vector< T >"), "<< x >>"
            break
    return min(firsts)[1] if firsts else None


def bracket_looks_like_code(blank: str) -> bool:
    """Whether a '<...>' blank reads as code (a generic's parameters: "<String,
    Integer>", "<T>", "<FirstName>"), so its refusal may say where code goes;
    "<first name>" is words."""
    inner = blank[1:-1]
    return bool(blank.startswith('<') and _CODE_LIKE.fullmatch(inner) and (
        ',' in inner or '::' in inner or re.fullmatch(r'[A-Z]', inner)
        or re.search(r'[a-z][A-Z]', inner)))
#: The element names of the HTML standard, current and obsolete (the WHATWG
#: element index and its obsolete features), and its boolean attributes:
#: what tells a tag of markup from words in brackets (_is_markup_tag).
_HTML_ELEMENTS = frozenset(
    'a abbr acronym address applet area article aside audio b base basefont bdi '
    'bdo bgsound big blink blockquote body br button canvas caption center cite '
    'code col colgroup data datalist dd del details dfn dialog dir div dl dt em '
    'embed fieldset figcaption figure font footer form frame frameset h1 h2 h3 h4 '
    'h5 h6 head header hgroup hr html i iframe image img input ins isindex kbd '
    'keygen label legend li link listing main map mark marquee math menu menuitem '
    'meta meter multicol nav nextid nobr noembed noframes noscript object ol '
    'optgroup option output p param picture plaintext pre progress q rb rp rt rtc '
    'ruby s samp script search section select slot small source spacer span '
    'strike strong style sub summary sup svg table tbody td template textarea '
    'tfoot th thead time title tr track tt u ul var video wbr xmp'.split())
_HTML_BOOLEAN_ATTRIBUTES = frozenset(
    'allowfullscreen async autofocus autoplay checked compact controls declare '
    'default defer disabled formnovalidate hidden inert ismap itemscope loop '
    'multiple muted nohref nomodule noresize noshade novalidate nowrap open '
    'playsinline readonly required reversed selected truespeed '
    'typemustmatch download'.split())
#: Bare attributes any element takes: data-* and aria-* (review rv_e4f5:
#: '<div data-empty>').
_BARE_ATTRIBUTE_PREFIXES = ('data-', 'aria-')
#: The non-standard names an email's markup uses: MJML's (mj-*) and
#: Microsoft Office's namespaces (o:, v:, w:).  Any other custom name
#: ("<your-message-here>") is no markup unless its own end tag follows.
_EMAIL_MARKUP_PREFIXES = ('mj-', 'o:', 'v:', 'w:')
#: One start tag, read as markup: a name, then attributes, each name=value
#: (quoted, or unquoted up to a space: "src=https://x.example/i.png?w=100")
#: or bare.  An end tag alone ("</p>") is no markup the reader sees.
_ONE_TAG = re.compile(
    r'<([A-Za-z][\w:.-]*)'
    r'((?:\s+[^\s"\'=<>/]+(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s"\'<>`]+))?)*)\s*/?>')
_TAG_ATTRIBUTE = re.compile(
    r'([^\s"\'=<>/]+)(\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s"\'<>`]+))?')
#: A '<...>' in text a person reads, shown ("&lt;first name&gt;") or plain
#: (a subject): a blank when it has a blank's shape (_placeholder_shaped).
_ANGLE_BRACKETED = re.compile(r'<[^<>]*>')
#: What a blank never holds: an operator ("a<b && c>d", "fetch<User[]>",
#: "<? super T>", "<= budget =>"), or a '-' or '.' outside a word (an
#: arrow, "<- Settings ->"; "<Dr. Smith>").  "<first-name>", "<2-3 lines>"
#: and "<example.com>" hold theirs inside words.
_BLANK_OPERATORS = frozenset('&[]?!=<>')
_LOOSE_JOINER = re.compile(r'(?<!\w)[-.]|[-.](?!\w)')
#: The tags a reader knows as formatting when they are shown as text
#: ("&lt;br&gt;", "&lt;b&gt;", "&lt;td nowrap&gt;"): no blank.  Any other
#: name shown is one: "Meet at &lt;address&gt; on &lt;date&gt;" (review of
#: the lxml reading, hartos-e1 #4).
_SHOWN_FORMATTING_TAGS = frozenset(
    'br hr p b i u em div span li td tr img html body'.split())
#: Names of the standard a template also writes as a blank ("Meet at
#: <address> on <date>"): such a tag written bare, and never closed or
#: void, is the blank (hartos-e1 #7).
_BLANK_NAMED_ELEMENTS = frozenset(
    'address time title label caption summary data output option link source '
    'section header footer main nav mark map small code button select input'.split())
_VOID_ELEMENTS = frozenset(
    'area base br col embed hr img input link meta source track wbr'.split())
#: The tags that end an <svg> or a <math> in an HTML parser: a start tag of
#: these, or "</p>" or "</br>"; what follows is HTML again (review rv_f32b:
#: an svg left open hid every blank after it).
_FOREIGN_BREAKOUT = frozenset(
    'b big blockquote body br center code dd div dl dt em embed h1 h2 h3 h4 '
    'h5 h6 head hr i img li listing menu meta nobr ol p pre ruby s small span '
    'strong strike sub sup table tt u ul var'.split())


def _shown_formatting_tag(shown: str) -> bool:
    """Whether ``shown`` is a formatting tag of markup shown as text
    (_SHOWN_FORMATTING_TAGS): "<b>", "<td nowrap>", not "<address>"."""
    parts = _ONE_TAG.fullmatch(shown)
    return (bool(parts) and parts.group(1).lower() in _SHOWN_FORMATTING_TAGS
            and _is_markup_tag(shown))


def _placeholder_shaped(shown: str, before: str = '', after: str = '') -> bool:
    """Whether ``shown``, one '<...>' a reader sees or a tag written where a
    value belongs, has a blank's shape: a word character, '$' or '#' at
    both ends of what it holds ("<first name>", "<$price>", "<Name,
    Surname>", "<product/service>"); no operator (_BLANK_OPERATORS,
    _LOOSE_JOINER); its '>' not glued to a letter or digit after it ("x<y
    and y>z", "2<3 and 5>4"); not doubled on both sides ("<<Newsletter>>",
    while "<<first name>" is a blank); no
    address or link ("<sam@x.example>", "<www.example.com>"); no formatting
    tag shown (_shown_formatting_tag).  ``before`` and ``after``: what the
    reader sees just before and after it.

    The one rule for an email's body and its subject (review rv_1a81: a
    looser rule refused 27 of 100 ordinary sentences, "Home <- Settings ->
    Profile" among them).  Code in a body's prose is held to it too
    ("Box<dyn Error>"), since code belongs inside <code>, whose brackets
    are code (hartos-e1 #5)."""
    inner = shown[1:-1]
    return bool(
        inner and (inner[0].isalnum() or inner[0] in '_$#')
        and (inner[-1].isalnum() or inner[-1] in '_$#')
        and not _BLANK_OPERATORS.intersection(inner)
        and not _LOOSE_JOINER.search(inner)
        and '@' not in inner and '://' not in inner
        and not inner.lower().startswith('www.')
        and not after.isalnum() and not (before == '<' and after == '>')
        and not _shown_formatting_tag(shown))


def _placeholder_in(text: str) -> Optional[str]:
    """The first '<...>' in plain ``text`` with a blank's shape
    (_placeholder_shaped), else None."""
    for found in _ANGLE_BRACKETED.finditer(text):
        at, end = found.start(), found.end()
        if _placeholder_shaped(found.group(0), text[at - 1:at], text[end:end + 1]):
            return found.group(0)
    return None


def _no_bare_words(tag: str) -> bool:
    """Whether ``tag`` has a name and only name=value attributes ("<xml>",
    '<amp-img src="...">'), as no placeholder has ("<a call-to-action
    label>", "<summary of call>"; review rv_f32b)."""
    parts = _ONE_TAG.fullmatch(tag)
    return bool(parts) and all(value for _, value in _TAG_ATTRIBUTE.findall(parts.group(2)))


def _is_markup_tag(tag: str) -> bool:
    """Whether ``tag`` (one '<...>') is a tag of markup, not words in
    brackets left for a value: an element of the HTML standard, or of an
    email's own markup ("<mj-raw>", "<o:p>"; _EMAIL_MARKUP_PREFIXES), whose
    bare attributes are the standard's boolean ones or any element's
    ('<img src="..." hidden>', "<td nowrap>", '<a href="..." download>',
    "<div data-empty>"; reviews rv_fc0c, rv_e4f5).  "<your message here>"
    and "<your-message-here>" name no element, and "<a short intro>" and
    "<link here>" carry bare words no element takes."""
    parts = _ONE_TAG.fullmatch(tag)
    if not parts:
        return False
    name = parts.group(1).lower()
    if name not in _HTML_ELEMENTS and not name.startswith(_EMAIL_MARKUP_PREFIXES):
        return False
    return all(value or attribute.lower() in _HTML_BOOLEAN_ATTRIBUTES
               or attribute.lower().startswith(_BARE_ATTRIBUTE_PREFIXES)
               for attribute, value in _TAG_ATTRIBUTE.findall(parts.group(2)))


class UnreadableEmail(ValueError):
    """An email body this node cannot read as its reader's parser does:
    lxml is not installed, or libxml2 fails on the text or cuts its tree
    short (a nesting deeper than its limit).  verify_outgoing_email refuses
    the email with the reason (``str``) and the fix (``fix``): an email
    whose blanks cannot be checked never leaves (hartos-e1 #1)."""

    def __init__(self, reason: str, fix: str):
        super().__init__(reason)
        self.fix = fix


#: A '<' that opens no tag, comment or declaration (no ASCII letter, '/',
#: '!' or '?' after it): text to an HTML5 parser ("3 < 5", "<2 lines>").
_LONE_LT = re.compile(r'<(?![A-Za-z/!?])')
#: The end tags an HTML5 parser reads otherwise than libxml2: "</p>" ends
#: a paragraph, or with none open makes an empty one (a line break either
#: way; libxml2 drops a stray one), and "</br>" is a line break.
_P_END = re.compile(r'</p\s*>', re.IGNORECASE)
_BR_END = re.compile(r'</br\s*>', re.IGNORECASE)
#: A named character reference ("&lbrack;", "&LT;", "&lt" with no ';'),
#: read left to right, so "&amp;lbrack;" is "&" and the text "lbrack;".
_NAMED_REFERENCE = re.compile(r'&([A-Za-z][A-Za-z0-9]*)(;?)')


def _numbered(found) -> str:
    """One named reference as the numeric ones it stands for in HTML5
    (html.entities.html5), libxml2 2.11 knowing only HTML4's names ("&lbrack;
    COMPANY_NAME&rbrack;" was sent as text while every reader shows
    [COMPANY_NAME]; review rv_52c3).  Without its ';' a name is read as
    HTML5 reads one in text: its longest prefix that HTML5 knows bare
    ("&ltfirst" is "<first"); else it is left as written.  A name of
    HTML4's with its ';' is left as written: libxml2 2.11 reads those
    itself, and rewriting each made a long email about 6x slower to read
    (review rv_52c3: "&nbsp;", "&amp;")."""
    name, semicolon = found.group(1), found.group(2)
    if semicolon and name in _HTML4_NAMES:
        return found.group(0)
    if semicolon and name + ';' in _HTML5_ENTITIES:
        return ''.join(f'&#{ord(c)};' for c in _HTML5_ENTITIES[name + ';'])
    for end in range(len(name), 0, -1):
        if name[:end] in _HTML5_ENTITIES:
            return (''.join(f'&#{ord(c)};' for c in _HTML5_ENTITIES[name[:end]])
                    + name[end:] + semicolon)
    return found.group(0)


def _parseable(text: str) -> str:
    """``text`` as libxml2 can take all of it, read as an HTML5 parser reads
    it: a NUL is dropped, as an HTML parser drops it from text (in a tag,
    libxml2 stops reading the rest of the email at it); a lone surrogate,
    which no UTF-8 holds, is read as U+FFFD; a '<' that opens nothing is
    written "&lt;", the text an HTML5 parser makes of it (libxml2 2.11 drops
    one that starts the email: "<2 lines>" read "2 lines>"); a "</p>" is
    followed by an empty paragraph and a "</br>" is a "<br>", as an HTML5
    parser reads them ("<div>&lt;first name&gt;</p>Hi" holds the blank on a
    line of its own; libxml2 read "<first name>Hi"); a named reference is
    written as the numeric ones HTML5 reads it as (_numbered)."""
    text = _LONE_LT.sub('&lt;', text.replace('\x00', ''))
    text = _BR_END.sub('<br>', _P_END.sub('</p><p></p>', text))
    if '&' in text and any(not semicolon or name not in _HTML4_NAMES
                           for name, semicolon in set(_NAMED_REFERENCE.findall(text))):
        text = _NAMED_REFERENCE.sub(_numbered, text)    # none to rewrite: not walked
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        text = text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')
    return text


def _email_tree(text: str):
    """(lxml.etree, the root element libxml2 builds for ``text`` or None
    when it holds no element, the text read).  lxml is imported here, so a
    node without it refuses each email (UnreadableEmail) rather than
    failing to start or sending one unchecked.  The text goes in as UTF-8
    bytes with the encoding named, so an XML declaration in it is read, not
    refused.  A comment left open runs to the end of the text, as an HTML5
    parser reads it (libxml2 2.11 drops it, and with it an [if mso] that
    Outlook shows), so it is closed there and read again.  A tree libxml2
    cut short is never judged: a fatal error, an internal one (deeper than
    256 elements) or no memory refuses the email."""
    try:
        from lxml import etree
    except ImportError as missing:
        raise UnreadableEmail(
            'lxml is not installed',
            'Install lxml on this node (requirements.txt pins it); until it is, no '
            'outreach email leaves this node.') from missing
    for _ in range(2):
        parser = etree.HTMLParser(encoding='utf-8')
        try:
            root = etree.fromstring(text.encode('utf-8'), parser)
        except Exception as failed:  # libxml2's own failure: refused, never sent unread
            raise UnreadableEmail(
                f'libxml2 could not parse its HTML ({type(failed).__name__})',
                'Send it as plainer HTML.') from failed
        if not any(e.type_name == 'ERR_COMMENT_NOT_FINISHED' for e in parser.error_log):
            break
        text += '-->'
    for entry in parser.error_log:
        if entry.type_name == 'ERR_DOCUMENT_EMPTY':
            continue             # an empty body: nothing to read, nothing cut
        if 'depth' in entry.message.lower():
            raise UnreadableEmail(
                'its HTML nests more elements than libxml2 reads (256)',
                'Nest its HTML less deeply.')
        if (entry.level == etree.ErrorLevels.FATAL
                or entry.type_name in ('ERR_INTERNAL_ERROR', 'ERR_NO_MEMORY')):
            raise UnreadableEmail(
                f'libxml2 stopped reading its HTML ({entry.message.strip()})',
                'Send it as plainer HTML.')
    return etree, root, text


#: Each element name's start tag and end tag as an email writes them
#: (_WrittenTags), compiled once per name.
_WRITTEN_TAG_PATTERNS = {}
#: An attribute name as an HTML parser keeps it (libxml2 drops a "}--!").
_ATTRIBUTE_NAME = re.compile(r'[A-Za-z_:][\w:.-]*')
#: The tags that end an svg or a math for an HTML5 parser which libxml2
#: drops, keeping what follows inside it (_WrittenTags.breakout).
_DROPPED_BREAKOUT = re.compile(r'<(?:body|head)(?![\w:.-])[^<>]*>|</(?:p|br)\s*>', re.IGNORECASE)


def _written_tag_patterns(name: str):
    if name not in _WRITTEN_TAG_PATTERNS:
        if len(_WRITTEN_TAG_PATTERNS) > 4096:
            _WRITTEN_TAG_PATTERNS.clear()
        escaped = re.escape(name)
        _WRITTEN_TAG_PATTERNS[name] = (
            re.compile(rf'<{escaped}(?![\w:.-])[^<>]*>', re.IGNORECASE),
            re.compile(rf'</{escaped}\s*>', re.IGNORECASE))
    return _WRITTEN_TAG_PATTERNS[name]


class _WrittenTags:
    """Where the email writes each element's start tag, as it is written:
    libxml2 keeps "<Company Name>" as <company name=""> and "<a}--!>" as
    <a>, so whether a tag is markup is judged on what is written.  Each
    name's tags are found in order, each search going on from the last, so
    the email is read once per name.  libxml2 gives an element the line its
    start tag ends on: a tag of the name ending on an earlier line is one no
    element came from (in a comment, a script), and one ending on a later
    line means the parser added this element ("<tbody>", a "<p>" for loose
    text), which has nothing written."""

    def __init__(self, text: str):
        self.text, self.line_starts, self.next, self.last_end = text, None, {}, {}
        self.pairs, self.dropped = {}, None

    def find(self, name: str, line: Optional[int],
             attributes=()) -> Tuple[Optional[str], int]:
        """(the start tag as written, where it ends), or (None, -1).
        ``attributes``: the element's own; a tag written with a name the
        element lacks is another's ("Hi\\n<p your note>": libxml2 gives the
        <p> it adds for "Hi" the line of the written one)."""
        if self.line_starts is None:
            self.line_starts = [0] + [m.end() for m in re.finditer('\n', self.text)]
        for found in _written_tag_patterns(name)[0].finditer(self.text, self.next.get(name, 0)):
            ends_on = bisect.bisect_right(self.line_starts, found.end() - 1)
            if line and line < 65535 and ends_on < line:
                continue                         # no element came from it
            # only a tag written well-formed tells its attributes ("<product/
            # service>" is <product> to libxml2 2.11, <product service> to 2.14)
            well_formed = _ONE_TAG.fullmatch(found.group(0)) if ' ' in found.group(0) else None
            written_names = {a.lower() for a, _ in _TAG_ATTRIBUTE.findall(well_formed.group(2))
                             if _ATTRIBUTE_NAME.fullmatch(a)} if well_formed else set()
            if ((line and line < 65535 and ends_on > line)
                    or written_names - {a.lower() for a in attributes}):
                return None, -1                  # an element the parser added
            self.next[name] = found.end()
            return found.group(0), found.end()
        self.next[name] = len(self.text)         # none further on
        return None, -1

    def breakout(self, name: str, after: int) -> int:
        """Where the svg or math ``name`` whose start tag ends at ``after``
        ends for an HTML5 parser, as written: at its own end tag (nested
        ones counted; libxml2 keeps it open past one when a tag it cannot
        close is open, "<svg><td></svg>"), or at a tag that ends it which
        libxml2 drops ("<body>", "<head>", "</p>", "</br>"); -1 for none."""
        if after < 0:
            return -1
        if self.dropped is None:     # each found once, then looked up
            self.dropped = [m.start() for m in _DROPPED_BREAKOUT.finditer(self.text)]
        at = bisect.bisect_left(self.dropped, after)
        ends = [self.dropped[at]] if at < len(self.dropped) else []
        own = self.own_end(name, after)
        return min(ends + [own] if own >= 0 else ends) if ends or own >= 0 else -1

    def own_end(self, name: str, after: int) -> int:
        """Where the end tag matching the ``name`` start tag that ends at
        ``after`` is written (nested ones counted), or -1: an HTML5 parser
        ends the element there ("<code><div>x</code><your note>": the
        note is out of the code, while libxml2 kept the code open).  Each
        name's start and end tags are paired once, in one pass."""
        if after < 0:
            return -1
        if name not in self.pairs:
            pairs, open_at = {}, []
            for tag in re.finditer(rf'<(/?){re.escape(name)}(?![\w:.-])[^<>]*>', self.text,
                                   re.IGNORECASE):
                if tag.group(1):
                    if open_at:
                        pairs[open_at.pop()] = tag.start()
                elif not tag.group(0).endswith('/>'):
                    open_at.append(tag.end())
            self.pairs[name] = pairs
        return self.pairs[name].get(after, -1)

    def closed(self, name: str, after: int) -> bool:
        """Whether an end tag of ``name`` is written after ``after``."""
        if name not in self.last_end:
            ends = [m.start() for m in _written_tag_patterns(name)[1].finditer(self.text)]
            self.last_end[name] = ends[-1] if ends else -1
        return after >= 0 and self.last_end[name] >= after


class _EmailReading:
    """One HTML email read once, from the tree libxml2 builds (_email_tree):
    its texts and its lines (_read_email)."""

    def __init__(self):
        self.seen, self.read, self.words, self.markup = [], [], [], []
        self.lines, self.unknown, self.code_tags, self.line = [], [], [], None
        self.break_line()

    def break_line(self):
        if self.line is None or self.line['text'] or self.line['tags']:
            self.line = {'text': [], 'at': 0, 'tags': []}
            self.lines.append(self.line)
            # a line ends the words too: "Hi<br>[A_B]" holds a blank on its
            # own line, not one glued to "Hi" as code is
            self.read.append('\n')
            self.words.append('\n')

    def flow(self, piece: Optional[str], state: tuple):
        """A text node, read in its element's state (unshown, the svg or
        math it is in or None, code, where the code block's end tag is
        written)."""
        if not piece:
            return
        unshown, code = state[0], state[2]
        if unshown:
            self.read.append(' ' + piece + ' ')
            return
        self.seen.append(piece)
        self.markup.append(piece)
        if code:
            # a code block's brackets are code; only its blanks in capitals
            piece = ' ' + ' '.join(_CODE_BLANK.findall(piece)) + ' '
        self.read.append(piece)
        self.words.append(piece)
        if code:                 # in its line, a code block is what it starts with, a
            # letter or digit ("<code>x": a tag glued to it), else a space;
            # never its brackets, which are code
            first = self.seen[-1][:1]
            piece = first if first.isalnum() else ' '
        self.line['text'].append(piece)
        self.line['at'] += len(piece)

    def element(self, node, name: str, tag: Optional[str], end: int,
                written: _WrittenTags, code: bool):
        """An element outside svg and math: a tag that is no markup
        (_is_markup_tag: "<your message here>", "<first-name>", "<a short
        intro>"), or a bare one the standard names that is never closed or
        is void ("<address>", "<time>"), is where a value belongs; held as
        written, where it is in its line.  In a code block, whose brackets
        are code, such a tag is held for the ask hint's blanks alone
        ("<pre><why you ask></pre>").  ``tag``/``end``: its start tag as
        written and where that ends (_WrittenTags.find)."""
        if tag is None:
            return               # the parser's own, or a tag never ended ('<code x="...')
        known = name in _HTML_ELEMENTS or name.startswith(_EMAIL_MARKUP_PREFIXES)
        if known and _is_markup_tag(tag):
            if (name not in _BLANK_NAMED_ELEMENTS or len(node.attrib)
                    or (name not in _VOID_ELEMENTS and written.closed(name, end))):
                return           # markup; "<address>...</address>" the element
        elif written.closed(name, end) and _no_bare_words(tag):
            return               # closed by its own end tag: an element ("<xml></xml>")
        if code:
            self.code_tags.append(tag)
            return
        self.unknown.append(tag)
        self.read.append(' ' + tag + ' ')
        self.markup.append(' ' + tag + ' ')
        self.line['tags'].append((self.line['at'], len(self.unknown), tag))

    def document(self, text: str, depth: int = 0):
        """Read one HTML text: the email, or what a conditional comment in
        it holds (Outlook reads that as HTML), each node in order, without
        recursion (a deep email reads as fast as a flat one)."""
        etree, root, text = _email_tree(_parseable(text))
        if root is None and '<!--' in text:
            # only comments: libxml2 keeps none without an element, so the
            # text is read in a body (Outlook shows what an [if mso] holds)
            etree, root, text = _email_tree('<body>' + text)
        if root is None:
            return
        written = _WrittenTags(text)
        top = [*reversed(list(root.itersiblings(preceding=True))), root,
               *root.itersiblings()]
        # (unshown, the <svg>/<math> the node is in, code); an HTML tag in
        # an svg or a math ends it for all that follows, as an HTML5
        # parser leaves the svg there (libxml2 keeps the rest inside it);
        # one libxml2 drops ("<body>", "</p>") ends it where it is written
        plain, ended, breaks = (False, None, False, -1), {}, {}  # by id, each kept alive
        stack = [(node, plain, None) for node in reversed(top)]
        while stack:
            node, outer, own = stack.pop()
            if own is not None:                  # the element's end
                if node.tag.lower() in _LINE_ELEMENTS:
                    self.break_line()
                self.flow(node.tail, outer)
                continue
            if not isinstance(node.tag, str):    # a comment, an instruction
                body = node.text or '' if node.tag is etree.Comment else ''
                if depth < 3 and _CONDITIONAL.match(body):
                    self.break_line()
                    self.document(_CONDITIONAL_CLOSE.sub('', _CONDITIONAL_OPEN.sub('', body)),
                                  depth + 1)
                    self.break_line()
                self.flow(node.tail, outer)
                continue
            name = node.tag.lower()
            unshown, svg, code, code_end = outer
            tag, end = (None, -1) if name in ('html', 'head', 'body') else (
                written.find(name, node.sourceline, node.attrib.keys()))
            if code and 0 <= code_end < end:
                code, code_end = False, -1   # written after the code block's end tag
            if svg is not None and (id(svg) in ended or 0 <= breaks[id(svg)][1] < end):
                ended[id(svg)] = svg
                svg = None
            if not code:
                if svg is None and name in ('svg', 'math'):
                    svg = node                   # svg's and math's tags are their own
                    breaks[id(svg)] = (svg, written.breakout(name, end))
                elif svg is not None and name in _FOREIGN_BREAKOUT:
                    ended[id(svg)] = svg         # an HTML tag ends them
                    svg = None
            opens_code = not code and svg is None and name in ('pre', 'code')
            own = (unshown or name in _UNSHOWN_ELEMENTS, svg, code or opens_code,
                   written.own_end(name, end) if opens_code else code_end)
            if name in _LINE_ELEMENTS:
                self.break_line()
            if not code:
                for value in node.attrib.values():   # href="[UNSUBSCRIBE_URL]"
                    self.read.append(' ' + value + ' ')
                if name == 'img' and not own[0] and (node.get('alt') or '').strip():
                    # what a reader is shown for an image it does not load: words
                    # to read (review rv_52c3: an image alone with its alt text was
                    # refused as empty), held to the rules as the words are
                    self.words.append(' ' + node.get('alt') + ' ')
            if svg is None:              # in the head too ("<link here>" is put there)
                self.element(node, name, tag, end, written, code)
            self.flow(node.text, own)
            stack.append((node, outer, own))
            stack.extend((child, own, None) for child in reversed(node))

    def blank(self) -> Optional[str]:
        """The blank the reader meets in a line: a '<...>' that is all its
        line holds, by the memory keys' rule ("<p><your message here></p>",
        "<p>&lt;2-3 sentences&gt;</p>", "< 5 mins >" alone, the owner's call);
        then a '<...>' shown in a line, or a tag written where a value
        belongs, with a blank's shape (_placeholder_shaped: "Hi
        &lt;first-name&gt;, welcome", "Meet at <address> on <date>"); then a
        tag of no element in an email that shows nothing ("</div><your-note>",
        review rv_f32b); then an ask hint's blank written as a tag in a code
        block."""
        lines = [(''.join(line['text']), line['tags']) for line in self.lines]
        for text, tags in lines:
            one = text.strip()
            if (one and unfilled_key_placeholder(one) == one
                    and not _shown_formatting_tag(one)):
                return one       # all its line shows, whatever tags it holds
            if (not one and len(tags) == 1 and '=' not in tags[0][2]
                    and unfilled_key_placeholder(tags[0][2]) == tags[0][2]):
                return tags[0][2]    # a tag with a name=value is markup ('<path d="M0 0"/>')
        for text, tags in lines:
            # a tag before the text at its place, the inner one first ("<pre-header><your
            # message here>": libxml2 nests one tag of no element in another)
            found = [(at, 0, -order, tag, '', text[at:at + 1]) for at, order, tag in tags]
            found += [(m.start(), 1, 0, m.group(0), text[m.start() - 1:m.start()],
                       text[m.end():m.end() + 1]) for m in _ANGLE_BRACKETED.finditer(text)]
            for *_place, shown, before, after in sorted(found):
                if _placeholder_shaped(shown, before, after):
                    return shown
        if self.unknown and not ''.join(self.seen).strip():
            left_for = [tag for tag in self.unknown if '=' not in tag]
            if left_for:
                return left_for[0]
        return _ask_hint_blank_in(' '.join(self.code_tags)) if self.code_tags else None


def _read_email(text: str) -> Tuple[str, str, str, str, Optional[str]]:
    """One HTML email read once, from the tree its reader's parser builds
    (libxml2: _EmailReading), as four texts and its blank:

    - ``seen``: the words its reader sees, in order (email_reader_text: the
      plain-text part of a sent email); not the head, title, styles or
      scripts, nor a comment, but what a conditional comment holds, since
      Outlook shows it;
    - ``read``: what the blank check reads: those words, a code block's
      blanks in capitals only (its other brackets are code), every
      attribute's value (href="[UNSUBSCRIBE_URL]"), the unshown text, and
      each tag written where a value belongs ("<first_name>");
    - ``seen_words``: the reader's words outside code, joined as the reader
      joins them ("[COMPANY<b></b>_NAME]" is seen as [COMPANY_NAME]), and
      each shown image's alt text, which a reader is shown in its place;
    - ``markup``: the words with code's and those tags, where the ask
      hint's blanks and the whole-text blank are looked for;
    - ``blank``: the blank the reader meets in a line (_EmailReading.blank).

    Raises UnreadableEmail when it cannot be read (_email_tree)."""
    reading = _EmailReading()
    reading.document(text)
    return (''.join(reading.seen), ''.join(reading.read), ''.join(reading.words),
            ''.join(reading.markup), reading.blank())


def email_reader_text(html_text) -> str:
    """The words an HTML email's reader sees, as plain text: the plain-text
    part outreach_crm_tools._send_email sends beside the HTML.  It is the
    blank check's own reading (_read_email), so the plain part shows nothing
    the check did not read (review rv_c0f5: '<!-- a > b [COMPANY_NAME] -->'
    was a comment to the check and "b [COMPANY_NAME] -->" in the plain
    part).  An email it cannot read has no plain part (''): the check
    refuses that email (UnreadableEmail), so none is sent with it."""
    try:
        return _read_email(str(html_text or ''))[0].strip()
    except UnreadableEmail:
        return ''


def unfilled_text_placeholder(text, braces: bool = False, *,
                              as_html: bool = True) -> Optional[str]:
    """The fill-in mark still left in text a person will read, or None.

    With ``braces`` (an outreach email), the owner's rule (2026-11-23,
    "Block them") holds for the body and the subject, as each reader sees
    them: a word in capitals in square brackets, a merge tag and a common
    field name are blanks, "[1]" and "[sic]" are not (_owner_blank_in),
    looked for after every other shape.  An email body that shows no text
    (nor an image's alt text) and holds no blank is '' (no blank, and
    nothing to read: verify_outgoing_email refuses it).

    ``braces``: also a word in braces ({POSTAL ADDRESS}, {{first_name}}),
    the shape a mail template leaves, looked for before the other shapes.
    Only the outreach email check asks for it (outreach_crm_tools
    .verify_outgoing_email, which kept its own pattern for it until audit
    H9): a message or an ask may hold braces of its own (a coding lesson's
    f"{name}", a set written {x}), so they never ask.  With it:

    - ``as_html`` (the default): the text is an email body, read as its
      reader's HTML parser reads it (_read_email).  The underscore shape is
      looked for in what that reading leaves: not inside an HTML comment
      nor, but for a blank in capitals ("[DISCOUNT_CODE]"), in a code
      block; as written, as its character references decode
      ("&lt;first_name&gt;" shows as <first_name>, the blank named), and in
      the words the reader sees.  Then the ask hint's blanks, with the
      comments cut; then the whole-text blank, unless the one tag is markup
      ("<p>", '<img src="..." hidden>'); then the blank the reader meets
      in a line ("<p><your message here></p>", "Hi &lt;first name&gt;,").
    - ``as_html=False``: the text is plain, as an email's subject line is,
      and is held to every shape below, as written and as decoded, a blank
      glued to a word included, since a subject is never code; and words
      in angle brackets ("Hi <first name>") are a blank there too.

    The one rule for every such text: a message (send_message_to_user,
    send_message_in_seconds) and an ask's reason and field labels
    (person_ask).  Two rules in one commit let each tool send what the other
    refused (review C-1 of 0516f3981: "Hi <parent_name>, which day suits
    you?" went out as an ask, "<why you ask>" as a message).  Two shapes:

    - The whole text is one blank, as the ask tool's example writes it
      ("<why you ask>", "<your question>"; the 4B copied it word for word,
      live 2026-11-07).  What a blank is, is the memory keys' rule
      (unfilled_key_placeholder).
    - An identifier joined by '_' inside <> or [] anywhere in it.  Narrower
      than the keys' rule on purpose: a key never holds '<' or '>' of its
      own, but a message does (a maths lesson's "3 < 5 and 7 > 2", a coding
      lesson's <div>, a citation [1]).  Measured: live 2026-11-11 01:59 IST
      on build 69 (request msg_1794342560815_sim) the parent received
      "Thank you, <parent_name>!", and of the 1061 send texts in this
      desktop's llm_outbound logs the other unfilled marks were
      '[REVENUE_AMOUNT]', '[TOTAL_COSTS]' and three more in one send, plus
      '<tool_call>' call markup in two.  A mark glued to the word before it
      is code (my_list[item_index], os.environ[GITHUB_TOKEN],
      std::vector<my_type>) and [words](link) is a markdown link; neither
      is caught (review C-2), so a placeholder glued to a word ("Hi<name>")
      is not caught either.  Nor is a single word in brackets inside a
      longer text ("say <ball>", "[Note]").
    - One of the ask hint's own blanks with more words around it
      (_ask_hint_blank_in): live 2026-11-17 10:55:55 IST an ask's reason
      opened with "<why you ask>" and went on with the rest of the example,
      and reached the owner.  An ask refused that shape while a message
      still sent it, until this rule held it for both (audit H22)."""
    body = str(text or '').strip()
    if braces:
        blank = _BRACE_PLACEHOLDER.search(body)
        if blank:
            return blank.group(0)
        if not as_html:
            # A text read as it is written (an email's subject line): every
            # shape a chat text is held to, as written and as decoded.
            # Review rv_c0f5: the subject went in with the body and was read
            # as HTML, so "Quick question: <your question>" left.  A subject
            # is never code, so a blank glued to a word is one too
            # (_SUBJECT_PLACEHOLDER; review rv_fc0c: "Hi<first_name>" left).
            shown = html.unescape(body).strip()
            blank = _BRACE_PLACEHOLDER.search(shown)
            found = (_chat_text_blank(body, _SUBJECT_PLACEHOLDER)
                     or (blank.group(0) if blank
                         else _chat_text_blank(shown, _SUBJECT_PLACEHOLDER)))
            if found:
                return found
            # Words in angle brackets, by the one rule an email's body is
            # held to (_placeholder_shaped: "Hi <first name>"; not
            # "<sam@x.example>", "x<y and y>z", "<<Newsletter>>"), with no
            # code in a subject.  Looked for after every other shape, so a
            # blank found before is named the same; then the owner's rule,
            # as written and as decoded ("&#91;UPDATE&#93; pricing").
            return (_placeholder_in(body) or _placeholder_in(shown)
                    or _owner_blank_in(body) or _owner_blank_in(shown))
        # An email body is HTML, read once as its reader's parser reads it
        # (_read_email).  The underscore shape in what the check reads, as
        # written (review rv_c092 of c092acb4b: a comment or code block is
        # no words to its reader); then as decoded (rv_c092 #2: the reader
        # sees "&lt;first_name&gt;" as <first_name>; decoded after the
        # comments are cut, since "&lt;!--" opens none); then in the words
        # the reader sees.  The text as written is read first, so every
        # blank found before is found and named the same.
        # The reading is decoded already, as the reader sees it (libxml2
        # decodes "&lt;first_name&gt;"), so it is read once: braces, then
        # the underscore shape, in what the check reads and in the words the
        # reader sees ("my_list[item_index]" glued to a word stays code).
        # A blank in capitals glued to a word is code only inside a code
        # block: in prose it is the blank ("Dear[FIRST_NAME],", "Order
        # 12[ORDER_ID]"; review rv_52c3), as the reader sees it.
        seen, read, seen_words, markup, lone = _read_email(body)
        match = (_BRACE_PLACEHOLDER.search(read)
                 or _TEXT_PLACEHOLDER.search(read)
                 or _BRACE_PLACEHOLDER.search(seen_words)
                 or _TEXT_PLACEHOLDER.search(seen_words)
                 or _PROSE_CAPS_BLANK.search(seen_words))
        if match:
            return match.group(0)
        # The ask hint's blanks and the whole-text blank, in the words and
        # the tags written where a value belongs: a tag the reader drops is
        # still a blank left in ("<p>Q: <your question?></p>" reads "Q:", a body of
        # "<your message here>" is an empty email; review rv_c0f5, each
        # refused before 310d4a93e).  A comment that opens with the hint's
        # words is no blank, nor is one tag of markup (_is_markup_tag): an
        # image-only email, "<p>", "<td nowrap>" (reviews rv_c092, rv_fc0c).
        blank = _ask_hint_blank_in(markup)
        if blank:
            return blank
        one = markup.strip()
        if one and unfilled_key_placeholder(one) == one and not _is_markup_tag(one):
            return one
        # The blank the reader meets in a line of the tree libxml2 builds
        # (_EmailReading.blank): "<p>Hi Ana,</p><p><your message here></p>"
        # reads "Hi Ana," and a gap (review rv_fc0c); "Hi &lt;first
        # name&gt;, thanks" and "Hi <first name>, thanks" (hartos-e1 #7).
        # Then the owner's rule in the words the reader sees (an image's alt
        # text with them), and in attribute values and styles for the
        # capitals and merge tags (href="...?n=%FIRSTNAME%"); then nothing
        # to read at all ('': "</pre>", "<p></p>", an image with no alt).
        found = lone or _owner_blank_in(seen_words) or _owner_blank_in(read, prose=False)
        if found:
            return found
        return None if seen.strip() or seen_words.strip() else ''
    return _chat_text_blank(body)


def _chat_text_blank(body: str, shape=_TEXT_PLACEHOLDER) -> Optional[str]:
    """The three shapes of unfilled_text_placeholder in a text read as it
    is written (a message, an ask, an email's subject): the whole text one
    blank, the underscore shape (``shape``: a subject's has no code), an ask
    hint's blank."""
    if body and unfilled_key_placeholder(body) == body:
        return body
    match = shape.search(body)
    return match.group(0) if match else _ask_hint_blank_in(body)


#: A '<...>' in a text, the shape of the ask hint's blanks.
_BRACKETED = re.compile(r'<([^<>]*)>')


def _ask_hint_blank_in(body: str) -> Optional[str]:
    """The first '<...>' in ``body`` whose words open with the words of one
    of the blanks in the ask tool's own example, else None.  The blanks are
    read from the hint the model is given (core.agent_tool_menu.
    ASK_PERSON_SHAPE, at each call), so a reworded example is caught without
    a second list.  Whole words, any case, punctuation ignored: "<your
    question>" and "<Your question: which day>" are the example's "<your
    question?>"; "<why you asked>" and "<2 kg" are not."""
    from core import agent_tool_menu
    blanks = [w for w in (re.findall(r'\w+', b.lower())
                          for b in _BRACKETED.findall(agent_tool_menu.ASK_PERSON_SHAPE))
              if w]
    for m in _BRACKETED.finditer(body):
        inner = re.findall(r'\w+', m.group(1).lower())
        if any(inner[:len(b)] == b for b in blanks):
            return m.group(0)
    return None


def _placeholder_text_refusal(tool, placeholder) -> str:
    """The failed-call reply for a message unfilled_text_placeholder found
    a mark in; nothing is sent.  Built by core.tool_refusal.refuse, so it
    reads as a failed call and says what to change before calling again."""
    from core.tool_refusal import refuse
    return refuse(tool, f"the text still holds {placeholder}, which the "
                        f"person would read as it is",
                  "Put the real value in its place, or leave that part out "
                  "when it is not known, then send the text")


def _placeholder_key_refusal(tool, key, placeholder) -> str:
    """The failed-call reply for a key ``unfilled_key_placeholder`` found,
    built by core.tool_refusal.refuse: it starts with "Error:", so
    core.constants.tool_reply_failed reads it as a failed call and the
    fabrication gate re-steers instead of crediting it, and it says what to
    change before calling again."""
    from core.tool_refusal import refuse
    return refuse(tool, f"the key {key} still holds the placeholder {placeholder}",
                  "Look up the real value and put it in the placeholder's place "
                  "(for example, a user id comes from get_user_id)")


def key_not_found_reply(own, key) -> str:
    """get_data_by_key's answer when nothing is saved at ``key``.

    It opens with the sentence agents' saved steps already quote ("'Key not
    found' means a new learner"), so those steps read it as before, and adds
    what THIS PERSON has saved where the key's path stopped, so the next call
    can use a real key instead of guessing another (owner 2026-11-09: every
    refusal says what was wrong; measured that day, 22 'Key not found'
    replies in two days and no hint of which keys exist).  It is a real
    answer, not a failed call: a key nobody saved yet is how a first visit
    looks.

    ``own`` is the person's OWN store (person_store_id), never the agent's
    shared store.  The shared store holds templates and, on older agents,
    records saved before the per-person ruling, other people's among them:
    measured on the owner's desktop 2026-11-09 (review of 2ddf102da), agent
    54's shared 'teach' is the creator's own progress and agent
    96985165407's shared 'tutor' holds another person's id.  The first cut
    named keys from the whole view and told the model to read them, steering
    a new learner's turn to someone else's record.  It also said "read that
    key" when no key was named; now that is said only after a list.
    """
    node, walked = own if isinstance(own, dict) else {}, []
    for part in str(key or '').split('.'):
        if isinstance(node, dict) and part in node:
            node, walked = node[part], walked + [part]
        else:
            break
    where = '.'.join(walked)
    names = sorted(str(k) for k in node) if isinstance(node, dict) else []
    if walked and not isinstance(node, dict):
        known = (f" This person's saved {where} is a single value, with no keys "
                 f"under it.")
    elif not names:
        known = (f" This person has nothing saved under {where}." if where
                 else " This person has nothing saved in this memory yet.")
    else:
        shown = ', '.join(names[:20]) + (', ...' if len(names) > 20 else '')
        known = ((f" This person's keys under {where} are: {shown}." if where
                  else f" This person's saved top-level keys are: {shown}.")
                 + " If you meant one of those, read that key.")
    return (f"{KEY_NOT_FOUND} Nothing is saved at {key}.{known} If nothing was "
            f"saved yet, this is a first visit: carry on without it.")


#: The opening sentence of get_data_by_key's not-found answer, quoted by
#: agents' saved steps, and the sentinel code here compares against.
KEY_NOT_FOUND = "Key not found in stored data."


def build_core_tool_closures(ctx):
    """Build session-scoped tool closures.  Returns list of (name, desc, func).

    Args:
        ctx: dict with session variables:
            user_id, prompt_id, agent_data, helper_fun, user_prompt,
            request_id_list, recent_file_id, scheduler,
            simplemem_store (optional), memory_graph (optional),
            log_tool_execution (decorator), send_message_to_user1 (func),
            retrieve_json (func), strip_json_values (func),
            save_conversation_db (func),
            building_agent (optional, True in CREATE: the agent's creator
            may then save a template or example for everyone)
    """
    # Unpack context -------------------------------------------------------
    user_id = ctx['user_id']
    prompt_id = ctx['prompt_id']
    agent_data = ctx['agent_data']
    helper_fun = ctx['helper_fun']
    user_prompt = ctx['user_prompt']
    request_id_list = ctx['request_id_list']
    recent_file_id = ctx['recent_file_id']
    scheduler = ctx['scheduler']
    simplemem_store = ctx.get('simplemem_store')
    memory_graph = ctx.get('memory_graph')
    # log_tool_execution: optional decorator (create_recipe.py has it, reuse_recipe.py may not)
    log_tool_execution = ctx.get('log_tool_execution') or (lambda f: f)
    send_message_to_user1 = ctx['send_message_to_user1']
    _retrieve_json = ctx['retrieve_json']
    _strip_json_values = ctx['strip_json_values']
    save_conversation_db = ctx['save_conversation_db']

    # This person's memory for this agent (memory_view): the memory tools
    # write here, and read here before the agent's own store.
    own_key = person_store_id(user_prompt)
    # CREATE (hartos/create_recipe.py) sets it: the agent is being built,
    # so its creator may save a template or example for everyone.
    building_agent = bool(ctx.get('building_agent'))

    def _is_creator():
        """Any user can land in CREATE on a half-built agent
        (hart_intelligence_entry sends a no-recipe turn there), so being in
        CREATE is not enough to write for everyone: is_agent_creator."""
        return is_agent_creator(user_id, prompt_id)

    def _own_store():
        """The person's store, restored from disk when the cache lacks it,
        so a first save after a restart adds to it instead of replacing it."""
        if own_key not in agent_data:
            helper_fun.load_agent_data_from_file(own_key, agent_data)
        return agent_data.setdefault(own_key, {})

    def _saved_in_this_memory(path):
        """Whether this person's memory holds a value at ``path``: their own
        store, or, for the agent's creator, the agent's own store, where the
        creator's saves went before the 2026-10-17 ruling.  Never another
        person's record (key_not_found_reply's rule: a key named from the
        agent's shared store steered a new learner to someone else's
        record).  Only reads; never raises."""
        def _at(node):
            for part in str(path).split('.'):
                node = node.get(part) if isinstance(node, dict) else None
            return node
        try:
            if _at(agent_data.get(own_key)) is not None:
                return True
            return (_at(agent_data.get(prompt_id)) is not None
                    and is_agent_creator(user_id, prompt_id))
        except Exception:
            return False

    # The example key both key descriptions gave until 28a9001b0.  The 4B
    # copies a description's example, so it read 'user.info.name' where
    # nothing was saved and asked for the name again and again (cafe agent,
    # 18 Nov).  But the model also saved under it: 14 stores in 13 agents on
    # the owner's desktop hold a value there, and without the example their
    # next sessions were no longer steered to their own record (rv_28a9 S2).
    # So a memory that holds a value at it keeps the example; any other
    # memory is shown no key name (the owner's 11-03 ruling).
    _key_example = (" (e.g., 'user.info.name', saved in this memory)"
                    if _saved_in_this_memory('user.info.name') else '')

    tools: List[Tuple[str, str, Any]] = []

    # ------------------------------------------------------------------
    # 1. text_2_image
    # ------------------------------------------------------------------
    def _make_image(text, called_as):
        url = helper_fun.txt2img(text)
        if url:
            return url
        # txt2img answers '' for a failed request, an open circuit breaker
        # and a reply with no image alike.  Handed that '' as is, the model
        # called again: central 2026-11-19, two goals paused needs_help on a
        # text_2_image step ('did not complete after 3 attempts', 'looped').
        # Refused in the name it was called by: the gates take a refusal as
        # the step's receipt only from the called tool itself
        # (core.tool_refusal.refusal_lets_step_finish).
        from core.tool_refusal import image_not_made
        return image_not_made(called_as, 'it is unreachable or busy')

    @log_tool_execution
    def text_2_image(text: Annotated[str, "Text to create image"]) -> str:
        return _make_image(text, 'text_2_image')

    tools.append((
        "text_2_image",
        "Text to image Creator",
        text_2_image,
    ))

    # Alias — reuse_recipe.py main flow LLM prompts advertise `txt2img` (#510).
    # Same canonical work (helper_fun.txt2img — local-first, sovereignty).
    @log_tool_execution
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        return _make_image(text, 'txt2img')

    tools.append((
        "txt2img",
        "Text to image Creator (alias of text_2_image)",
        txt2img,
    ))

    # ------------------------------------------------------------------
    # 2. get_user_camera_inp
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_camera_inp(
        inp: Annotated[str, "A question about what the user's live camera shows right now, e.g. 'what is the user holding?'"],
    ) -> str:
        # No int() — user_id is a UUID on desktop installs and int() raised
        # on every call (152/152 failures across three log rotations,
        # 10/10 on 2026-09-07).  The callee never needs an int: helper.py:2163
        # does get_frame(str(user_id)) and :2165 interpolates it into a
        # filename.  An integer id still passes through unchanged.
        _rid = camera_turn_id()
        if not turn_may_see_the_camera():
            tool_logger.warning(f"get_user_camera_inp refused on turn {_rid!r}: "
                                f"not the person's own")
            return camera_refusal('get_user_camera_inp')
        return helper_fun.get_user_camera_inp(inp, user_id, _rid)

    # The schema says what the tool does.  It read "Get user's visual
    # information to process somethings" with one argument "The Question to
    # check from visual context" -- on the main leg the only core tool besides
    # google_search that takes any question -- and goals whose own tool was
    # missing asked it about gradient sync and build status: 732 calls
    # 2026-09-22..29 on the MSI desktop, none with a frame (helper.py
    # get_user_camera_inp now raises for that).
    tools.append((
        "get_user_camera_inp",
        "Look through the user's live camera and answer a question about what "
        "it shows right now. Only for what is in front of the camera: it cannot "
        "answer anything else, and it fails when the user is not sharing a "
        "camera.",
        get_user_camera_inp,
    ))

    # ------------------------------------------------------------------
    # 2b. start_demonstration / stop_demonstration
    # ------------------------------------------------------------------
    # Owner 2026-11-11: when the person says "I'll show you how", the agent
    # invokes the demonstration recorder (integrations/vlm/demo_session.py);
    # the ribbon shows a recording symbol, Stop is the ribbon's own.  Not in
    # MAIN_LEG_CORE_TOOLS on purpose: an agent that needs it attaches it by
    # name or through request_tools, so the main leg's schema budget is
    # untouched.  Both tools answer only the person's OWN turn, on the desktop
    # owner's own account: a goal, a peer agent's relay or another user must
    # never watch the owner's mouse and keyboard, or read what they did.
    def _demonstration_refusal(tool_name):
        """None when this turn may start or read a demonstration, else the
        refusal that says what is wrong and what to do."""
        from core.tool_refusal import RETRY_LATER, RETRY_NEVER, refuse
        if not is_a_persons_request(camera_turn_id()):
            return refuse(
                tool_name,
                "this turn is not the person's own message (it is a goal running "
                "in the background, another agent's request, or a scheduled "
                "run), and only the person may start or read a recording of what "
                "they do on their own computer",
                "Do this step without a demonstration; if the person wants to "
                "show you something, wait for them to say so in chat",
                retry=RETRY_LATER)
        from integrations.agent_engine.capability_setup import setup_owner
        owner = setup_owner()
        if not owner:
            return refuse(
                tool_name,
                "no one is signed in as this computer's owner, so there is "
                "nobody whose mouse and keyboard may be recorded",
                "Ask the person to sign in to the desktop app, then try again",
                retry=RETRY_LATER)
        if str(user_id) != str(owner):
            return refuse(
                tool_name,
                "a recording would capture the desktop owner's mouse and "
                "keyboard, and the person asking is not the owner",
                "Ask the owner to show it from their own account; you cannot "
                "record someone else's computer",
                retry=RETRY_NEVER)
        return None

    @log_tool_execution
    def start_demonstration(
        what_will_be_shown: Annotated[str, "What the person is about to show you, in a few words, e.g. 'how I file my expenses'"],
    ) -> str:
        refusal = _demonstration_refusal('start_demonstration')
        if refusal:
            return refusal
        from core.tool_refusal import RETRY_LATER, refuse
        from integrations.vlm import demo_session
        try:
            session = demo_session.begin(user_id, prompt_id)
        except demo_session.DemoRefused as refused:
            return refuse('start_demonstration', refused.problem, refused.fix,
                          retry=RETRY_LATER)
        return (f"Recording started. The person's screen shows the red "
                f"recording ribbon, and it ends by itself after "
                f"{int(session.max_seconds // 60)} minutes or when they press "
                f"Stop AI control. Tell them to go ahead and show you, and to say "
                f"when they are done, then call stop_demonstration. It records "
                f"which windows and controls they use (clicks, scrolls, key "
                f"presses like Enter or Ctrl+S, and that they typed), never what "
                f"they type. It does not record screenshots or captions of the "
                f"screen, or middle and side mouse buttons, so do not promise "
                f"those. Nothing is saved to disk, but the words you get back "
                f"(window titles and control names, never what they type) go to "
                f"the AI model running this chat, which may be an online model: "
                f"tell the person that before they start.")

    tools.append((
        "start_demonstration",
        "Start recording while the person shows you how to do something on "
        "their own computer. Call it only when they say they will show you. "
        "A recording ribbon is shown to them and they can stop it any time.",
        start_demonstration,
    ))

    @log_tool_execution
    def stop_demonstration() -> str:
        refusal = _demonstration_refusal('stop_demonstration')
        if refusal:
            return refusal
        from core.tool_refusal import RETRY_AFTER_FIX, refuse
        from integrations.vlm import demo_session
        session = demo_session.active_session()
        if session is not None:
            result = session.stop('person')
            demo_session.take_finished(user_id)      # the ribbon may have ended it first
        else:
            # The person's own Stop, the time box or a lost ribbon ended the
            # recording before this call: what they showed was kept for it.
            result = demo_session.take_finished(user_id)
            if result is None:
                return refuse(
                    'stop_demonstration', "no demonstration is being recorded right now",
                    "Call start_demonstration first, and only when the person says "
                    "they will show you how", retry=RETRY_AFTER_FIX)
        # Read from the result on both paths: when the ribbon's Stop wins the race
        # inside session.stop('person'), the result is the ribbon's.
        how = demo_session.how_it_ended(result.ended_by)
        return (f"The demonstration is over ({result.seconds:g} seconds{how}). What the "
                f"person did, in order (what they typed is never recorded, only "
                f"that they typed):\n{result.transcript}\n"
                f"Turn this into the steps of the agent you are building: ask "
                f"the person for any text they typed, and use their vault "
                f"({{{{secret:NAME}}}}) for a secret.")

    tools.append((
        "stop_demonstration",
        "Stop the recording the person's demonstration started, and get what "
        "they did as numbered steps in words.",
        stop_demonstration,
    ))

    # ------------------------------------------------------------------
    # 3. save_data_in_memory
    # ------------------------------------------------------------------
    @log_tool_execution
    def save_data_in_memory(
        key: Annotated[str, f"Key path for storing data now & retrieving data later, a name you choose for this value. Use dot notation for nested keys{_key_example}. Read it back later with this same key."],
        value: Annotated[Optional[Any], "Value you want to store; strictly should be one of int, float, bool, json array or json object."] = None,
        for_everyone: Annotated[bool, "True only when the agent's creator is building it, and only for a template, or an example every user of the agent can start from (a form to fill, a sample lesson). Never a person's own data. Otherwise the value is this person's alone."] = False,
    ) -> str:
        """Store data with validation to prevent corruption.

        The value is this person's memory for the agent (memory_view).
        Owner ruling 2026-10-17: what is saved while the agent is being
        built is the creator's data too, except a template, or data that
        can serve as an example, which ``for_everyone`` puts in the agent's
        own store for every user when the agent's creator
        (is_agent_creator) saves it while building.  Anyone else, and
        anyone outside building, keeps the value personal, so no user's
        save reaches another user.
        """
        tool_logger.info('INSIDE save_data_in_memory')
        _placeholder = unfilled_key_placeholder(key)
        if _placeholder:
            tool_logger.warning(f"save_data_in_memory refused: key {key!r} holds "
                                f"the placeholder {_placeholder!r}")
            return _placeholder_key_refusal('save_data_in_memory', key, _placeholder)
        # An outline's mark ("<saved_str>") stands where a saved value is; it
        # is never the value.  rv_88d7's review of 88d7e3aac: a copied
        # '<saved_str>' was saved over the email's body, "Saved", and the
        # real body was gone.  Only that mark: a template the agent saves on
        # purpose may hold other fill-in marks.
        from core.constants import OUTLINE_MARKER_RE
        try:
            _flat = value if isinstance(value, str) else json.dumps(value, default=str)
        except Exception:
            _flat = str(value)
        _marker = OUTLINE_MARKER_RE.search(_flat or '')
        if _marker:
            tool_logger.warning(f"save_data_in_memory refused: the value for {key!r} "
                                f"holds the outline mark {_marker.group(0)!r}")
            from core.tool_refusal import refuse
            return refuse('save_data_in_memory',
                          f"the value holds {_marker.group(0)}, the outline's mark "
                          f"for a value saved there, not the value itself",
                          f"Read the saved value with get_data_by_key('{key}') "
                          f"first, then save the real value")
        try:
            if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                value = _retrieve_json(value)
                tool_logger.info(f"REPAIRED JSON STRING: {value}")
            if value is not None:
                json_str = json.dumps(value)
                validated_value = json.loads(json_str)
                tool_logger.info(f"VALIDATED VALUE (post JSON cycle): {validated_value}")
            else:
                validated_value = None

            keys = key.split('.')
            # One reading of a bool that may arrive as the string "false"
            # (autogen passes the model's arguments as given).
            from core.config_cache import flag_value
            wants_shared = flag_value(for_everyone, False)
            shared = wants_shared and building_agent and _is_creator()
            if shared:
                # The agent's own store, restored first so this adds a key.
                if prompt_id not in agent_data or not agent_data[prompt_id]:
                    helper_fun.load_agent_data_from_file(prompt_id, agent_data)
                store_key = prompt_id
                d = agent_data.setdefault(prompt_id, {})
            else:
                store_key = own_key
                d = _own_store()
            # A saved text keeps its kind (#282): asked before the walk below,
            # which makes the parents, so a refused save changes nothing, in
            # memory, on disk or in the memory graph.
            _kept = _saved_text_refusal(key, keys, d, validated_value)
            if _kept:
                tool_logger.warning(f"save_data_in_memory refused: {key!r} holds "
                                    f"text, and the value would replace its kind")
                return _kept
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = validated_value
            tool_logger.info(f"VALUES STORED IN AGENT DATA: {validated_value}")
            tool_logger.info(f"FULL AGENT DATA AT KEY: {d}")

            if helper_fun.save_agent_data_to_file(store_key, agent_data):
                tool_logger.info(f"[OK] Data persisted to file for {store_key}")
            else:
                tool_logger.warning(f"Failed to persist data to file for {store_key}")

            # Mirror to MemoryGraph (fire-and-forget).  Carried by
            # reuse_recipe's inline twin before the #743 migration, lost
            # in the swap, restored here (owner audit 2026-09-01).  Pairs
            # with get_data_by_key's [KV] recall fallback below.
            if memory_graph is not None:
                try:
                    threading.Thread(target=lambda: memory_graph.register(
                        f"[KV] {key} = {json.dumps(validated_value)[:200]}",
                        {'memory_type': 'fact', 'source_agent': 'helper',
                         'session_id': user_prompt, 'kv_key': key},
                    ), daemon=True).start()
                except Exception:
                    pass

            try:
                # The value as stored, not the tool's page of it.
                stored_value = _read_saved(key)
                tool_logger.info(f"VERIFICATION - READ BACK VALUE: {stored_value}")
                if stored_value == _KEY_NOT_FOUND:
                    tool_logger.error(f"VERIFICATION FAILED: Data not properly stored at key {key}")
                    return f"Error: {key} was written but could not be read back"
            except Exception as e:
                tool_logger.error(f"VERIFICATION ERROR: {str(e)}")

            # Report the save, not the store. This returned the whole
            # agent_data store on every call, so each save put all of it in
            # the model's context and, through the group chat's write-back,
            # into memory again: on central 2026-09-14 (#104) the large
            # MemoryGraph rows were all this repr, 0.9M to 3.96M chars each.
            where = ''
            if shared:
                where = ' (for everyone who uses this agent)'
            elif wants_shared:
                where = (' (for this person only: a value is saved for everyone '
                         'only by the agent\'s creator while building it)')
            return _bounded_observation(
                f'Saved at {key}{where}: {json.dumps(validated_value)}',
                'the whole value was saved')
        except json.JSONDecodeError as je:
            error_msg = f"Invalid JSON structure in value: {str(je)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except TypeError as te:
            error_msg = f"Type error in value: {str(te)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"
        except Exception as e:
            error_msg = f"Unexpected error saving data: {str(e)}"
            tool_logger.error(error_msg)
            return f"Error: {error_msg} - Data not saved"

    # ------------------------------------------------------------------
    # bind_game_sound — a game's sounds, composed once and kept
    # ------------------------------------------------------------------
    @log_tool_execution
    def bind_game_sound(
        game_id: Annotated[str, "The game's id as the app knows it (a game config's id, e.g. 'eng-spell-animals-01')"],
        mood: Annotated[str, "How the game should feel: happy, calm, adventurous, triumphant"] = "happy",
        description: Annotated[str, "What happens in the game, for the composer"] = "",
        state: Annotated[str, "Which state of the game: bgm for the music under the game, or correct, wrong, streak, complete, starEarned, intro, countdownTick, countdownEnd, cardFlip, matchFound, dragStart, dragDrop, tap"] = "bgm",
        level: Annotated[str, "Only when THIS level needs its own sound; leave empty so every level of the game shares one"] = "",
        scope: Annotated[str, "'agent' is the agent's own sound, composed once and played for everyone who reuses it; 'mine' is a correction for this person alone"] = "agent",
    ) -> str:
        """Compose this game's background music and bind it to the game.

        Call it in CREATE for each game this agent plays with.  The music
        is composed by the node's media capability and recorded against
        this agent, so REUSE plays the same music rather than composing
        again, and the reviewer approves one piece of music per game.

        Idempotent: once a game is bound, calling it again returns the
        binding.  If the composer is still working, call it again later
        with the same game_id and it picks the task back up.
        """
        tool_logger.info(f'INSIDE bind_game_sound for game {game_id}')
        if not game_id or not str(game_id).strip():
            return "A game_id is required: use the game config's id."
        slot = str(game_id).strip()

        which = str(state or 'bgm').strip() or 'bgm'
        if which not in GAME_STATES:
            return (f"{which} is not one of a game's states. Use one of: "
                    f"{', '.join(sorted(GAME_STATES))}.")
        mine_only = str(scope or 'agent').strip() == 'mine'
        for_agent = not mine_only and _is_creator()

        def _games(own):
            """The person's own memo, or the agent's, looked up at each use:
            a card answered meanwhile drops the cached store, and a dict held
            from the start was an orphan (review of fafa03c40, R1)."""
            store = _own_store() if own else agent_data.setdefault(prompt_id, {})
            return store.setdefault('games', {})

        def _lookup(own_only=mine_only):
            """What this person's call finds: the creator reads the agent's
            memo alone, anyone else their own first.  Made again before any
            write (_chosen_meanwhile)."""
            return game_state_sound(
                _games(False), slot, which, level,
                own=None if for_agent else _games(True), own_only=own_only)

        bound, matched = _lookup()
        if mine_only:
            personal = True
        elif for_agent:
            personal = False
        else:
            # Composed once, replayed for everyone: an empty slot is filled
            # in the agent's memo by whoever reaches it first.  A choice is
            # the person's own: once they have turned the agent's take down,
            # their next take is composed for them alone, and the agent's
            # stays for everyone else (owner 2026-10-17).
            personal = not bound.get('url') and bool(
                game_state_record(_games(True), slot, which, level)
                or game_state_record(_games(True), slot, which))
            if personal:
                bound, matched = _lookup(own_only=True)
        store_id = own_key if personal else prompt_id

        def _memo():
            """The memo this call writes."""
            return _games(personal)

        def _already_bound(record, matched, note=None):
            """The answer for a state that has its take."""
            return json.dumps({
                'status': 'already_bound',
                'game_id': slot,
                'state': which,
                'matched': matched,
                'music': record,
                'note': note or f'This game already has its {which}; it is never composed twice.',
            })

        def _chosen_meanwhile():
            """A take that landed here while this call composed ("Keep it" on
            a card, another turn filling the slot), and what matched, else
            ({}, None).  Nothing this call holds is written over it: not its
            finished take (review of 4528e339d, F-1), task or submit (review
            of 0c542e278, F-A).  Asked of the call's own lookup (F-B), then of
            the key it writes, where a take this person turned down still
            holds the agent's slot for everyone else ('miss' for them)."""
            found, matched = _lookup()
            if found.get('url'):
                return found, matched
            here = game_state_record(_memo(), slot, which, level)
            return (here, 'miss') if here.get('url') else ({}, None)

        def _not_over(landed, matched, what):
            """The answer when ``what`` of this call's is not written over
            the take that landed meanwhile, and the log line that says so."""
            tool_logger.info(
                f'bind_game_sound {slot}/{which}: a take landed while this one '
                f'composed; {what} is not bound over it')
            if matched == 'miss':
                note = (f"The {which} chosen meanwhile is one this person turned "
                        f"down; call again to compose one of their own.")
            else:
                note = f'A {which} was chosen while this one composed; that one stays.'
            return _already_bound(landed, matched, note)

        def _still_ours(field, value):
            """Whether this call's composition is still the one at its key:
            its submit note (``submitted_at``) or its task (``task_id``).  A
            keep made meanwhile ended it there (record_verdict, the one rule
            for what a keep ends) and a take landing at the key replaced it,
            so nothing more of it is written (review of bc7bbf721, F-D1)."""
            return holds_composition(
                game_state_record(_memo(), slot, which, level), field, value)

        def _drop_note(note):
            """Clear this call's submit note when the submit left no
            composition behind it, so the key does not read 'composing' for
            SUBMIT_COOLDOWN_S after a refusal (without_submit_note)."""
            here = game_state_record(_memo(), slot, which, level)
            cleared = without_submit_note(here, note)
            if cleared == here:
                return
            if cleared:
                _remember(cleared)
            else:
                forget_game_state_record(_memo(), slot, which, level)
                _persist()

        def _ended(what):
            """The answer when a choice made while this call composed ended its
            composition, and none of the takes it covers is one this person
            hears: ``what`` of this call's is not bound."""
            tool_logger.info(
                f'bind_game_sound {slot}/{which}: a choice made while this one '
                f'composed ended it; {what} is not bound')
            return json.dumps({
                'status': 'not_bound',
                'game_id': slot,
                'state': which,
                'note': (f'A {which} was chosen while this one composed, so this '
                         f'one is not bound; call again to compose one.'),
            })

        if bound.get('url'):
            return _already_bound(bound, matched)
        # NO early return for a composition already under way.  It used to
        # return here saying "call again with the same arguments to finish
        # it" -- and calling again hit this same branch and said it again,
        # forever.  MEASURED 2026-09-22: twelve consecutive calls, the
        # composition finishing on the server in the middle of them, and the
        # memo never receiving the url.  The resume path below (`task_id =
        # bound.get('task_id')`, which skips the submit and polls the
        # existing task) was unreachable, so the note was a promise the code
        # could not keep.  Falling through IS the dedupe: an existing
        # task_id means poll it, never start a second composition.

        def _persist():
            try:
                helper_fun.save_agent_data_to_file(store_id, agent_data)
            except Exception as e:
                tool_logger.warning(f'bind_game_sound could not persist: {e}')

        def _remember(record):
            record = set_game_state_sound(_memo(), slot, which, record, level)
            _persist()
            return record

        def _offer(record):
            """Hand a finished piece to the person, to hear and answer."""
            if record.get('url'):
                offer_sound_for_review(user_id, prompt_id, slot, which, record)
            return record

        try:
            from integrations.service_tools.media_agent import (
                MEDIA_FAILED_STATUSES,
                _reads_as_still_waking,
                check_media_status,
                generate_media,
            )
        except ImportError as e:
            tool_logger.warning(f'bind_game_sound: no media capability ({e})')
            return ("This node cannot compose music (the media capability is "
                    "not available here), so the game keeps no sound.")

        def _failure_kind(result):
            """Why a composer call failed, told apart by the module that wrote it.

            media_agent.classify_error is the reader that lives next to the
            returns it reads (hartos-94, HARTOS 11d0aebee), and it makes
            distinctions a prose match here could not: a node with NOTHING
            installed should be offered an install; an AceStep that is
            installed and merely not running -- or will not fit beside
            whatever holds the GPU -- must be waited for: not offered again
            (installing what is on the disk is its own defect) and not
            reported as a refusal.
            """
            try:
                from integrations.service_tools.media_agent import classify_error
                return classify_error(result)
            except Exception:
                return None

        def _no_composer_here(result):
            try:
                from integrations.service_tools.media_agent import ABSENT
            except Exception:
                return False
            return _failure_kind(result) == ABSENT

        def _composer_not_up(result):
            """Run 8, 2026-09-22: a 3 GB llama-server on the card made the
            runtime refuse to start the composer, and this tool told the
            agent the composer REFUSED the game's music.  Nothing was posted,
            so nothing is remembered; the next call simply asks again."""
            try:
                from integrations.service_tools.media_agent import UNREACHABLE
            except Exception:
                return False
            return _failure_kind(result) == UNREACHABLE

        def _ask_for_a_composer(why):
            """Offer to set a music model up, rather than failing quietly.

            A node with no composer cannot give a game its sounds, and
            silence tells the person nothing.  The ask is the canonical
            consent card, scoped to this one capability, and the owner's
            yes routes into the provisioning that already exists.
            """
            try:
                from integrations.agent_engine.capability_setup import (
                    request_capability_setup)
                outcome = request_capability_setup(
                    'music:acestep',
                    reason=(f"To give {slot} its {which} sound I need a music "
                            f"model on this computer. May I set one up?"),
                    category='subprocess.tool_load',
                    # NOT 'backend': that key is what the TTS venv repair
                    # tool reads, and its documented backends are TTS engine
                    # ids only (backend_repair_tools) -- so naming acestep
                    # there sent a granted consent into a repair path aimed
                    # at a tool that cannot install a music model.  With no
                    # backend, goal_manager routes tool_load to dependency
                    # remediation instead of a venv rebuild, which is what
                    # a missing music engine actually needs.
                    context={'tool': 'acestep', 'game_id': slot,
                             'state': which},
                )
            except Exception as ask_error:
                tool_logger.warning(f'could not offer a composer: {ask_error}')
                outcome = 'unavailable'
            return json.dumps({
                'status': 'needs_capability',
                'capability': 'music:acestep',
                'game_id': slot,
                'state': which,
                'asked': outcome,
                'why': why,
                'note': {
                    'provisioning': 'Setting the music model up now; ask again '
                                    'once it is ready.',
                    'asked': 'I have asked the owner of this computer whether '
                             'I may set a music model up.',
                    'declined': 'The owner said no to a music model, so this '
                                'game keeps the sounds it already has.',
                    'unavailable': 'There is nobody to ask on this node, so no '
                                   'sound can be composed here.',
                }.get(outcome, 'No music model is available on this node.'),
            })

        prompt = GAME_STATES[which].format(
            what=description or slot, mood=mood)
        # A take the reviewer rejected is composed again WITH the reason
        # they gave, and kept as the next variant (spec §6.1).
        rejected = rejected_take(_memo(), slot, which, level)
        variant = take_variant(rejected) + 1 if rejected else 1
        if rejected.get('rejected_reason'):
            prompt = f"{prompt}. Not like the last one: {rejected['rejected_reason']}"
        # The takes turned down before this one are carried by the memo's
        # setters (set_game_state_sound), not built here: every record this
        # call writes REPLACES the one at this key, and the setter carries
        # that record's history forward, on a resumed take too, where a list
        # built from `rejected` at the start was empty (review of
        # ed6576624, F3).
        task_id = bound.get('task_id')
        labels = {'mood': mood, 'prompt': prompt, 'variant': variant}
        resumed = task_labels(bound) if task_id else {}

        def _take(**fields):
            """This call's take as a record, ``fields`` on top; a task picked up
            again keeps what it was composed with (task_take; c67afc26e, F-1)."""
            return {**labels, 'state': which, 'level': level or None,
                    'composed_at': None, 'approved_at': None, **resumed, **fields}

        def _bind_finished(url, ours):
            """Bind a finished take, unless a take landed at this state while
            it composed (_chosen_meanwhile) or a choice ended its composition
            (``ours()`` is False, _still_ours)."""
            landed, matched = _chosen_meanwhile()
            if landed.get('url') == url and matched != 'miss':
                # Another call polling the same task bound it first: nothing
                # to write, and no second card (review of 0c542e278, F-C).
                return _already_bound(landed, matched)
            if landed:
                # Its job stays behind that take, to be bound if it is turned
                # down, as between calls; a keep ended it (review of 5feeee2b8, F1).
                return _not_over(landed, matched, url)
            if not ours():
                # A keep this person does not hear ended it: the visitor
                # turned the creator's kept take down while their job filled
                # the agent's slot, and it was bound there over the keep
                # (found reworking bc7bbf721).
                return _ended(url)
            record = _offer(_remember(_take(url=url, composed_at=time.time())))
            return json.dumps({'status': 'bound', 'game_id': slot,
                               'state': which, 'music': record})

        def _forget_task(reason):
            """Drop a task the composer will never finish, keep the rest.

            hartos-3a F2: nothing ever cleared a dead task_id, so after a
            composer restart this state answered "composing" for good, and
            after a failure "failed" for good.  History and variant stay."""
            _remember(ended_composition(
                game_state_record(_memo(), slot, which, level), reason, task_id))

        # A record from before task_since existed has no age to judge; it is
        # polled as before rather than composed a second time.
        if (task_id and bound.get('task_since')
                and time.time() - float(bound['task_since']) > TASK_STALE_S):
            _forget_task('no answer within TASK_STALE_S')
            task_id, resumed = None, {}
        try:
            if not task_id:
                # A submit whose RESPONSE timed out may still have been
                # ACCEPTED.  MEASURED 2026-09-22 on a live AceStep: two
                # 'warming_up' answers, then a third submit that got an id --
                # and /v1/stats reported FIVE jobs from this one caller
                # (2 succeeded, 1 running, 2 queued, avg 907s each).  Every
                # retry had enqueued a real job the client never learned the
                # id of, and the one id it did hold sat "queued" behind its
                # own orphans.  /release_task takes no idempotency key, so
                # the only dedupe is here: after a timed-out submit, do not
                # submit again for this state until a cooldown has passed.
                # One reading of it, the memo's, guarded (review of 14c6bbd5f, F-C).
                pending = game_state_record(_memo(), slot, which, level)
                if not pending.get('task_id') and submit_in_flight(pending):
                    # Another call's submit for this key is out, or one whose
                    # reply timed out may be queued: wait for it, and say what.
                    waited = int(max(0.0, time.time() - float(pending['submitted_at'])))
                    return json.dumps({
                        'status': 'composing',
                        'game_id': slot,
                        'state': which,
                        'level': level or None,
                        'note': (f"The {which} for {slot}"
                                 f"{f' level {level}' if level else ''} is being "
                                 f"composed: a submission went out {waited} s ago "
                                 f"and may be in the composer's queue. Waiting "
                                 f"for it rather than queueing a second."),
                    })
                # The note goes in BEFORE the submit (submit_note): a keep made
                # while it waits ends it in record_verdict, as it ends any job,
                # and every write below asks _still_ours first (review of
                # bc7bbf721).  A note the submit left no composition behind is
                # cleared on the one way out (finally).
                noted, note = submit_note(pending, labels)
                _remember(noted)
                started = {}
                try:
                    started = json.loads(generate_media(
                        context=prompt,
                        output_modality='audio_music',
                        input_text=prompt,
                        # per state: a chime is two seconds, a loop is thirty (spec 3).
                        duration=GAME_STATE_DURATIONS.get(which, 30),
                        style=mood,
                    ))
                    if started.get('status') == 'completed':
                        results = started.get('results') or []
                        url = results[0].get('url') if results else None
                        if url:
                            return _bind_finished(
                                url, lambda: _still_ours('submitted_at', note))
                        return "The composer answered without any music; nothing bound."
                    if started.get('status') == 'warming_up':
                        # Not a refusal: the composer is getting ready, which on
                        # a first run means downloading its model.  Saying it
                        # refused would be wrong AND would leave the game with
                        # nothing pending to come back to.  And the POST may have
                        # been accepted: the note stays, timed from now, so the
                        # next call waits instead of queueing a duplicate.  Written
                        # ON TOP of what this key already holds: a take the
                        # reviewer turned down keeps its rejected_url, its reason
                        # and its variant, so the next call still answers them.
                        # MEASURED 2026-09-22 (hartos-14): replacing the record
                        # here erased all three -- on a node restarted overnight,
                        # which is exactly when a rejection is waiting.  Only
                        # while the composition is still this call's (review of
                        # 0c542e278, F-A; bc7bbf721, F-D4).
                        landed, matched = _chosen_meanwhile()
                        ours = _still_ours('submitted_at', note)
                        if ours:
                            _remember({**_take(),
                                       **game_state_record(_memo(), slot, which, level),
                                       'submitted_at': time.time()})
                        if landed:
                            return _not_over(landed, matched, 'a submit still queued')
                        if not ours:
                            return _ended('a submit still queued')
                        return json.dumps({
                            'status': 'composing',
                            'game_id': slot,
                            'state': which,
                            'note': started.get(
                                'message',
                                'The composer is starting up; ask again shortly.'),
                        })
                    if started.get('status') != 'pending':
                        why = str(started.get('error', 'unknown reason'))
                        if _no_composer_here(started):
                            return _ask_for_a_composer(why)
                        if _composer_not_up(started):
                            return (f"The composer is installed but not running "
                                    f"right now ({why}). Nothing was started; ask "
                                    f"again in a while by calling bind_game_sound "
                                    f"with the same game_id and state.")
                        return f"The composer refused this game's music: {why}"
                    task_id = started.get('task_id')
                    # The submit can wait two minutes: the task is written only
                    # while the composition is still this call's, never over a
                    # take that landed at its key (review of 0c542e278, F-A)
                    # nor behind a keep that ended it (bc7bbf721, F-D1, F-D2).
                    landed, matched = _chosen_meanwhile()
                    ours = _still_ours('submitted_at', note)
                    if ours:
                        _remember(_take(task_id=task_id, task_since=time.time(),
                                        task_take=labels))
                    if landed:
                        return _not_over(landed, matched, f'task {task_id}')
                    if not ours:
                        return _ended(f'task {task_id}')
                finally:
                    if not (isinstance(started, dict)
                            and started.get('status') == 'warming_up'):
                        _drop_note(note)

            # Give it a while, then hand the task back rather than block.
            deadline = time.time() + 90
            while time.time() < deadline:
                time.sleep(3)
                progress = json.loads(check_media_status(task_id))
                state = progress.get('status')
                if state in ('complete', 'completed', 'done'):
                    results = progress.get('results') or []
                    url = (progress.get('url')
                           or (results[0].get('url') if results else None))
                    if not url:
                        return "The composer finished without any music; nothing bound."
                    return _bind_finished(url, lambda: _still_ours('task_id', task_id))
                if state in MEDIA_FAILED_STATUSES:
                    why = str(progress.get('error', 'unknown reason'))
                    if progress.get('unreachable') and _reads_as_still_waking(why):
                        # The POLL can be reset by a busy server just as the
                        # submit can (MEASURED 2026-09-22: attempts 9-10 of a
                        # live bind reported "failed" on ConnectionResetError
                        # 10054 while the composer was mid-generation and went
                        # on to finish).  Keep polling; the deadline below
                        # still hands the task back as 'composing'.
                        continue
                    if _no_composer_here(progress):
                        return _ask_for_a_composer(why)
                    # the task is over: the next call composes this state again
                    # (hartos-3a F2)
                    _forget_task(why)
                    return (f"The composer failed on this game: {why}. Call "
                            f"bind_game_sound again to compose it afresh.")
            return json.dumps({
                'status': 'composing',
                'game_id': slot,
                'state': which,
                'task_id': task_id,
                'note': 'Still composing. Call bind_game_sound again with the '
                        'same game_id and state to finish binding it.',
            })
        except Exception as e:
            tool_logger.warning(f'bind_game_sound failed for {slot}: {e}')
            return f"Could not bind this game's sound: {e}"

    tools.append((
        "bind_game_sound",
        "Compose a kids game's background music and bind it to that game for "
        "good, so every later run of this agent plays the same music. Pass the "
        "game's id, a mood and a short description. Call it once per game.",
        bind_game_sound,
    ))

    # ------------------------------------------------------------------
    # get_game_sound — what a game is bound to play
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_game_sound(
        game_id: Annotated[str, "The game's id as the app knows it"],
        state: Annotated[str, "Which state of the game: bgm, correct, wrong, complete, intro…"] = "bgm",
        level: Annotated[str, "The level being played, when levels have their own sounds"] = "",
    ) -> str:
        """A sound this game is bound to play. REUSE reads it; it never composes."""
        slot = str(game_id or '').strip()
        which = str(state or 'bgm').strip() or 'bgm'
        if which not in GAME_STATES:
            return (f"{which} is not one of a game's states. Use one of: "
                    f"{', '.join(sorted(GAME_STATES))}.")
        level = str(level or '').strip()
        bound, matched = game_state_sound(
            agent_data.get(prompt_id, {}).get('games', {}), slot, which, level,
            own=_own_store().get('games', {}))
        if bound.get('url'):
            return json.dumps({
                'status': 'bound',
                'game_id': slot,
                'state': which,
                'matched': matched,
                'approved': bool(bound.get('approved_at')),
                'music': bound,
            })
        # A task, or a submit still out (its note is in the memo before the
        # submit goes, bind_game_sound): both are a composition under way,
        # which REUSE reads as composing, never as nothing bound.
        if bound.get('task_id') or submit_in_flight(bound):
            return json.dumps({'status': 'composing', 'game_id': slot,
                               'state': which, 'task_id': bound.get('task_id')})
        return json.dumps({'status': 'unbound', 'game_id': slot, 'state': which,
                           'note': f'No {which} is bound to this game yet.'})

    # ------------------------------------------------------------------
    # approve_game_sound — the reviewer's word on a game's music
    # ------------------------------------------------------------------
    @log_tool_execution
    def approve_game_sound(
        game_id: Annotated[str, "The game whose sound the reviewer just approved"],
        approved: Annotated[bool, "True when the reviewer accepts this sound, False to drop it so it can be composed again"] = True,
        state: Annotated[str, "Which state of the game: bgm, correct, wrong, complete, intro…"] = "bgm",
        reason: Annotated[str, "Why it was rejected, in the reviewer's words — the next take is composed to answer it"] = "",
        level: Annotated[str, "The level, when this sound belongs to one level"] = "",
        scope: Annotated[str, "'agent' is the agent's creator deciding for everyone (anyone else's decision is theirs alone); 'mine' is this person correcting their own copy"] = "agent",
    ) -> str:
        """Record that the reviewer approved (or rejected) a game's music.

        The agent's creator meets this agent in Evaluation Mode after
        creation and hears the game's music there.  Their word is recorded
        on the binding, so the person who reuses this agent gets the music
        that was approved.  A rejection clears the binding, and the next
        bind_game_sound composes a fresh one.  Anyone else's word changes
        only what they hear themselves.
        """
        slot = str(game_id or '').strip()
        which = str(state or 'bgm').strip() or 'bgm'
        if which not in GAME_STATES:
            return (f"{which} is not one of a game's states. Use one of: "
                    f"{', '.join(sorted(GAME_STATES))}.")
        level = str(level or '').strip()
        wants_everyone = str(scope or 'agent').strip() != 'mine'
        # Only the agent's creator decides for everyone (owner 2026-10-17).
        # Anyone else's word is their own choice, kept in their own store,
        # and the agent's take stays as it is for everyone else.
        for_everyone = wants_everyone and _is_creator()
        own = None if for_everyone else _own_store().setdefault('games', {})
        games = agent_data.setdefault(prompt_id, {}).setdefault('games', {})
        # A verdict belongs to the memo it was GIVEN, and the ladder may have
        # found that under a different key than the one asked for: rejecting
        # while playing level 3 wrote at 'correct@3' and left 'correct' --
        # the take actually sounding -- playing on, url intact.  A person's
        # own word still lands in their own memo.
        music, matched, write_key = record_verdict(
            games, slot, which, approved, reason, level, own=own)
        if not music:
            return (f"No {which} is bound to {slot or 'that game'} yet, so "
                    f"there is nothing to approve.")
        try:
            helper_fun.save_agent_data_to_file(
                prompt_id if for_everyone else own_key, agent_data)
        except Exception as e:
            tool_logger.warning(f'approve_game_sound could not persist: {e}')
        answer = {
            'status': 'approved' if approved else 'rejected',
            'game_id': slot,
            'state': which,
            # what this person hears now
            'music': game_state_sound(games, slot, which, level, own=own)[0] or None,
            'scope': 'agent' if for_everyone else 'mine',
            # which memo the verdict landed on, so a caller can see that a
            # level-3 rejection marked the game-wide take that was playing
            'matched': matched,
            'key': write_key,
        }
        if wants_everyone and not for_everyone:
            answer['note'] = ("Recorded for this person only: only the agent's "
                              "creator changes a game's sound for everyone.")
        return json.dumps(answer)

    tools.append((
        "approve_game_sound",
        "Record a decision on a kids game's music: approved keeps it, rejected "
        "drops it so it can be composed again. The agent's creator decides for "
        "everyone who reuses this agent; anyone else's decision is theirs alone.",
        approve_game_sound,
    ))

    tools.append((
        "get_game_sound",
        "The music bound to a kids game by this agent, if any. Use it before "
        "playing a game so the sound stays the one the reviewer approved.",
        reads_persisted_state(get_game_sound),
    ))

    tools.append((
        "save_data_in_memory",
        "Use this to Store and retrieve data using key-value storage system",
        # Marked reads_persisted_state (#104): its result reports what is now
        # stored, so the group chat's write-back does not store it again. The
        # same mark goes on every tool below whose result is a read of state
        # HARTOS already keeps.
        reads_persisted_state(save_data_in_memory),
    ))

    # ------------------------------------------------------------------
    # 4. get_saved_metadata
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_saved_metadata() -> str:
        """The keys of this person's memory for this agent (memory_view),
        values stripped, loaded from persistent storage when needed."""
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for get_saved_metadata, prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        _own_store()
        stripped_json = _strip_json_values(
            memory_view(agent_data, prompt_id, user_prompt))
        # Bounded like every row memory keeps (MEMORY_ITEM_MAX_CHARS: the
        # group chat writes this reply back into memory, and MemoryGraph cuts
        # a row there anyway).  Since 88d7e3aac marks a saved text as
        # '<saved_str>' instead of None, outlines grew 28% (rv_88d7 S4,
        # 628,055 to 806,259 chars over the desktop's stores), the largest
        # 226,922 chars, and nothing bounded them.  A cut outline names the
        # top-level keys, so no key goes unnamed, and get_data_by_key reads
        # what is under one of them.
        text = f'{stripped_json}'
        from core.constants import MEMORY_ITEM_MAX_CHARS
        if len(text) <= MEMORY_ITEM_MAX_CHARS:
            return text
        from core.token_utils import bound_text
        head = f"\n...[cut: the outline is {len(text)} chars"
        tail = ". Read a key with get_data_by_key to see what is saved under it.]"
        budget = MEMORY_ITEM_MAX_CHARS // 4
        # The top-level keys the shown part of the outline does not reach.  A
        # key the outline already shows costs the list nothing: listing every
        # key spent the list on keys 000 to about 118 of a 400-key store that
        # the outline showed anyway, and named 153 of 400 (rv_3local's review
        # of c0a1f8c17).  "Shown" is read from where each key's "'key':" ends
        # in the outline's text, against the part the note leaves: from the
        # least it can leave, settled to what this note leaves.
        ends, pos = {}, 1
        if isinstance(stripped_json, dict):
            for key, value in stripped_json.items():
                ends[str(key)] = pos + len(repr(key)) + 1
                pos += len(repr(key)) + 2 + len(repr(value)) + 2
        kept = MEMORY_ITEM_MAX_CHARS - len(head) - len(tail) - budget - 64
        for _ in range(4):
            names = sorted(k for k, end in ends.items() if end > kept)
            # Bounded by characters, not by a count: twenty names left eleven
            # of a store's thirty keys unnamed (rv_3local's review of
            # 65d65a767).  A quarter of the bound holds the longest key list
            # on the owner's desktop (1,924 chars, 106 keys) twice over.
            listed, used = [], 0
            for name in names:
                if used + len(name) + 2 > budget:
                    break
                listed.append(name)
                used += len(name) + 2
            shown = ', '.join(listed) + (
                f', and {len(names) - len(listed)} more' if len(listed) < len(names) else '')
            mark = (head
                    + (f"; the top-level keys it does not reach are: {shown}" if names else '')
                    + tail)
            if MEMORY_ITEM_MAX_CHARS - len(mark) == kept:
                break
            kept = MEMORY_ITEM_MAX_CHARS - len(mark)
        return bound_text(text, MEMORY_ITEM_MAX_CHARS, mark)

    tools.append((
        "get_saved_metadata",
        "Returns the schema of the json from internal memory with all keys but without actual values.",
        reads_persisted_state(get_saved_metadata),
    ))

    # ------------------------------------------------------------------
    # 5. get_data_by_key
    # ------------------------------------------------------------------
    _KEY_NOT_FOUND = KEY_NOT_FOUND

    def _read_saved(key):
        """The value saved at ``key``, whole, or the not-found sentinel.

        For code in this module that needs the value as stored (the receipt
        template, the save check). The get_data_by_key tool pages what the
        model reads; the receipt read its template through that tool, so a
        template longer than a page was cut and the page note was printed into
        the customer's receipt (#104 review).

        Read through memory_view, the one person's view: this person's
        entries over what the agent itself holds, merged key by key, so a
        key this person never saved still finds an entry the agent kept for
        everyone (never another person's), and a nested key saved over an
        agent entry keeps the agent entry's other fields.
        """
        if prompt_id not in agent_data or not agent_data[prompt_id]:
            tool_logger.info(f"Loading agent data from file for prompt_id {prompt_id}")
            helper_fun.load_agent_data_from_file(prompt_id, agent_data)
        _own_store()
        d = memory_view(agent_data, prompt_id, user_prompt)
        try:
            for k in key.split('.'):
                d = d[k]
            return f'{d}'
        # TypeError too: a path that runs through a None, a string or a list
        # is as missing as an absent key. It used to escape as a tool
        # exception and skip the fallback below (central 2026-09-13, a hive
        # reuse turn asking for a nested key under a None value).
        except (KeyError, TypeError):
            # Fallback: check MemoryGraph for persisted [KV] data — the
            # read half of save_data_in_memory's dual-write, carried by
            # reuse_recipe's inline twin before the #743 migration and
            # restored here (owner audit 2026-09-01).
            if memory_graph is not None:
                try:
                    results = memory_graph.recall(f"[KV] {key}", mode='text', top_k=1)
                    if results:
                        return results[0].content
                except Exception:
                    pass
            return _KEY_NOT_FOUND

    @log_tool_execution
    def get_data_by_key(
        key: Annotated[str, f"Key path for retrieving data: the key it was saved under. Use dot notation for nested keys{_key_example}. get_saved_metadata lists the keys saved so far."],
        offset: Annotated[int, "Where to start reading a long value, in characters. Leave 0 to read from the start."] = 0,
    ) -> str:
        # One page of the value, not all of it (#104): a key like 'hive'
        # returned the whole subtree, which went to the model and back into
        # memory. A long value is read a page at a time, the way book pages
        # are, and the note names the offset of the next page (_one_page).
        # A pointer the wire trim put where it elided text
        # ([elided:<id> ...], core.llm_outbound_logger): the original, whole,
        # from the store's `elided` namespace -- no per-item cap.
        from core.llm_outbound_logger import (
            ELIDED_KEY_PREFIX, elision_scope, read_elided)
        if str(key).strip().startswith(ELIDED_KEY_PREFIX):
            # Only this user's elided text (or, when the call that elided it
            # knew no user, this request's): never another user's.
            pid = str(key).strip()[len(ELIDED_KEY_PREFIX):]
            value = read_elided(pid, elision_scope(user_id=user_id))
            if value is None and request_id_list.get(user_prompt):
                value = read_elided(pid, elision_scope(
                    request_id=request_id_list.get(user_prompt)))
            if value is None:
                return (f'Nothing is stored for {key}: the elided text is kept '
                        f'for a day, and this one is gone or never existed.')
        else:
            _placeholder = unfilled_key_placeholder(key)
            if _placeholder:
                tool_logger.warning(f"get_data_by_key refused: key {key!r} holds "
                                    f"the placeholder {_placeholder!r}")
                return _placeholder_key_refusal('get_data_by_key', key, _placeholder)
            value = _read_saved(key)
            if value == _KEY_NOT_FOUND:
                return key_not_found_reply(
                    agent_data.get(person_store_id(user_prompt)) or {}, key)
        return _one_page(value, offset, 'get_data_by_key with ')

    tools.append((
        "get_data_by_key",
        "Returns the data saved at a key. A long value comes back one page at a "
        "time; pass the offset the reply names to read the next page.",
        reads_persisted_state(get_data_by_key),
    ))
    # Alias — Helper system prompts in reuse_recipe.py advertise this name (#510).
    # Same closure → identical behavior under both names, the persisted-read
    # mark included.  Never remove a registered tool: phantom tool fixed by
    # adding a real registration.
    tools.append((
        "get_data_from_memory",
        "Returns the data saved at a key, a page at a time (alias of get_data_by_key)",
        get_data_by_key,
    ))

    # ------------------------------------------------------------------
    # 5b. generate_receipt (#752 — receipt for a service, over any channel)
    # ------------------------------------------------------------------
    @log_tool_execution
    def generate_receipt(
        service: Annotated[str, "Service provided, e.g. 'Bridal makeup'."],
        amount: Annotated[str, "Amount charged, e.g. '5000'."],
        client_name: Annotated[str, "Client's name."] = "",
        currency: Annotated[str, "Currency code or symbol, e.g. 'INR'."] = "INR",
        date: Annotated[str, "Receipt date; defaults to today when omitted."] = "",
        notes: Annotated[str, "Optional notes or line items."] = "",
        business_name: Annotated[str, "Your business or artist name."] = "",
        advance: Annotated[str, "Advance already received against the total, e.g. '5000'. Required every time; use '0' when none."] = "",
        event_timing: Annotated[str, "Event-day timing, e.g. 'ready by 6am, event at 11am'. Required every time."] = "",
        render: Annotated[str, "'text' (default) or 'image' for the branded PNG receipt with the saved logo."] = "text",
    ) -> str:
        """Fill the user's accepted receipt template with the given details.

        Balance is computed HERE (total - advance), never by the model -
        money math is deterministic code. render='image' additionally
        produces the branded PNG (logo saved once via set_receipt_logo)
        and appends a [[MEDIA:<path>]] marker that the channel reply path
        converts into a real image attachment on the same channel.

        Reuses the memory tools' store (save_data_in_memory / get_data_by_key,
        the person's own memory for this agent: memory_view) as the "template
        they accept": the agent proposes a template, the user confirms it, and
        it is saved under key 'receipt_template'.  Falls back to
        a neutral default when none is stored.  Renders via the existing
        TemplateEngine and returns the receipt text for the agent to send back
        over the same channel the request arrived on (WhatsApp, etc.).
        """
        from integrations.service_tools.receipt_image import (
            compute_balance, render_receipt_png)
        balance = compute_balance(amount, advance) or ""
        # The template as saved: the get_data_by_key tool pages what the model
        # reads, and a paged template printed its page note into the receipt.
        template = _read_saved("receipt_template")
        if not template or template == _KEY_NOT_FOUND:
            template = _DEFAULT_RECEIPT_TEMPLATE
        fields = {
            "business_name": business_name,
            "client_name": client_name,
            "service": service,
            "amount": amount,
            "currency": currency,
            "date": date or datetime.now().strftime("%Y-%m-%d"),
            "notes": notes,
            "advance": advance,
            "event_timing": event_timing,
            "balance": balance,
        }
        from integrations.channels.response.templates import TemplateEngine
        text = TemplateEngine().render(template, extra_vars=fields)
        if str(render).lower() != "image":
            return text
        logo_path = _read_saved("receipt_logo_path")
        if logo_path == _KEY_NOT_FOUND:
            logo_path = None
        png = render_receipt_png(fields, logo_path=logo_path)
        if not png:
            return (text + "\n(Image receipt unavailable on this install - "
                    "sent as text instead.)")
        return text + "\n[[MEDIA:" + png + "]]"

    tools.append((
        "generate_receipt",
        "Generate a receipt for a service the user provided, using their "
        "accepted receipt template. EVERY receipt must collect: total amount, "
        "date, event-day timing, and advance received (use '0' if none) - ask "
        "for any that are missing, confirm the parsed values back to the user, "
        "THEN call this. Balance due is computed automatically. On first use, "
        "propose a template; once the user confirms it, save it with "
        "save_data_in_memory under key 'receipt_template'. Pass render='image' "
        "to produce the branded PNG receipt (set the logo once via "
        "set_receipt_logo). Returns the receipt to send back to the user.",
        generate_receipt,
    ))

    # ------------------------------------------------------------------
    # 5c. set_receipt_logo (#752 leg B - capture the logo ONCE, durably)
    # ------------------------------------------------------------------
    @log_tool_execution
    def set_receipt_logo(
        file_path: Annotated[str, "Path to the uploaded logo image file on this machine."],
    ) -> str:
        """Persist the business logo for all future receipts.

        Inbound channel media lands in short-lived locations (the upload
        TTLCache lives 2 hours), so the logo is COPIED once to the durable
        data dir and its path stored in the person's memory for this agent
        under 'receipt_logo_path' - the same store the accepted template uses.
        """
        import shutil
        src = str(file_path or "").strip().strip('"')
        if not src or not os.path.isfile(src):
            return ("Logo file not found at '" + src + "'. Ask the user to "
                    "send the logo image again, then retry with its saved path.")
        ext = os.path.splitext(src)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'):
            return ("'" + ext + "' is not an image type I can put on a "
                    "receipt - please send PNG or JPG.")
        from core.platform_paths import get_data_dir
        # One folder per person on this agent, beside the path the person's
        # memory stores (memory_view): a shared folder let the next person's
        # logo overwrite this one's file under a path still stored as theirs.
        dest_dir = os.path.join(get_data_dir(), 'receipt_assets',
                                str(prompt_id), own_key)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, 'logo' + ext)
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            tool_logger.error("set_receipt_logo copy failed: %s" % e)
            return "Could not save the logo: %s" % e
        save_data_in_memory('receipt_logo_path', dest)
        return ("Logo saved for all future receipts (" + os.path.basename(dest)
                + "). It will appear on every image receipt from now on.")

    tools.append((
        "set_receipt_logo",
        "Save the user's business logo ONCE for all future receipts. Call this "
        "when the user sends their logo image (get the file path from the "
        "uploaded file). The logo is stored durably and composited onto every "
        "render='image' receipt.",
        set_receipt_logo,
    ))

    # ------------------------------------------------------------------
    # 6. get_user_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_id() -> str:
        tool_logger.info('INSIDE get_user_id')
        return f'{user_id}'

    tools.append((
        "get_user_id",
        "Returns the current user's internal id, for a tool that asks for a "
        "user_id. It means nothing to the person: never say or show it to them.",
        get_user_id,
    ))

    # ------------------------------------------------------------------
    # 7. get_prompt_id
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_prompt_id() -> str:
        tool_logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    tools.append((
        "get_prompt_id",
        "Returns the unique identifier (prompt_id) associated with the current prompt or conversation.",
        get_prompt_id,
    ))

    # ------------------------------------------------------------------
    # 8. Generate_video (canonical — full LTX-2 + avatar)
    # ------------------------------------------------------------------
    @log_tool_execution
    def Generate_video(
        text: Annotated[str, "Text to be used for video generation"],
        avatar_id: Annotated[int, "Unique identifier for the avatar (use 0 for LTX-2 text-to-video)"],
        realtime: Annotated[bool, "If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"],
        model: Annotated[str, "Video model to use: 'avatar' for avatar-based video, 'ltx2' for LTX-2 text-to-video generation"] = "avatar",
    ) -> str:
        tool_logger.info(f'INSIDE Generate_video with model={model}')

        # LTX-2 Text-to-Video Generation
        if model.lower() == "ltx2":
            tool_logger.info(f'Using LTX-2 for video generation: {text[:50]}...')
            LOCAL_COMFYUI_URL = "http://localhost:8188"
            LOCAL_LTX_URL = "http://localhost:5002"
            headers = {'Content-Type': 'application/json'}
            ltx_payload = {
                "prompt": text,
                "negative_prompt": "worst quality, inconsistent motion, blurry, jittery, distorted",
                "num_frames": 97,
                "width": 832,
                "height": 480,
                "num_inference_steps": 30 if realtime else 50,
                "guidance_scale": 3.0,
                "fps": 24,
            }

            # Fast health probe — skip dead servers instantly (0ms vs 10s timeout)
            def _is_server_up(url, name):
                try:
                    r = pooled_get(f"{url}/health", timeout=1.5)
                    return r.status_code < 500
                except Exception:
                    tool_logger.info(f"{name} not reachable — skipping instantly")
                    return False

            # Try local LTX-2 server first — only if alive
            if _is_server_up(LOCAL_LTX_URL, "LTX-2"):
                try:
                    tool_logger.info(f"LTX-2 server is UP, generating...")
                    response = pooled_post(f"{LOCAL_LTX_URL}/generate", json=ltx_payload, headers=headers, timeout=600)
                    if response.status_code == 200:
                        result = response.json()
                        video_url = result.get('video_url') or result.get('output_url') or result.get('video_path')
                        if video_url:
                            tool_logger.info(f"LTX-2 video generated: {video_url}")
                            return f"LTX-2 Video generated successfully. URL: {video_url}"
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"LTX-2 generation failed: {e}")

            # Try ComfyUI — only if alive
            if _is_server_up(LOCAL_COMFYUI_URL, "ComfyUI"):
                try:
                    tool_logger.info(f"ComfyUI is UP, submitting workflow...")
                    comfyui_workflow = {
                        "prompt": {
                            "1": {"class_type": "LTXVLoader", "inputs": {"ckpt_name": "ltx-video-2b-v0.9.safetensors"}},
                            "2": {"class_type": "LTXVConditioning", "inputs": {"positive": text, "negative": ltx_payload["negative_prompt"], "ltxv_model": ["1", 0]}},
                            "3": {"class_type": "LTXVSampler", "inputs": {"seed": int(time.time()) % 2147483647, "steps": ltx_payload["num_inference_steps"], "cfg": ltx_payload["guidance_scale"], "width": ltx_payload["width"], "height": ltx_payload["height"], "num_frames": ltx_payload["num_frames"], "ltxv_model": ["1", 0], "conditioning": ["2", 0]}},
                            "4": {"class_type": "LTXVDecode", "inputs": {"ltxv_model": ["1", 0], "samples": ["3", 0]}},
                            "5": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": ltx_payload["fps"], "filename_prefix": "ltx2_output", "format": "video/h264-mp4", "images": ["4", 0]}},
                        }
                    }
                    response = pooled_post(f"{LOCAL_COMFYUI_URL}/prompt", json=comfyui_workflow, headers=headers, timeout=10)
                    if response.status_code == 200:
                        comfy_prompt_id = response.json().get('prompt_id')
                        tool_logger.info(f"ComfyUI LTX-2 job queued: {comfy_prompt_id}")
                        for _ in range(120):
                            time.sleep(5)
                            history_response = pooled_get(f"{LOCAL_COMFYUI_URL}/history/{comfy_prompt_id}")
                            if history_response.status_code == 200:
                                history = history_response.json()
                                if comfy_prompt_id in history:
                                    outputs = history[comfy_prompt_id].get('outputs', {})
                                    for node_id, output in outputs.items():
                                        for media_key in ('gifs', 'videos'):
                                            if media_key in output:
                                                filename = output[media_key][0].get('filename')
                                                if filename:
                                                    video_url = f"{LOCAL_COMFYUI_URL}/view?filename={filename}"
                                                    return f"LTX-2 Video generated via ComfyUI. URL: {video_url}"
                        return f"LTX-2 Video generation queued in ComfyUI (prompt_id: {comfy_prompt_id}). Check ComfyUI interface for output."
                except requests.exceptions.RequestException as e:
                    tool_logger.info(f"ComfyUI generation failed: {e}")

            # Local servers unavailable — try hive mesh peer with GPU
            try:
                from integrations.agent_engine.compute_config import get_compute_policy
                policy = get_compute_policy()
                if policy.get('compute_policy') != 'local_only':
                    from integrations.agent_engine.compute_mesh_service import get_compute_mesh
                    mesh = get_compute_mesh()
                    # user_id: the person this peer's compute is charged to
                    # (ComputeMeshService._charged).
                    result = mesh.offload_to_best_peer(
                        model_type=ModelType.VIDEO_GEN,
                        prompt=text,
                        options={'model': 'ltx2', 'timeout': 300,
                                 'user_id': str(user_id or '')},
                    )
                    if result and 'error' not in result:
                        video_url = result.get('response', result.get('video_url', ''))
                        peer = result.get('offloaded_to', 'hive_peer')
                        tool_logger.info(f"LTX-2 video generated via hive peer {peer}: {video_url}")
                        return f"LTX-2 Video generated via hive peer. URL: {video_url}"
                    tool_logger.info(f"Hive mesh video offload failed: {result.get('error')}")
            except Exception as e:
                tool_logger.info(f"Hive mesh offload not available: {e}")

            return ("LTX-2 video generation failed. No local GPU, no hive peers with GPU. "
                    "Options: (1) Pair a GPU device: hart compute pair <address>, "
                    "(2) Set HEVOLVE_COMPUTE_POLICY=any, "
                    "(3) Install local LTX-2 server with CUDA GPU")

        # Default: Avatar-based video generation
        from core.config_cache import get_db_url
        from core.teacher_avatar import lookup_avatar
        database_url = get_db_url() or 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        tool_logger.info(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")

        headers = {'Content-Type': 'application/json'}
        data = {
            "text": str(text),
            'flag_hallo': 'false',
            'chattts': False,
            'openvoice': "false",
        }

        # The avatar's image and voice sample: the one lookup a spoken reply
        # uses too (core/teacher_avatar.py).
        avatar = lookup_avatar(avatar_id, database_url)
        if avatar['openvoice']:
            data['openvoice'] = "true"

        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = avatar['image_url']
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = str(request_id)
        data['gradient'] = "true"
        data['cus_bg'] = "false"
        data['solid_color'] = "false"
        data['inpainting'] = "false"
        data['prompt'] = ""
        data['gender'] = 'male'

        timeout = 60
        if not realtime:
            timeout = 600
            data['chattts'] = True
            data['flag_hallo'] = "true"
            data["cartoon_image"] = "False"

        data["audio_sample_url"] = avatar['audio_sample_url']
        data['voice_id'] = avatar['voice_id']

        conv_id = save_conversation_db(text, user_id, prompt_id, database_url, request_id)
        data['conv_id'] = int(conv_id)
        data['avatar_id'] = int(avatar_id)
        data['timeout'] = int(timeout)

        # Reported made or queued ONLY when the video service took the job.  Walk 27 Nov step 11
        # (msg_1795728921734_t7qoi3): on a desktop get_db_url() is this Nunba, which has no
        # /video_generate_save; the POST reached nothing, the failure was swallowed, and the reply
        # still said "completed", so the agent told a child "Here is a video" with none shown.
        # The 1 s timeout stays a fire-and-forget send: a READ timeout means the service has the
        # request (as before); no connection, or an HTTP error, means it does not.  Sent ONCE
        # (pooled_post_once): the shared session's remote adapter retries a POST, which posted a
        # paid job three times and then surfaced as a ConnectionError (review rv_face2 C).
        try:
            from core.http_pool import pooled_post_once
            resp = pooled_post_once(f"{database_url}/video_generate_save",
                                    data=json.dumps(data), headers=headers, timeout=1)
            accepted = getattr(resp, 'status_code', 200) < 400
        except requests.exceptions.ReadTimeout:
            accepted = True
        except Exception as e:
            tool_logger.info(f"Generate_video: the video service did not take conv_id {conv_id}: {e}")
            accepted = False
        if not accepted:
            return (f"No video was made: this node's video service did not take the job "
                    f"(conv_id:{conv_id}). Do not tell the person a video is shown; answer in words.")

        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"

    tools.append((
        "Generate_video",
        "Generate video with text. Use model='ltx2' for AI text-to-video generation, or model='avatar' (default) for avatar-based video with voice synthesis.",
        Generate_video,
    ))

    # ------------------------------------------------------------------
    # 9. get_user_uploaded_file
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_uploaded_file(
        name: Annotated[str, "On a goal's own turn: a file of the goal's folder to read, such as EMAIL_FACTS.md. Leave empty for the person's uploads, or give one of their file_ids to read that file alone."] = '',
        offset: Annotated[int, "Where to start reading a long file, in characters; for the person's uploads, the offset their list names for its next page. Leave 0 to read from the start."] = 0,
    ) -> str:
        tool_logger.info('INSIDE get_user_uploaded_file')
        # A goal's own turn opens the file its goal names, read-only, from
        # the folder its config names (integrations.agent_engine.goal_files:
        # only this node's own goal turn, a goal made by an account the node
        # holds, inside config files_root).  Live on central 2026-11-20, goal
        # 61e48abc's step "get_user_uploaded_file: load EMAIL_FACTS.md"
        # looped: nothing read a goal's file.  Any other turn, a person's
        # included, answers as before; a name opens nothing there.
        if str(name or '').strip():
            from integrations.agent_engine.goal_files import read_goal_file
            got = read_goal_file('get_user_uploaded_file', name)
            if got is not None:
                if got.refusal is not None:
                    return got.refusal
                return _one_page(got.text, offset,
                                 f'get_user_uploaded_file with name={str(name).strip()!r} and ')
        # .get(), not [] — recent_file_id is a TTLCache written only when a
        # file is actually uploaded, so a user who uploaded nothing has no
        # key and [] raised KeyError (44/44 failures, 4/4 on 2026-09-07).
        # That case is exactly the answer below, which was unreachable.
        file_id = recent_file_id.get(user_id)
        if file_id:
            return f'Got user uploaded file the file_id is {file_id}'
        # Else the clips she sent this agent that no reel has used yet, by
        # the rule reel_make reads (core.chat_attachments.latest_inbound).
        # Measured 2026-10-20 (request 8610dffc): her Reel Studio agent 342
        # called this three times, read "No file uploaded from user" while
        # the store held her five clips for it, and asked her where they were.
        from core.chat_attachments import given_inbound, latest_inbound

        def _at(seconds):
            try:        # captured_at is what her phone sent: any int > 0
                return datetime.fromtimestamp(seconds).astimezone().isoformat(
                    timespec='seconds')
            except (OverflowError, OSError, ValueError):
                return None
        files = latest_inbound(user_id, prompt_id)
        if files:
            return json.dumps({
                'count': len(files),
                'files': [{'file_id': f['xid'], 'name': f['name'], 'mime': f['mime'],
                           'size': f['size'], 'sent_at': _at(f['created_at']),
                           'filmed_at': (_at(f['meta']['captured_at'] / 1000)
                                         if f['meta'].get('captured_at') else None)}
                          for f in files],
                'note': ('The files she sent this agent that no reel has used yet, '
                         'in the order she sent them.'),
            })
        # Else every other file this person gave THIS agent: photos,
        # documents, voice notes, a logo, a song (given_inbound), newest
        # first.  Measured 2026-11-26 10:44-10:57 (agent 95568353503): the
        # parent answered five file_request cards with family photos, the
        # store held all five for the agent as chat attachments, and this
        # tool said "No file uploaded from user" eight times while the agent
        # asked for them six more times.  A file id given as the name reads
        # that file alone, through the store's own read (resolve), when it is
        # a file of this agent's.
        given = given_inbound(user_id, prompt_id)
        ref = str(name or '').strip().lower()
        from core.user_file_store import get_user_file_store, is_xid
        if is_xid(ref):
            try:
                held = get_user_file_store().resolve(user_id, ref, box='in')
            except Exception:
                tool_logger.warning('get_user_uploaded_file: file %s not read', ref[:12],
                                    exc_info=True)
                held = None
            if held is not None and held.prompt_id == str(prompt_id):
                given = [held.to_dict()]
        if given:
            return _given_files_page(given, offset, _at)
        # A turn that is not a person's (a goal turn, a peer's A2A turn, or
        # one whose tag was lost: is_a_persons_request, the person rule) has
        # nobody who could upload anything, so "No file uploaded" there held
        # its step forever: live on central 2026-11-20, goal 61e48abc's step
        # "get_user_uploaded_file: load EMAIL_FACTS.md" paused needs_help
        # after 3 attempts.  There the step may finish without the file.
        # Here no id counts as no person, unlike free_text_recall: an
        # id-less run has nobody who could have uploaded anything, while a
        # recall on one may be a person's own scheduled or camera agent.
        if not is_a_persons_request(camera_turn_id()):
            # The goal's own turn whose goal names a folder: say which files
            # it can open by name instead.
            from integrations.agent_engine.goal_files import goal_folder_listing
            listing = goal_folder_listing('get_user_uploaded_file')
            if listing is not None:
                return listing
            from core.tool_refusal import RETRY_FINISH_WITHOUT, refuse
            return refuse('get_user_uploaded_file',
                          'no file was uploaded, and no person can upload one '
                          'on a background turn',
                          "Carry on with the goal's own text instead of an "
                          "uploaded file",
                          retry=RETRY_FINISH_WITHOUT)
        return 'No file uploaded from user'

    tools.append((
        "get_user_uploaded_file",
        "get user's recent uploaded files; on a goal's own turn, pass name to "
        "read a file of the goal's folder",
        get_user_uploaded_file,
    ))

    # ------------------------------------------------------------------
    # 10. get_text_from_image (img2txt)
    # ------------------------------------------------------------------
    @log_tool_execution
    def img2txt(
        image_url: Annotated[str, "image url of which you want text"],
        text: Annotated[str, "the details you want from image"] = 'Describe the Images & Text data in this image in detail',
    ) -> str:
        tool_logger.info('INSIDE img2txt')
        # SSRF protection: validate image URL before fetching
        try:
            from security.sanitize import validate_url
            image_url = validate_url(image_url)
        except (ImportError, ValueError) as e:
            tool_logger.warning(f"Image URL blocked by SSRF filter: {image_url} — {e}")
            from core.tool_refusal import refuse
            return refuse('get_text_from_image',
                          f"image_url was refused by the security filter ({e})",
                          "Pass a public http or https link to the image itself")
        # Try local Qwen Vision first (bundled mode), fall back to cloud
        from core.config_cache import get_vision_api, is_bundled
        url = get_vision_api()
        if not url:
            tool_logger.warning("No LLAVA_API configured — vision inference may fail on no-GPU instances")
            url = "http://azurekong.hertzai.com:8000/llava/image_inference"

        if is_bundled():
            # Local: use Qwen Vision via upload/vision endpoint
            payload = json.dumps({'image_url': image_url, 'prompt': text})
            response = requests.post(url, data=payload,
                                     headers={'Content-Type': 'application/json'}, timeout=60)
        else:
            payload = {'url': image_url, 'prompt': text}
            response = requests.request("POST", url, headers={}, data=payload, files=[], timeout=300)
        if response.status_code == 200:
            return response.text
        # Was 'Not able to get this page details try later': not a failure to
        # core.constants.tool_reply_failed, so a step could be credited with
        # it, and it did not say whether the link or the service was at fault
        # (owner 2026-11-09: every refusal says what was wrong).
        from core.tool_refusal import RETRY_LATER, refuse
        code = response.status_code
        if 400 <= code < 500:
            return refuse('get_text_from_image',
                          f"the image service could not read image_url (HTTP {code})",
                          "Check that image_url is a direct link to an image file "
                          "that opens without signing in")
        return refuse('get_text_from_image',
                      f"the image service is failing (HTTP {code})",
                      "Carry on without this image's details, or tell the person "
                      "the image could not be read", retry=RETRY_LATER)

    tools.append((
        "get_text_from_image",
        "Image to Text/Question Answering from image",
        img2txt,
    ))
    # Alias — reuse_recipe.py main flow LLM prompts advertise `img2txt` (#510).
    # Same canonical closure (SSRF-validated, local Qwen Vision + cloud LLaVA fallback).
    tools.append((
        "img2txt",
        "Image to Text/Question Answering from image (alias of get_text_from_image)",
        img2txt,
    ))

    # ------------------------------------------------------------------
    # 11. create_scheduled_jobs
    # ------------------------------------------------------------------
    @log_tool_execution
    def create_scheduled_jobs(
        interval_sec: Annotated[int, "time between two Interval in seconds."],
        job_description: Annotated[str, "Description of the job to be performed"],
        cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"] = None,
    ) -> str:
        tool_logger.info('INSIDE create_scheduled_jobs')
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'

    tools.append((
        "create_scheduled_jobs",
        "Creates time-based jobs using APScheduler to schedule jobs",
        create_scheduled_jobs,
    ))

    # ------------------------------------------------------------------
    # 12. send_message_to_user
    # ------------------------------------------------------------------
    # Guard absorbed from reuse_recipe's inline twin (#743 migration):
    # group-chat models sometimes route "@helper ..." steering text into
    # this tool — that internal chatter must never reach the user.
    _AGENT_MENTIONS = ("@statusverifier", "@status verifier", "@verification",
                      "@helper", "@executor")

    # expects_reply is REQUIRED and has no default: a message that asks the
    # person something and one that only tells them something are different
    # acts, and the step's completion is decided from which act was called,
    # never from the words (lifecycle_hooks.send_is_ask).  Live 2026-10-31
    # 17:13 IST, agent 45620673143 action 3: the step's only output was a
    # send ending "Ready to begin?", and reading every send as delivery would
    # have closed the step on its own question.  A call that omits it fails
    # here (the tool_logging error envelope names the argument) and sends
    # nothing, so an omission is a visible failed call, never a silent ask
    # or a silent completion.  What it means is EXPECTS_REPLY_RULE's one
    # definition, shared with the tool description and REUSE's rule 11.ii.
    from core.constants import EXPECTS_REPLY_RULE

    @log_tool_execution
    def send_message_to_user(
        text: Annotated[str, "Text you want to send to the user"],
        expects_reply: Annotated[bool, EXPECTS_REPLY_RULE],
        avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
        response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime',
    ) -> str:
        low = text.lower()
        mention = next((m for m in _AGENT_MENTIONS if m in low), None)
        if mention is not None:
            tool_logger.info(
                f'Message directed to agent ({mention}), not sending to user: {text[:50]}...')
            return f'Message directed to {mention} agent, not sending to user'
        _placeholder = unfilled_text_placeholder(text)
        if _placeholder:
            tool_logger.warning(f'send_message_to_user refused: the text holds '
                                f'the placeholder {_placeholder!r}')
            return _placeholder_text_refusal('send_message_to_user', _placeholder)
        tool_logger.info('INSIDE send_message_to_user')
        # One line per send with what decides the step: which act it was,
        # the turn it ran in and the action it ran for, so the share of sends
        # that ask is measured, not guessed (hartos-00's review of the
        # 476bbd5d8 rework).
        # Attribution is evidence, not behaviour: an unresolved id is logged
        # as None and never stops the send (tool_logging._session_suffix's
        # rule, at debug for the same reason: a daemon tick has no request).
        try:
            from hartos.threadlocal import thread_local_data as _tld
            _rid = _tld.get_request_id()
        except Exception:
            tool_logger.debug('[SEND-KIND] request id unavailable', exc_info=True)
            _rid = None
        try:
            from hartos.lifecycle_hooks import action_in_progress
            _aid = action_in_progress(user_prompt)
        except Exception:
            tool_logger.debug('[SEND-KIND] action id unavailable', exc_info=True)
            _aid = None
        tool_logger.info(
            f'[SEND-KIND] {user_prompt} action={_aid} request={_rid} '
            f'expects_reply={expects_reply}')
        tool_logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        # The send runs HERE and its result is the tool's result.  This used
        # to start the send on a thread and return "sent successfully" before
        # it ran: Nunba gui_app.log 2026-09-26 00:02:10 the tool answered
        # sent, 00:02:19 the send failed (WinError 10061), and the agent
        # waited on a question the user never saw.  Both
        # send_message_to_user1 copies return "Message sent successfully ..."
        # or "Failed to send message ...".
        result = send_message_to_user1(user_id, text, '', prompt_id)
        if isinstance(result, str) and result:
            return result
        return 'Message handed to the sender; its delivery was not confirmed'

    tools.append((
        "send_message_to_user",
        "Sends a message to the user. " + EXPECTS_REPLY_RULE
        + " A message that waits for their answer holds your step until "
        "they answer.",
        send_message_to_user,
    ))

    # ------------------------------------------------------------------
    # 13. send_presynthesized_video_to_user
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_presynthesized_video_to_user(
        conv_id: Annotated[str, "Conversation ID associated with the text from memory"],
    ) -> str:
        tool_logger.info('INSIDE send_presynthesized_video_to_user')
        tool_logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'

    tools.append((
        "send_presynthesized_video_to_user",
        "Sends a presynthesized message/video/dialogue to user using conv_id.",
        send_presynthesized_video_to_user,
    ))

    # ------------------------------------------------------------------
    # 14. send_message_in_seconds
    # ------------------------------------------------------------------
    @log_tool_execution
    def send_message_in_seconds(
        text: Annotated[str, "text to send to user"],
        delay: Annotated[int, "time to wait in seconds before sending text"],
        conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"] = None,
    ) -> str:
        tool_logger.info('INSIDE send_message_in_seconds')
        tool_logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        _placeholder = unfilled_text_placeholder(text)
        if _placeholder:
            tool_logger.warning(f'send_message_in_seconds refused: the text '
                                f'holds the placeholder {_placeholder!r}')
            return _placeholder_text_refusal('send_message_in_seconds', _placeholder)
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '', prompt_id])
        return 'Message scheduled successfully'

    tools.append((
        "send_message_in_seconds",
        "Sends a presynthesized message/video/dialogue to user using conv_id with a timer.",
        send_message_in_seconds,
    ))

    # ------------------------------------------------------------------
    # 15. get_chat_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_chat_history(
        text: Annotated[str, "Text related to which you want history"],
        start: Annotated[Optional[str], "start date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
        end: Annotated[Optional[str], "end date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
    ) -> str:
        tool_logger.info('INSIDE get_chat_history')
        if not recall_runs_on_a_background_turn():
            # A person's turn, and a run with no id: the answer as before.
            return helper_fun.get_time_based_history(
                text, f'user_{user_id}', start, end)
        # On a goal or peer turn "read, nothing matched" is the step's
        # finding, and "not read" is said as that, as search_long_term_memory
        # says them (nothing_recalled / recall_failed).  Live on central
        # 2026-11-20, goal 74416785's step "get_chat_history: pull the last
        # 14 days of chats to find importer replies and open requests" got
        # {"res_in_filter": []} and paused needs_help after 3 attempts; that
        # answer also stood for a store never read (reviews of 59ae091a and
        # 04ad8cbc), so the reader is asked strictly and raises then.
        from core.tool_refusal import nothing_recalled, recall_failed
        try:
            history = helper_fun.get_time_based_history(
                text, f'user_{user_id}', start, end, strict=True)
        except Exception as e:
            tool_logger.info(f'get_chat_history: history not read: {e}')
            return recall_failed('get_chat_history', 'chat history', e)
        try:
            found = json.loads(history).get('res_in_filter')
        except (TypeError, ValueError, AttributeError):
            found = None
        if found == []:
            # Named from the window the reader searched, never the dates
            # asked: a start alone is read as that day +-2, not "to now"
            # (review of 0c1cd2fa).
            window, open_side = helper_fun.searched_recall_window(start, end)
            return nothing_recalled(text, window=window, open_side=open_side)
        return history

    tools.append((
        "get_chat_history",
        "Get Chat history based on text & start & end date",
        reads_persisted_state(get_chat_history),
    ))

    # ------------------------------------------------------------------
    # 16. search_visual_history
    # ------------------------------------------------------------------
    @log_tool_execution
    def search_visual_history(
        query: Annotated[str, "What to search for in visual/screen descriptions"],
        minutes_back: Annotated[int, "How many minutes back to search (default 30)"] = 30,
        channel: Annotated[str, "Which feed: 'camera', 'screen', or 'both' (default)"] = "both",
    ) -> str:
        """Search past camera/screen descriptions. Use for questions about what happened earlier visually."""
        _rid = camera_turn_id()
        if not turn_may_see_the_camera():
            tool_logger.warning(f"search_visual_history refused on turn {_rid!r}: "
                                f"not the person's own")
            return camera_refusal('search_visual_history')
        results = helper_fun.search_visual_history(user_id, query, mins=minutes_back, channel=channel)
        if results:
            return '\n'.join(results)
        return "No matching visual/screen descriptions found in the given time range."

    tools.append((
        "search_visual_history",
        "Search past camera and screen descriptions by keyword and time range.",
        reads_persisted_state(search_visual_history),
    ))

    # ------------------------------------------------------------------
    # 17. google_search
    # ------------------------------------------------------------------
    @log_tool_execution
    def google_search(
        text: Annotated[str, "Text/Query which you want to search"],
    ) -> str:
        tool_logger.info('INSIDE google search')
        return helper_fun.top5_results(text)

    tools.append((
        "google_search",
        "web/google/bing search api tool for a given query",
        google_search,
    ))

    # ------------------------------------------------------------------
    # Conditional: long-term memory — SimpleMem preferred, MemoryGraph fallback
    # ------------------------------------------------------------------
    # Gated on EITHER store, not SimpleMem alone.  SimpleMem only constructs
    # when `sm_config.enabled and sm_config.api_key` (reuse_recipe.py:963,
    # create_recipe.py:886 — byte-identical twins), so a local desktop with no
    # cloud key silently got neither tool, while nine prompt sites kept
    # instructing the model to call save_to_long_term_memory by name.  Measured
    # live 2026-09-05 (Auto Research 18088688973): of the 18 _MAIN_LEG_CORE
    # tools, these two — and only these two, the only two gated here — were
    # missing from all 6 wire bodies.  The model skipped the save step with no
    # error, then read an empty store 24x and asserted completion on nothing.
    #
    # MemoryGraph needs no key, initialized 27x in that same log with 0
    # failures (including for this agent), and was ALREADY the dual-write
    # target below and the read-back path in get_data_by_key.  So it backs the
    # tools when SimpleMem is absent rather than the capability disappearing.
    if simplemem_store is not None or memory_graph is not None:
        from core.event_loop import get_or_create_event_loop

        from core.tool_refusal import recall_failed

        @log_tool_execution
        def search_long_term_memory(
            query: Annotated[str, "Natural language query to search long-term memory"],
        ) -> str:
            """Search compressed long-term memory using semantic retrieval."""
            if simplemem_store is not None:
                try:
                    loop = get_or_create_event_loop()
                    results = loop.run_until_complete(simplemem_store.search(query))
                    # SimpleMem's item is an answer, not a stored row: cut it,
                    # never skip it.
                    text = _bounded_recall(
                        [r.content for r in (results or [])], max_items=1,
                        skip_oversize=False)
                    return text or nothing_recalled_on_this_turn(query)
                except Exception as e:
                    tool_logger.info(f"SimpleMem search error: {e}")
                    return recall_failed('search_long_term_memory', 'SimpleMem', e)
            # MemoryGraph leg — same contract, local store, no API key.
            try:
                # Fetch past the 5 shown: rows _bounded_recall skips as
                # over-size must not leave real memories unreturned (#104).
                # free_text_recall: some-words matching (a key lookup such as
                # get_data_by_key's stays exact; review of e0b313bdd), and on
                # a goal turn no conversation rows (its own talk).
                results = free_text_recall(memory_graph, query, top_k=10)
                text = _bounded_recall(
                    [r.content for r in (results or [])], max_items=5)
                return text or nothing_recalled_on_this_turn(query)
            except Exception as e:
                tool_logger.info(f"MemoryGraph search error: {e}")
                return recall_failed('search_long_term_memory', 'MemoryGraph', e)

        tools.append((
            "search_long_term_memory",
            "Search long-term memory for saved facts and context using a natural language query. Past conversations come back too on a person's turn or a run with no turn id, not on a goal's or a peer agent's turn.",
            reads_persisted_state(search_long_term_memory),
        ))

        @log_tool_execution
        def save_to_long_term_memory(
            content: Annotated[str, "The information/fact to remember long-term"],
            speaker: Annotated[str, "Who said this (e.g. 'User', 'Assistant', 'System')"] = "System",
        ) -> str:
            """Save important information to compressed long-term memory."""
            if simplemem_store is None:
                # MemoryGraph leg.  SYNCHRONOUS on purpose: the dual-write
                # below can be fire-and-forget because SimpleMem already
                # persisted, but when the graph is the ONLY store, a detached
                # thread would let this return "Saved" for a write that never
                # landed — the fabricated success this whole fix exists to
                # remove.  Report what actually happened.
                try:
                    memory_graph.register(
                        content, {'memory_type': 'fact',
                                  'source_agent': speaker,
                                  'session_id': user_prompt,
                                  'source': 'memory_graph'},
                    )
                    return "Saved to long-term memory."
                except Exception as e:
                    tool_logger.info(f"MemoryGraph save error: {e}")
                    return "Failed to save to long-term memory."
            try:
                loop = get_or_create_event_loop()
                loop.run_until_complete(simplemem_store.add(content, {
                    "sender_name": speaker,
                    "user_id": user_id,
                    "prompt_id": prompt_id,
                }))
                # Dual-write to MemoryGraph (fire-and-forget).  Carried by
                # reuse_recipe's inline twin before the #743 migration and
                # lost in the swap — restored HERE so all three legs
                # (main/time/visual) get graph provenance, not just reuse.
                if memory_graph is not None:
                    try:
                        threading.Thread(target=lambda: memory_graph.register(
                            content, {'memory_type': 'fact',
                                      'source_agent': speaker,
                                      'session_id': user_prompt,
                                      'source': 'simplemem'},
                        ), daemon=True).start()
                    except Exception:
                        pass
                return "Saved to long-term memory."
            except Exception as e:
                tool_logger.info(f"SimpleMem save error: {e}")
                return "Failed to save to long-term memory."

        tools.append((
            "save_to_long_term_memory",
            "Save important facts or information to long-term memory for future retrieval across sessions.",
            save_to_long_term_memory,
        ))

    # ------------------------------------------------------------------
    # Suggest_Share_Worthy_Content
    # ------------------------------------------------------------------
    @log_tool_execution
    def suggest_share_worthy_content(
        query: Annotated[str, "Any text — not used for filtering, just provide context"] = "",
    ) -> str:
        """Find high-engagement posts that haven't been shared much and suggest sharing them."""
        try:
            from integrations.social.models import get_db, Post, ShareableLink
            from sqlalchemy import func as sa_func

            db = get_db()
            try:
                share_counts = (
                    db.query(
                        ShareableLink.resource_id,
                        sa_func.count(ShareableLink.id).label('link_count'),
                    )
                    .filter(ShareableLink.resource_type == 'post')
                    .group_by(ShareableLink.resource_id)
                    .subquery()
                )

                posts = (
                    db.query(Post, share_counts.c.link_count)
                    .outerjoin(share_counts, Post.id == share_counts.c.resource_id)
                    .filter(
                        Post.is_deleted == False,
                        Post.is_hidden == False,
                        Post.upvotes > 5,
                        Post.comment_count > 3,
                    )
                    .filter(
                        (share_counts.c.link_count == None) |  # noqa: E711
                        (share_counts.c.link_count < 3)
                    )
                    .order_by(Post.score.desc())
                    .limit(3)
                    .all()
                )

                if not posts:
                    return ("No under-shared high-engagement content found right now. "
                            "Keep creating great posts and the community will notice!")

                suggestions = []
                for post, link_count in posts:
                    title = (post.title or post.content or '')[:80].strip()
                    shares = link_count or 0
                    suggestions.append(
                        f"- \"{title}\" ({post.upvotes} upvotes, "
                        f"{post.comment_count} comments, only {shares} shares) "
                        f"[post_id: {post.id}]"
                    )

                header = ("These posts are resonating with the community but haven't "
                           "been shared much yet. Consider sharing them:\n")
                return header + "\n".join(suggestions)
            finally:
                db.close()
        except Exception as e:
            tool_logger.warning(f"Suggest_Share_Worthy_Content failed: {e}")
            return f"Could not fetch share-worthy content right now: {e}"

    tools.append((
        "Suggest_Share_Worthy_Content",
        "Find high-engagement posts that deserve wider reach but haven't been shared much. "
        "Use when the user asks about content worth sharing or to proactively suggest "
        "share-worthy community posts.",
        suggest_share_worthy_content,
    ))

    # ------------------------------------------------------------------
    # Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        input_text: Annotated[str, "JSON string with event, page, duration_ms, outcome fields"],
    ) -> str:
        """Record a user experience observation for self-improvement."""
        try:
            data = json.loads(input_text) if input_text.startswith('{') else {'event': input_text}
            event = data.get('event', 'interaction')
            page = data.get('page', '')
            duration_ms = data.get('duration_ms', 0)
            outcome = data.get('outcome', 'recorded')

            observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"

            if memory_graph:
                session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                memory_id = memory_graph.register(
                    content=observation,
                    metadata={
                        'memory_type': 'observation',
                        'source_agent': 'agent',
                        'session_id': session_key,
                        'page': page,
                        'event': event,
                    },
                    context_snapshot=f"UX observation during session {session_key}",
                )
                return f"Observation recorded (id: {memory_id}): {observation}"

            return f"Observation noted: {observation}"
        except Exception as e:
            tool_logger.warning(f"Observe_User_Experience failed: {e}")
            return f"Observation noted: {input_text}"

    tools.append((
        "Observe_User_Experience",
        "Record a user experience observation. Input: JSON with event, page, "
        "duration_ms, outcome. Used for self-improvement and understanding user "
        "behavior patterns.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        input_text: Annotated[str, "Topic or area to critique"],
    ) -> str:
        """Review past suggestions and outcomes to improve future behavior."""
        try:
            if not memory_graph:
                return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

            session_key = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)

            # Recall past suggestions and observations
            suggestions = memory_graph.recall(
                input_text or 'suggestions made outcomes', mode='semantic', top_k=10,
            )
            observations = memory_graph.recall(
                'user experience observation', mode='semantic', top_k=10,
            )

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            # Format findings for agent reasoning
            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            # Store the critique itself as an insight
            insight = f"Self-critique on: {input_text}"
            memory_graph.register(
                content=insight,
                metadata={
                    'memory_type': 'insight',
                    'source_agent': 'agent',
                    'session_id': session_key,
                    'type': 'self_critique',
                },
                context_snapshot=f"Self-critique during session {session_key}",
            )

            return critique
        except Exception as e:
            tool_logger.warning(f"Self_Critique_And_Enhance failed: {e}")
            return f"Self-critique on '{input_text}': Will adjust future behavior based on observations."

    tools.append((
        "Self_Critique_And_Enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique. Helps the agent "
        "learn from its own interactions.",
        self_critique_and_enhance,
    ))

    # ------------------------------------------------------------------
    # device_control — Cross-device control via PeerLink (SAME_USER only)
    # ------------------------------------------------------------------
    @log_tool_execution
    def device_control(
        action: Annotated[str, "What to do: 'turn on light', 'check temperature', 'list files', 'run command ls -la'"],
        device_hint: Annotated[str, "Which device: 'phone', 'desktop', 'iot hub', or empty for default"] = '',
    ) -> str:
        """Control a device on the user's private network via PeerLink.

        Privacy-first: only targets the user's own devices (SAME_USER trust).
        Uses PeerLink dispatch channel with FleetCommandService fallback.
        """
        try:
            # Step 1: Find the target device via DeviceRoutingService
            target_device = None
            try:
                from integrations.social.models import db_session
                from integrations.social.device_routing_service import DeviceRoutingService
                with db_session(commit=False) as db:
                    if device_hint:
                        # Map hint to capability or form factor
                        capability = 'general'
                        if device_hint.lower() in ('phone', 'desktop', 'tablet', 'tv', 'embedded', 'robot'):
                            # Filter by form factor
                            devices = DeviceRoutingService.get_user_device_map(db, str(user_id))
                            for d in devices:
                                if d.get('form_factor', '') == device_hint.lower():
                                    target_device = d
                                    break
                        if not target_device:
                            target_device = DeviceRoutingService.pick_device(
                                db, str(user_id), required_capability=capability)
                    else:
                        target_device = DeviceRoutingService.pick_device(
                            db, str(user_id), required_capability='general')
            except Exception as e:
                tool_logger.debug(f"Device routing lookup failed: {e}")

            target_node_id = (target_device or {}).get('device_id', '')

            # Step 2: Try PeerLink dispatch channel (SAME_USER trust only)
            peerlink_sent = False
            if target_node_id:
                try:
                    from core.peer_link.link_manager import get_link_manager
                    from core.peer_link.link import TrustLevel
                    mgr = get_link_manager()
                    link = mgr.get_link(target_node_id)
                    if link and link.trust == TrustLevel.SAME_USER:
                        result = mgr.send(
                            target_node_id, 'dispatch',
                            {'type': 'device_control', 'action': action,
                             'user_id': str(user_id)},
                            wait_response=True, timeout=30.0,
                        )
                        if result is not None:
                            peerlink_sent = True
                            msg = result.get('message', str(result))
                            return f"Device control result: {msg}"
                    elif link and link.trust != TrustLevel.SAME_USER:
                        return ("Device control blocked: target device is not a SAME_USER "
                                "trusted device. Only your own devices can be controlled.")
                except Exception as e:
                    tool_logger.debug(f"PeerLink dispatch failed: {e}")

            # Step 3: Fallback to FleetCommandService
            if not peerlink_sent:
                try:
                    from integrations.social.models import db_session
                    from integrations.social.fleet_command import FleetCommandService
                    with db_session() as db:
                        cmd = FleetCommandService.push_command(
                            db, target_node_id or 'self',
                            'device_control',
                            {'action': action, 'device_hint': device_hint},
                        )
                        if cmd:
                            return f"Device control command queued (id={cmd.get('id', '?')}): {action}"
                        return "Device control: failed to queue command"
                except Exception as e:
                    tool_logger.debug(f"Fleet command fallback failed: {e}")

            # Step 4: Local execution as last resort (this IS the target device)
            try:
                from integrations.social.fleet_command import FleetCommandService
                result = FleetCommandService.execute_command(
                    'device_control', {'action': action})
                if result.get('success'):
                    return f"Device control (local): {result.get('message', 'OK')}"
                return f"Device control failed: {result.get('message', 'Unknown error')}"
            except Exception as e:
                return f"Device control unavailable: {e}"

        except Exception as e:
            tool_logger.warning(f"device_control failed: {e}")
            return f"Device control error: {e}"

    tools.append((
        "device_control",
        "Control a device on the user's private network. Actions: turn on/off lights, "
        "check temperature, list files, run commands. Privacy-first: only your own devices.",
        device_control,
    ))

    # ------------------------------------------------------------------
    # data_extraction_from_url — Parity with LangChain Data_Extraction_From_URL
    # ------------------------------------------------------------------
    @log_tool_execution
    def data_extraction_from_url(
        url: Annotated[str, "The URL to extract content from"],
        url_type: Annotated[str, "Type of URL: 'pdf' or 'website'"] = "website",
    ) -> str:
        """Extract content from a URL (PDF or website). Uses Crawl4AI or direct parsing."""
        try:
            # A PDF is downloaded and counted, as before.
            if url_type == 'pdf':
                import requests as _req
                resp = _req.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
                return f"PDF downloaded ({len(resp.content)} bytes). Use a PDF parser for full extraction."

            # A website is read by the one page reader, integrations.web_
            # crawler.crawl_url (crawl4ai when it runs, else its own plain
            # read), and fitted to this tool's budget by the reader's own
            # _fit_for_agent.  The answer keeps its shapes: 'Extracted from
            # <url>:' and the text, or 'URL extraction failed:' and why.
            #
            # This tool used to carry a fetch of its own: a 'Crawl4AI'
            # registry entry that never existed, then requests.get and a
            # tag strip of the first 5000 raw characters (audit row H19).
            # Kept beside the reader as a second try, it fetched every
            # unreadable page twice (a dead host waited 2 x 30 s), and it
            # answered an error page's body (a 404, a login wall) as
            # 'Extracted from <url>', a success the page never was.  A page
            # the reader cannot read is now a failure, in the reader's words
            # (review of 8c595f0db).
            from integrations import web_crawler
            page = web_crawler.crawl_url(url, 30)
            # Why the page came without JavaScript, when crawl4ai's setup
            # failed (the reader's own line, '' otherwise): this tool does
            # not show the crawl's progress, so its agent never read it
            # (review of 42caf1a6f).
            note = web_crawler.setup_failure_note()
            note = f"\n[{note}]" if note else ''
            if page.get('success') and page.get('markdown'):
                text = web_crawler._fit_for_agent(
                    page['markdown'], 5000, "\n[Content truncated]")
                return f"Extracted from {url}:\n{text}{note}"
            return ("URL extraction failed: "
                    + str(page.get('error') or 'the page had no text to read') + note)
        except Exception as e:
            return f"URL extraction failed: {e}"

    tools.append((
        "data_extraction_from_url",
        "Extract content from a URL (PDF or website). Input: URL and type ('pdf' or 'website'). "
        "Uses Crawl4AI for rich extraction with fallback to direct HTTP fetch.",
        data_extraction_from_url,
    ))

    # ------------------------------------------------------------------
    # get_user_details — Parity with LangChain User_details_tool
    # ------------------------------------------------------------------
    @log_tool_execution
    def get_user_details() -> str:
        """Get current user's profile details."""
        try:
            uid = user_id
            # Try local DB first
            try:
                from integrations.social.models import get_db, User
                db = get_db()
                try:
                    user = db.query(User).filter_by(id=str(uid)).first()
                    if user:
                        return json.dumps(user.to_dict(), default=str)
                finally:
                    db.close()
            except Exception:
                pass

            # Fallback: cloud API
            import requests as _req
            resp = _req.post(
                'https://azurekong.hertzai.com:8443/db/getstudent_by_user_id',
                json={'user_id': uid}, timeout=10,
            )
            return f"User details: {resp.text}"
        except Exception as e:
            return f"Could not fetch user details: {e}"

    tools.append((
        "get_user_details",
        "Get the current user's profile information (name, email, preferences, etc.). "
        "Use when the user asks about their profile or when you need user context.",
        reads_persisted_state(get_user_details),
    ))

    # ------------------------------------------------------------------
    # request_resource — Parity with LangChain Request_Resource
    # ------------------------------------------------------------------
    # The one description of a credential ask, shared with Request_Resource in
    # hart_intelligence_entry; the ask goes through the same gate there too.
    from hartos.ai_key_vault import (
        REQUEST_RESOURCE_DESCRIPTION, REQUEST_RESOURCE_INPUT)

    @log_tool_execution
    def request_resource(
        resource_description: Annotated[str, REQUEST_RESOURCE_INPUT],
    ) -> str:
        """Ask the person for an outside service's credential a step needs."""
        from hartos.ai_key_vault import request_credential_for_agent
        return request_credential_for_agent(resource_description, agent_id=prompt_id)

    tools.append((
        "request_resource",
        REQUEST_RESOURCE_DESCRIPTION,
        request_resource,
    ))

    # ------------------------------------------------------------------
    # observe_user_experience — Parity with LangChain Observe_User_Experience
    # ------------------------------------------------------------------
    @log_tool_execution
    def observe_user_experience(
        event: Annotated[str, "What happened (e.g. 'clicked', 'scrolled', 'left page')"],
        page: Annotated[str, "Which page or screen"] = "",
        outcome: Annotated[str, "What was the result or user reaction"] = "",
        duration_ms: Annotated[int, "How long the interaction lasted in ms"] = 0,
    ) -> str:
        """Record a user experience observation for self-improvement."""
        observation = f"User {event} on {page} ({duration_ms}ms): {outcome}"
        try:
            if memory_graph:
                session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
                mid = memory_graph.register(
                    content=observation,
                    metadata={'memory_type': 'observation', 'source_agent': 'agent',
                              'session_id': session_id, 'page': page, 'event': event},
                    context_snapshot=f"UX observation during session {session_id}",
                )
                return f"Observation recorded (id: {mid}): {observation}"
        except Exception:
            pass
        return f"Observation noted: {observation}"

    tools.append((
        "observe_user_experience",
        "Record a user experience observation. Use to track behavior patterns "
        "for self-improvement. Input: event, page, outcome, duration_ms.",
        observe_user_experience,
    ))

    # ------------------------------------------------------------------
    # self_critique_and_enhance — Parity with LangChain Self_Critique_And_Enhance
    # ------------------------------------------------------------------
    @log_tool_execution
    def self_critique_and_enhance(
        topic: Annotated[str, "Topic or area to critique (e.g. 'my recommendations', 'search quality')"] = "",
    ) -> str:
        """Review past agent suggestions and user observations to improve future behavior."""
        try:
            if not memory_graph:
                return "Self-critique unavailable: no memory graph for this session."

            suggestions = memory_graph.recall(topic or 'suggestions made outcomes', mode='semantic', top_k=10)
            observations = memory_graph.recall('user experience observation', mode='semantic', top_k=10)

            if not suggestions and not observations:
                return "No past interactions to critique yet. Will observe and learn."

            critique = "Self-critique findings:\n"
            if suggestions:
                critique += f"Past suggestions ({len(suggestions)}):\n"
                for s in suggestions[:5]:
                    critique += f"  - {s.content[:100]}\n"
            if observations:
                critique += f"User observations ({len(observations)}):\n"
                for o in observations[:5]:
                    critique += f"  - {o.content[:100]}\n"

            session_id = f"{user_id}_{prompt_id}" if prompt_id else str(user_id)
            memory_graph.register(
                content=f"Self-critique on: {topic}",
                metadata={'memory_type': 'insight', 'source_agent': 'agent',
                          'session_id': session_id, 'type': 'self_critique'},
                context_snapshot=f"Self-critique during session {session_id}",
            )
            return critique
        except Exception as e:
            return f"Self-critique unavailable: {e}"

    tools.append((
        "self_critique_and_enhance",
        "Review past agent suggestions and user behavior observations to improve "
        "future recommendations. Input: topic or area to critique.",
        self_critique_and_enhance,
    ))

    # ------------------------------------------------------------------
    # Browser Research tools — T3 (no auth, no browser).
    # ------------------------------------------------------------------
    # Single canonical entry point — integrations.browser_research.tools.dispatch.
    # Every invocation logs to web_research_audit.log and surfaces a
    # `connection_mechanism` field so the agent can describe to the user how
    # it accessed the resource (public_http, obscura_b2_cdp_user_chrome, ...).
    #
    # T2 platform tools (Twitter / Reddit / LinkedIn / Bilibili / XHS / Weibo)
    # land via the same dispatcher in C4+ — agent_tools wiring stays here so
    # there is exactly one tool-registration surface for both LangChain and
    # autogen consumers (no parallel registry).
    try:
        from integrations.browser_research import tools as br_tools_module
    except ImportError as _br_imp_err:
        tool_logger.warning("browser_research unavailable, skipping registration: %s",
                            _br_imp_err)
        br_tools_module = None

    if br_tools_module is not None:
        @log_tool_execution
        def YouTube_Transcript(
            url: Annotated[str, "Full YouTube URL (watch / youtu.be / shorts)."],
            language: Annotated[str, "Preferred subtitle language code, e.g. 'en'."] = "en",
        ) -> str:
            """Fetch a YouTube video's transcript text without authentication.

            Returns a JSON string with success/text/segment_count/connection_mechanism.
            Domain-locked to youtube.com / youtu.be by the dispatcher's allowlist.

            Note: this is distinct from `data_extraction_from_url` because YouTube
            transcripts use the youtube_transcript_api endpoint (captions/cc), not
            page-content scraping.  Generic URL fetch belongs in
            data_extraction_from_url (Crawl4AI → web_crawler.py).
            """
            result = br_tools_module.dispatch(
                tool='YouTube_Transcript', user_id=str(user_id),
                url=url, language=language,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "YouTube_Transcript",
            "Fetch a YouTube video's transcript text via the captions/cc endpoint. "
            "No login, no API key. Input: full YouTube URL (watch/youtu.be/shorts) "
            "+ optional language code. For generic web pages use "
            "data_extraction_from_url instead — Crawl4AI is the canonical URL fetch.",
            YouTube_Transcript,
        ))

        # T2: a platform read that uses the person's stored cookies WHEN the
        # vault holds some.  Routes through web_crawler.crawl_url_with_cookies
        # (extending the canonical crawler) with AccountVault cookies +
        # optional CDP attach.  Nothing writes to that vault in this build
        # (AccountVault.add has no production caller), so today every call goes
        # out logged out and the result says so: connection_mechanism
        # 'logged_out_public', authenticated False, and a 'notice'.  Keep the
        # text below honest about that -- the model reads these descriptions
        # before it ever sees a result.
        @log_tool_execution
        def Search_Platform(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            query: Annotated[str, "Search keywords/phrase."],
            handle: Annotated[Optional[str], "Vault handle whose cookies to use; first if omitted."] = None,
        ) -> str:
            """Search a platform, with the person's stored session cookies if
            the vault holds any for it.

            Returns JSON with success/markdown/connection_mechanism/
            authenticated.  connection_mechanism says how access REALLY
            happened: obscura_b2_cdp_user_chrome or
            obscura_b1_headless_profile when cookies were sent, and
            'logged_out_public' when none were — then authenticated is False,
            a 'notice' explains it, and the page is the public view or a login
            wall, not the person's own feed.  Nothing writes to the vault in
            this build, so logged_out_public is what a default install returns
            today.  Trust 'authenticated', not the absence of cookies: with
            HEVOLVE_BROWSER_USE_B2 the crawler attaches to the person's own
            Chrome, and then it IS their session and authenticated is True.
            Consent-gated on `web_research:<platform>`.
            """
            result = br_tools_module.dispatch(
                tool='Search_Platform', user_id=str(user_id),
                platform=platform, query=query, handle=handle,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Search_Platform",
            "Search a social platform (twitter/reddit/linkedin/bilibili/xiaohongshu/weibo), "
            "with the person's stored session if one exists. Input: platform, query, optional "
            "handle. Returns JSON with markdown + connection_mechanism + authenticated; if "
            "false it is the logged-out public view, not their feed — say so. Consent-gated.",
            Search_Platform,
        ))

        @log_tool_execution
        def Read_Timeline(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            target_handle: Annotated[str, "Whose timeline to read (e.g. '@elonmusk')."],
            handle: Annotated[Optional[str], "Vault handle whose cookies to use; first if omitted."] = None,
        ) -> str:
            """Read someone's public timeline, with the person's stored session
            if the vault has one; otherwise it is a logged-out public read and
            the result says so (authenticated False)."""
            result = br_tools_module.dispatch(
                tool='Read_Timeline', user_id=str(user_id),
                platform=platform, target_handle=target_handle, handle=handle,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Read_Timeline",
            "Read someone's public timeline on a platform, with the person's stored session "
            "if one exists. Input: platform, target_handle. Returns markdown + "
            "connection_mechanism + authenticated; if false it is the logged-out public view "
            "— say so. Consent-gated on web_research:<platform>.",
            Read_Timeline,
        ))

        @log_tool_execution
        def Post_As_User(
            platform: Annotated[str, "twitter / reddit / linkedin / bilibili / xiaohongshu / weibo"],
            content: Annotated[str, "Text to post on the user's behalf."],
            handle: Annotated[Optional[str], "Vault handle to post as; first if omitted."] = None,
            dry_run: Annotated[bool, "TRUE returns a preview card; FALSE is the confirmed post, which this build cannot send."] = True,
        ) -> str:
            """Offer to post on the person's behalf — this build cannot send it.

            **Preview-confirm gate**: dry_run defaults to TRUE.  First invocation
            returns a `liquid_ui: post_preview` component for the UI to render
            with explicit Cancel/Confirm buttons.  The agent MUST re-invoke with
            dry_run=False after the user taps Confirm.  Same canonical pattern
            as Invite_Friend and channel_send.

            dry_run=False does NOT post: no per-platform write path exists, so
            it returns success False with the text back under 'content'.  Tell
            the person nothing was posted and hand them their text.

            Consent-gated on `web_research:<platform>`.
            """
            result = br_tools_module.dispatch(
                tool='Post_As_User', user_id=str(user_id),
                platform=platform, content=content, handle=handle, dry_run=dry_run,
            )
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Post_As_User",
            "Offer to post on the person's behalf on a social platform. This build CANNOT "
            "post: dry_run=True (default) returns a preview card to confirm, and dry_run=False "
            "returns the text unsent because no write path exists — say nothing was posted and "
            "hand their text back. Consent-gated on web_research:<platform>. "
            "Input: platform, content, optional handle, dry_run.",
            Post_As_User,
        ))

        # An agent's OWN browser (owner 2026-11-27: "the llm shd be able to
        # work on or use headless and with head browser (no-GUI) and (with
        # GUI) actions respectivelu").  One tool whose `action` picks the
        # step, so an agent's schema grows by one entry, not five; it is not
        # on the main leg's always-on set (agent_tool_menu.
        # MAIN_LEG_CORE_TOOLS): a recipe step naming it, or request_tools,
        # attaches it.  Whose browser profile it drives is
        # outreach_crm_tools.browser_mailbox's answer: a goal's turn works
        # only in its own goal's mailbox (owner 2026-11-24, one mailbox and
        # one isolated browser per agent).
        _BROWSER_ROUTES = {
            'open': 'Agent_Browser_Open', 'read': 'Agent_Browser_Read',
            'click': 'Agent_Browser_Click', 'type': 'Agent_Browser_Type',
            'window': 'Agent_Browser_Window'}
        # The script's own step names, which its refusals tell the model to
        # use ("open the window (action "open_window")"; review of
        # 123078e38, M3).
        _BROWSER_ALIASES = {'open_page': 'open', 'read_page': 'read',
                            'type_text': 'type', 'open_window': 'window'}
        from core.tool_refusal import RETRY_AFTER_FIX, RETRY_LATER, RETRY_NEVER
        # A step the browser could not do, by the kind the script names
        # (agent_browser._failure 'reason'): its code, whether calling
        # again can help, and what to do (review of 123078e38, M2: "read
        # the page and call again" was said even when no retry can help).
        _BROWSER_STEP_FAILURES = {
            'no_match': ('nothing_matching', RETRY_AFTER_FIX,
                         "Read the page with action 'read' to see the words it "
                         "has, then use one of those"),
            'owner_only': ('owner_fills_it_in', RETRY_AFTER_FIX,
                           "Open the window with action 'window' and ask the "
                           "owner to enter it there"),
            'no_control': ('control_not_allowed', RETRY_NEVER,
                           "Tell the person this step needs their permission "
                           "for agents to control this computer"),
            'browser_unavailable': ('browser_unavailable', RETRY_LATER,
                                    "Tell the person the browser could not "
                                    "start, or do this step another way"),
            'page_error': ('page_did_not_answer', RETRY_LATER,
                           "The page did not answer; do this step another way "
                           "or try it later"),
            # The refusal itself says what was wrong with the address (a
            # scheme, the metadata address); a metadata address is already
            # http, so the fix is never "give an http address".
            'not_web_address': ('not_a_web_address', RETRY_AFTER_FIX,
                                "Use another address: the reason above says "
                                "what was wrong with this one"),
        }

        def _browser_refusal(code, problem, fix, retry=RETRY_AFTER_FIX):
            from core.tool_refusal import refusal_fields
            return json.dumps(dict(refusal_fields('Agent_Browser', code, problem, fix,
                                                  retry=retry),
                                   success=False), ensure_ascii=False)

        @log_tool_execution
        def Agent_Browser(
            action: Annotated[str, "open, read, click, type or window"],
            url: Annotated[Optional[str], "open: the http(s) address to go to; window: optional."] = None,
            target: Annotated[Optional[str], "click: the words on the button or link; type: the field's label, placeholder or name."] = None,
            text: Annotated[Optional[str], "type: the text to put in the field."] = None,
            submit: Annotated[bool, "type: press Enter after the text."] = False,
            mailbox: Annotated[Optional[str], "Whose browser: a mailbox address. Leave empty in a goal's turn."] = None,
        ) -> str:
            """Work in this agent's own browser profile (one per mailbox, its
            own sign-in).  open/read/click/type run without a window unless
            one is open; window opens it visibly, for the owner's sign-in and
            for steps that need the screen (Computer_Action)."""
            act = str(action or '').strip().lower()
            act = _BROWSER_ALIASES.get(act, act)
            tool = _BROWSER_ROUTES.get(act)
            if tool is None:
                return _browser_refusal(
                    'unknown_action', f"action {action!r} is not a browser step",
                    "Give action as one of: open, read, click, type, window")
            needs = {'open': ('url', url), 'click': ('target', target),
                     'type': ('target', target)}.get(act)
            if needs and not needs[1]:
                return _browser_refusal(
                    'missing_' + needs[0], f"action {act!r} needs {needs[0]}",
                    f"Give {needs[0]}" + (" (the http or https address)" if act == 'open'
                                          else " (the words shown on the page)"))
            if act == 'type' and text is None:
                return _browser_refusal('missing_text', "action 'type' needs text",
                                        "Give text: what to type into the field")
            from integrations.agent_engine.outreach_crm_tools import browser_mailbox
            address, why = browser_mailbox(user_id, mailbox)
            if address is None:
                return _browser_refusal(*why)
            kwargs = {'mailbox': address}
            if act in ('open', 'window') and url:
                kwargs['url'] = url
            if act == 'click':
                kwargs.update(target=target, agent_id=prompt_id)
            if act == 'type':
                kwargs.update(field=target, text=text, submit=bool(submit),
                              agent_id=prompt_id)
            result = br_tools_module.dispatch(tool=tool, user_id=str(user_id), **kwargs)
            if not result.get('success') and not result.get('note'):
                from core.tool_refusal import refusal_fields
                code, retry, fix = _BROWSER_STEP_FAILURES.get(
                    result.get('reason'),
                    ('step_not_done', RETRY_AFTER_FIX,
                     "Do what that says, then make the next call"))
                result = dict(result, **refusal_fields(
                    'Agent_Browser', code,
                    str(result.get('error') or 'the browser step was not done'),
                    fix, retry=retry))
            return json.dumps(result, ensure_ascii=False)

        tools.append((
            "Agent_Browser",
            "Work in this agent's OWN browser (its own Chrome profile and sign-in, one "
            "per mailbox). action: open (go to url and read the page), read (the page "
            "now), click (target = the words on a button or link), type (target = the "
            "field's label; text; submit=true presses Enter), window (a visible window: "
            "the owner signs in there once, and steps that need the screen use "
            "Computer_Action on it). Runs without a window unless one is open. "
            "Passwords, one-time codes, card, PAN, Aadhaar and bank numbers are entered "
            "by the owner in the window: this tool refuses those fields. In a goal's "
            "turn leave mailbox empty: the goal's own mailbox is used.",
            Agent_Browser,
        ))

        # NOTE: Read_Webpage was removed 2026-06-08 as a parallel-path violation.
        # The canonical "fetch a URL's content" tool is `data_extraction_from_url`
        # above (line ~1008), which already delegates to Crawl4AI →
        # integrations/web_crawler.py (Playwright/Chromium headless with a
        # requests+BeautifulSoup fallback).  T2 (cookie-authenticated) work in
        # commits C4+ EXTENDS web_crawler.py with cookie injection + B2 CDP
        # attach, instead of building a parallel driver.  See
        # memory/project_browser_research_subsystem.md for the corrected plan.

    @log_tool_execution
    def validate_json_response(response: Annotated[str, "The response from a tool that should be JSON"]) -> str:
        """
        Validates and repairs JSON response from tools.

        Args:
            response: string responses from a tool that should be JSON formatted
        Returns:
            Valid JSON string or the original string if not repairable
        """
        tool_logger.info("INSIDE validate json response")
        try:
            # First try to parse as is
            json_obj = json.loads(response)
            return json.dumps(json_obj)
        except json.JSONDecodeError:
            try:

                # If parsing fails, try to repair
                repaired_json = repair_json(response)
                # Verify the repaired JSON is valid
                json_obj = json.loads(repaired_json)
                return json.dumps(json_obj)
            except Exception as e:
                # If repair filas, return the original with a warning
                tool_logger.info("JSON repair has failed")
                return f"{response}"

    tools.append((
        "validate_json_response",
        "Checks and corrects if the tool response is not JSON but expected to be.",
        validate_json_response,
    ))

    # ------------------------------------------------------------------
    # Coding-agent leg
    #
    # These four were inline closures in create_recipe.create_agents
    # (register_dual, L1674-1816) until 2026-09-10.  CREATE advertised them
    # to the recipe-authoring LLM while REUSE — which builds its tools from
    # THIS factory (reuse_recipe.py:2238) — held no copy.  Two consequences,
    # and the second is the one that hid the first:
    #   * a saved action naming one could never execute; and
    #   * _reuse_fabricated_tools could not see the name as `referenced`
    #     (that helper intersects the action text with names REGISTERED ON
    #     THE AGENTS), so it returned [] at its second early-return, before
    #     its log line.  A tool the leg cannot run was indistinguishable
    #     from an action naming no tool, and the action advanced silently.
    # Measured live 2026-09-10, agent 88719487304 action 4: FAB-GUARD
    # watermark 23 in, 23 out — zero tool calls in the whole window — no
    # verdict line at all, both subtasks closed, parent terminated in 14s.
    # 36 of the 185 saved recipes on that box name such a tool; 87 actions
    # name execute_coding_task alone.
    #
    # Deliberately NOT added to MAIN_LEG_CORE_TOOLS: reuse reaches them via
    # attach_for_names, for the action whose own recipe names one, so the
    # always-on schema stays 18 tools / ~1,859 tokens against the 12,288
    # slot (#730).  Same rule reuse_recipe.py:2429-2442 already states for
    # execute_windows_or_android_command — that closure captures 33 locals
    # of its defining function so ctx cannot build it and it is handed over
    # inline; these four capture nothing but user_id, which ctx supplies.
    # ------------------------------------------------------------------
    async def execute_coding_task(
        task: Annotated[str, "The coding task to execute (e.g., 'review this function for bugs', 'implement a login form')"],
        task_type: Annotated[str, "Task type: code_review, feature, bug_fix, refactor, app_build, debugging, multi_session"] = "feature",
        preferred_tool: Annotated[str, "Optional tool override: kilocode, claude_code, opencode, aider_native, or claw_native (empty = auto-select best)"] = "",
        working_dir: Annotated[str, "Working directory / repo path for the coding task (empty = HEVOLVE_CODING_WORKDIR, else this agent's goal repo_path, else the coding workspace)"] = "",
    ) -> str:
        """Execute a coding task using the best available coding agent tool (KiloCode, Claude Code, OpenCode, or AiderNative).

        Routes to the best tool based on benchmarks and task type.
        This is for writing, reviewing, refactoring, or debugging code —
        NOT for GUI automation (use execute_windows_or_android_command for that).
        """
        try:
            from integrations.coding_agent.orchestrator import get_coding_orchestrator
            orchestrator = get_coding_orchestrator()
            result = orchestrator.execute(
                task=task,
                task_type=task_type,
                preferred_tool=preferred_tool,
                user_id=user_id,
                model=os.environ.get('HEVOLVE_CODING_MODEL', ''),
                working_dir=_coding_workdir(working_dir),
                # The asking agent: the owner's permission and the ribbon
                # name it (orchestrator._execute_local).
                prompt_id=str(prompt_id or ''),
            )
            return json.dumps(result, indent=2)
        except Exception as e:
            tool_logger.exception("execute_coding_task failed: %s", e)
            return f"Coding task execution error: {e}"

    tools.append((
        "execute_coding_task",
        "Execute a coding task (write, review, refactor, debug code) using the best available coding agent tool. Routes to KiloCode, Claude Code, OpenCode, AiderNative, or ClawNative (Rust) based on benchmarks. Pass working_dir for the target repo path.",
        execute_coding_task,
    ))

    # The gated command path, for the coding leg: the chat's Shell_Command
    # handler (hart_intelligence_entry._handle_shell_command_tool) with a
    # coding command's bounds.  Until 2026-11-22 a coding goal ran its
    # commands through execute_windows_or_android_command, i.e. the
    # computer-use loop (a screen, 30 s, 2,000 characters of the head), and
    # 0f6f65e7e's way round that (Executor code blocks) skipped every gate.
    # No second runner: the handler is the one that passes the denylist,
    # the hard-deny policy, the owner's computer_control permission, the
    # activity indicator and run_bounded.  Fails closed while HARTOS loads,
    # as the computer-use loop's shell step does (local_computer_tool).
    async def Shell_Command(
        command: Annotated[str, "One command, run in your workspace with no window: tests, lint, build, install, git, reading or writing a file. Windows runs cmd.exe and Linux/macOS /bin/sh; 'powershell: ' or 'bash: ' in front picks that shell"],
        working_dir: Annotated[str, "Folder to run in (empty = HEVOLVE_CODING_WORKDIR, else this agent's goal repo_path, else the coding workspace; a relative path is inside the coding workspace)"] = "",
    ) -> str:
        """Run one command through the gated command path and return its
        exit code and output (the first and last part of each stream)."""
        from core.constants import (AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE,
                                    CODING_COMMAND_OUTPUT_CHARS,
                                    CODING_COMMAND_TIMEOUT_S)
        from core.safe_hartos_attr import safe_hartos_attr
        from integrations.vlm.vlm_adapter import resolve_task_workspace
        handler = safe_hartos_attr('_handle_shell_command_tool')
        if handler is None:
            return ("Shell_Command not run: HARTOS is still loading the "
                    "command safety list. Try again in a moment.")
        root = resolve_task_workspace(prompt_id=prompt_id,
                                      explicit=_coding_workdir(working_dir))
        # The whole reply fits one seat message: every seat keeps
        # AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE tokens of a message and cuts the
        # end, where stderr's exception is (review of 2982773f7).
        return handler(command, timeout_s=CODING_COMMAND_TIMEOUT_S,
                       output_chars=CODING_COMMAND_OUTPUT_CHARS, cwd=root,
                       reply_tokens=AUTOGEN_MESSAGE_TOKENS_PER_MESSAGE)

    tools.append((
        "Shell_Command",
        "Run one command (tests, lint, build, install, git, reading or writing a file) in the coding workspace, in the background with no window, through the owner's permission and the command safety list. Returns the exit code, then the first and last part of each output stream.",
        Shell_Command,
    ))

    # Repository map tool — tree-sitter based code understanding.
    # Import-gated exactly as create_recipe had it: absent, not broken,
    # when aider_core is not installed.
    try:
        from integrations.coding_agent.recipe_bridge import CodingRecipeBridge

        async def get_repository_map(
            working_dir: Annotated[str, "Directory to map (empty = HEVOLVE_CODING_WORKDIR, else this agent's goal repo_path, else the coding workspace)"] = "",
            max_tokens: Annotated[int, "Maximum tokens for the map output"] = 2048,
        ) -> str:
            """Generate a tree-sitter based repository map showing key functions, classes, and their relationships.

            Use this to understand a codebase's structure before making changes.
            Returns a ranked summary of the most important code symbols.
            """
            # Never the process cwd.  The map writes its tags cache under the
            # root it maps, and '.' put the 14 MB .aider.tags.cache.v4 in the
            # installed app's own folder (review of cd99c540d).
            from integrations.vlm.vlm_adapter import resolve_task_workspace
            root = resolve_task_workspace(prompt_id=prompt_id,
                                          explicit=_coding_workdir(working_dir))
            return CodingRecipeBridge.get_repository_map(root, max_tokens)

        tools.append((
            "get_repository_map",
            "Generate a tree-sitter repository map showing key functions, classes, and structure. Use before coding tasks to understand the codebase.",
            get_repository_map,
        ))
    except ImportError:
        tool_logger.debug("Repository map tool not available (aider_core not installed)")

    # Shard Engine: Call-chain context for coding tasks.
    # Target function + upstream callers + downstream callees = FULL source.
    # Everything else = interfaces only. Exposure proportional to task.
    # Call graph from Trueflow MCP (IDE) or AST fallback (headless).
    try:
        async def create_code_shard(
            task: Annotated[str, "Description of the coding task"],
            target_file: Annotated[str, "Relative path to the file containing the target function"],
            target_function: Annotated[str, "Name of the function to modify"],
            repo_path: Annotated[str, "Path to the repository (default: HART OS install dir)"] = "",
        ) -> str:
            """Create a code shard with call-chain context for a coding task.

            Returns:
            - Target function: FULL source (what you're modifying)
            - Upstream callers: FULL source (who calls it, input contracts)
            - Downstream callees: FULL source (what it calls, output contracts)
            - Everything else: Interfaces only (signatures + types)

            Call graph sourced from Trueflow MCP (when IDE running) or AST fallback.
            Security: exposure proportional to the task. E2E encrypted for peer offload.
            Use execute_coding_task with working_dir to actually apply edits.
            """
            from integrations.agent_engine.shard_engine import ShardEngine
            engine = ShardEngine(code_root=repo_path) if repo_path else ShardEngine()
            shard = engine.create_call_chain_shard(
                task=task, target_file=target_file,
                target_function=target_function)
            return json.dumps({
                'shard_id': shard.shard_id,
                'task': shard.task_description,
                'scope': shard.scope.value,
                'target_files': shard.target_files,
                'call_chain_source': shard.full_content,
                'interfaces': [{'file': s.file_path, 'functions': s.functions,
                               'classes': s.classes} for s in shard.interface_specs],
            }, indent=2, default=str)

        tools.append((
            "create_code_shard",
            "Create a code shard with call-chain context: target function + upstream callers + downstream callees (FULL source), everything else interfaces only.",
            create_code_shard,
        ))
    except Exception:
        tool_logger.debug("Shard engine tool not available")

    # Benchmark Tracker: Query which coding tool performs best for each task type
    try:
        async def get_coding_benchmarks(
            task_type: Annotated[str, "Task type to check (code_review, feature, bug_fix, refactor, app_build, debugging, multi_session, or 'all')"] = "all",
        ) -> str:
            """Get coding tool benchmarks — which tool (KiloCode, Claude Code, OpenCode, AiderNative) performs best.

            Returns success rates, average times, and sample counts per tool per task type.
            Includes both local benchmarks and hive-aggregated intelligence from peers.
            """
            from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker
            tracker = get_benchmark_tracker()
            result = {'local': {}, 'hive': {}}

            if task_type == 'all':
                # None until a tool has MIN_SAMPLES rows under the current
                # success rule (benchmark_tracker.SUCCESS_RULE).
                delta = tracker.export_learning_delta() or {}
                result['local'] = delta.get('coding_benchmarks', {})
            else:
                best = tracker.get_best_tool(task_type)
                if best:
                    result['local'][task_type] = {
                        'best_tool': best[0], 'success_rate': best[1],
                        'avg_time_s': best[2],
                    }
                hive_best = tracker.get_hive_best_tool(task_type)
                if hive_best:
                    result['hive'][task_type] = {
                        'best_tool': hive_best[0], 'success_rate': hive_best[1],
                        'avg_time_s': hive_best[2],
                    }
            return json.dumps(result, indent=2, default=str)

        tools.append((
            "get_coding_benchmarks",
            "Query coding tool benchmarks — which tool performs best per task type. Includes local and hive-aggregated data.",
            get_coding_benchmarks,
        ))
    except Exception:
        tool_logger.debug("Benchmark tracker tool not available")

    # execute_coding_task and get_repository_map are dropped where they
    # cannot run (CODING_TOOL_NEEDS): no leg offers them, attach_for_names
    # cannot attach them, and the step checker (action_named_tools) does not
    # hold a step for one.  Live on central 2026-11-21, goal 4f294fa3: a step
    # named execute_coding_task, it answered "No coding tools installed", the
    # agent read the files with get_user_uploaded_file, and the step stayed
    # open until the goal paused.  coding_capabilities() never checks on this
    # thread (it reads the last background check), and before the first
    # check, or when reading it fails, both stay, as before.
    try:
        from integrations.coding_agent.orchestrator import coding_capabilities
        _withheld = withheld_coding_tools(coding_capabilities())
    except Exception as e:
        tool_logger.warning("coding capability read failed (%s); the coding "
                            "tools stay offered", e)
        _withheld = frozenset()
    if _withheld:
        tools[:] = [t for t in tools if t[0] not in _withheld]
        tool_logger.info("not offered on this node, which cannot run them: %s",
                         ', '.join(sorted(_withheld)))

    # ------------------------------------------------------------------
    # Book / learning navigation — appended HERE, not via a separate
    # register_*_if_available() registrar.
    #
    # register_remote_desktop_tools_if_available (below) has NO production
    # caller — only tests/unit/test_remote_desktop_agent_tools.py:235 — so a
    # tool registered that way never reaches a live turn.  The live path is
    # build_core_tool_closures() -> register_core_tools(), called from
    # create_recipe.py:1096/1116 and reuse_recipe.py:2238/2264.  Appending to
    # `tools` is therefore the only wiring that actually runs.
    # ------------------------------------------------------------------
    try:
        from integrations.learning.book_tools import build_book_tools
        _book = build_book_tools(ctx)
        if _book:
            tools.extend(_book)
            tool_logger.info("Book navigation tools registered (%d)", len(_book))
    except ImportError:
        pass
    except Exception as e:
        tool_logger.warning("Book tools registration failed: %s", e)

    # The person's answer, asked on whichever of their devices they answer
    # (a file_request for clips, a form, a choice), for every agent alike:
    # integrations.agent_engine.person_ask (owner 2026-10-27, "liquid ui is
    # asked as a tool by server").  The turn's request id is read at each
    # call: a background turn asks its goal's person (person_to_ask).
    try:
        from integrations.agent_engine.person_ask import ask_person_tool
        _name, _desc, _ask_fn = ask_person_tool(
            user_id, prompt_id,
            request_id=lambda: request_id_list.get(user_prompt))
        tools.append((_name, _desc, log_tool_execution(_ask_fn)))
    except Exception as e:
        tool_logger.warning("ask_person tool not registered: %s", e)

    return tools


def register_remote_desktop_tools_if_available(ctx, helper, executor):
    """Register remote desktop tools if the module is available.

    Gracefully skips if integrations/remote_desktop is not installed.
    """
    try:
        from integrations.remote_desktop.agent_tools import (
            build_remote_desktop_tools, register_remote_desktop_tools,
        )
        rd_tools = build_remote_desktop_tools(ctx)
        register_remote_desktop_tools(rd_tools, helper, executor)
        tool_logger.info(f"Remote desktop tools registered ({len(rd_tools)} tools)")
    except ImportError:
        pass
    except Exception as e:
        tool_logger.warning(f"Remote desktop tools registration failed: {e}")


def register_memory_graph_tools(memory_graph, helper, executor, user_id, user_prompt):
    """Register MemoryGraph provenance tools if memory_graph is available.

    Delegates to the existing agent_memory_tools module.
    """
    if memory_graph is None:
        return
    try:
        from integrations.channels.memory.agent_memory_tools import create_memory_tools, register_autogen_tools
        mem_tools = create_memory_tools(memory_graph, str(user_id), user_prompt)
        register_autogen_tools(mem_tools, executor, helper)
        tool_logger.info(f"MemoryGraph tools registered for {user_prompt}")
    except Exception as e:
        tool_logger.warning(f"MemoryGraph tools registration failed: {e}")
