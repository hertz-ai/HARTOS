"""Orphaned flow-recipe reconciler — closes the flywheel's Gate-1 leak.

Why this exists
---------------
A CREATE run saves one ``{pid}_{flow}_{i}.json`` per action the moment that
action reaches ``status:done``.  The flow-level ``{pid}_{flow}_recipe.json``
— the artifact the daemon's CREATE/REUSE split keys on
(``agent_daemon`` line ~817) — is written ONLY by
``create_recipe._save_flow_recipe``, which fires after EVERY action in the
flow reaches ``TERMINATED``.

When the local model completes all the actions but never emits the final
flow-level ``status:done`` (so the actions sit at COMPLETED and never advance
to TERMINATED — the dual-FSM gap, task #56), the flow recipe is never written.
The goal therefore stays in the CREATE queue and re-CREATEs on every daemon
tick **even though every action recipe it needs already exists on disk**.
On the live build that is 51 flows stranded (95 action recipes, only 13 flow
recipes) — the bulk of the flywheel's ~9% completion rate (#85).

What this does
--------------
Reassembles the missing flow recipe purely from the on-disk action recipes —
no model call, no synthesis, no invented content.  The flow recipe is, by the
pipeline's own definition (``set_individual_recipes`` + the saved files),
exactly ``{"status": "completed", "actions": [<action recipe>, ...]}`` in
action-id order, so this reproduces that shape byte-for-byte.

Safe by construction
--------------------
A flow recipe is written ONLY when the prompt config's planned action count
for that flow (``len(config['flows'][flow]['actions'])``) is matched by
**contiguous** action recipes ``1..N``, each in a terminal status
(``done``/``completed``).  A flow with a gap (missing action 1) or a short
count (fewer recipes than planned) is LEFT untouched for CREATE to finish —
never truncated into a partial recipe that REUSE would replay incompletely
and that would then poison the benchmark validator.  Existing flow recipes are
never overwritten.

This module is intentionally free of autogen/Flask imports so it can run in
the daemon thread and be unit-tested without the heavy CREATE pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger('flow_recipe_reconcile')

# Action recipe file: {prompt_id}_{flow}_{action}.json (three integer parts).
# Flow recipe (NOT an action) is {prompt_id}_{flow}_recipe.json and the prompt
# config is {prompt_id}.json — neither matches this three-integer pattern.
_ACTION_RECIPE_RE = re.compile(r'^(\d+)_(\d+)_(\d+)\.json$')

# An action recipe counts as a completed building block only in these states.
_TERMINAL_STATUSES = ('done', 'completed')


def _resolve_prompts_dir(prompts_dir: str | None) -> str:
    if prompts_dir:
        return prompts_dir
    # SINGLE SOURCE: the same resolver helper.PROMPTS_DIR / the pipeline /
    # the daemon CREATE-REUSE split all use — never a second path formula.
    from core.platform_paths import get_recipe_prompts_dir
    return get_recipe_prompts_dir()


def _load_json(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _atomic_write_json(path: str, obj) -> None:
    """Temp-write + os.replace — atomic on the same filesystem on Windows and
    POSIX, mirroring create_recipe.create_final_recipe_for_current_flow so a
    concurrent prompts_backup snapshot can never see a half-written recipe."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _planned_action_count(config: dict, flow_idx: int) -> int:
    """How many actions the config says this flow has (0 if unknowable)."""
    flows = config.get('flows') if isinstance(config, dict) else None
    if not isinstance(flows, list) or flow_idx >= len(flows):
        return 0
    flow = flows[flow_idx]
    actions = flow.get('actions') if isinstance(flow, dict) else None
    return len(actions) if isinstance(actions, list) else 0


