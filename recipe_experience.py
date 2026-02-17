"""
Recipe Experience Recorder
============================

Records execution telemetry during CREATE/REUSE and merges accumulated
experience back into recipe JSON when a goal completes (all actions TERMINATED).

Next REUSE load benefits from the accumulated experience:
- Avoids dead-end paths
- Uses effective fallback strategies
- Knows expected durations
- Skips tools that previously failed

All methods are fire-and-forget — never raise into the main execution flow.
"""
import json
import os
import time
import threading
import logging
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Thread-safe telemetry storage
_lock = threading.RLock()
_timers: Dict[str, Dict[int, float]] = {}       # user_prompt → {action_id → start_time}
_telemetry: Dict[str, Dict[int, Dict]] = {}      # user_prompt → {action_id → telemetry_dict}


class RecipeExperienceRecorder:
    """Records execution telemetry and merges it into recipe JSON on goal completion."""

    @staticmethod
    def start_action_timer(user_prompt: str, action_id: int):
        """Called when action enters IN_PROGRESS."""
        with _lock:
            if user_prompt not in _timers:
                _timers[user_prompt] = {}
            _timers[user_prompt][action_id] = time.time()

    @staticmethod
    def stop_action_timer(user_prompt: str, action_id: int, outcome: str):
        """Called when action reaches COMPLETED/ERROR/TERMINATED.

        Computes duration and stores in telemetry.
        """
        with _lock:
            start = _timers.get(user_prompt, {}).pop(action_id, None)
            if start is None:
                return
            duration = time.time() - start

            if user_prompt not in _telemetry:
                _telemetry[user_prompt] = {}
            if action_id not in _telemetry[user_prompt]:
                _telemetry[user_prompt][action_id] = _new_telemetry()

            tel = _telemetry[user_prompt][action_id]
            tel['durations'].append(duration)
            tel['outcomes'].append(outcome)
            tel['last_run_at'] = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def record_subtask(user_prompt: str, action_id: int,
                       subtask_desc: str, outcome: str, duration: float):
        """Record a subtask discovered during execution."""
        with _lock:
            tel = _get_or_create_telemetry(user_prompt, action_id)
            tel['subtasks'].append({
                'desc': subtask_desc,
                'outcome': outcome,
                'duration': duration,
            })

    @staticmethod
    def record_fallback_used(user_prompt: str, action_id: int,
                             fallback_action: str, success: bool):
        """Record which fallback strategy was used and whether it worked."""
        with _lock:
            tel = _get_or_create_telemetry(user_prompt, action_id)
            tel['fallbacks_used'].append({
                'action': fallback_action,
                'success': success,
            })

    @staticmethod
    def record_tool_call(user_prompt: str, action_id: int,
                         tool_name: str, success: bool, duration: float):
        """Record tool usage statistics."""
        with _lock:
            tel = _get_or_create_telemetry(user_prompt, action_id)
            if tool_name not in tel['tool_stats']:
                tel['tool_stats'][tool_name] = {'calls': 0, 'successes': 0, 'total_duration': 0}
            stats = tel['tool_stats'][tool_name]
            stats['calls'] += 1
            if success:
                stats['successes'] += 1
            stats['total_duration'] += duration

    @staticmethod
    def record_dead_end(user_prompt: str, action_id: int, path_description: str):
        """Record a path that was explored but led nowhere."""
        with _lock:
            tel = _get_or_create_telemetry(user_prompt, action_id)
            if path_description not in tel['dead_ends']:
                tel['dead_ends'].append(path_description)

    @staticmethod
    def get_telemetry(user_prompt: str) -> Dict[int, Dict]:
        """Get accumulated telemetry for a session (for testing/inspection)."""
        with _lock:
            return dict(_telemetry.get(user_prompt, {}))

    @staticmethod
    def merge_experience_into_recipe(prompt_id: str, flow: int, user_prompt: str):
        """Merge accumulated telemetry into the recipe JSON.

        Called when ALL actions reach TERMINATED (goal met).

        Reads: prompts/{prompt_id}_{flow}_recipe.json
        Adds 'experience' key to each action and 'experience_meta' at top level.
        Writes back atomically.
        """
        recipe_path = f'prompts/{prompt_id}_{flow}_recipe.json'
        if not os.path.exists(recipe_path):
            logger.debug(f"Recipe not found for experience merge: {recipe_path}")
            return

        with _lock:
            session_tel = _telemetry.get(user_prompt, {})
            if not session_tel:
                logger.debug(f"No telemetry for {user_prompt}, skipping merge")
                return

        try:
            with open(recipe_path, 'r') as f:
                recipe = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read recipe for experience merge: {e}")
            return

        # Merge per-action experience
        actions = recipe.get('actions', [])
        total_duration = 0.0
        action_durations = {}

        for action in actions:
            aid = action.get('action_id', 0)
            with _lock:
                tel = session_tel.get(aid, {})
            if not tel:
                continue

            existing_exp = action.get('experience', {})
            run_count = existing_exp.get('run_count', 0) + 1

            # Compute rolling average duration
            new_durations = tel.get('durations', [])
            old_avg = existing_exp.get('avg_duration_seconds', 0)
            old_count = existing_exp.get('run_count', 0)
            all_dur_sum = old_avg * old_count + sum(new_durations)
            avg_dur = all_dur_sum / run_count if run_count > 0 else 0

            # Compute success rate
            outcomes = tel.get('outcomes', [])
            old_successes = int(existing_exp.get('success_rate', 1.0) * old_count)
            new_successes = sum(1 for o in outcomes if o in ('completed', 'terminated'))
            total_outcomes = old_count + len(outcomes)
            success_rate = (old_successes + new_successes) / total_outcomes if total_outcomes > 0 else 1.0

            # Merge fallbacks (keep effective ones — those that succeeded)
            old_fallbacks = existing_exp.get('effective_fallbacks', [])
            new_fallbacks = [
                fb['action'] for fb in tel.get('fallbacks_used', []) if fb['success']
            ]
            effective_fallbacks = list(dict.fromkeys(old_fallbacks + new_fallbacks))[:10]

            # Merge dead ends
            old_dead_ends = existing_exp.get('dead_ends', [])
            new_dead_ends = tel.get('dead_ends', [])
            dead_ends = list(dict.fromkeys(old_dead_ends + new_dead_ends))[:20]

            # Merge subtasks
            old_subtasks = existing_exp.get('subtasks', [])
            new_subtasks = tel.get('subtasks', [])
            subtasks = _merge_subtasks(old_subtasks, new_subtasks)

            # Merge tool stats
            old_tool_stats = existing_exp.get('tool_stats', {})
            new_tool_stats = tel.get('tool_stats', {})
            tool_stats = _merge_tool_stats(old_tool_stats, new_tool_stats)

            action['experience'] = {
                'run_count': run_count,
                'avg_duration_seconds': round(avg_dur, 2),
                'success_rate': round(success_rate, 3),
                'last_run_at': tel.get('last_run_at', existing_exp.get('last_run_at', '')),
                'effective_fallbacks': effective_fallbacks,
                'dead_ends': dead_ends,
                'subtasks': subtasks,
                'tool_stats': tool_stats,
            }

            if new_durations:
                action_durations[aid] = sum(new_durations)
                total_duration += sum(new_durations)

        # Add flow-level experience metadata
        bottleneck = max(action_durations, key=action_durations.get) if action_durations else None
        recipe['experience_meta'] = {
            'last_total_duration_seconds': round(total_duration, 2),
            'last_run_at': datetime.now(timezone.utc).isoformat(),
            'bottleneck_action_id': bottleneck,
            'total_runs': recipe.get('experience_meta', {}).get('total_runs', 0) + 1,
        }

        # Write back atomically (write to tmp, rename)
        try:
            tmp_path = recipe_path + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(recipe, f, indent=2)
            os.replace(tmp_path, recipe_path)
            logger.info(f"Experience merged into {recipe_path}")

            # Capture baseline snapshot on recipe change
            try:
                from integrations.agent_engine.agent_baseline_service import (
                    capture_baseline_async)
                capture_baseline_async(
                    prompt_id=str(prompt_id), flow_id=flow,
                    trigger='recipe_change', user_prompt=user_prompt)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Failed to write experience to recipe: {e}")
            # Clean up tmp file on failure
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        # Clean up session telemetry
        with _lock:
            _telemetry.pop(user_prompt, None)
            _timers.pop(user_prompt, None)

    @staticmethod
    def cleanup_session(user_prompt: str):
        """Clean up telemetry for a session (e.g. on error/abort)."""
        with _lock:
            _telemetry.pop(user_prompt, None)
            _timers.pop(user_prompt, None)


