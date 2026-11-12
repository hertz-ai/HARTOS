"""Which files in the prompts dir are agents, and which are plans awaiting
review: ONE naming contract for the writer and every reader.

The CREATE path (``hart_intelligence_entry._autonomous_gather_info``) stages
each proposed plan in the prompts dir, beside the agents:

    {prompt_id}.proposed.r{n}.json   one per auto-review round
    {prompt_id}.proposed.json        the plan handed to a peer reviewer

Those names come from ``proposed_plan_filename`` below, and
``is_proposed_plan_filename`` recognises exactly what it writes.  The agent
listings (GET /prompts, GET /prompts/public) read through
``local_agent_prompts``, so a staged plan is never listed as an agent.
Measured 2026-09-26: before this, the listings' ``'_' not in fname`` filter
let the plans through and the owner's agent list grew from 20 to 26
(``79991757345.proposed.r0`` and the like); the live prompts dir held 503
plan files beside 852 agents.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

PROPOSED_PLAN_MARKER = '.proposed'
_ROUND_PREFIX = '.r'
_PROPOSED_PLAN_RE = re.compile(
    r'.+' + re.escape(PROPOSED_PLAN_MARKER)
    + r'(?:' + re.escape(_ROUND_PREFIX) + r'\d+)?\.json')


def proposed_plan_filename(prompt_id, review_round=None):
    """The file name a proposed plan for ``prompt_id`` is staged under.

    ``review_round`` names one auto-review round's plan; None names the plan
    handed to a peer reviewer.
    """
    name = f'{prompt_id}{PROPOSED_PLAN_MARKER}'
    if review_round is not None:
        name += f'{_ROUND_PREFIX}{int(review_round)}'
    return name + '.json'


def staged_plan(prompts_dir, prompt_id):
    """The plan staged for review of ``prompt_id`` ({prompt_id}.proposed.json),
    as the CREATE path wrote it, or None.  The one reader of that file."""
    path = os.path.join(prompts_dir, proposed_plan_filename(prompt_id))
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning('staged plan %s could not be read: %s', path, e)
        return None
    return data if isinstance(data, dict) else None


# A person's plan that waits on their approval card: one per creating person.
# The person paths mint a new prompt id on every create, so a hold kept only
# under the plan's id would never be found again (hartos-16, 11-07).  Keyed by
# the user who CREATES, never by whom the card goes to.
def _hold_path(user_id):
    from core.platform_paths import get_data_dir
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', str(user_id))
    return os.path.join(get_data_dir(), 'plan_holds', f'{safe}.json')


def hold_plan(user_id, prompt_id):
    """Keep that ``user_id``'s plan ``prompt_id`` waits on their card."""
    from core.file_cache import atomic_json_write
    path = _hold_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_json_write(path, {'prompt_id': prompt_id})


def held_plan(user_id):
    """The prompt id of ``user_id``'s plan waiting on their card (as it was
    given: the creating path's own type), or None."""
    try:
        with open(_hold_path(user_id), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning('plan hold of %s could not be read: %s', user_id, e)
        return None
    return data.get('prompt_id') if isinstance(data, dict) and data.get('prompt_id') else None


def clear_held_plan(user_id):
    try:
        os.remove(_hold_path(user_id))
    except FileNotFoundError:
        pass


def is_proposed_plan_filename(fname):
    """True for exactly the names ``proposed_plan_filename`` makes."""
    return _PROPOSED_PLAN_RE.fullmatch(fname) is not None


def local_agent_prompts(prompts_dir):
    """``[(prompt_id, record)]`` for every agent record in ``prompts_dir``.

    An agent record is ``{prompt_id}.json`` with no second name component
    (``_`` joins the flow / action / recipe / personality files) and is not
    a staged plan.  Unreadable or non-object records are skipped, as the
    listings always did, with a debug line naming the file.
    """
    if not os.path.isdir(prompts_dir):
        return []
    out = []
    for fname in os.listdir(prompts_dir):
        if (not fname.endswith('.json') or '_' in fname
                or is_proposed_plan_filename(fname)):
            continue
        try:
            with open(os.path.join(prompts_dir, fname), 'r') as f:
                data = json.load(f)
        except Exception as e:
            logger.debug('local_agent_prompts: skipped %s: %s', fname, e)
            continue
        if not isinstance(data, dict):
            logger.debug('local_agent_prompts: skipped %s: not an object',
                         fname)
            continue
        out.append((fname[:-len('.json')], data))
    return out
