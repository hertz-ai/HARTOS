"""
Pure helpers for the VLM benchmark's --gate / --bump-baseline modes.

Lives outside vlm_grounding_benchmark.py so the helpers can be
imported without triggering ImageGrab.grab() + a full benchmark run.
The benchmark file imports from here; the unit tests import from
here too — single source of truth for "what counts as a regression".

§0 of memory/vlm_best_of_all_worlds_plan.md depends on this.
"""

from collections import defaultdict
from typing import Dict, Iterable, List, Tuple


def summarize_bucket(items: Iterable[dict], key: str) -> Dict[str, dict]:
    """Group items by item[key] → {bucket: {avg_err, median_err,
    exact_count, good_count, fail_count, avg_time_s, n}}.

    avg_err / median_err use only non-FAIL items (error < 9000) so a
    single FAIL doesn't swamp the average and mask real grounding-
    quality drift.  avg_time uses ALL items (FAILs still consumed
    wall-clock time).

    Single source of truth — both the gate (--gate) and the benchmark's
    METHOD SUMMARY table read from this.  The legacy inline aggregator
    in vlm_grounding_benchmark.py used a slightly different sort key
    (sum/len over ALL items including 9999s) which displaced methods
    with FAILs lower than they deserved; the call site now uses
    avg_err for the sort, matching the displayed value.
    """
    buckets: Dict[str, list] = defaultdict(list)
    for r in items:
        bucket_key = r.get(key)
        if bucket_key is None:
            continue
        buckets[bucket_key].append(r)
    summary: Dict[str, dict] = {}
    for k, group in buckets.items():
        clean = [r for r in group if r['error'] < 9000]
        clean = clean or group
        sorted_errs = sorted(r['error'] for r in clean)
        summary[k] = {
            'avg_err': sum(sorted_errs) / len(sorted_errs),
            'median_err': sorted_errs[len(sorted_errs) // 2],
            'exact_count': sum(1 for r in group if r['error'] < 30),
            'good_count': sum(1 for r in group if r['error'] < 80),
            'fail_count': sum(1 for r in group if r['error'] >= 9000),
            'avg_time_s': sum(r['time'] for r in group) / max(len(group), 1),
            'n': len(group),
        }
    return summary


def strategy_attribution(method_items: Iterable[dict]) -> Dict[Tuple[str, str], str]:
    """{(target, method): strategy_name} — snapshot of which sub-strategy
    fired per (target, method).  Stable across runs unless a code path
    silently shifts (e.g. a refactor that bypasses taskbar_pre_check).
    """
    return {(r['target'], r['method']): r.get('strategy', '?')
            for r in method_items}


def compare_buckets(
    current: Dict[str, dict],
    baseline: Dict[str, dict],
    label: str,
    err_threshold_pct: float = 10.0,
    time_threshold_pct: float = 20.0,
) -> List[str]:
    """Return list of human-readable regression strings.  Empty list = pass."""
    regressions: List[str] = []
    err_factor = 1.0 + err_threshold_pct / 100.0
    time_factor = 1.0 + time_threshold_pct / 100.0
    for k, base in baseline.items():
        cur = current.get(k)
        if cur is None:
            regressions.append(
                f'{label} "{k}": present in baseline, missing in current run')
            continue
        # +5 absolute fuzz on err so a baseline of 0 doesn't trip on
        # rounding noise.  +1.0s absolute fuzz on time for the same.
        if cur['avg_err'] > base['avg_err'] * err_factor + 5:
            regressions.append(
                f'{label} "{k}": avg_err {cur["avg_err"]:.0f} > baseline '
                f'{base["avg_err"]:.0f} × {err_factor:.2f}')
        if cur['exact_count'] < base['exact_count']:
            regressions.append(
                f'{label} "{k}": EXACT {cur["exact_count"]} < baseline {base["exact_count"]}')
        if cur['fail_count'] > base['fail_count']:
            regressions.append(
                f'{label} "{k}": FAIL {cur["fail_count"]} > baseline {base["fail_count"]}')
        if cur['avg_time_s'] > base['avg_time_s'] * time_factor + 1.0:
            regressions.append(
                f'{label} "{k}": avg_time {cur["avg_time_s"]:.1f}s > baseline '
                f'{base["avg_time_s"]:.1f}s × {time_factor:.2f}')
    return regressions


def compare_attribution(
    current_attr: Dict[Tuple[str, str], str],
    baseline_attr: Dict[Tuple[str, str], str],
) -> List[str]:
    """Strategy-attribution drift — same target+method, different
    sub-strategy fired = silent code-path change.  Per §0 invariant:
    "Same target → same strategy chain unless baseline-bump justified
    separately.\""""
    regressions: List[str] = []
    for key, base_strat in baseline_attr.items():
        cur_strat = current_attr.get(key)
        if cur_strat is None:
            continue  # bucket compare already flags missing-target
        if cur_strat != base_strat:
            regressions.append(
                f'attribution {key}: strategy "{cur_strat}" != baseline '
                f'"{base_strat}" (silent drift — accuracy may match but '
                f'the code path changed)')
    return regressions


def compare_router_decisions(
    current_router: List[dict],
    baseline_router: List[dict],
) -> List[str]:
    """Router-decision drift detection.  Phase 3.5 of the plan §13
    regression contract:

        "Router decisions are part of the §0 baseline.  Router shifting
        decisions silently is treated as a regression even if accuracy
        looks the same."

    Each entry shape: ``{'task': str, 'expected': str, 'actual': str, 'pass': bool}``.
    Two failure modes flagged:
      1. ``actual != expected`` in the current run — heuristic broke for
         a task in the regression contract.
      2. ``actual`` differs between baseline and current — silent drift
         even if both happen to "pass" (could happen if the expected
         field was bumped along with a code change).
    """
    regressions: List[str] = []
    baseline_by_task = {r['task']: r for r in baseline_router}
    current_by_task = {r['task']: r for r in current_router}

    for task, base in baseline_by_task.items():
        cur = current_by_task.get(task)
        if cur is None:
            regressions.append(
                f'router task "{task[:60]}": present in baseline, missing in current')
            continue
        if cur.get('actual') != base.get('actual'):
            regressions.append(
                f'router task "{task[:60]}": actual route '
                f'"{cur.get("actual")}" != baseline "{base.get("actual")}" '
                f'(silent router drift)')

    # Also catch cases where the current run flipped a 'pass' to 'fail'
    # without the baseline having any record (new tests not yet baselined).
    for task, cur in current_by_task.items():
        if not cur.get('pass') and task not in baseline_by_task:
            regressions.append(
                f'router task "{task[:60]}": new test added since baseline '
                f'and FAILS — expected="{cur.get("expected")}" '
                f'actual="{cur.get("actual")}" (bump baseline if intentional)')

    return regressions


def render_baseline_md(out_dict: dict) -> str:
    """Human-readable baseline summary committed alongside the JSON."""
    lines = [
        '# VLM Grounding Benchmark — Baseline',
        '',
        f'_Generated {out_dict.get("timestamp", "?")}'
        f' on screen={out_dict.get("screen", "?")} image={out_dict.get("image", "?")}_',
        '',
        '## Methods (the 3 sibling code paths)',
        '',
        '| Method | Avg Err | EXACT | FAIL | Avg Time (s) | N |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    method_summary = summarize_bucket(out_dict.get('method_results', []), 'method')
    for m, s in sorted(method_summary.items(), key=lambda kv: kv[1]['avg_err']):
        lines.append(
            f'| `{m}` | {s["avg_err"]:.0f} | {s["exact_count"]} | '
            f'{s["fail_count"]} | {s["avg_time_s"]:.1f} | {s["n"]} |')
    lines += ['', '## Prompt strategies', '',
              '| Strategy | Avg Err | EXACT | FAIL | Avg Time (s) | N |',
              '|---|---:|---:|---:|---:|---:|']
    strat_summary = summarize_bucket(out_dict.get('results', []), 'strategy')
    for s_name, s in sorted(strat_summary.items(), key=lambda kv: kv[1]['avg_err']):
        lines.append(
            f'| `{s_name}` | {s["avg_err"]:.0f} | {s["exact_count"]} | '
            f'{s["fail_count"]} | {s["avg_time_s"]:.1f} | {s["n"]} |')
    lines += ['', '## Per-target winners (lowest err per target)',
              '', '| Target | Best Method | Sub-strategy | Err |',
              '|---|---|---|---:|']
    target_buckets: Dict[str, list] = {}
    for r in out_dict.get('method_results', []):
        target_buckets.setdefault(r['target'], []).append(r)
    for target, rs in sorted(target_buckets.items()):
        winner = min(rs, key=lambda r: r['error'])
        lines.append(
            f'| {target} | `{winner["method"]}` | `{winner.get("strategy", "?")}` | '
            f'{winner["error"]:.0f} |')
    # Phase 3.5: router-decision section.  Same baseline-bump discipline
    # — silent change to a routed path is treated as a regression.
    router_results = out_dict.get('router_results', [])
    if router_results:
        lines += ['', '## Router decisions (Phase 3.5 §13 contract)',
                  '', '| Task | Expected | Actual | Pass |',
                  '|---|---|---|:---:|']
        for r in router_results:
            mark = '[OK]' if r.get('pass') else '[FAIL]'
            task_short = r.get('task', '')[:60]
            lines.append(
                f'| {task_short} | `{r.get("expected","?")}` | '
                f'`{r.get("actual","?")}` | {mark} |')
    return '\n'.join(lines) + '\n'
