"""
GAIA dataset loader — adapter between the public `gaia-benchmark/GAIA`
HuggingFace dataset and hive_benchmark_prover's problem contract.

GAIA (General AI Assistants, Mialon et al. 2023) ships 466 real-world
agent tasks across 3 difficulty levels.  Public scores:
  - Best human (2023):      92.0%
  - GPT-4 + plugins:        15.0%
  - Claude 3 Opus:          17.0%
  - GPT-4o (Mar 2024 card): 32.0%
Frontier single-model scores still sit below 65% — the clean signal
that sum-of-many-agents should beat any-single-model on real agentic
work is here, not in MMLU.

Design:
- DO NOT auto-download at import time.  The dataset is gated + large;
  loading it silently on boot would surprise users.  Load is explicit
  via load_gaia_problems().
- Three-layer fallback:
    1. Local cached JSON at ~/.hevolve/benchmarks/gaia_mini.json
    2. HuggingFace `datasets` library (if installed + auth present)
    3. Synthetic stubs (hive_benchmark_prover already provides these
       when load returns empty)
- Level filter + limit so the caller can sample without loading the
  full 466-task set.  Our mini rotation pulls 30 problems per run.

Each returned problem is a dict with fields:
    id          unique problem id
    type        'agent' (to match BUILTIN_BENCHMARKS['gaia_mini'])
    level       1 | 2 | 3
    prompt      the Question field from GAIA
    answer      the Final answer field (for scoring — NOT shown to node)
    tools       hint about expected tools (browser, code, search)
    has_file    bool — problem comes with an attachment
"""
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger('hevolve_social')


_CACHE_PATH_ENV = 'HEVOLVE_GAIA_CACHE'


def _resolve_cache_path() -> str:
    override = os.environ.get(_CACHE_PATH_ENV, '')
    if override:
        return override
    root = os.path.join(
        os.path.expanduser('~'), '.hevolve', 'benchmarks',
    )
    return os.path.join(root, 'gaia_mini.json')


def _try_cache(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get('problems'), list):
            return data['problems']
    except Exception as exc:
        logger.debug(f'[gaia] cache read failed {path}: {exc}')
    return []


def _try_huggingface(levels: List[int], limit: int) -> List[dict]:
    """Attempt to load GAIA via the HuggingFace datasets library.

    Gated dataset — requires HF token + accepted dataset terms.  We
    do NOT prompt.  Silent fallback if any step fails; the caller
    receives an empty list and hive_benchmark_prover uses stubs.
    """
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        return []
    try:
        # The GAIA dataset has a 'validation' split with labeled answers
        # across all 3 levels.  We read only the validation split so
        # we never train on the gated test set.
        ds = load_dataset('gaia-benchmark/GAIA', '2023_all', split='validation')
    except Exception as exc:
        logger.debug(f'[gaia] HF load failed: {exc}')
        return []

    out: List[dict] = []
    level_set = set(int(l) for l in levels)
    # Iterate a limited window — GAIA validation has ~165 items
    for i, row in enumerate(ds):
        lvl = int(row.get('Level', 0) or 0)
        if lvl not in level_set:
            continue
        question = str(row.get('Question', '') or '').strip()
        if not question:
            continue
        out.append({
            'id': f'gaia_L{lvl}_{row.get("task_id", i)}',
            'type': 'agent',
            'level': lvl,
            'prompt': question,
            'answer': str(row.get('Final answer', '') or '').strip(),
            'tools': row.get('Annotator Metadata', {}).get('Tools', '')
                if isinstance(row.get('Annotator Metadata'), dict) else '',
            'has_file': bool(row.get('file_name')),
            'dataset': 'gaia-benchmark/GAIA',
        })
        if len(out) >= limit:
            break
    return out


def load_gaia_problems(
    levels: Optional[List[int]] = None,
    limit: int = 30,
) -> List[dict]:
    """Return up to `limit` GAIA validation problems filtered by level.

    Resolution order:
        1. `HEVOLVE_GAIA_CACHE` env path (raw JSON list or {"problems": [...]}).
        2. Default user cache at `~/.hevolve/benchmarks/gaia_mini.json`.
        3. HuggingFace `datasets` library (gated — needs HF auth).
    Returns `[]` when none of the above are available; caller is
    expected to generate synthetic stubs.
    """
    levels = levels or [1, 2, 3]
    limit = max(1, int(limit or 30))

    cache_path = _resolve_cache_path()
    cached = _try_cache(cache_path)
    if cached:
        level_set = set(int(l) for l in levels)
        filtered = [p for p in cached
                    if int(p.get('level', 0) or 0) in level_set]
        return filtered[:limit] if filtered else cached[:limit]

    hf = _try_huggingface(levels, limit)
    if hf:
        return hf

    return []


def save_cache(problems: List[dict]) -> bool:
    """Persist a loaded problem set so subsequent runs skip the HF call.

    Called by a one-off prefetch script; NOT invoked automatically so
    we never surprise the user with a large download."""
    path = _resolve_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'problems': problems}, fh, indent=2)
        return True
    except Exception as exc:
        logger.warning(f'[gaia] cache write failed: {exc}')
        return False
