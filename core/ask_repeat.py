"""Whether a step that asks a person asks again on their next run.

Owner rulings this module carries:
- 2026-11-16: "to a kid asking repeatedly is a feature to a parent it's spam
  ... the framework shd be supporting both mechanisms ... controlled by the
  flow action in agents".  The repeat point belongs to the agent, decided
  when it is created and stored with its recipe; REUSE has no loop rule of
  its own.
- 2026-11-19: "if the new request answers the old question or agent got the
  response by some other means do not ask the question again ... no false
  positives or false negatives".

Before this, a recipe step that asks the person (its
can_perform_without_user_input says 'no') asked on every run.  The speech
agent's Parent flow starts with "ask the parent for home Tamil names and
photos", so each time the parent came back the agent asked for the names
already saved.

A recipe action may carry ``repeat``: "once" or "every_run".  CREATE's recipe
templates offer the field with the one line REPEAT_FIELD_MEANING, and the
model picks it as it authors the step, as it picks key names (owner
2026-11-03, "it's up to the agent").  No field, or any value other than
"once", is every_run: the step asks, exactly as before.

A "once" step is already known for a person when every key its following
save step names (save_step_keys, read from the authored recipe) holds an
answer in that person's memory as get_data_by_key reads it (held_answers
over core.agent_tools.memory_view, counting the placeholders in
is_placeholder as absent); there is no store or run record of its own.
REUSE then skips the question, and the save step runs with the person's
words and the known values, so a correction is still saved
(hartos.reuse_recipe._reuse_pass_known_ask).

Agents built before the field: a back-fill classifies their ask steps with
the node's own model into a SIDECAR store, one JSON file per agent under
<data dir>/ask_repeat, keyed by flow index and 1-based action id.  It never
writes an agent's recipe files.  The model's "once" is the only "once": an
"uncertain" answer, any other answer, or no answer is every_run, decided here
in code.  REUSE reads the sidecar only while HEVOLVE_ASK_REPEAT_SIDECAR is
on, and it is off by default: the classifier is measured on a held-out set
first.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

REPEAT_FIELD = 'repeat'
ONCE = 'once'
EVERY_RUN = 'every_run'

#: What CREATE's recipe templates say the field means: the field and its
#: meaning only, never which value to pick.
REPEAT_FIELD_MEANING = (
    "only for a step that asks the person something: once (a later session "
    "does not ask it again while the answer is saved) or every_run (every "
    "session asks it)")

#: The memory tool whose calls name a step's output keys (core.agent_tools).
SAVE_TOOL = 'save_data_in_memory'
#: How that tool's answer to a save that held opens ("Saved at <key>: ..."):
#: such text saved as a value is no answer.
SAVED_AT = 'Saved at '

#: The config gate of the sidecar.  Off unless set truthy.
SIDECAR_FLAG = 'HEVOLVE_ASK_REPEAT_SIDECAR'


def authored_repeat(action) -> Optional[str]:
    """The ``repeat`` an action's author gave it: ONCE when the field says
    "once" (case and surrounding space aside), EVERY_RUN for any other
    value, None when the action has no such field."""
    if not isinstance(action, dict):
        return None
    value = action.get(REPEAT_FIELD)
    if value is None or str(value).strip() == '':
        return None
    return ONCE if str(value).strip().lower() == ONCE else EVERY_RUN


# -- the step's output keys -------------------------------------------------

_KEY_IN_TEXT = re.compile(r'''["']key["']\s*:\s*["']([^"']+)["']''')


def _call_key(text) -> Optional[str]:
    """The ``key`` a saved call "tool({...})" names, from its JSON arguments,
    else from a "key": "..." pair in the text (a trace-banked call is cut at
    400 characters, after its key); None when neither is there."""
    text = str(text or '')
    start, end = text.find('('), text.rfind(')')
    if 0 <= start < end:
        try:
            args = json.loads(text[start + 1:end])
        except ValueError:
            args = None
        if isinstance(args, dict):
            key = str(args.get('key') or '').strip()
            return key or None
    m = _KEY_IN_TEXT.search(text)
    return (m.group(1).strip() or None) if m else None


def save_step_keys(action) -> Optional[list]:
    """The memory keys ``action``'s save calls name, in order, from its
    authored recipe steps (each step's tool_name, as CREATE writes it); None
    when it saves nothing, or when a save's key cannot be read or is still a
    template (``teach.<name>``): then what the ask collects is not known."""
    from core.agent_tools import unfilled_key_placeholder
    from core.constants import tool_name_in
    steps = action.get('recipe') if isinstance(action, dict) else None
    if not isinstance(steps, list):
        return None
    keys = []
    for step in steps:
        if not isinstance(step, dict) or not tool_name_in(
                str(step.get('tool_name') or '').strip() or None, (SAVE_TOOL,)):
            continue
        key = _call_key(step.get('steps'))
        if not key or unfilled_key_placeholder(key):
            return None
        if key not in keys:
            keys.append(key)
    return keys or None


# -- what counts as an answer ---------------------------------------------

#: Saved values that say nothing was answered, compared folded (_fold).
PLACEHOLDER_WORDS = frozenset({
    '', '[]', '{}', 'null', 'unknown', 'n/a', 'na', 'tbd', 'pending', 'later',
    'skip', 'not provided', '-', '?', "i don't know",
    'தெரியாது', 'அப்புறம்',
})
#: Placeholders only for a key that holds a name or an identity: "none" is a
#: real answer to "any allergies?".
IDENTITY_PLACEHOLDER_WORDS = frozenset({'none'})

_TEMPLATE_VALUES = (
    re.compile(r'^<[^<>]+>$'),
    re.compile(r"^\{\s*[\w .'-]*\}$"),
    re.compile(r"^(the )?(user|person|child|kid|parent|mother|father|student|customer)'s name$"),
)


def _fold(text) -> str:
    folded = ' '.join(str(text).replace('’', "'").casefold().split())
    return folded.strip(' .!') or folded


def is_identity_key(key) -> bool:
    """A key that holds a name or an identity: one of its words is or ends in
    'name', or is 'identity'."""
    words = re.split(r'[^0-9a-z]+', re.sub(r'([a-z])([A-Z])', r'\1_\2', str(key or '')).lower())
    return any(w == 'identity' or w.endswith('name') or w.endswith('names') for w in words if w)


def is_placeholder(value, key='', asked='') -> bool:
    """True when a saved ``value`` is no answer: missing, empty, a
    placeholder word (PLACEHOLDER_WORDS; IDENTITY_PLACEHOLDER_WORDS for an
    identity key), a template left unfilled, the ask's own text ``asked``, or
    a tool's not-found, saved or failure text.  A list or dict is no answer
    when every item in it is none."""
    if value is None:
        return True
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return False
    if isinstance(value, dict):
        return all(is_placeholder(v, key, asked) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return all(is_placeholder(v, key, asked) for v in value)
    from core.agent_tools import KEY_NOT_FOUND
    from core.constants import tool_reply_failed
    text = str(value)
    folded = _fold(text)
    if folded in PLACEHOLDER_WORDS:
        return True
    if folded in IDENTITY_PLACEHOLDER_WORDS and is_identity_key(key):
        return True
    if any(p.match(folded) for p in _TEMPLATE_VALUES):
        return True
    if str(asked or '').strip() and folded == _fold(asked):
        return True
    stripped = text.strip()
    return (stripped.startswith(KEY_NOT_FOUND) or 'key not found' in folded
            or stripped.startswith(SAVED_AT) or tool_reply_failed(stripped))


def held_answers(view, keys: Iterable, asked='') -> Optional[dict]:
    """``{key: value}`` when ``view``, ONE person's memory for one agent as
    get_data_by_key reads it (core.agent_tools.memory_view: their own entries
    over what the agent holds for everyone, never another person's), holds an
    answer at every key, a dotted key read as nested the way that tool reads
    it; None when any key is missing or holds a placeholder (is_placeholder),
    or there are no keys."""
    keys = list(keys or [])
    if not keys or not isinstance(view, dict):
        return None
    out = {}
    for key in keys:
        node = view
        for part in str(key).split('.'):
            node = node.get(part) if isinstance(node, dict) else None
        if is_placeholder(node, key, asked):
            return None
        out[key] = node
    return out


# -- the sidecar back-fill (gated off) --------------------------------------

def sidecar_enabled() -> bool:
    from core.config_cache import env_flag
    return env_flag(SIDECAR_FLAG, False)


def _sidecar_path(prompt_id) -> str:
    from core.agent_tools import person_store_id
    from core.platform_paths import get_data_dir
    return os.path.join(get_data_dir(), 'ask_repeat', person_store_id(str(prompt_id)) + '.json')


def _sidecar_read(prompt_id) -> dict:
    try:
        with open(_sidecar_path(prompt_id), encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("ask repeat sidecar for %s unreadable (%s)", prompt_id, e)
        return {}
    return data if isinstance(data, dict) else {}


def sidecar_repeat(prompt_id, flow, action_id) -> Optional[str]:
    """The back-fill's ONCE or EVERY_RUN for this agent's step, or None when
    it has none or the sidecar is off (SIDECAR_FLAG): while off nothing is
    read."""
    if not sidecar_enabled():
        return None
    entry = ((_sidecar_read(prompt_id).get(str(int(flow))) or {})
             .get(str(int(action_id))) or {})
    value = entry.get(REPEAT_FIELD) if isinstance(entry, dict) else None
    return value if value in (ONCE, EVERY_RUN) else None


CLASSIFY_PROMPT = (
    "An agent works through a flow of steps with a person. The flow's goal "
    "is:\n{goal}\n\nOne step of it asks the person something:\n{step}\n\n"
    "Does this step ask for something that, once answered, stays true for "
    "this person in their later sessions ('once'), or for something new or "
    "to be done again in each session ('every_run')? If the step does not say "
    "enough to tell, answer 'uncertain'. Answer with the JSON object only; "
    "'reason' is one short line.")
#: 'reason' before 'repeat': the server writes the fields in this order, so
#: the answer follows the reason instead of being committed before it (the
#: 4B's first order gave reasons that argued the other answer).
_CLASSIFY_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['reason', 'repeat'],
    'properties': {'reason': {'type': 'string'},
                   'repeat': {'type': 'string', 'enum': [ONCE, EVERY_RUN, 'uncertain']}}}


def classify_step(step, goal, ask: Optional[Callable] = None) -> dict:
    """The node's model's judgment of one ask step:
    ``{'repeat': ONCE or EVERY_RUN, 'answer': its raw answer or None,
    'reason': its one line}``.  Only an answer of "once" is ONCE."""
    if ask is None:
        from core.llm_json import ask_model_json as ask
    reply = ask(CLASSIFY_PROMPT.format(goal=str(goal or '').strip() or '(none given)',
                                       step=str(step or '').strip()),
                _CLASSIFY_SCHEMA, 'ask_repeat')
    answer = reply.get('repeat') if isinstance(reply, dict) else None
    reason = str(reply.get('reason') or '')[:300] if isinstance(reply, dict) else ''
    return {'repeat': ONCE if answer == ONCE else EVERY_RUN,
            'answer': answer if isinstance(answer, str) else None,
            'reason': reason}


def backfill(prompt_id, flows, flow_recipes, ask: Optional[Callable] = None) -> int:
    """Classify every ask step of one agent that has no ``repeat`` into the
    sidecar.  ``flows`` is the agent's prompt file's 'flows' (each flow's
    sub_goal), ``flow_recipes`` maps a flow index to its flow recipe's
    actions, both read by the caller and never written here.  A step the
    sidecar already holds is not asked again.  Returns how many steps were
    classified.  Nothing calls this on its own."""
    from core.constants import autonomy_needs_user
    from core.file_cache import atomic_json_write
    data = _sidecar_read(prompt_id)
    done = 0
    for flow, actions in sorted((flow_recipes or {}).items()):
        goal = ((flows[int(flow)] or {}).get('sub_goal')
                if 0 <= int(flow) < len(flows or []) else '') or ''
        for index, action in enumerate(actions or [], start=1):
            if (not isinstance(action, dict) or authored_repeat(action) is not None
                    or not autonomy_needs_user(action.get('can_perform_without_user_input'))):
                continue
            kept = data.setdefault(str(int(flow)), {})
            if str(index) in kept:
                continue
            judged = classify_step(action.get('action'), goal, ask)
            kept[str(index)] = dict(judged, at=time.time())
            done += 1
    if done:
        atomic_json_write(_sidecar_path(prompt_id), data)
    return done
