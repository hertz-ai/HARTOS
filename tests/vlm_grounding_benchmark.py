"""
VLM Grounding Benchmark — All targets x All strategies.
Measures <point>x,y</point> accuracy, finds optimal strategy+prompt combos.

Run modes:
    # Plain run — print tables, save tests/vlm_benchmark_results.json
    PYTHONIOENCODING=utf-8 python tests/vlm_grounding_benchmark.py

    # Seed / refresh the no-regression baseline (commit with prefix
    # 'baseline-bump:' so reviewers see it's a deliberate move).
    python tests/vlm_grounding_benchmark.py --bump-baseline

    # Gate mode — compare to baseline; exit non-zero on regression.
    # CI hook + manual verification before merging anything that
    # touches integrations/vlm/** or qwen3vl_backend.py.
    python tests/vlm_grounding_benchmark.py --gate

Requires: VLM server reachable via core.port_registry.get_port('llm')
or HEVOLVE_VLM_ENDPOINT_URL (default 127.0.0.1:8080), PIL, pyautogui.
"""

import base64, re, io, requests, time, sys, json, argparse
from PIL import ImageGrab, Image

# ── CLI flags (parse early; rest of the file consults _ARGS) ───────
# parse_known_args so pytest / kernel-runner extra flags don't error
_parser = argparse.ArgumentParser(
    description='VLM grounding benchmark + no-regression gate.')
_parser.add_argument(
    '--gate', action='store_true',
    help='After running, compare to baseline JSON and exit non-zero on regression')
_parser.add_argument(
    '--bump-baseline', action='store_true',
    help='Promote current run to baseline (writes JSON + .md).  Commit with prefix "baseline-bump:"')
_parser.add_argument(
    '--baseline-path', default='tests/vlm_benchmark_baseline.json',
    help='Path to baseline JSON (default: tests/vlm_benchmark_baseline.json)')
_parser.add_argument(
    '--err-threshold-pct', type=float, default=10.0,
    help='Allow up to N%% increase in avg_err per group before failing the gate (default 10)')
_parser.add_argument(
    '--time-threshold-pct', type=float, default=20.0,
    help='Allow up to N%% increase in avg_time per group before failing the gate (default 20)')
_parser.add_argument(
    '--exec-test', action='store_true',
    help='Phase 4 §3: actually execute clicks at predicted coords inside '
         'a sandboxed scratch window (NOT user real desktop).  Measures '
         'click-on-target accuracy in pixels.  Requires a scratch window '
         'spec via --exec-target-rect.  Default off — never clicks the '
         'user real desktop without explicit opt-in.')
_parser.add_argument(
    '--exec-target-rect', type=str, default='',
    help='"x,y,w,h" of a scratch test target rect inside a benign window '
         '(e.g. a Notepad-with-a-button screen).  Required by --exec-test.  '
         'Coords in physical screen pixels.')
_ARGS, _ = _parser.parse_known_args()

# ── Screenshot ──────────────────────────────────────────────────────
img = ImageGrab.grab()
SW, SH = img.size
print(f"Screen: {SW}x{SH}")

IMG_W, IMG_H = 1024, 576
img_resized = img.resize((IMG_W, IMG_H), Image.LANCZOS)
buf = io.BytesIO()
img_resized.save(buf, 'JPEG', quality=50)
b64 = base64.b64encode(buf.getvalue()).decode('ascii')
print(f"Image: {IMG_W}x{IMG_H}, {len(buf.getvalue())//1024}KB")

# ── VLM call ────────────────────────────────────────────────────────
# Resolve the VLM URL the same way Qwen3VLBackend does: env override →
# port_registry default.  Avoids the hardcoded 8080 that was wrong on
# installs where the port_registry maps llm to a different port (e.g.
# 8082 in the current Nunba bundle).
def _resolve_vlm_url():
    explicit = os.environ.get('HEVOLVE_VLM_ENDPOINT_URL') or os.environ.get('HEVOLVE_LLM_ENDPOINT_URL')
    if explicit:
        return explicit.rstrip('/') + '/chat/completions'
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from core.port_registry import get_port
        port = get_port('llm')
    except Exception:
        port = int(os.environ.get('HEVOLVE_LLM_PORT', 8080))
    return f'http://127.0.0.1:{port}/v1/chat/completions'

import os
_VLM_URL = _resolve_vlm_url()
print(f"VLM endpoint: {_VLM_URL}")

def vlm(prompt, max_tok=100):
    r = requests.post(_VLM_URL, json={
        'model': 'local', 'max_tokens': max_tok, 'temperature': 0.1,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}}
        ]}]
    }, timeout=90)
    d = r.json()
    return d['choices'][0]['message']['content'].strip()

