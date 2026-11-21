"""Where each person is in an agent's flows, kept across restarts.

Owner, 2026-11-03: a person moves to their next flow "when its goal is met".
Owner rulings, 2026-11-04:
- When the goal of a persona's LAST flow is met, the agent goes back to the
  weakest earlier flow, per the progress record.
- A flow whose goal cannot be checked moves after one finished run.
- The parent and the therapist who joined the agent move the child from
  their own chat.
Owner rulings, 2026-11-06: the agent asks a person's persona in conversation
"so that appropriate persona and corresponding flow gets associated at first
time agent use"; "multiple persons multiple accounts associated via consent"
(a parent or therapist joins the child's chat from their own account once the
child's side approves; one account on several devices is set aside for now).
So the pick is kept per account, and the place per persona.

Nothing kept this before. The persona a person picked lived only in 2 h
in-memory caches (core.persona_registry), so after two idle hours or a
restart get_role read 'user' and a returning parent ran the child's first
flow. A flow pointer did not exist at all, so REUSE always ran a persona's
first flow (get_flow_number, "FIRST match").

One JSON file per account per agent under <data dir>/flow_progress, named by
core.agent_tools.person_store_id(f"{user_id}_{prompt_id}"). The fields:
- persona: written only by set_persona, and only with one of the agent's
  personas (review of f9951c0d2: any name the model passed was kept forever).
- join {creator, persona, ask_id}: a join waiting on the child's side,
  written by set_pending_join, cleared by clear_pending_join.
- places {persona: {flow, by, at}}: written only by set_person_flow.
- running {persona: {flow, at}}: written only by note_run_started when the
  first step of a run of that flow starts (cold or warm), once per run;
  taken and cleared by record_flow_finished as the run's start, so a
  person's move made during the run outlives its verdict.
- unjudged: written by record_flow_finished, emptied by settle.  Each record
  names the persona that ran it, so it is judged against that persona's
  flows whoever's turn comes next (review of f9951c0d2: a record judged
  against the next turn's persona was dropped unjudged for every
  one-persona agent after a restart).  Its 'memory' is what the run read
  and saved, in order (run_memory), or None when that is not known.
- saved_keys {flow: [key]}: every key a finished run of that flow saved,
  written only by record_flow_finished; settle reads it to tell another
  flow's key from this flow's own.
- checkable, verdicts: written only by settle.

Each write is a read-modify-write under one lock per file: one HARTOS process
writes this store per node, and the child's turn and the therapist's turn
can land in the same second. It is a child's record: local only, never
synced, never in a recipe.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

VERDICTS_KEPT = 10       # per flow: what the weakest-flow question reads (owner, 11-04)
UNJUDGED_KEPT = 5
EVIDENCE_KEPT = 4000     # characters of one finished run's record, per part
MEMORY_KEPT = 200        # memory calls of one finished run; a longer run keeps none
SAVED_KEYS_KEPT = 200    # per flow, the newest
KEY_KEPT = 200           # characters of one key

#: The memory tools a run's reads and saves are read from (core.agent_tools):
#: get_data_by_key reads a key (get_data_from_memory is the same tool,
#: core.constants.TOOL_NAME_ALIASES), save_data_in_memory saves one.
READ_TOOL = 'get_data_by_key'
SAVE_TOOL = 'save_data_in_memory'
#: How save_data_in_memory's answer to a save that held opens ("Saved at
#: <key>: <value>"); its refusals and failures open with "Error:".
SAVED_AT = 'Saved at '

_CHECKABLE_SCHEMA = {'type': 'object', 'additionalProperties': False,
                     'required': ['checkable'],
                     'properties': {'checkable': {'type': 'boolean'}}}
_MET_SCHEMA = {'type': 'object', 'additionalProperties': False,
               'required': ['met', 'evidence'],
               'properties': {'met': {'type': 'boolean'}, 'evidence': {'type': 'string'}}}
_WEAKEST_SCHEMA = {'type': 'object', 'additionalProperties': False,
                   'required': ['flow'], 'properties': {'flow': {'type': 'string'}}}

CHECKABLE_PROMPT = (
    "An agent works through flows with a person. One flow's goal is:\n{goal}\n\n"
    "Can what the person said or did in one session, as recorded, show whether "
    "this goal was met? Answer with the JSON object only.")
MET_PROMPT = (
    "An agent works through flows with a person. This flow's goal is:\n{goal}\n\n"
    "What the agent set out to do in each step (its own instructions, NOT "
    "evidence):\n{steps}\n\n"
    "What the run observed (the person's replies and each step's verified "
    "result):\n{observed}\n\n"
    "By what the run observed alone, was the goal met? Answer with the JSON "
    "object only; 'evidence' quotes the part of the observed record that "
    "decides it.")
WEAKEST_PROMPT = (
    "The person has met the goal of their last flow. Their earlier flows, each "
    "with what its checks found:\n{flows}\n\nWhich of these flows is weakest now "
    "(least retained or recalled)? Answer with the JSON object only, giving its "
    "exact name.")

_locks: dict = {}
_locks_guard = threading.Lock()


def _path(user_id, prompt_id) -> str:
    from core.agent_tools import person_store_id
    from core.platform_paths import get_data_dir
    return os.path.join(get_data_dir(), 'flow_progress',
                        person_store_id(f'{user_id}_{prompt_id}') + '.json')


def _lock(path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def read(user_id, prompt_id) -> dict:
    """This account's record for this agent; {} when there is none or it
    cannot be read (logged)."""
    path = _path(user_id, prompt_id)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("flow progress %s: unreadable (%s)", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _update(user_id, prompt_id, change: Callable[[dict], None]) -> dict:
    from core.file_cache import atomic_json_write
    path = _path(user_id, prompt_id)
    with _lock(path):
        data = read(user_id, prompt_id)
        change(data)
        atomic_json_write(path, data)
        return data


def _key(name) -> str:
    return str(name or '').strip().lower()


def _same(a, b) -> bool:
    return _key(a) == _key(b)


def persona_flows(flows, persona) -> list:
    """The indexes of ``flows`` that belong to ``persona``, in order."""
    return [i for i, f in enumerate(flows or [])
            if isinstance(f, dict) and persona and _same(f.get('persona'), persona)]


def persona_named(persona, personas: Iterable) -> Optional[str]:
    """``persona`` spelled as the agent spells it, when it is one of
    ``personas``; else None."""
    for name in personas or ():
        if isinstance(name, str) and name.strip() and _same(name, persona):
            return name.strip()
    return None


def place_persona(role, personas) -> Optional[str]:
    """The persona whose place a run uses: the person's role when it is one
    of the agent's ``personas``, else the agent's only persona when it has
    exactly one (a one-persona agent never asks the persona question, so its
    role reads 'user'; review of f9951c0d2), else None."""
    personas = [p for p in (personas or ()) if isinstance(p, str) and p.strip()]
    named = persona_named(role, personas)
    if named is None and len(personas) == 1:
        named = personas[0].strip()
    return named


def stored_persona(user_id, prompt_id, personas) -> Optional[str]:
    """The persona this person picked at their first use of the agent, when
    it is still one of the agent's ``personas``.  A pick the agent no longer
    has reads as none, so the persona question comes back."""
    return persona_named(read(user_id, prompt_id).get('persona'), personas)


def set_persona(user_id, prompt_id, persona, personas) -> Optional[str]:
    """Keep the persona a person picked (update_persona only).  Only one of
    the agent's ``personas`` is kept; anything else is refused and logged, so
    a wrong name never sticks (review of f9951c0d2).  Returns the name kept."""
    name = persona_named(persona, personas)
    if name is None:
        logger.warning("flow progress %s_%s: %r is not one of this agent's personas; "
                       "not kept", user_id, prompt_id, persona)
        return None
    _update(user_id, prompt_id, lambda d: d.update(persona=name))
    return name


def pending_join(user_id, prompt_id) -> Optional[dict]:
    """The join this person asked for and the child's side has not answered."""
    join = read(user_id, prompt_id).get('join')
    return join if isinstance(join, dict) and join.get('ask_id') else None