def _collect_complete_actions(prompts_dir: str, pid: str, flow_idx: int,
                              planned_n: int):
    """Return the assembled action-recipe list IFF actions 1..planned_n all
    exist on disk in a terminal status; otherwise None (incomplete → skip)."""
    actions = []
    for i in range(1, planned_n + 1):
        ap = os.path.join(prompts_dir, f'{pid}_{flow_idx}_{i}.json')
        if not os.path.exists(ap):
            return None  # gap or short count — leave for CREATE to finish
        try:
            adata = _load_json(ap)
        except Exception as e:
            logger.debug('reconcile: unreadable action recipe %s: %s', ap, e)
            return None
        status = str((adata or {}).get('status', '')).lower()
        if status not in _TERMINAL_STATUSES:
            return None  # an action is still mid-flight — not safe to assemble
        actions.append(adata)
    return actions


def _flows_needing_reconcile(prompts_dir: str, prompt_id) -> dict:
    """Build {pid -> set(flow_idx)} to examine.

    Targeted (prompt_id given) — just that prompt's configured flows.
    Sweep (None) — every prompt that has at least one action recipe on disk.
    """
    work: dict[str, set] = {}
    if prompt_id is not None:
        pid = str(prompt_id)
        try:
            config = _load_json(os.path.join(prompts_dir, f'{pid}.json'))
            n_flows = len(config.get('flows') or [])
        except Exception:
            n_flows = 0
        work[pid] = set(range(n_flows))
        return work
    try:
        names = os.listdir(prompts_dir)
    except Exception:
        names = []
    for nm in names:
        m = _ACTION_RECIPE_RE.match(nm)
        if m:
            work.setdefault(m.group(1), set()).add(int(m.group(2)))
    return work


def reconcile_orphaned_flow_recipes(prompt_id=None, prompts_dir: str | None = None):
    """Write any flow recipe whose actions are all present+terminal but whose
    flow-level recipe was never saved.

    Args:
        prompt_id: reconcile only this prompt's flows (cheap, targeted).
            None -> sweep every prompt with orphaned action recipes on disk.
        prompts_dir: override the recipe directory (tests); defaults to the
            canonical get_recipe_prompts_dir().

    Returns:
        list[tuple[str, int]] of (prompt_id, flow_idx) recipes written.
    """
    pdir = _resolve_prompts_dir(prompts_dir)
    # The optimizer retires an underperforming recipe to {..}_recipe.json.optbak
    # so the daemon re-CREATEs it WITH experience hints (Gate-4). Honour that
    # marker: a flow mid-optimization must NOT be reconciled back from its
    # unchanged action recipes (#111), or the archive instantly no-ops and the
    # measure->improve loop never fires. Reuse the optimizer's own check so the
    # .optbak suffix stays single-sourced (no parallel literal).
    from core.flow_recipe_optimizer import has_pending_optimization
    reconciled: list[tuple[str, int]] = []

    for pid, flow_idxs in _flows_needing_reconcile(pdir, prompt_id).items():
        try:
            config = _load_json(os.path.join(pdir, f'{pid}.json'))
        except Exception:
            continue  # no/broken config — cannot verify completeness, skip
        for flow_idx in sorted(flow_idxs):
            recipe_path = os.path.join(pdir, f'{pid}_{flow_idx}_recipe.json')
            if os.path.exists(recipe_path):
                continue  # already saved — never overwrite
            if has_pending_optimization(pid, flow_idx, prompts_dir=pdir):
                continue  # optimizer archived this flow (#111) — don't resurrect
            planned_n = _planned_action_count(config, flow_idx)
            if planned_n <= 0:
                continue  # unknowable count — refuse to guess
            actions = _collect_complete_actions(pdir, pid, flow_idx, planned_n)
            if actions is None or len(actions) != planned_n:
                continue  # incomplete — leave for CREATE
            try:
                _atomic_write_json(recipe_path,
                                   {'status': 'completed', 'actions': actions})
                reconciled.append((pid, flow_idx))
            except Exception as e:
                logger.warning('reconcile: write failed for %s flow %s: %s',
                               pid, flow_idx, e)

    if reconciled:
        logger.info('Recovered %d orphaned flow recipe(s): %s',
                    len(reconciled), reconciled)
    return reconciled