# ── Parse helpers ───────────────────────────────────────────────────
def parse_point(raw):
    """Extract <point>x,y</point> from response."""
    m = re.search(r'<point>\s*(\d+)\s*,\s*(\d+)\s*</point>', raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None

def parse_box(raw):
    """Extract <box>x1,y1,x2,y2</box> center."""
    m = re.search(r'<box>\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*</box>', raw)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1+x2)//2, (y1+y2)//2
    return None

def parse_any_coord(raw):
    """Try point, then box, then raw number pair."""
    p = parse_point(raw)
    if p: return p, 'point'
    p = parse_box(raw)
    if p: return p, 'box'
    # Fallback: last two numbers in range 0-1000
    nums = re.findall(r'\b(\d{1,4})\b', raw)
    nums = [(int(n)) for n in nums if 0 <= int(n) <= 1000]
    if len(nums) >= 2:
        return (nums[-2], nums[-1]), 'fallback'
    return None, 'none'

# ── Ground truth targets (normalized 0-1000 coords) ────────────────
# These were verified in previous sessions
TARGETS = {
    'Start button':             (238, 977),
    'Search icon':              (267, 977),
    'Chrome icon':              (383, 977),
    'Close button (top-right)': (985, 10),
    'File Explorer icon':       (440, 977),
    'Clock/time display':       (900, 977),
}

# Phase 1 §1 deferred — occluded-window targets behind a flag so the
# baseline isn't broken by their absence.  Setup: open Notepad +
# Calculator BEFORE running the benchmark, then put a fullscreen
# Chrome on top of them.  Ground-truth coords are for Notepad's title
# bar / Calculator's "=" button as they appear in the captured
# screenshot taken THROUGH the occluded-window capture path.
# Activate with HEVOLVE_VLM_BENCH_OCCLUDED=1.  The baseline gate
# treats these as additive — adding/removing them is a baseline-bump.
if os.environ.get('HEVOLVE_VLM_BENCH_OCCLUDED', '').lower() in ('1', 'true', 'yes'):
    TARGETS.update({
        'Notepad title bar (occluded)':        (500, 50),
        'Notepad menu bar (occluded)':         (200, 80),
        'Calculator equals button (occluded)': (650, 850),
        'Calculator clear button (occluded)':  (650, 250),
    })
    print("Occluded targets enabled (HEVOLVE_VLM_BENCH_OCCLUDED=1)")

# ── Strategies ──────────────────────────────────────────────────────
STRATEGIES = {
    # --- Existing strategies ---
    'direct': {
        'prompt': 'Point to the {target}. <point>x,y</point>',
        'max_tok': 50,
    },
    'describe_first': {
        'prompt': 'Where is the {target}? First describe its position on screen in 1 sentence, then give the exact location as <point>x,y</point>',
        'max_tok': 100,
    },
    'negative': {
        'prompt': 'Point to the {target}. Do NOT point to the center of the screen. Give exact edge position. <point>x,y</point>',
        'max_tok': 80,
    },
    'bbox': {
        'prompt': 'Locate the {target}. Give its bounding box as <box>x1,y1,x2,y2</box> (0-1000 normalized)',
        'max_tok': 60,
    },

    # --- New optimized strategies ---
    'cot_anchor': {
        'prompt': (
            'I need to click the {target}.\n'
            'Step 1: What edge of the screen is the {target} near? (top/bottom/left/right)\n'
            'Step 2: Estimate its x position as percentage from left edge (0%=left, 100%=right)\n'
            'Step 3: Estimate its y position as percentage from top edge (0%=top, 100%=bottom)\n'
            'Step 4: Give the location as <point>x,y</point> (0-1000 scale)'
        ),
        'max_tok': 150,
    },
    'region_then_point': {
        'prompt': (
            'The screen is divided into 9 regions:\n'
            'TL TC TR\n'
            'ML MC MR\n'
            'BL BC BR\n'
            'Which region contains the {target}? Then give its exact <point>x,y</point> (0-1000).'
        ),
        'max_tok': 100,
    },
    'relative_anchor': {
        'prompt': (
            'Look at the {target}. '
            'How far from the LEFT edge is it? (percentage) '
            'How far from the TOP edge is it? (percentage) '
            'Now give <point>x,y</point> where x=left% * 10, y=top% * 10.'
        ),
        'max_tok': 120,
    },
    'elimination': {
        'prompt': (
            'I need to find the {target}.\n'
            'Is it in the top half or bottom half? '
            'Is it in the left third, middle third, or right third? '
            'Now give the precise <point>x,y</point> (0-1000 normalized).'
        ),
        'max_tok': 120,
    },
    'taskbar_list': {
        'prompt': (
            'List every icon in the taskbar at the bottom of the screen, from LEFT to RIGHT. '
            'For each icon give its <point>x,y</point> location. Format:\n'
            '1. [icon name] <point>x,y</point>\n'
            '2. [icon name] <point>x,y</point>\n...'
        ),
        'max_tok': 300,
    },
    'pixel_hint': {
        'prompt': (
            'This image is 1024x576 pixels. '
            'The {target} is a small UI element. '
            'Give its center location as <point>x,y</point> (0-1000 normalized, where 0=left/top, 1000=right/bottom).'
        ),
        'max_tok': 80,
    },
    'describe_all_then_pick': {
        'prompt': (
            'List every clickable element you see at the bottom taskbar. '
            'For each one say: [name] at <point>x,y</point>\n'
            'Then answer: which one is the {target}?'
        ),
        'max_tok': 300,
    },
}

