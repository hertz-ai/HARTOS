"""
Aider Native Backend — In-process coding backend using vendored Aider modules.

Unlike KiloCode/ClaudeCode/OpenCode backends which shell out via subprocess,
this backend runs Aider's code intelligence in-process for:
- Zero-latency startup (no subprocess spawn)
- Direct access to repo map, edit diffs, linting
- Recipe integration (edit results flow into HARTOS recipe pattern)
- Budget gate integration (metered usage tracking)

Requires: tree-sitter, tree-sitter-language-pack, grep-ast, diskcache,
          diff-match-patch, gitpython (all in requirements.txt)
"""
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from .tool_backends import CodingToolBackend

logger = logging.getLogger('hevolve.coding_agent')

# Lazy import flag — set on first is_installed() check
_AIDER_CORE_AVAILABLE = None


def _check_aider_core():
    """Check if vendored aider_core modules are importable."""
    global _AIDER_CORE_AVAILABLE
    if _AIDER_CORE_AVAILABLE is None:
        try:
            from .aider_core.repomap import RepoMap
            from .aider_core.coders.search_replace import flexible_search_and_replace
            from .aider_core.io_adapter import SimpleIO
            from .aider_core.hart_model_adapter import HartModelAdapter
            _AIDER_CORE_AVAILABLE = True
        except ImportError as e:
            logger.debug(f"Aider core not available: {e}")
            _AIDER_CORE_AVAILABLE = False
    return _AIDER_CORE_AVAILABLE