def build_experience_hints(individual_recipes: list) -> str:
    """Build experience context from recipe experience data for REUSE prompts."""
    hints = []
    for recipe in individual_recipes:
        exp = recipe.get('experience', {})
        if not exp:
            continue
        action = recipe.get('action', 'unknown')
        dead_ends = exp.get('dead_ends', [])
        fallbacks = exp.get('effective_fallbacks', [])
        avg_dur = exp.get('avg_duration_seconds', 0)
        success_rate = exp.get('success_rate', 1.0)

        if dead_ends:
            hints.append(f"Action '{action}': AVOID these paths: {'; '.join(dead_ends[:3])}")
        if fallbacks:
            hints.append(f"Action '{action}': If stuck, try: {'; '.join(fallbacks[:2])}")
        if avg_dur > 0:
            hints.append(f"Action '{action}': Expected duration ~{avg_dur:.1f}s")
        if success_rate < 0.7:
            hints.append(f"Action '{action}': Low success rate ({success_rate:.0%}), consider alternatives")

    return '\n'.join(hints) if hints else 'No prior experience recorded.'


# ─── Internal helpers ───

def _new_telemetry() -> Dict:
    """Create a fresh telemetry dict for an action."""
    return {
        'durations': [],
        'outcomes': [],
        'subtasks': [],
        'fallbacks_used': [],
        'tool_stats': {},
        'dead_ends': [],
        'last_run_at': '',
    }