# ── Run benchmark ───────────────────────────────────────────────────
# Skip-strategy escape hatch: set HEVOLVE_VLM_BENCH_METHODS_ONLY=1 to
# bypass the prompt-strategy section entirely and run only the new
# METHOD section below.  Useful when the strategy results from a prior
# run are still in tests/vlm_benchmark_results.json and you only want
# to refresh the per-method comparison (e.g. after a code change in
# qwen3vl_backend or local_loop).
_methods_only = os.environ.get('HEVOLVE_VLM_BENCH_METHODS_ONLY', '').lower() in ('1', 'true', 'yes')

results = []
if _methods_only:
    print(f"\nSkipping prompt-strategy section (HEVOLVE_VLM_BENCH_METHODS_ONLY=1)")
    print(f"Loading prior strategy results from tests/vlm_benchmark_results.json if present")
    try:
        with open('tests/vlm_benchmark_results.json') as _prev_f:
            _prev = json.load(_prev_f)
            results = _prev.get('results', [])
            print(f"  Loaded {len(results)} prior strategy results")
    except Exception as _prev_err:
        print(f"  Could not load prior results ({_prev_err}) — strategy section will be empty")
else:
    print(f"\n{'='*90}")
    print(f"{'Target':30s} {'Strategy':22s} {'Got':12s} {'Expected':12s} {'Err':>6s} {'Grade':6s} {'Time':>5s}")
    print(f"{'='*90}")

for target, (exp_x, exp_y) in (TARGETS.items() if not _methods_only else ()):
    for strat_name, strat in STRATEGIES.items():
        # taskbar_list is target-agnostic — only run once, then extract per-target
        if strat_name in ('taskbar_list', 'describe_all_then_pick') and target not in ('Start button',):
            # We'll run it once for Start, then parse results for other targets
            continue

        prompt = strat['prompt'].format(target=target)
        max_tok = strat['max_tok']

        t0 = time.time()
        try:
            raw = vlm(prompt, max_tok=max_tok)
        except Exception as e:
            print(f"{target:30s} {strat_name:22s} {'ERROR':12s} {f'({exp_x},{exp_y})':12s} {'---':>6s} {'ERR':6s} {'--':>5s}")
            results.append({
                'target': target, 'strategy': strat_name,
                'got': None, 'expected': (exp_x, exp_y),
                'error': 9999, 'grade': 'ERR', 'time': 0, 'raw': str(e),
            })
            continue
        elapsed = time.time() - t0

        # For list strategies, try to extract the specific target
        coord, method = parse_any_coord(raw)

        if strat_name in ('taskbar_list', 'describe_all_then_pick'):
            # Try to find specific target in the list
            # For the initial run (Start button), also extract other targets
            for t_name, (t_ex, t_ey) in TARGETS.items():
                # Find the line mentioning this target
                for line in raw.split('\n'):
                    if any(kw in line.lower() for kw in t_name.lower().split()):
                        lc = parse_point(line)
                        if lc:
                            lerr = ((lc[0]-t_ex)**2 + (lc[1]-t_ey)**2) ** 0.5
                            lgrade = "EXACT" if lerr < 30 else "GOOD" if lerr < 80 else "OK" if lerr < 150 else "BAD"
                            print(f"{t_name:30s} {strat_name:22s} {f'({lc[0]},{lc[1]})':12s} {f'({t_ex},{t_ey})':12s} {lerr:6.0f} {lgrade:6s} {elapsed:5.1f}s")
                            results.append({
                                'target': t_name, 'strategy': strat_name,
                                'got': lc, 'expected': (t_ex, t_ey),
                                'error': lerr, 'grade': lgrade, 'time': elapsed,
                                'raw': line.strip(),
                            })
                            break
            continue

        if coord:
            err = ((coord[0]-exp_x)**2 + (coord[1]-exp_y)**2) ** 0.5
            grade = "EXACT" if err < 30 else "GOOD" if err < 80 else "OK" if err < 150 else "BAD"
            got_str = f'({coord[0]},{coord[1]})'
        else:
            err = 9999
            grade = "FAIL"
            got_str = "FAIL"

        exp_str = f'({exp_x},{exp_y})'
        print(f"{target:30s} {strat_name:22s} {got_str:12s} {exp_str:12s} {err:6.0f} {grade:6s} {elapsed:5.1f}s")

        results.append({
            'target': target, 'strategy': strat_name,
            'got': coord, 'expected': (exp_x, exp_y),
            'error': err, 'grade': grade, 'time': elapsed,
            'raw': raw[:200],
        })

