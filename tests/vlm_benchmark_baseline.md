# VLM Grounding Benchmark — Baseline

_Generated 2026-05-04 00:20:14 on screen=2560x1440 image=1024x576_

## Methods (the 3 sibling code paths)

| Method | Avg Err | EXACT | FAIL | Avg Time (s) | N |
|---|---:|---:|---:|---:|---:|
| `parse_and_reason` | 86 | 1 | 2 | 30.9 | 6 |
| `point_and_act` | 132 | 1 | 1 | 23.4 | 6 |
| `loop_one_iter` | 9999 | 0 | 6 | 40.4 | 6 |

## Prompt strategies

| Strategy | Avg Err | EXACT | FAIL | Avg Time (s) | N |
|---|---:|---:|---:|---:|---:|
| `describe_first` | 92 | 3 | 0 | 1.2 | 6 |
| `cot_anchor` | 118 | 1 | 0 | 1.8 | 6 |
| `elimination` | 166 | 1 | 0 | 1.3 | 6 |
| `taskbar_list` | 171 | 0 | 0 | 4.7 | 3 |
| `describe_all_then_pick` | 263 | 3 | 0 | 4.7 | 5 |
| `negative` | 284 | 1 | 0 | 0.7 | 6 |
| `region_then_point` | 336 | 1 | 2 | 1.6 | 6 |
| `direct` | 430 | 1 | 0 | 0.8 | 6 |
| `bbox` | 483 | 0 | 0 | 0.9 | 6 |
| `pixel_hint` | 641 | 1 | 0 | 0.8 | 6 |
| `relative_anchor` | 826 | 0 | 0 | 1.9 | 6 |

## Per-target winners (lowest err per target)

| Target | Best Method | Sub-strategy | Err |
|---|---|---|---:|
| Chrome icon | `point_and_act` | `describe_first` | 37 |
| Clock/time display | `point_and_act` | `HTTPConnectionPool(host='127.0.0.1', port=8082): Read timed ` | 9999 |
| Close button (top-right) | `parse_and_reason` | `parse_and_reason` | 12 |
| File Explorer icon | `parse_and_reason` | `parse_and_reason` | 100 |
| Search icon | `parse_and_reason` | `parse_and_reason` | 123 |
| Start button | `point_and_act` | `taskbar_list` | 47 |

## Router decisions (Phase 3.5 §13 contract)

| Task | Expected | Actual | Pass |
|---|---|---|:---:|
| Click the Start button | `single_shot` | `single_shot` | [OK] |
| Tap the play button on Spotify | `single_shot` | `single_shot` | [OK] |
| Open Notepad and type Hello | `multi_step` | `multi_step` | [OK] |
| Open Spotify and play Sgt Pepper | `multi_step` | `multi_step` | [OK] |
| list all clickable elements on screen | `enumerate` | `enumerate` | [OK] |
| what's on screen right now? | `enumerate` | `enumerate` | [OK] |