def _get_or_create_telemetry(user_prompt: str, action_id: int) -> Dict:
    """Get or create telemetry dict for an action (caller must hold _lock)."""
    if user_prompt not in _telemetry:
        _telemetry[user_prompt] = {}
    if action_id not in _telemetry[user_prompt]:
        _telemetry[user_prompt][action_id] = _new_telemetry()
    return _telemetry[user_prompt][action_id]


def _merge_subtasks(old: List[Dict], new: List[Dict]) -> List[Dict]:
    """Merge subtask lists, updating avg_duration for known subtasks."""
    by_desc = {}
    for s in old:
        by_desc[s.get('desc', '')] = s
    for s in new:
        desc = s.get('desc', '')
        if desc in by_desc:
            existing = by_desc[desc]
            old_dur = existing.get('avg_duration', existing.get('duration', 0))
            new_dur = s.get('duration', 0)
            existing['avg_duration'] = round((old_dur + new_dur) / 2, 2)
        else:
            by_desc[desc] = {
                'desc': desc,
                'avg_duration': s.get('duration', 0),
            }
    return list(by_desc.values())[:20]


def _merge_tool_stats(old: Dict[str, Dict], new: Dict[str, Dict]) -> Dict[str, Dict]:
    """Merge tool usage statistics."""
    merged = dict(old)
    for tool, stats in new.items():
        if tool in merged:
            m = merged[tool]
            m['calls'] = m.get('calls', 0) + stats.get('calls', 0)
            m['successes'] = m.get('successes', 0) + stats.get('successes', 0)
            m['success_rate'] = round(
                m['successes'] / m['calls'], 3) if m['calls'] > 0 else 0
        else:
            calls = stats.get('calls', 0)
            successes = stats.get('successes', 0)
            merged[tool] = {
                'calls': calls,
                'successes': successes,
                'success_rate': round(successes / calls, 3) if calls > 0 else 0,
            }
    return merged
