"""
Content Generation AutoGen Tools

4 tools for the content_gen goal type — used by the monitor agent to
check status, retry stuck tasks, inspect services, and force regeneration.
"""
import json
import logging
from typing import Annotated

logger = logging.getLogger('hevolve_social')


def get_content_gen_status(
    game_id: Annotated[str, "Game ID to check (e.g. 'eng-spell-animals-01')"]
) -> str:
    """Get content generation status for a game.

    Returns JSON with per-task breakdown, progress_pct, 24h delta, stuck tasks.
    """
    try:
        from integrations.social.models import get_db
        from .content_gen_tracker import ContentGenTracker

        db = get_db()
        try:
            progress = ContentGenTracker.get_game_progress(db, game_id)
            if not progress:
                return json.dumps({
                    'error': f'No content generation goal found for game {game_id}'
                })
            return json.dumps(progress, default=str)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def retry_stuck_task(
    game_id: Annotated[str, "Game ID with stuck content"],
    task_type: Annotated[str, "Media type to retry: image, tts, music, or video"] = None
) -> str:
    """Retry a stuck content generation task.

    Checks if the service is running, restarts if needed, then retries.
    If task_type is omitted, retries all stuck tasks for the game.
    """
    try:
        from integrations.social.models import get_db
        from .content_gen_tracker import ContentGenTracker

        db = get_db()
        try:
            if task_type:
                ContentGenTracker.update_task_job(
                    db, game_id, task_type,
                    status='retrying', error=None)
                db.commit()
                return json.dumps({
                    'success': True,
                    'detail': f'Retrying {task_type} for game {game_id}'
                })
            else:
                result = ContentGenTracker.attempt_unblock(db, game_id)
                db.commit()
                return json.dumps(result)
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


def check_media_services() -> str:
    """Check health of all media generation services.

    Returns which services are running, which need restart.
    Services: txt2img, tts_audio_suite, acestep, wan2gp, ltx2.
    """
    try:
        from .content_gen_tracker import ContentGenTracker
        health = ContentGenTracker.get_services_health()
        return json.dumps({
            'services': {name: 'running' if ok else 'offline'
                         for name, ok in health.items()},
            'all_healthy': all(health.values()),
        })
    except Exception as e:
        return json.dumps({'error': str(e)})


def force_regenerate(
    game_id: Annotated[str, "Game ID to regenerate content for"],
    asset_type: Annotated[str, "Asset type: image, tts, music, or video"],
    prompt: Annotated[str, "Generation prompt"] = None
) -> str:
    """Force regeneration of a specific asset type for a game.

    Clears the existing job status and triggers a fresh generation.
    """
    try:
        from integrations.social.models import get_db
        from .content_gen_tracker import ContentGenTracker
        import uuid

        db = get_db()
        try:
            new_job_id = f'{asset_type}_{uuid.uuid4().hex[:12]}'
            ContentGenTracker.update_task_job(
                db, game_id, asset_type,
                job_id=new_job_id,
                status='pending',
                progress=0,
                error=None)
            db.commit()
            return json.dumps({
                'success': True,
                'job_id': new_job_id,
                'detail': f'Queued {asset_type} regeneration for {game_id}'
            })
        finally:
            db.close()
    except Exception as e:
        return json.dumps({'error': str(e)})


# Tool registration for ServiceToolRegistry
CONTENT_GEN_TOOLS = [
    {
        'name': 'get_content_gen_status',
        'func': get_content_gen_status,
        'description': 'Get content generation status for a kids learning game',
        'tags': ['content_gen'],
    },
    {
        'name': 'retry_stuck_task',
        'func': retry_stuck_task,
        'description': 'Retry a stuck content generation task for a game',
        'tags': ['content_gen'],
    },
    {
        'name': 'check_media_services',
        'func': check_media_services,
        'description': 'Check health of all media generation services',
        'tags': ['content_gen'],
    },
    {
        'name': 'force_regenerate',
        'func': force_regenerate,
        'description': 'Force regeneration of a specific asset for a game',
        'tags': ['content_gen'],
    },
]