def set_pending_join(user_id, prompt_id, creator, persona, ask_id) -> None:
    _update(user_id, prompt_id, lambda d: d.update(join={
        'creator': str(creator), 'persona': str(persona), 'ask_id': str(ask_id),
        'at': time.time()}))


def clear_pending_join(user_id, prompt_id) -> None:
    _update(user_id, prompt_id, lambda d: d.pop('join', None))


def _place(data, persona) -> dict:
    place = (data.get('places') or {}).get(_key(persona))
    return place if isinstance(place, dict) else {}


def current_flow(user_id, prompt_id, flows, persona) -> Optional[int]:
    """``persona``'s stored flow when it is still one of theirs, else None
    (the builders then take the persona's first flow, as before)."""
    index = _place(read(user_id, prompt_id), persona).get('flow')
    return index if isinstance(index, int) and index in persona_flows(flows, persona) else None


def set_person_flow(user_id, prompt_id, flows, persona, flow_index, by) -> bool:
    """The one writer of a persona's place: ``by`` is 'goal_met' or the id of
    the person who moved them. A flow of another persona is refused."""
    if flow_index not in persona_flows(flows, persona):
        logger.warning("flow progress %s_%s: flow %r is not %r's; not moved",
                       user_id, prompt_id, flow_index, persona)
        return False
    _update(user_id, prompt_id, lambda d: d.setdefault('places', {}).__setitem__(
        _key(persona), {'flow': int(flow_index), 'by': str(by), 'at': time.time()}))
    logger.info("flow progress %s_%s: %s now on flow %s (by %s)",
                user_id, prompt_id, persona, flow_index, by)
    return True


