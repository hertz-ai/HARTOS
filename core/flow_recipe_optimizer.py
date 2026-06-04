"""Recipe self-optimizer — closes the flywheel's Gate 4 (measure -> improve).

The measure half already runs: agent_daemon's remediation block calls
AgentBaselineService.validate_against_baseline() (the 5% per-action success-rate
regression rule) + compute_trend() for every goal every ~10 min, but on a
regression it ONLY re-snapshots — it never acts.  That's the dangling half of
the loop (#88): rollouts are captured + distilled into a per-recipe reward, a
regression is detected, and then nothing improves.

This module is the ACT half, built to add NO parallel path:

  * It does not invent a reward — the daemon reads the EXISTING signals
    (validate_against_baseline + compute_trend) and decides.
  * It does not invent an invalidation mechanism — it reuses the file-existence
    CREATE/REUSE split (hart_intelligence_entry.py:8647-8670): a flow with a
    missing {pid}_{flow}_recipe.json re-enters CREATE on its next dispatch and
    re-decomposes WITH the accumulated experience hints already applied
    (recipe_experience.build_experience_hints -> "Low success rate, consider
    alternatives").  So "retire the recipe" == "rename the file".
  * It mirrors core/flow_recipe_reconcile.py exactly: import-light (no autogen/
    Flask), atomic os.replace, single-source dir resolver, daemon-safe,
    unit-testable.

Safety is rename-not-delete + a sidecar recording the retired recipe's reward,
so a WORSE re-CREATE is rolled back to the proven-better original:

    measure (regress) -> archive (rename -> .optbak + sidecar) -> daemon
    re-CREATEs with experience hints -> fresh baseline -> daemon compares:
        new reward >= archived reward  -> accept (drop the .optbak)
        new reward <  archived reward  -> rollback (restore the .optbak)

The optimizer is a no-op until a regression is BOTH detected and corroborated;
it archives at most one recipe per flow at a time (the .optbak-present guard
blocks re-archiving and the documented re-CREATE churn hazard, #85).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger('flow_recipe_optimizer')

# Suffix for the retired recipe (the proven-better version kept for rollback).
_BAK_SUFFIX = '.optbak'
# Sidecar holding the retired recipe's reward + bookkeeping, next to the .optbak.
_META_SUFFIX = '.optbak.meta.json'


def _resolve_prompts_dir(prompts_dir: Optional[str]) -> str:
    if prompts_dir:
        return prompts_dir
    # SAME single-source resolver the pipeline / reconciler / daemon use.
    from core.platform_paths import get_recipe_prompts_dir
    return get_recipe_prompts_dir()


def _paths(prompts_dir: str, prompt_id, flow_id):
    base = os.path.join(prompts_dir, f'{prompt_id}_{flow_id}_recipe.json')
    return base, base + _BAK_SUFFIX, base + _META_SUFFIX


def recipe_exists(prompt_id, flow_id, prompts_dir: Optional[str] = None) -> bool:
    """True iff the live flow recipe is present (e.g. the daemon has finished
    re-CREATEing it after an archive)."""
    pdir = _resolve_prompts_dir(prompts_dir)
    recipe, _bak, _meta = _paths(pdir, prompt_id, flow_id)
    return os.path.exists(recipe)


def has_pending_optimization(prompt_id, flow_id, prompts_dir: Optional[str] = None) -> bool:
    """True iff this flow already has a retired recipe awaiting accept/rollback.
    The daemon uses this to (a) never re-archive a flow mid-optimization and
    (b) know to run the accept/rollback decision instead of a fresh archive."""
    pdir = _resolve_prompts_dir(prompts_dir)
    _, bak, _meta = _paths(pdir, prompt_id, flow_id)
    return os.path.exists(bak)


def archived_reward(prompt_id, flow_id, prompts_dir: Optional[str] = None) -> Optional[float]:
    """The reward of the retired (pre-re-CREATE) recipe, or None if not pending."""
    pdir = _resolve_prompts_dir(prompts_dir)
    _, _bak, meta = _paths(pdir, prompt_id, flow_id)
    try:
        with open(meta, 'r', encoding='utf-8') as f:
            return float(json.load(f).get('archived_reward'))
    except Exception:
        return None


def archive_recipe_for_reoptimization(prompt_id, flow_id, reward: float,
                                      prompts_dir: Optional[str] = None) -> bool:
    """Retire an underperforming flow recipe so the daemon re-CREATEs it.

    Renames {pid}_{flow}_recipe.json -> ...recipe.json.optbak (atomic) and writes
    a sidecar with the retired recipe's reward.  Returns True if it acted.

    No-ops (returns False) when: the recipe is missing (nothing to retire), or a
    .optbak already exists (a re-optimization is already in flight — never stack
    archives or you re-enter the #85 re-CREATE churn).
    """
    pdir = _resolve_prompts_dir(prompts_dir)
    recipe, bak, meta = _paths(pdir, prompt_id, flow_id)
    if not os.path.exists(recipe):
        return False
    if os.path.exists(bak):
        return False  # already optimizing this flow — anti-churn guard
    try:
        tmp = meta + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'archived_reward': float(reward),
                       'prompt_id': str(prompt_id), 'flow_id': int(flow_id)}, f)
        os.replace(tmp, meta)
        os.replace(recipe, bak)   # atomic retire; daemon re-CREATEs next tick
        logger.info('Retired underperforming recipe %s_%s (reward=%.4f) for '
                    're-CREATE; original kept at %s', prompt_id, flow_id, reward,
                    os.path.basename(bak))
        return True
    except Exception as e:
        logger.warning('archive %s_%s failed: %s', prompt_id, flow_id, e)
        # Best-effort cleanup so we don't leave a sidecar without a .optbak.
        try:
            if os.path.exists(meta) and not os.path.exists(bak):
                os.remove(meta)
        except Exception:
            pass
        return False


def accept_reoptimization(prompt_id, flow_id, prompts_dir: Optional[str] = None) -> bool:
    """Keep the re-CREATEd recipe (it's >= the retired one): drop the .optbak."""
    pdir = _resolve_prompts_dir(prompts_dir)
    _recipe, bak, meta = _paths(pdir, prompt_id, flow_id)
    acted = False
    for p in (bak, meta):
        try:
            if os.path.exists(p):
                os.remove(p)
                acted = True
        except Exception as e:
            logger.warning('accept cleanup %s failed: %s', os.path.basename(p), e)
    if acted:
        logger.info('Accepted re-optimized recipe %s_%s (dropped backup)',
                    prompt_id, flow_id)
    return acted


def rollback_recipe(prompt_id, flow_id, prompts_dir: Optional[str] = None) -> bool:
    """Restore the retired recipe (the re-CREATE was WORSE): .optbak -> recipe.

    Atomic restore; removes the sidecar.  Returns True if it restored.
    """
    pdir = _resolve_prompts_dir(prompts_dir)
    recipe, bak, meta = _paths(pdir, prompt_id, flow_id)
    if not os.path.exists(bak):
        return False
    try:
        os.replace(bak, recipe)   # atomic: the proven-better recipe is live again
        try:
            if os.path.exists(meta):
                os.remove(meta)
        except Exception:
            pass
        logger.info('Rolled back recipe %s_%s — re-CREATE regressed vs the '
                    'retired version; restored the proven-better recipe',
                    prompt_id, flow_id)
        return True
    except Exception as e:
        logger.warning('rollback %s_%s failed: %s', prompt_id, flow_id, e)
        return False
