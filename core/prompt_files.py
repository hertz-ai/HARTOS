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