def note_run_started(user_id, prompt_id, persona, flow_index) -> None:
    """The first step of a run of ``persona``'s ``flow_index`` starts now.
    record_flow_finished takes this time, so a move made while the run was
    going on (a therapist moving the child mid-session) is never undone by
    that run's verdict (hartos-16, 11-07).  Once per run: a run's first step
    starts again when it waited for the person's answer or spans turns, and
    that must not move the stamp past a move made meanwhile, so a stamp for
    the same flow stays until record_flow_finished takes it.  (A run left
    unfinished leaves its older stamp, which only ever favours keeping a
    person's move.)"""
    def change(d):
        running = d.setdefault('running', {})
        current = running.get(_key(persona))
        if isinstance(current, dict) and current.get('flow') == int(flow_index):
            return      # this run's start is already stamped
        running[_key(persona)] = {'flow': int(flow_index), 'at': time.time()}
    _update(user_id, prompt_id, change)


def _run_start(messages) -> Optional[int]:
    """Where the latest run starts in ``messages``: the first of the last
    unbroken string of action 1's dispatches (a re-steer of action 1, or the
    seed of the turn the person answered on, dispatches it again), or None
    when ``messages`` holds no dispatch of action 1.  Seeded history
    (``_from_shared``) is an earlier run's, never this one's."""
    from hartos.lifecycle_hooks import dispatch_action_id
    start = previous = None
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get('_from_shared'):
            continue
        action = dispatch_action_id(m.get('content'))
        if action is None:
            continue
        if action == 1 and previous != 1:
            start = i
        previous = action
    return start


def _call_key(function) -> Optional[str]:
    """The key a memory call named, from its arguments as the model wrote
    them; None when they cannot be read."""
    args = function.get('arguments')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    key = str(args.get('key') or '').strip() if isinstance(args, dict) else ''
    return key[:KEY_KEPT] or None


