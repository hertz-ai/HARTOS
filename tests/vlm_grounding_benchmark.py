"""
VLM Grounding Benchmark — All targets x All strategies.
Measures <point>x,y</point> accuracy, finds optimal strategy+prompt combos.

Run: PYTHONIOENCODING=utf-8 python tests/vlm_grounding_benchmark.py
Requires: VLM server on 127.0.0.1:8080, PIL, pyautogui (for screen size)
"""

import base64, re, io, requests, time, sys, json
from PIL import ImageGrab, Image

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
def vlm(prompt, max_tok=100):
    r = requests.post('http://127.0.0.1:8080/v1/chat/completions', json={
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
results = []
print(f"\n{'='*90}")
print(f"{'Target':30s} {'Strategy':22s} {'Got':12s} {'Expected':12s} {'Err':>6s} {'Grade':6s} {'Time':>5s}")
print(f"{'='*90}")

for target, (exp_x, exp_y) in TARGETS.items():
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

# ── Save JSON ────────────────────────────────────────────────────────
out = {
    'screen': f'{SW}x{SH}', 'image': f'{IMG_W}x{IMG_H}',
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'results': results,
}
with open('tests/vlm_benchmark_results.json', 'w') as f:
    json.dump(out, f, indent=2, default=str)
print(f"\nResults saved to tests/vlm_benchmark_results.json")
print(f"Total tests: {len(results)}")
