"""
Coding Recipe Bridge — Connects Aider edit results to the HARTOS Recipe Pattern.

CREATE mode: Captures file edits as recipe steps with search/replace metadata
REUSE mode:  Replays coding recipes by applying patches directly (no LLM needed)

Falls back to full LLM execution if files have changed since recipe capture.
"""
import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.coding_agent.recipe_bridge')


class CodingRecipeBridge:
    """Bridges coding tool outputs to HARTOS recipe format."""

    @staticmethod
    def capture_edit_as_recipe_step(
        task: str,
        tool_name: str,
        file_edits: List[Dict],
        working_dir: str = '',
    ) -> Dict:
        """Convert coding tool file edits to a HARTOS recipe step.

        Args:
            task: The original task description
            tool_name: Which coding tool produced the edits
            file_edits: List of {filename, original, updated} or
                        {filename, search, replace} dicts
            working_dir: Working directory context

        Returns:
            Recipe step dict compatible with HARTOS recipe format.
        """
        # Normalize edits to search/replace blocks
        sr_blocks = []
        for edit in file_edits:
            filename = edit.get('filename', '')
            if 'search' in edit and 'replace' in edit:
                sr_blocks.append({
                    'filename': filename,
                    'search': edit['search'],
                    'replace': edit['replace'],
                })
            elif 'original' in edit and 'updated' in edit:
                sr_blocks.append({
                    'filename': filename,
                    'search': edit['original'],
                    'replace': edit['updated'],
                })

        return {
            'description': task,
            'tool_name': tool_name,
            'aider_edit_format': 'search_replace',
            'search_replace_blocks': sr_blocks,
            'working_dir': working_dir,
            'files_modified': list({b['filename'] for b in sr_blocks}),
        }

    @staticmethod
    def replay_recipe_step(
        step: Dict,
        working_dir: str = '',
    ) -> Dict:
        """Replay a coding recipe step without LLM — apply search/replace directly.

        Args:
            step: Recipe step dict (must have aider_edit_format + search_replace_blocks)
            working_dir: Override working directory

        Returns:
            {success, files_modified, errors}
        """
        if step.get('aider_edit_format') != 'search_replace':
            return {
                'success': False,
                'error': 'Not a coding recipe step (missing aider_edit_format)',
            }

        blocks = step.get('search_replace_blocks', [])
        if not blocks:
            return {'success': True, 'files_modified': [], 'errors': []}

        base_dir = working_dir or step.get('working_dir', '')
        modified = []
        errors = []

        for block in blocks:
            filename = block.get('filename', '')
            search = block.get('search', '')
            replace = block.get('replace', '')

            if not filename or not search:
                continue

            filepath = os.path.join(base_dir, filename) if base_dir else filename

            if not os.path.exists(filepath):
                errors.append(f'File not found: {filepath}')
                continue

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()

                if search not in content:
                    # Try flexible matching
                    matched = _flexible_patch(content, search, replace)
                    if matched is None:
                        errors.append(
                            f'Search text not found in {filename} (file may have changed)')
                        continue
                    content = matched
                else:
                    content = content.replace(search, replace, 1)

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)

                modified.append(filename)
                logger.info(f'Recipe replay: patched {filename}')

            except Exception as e:
                errors.append(f'Error patching {filename}: {e}')

        return {
            'success': len(errors) == 0,
            'files_modified': modified,
            'errors': errors,
        }

    @staticmethod
    def get_repository_map(
        working_dir: str = '.',
        max_tokens: int = 2048,
    ) -> str:
        """Generate a tree-sitter repository map for coding context.

        Registered as an AutoGen tool so agents can understand repo structure.
        """
        try:
            from integrations.coding_agent.aider_native_backend import AiderNativeBackend
            backend = AiderNativeBackend()
            if not backend.is_installed():
                return 'Repository map not available (aider_core not installed)'
            return backend.get_repo_map(working_dir=working_dir, max_tokens=max_tokens) or ''
        except Exception as e:
            return f'Repository map error: {e}'


def _flexible_patch(content: str, search: str, replace: str) -> Optional[str]:
    """Try flexible search/replace when exact match fails.

    Uses the vendored Aider search_replace if available, otherwise
    falls back to whitespace-normalized matching.
    """
    try:
        from integrations.coding_agent.aider_core.coders.search_replace import (
            flexible_search_and_replace, editblock_strategies,
        )
        texts = (search, replace, content)
        result = flexible_search_and_replace(texts, editblock_strategies)
        return result if result != content else None
    except ImportError:
        pass

    # Fallback: normalize whitespace and try again
    import re
    search_normalized = re.sub(r'\s+', r'\\s+', re.escape(search.strip()))
    match = re.search(search_normalized, content)
    if match:
        return content[:match.start()] + replace + content[match.end():]

    return None
