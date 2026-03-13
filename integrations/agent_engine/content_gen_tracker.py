"""
Content Generation Tracker — per-game content gen task tracking.

Links games to AgentGoal (goal_type='content_gen') with SmartLedger subtasks.
Tracks progress snapshots for 24h delta computation. Detects stuck jobs.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger('hevolve_social')

# Media types that a game can require
MEDIA_TYPES = ('image', 'tts', 'music', 'video')

# Max snapshot history (7 days of hourly snapshots)
MAX_SNAPSHOTS = 168


class ContentGenTracker:
    """Track content generation tasks per game using AgentGoal + SmartLedger."""

    @staticmethod
    def get_or_create_game_goal(db, game_id: str, game_config: dict) -> Optional[Dict]:
        """Find existing content_gen goal for game or create one.

        Uses config_json.game_id for lookup. Idempotent.

        Args:
            db: SQLAlchemy session
            game_id: Unique game identifier (e.g. 'eng-spell-animals-01')
            game_config: Game configuration dict with content requirements

        Returns:
            Goal dict or None on error
        """
        try:
            from integrations.social.models import AgentGoal
            from .goal_manager import GoalManager

            # Check for existing goal
            existing = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'content_gen',
                AgentGoal.status.in_(['active', 'paused']),
            ).all()

            for goal in existing:
                config = goal.config_json or {}
                if config.get('game_id') == game_id:
                    return goal.to_dict()

            # Compute media requirements from game config
            media_reqs = ContentGenTracker._compute_media_requirements(game_config)

            # Create new goal
            config_json = {
                'game_id': game_id,
                'game_title': game_config.get('title', game_id),
                'media_requirements': media_reqs,
                'progress_snapshots': [],
                'task_jobs': {},  # {task_type: {job_id, status, progress}}
            }

            result = GoalManager.create_goal(
                db,
                goal_type='content_gen',
                title=f"Content generation: {game_config.get('title', game_id)}",
                description=f"Generate media assets for game {game_id}: "
                            f"{json.dumps(media_reqs)}",
                owner_id=None,
                config_json=config_json,
                spark_budget=100,
            )
            if result:
                db.flush()
            return result
        except Exception as e:
            logger.debug(f"ContentGenTracker.get_or_create_game_goal failed: {e}")
            return None

    @staticmethod
    def _compute_media_requirements(game_config: dict) -> Dict:
        """Compute how many media assets a game needs.

        Walks the game config to count image prompts, text for TTS, etc.
        """
        reqs = {'images': 0, 'tts': 0, 'music': 0, 'video': 0}
        content = game_config.get('content', {})

        # Count image prompts
        for key in ('questions', 'words', 'pairs', 'rounds', 'items',
                     'sequences', 'statements', 'sentences', 'scenes'):
            items = content.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        if item.get('imagePrompt') or item.get('image_prompt'):
                            reqs['images'] += 1
                        # Count nested items (e.g., options with images)
                        for sub_key in ('options', 'cards', 'items'):
                            sub_items = item.get(sub_key, [])
                            if isinstance(sub_items, list):
                                for sub in sub_items:
                                    if isinstance(sub, dict) and (
                                            sub.get('imagePrompt') or sub.get('image_prompt')):
                                        reqs['images'] += 1

        # Count TTS text segments
        for key in ('questions', 'words', 'statements', 'sentences'):
            items = content.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        # Each question/word/statement needs TTS for various fields
                        for text_field in ('question', 'text', 'hint', 'word',
                                           'explanation', 'statement'):
                            if item.get(text_field):
                                reqs['tts'] += 1

        # Music: 1 per game if category has BGM
        if game_config.get('category') in ('english', 'math', 'science',
                                            'lifeSkills', 'creativity'):
            reqs['music'] = 1

        return reqs

    @staticmethod
    def get_game_progress(db, game_id: str) -> Optional[Dict]:
        """Get content generation progress for a game.

        Returns:
            {game_id, goal_id, status, progress_pct, delta_24h,
             tasks: [{task_type, job_id, status, progress_pct}],
             media_requirements, created_at, updated_at}
        """
        try:
            from integrations.social.models import AgentGoal

            goals = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'content_gen',
            ).all()

            goal = None
            for g in goals:
                config = g.config_json or {}
                if config.get('game_id') == game_id:
                    goal = g
                    break

            if not goal:
                return None

            config = goal.config_json or {}
            media_reqs = config.get('media_requirements', {})
            task_jobs = config.get('task_jobs', {})
            snapshots = config.get('progress_snapshots', [])

            # Compute overall progress from task_jobs
            total_assets = sum(media_reqs.values())
            completed_assets = 0
            tasks = []

            # Map media_type → requirements key
            _REQ_KEYS = {'image': 'images', 'tts': 'tts', 'music': 'music', 'video': 'video'}

            for media_type in MEDIA_TYPES:
                required = media_reqs.get(_REQ_KEYS.get(media_type, media_type), 0)
                if required == 0:
                    continue
                job_info = task_jobs.get(media_type, {})
                task_progress = job_info.get('progress', 0)
                task_completed = int(required * task_progress / 100) if task_progress else 0
                completed_assets += task_completed
                tasks.append({
                    'task_type': media_type,
                    'job_id': job_info.get('job_id'),
                    'status': job_info.get('status', 'pending'),
                    'progress_pct': task_progress,
                    'required': required,
                    'completed': task_completed,
                    'error': job_info.get('error'),
                    'updated_at': job_info.get('updated_at'),
                })

            progress_pct = round(completed_assets / total_assets * 100, 1) if total_assets else 0

            # Compute 24h delta from snapshots
            delta_24h = ContentGenTracker._compute_delta(snapshots, hours=24)

            return {
                'game_id': game_id,
                'goal_id': goal.id,
                'game_title': config.get('game_title', game_id),
                'status': _classify_status(goal.status, progress_pct, delta_24h),
                'progress_pct': progress_pct,
                'delta_24h': delta_24h,
                'tasks': tasks,
                'media_requirements': media_reqs,
                'created_at': goal.created_at.isoformat() if goal.created_at else None,
                'updated_at': goal.updated_at.isoformat() if goal.updated_at else None,
            }
        except Exception as e:
            logger.debug(f"ContentGenTracker.get_game_progress failed: {e}")
            return None

    @staticmethod
    def _compute_delta(snapshots: list, hours: int = 24) -> float:
        """Compute progress delta from snapshots over the given window."""
        if not snapshots or len(snapshots) < 2:
            return 0.0

        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)
        latest_pct = snapshots[-1].get('pct', 0)

        # Find the snapshot closest to `hours` ago
        best_snap = None
        best_dist = float('inf')
        for snap in snapshots:
            try:
                ts = datetime.fromisoformat(snap['ts'])
                dist = abs((ts - cutoff).total_seconds())
                if dist < best_dist:
                    best_dist = dist
                    best_snap = snap
            except (KeyError, ValueError):
                continue

        if best_snap is None:
            return 0.0

        return round(latest_pct - best_snap.get('pct', 0), 1)

    @staticmethod
    def record_progress_snapshot(db, game_id: str):
        """Append a progress snapshot for delta tracking.

        Called periodically by the daemon. Keeps last MAX_SNAPSHOTS entries.
        """
        try:
            from integrations.social.models import AgentGoal

            goals = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'content_gen',
            ).all()

            for goal in goals:
                config = goal.config_json or {}
                if config.get('game_id') != game_id:
                    continue

                # Get current progress
                progress = ContentGenTracker.get_game_progress(db, game_id)
                if not progress:
                    return

                snapshots = config.get('progress_snapshots', [])
                snapshots.append({
                    'ts': datetime.utcnow().isoformat(),
                    'pct': progress['progress_pct'],
                })

                # Prune old snapshots
                if len(snapshots) > MAX_SNAPSHOTS:
                    snapshots = snapshots[-MAX_SNAPSHOTS:]

                config['progress_snapshots'] = snapshots
                goal.config_json = config
                flag_modified(goal, 'config_json')
                db.flush()
                return
        except Exception as e:
            logger.debug(f"ContentGenTracker.record_progress_snapshot failed: {e}")

    @staticmethod
    def update_task_job(db, game_id: str, media_type: str,
                        job_id: str = None, status: str = None,
                        progress: float = None, error: str = None):
        """Update a specific media task's job info.

        Called by media generation services when jobs start/progress/complete.
        """
        try:
            from integrations.social.models import AgentGoal

            goals = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'content_gen',
            ).all()

            for goal in goals:
                config = goal.config_json or {}
                if config.get('game_id') != game_id:
                    continue

                task_jobs = config.get('task_jobs', {})
                job_info = task_jobs.get(media_type, {})

                if job_id is not None:
                    job_info['job_id'] = job_id
                if status is not None:
                    job_info['status'] = status
                if progress is not None:
                    job_info['progress'] = progress
                if error is not None:
                    job_info['error'] = error
                job_info['updated_at'] = datetime.utcnow().isoformat()

                task_jobs[media_type] = job_info
                config['task_jobs'] = task_jobs
                goal.config_json = config
                flag_modified(goal, 'config_json')
                db.flush()
                return
        except Exception as e:
            logger.debug(f"ContentGenTracker.update_task_job failed: {e}")

    @staticmethod
    def get_stuck_games(db, stall_threshold_hours: int = 24) -> List[Dict]:
        """Find games where progress hasn't changed in stall_threshold_hours."""
        try:
            from integrations.social.models import AgentGoal

            stuck = []
            goals = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'content_gen',
                AgentGoal.status == 'active',
            ).all()

            for goal in goals:
                config = goal.config_json or {}
                game_id = config.get('game_id')
                if not game_id:
                    continue

                progress = ContentGenTracker.get_game_progress(db, game_id)
                if not progress:
                    continue

                # Skip completed games
                if progress['progress_pct'] >= 100:
                    continue

                delta = progress['delta_24h']
                if delta == 0:
                    # Check how long it's been stuck
                    snapshots = config.get('progress_snapshots', [])
                    stuck_hours = 0
                    if len(snapshots) >= 2:
                        try:
                            latest_ts = datetime.fromisoformat(snapshots[-1]['ts'])
                            stuck_hours = (datetime.utcnow() - latest_ts).total_seconds() / 3600
                        except (KeyError, ValueError):
                            pass

                    if stuck_hours >= stall_threshold_hours or len(snapshots) < 2:
                        stuck.append({
                            **progress,
                            'stuck_hours': round(stuck_hours, 1),
                            'stuck_tasks': [t for t in progress['tasks']
                                            if t['status'] in ('failed', 'stuck', 'pending')
                                            and t['progress_pct'] < 100],
                        })
            return stuck
        except Exception as e:
            logger.debug(f"ContentGenTracker.get_stuck_games failed: {e}")
            return []

    @staticmethod
    def attempt_unblock(db, game_id: str) -> Dict:
        """Attempt to unblock a stuck game's content generation.

        Strategy:
        1. Retry failed tasks
        2. Check if media services are running
        3. Restart stalled services
        4. Escalate if retry fails

        Returns:
            {action_taken, success, detail}
        """
        try:
            from integrations.social.models import AgentGoal

            progress = ContentGenTracker.get_game_progress(db, game_id)
            if not progress:
                return {'action_taken': None, 'success': False,
                        'detail': 'Game not found'}

            stuck_tasks = [t for t in progress['tasks']
                           if t['status'] in ('failed', 'stuck')
                           and t['progress_pct'] < 100]

            if not stuck_tasks:
                return {'action_taken': None, 'success': True,
                        'detail': 'No stuck tasks'}

            actions = []
            for task in stuck_tasks:
                media_type = task['task_type']

                # 1. Check if the service is running
                service_ok = _check_media_service(media_type)
                if not service_ok:
                    restarted = _restart_media_service(media_type)
                    actions.append(f'restarted_{media_type}_service'
                                   if restarted else f'failed_restart_{media_type}')
                    if not restarted:
                        continue

                # 2. Retry the task
                ContentGenTracker.update_task_job(
                    db, game_id, media_type,
                    status='retrying', error=None)
                actions.append(f'retry_{media_type}')

            return {
                'action_taken': ', '.join(actions) if actions else None,
                'success': len(actions) > 0,
                'detail': f"Actions: {actions}",
            }
        except Exception as e:
            return {'action_taken': None, 'success': False,
                    'detail': str(e)}

    @staticmethod
    def get_all_game_tasks(db) -> List[Dict]:
        """Get all content_gen goals with per-task breakdown for admin dashboard."""
        try:
            from integrations.social.models import AgentGoal

            result = []
            goals = db.query(AgentGoal).filter(
                AgentGoal.goal_type == 'content_gen',
            ).order_by(AgentGoal.created_at.desc()).all()

            for goal in goals:
                config = goal.config_json or {}
                game_id = config.get('game_id')
                if not game_id:
                    continue

                progress = ContentGenTracker.get_game_progress(db, game_id)
                if progress:
                    result.append(progress)
            return result
        except Exception as e:
            logger.debug(f"ContentGenTracker.get_all_game_tasks failed: {e}")
            return []

    @staticmethod
    def get_services_health() -> Dict:
        """Check health of all media generation services."""
        services = {}
        for svc_name in ('txt2img', 'tts_audio_suite', 'acestep', 'wan2gp', 'ltx2'):
            services[svc_name] = _check_media_service(svc_name)
        return services