def run_memory(lists) -> Optional[list]:
    """What a finished run read and saved in memory, in the order it did:
    ``[{'read': key, 'ok': bool} or {'save': key, 'ok': bool}]``, from the
    run's tool calls, never from its steps' text (the model names keys as it
    goes).  ``lists`` are the run's message lists
    (hartos.lifecycle_hooks.evidence_lists: the group log, then each
    participant's buffer); a call is counted once, from where the run starts
    in each list (_run_start), ordered by the action it ran under, then where
    it stands.  A read is not ok only on get_data_by_key's own not-found
    answer (core.agent_tools.KEY_NOT_FOUND), and a save is ok only on
    save_data_in_memory's own success answer (SAVED_AT); a call whose answer
    is missing is neither.  A key that cannot be read is None.

    None when no list holds the run's start (what it did before is not
    there), or when the run made more than MEMORY_KEPT memory calls: then
    nothing is concluded from it."""
    from core.agent_tools import KEY_NOT_FOUND
    from core.constants import HISTORICAL_TOOL_PLACEHOLDER, tool_name_in
    from hartos.lifecycle_hooks import _tool_results, dispatch_action_id, tool_call_names
    calls, answers, seen = {}, {}, False
    for rank, msgs in enumerate(lists or []):
        start = _run_start(msgs) if isinstance(msgs, list) else None
        if start is None:
            continue
        seen, action = True, 1
        names = tool_call_names([msgs])
        for i in range(start, len(msgs)):
            m = msgs[i]
            if not isinstance(m, dict) or m.get('_from_shared'):
                continue
            dispatched = dispatch_action_id(m.get('content'))
            action = action if dispatched is None else dispatched
            for tc in (m.get('tool_calls') or []):
                function = tc.get('function') if isinstance(tc, dict) else None
                if not isinstance(function, dict):
                    continue
                kind = ('read' if tool_name_in(function.get('name'), (READ_TOOL,))
                        else 'save' if tool_name_in(function.get('name'), (SAVE_TOOL,))
                        else None)
                call_id = tc.get('id')
                if kind and call_id and call_id not in calls:
                    calls[call_id] = ((action, rank, i), kind, _call_key(function))
            if m.get('role') == 'tool':
                for call_id, _name, body in _tool_results(m, names):
                    if (call_id and body is not None and call_id not in answers
                            and HISTORICAL_TOOL_PLACEHOLDER not in str(body)):
                        answers[call_id] = str(body).lstrip()
    if not seen or len(calls) > MEMORY_KEPT:
        return None
    out = []
    for call_id, (_at, kind, key) in sorted(calls.items(), key=lambda c: c[1][0]):
        answer = answers.get(call_id)
        ok = ((answer is None or not answer.startswith(KEY_NOT_FOUND)) if kind == 'read'
              else (answer is not None and answer.startswith(SAVED_AT)))
        out.append({kind: key, 'ok': ok})
    return out


