"""
HevolveSocial - Recipe Sharing
Share and fork trained agent recipes as social posts.
"""
import json
import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger('hevolve_social')

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'prompts')


def load_recipe(recipe_file: str) -> Optional[dict]:
    """Load a recipe JSON file from the prompts directory."""
    path = os.path.join(PROMPTS_DIR, recipe_file)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"Failed to load recipe {recipe_file}: {e}")
        return None


def get_recipe_summary(recipe_file: str) -> dict:
    """Get a summary of a recipe without loading the full content."""
    data = load_recipe(recipe_file)
    if not data:
        return {'error': 'Recipe not found'}

    return {
        'file': recipe_file,
        'persona': data.get('persona', ''),
        'action': data.get('action', ''),
        'steps': len(data.get('recipe', data.get('steps', []))),
        'has_fallback': bool(data.get('fallback_strategy')),
    }


def fork_recipe(recipe_file: str, new_prompt_id: int, new_flow_id: int) -> Optional[str]:
    """Copy a recipe to a new prompt_id/flow_id. Returns the new filename."""
    source = os.path.join(PROMPTS_DIR, recipe_file)
    if not os.path.exists(source):
        return None

    new_name = f"{new_prompt_id}_{new_flow_id}_recipe.json"
    dest = os.path.join(PROMPTS_DIR, new_name)

    if os.path.exists(dest):
        return None  # Don't overwrite existing

    try:
        shutil.copy2(source, dest)
        return new_name
    except Exception as e:
        logger.debug(f"Failed to fork recipe: {e}")
        return None