class AiderNativeBackend(CodingToolBackend):
    """In-process Aider backend using vendored modules.

    This is NOT a subprocess wrapper — it runs Aider's code intelligence
    directly in the HARTOS Python process.
    """

    name = 'aider_native'
    binary = ''  # No external binary needed
    strengths = [
        'code_review', 'refactoring', 'multi_file_edit',
        'repo_understanding', 'architecture', 'debugging',
    ]

    def is_installed(self) -> bool:
        """Check if vendored aider_core modules are available."""
        return _check_aider_core()

    def build_command(self, task: str, context: Optional[Dict] = None) -> List[str]:
        """Not used — this backend runs in-process, not subprocess."""
        return []

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict:
        """Not used — this backend runs in-process, not subprocess."""
        return {'success': True, 'output': stdout}

    def get_capabilities(self) -> Dict:
        caps = super().get_capabilities()
        caps['type'] = 'native'
        caps['features'] = ['repo_map', 'search_replace', 'linting', 'recipe_capture']
        return caps

    def execute(self, task: str, context: Optional[Dict] = None,
                timeout: int = 300) -> Dict:
        """Execute a coding task using in-process Aider intelligence.

        This is a TERMINAL operation — runs code analysis/editing in-process.
        Never re-dispatches to /chat or creates new agents.

        Returns:
            {success, output, tool, execution_time_s, repo_map?, files_changed?, error?}
        """
        if not self.is_installed():
            return {
                'success': False,
                'output': '',
                'tool': self.name,
                'execution_time_s': 0,
                'error': 'Aider core not available (missing dependencies)',
            }

        start = time.time()
        try:
            result = self._execute_task(task, context or {})
            elapsed = time.time() - start
            result['tool'] = self.name
            result['execution_time_s'] = round(elapsed, 2)
            return result
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"Aider native execution failed: {e}", exc_info=True)
            return {
                'success': False,
                'output': '',
                'tool': self.name,
                'execution_time_s': round(elapsed, 2),
                'error': str(e),
            }

    def _execute_task(self, task: str, context: Dict) -> Dict:
        """Core task execution logic."""
        from .aider_core.hart_model_adapter import HartModelAdapter, send_completion
        from .aider_core.io_adapter import SimpleIO
        from .aider_core.coders.search_replace import (
            flexible_search_and_replace, editblock_strategies,
        )

        working_dir = context.get('working_dir', '.')
        files = context.get('files', [])
        task_type = context.get('task_type', 'feature')

        io = SimpleIO()
        model = HartModelAdapter.from_hartos_config()

        # Build repo map for context (the key differentiator)
        repo_map_text = ''
        if not files:
            # Auto-discover relevant files via repo map
            repo_map_text = self._get_repo_map(working_dir, io, model, files)

        # Build system prompt based on task type
        system_prompt = self._build_system_prompt(task_type, repo_map_text)

        # Send to LLM
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': task},
        ]

        # Add file contents if specified
        if files:
            file_contents = self._read_files(files, working_dir)
            if file_contents:
                messages.insert(1, {
                    'role': 'user',
                    'content': f"Here are the files to work with:\n\n{file_contents}",
                })

        model_name = context.get('model', '')
        user_id = context.get('user_id', '')
        response = send_completion(
            messages, model=model_name, user_id=user_id,
        )

        if response is None:
            return {
                'success': False,
                'output': '',
                'error': 'LLM completion failed',
            }

        # Parse edit blocks from response and apply them
        applied_edits = self._apply_edits(response, working_dir, files)

        output_parts = [response]
        if applied_edits:
            output_parts.append(
                f"\n--- Applied {len(applied_edits)} edit(s) ---"
            )
            for edit in applied_edits:
                output_parts.append(f"  {edit['file']}: {edit['status']}")

        return {
            'success': True,
            'output': '\n'.join(output_parts),
            'repo_map': repo_map_text[:2000] if repo_map_text else '',
            'files_changed': [e['file'] for e in applied_edits if e['status'] == 'applied'],
            'edits': applied_edits,
        }

    def get_repo_map(self, working_dir: str = '.', files: Optional[List[str]] = None,
                     max_tokens: int = 2048) -> str:
        """Get tree-sitter based repo map for a directory.

        Exposed for use by other HARTOS components (e.g., AutoGen tool).

        Args:
            working_dir: Root directory to map
            files: Optional list of files to focus on (chat files)
            max_tokens: Maximum tokens for the map

        Returns:
            Formatted repo map string with function/class signatures.
        """
        if not self.is_installed():
            return 'Repo map unavailable (aider core not installed)'

        from .aider_core.io_adapter import SimpleIO
        from .aider_core.hart_model_adapter import HartModelAdapter

        io = SimpleIO()
        model = HartModelAdapter.from_hartos_config()
        return self._get_repo_map(working_dir, io, model, files or [], max_tokens)

    def _get_repo_map(self, working_dir: str, io, model, chat_files: List[str],
                      max_tokens: int = 2048) -> str:
        """Internal: generate repo map using vendored RepoMap."""
        try:
            from .aider_core.repomap import RepoMap

            abs_dir = str(Path(working_dir).resolve())
            rm = RepoMap(
                root=abs_dir,
                io=io,
                main_model=model,
                map_tokens=max_tokens,
            )

            # Collect all source files in directory
            other_files = []
            for root, dirs, filenames in os.walk(abs_dir):
                # Skip hidden dirs and common non-source dirs
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in (
                    'node_modules', '__pycache__', 'venv', '.git', 'dist', 'build',
                )]
                for fname in filenames:
                    fpath = os.path.join(root, fname)
                    other_files.append(fpath)

            # Resolve chat files to absolute paths
            abs_chat = [str(Path(f).resolve()) for f in chat_files]

            # Remove chat files from other_files
            other_files = [f for f in other_files if f not in abs_chat]

            repo_map = rm.get_repo_map(
                chat_files=abs_chat,
                other_files=other_files,
            )
            return repo_map or ''

        except Exception as e:
            logger.warning(f"Repo map generation failed: {e}")
            return ''

    def _build_system_prompt(self, task_type: str, repo_map: str) -> str:
        """Build system prompt for LLM based on task type."""
        prompt = (
            "You are an expert coding assistant. "
            "When making code changes, use SEARCH/REPLACE blocks:\n\n"
            "```\n"
            "<<<<<<< SEARCH\n"
            "exact code to find\n"
            "=======\n"
            "replacement code\n"
            ">>>>>>> REPLACE\n"
            "```\n\n"
            "Always include the filename before each block as: `filename.py`\n"
        )

        if task_type == 'code_review':
            prompt += "\nFocus on code quality, bugs, security issues, and improvements.\n"
        elif task_type == 'refactor':
            prompt += "\nFocus on improving code structure without changing behavior.\n"
        elif task_type == 'bug_fix':
            prompt += "\nFocus on identifying and fixing the bug described.\n"

        if repo_map:
            prompt += f"\n## Repository structure:\n{repo_map}\n"

        return prompt

    def _read_files(self, files: List[str], working_dir: str) -> str:
        """Read file contents for inclusion in prompt."""
        parts = []
        for fname in files[:10]:  # Cap at 10 files
            fpath = Path(working_dir) / fname
            try:
                content = fpath.read_text(encoding='utf-8', errors='replace')
                parts.append(f"### {fname}\n```\n{content}\n```\n")
            except (OSError, UnicodeDecodeError):
                parts.append(f"### {fname}\n(could not read)\n")
        return '\n'.join(parts)

    def _apply_edits(self, response: str, working_dir: str,
                     files: List[str]) -> List[Dict]:
        """Parse SEARCH/REPLACE blocks from LLM response and apply them."""
        from .aider_core.coders.search_replace import (
            flexible_search_and_replace, editblock_strategies,
        )

        edits = self._parse_edit_blocks(response)
        results = []

        for edit in edits:
            fname = edit['file']
            fpath = Path(working_dir) / fname

            if not fpath.exists():
                results.append({'file': fname, 'status': 'skipped', 'reason': 'file not found'})
                continue

            try:
                original = fpath.read_text(encoding='utf-8', errors='replace')
                texts = (edit['search'], edit['replace'], original)
                new_text = flexible_search_and_replace(texts, editblock_strategies)

                if new_text and new_text != original:
                    fpath.write_text(new_text, encoding='utf-8')
                    results.append({
                        'file': fname,
                        'status': 'applied',
                        'search': edit['search'][:100],
                        'replace': edit['replace'][:100],
                    })
                    logger.info(f"Applied edit to {fname}")
                elif new_text == original:
                    results.append({'file': fname, 'status': 'no_change'})
                else:
                    results.append({'file': fname, 'status': 'failed', 'reason': 'search text not found'})
            except Exception as e:
                results.append({'file': fname, 'status': 'error', 'reason': str(e)})

        return results

    @staticmethod
    def _parse_edit_blocks(response: str) -> List[Dict]:
        """Parse SEARCH/REPLACE blocks from LLM response.

        Expected format:
            `filename.py`
            <<<<<<< SEARCH
            exact code to find
            =======
            replacement code
            >>>>>>> REPLACE
        """
        import re

        blocks = []
        # Match filename followed by search/replace block
        pattern = re.compile(
            r'`([^`]+\.\w+)`\s*\n'
            r'<<<<<<< SEARCH\n'
            r'(.*?)\n'
            r'=======\n'
            r'(.*?)\n'
            r'>>>>>>> REPLACE',
            re.DOTALL,
        )

        for match in pattern.finditer(response):
            blocks.append({
                'file': match.group(1).strip(),
                'search': match.group(2),
                'replace': match.group(3),
            })

        return blocks