def record_flow_finished(user_id, prompt_id, flow_index, persona, run_id,
                         observed='', steps='', messages=None) -> None:
    """A run of ``persona`` finished every action of ``flow_index``.
    ``observed`` is what the run saw (the person's replies and each step's
    verified result); ``steps`` is what the agent set out to do, kept apart
    because a step's own text restates the goal and must never count as
    evidence that it was met (review of f9951c0d2).  Both are taken while
    the run's ledger is live and kept on disk, unjudged, until settle runs at
    the next run's start, so a restart in between loses nothing and the
    reply that finished the flow never waits on the judge.  ``messages``:
    the run's message lists, from which what it read and saved is kept
    (run_memory), and each key it saved is added to the flow's saved_keys."""
    memory = run_memory(messages)

    def change(d):
        now = time.time()
        running = (d.get('running') or {}).get(_key(persona)) or {}
        started = running.get('at') if running.get('flow') == int(flow_index) else None
        # The run is over: its start is taken, so the next run stamps fresh.
        (d.get('running') or {}).pop(_key(persona), None)
        records = [r for r in (d.get('unjudged') or []) if isinstance(r, dict)]
        records.append({'flow': int(flow_index), 'persona': str(persona or ''),
                        'run': str(run_id), 'at': now,
                        'started': started if isinstance(started, (int, float)) else now,
                        'observed': str(observed or '')[:EVIDENCE_KEPT],
                        'steps': str(steps or '')[:EVIDENCE_KEPT],
                        'memory': memory})
        d['unjudged'] = records[-UNJUDGED_KEPT:]
        # A key this flow tried to save is its own whether or not the save
        # held: only another flow's key can be a report input.
        saved = [k for k in ((d.get('saved_keys') or {}).get(str(int(flow_index))) or [])
                 if isinstance(k, str)]
        for call in (memory or []):
            if call.get('save') and call['save'] not in saved:
                saved.append(call['save'])
        if saved:
            d.setdefault('saved_keys', {})[str(int(flow_index))] = saved[-SAVED_KEYS_KEPT:]
    _update(user_id, prompt_id, change)


def _fold(text) -> str:
    return ' '.join(str(text or '').lower().split()).strip(' "\'.,;:!?')


def _quoted_in(quote, record) -> bool:
    """``quote`` appears in ``record``, ignoring case, spacing and the quote
    marks or end punctuation a model wraps it in."""
    q = _fold(quote)
    return bool(q) and q in _fold(record)


def _flow_name(flows, index) -> str:
    flow = flows[index] if 0 <= index < len(flows or []) else {}
    return str((flow or {}).get('flow_name') or f'flow {index}')


def _checkable(user_id, prompt_id, flows, index, ask) -> Optional[bool]:
    """Whether ``index``'s goal can be checked from one run's record: decided
    once per flow, from the goal text alone, and kept (hartos-7a: a model that
    sometimes says no on a real goal would move a child on unseen). An empty
    goal is not checkable. None: the model could not be asked."""
    known = (read(user_id, prompt_id).get('checkable') or {}).get(str(index))
    if isinstance(known, bool):
        return known
    goal = str((flows[index] or {}).get('sub_goal') or '').strip()
    if not goal:
        decided = False
    else:
        reply = ask(CHECKABLE_PROMPT.format(goal=goal), _CHECKABLE_SCHEMA, 'flow_goal_checkable')
        if reply is None or not isinstance(reply.get('checkable'), bool):
            return None
        decided = reply['checkable']
    _update(user_id, prompt_id,
            lambda d: d.setdefault('checkable', {}).__setitem__(str(index), decided))
    logger.info("flow progress %s_%s: flow %s (%s) goal checkable=%s",
                user_id, prompt_id, index, _flow_name(flows, index), decided)
    return decided


def _next_after_met(user_id, prompt_id, flows, persona, index, ask) -> int:
    mine = persona_flows(flows, persona)
    later = [i for i in mine if i > index]
    if later:
        return later[0]
    earlier = [i for i in mine if i < index]
    if not earlier:
        return index
    verdicts = read(user_id, prompt_id).get('verdicts') or {}
    listing = '\n'.join(
        f"- {_flow_name(flows, i)}: " + ('; '.join(
            f"{'met' if v.get('met') else 'not met'} ({str(v.get('evidence') or '')[:120]})"
            for v in (verdicts.get(str(i)) or [])[-3:]) or 'no checks recorded')
        for i in earlier)
    reply = ask(WEAKEST_PROMPT.format(flows=listing), _WEAKEST_SCHEMA, 'flow_weakest')
    name = str((reply or {}).get('flow') or '').strip()
    for i in earlier:
        if _same(_flow_name(flows, i), name):
            return i
    logger.info("flow progress %s_%s: no weakest earlier flow named (%r); stays on %s",
                user_id, prompt_id, name, index)
    return index


