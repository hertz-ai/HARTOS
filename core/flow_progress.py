"""Where each person is in an agent's flows, kept across restarts.

Owner, 2026-11-03: a person moves to their next flow "when its goal is met".
Owner rulings, 2026-11-04:
- When the goal of a persona's LAST flow is met, the agent goes back to the
  weakest earlier flow, per the progress record.
- A flow whose goal cannot be checked moves after one finished run.
- The parent and the therapist who joined the agent move the child from
  their own chat.

Nothing kept this before. The persona a person picked lived only in 2 h
in-memory caches (core.persona_registry), so after two idle hours or a
restart get_role read 'user' and a returning parent ran the child's first
flow. A flow pointer did not exist at all, so REUSE always ran a persona's
first flow (get_flow_number, "FIRST match").

One JSON file per person per agent under <data dir>/flow_progress, named by
core.agent_tools.person_store_id(f"{user_id}_{prompt_id}"). The fields:
- persona: written only by set_persona (the person's own pick, update_persona).
- flow, by, moved_at: written only by set_person_flow.
- unjudged: written by record_flow_finished, emptied by settle.
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
from typing import Callable, Optional

logger = logging.getLogger(__name__)

VERDICTS_KEPT = 10       # per flow: what the weakest-flow question reads (owner, 11-04)
UNJUDGED_KEPT = 5
EVIDENCE_KEPT = 4000     # characters of one finished run's record

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
    "Recorded in the person's last finished run of it:\n{evidence}\n\n"
    "By this record alone, was the goal met? Answer with the JSON object only; "
    "'evidence' quotes the part of the record that decides it.")
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
    """This person's record for this agent; {} when there is none or it
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


def _same(a, b) -> bool:
    return str(a or '').strip().lower() == str(b or '').strip().lower()


def persona_flows(flows, persona) -> list:
    """The indexes of ``flows`` that belong to ``persona``, in order."""
    return [i for i, f in enumerate(flows or [])
            if isinstance(f, dict) and persona and _same(f.get('persona'), persona)]


def stored_persona(user_id, prompt_id) -> Optional[str]:
    persona = read(user_id, prompt_id).get('persona')
    return persona if isinstance(persona, str) and persona.strip() else None


def set_persona(user_id, prompt_id, persona) -> None:
    """The persona this person picked for this agent (update_persona only:
    a registration of the agent's whole persona list is nobody's pick)."""
    _update(user_id, prompt_id, lambda d: d.update(persona=str(persona)))


def current_flow(user_id, prompt_id, flows, persona) -> Optional[int]:
    """The stored flow when it is one of ``persona``'s flows, else None (the
    builders then take the persona's first flow, as before)."""
    index = read(user_id, prompt_id).get('flow')
    return index if isinstance(index, int) and index in persona_flows(flows, persona) else None


def set_person_flow(user_id, prompt_id, flows, persona, flow_index, by) -> bool:
    """The one writer of the pointer: ``by`` is 'goal_met' or the id of the
    person who moved them. A flow of another persona is refused."""
    if flow_index not in persona_flows(flows, persona):
        logger.warning("flow progress %s_%s: flow %r is not %r's; not moved",
                       user_id, prompt_id, flow_index, persona)
        return False
    _update(user_id, prompt_id, lambda d: d.update(
        flow=int(flow_index), by=str(by), moved_at=time.time()))
    logger.info("flow progress %s_%s: %s now on flow %s (by %s)",
                user_id, prompt_id, persona, flow_index, by)
    return True


def record_flow_finished(user_id, prompt_id, flow_index, run_id, evidence='') -> None:
    """A person's run finished every action of ``flow_index``, with what that
    run recorded (``evidence``: each step's verified result and the person's
    answers, taken while the run's ledger is still live). Kept on disk,
    unjudged, until settle runs at their next run's start, so a restart in
    between loses nothing and the reply that finished the flow never waits
    on the judge."""
    def change(d):
        records = [r for r in (d.get('unjudged') or []) if isinstance(r, dict)]
        records.append({'flow': int(flow_index), 'run': str(run_id), 'at': time.time(),
                        'evidence': str(evidence or '')[:EVIDENCE_KEPT]})
        d['unjudged'] = records[-UNJUDGED_KEPT:]
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


def settle(user_id, prompt_id, flows, persona, ask: Optional[Callable] = None) -> Optional[int]:
    """At the start of a person's run: judge the flows they finished since
    their last run, move them through set_person_flow, and return the flow
    they are on (None: no pointer, the builders' first match stands). Asks
    the model only when a finished run waits to be judged, so most turns pay
    nothing. A record the model could not judge stays for the next run."""
    pending = [r for r in (read(user_id, prompt_id).get('unjudged') or [])
               if isinstance(r, dict) and isinstance(r.get('flow'), int)]
    if not pending:
        return current_flow(user_id, prompt_id, flows, persona)
    if ask is None:
        from core.llm_json import ask_model_json as ask
    started = time.monotonic()
    judged = set()
    for record in pending:
        index = record['flow']
        if index not in persona_flows(flows, persona):
            judged.add(record['run'])
            continue
        checkable = _checkable(user_id, prompt_id, flows, index, ask)
        if checkable is None:
            continue
        if checkable:
            goal = str(flows[index].get('sub_goal') or '').strip()
            reply = ask(MET_PROMPT.format(goal=goal, evidence=record.get('evidence') or '(nothing recorded)'),
                        _MET_SCHEMA, 'flow_goal_met')
            if reply is None or not isinstance(reply.get('met'), bool):
                continue
            met, evidence = reply['met'], str(reply.get('evidence') or '')[:300]
            if met and not _quoted_in(evidence, record.get('evidence')):
                # A "met" must quote the record it was given; a quote the
                # record does not hold is the model's own words, and a small
                # model's made-up quote must not move a child on unseen
                # (hartos-7a, 11-06).
                logger.info("flow progress %s_%s: flow %s 'met' quote not in the record; counted not met",
                            user_id, prompt_id, index)
                met, evidence = False, f'quote not in the record: {evidence[:200]}'
        else:
            met, evidence = True, 'goal not checkable: moves after one finished run (owner, 11-04)'

        def keep(d, i=index, m=met, e=evidence):
            history = d.setdefault('verdicts', {}).setdefault(str(i), [])
            history.append({'met': m, 'evidence': e, 'run': record['run'], 'at': time.time()})
            d['verdicts'][str(i)] = history[-VERDICTS_KEPT:]
        _update(user_id, prompt_id, keep)
        logger.info("flow progress %s_%s: flow %s (%s) met=%s: %s", user_id, prompt_id, index,
                    _flow_name(flows, index), met, evidence[:120])
        moved_since = (read(user_id, prompt_id).get('moved_at') or 0) > (record.get('at') or 0)
        if met and moved_since:
            # Someone (a therapist, a parent) moved them after this run
            # finished: a verdict on the older run never undoes that move.
            logger.info("flow progress %s_%s: flow %s met, but moved since; place kept",
                        user_id, prompt_id, index)
        elif met:
            set_person_flow(user_id, prompt_id, flows, persona,
                            _next_after_met(user_id, prompt_id, flows, persona, index, ask),
                            'goal_met')
        judged.add(record['run'])
    if judged:
        _update(user_id, prompt_id, lambda d: d.update(unjudged=[
            r for r in (d.get('unjudged') or []) if r.get('run') not in judged]))
    logger.info("flow progress %s_%s: settled %d finished run(s) in %.1f s",
                user_id, prompt_id, len(judged), time.monotonic() - started)
    return current_flow(user_id, prompt_id, flows, persona)