# ── Summary by strategy ─────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"STRATEGY SUMMARY")
print(f"{'='*70}")
print(f"{'Strategy':22s} {'Avg_err':>8s} {'Median':>8s} {'Best':>6s} {'Worst':>6s} {'EXACT':>6s} {'GOOD+':>6s} {'N':>4s}")
print(f"{'-'*70}")

from collections import defaultdict
strat_errors = defaultdict(list)
for r in results:
    strat_errors[r['strategy']].append(r['error'])

# Sort by avg error
for strat_name, errors in sorted(strat_errors.items(), key=lambda x: sum(x[1])/len(x[1])):
    errors_clean = [e for e in errors if e < 9000]
    if not errors_clean:
        errors_clean = [9999]
    avg = sum(errors_clean) / len(errors_clean)
    srt = sorted(errors_clean)
    median = srt[len(srt)//2]
    best = min(errors_clean)
    worst = max(errors_clean)
    exact = sum(1 for e in errors if e < 30)
    good = sum(1 for e in errors if e < 80)
    n = len(errors)
    print(f"{strat_name:22s} {avg:8.0f} {median:8.0f} {best:6.0f} {worst:6.0f} {exact:6d} {good:6d} {n:4d}")

# ── Summary by target ───────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"TARGET SUMMARY (best strategy per target)")
print(f"{'='*70}")
target_results = defaultdict(list)
for r in results:
    target_results[r['target']].append(r)

for target, trs in target_results.items():
    best = min(trs, key=lambda x: x['error'])
    print(f"  {target:30s} best={best['strategy']:22s} err={best['error']:.0f} grade={best['grade']}")

# ════════════════════════════════════════════════════════════════════
# METHOD BENCHMARK — runs the 3 sibling code paths against the SAME
# 6 targets so we can pick the right path per task class instead of
# guessing.  Sibling paths:
#
#   1. point_and_act      single-shot, has 3 internal strategies
#                         (taskbar_list shortcut, describe_first,
#                         elimination_retry).  Used by /visual_agent.
#   2. parse_and_reason   single-shot, returns SoM + action.  Heavy.
#                         Currently no production callers (only tests).
#   3. run_local_agentic_loop  multi-iter loop, used by every other
#                              VLM entry point (chat → autogen,
#                              CREATE/REUSE recipes, robot, coding
#                              agent).  Has its own inline prompt; got
#                              taskbar+bias helpers added in b7936bf.
#
# This section measures each method's grounding accuracy + latency on
# the same 6 targets the prompt-strategy benchmark above uses, so the
# comparison is apples-to-apples (same image, same ground truth).
#
# Loop is run with max_iterations=1 and execute_action mocked to a
# no-op so nothing actually clicks on the runner's screen — we just
# capture the loop's first grounding decision.
# ════════════════════════════════════════════════════════════════════

# Single source of truth for bucket aggregation + gate logic — used
# both by the METHOD SUMMARY table below AND by --gate / --bump-baseline
# at the bottom.  Imported above the screenshot/benchmark execution so
# the methods section's table can call summarize_bucket on the fly.
from vlm_gate_lib import (
    summarize_bucket, strategy_attribution,
    compare_buckets, compare_attribution, compare_router_decisions,
    render_baseline_md,
)

method_results = []
print(f"\n{'='*90}")
print("METHOD BENCHMARK — running parse_and_reason / point_and_act / loop_one_iter")
print(f"{'='*90}")

# Lazy import — the existing prompt-strategy section above has no
# dependency on HARTOS internals; only the method section does.  This
# keeps the prompt-strategy benchmark runnable in environments that
# don't have hart-backend importable (e.g. raw VLM probe).
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
    from integrations.vlm.qwen3vl_backend import get_qwen3vl_backend
    _backend = get_qwen3vl_backend()
    _methods_available = True
except Exception as _e:
    print(f"Method benchmark skipped — HARTOS not importable: {_e}")
    _backend = None
    _methods_available = False


def _grade(err):
    return 'EXACT' if err < 30 else 'GOOD' if err < 80 else 'OK' if err < 150 else 'BAD'


def _norm_xy_from_method_result(action_dict, img_w, img_h, screen_w, screen_h):
    """Pull the (nx, ny) in 0-1000 normalized space out of a method's
    result dict.  Different methods name the keys differently:

      point_and_act      → 'norm_x', 'norm_y' (already 0-1000)
      parse_and_reason   → action_json['coordinate'] = [px, py] image px
      loop_one_iter      → action_json['coordinate'] = [screen_x, screen_y]
                           (back-scaled below to 0-1000 for comparison)
    """
    if action_dict is None:
        return None, None
    # point_and_act
    nx = action_dict.get('norm_x')
    ny = action_dict.get('norm_y')
    if nx is not None and ny is not None:
        return int(nx), int(ny)
    # parse_and_reason / loop  — pull from coordinate field
    coord = action_dict.get('coordinate') or (
        action_dict.get('action_json') or {}).get('coordinate')
    if coord and isinstance(coord, list) and len(coord) == 2:
        cx, cy = coord
        # If coordinate is already 0-1000 normalized, return as-is.
        # Otherwise back-scale from screen pixels.
        if cx <= 1000 and cy <= 1000 and (img_w == 1024 and img_h == 576):
            return int(cx), int(cy)
        # Treat as screen-space → back-scale to 0-1000
        nx = int(cx * 1000 / screen_w)
        ny = int(cy * 1000 / screen_h)
        return nx, ny
    return None, None


def _run_point_and_act(target):
    t0 = time.time()
    res = _backend.point_and_act(b64, f'click the {target}')
    elapsed = time.time() - t0
    nx, ny = _norm_xy_from_method_result(res, IMG_W, IMG_H, SW, SH)
    return nx, ny, elapsed, res.get('strategy', '?')


def _run_parse_and_reason(target):
    t0 = time.time()
    res = _backend.parse_and_reason(b64, f'click the {target}')
    elapsed = time.time() - t0
    nx, ny = _norm_xy_from_method_result(
        {'coordinate': (res.get('action_json') or {}).get('coordinate')},
        IMG_W, IMG_H, SW, SH,
    )
    return nx, ny, elapsed, 'parse_and_reason'


def _run_loop_one_iter(target):
    """Drive run_local_agentic_loop for ONE iteration with the unified
    branch enabled and execute_action mocked.  Captures the first
    action the loop's inline prompt produces — including any
    taskbar_pre_check shortcut and bias-retry that fired."""
    from unittest import mock
    from integrations.vlm import local_loop as _ll
    # Force the unified branch (the one with my new helpers).
    _os.environ['HEVOLVE_VLM_UNIFIED'] = 'true'
    message = {
        'instruction_to_vlm_agent': f'click the {target}',
        'enhanced_instruction': f'click the {target}',
        'user_id': 'bench', 'prompt_id': 'bench',
        'os_to_control': 'windows', 'max_ETA_in_seconds': 60,
    }
    t0 = time.time()
    with mock.patch('integrations.vlm.local_computer_tool.execute_action',
                    return_value={'status': 'mocked', 'success': True}), \
         mock.patch('integrations.vlm.local_computer_tool.take_screenshot',
                    return_value=b64):
        res = _ll.run_local_agentic_loop(message, tier='inprocess', max_iterations=1)
    elapsed = time.time() - t0
    actions = res.get('extracted_responses') or []
    if not actions:
        return None, None, elapsed, '(no action)'
    first_entry = actions[0] or {}
    first = first_entry.get('content')
    # Guard against the iteration-error path: when the loop's body
    # raises, extracted_responses[i]['content'] is the str(exception)
    # — see local_loop.py iteration except clause.  Detect and surface
    # as a benchmarkable failure instead of crashing on first.get().
    if isinstance(first, str):
        return None, None, elapsed, f'(iter error: {first[:30]})'
    if first is None:
        return None, None, elapsed, '(empty content)'
    coord = first.get('coordinate')
    strategy = first.get('_strategy', 'inline_prompt')
    nx = ny = None
    if coord and isinstance(coord, list) and len(coord) == 2:
        cx, cy = coord
        # Loop emits screen-space coordinates after scaling — back-scale
        # to 0-1000 normalized for comparison with TARGETS.
        nx = int(cx * 1000 / SW) if SW else cx
        ny = int(cy * 1000 / SH) if SH else cy
    return nx, ny, elapsed, strategy


_METHODS = (
    ('point_and_act',     _run_point_and_act),
    ('parse_and_reason',  _run_parse_and_reason),
    ('loop_one_iter',     _run_loop_one_iter),
)

if _methods_available:
    print(f"{'Target':30s} {'Method':20s} {'Got':12s} {'Expected':12s} {'Err':>6s} {'Grade':6s} {'Time':>6s} {'Strat':20s}")
    print(f"{'-'*120}")
    for target, (exp_x, exp_y) in TARGETS.items():
        for m_name, m_fn in _METHODS:
            try:
                nx, ny, elapsed, strat = m_fn(target)
            except Exception as e:
                print(f"{target:30s} {m_name:20s} {'ERROR':12s} {f'({exp_x},{exp_y})':12s} {'---':>6s} {'ERR':6s} {'--':>6s} {str(e)[:18]:20s}")
                method_results.append({
                    'target': target, 'method': m_name,
                    'got': None, 'expected': (exp_x, exp_y),
                    'error': 9999, 'grade': 'ERR',
                    'time': 0, 'strategy': str(e)[:60],
                })
                continue
            if nx is None or ny is None:
                err = 9999
                grade = 'FAIL'
                got_str = 'FAIL'
            else:
                err = ((nx - exp_x) ** 2 + (ny - exp_y) ** 2) ** 0.5
                grade = _grade(err)
                got_str = f'({nx},{ny})'
            print(f"{target:30s} {m_name:20s} {got_str:12s} {f'({exp_x},{exp_y})':12s} {err:6.0f} {grade:6s} {elapsed:5.1f}s {strat[:18]:20s}")
            method_results.append({
                'target': target, 'method': m_name,
                'got': (nx, ny) if nx is not None else None,
                'expected': (exp_x, exp_y),
                'error': err, 'grade': grade,
                'time': elapsed, 'strategy': strat,
            })

    # Per-method summary — uses the single-source-of-truth summarize_bucket
    # from vlm_gate_lib so the gate and the human-readable table compute
    # avg_err / median_err / exact / good / fail / avg_time identically.
    # The earlier inline aggregator used a different sort key
    # (sum/len over ALL items including 9999 FAILs) that displaced
    # methods-with-FAILs lower than their actual avg_err warranted.
    print(f"\n{'='*70}")
    print("METHOD SUMMARY")
    print(f"{'='*70}")
    print(f"{'Method':20s} {'Avg_err':>8s} {'Median':>8s} {'EXACT':>6s} {'GOOD+':>6s} {'AvgTime':>8s} {'N':>4s}")
    print(f"{'-'*70}")
    _method_summary = summarize_bucket(method_results, 'method')
    for m_name, s in sorted(_method_summary.items(),
                            key=lambda kv: kv[1]['avg_err']):
        print(f"{m_name:20s} {s['avg_err']:8.0f} {s['median_err']:8.0f} "
              f"{s['exact_count']:6d} {s['good_count']:6d} "
              f"{s['avg_time_s']:7.1f}s {s['n']:4d}")

    # Fallback recommendation: per-task-class winner.
    # Rebuild the per-method bucket from method_results — the
    # summarize_bucket DRY refactor in commit 0fa2cb0 removed the
    # earlier method_buckets variable.  Reviewer (post-shipment)
    # caught the NameError before any user hit it.
    from collections import defaultdict as _dd_local
    print(f"\n{'='*70}")
    print("PER-TARGET WINNER (lowest error)")
    print(f"{'='*70}")
    target_buckets = _dd_local(list)
    method_buckets = _dd_local(list)
    for r in method_results:
        target_buckets[r['target']].append(r)
        method_buckets[r['method']].append(r)
    for target, rs in target_buckets.items():
        winner = min(rs, key=lambda r: r['error'])
        print(f"  {target:30s} → {winner['method']:20s} "
              f"err={winner['error']:.0f} ({winner['strategy']})")

    print(f"\n{'='*70}")
    print("RECOMMENDED FALLBACK CHAIN")
    print(f"{'='*70}")
    # Rank methods by their per-target win count + by avg error.
    win_counts = _dd_local(int)
    for target, rs in target_buckets.items():
        winner = min(rs, key=lambda r: r['error'])
        win_counts[winner['method']] += 1
    avg_errs = {
        m: sum(r['error'] for r in rs) / len(rs)
        for m, rs in method_buckets.items()
    }
    ranked = sorted(
        method_buckets.keys(),
        key=lambda m: (-win_counts[m], avg_errs[m]),
    )
    for i, m in enumerate(ranked, 1):
        print(f"  {i}. {m:20s} (wins={win_counts[m]}/{len(target_buckets)}, "
              f"avg_err={avg_errs[m]:.0f})")
    print(f"\nUse #{ranked[0]!r} as primary, #{ranked[1]!r} as fallback when "
          f"primary FAILs/errors out.  When the primary's confidence is low "
          f"(e.g. EXACT/GOOD threshold violated by avg+1σ), retry with "
          f"#{ranked[1]!r} before surfacing the result to the agent.")


# ════════════════════════════════════════════════════════════════════
# EXEC-TEST MODE — Phase 4 §3 deferred deliverable.
#
# Optional, opt-in via --exec-test plus --exec-target-rect.  Measures
# how many of the predicted norm coords would land INSIDE a known
# safe target rect when actually clicked.  Doesn't actually move the
# mouse — that requires user trust we don't presume here.  This is
# pure post-hoc analysis on the existing method_results.
# ════════════════════════════════════════════════════════════════════

exec_test_results = []
if _ARGS.exec_test:
    if not _ARGS.exec_target_rect:
        print("\n[exec-test] --exec-test requires --exec-target-rect 'x,y,w,h'")
    else:
        try:
            tx, ty, tw, th = [int(s) for s in _ARGS.exec_target_rect.split(',')]
            print(f"\n{'='*70}")
            print(f"EXEC-TEST — target rect=({tx},{ty})+{tw}x{th}")
            print(f"{'='*70}")
            print(f"{'Method':22s} {'Target':30s} {'Pred':12s} {'Inside':>6s} {'Margin':>7s}")
            for r in method_results:
                got = r.get('got')
                if not got or len(got) != 2:
                    continue
                # got is normalized 0-1000; convert to screen px.
                gx_px = int(got[0] * SW / 1000)
                gy_px = int(got[1] * SH / 1000)
                inside = (tx <= gx_px <= tx + tw and ty <= gy_px <= ty + th)
                # Margin = signed distance from target rect (+inside, -outside).
                if inside:
                    margin = min(gx_px - tx, tx + tw - gx_px,
                                 gy_px - ty, ty + th - gy_px)
                else:
                    dx = max(0, tx - gx_px, gx_px - (tx + tw))
                    dy = max(0, ty - gy_px, gy_px - (ty + th))
                    margin = -((dx ** 2 + dy ** 2) ** 0.5)
                print(f"  {r['method']:22s} {r['target'][:28]:30s} "
                      f"({gx_px},{gy_px})  {'YES' if inside else 'no':>6s} "
                      f"{margin:>7.0f}")
                exec_test_results.append({
                    'method': r['method'], 'target': r['target'],
                    'pred_screen': (gx_px, gy_px),
                    'inside_target': inside, 'margin_px': margin,
                })
            inside_count = sum(1 for r in exec_test_results if r['inside_target'])
            print(f"\nEXEC-TEST: {inside_count}/{len(exec_test_results)} predictions land inside target")
        except ValueError:
            print(f"[exec-test] bad rect format {_ARGS.exec_target_rect!r} — expect 'x,y,w,h'")


# ════════════════════════════════════════════════════════════════════
# ROUTER-DECISION TESTS — Phase 3.5 of the plan §13.
#
# The complementary path router (Qwen3VLBackend.route_task) is the
# keystone that makes the three sibling methods actually-complementary
# instead of just-coexisting.  Routing decisions are part of the §0
# baseline: same task → same routing decision unless baseline-bump
# justifies it.
#
# These 6 tests don't call the VLM — they just ask route_task what
# path it would pick.  Cheap (microseconds), runs every time so the
# baseline gate catches silent router drift.
# ════════════════════════════════════════════════════════════════════

router_results = []
print(f"\n{'='*80}")
print("ROUTER DECISIONS — task → expected path → actual path")
print(f"{'='*80}")
_ROUTER_CASES = [
    # (task, expected_route)  — drawn from plan §13's regression contract
    ('Click the Start button',                  'single_shot'),
    ('Tap the play button on Spotify',          'single_shot'),
    ('Open Notepad and type Hello',             'multi_step'),
    ('Open Spotify and play Sgt Pepper',        'multi_step'),
    ('list all clickable elements on screen',   'enumerate'),
    ("what's on screen right now?",             'enumerate'),
]
if _methods_available:
    for _task, _expected in _ROUTER_CASES:
        _actual = _backend.route_task(_task)
        _ok = (_actual == _expected)
        marker = 'PASS' if _ok else 'FAIL'
        print(f"  [{marker}] {_task[:48]:48s} → expect={_expected:11s} got={_actual}")
        router_results.append({
            'task': _task,
            'expected': _expected,
            'actual': _actual,
            'pass': _ok,
        })
    _router_passes = sum(1 for r in router_results if r['pass'])
    print(f"\nRouter: {_router_passes}/{len(router_results)} routing decisions match expected")

# ── Save JSON ────────────────────────────────────────────────────────
out = {
    'screen': f'{SW}x{SH}', 'image': f'{IMG_W}x{IMG_H}',
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'results': results,
    'method_results': method_results,
    'router_results': router_results,
    'exec_test_results': exec_test_results,
}
with open('tests/vlm_benchmark_results.json', 'w') as f:
    json.dump(out, f, indent=2, default=str)
print(f"\nResults saved to tests/vlm_benchmark_results.json")
print(f"Strategy tests: {len(results)}, method tests: {len(method_results)}, "
      f"router tests: {len(router_results)}")


# ════════════════════════════════════════════════════════════════════
# §0 of vlm_best_of_all_worlds_plan.md — the no-regression gate.
#
# Whole VLM workstream is gated by this.  No phase ships unless the
# gate stays green against the committed baseline.  Two operations:
#
#   --bump-baseline  Promote the just-finished run to baseline
#                    (writes both .json + .md).  Author MUST commit
#                    with prefix 'baseline-bump:' so reviewers see
#                    it's a deliberate move, not a silent drift.
#
#   --gate           Compare the just-finished run to baseline JSON
#                    and exit non-zero if ANY of:
#                      * avg_err per (strategy or method) bucket
#                        increased by more than --err-threshold-pct
#                      * EXACT count per bucket decreased
#                      * FAIL count per bucket increased
#                      * avg_time per bucket grew by more than
#                        --time-threshold-pct
#                      * per-(target,method) sub-strategy attribution
#                        changed (silent strategy drift catches code
#                        paths that pass coords but lose the smarts)
# ════════════════════════════════════════════════════════════════════


# ── --bump-baseline ───────────────────────────────────────────────
if _ARGS.bump_baseline:
    os.makedirs(os.path.dirname(_ARGS.baseline_path) or '.', exist_ok=True)
    with open(_ARGS.baseline_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    md_path = _ARGS.baseline_path[:-5] + '.md' if _ARGS.baseline_path.endswith('.json') \
        else _ARGS.baseline_path + '.md'
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(render_baseline_md(out))
    print(f"\n[baseline-bump] Wrote {_ARGS.baseline_path}")
    print(f"[baseline-bump] Wrote {md_path}")
    print(f"[baseline-bump] Commit with prefix 'baseline-bump:' so reviewers see "
          f"it's a deliberate move (not silent drift)")


# ── --gate ────────────────────────────────────────────────────────
if _ARGS.gate:
    if not os.path.exists(_ARGS.baseline_path):
        print(f"\n[gate] No baseline at {_ARGS.baseline_path} — nothing to "
              f"compare against.  First seed it with --bump-baseline.")
        sys.exit(0)
    with open(_ARGS.baseline_path) as f:
        _baseline = json.load(f)
    print(f"\n[gate] Comparing to baseline from {_baseline.get('timestamp', '?')}")
    _regressions = (
        compare_buckets(
            summarize_bucket(results, 'strategy'),
            summarize_bucket(_baseline.get('results', []), 'strategy'),
            'strategy', _ARGS.err_threshold_pct, _ARGS.time_threshold_pct)
        + compare_buckets(
            summarize_bucket(method_results, 'method'),
            summarize_bucket(_baseline.get('method_results', []), 'method'),
            'method', _ARGS.err_threshold_pct, _ARGS.time_threshold_pct)
        + compare_attribution(
            strategy_attribution(method_results),
            strategy_attribution(_baseline.get('method_results', [])))
        + compare_router_decisions(
            router_results, _baseline.get('router_results', []))
    )
    if _regressions:
        print(f"[gate] FAIL — {len(_regressions)} regression(s):")
        for _r in _regressions:
            print(f"  - {_r}")
        sys.exit(1)
    print(f"[gate] PASS — no regressions vs baseline")
    sys.exit(0)