def _same_path(a, b) -> bool:
    """Keys ``a`` and ``b`` name the same saved value or one holds the
    other (dot paths: 'family' holds 'family.amma')."""
    return a == b or a.startswith(b + '.') or b.startswith(a + '.')


def _only_report_inputs_missing(record, saved) -> list:
    """The keys a finished run found nothing at, when that is all that kept
    its goal unmet; [] when anything else may have (hartos-52's rule,
    2026-11-21).

    Live 2026-11-21, the speech agent's Parent flow "Home names and photos":
    it saves the family names, then reads the child's results to report
    them.  The judge said "Nothing is saved at name_response" while the child
    had not practised, so the parent was asked for the names every turn.

    Each read is placed by where it happened in the run (``record['memory']``,
    run_memory): before or between this flow's own saves it is a WORK input,
    after its last save a REPORT input.  A key is FOREIGN when no recorded
    run of this flow saved it (``saved``, the flow's saved_keys, and this
    run's own saves).  The run counts as done for now when every one of its
    saves held, it read at least one key it found nothing at, and every such
    key is a foreign report input.  A run with no save, a save that failed,
    a missing work input, a missing key of this flow's own, a key that could
    not be read, or no memory record is not."""
    memory = record.get('memory')
    if not isinstance(memory, list):
        return []
    calls = [c for c in memory if isinstance(c, dict)]
    saves = [i for i, c in enumerate(calls) if 'save' in c]
    if not saves or not all(calls[i].get('ok') is True for i in saves):
        return []
    own = [k for k in (saved if isinstance(saved, list) else []) if isinstance(k, str)]
    own += [calls[i]['save'] for i in saves if isinstance(calls[i].get('save'), str)]
    missing = []
    for i, call in enumerate(calls):
        if 'read' not in call or call.get('ok') is not False:
            continue
        key = call.get('read')
        if (i < saves[-1] or not isinstance(key, str) or not key
                or any(_same_path(key, k) for k in own)):
            return []
        if key not in missing:
            missing.append(key)
    return missing


def _keep_verdict(user_id, prompt_id, index, met, evidence, run) -> None:
    def keep(d):
        history = d.setdefault('verdicts', {}).setdefault(str(index), [])
        history.append({'met': met, 'evidence': evidence, 'run': run, 'at': time.time()})
        d['verdicts'][str(index)] = history[-VERDICTS_KEPT:]
    _update(user_id, prompt_id, keep)


