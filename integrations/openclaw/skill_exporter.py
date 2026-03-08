"""
Skill Exporter — Export HART recipes as ClawHub-compatible SKILL.md files.

This is the reverse direction: HART OS recipes become OpenClaw skills,
publishable to ClawHub for the 3,200+ skill ecosystem to use.

Any trained HART recipe can be exported:
  recipe → SKILL.md + supporting files → clawhub publish
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def recipe_to_skill_md(recipe_path: str,
                       name: Optional[str] = None,
                       description: Optional[str] = None,
                       version: str = '1.0.0') -> str:
    """Convert a HART recipe JSON to a ClawHub SKILL.md.

    Args:
        recipe_path: Path to the recipe JSON file
        name: Skill name (defaults to recipe prompt)
        description: Skill description
        version: Semver version

    Returns:
        SKILL.md content string
    """
    with open(recipe_path, 'r', encoding='utf-8') as f:
        recipe = json.load(f)

    # Extract recipe metadata
    prompt = recipe.get('prompt', recipe.get('task', ''))
    actions = recipe.get('actions', recipe.get('steps', []))
    persona = recipe.get('persona', '')

    if not name:
        # Generate name from prompt
        name = prompt.lower().replace(' ', '-')[:40]
        name = ''.join(c for c in name if c.isalnum() or c == '-')
        name = f"hart-{name}"

    if not description:
        description = f"HART OS trained recipe: {prompt[:100]}"

    # Build frontmatter
    metadata = {
        'openclaw': {
            'emoji': '\U0001f916',
            'requires': {
                'env': ['OPENAI_API_KEY'],
            },
            'primaryEnv': 'OPENAI_API_KEY',
        }
    }

    lines = [
        '---',
        f'name: {name}',
        f'description: {description}',
        f'version: {version}',
        f'homepage: https://github.com/hevolve/hartos',
        f'metadata: {json.dumps(metadata)}',
        '---',
        '',
        f'# {name}',
        '',
        f'{description}',
        '',
        '## Instructions',
        '',
    ]

    if persona:
        lines.append(f'You are acting as: **{persona}**')
        lines.append('')

    lines.append(f'Original task: {prompt}')
    lines.append('')

    # Convert recipe actions to step-by-step instructions
    if actions:
        lines.append('## Steps')
        lines.append('')
        for i, action in enumerate(actions, 1):
            if isinstance(action, dict):
                action_desc = action.get('action', action.get('description', ''))
                tool = action.get('tool', action.get('action_type', ''))
                expected = action.get('expected_output', '')
                lines.append(f'{i}. **{action_desc}**')
                if tool:
                    lines.append(f'   - Tool: `{tool}`')
                if expected:
                    lines.append(f'   - Expected: {expected}')
            else:
                lines.append(f'{i}. {action}')
        lines.append('')

    lines.extend([
        '## Source',
        '',
        'This skill was generated from a trained HART OS recipe.',
        'It can be replayed without LLM calls using HART REUSE mode.',
        '',
        '---',
        '*Exported by HART OS Recipe-to-Skill bridge*',
    ])

    return '\n'.join(lines)


def export_recipe_as_skill(recipe_path: str,
                           output_dir: str,
                           name: Optional[str] = None,
                           description: Optional[str] = None,
                           version: str = '1.0.0') -> str:
    """Export a HART recipe as a complete ClawHub skill directory.

    Args:
        recipe_path: Path to the recipe JSON
        output_dir: Directory to create the skill in
        name: Optional skill name
        description: Optional description
        version: Semver version

    Returns:
        Path to the created skill directory
    """
    skill_md = recipe_to_skill_md(recipe_path, name, description, version)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    (out / 'SKILL.md').write_text(skill_md, encoding='utf-8')

    # Copy the original recipe as reference
    recipe_dest = out / 'hart_recipe.json'
    with open(recipe_path, 'r', encoding='utf-8') as f:
        recipe_data = json.load(f)
    with open(recipe_dest, 'w', encoding='utf-8') as f:
        json.dump(recipe_data, f, indent=2)

    logger.info("Exported recipe %s as skill at %s", recipe_path, out)
    return str(out)


def publish_skill(skill_dir: str, slug: Optional[str] = None) -> bool:
    """Publish a skill to ClawHub (requires clawhub CLI).

    Args:
        skill_dir: Path to the skill directory containing SKILL.md
        slug: Optional slug override

    Returns:
        True if published successfully
    """
    import shutil
    import subprocess

    clawhub = shutil.which('clawhub')
    if not clawhub:
        logger.error("clawhub CLI not installed — cannot publish")
        return False

    cmd = [clawhub, 'publish', skill_dir]
    if slug:
        cmd.extend(['--slug', slug])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            logger.info("Published skill from %s", skill_dir)
            return True
        logger.error("Publish failed: %s", result.stderr)
        return False
    except Exception as e:
        logger.error("Publish error: %s", e)
        return False