def _classify_status(goal_status: str, progress_pct: float, delta_24h: float) -> str:
    """Classify a game's content gen status for display."""
    if goal_status == 'completed' or progress_pct >= 100:
        return 'complete'
    if goal_status == 'paused':
        return 'paused'
    if delta_24h == 0 and progress_pct > 0:
        return 'stuck'
    if 0 < delta_24h < 5:
        return 'slow'
    if progress_pct == 0:
        return 'pending'
    return 'generating'


def _check_media_service(media_type: str) -> bool:
    """Check if a media generation service is available."""
    try:
        from integrations.service_tools.runtime_manager import RuntimeToolManager
        manager = RuntimeToolManager.get_instance()
        tool_map = {
            'image': 'txt2img',
            'txt2img': 'txt2img',
            'tts': 'tts_audio_suite',
            'tts_audio_suite': 'tts_audio_suite',
            'music': 'acestep_generate',
            'acestep': 'acestep_generate',
            'video': 'wan2gp_generate',
            'wan2gp': 'wan2gp_generate',
            'ltx2': 'ltx2_generate',
        }
        tool_name = tool_map.get(media_type, media_type)
        return manager.is_tool_running(tool_name)
    except Exception:
        return False


def _restart_media_service(media_type: str) -> bool:
    """Attempt to restart a media generation service."""
    try:
        from integrations.service_tools.runtime_manager import RuntimeToolManager
        manager = RuntimeToolManager.get_instance()
        tool_map = {
            'image': 'txt2img',
            'tts': 'tts_audio_suite',
            'music': 'acestep_generate',
            'video': 'wan2gp_generate',
        }
        tool_name = tool_map.get(media_type, media_type)
        result = manager.ensure_tool_running(tool_name)
        return result is not None
    except Exception:
        return False