def settle(user_id, prompt_id, flows, persona=None, ask: Optional[Callable] = None) -> Optional[int]:
    """At the start of a run on this account: judge every flow finished since,
    each against the flows of the persona that ran it, move that persona
    through set_person_flow, and return ``persona``'s flow (None: no place,
    or no persona yet; the builders' first match stands).  Never needs the
    current turn's persona, so nothing waits on the persona question and a
    one-persona agent progresses.  Asks the model only when a finished run
    waits, so most turns pay nothing.  A record the model could not judge
    stays for the next run.  A run judged not met whose only missing reads
    are another flow's report inputs moves as a met one does
    (_only_report_inputs_missing)."""
    pending = [r for r in (read(user_id, prompt_id).get('unjudged') or [])
               if isinstance(r, dict) and isinstance(r.get('flow'), int)]
    if not pending:
        return current_flow(user_id, prompt_id, flows, persona) if persona else None
    if ask is None:
        from core.llm_json import ask_model_json as ask
    started = time.monotonic()
    judged = set()
    for record in pending:
        index, ran_as = record['flow'], record.get('persona')
        if index not in persona_flows(flows, ran_as):
            # The agent no longer gives this flow to that persona (it was
            # re-created): kept as a verdict, never silently dropped.
            _keep_verdict(user_id, prompt_id, index, False,
                          f"flow is no longer {ran_as!r}'s on this agent", record['run'])
            logger.info("flow progress %s_%s: flow %s is no longer %r's; run %s closed unjudged",
                        user_id, prompt_id, index, ran_as, record['run'])
            judged.add(record['run'])
            continue
        checkable = _checkable(user_id, prompt_id, flows, index, ask)
        if checkable is None:
            continue
        if checkable:
            goal = str(flows[index].get('sub_goal') or '').strip()
            observed = record.get('observed') or ''
            reply = ask(MET_PROMPT.format(goal=goal, steps=record.get('steps') or '(none recorded)',
                                          observed=observed or '(nothing observed)'),
                        _MET_SCHEMA, 'flow_goal_met')
            if reply is None or not isinstance(reply.get('met'), bool):
                continue
            met, evidence = reply['met'], str(reply.get('evidence') or '')[:300]
            if met and not _quoted_in(evidence, observed):
                # A "met" must quote what the run OBSERVED.  A quote of the
                # agent's own step text (which restates the goal) or a quote
                # the record does not hold is the model's own words, and must
                # not move a child on unseen (hartos-7a 11-06; review of
                # f9951c0d2).
                logger.info("flow progress %s_%s: flow %s 'met' quote not in what the run "
                            "observed; counted not met", user_id, prompt_id, index)
                met, evidence = False, f'quote not in what the run observed: {evidence[:200]}'
            if not met:
                # Unmet only for want of what another flow saves, read to
                # report it after this flow did its own work: done for now,
                # and the flow comes round again later (hartos-52, 11-21).
                # Never turns a met into unmet.
                missing = _only_report_inputs_missing(
                    record, ((read(user_id, prompt_id).get('saved_keys') or {})
                             .get(str(index))))
                if missing:
                    logger.info("flow progress %s_%s: flow %s judged not met (%s); only "
                                "report inputs missing, done for now", user_id, prompt_id,
                                index, evidence[:120])
                    met, evidence = True, (f"done for now: only report inputs missing "
                                           f"({', '.join(missing)})")[:300]
        else:
            met, evidence = True, 'goal not checkable: moves after one finished run (owner, 11-04)'
        _keep_verdict(user_id, prompt_id, index, met, evidence, record['run'])
        logger.info("flow progress %s_%s: flow %s (%s) met=%s: %s", user_id, prompt_id, index,
                    _flow_name(flows, index), met, evidence[:120])
        run_started = record.get('started') or record.get('at') or 0
        run_finished = record.get('at') or run_started
        place = _place(read(user_id, prompt_id), ran_as)
        # A person's move (a therapist, a parent) made after this run STARTED
        # is never undone by a verdict on it.  A verdict's own move
        # ('goal_met') blocks only when made after this run FINISHED: a newer
        # run judged first has moved them on, and this older one must not
        # move them back (review of 2eb963781, R3).  One made while this run
        # was going on never blocks, or a warm session would stop
        # progressing (hartos-16, 11-07).
        since = run_finished if place.get('by') == 'goal_met' else run_started
        moved_since = (place.get('at') or 0) > since
        if met and moved_since:
            # Someone (a therapist, a parent) moved them after this run
            # STARTED: a verdict on that run never undoes the move, even
            # when the run finished after it (hartos-16, 11-07).
            logger.info("flow progress %s_%s: flow %s met, but moved since; place kept",
                        user_id, prompt_id, index)
        elif met:
            set_person_flow(user_id, prompt_id, flows, ran_as,
                            _next_after_met(user_id, prompt_id, flows, ran_as, index, ask),
                            'goal_met')
        judged.add(record['run'])
    if judged:
        _update(user_id, prompt_id, lambda d: d.update(unjudged=[
            r for r in (d.get('unjudged') or []) if r.get('run') not in judged]))
    logger.info("flow progress %s_%s: settled %d finished run(s) in %.1f s",
                user_id, prompt_id, len(judged), time.monotonic() - started)
    return current_flow(user_id, prompt_id, flows, persona) if persona else None
