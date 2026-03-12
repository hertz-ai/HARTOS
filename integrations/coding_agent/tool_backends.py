"""
Coding Agent Tool Backends — KiloCode, Claude Code, OpenCode, Aider Native.

Subprocess backends wrap CLI tools via subprocess. AiderNativeBackend runs
in-process using vendored Aider modules for zero-latency code intelligence.

The orchestrator calls exactly ONE backend per task (never all three).
This is a leaf tool — never re-dispatches to /chat.
"""
import json
import logging
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.coding_agent')


class CodingToolBackend(ABC):
    """Base class for coding tool subprocess wrappers."""

    name: str = ''
    binary: str = ''
    strengths: List[str] = []

    def is_installed(self) -> bool:
        return shutil.which(self.binary) is not None

    @abstractmethod
    def build_command(self, task: str, context: Optional[Dict] = None) -> List[str]:
        """Build the CLI command for execution."""

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict:
        """Parse subprocess output into structured result."""

    def get_capabilities(self) -> Dict:
        return {
            'name': self.name,
            'binary': self.binary,
            'installed': self.is_installed(),
            'strengths': self.strengths,
        }

    def get_env(self) -> Dict[str, str]:
        """Build environment for subprocess.

        For 'own' tasks: passes through all API keys.
        For 'hive'/'idle' tasks: strips metered API keys unless the node
        operator explicitly opted in via compute policy (fail-closed).
        """
        env = os.environ.copy()
        task_source = os.environ.get('_CURRENT_TASK_SOURCE', 'own')
        allow_metered = True

        if task_source in ('hive', 'idle'):
            try:
                from integrations.agent_engine.compute_config import get_compute_policy
                policy = get_compute_policy(os.environ.get('HEVOLVE_NODE_ID'))
                allow_metered = policy.get('allow_metered_for_hive', False)
            except ImportError:
                allow_metered = False  # Fail-closed

        metered_keys = ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GROQ_API_KEY',
                        'GOOGLE_API_KEY', 'OPENROUTER_API_KEY')
        for key in metered_keys:
            val = os.environ.get(key)
            if val and (allow_metered or task_source == 'own'):
                env[key] = val
            else:
                env.pop(key, None)

        return env

    def execute(self, task: str, context: Optional[Dict] = None,
                timeout: int = 300) -> Dict:
        """Execute a coding task via subprocess.

        This is a TERMINAL operation — calls external CLI process,
        never re-dispatches to /chat or creates new agents.

        Returns:
            {success, output, tool, execution_time_s, error?}
        """
        if not self.is_installed():
            return {
                'success': False,
                'output': '',
                'tool': self.name,
                'execution_time_s': 0,
                'error': f'{self.name} not installed',
            }

        cmd = self.build_command(task, context)
        logger.info(f"Executing {self.name}: {cmd[0]} ...")

        start = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.get_env(),
                cwd=context.get('working_dir') if context else None,
            )
            elapsed = time.time() - start
            parsed = self.parse_output(result.stdout, result.stderr, result.returncode)
            parsed['tool'] = self.name
            parsed['execution_time_s'] = round(elapsed, 2)
            return parsed

        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            return {
                'success': False,
                'output': '',
                'tool': self.name,
                'execution_time_s': round(elapsed, 2),
                'error': f'Timeout after {timeout}s',
            }
        except (OSError, FileNotFoundError) as e:
            return {
                'success': False,
                'output': '',
                'tool': self.name,
                'execution_time_s': 0,
                'error': str(e),
            }


class KiloCodeBackend(CodingToolBackend):
    """KiloCode CLI wrapper — Apache 2.0 licensed."""

    name = 'kilocode'
    binary = 'kilocode'
    strengths = ['app_building', 'model_gateway', 'ide_integration', 'multi_provider']

    def build_command(self, task: str, context: Optional[Dict] = None) -> List[str]:
        cmd = [self.binary, '--auto', '--json-io']
        if context and context.get('model'):
            cmd.extend(['--model', context['model']])
        cmd.extend(['--prompt', task])
        return cmd

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict:
        try:
            data = json.loads(stdout)
            return {
                'success': returncode == 0,
                'output': data.get('result', data.get('output', stdout)),
                'metadata': data,
            }
        except (json.JSONDecodeError, ValueError):
            return {
                'success': returncode == 0,
                'output': stdout or stderr,
            }


class ClaudeCodeBackend(CodingToolBackend):
    """Claude Code CLI wrapper — Proprietary (Anthropic Commercial ToS).

    User must install themselves and provide their own ANTHROPIC_API_KEY.
    """

    name = 'claude_code'
    binary = 'claude'
    strengths = ['code_review', 'debugging', 'terminal_workflows', 'complex_reasoning']

    def build_command(self, task: str, context: Optional[Dict] = None) -> List[str]:
        cmd = [self.binary, '-p', task, '--output-format', 'json', '--print']
        if context and context.get('model'):
            cmd.extend(['--model', context['model']])
        return cmd

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict:
        try:
            data = json.loads(stdout)
            # Claude Code JSON output has a 'result' field
            output_text = data.get('result', '')
            if not output_text and isinstance(data, list):
                # Array format: extract text from content blocks
                output_text = '\n'.join(
                    item.get('text', '') for item in data
                    if isinstance(item, dict) and item.get('type') == 'text'
                )
            return {
                'success': returncode == 0,
                'output': output_text or stdout,
                'metadata': data,
            }
        except (json.JSONDecodeError, ValueError):
            return {
                'success': returncode == 0,
                'output': stdout or stderr,
            }


class OpenCodeBackend(CodingToolBackend):
    """OpenCode CLI wrapper — MIT licensed."""

    name = 'opencode'
    binary = 'opencode'
    strengths = ['multi_session', 'lsp_integration', 'refactoring', 'session_sharing']

    def build_command(self, task: str, context: Optional[Dict] = None) -> List[str]:
        cmd = [self.binary, '-p', task, '-f', 'json']
        if context and context.get('model'):
            cmd.extend(['--model', context['model']])
        return cmd

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> Dict:
        try:
            data = json.loads(stdout)
            return {
                'success': returncode == 0,
                'output': data.get('result', data.get('output', stdout)),
                'metadata': data,
            }
        except (json.JSONDecodeError, ValueError):
            return {
                'success': returncode == 0,
                'output': stdout or stderr,
            }


# Lazy import for AiderNativeBackend to avoid hard dependency
def _get_aider_native_class():
    from .aider_native_backend import AiderNativeBackend
    return AiderNativeBackend


class _LazyAiderNative:
    """Lazy proxy so BACKENDS dict doesn't force-import aider_core at module load."""

    _cls = None

    def __call__(self):
        if self._cls is None:
            try:
                self._cls = _get_aider_native_class()
            except ImportError:
                return None
        return self._cls()

    def __eq__(self, other):
        return False  # Never matches shutil.which checks


# Registry of all backends
BACKENDS = {
    'kilocode': KiloCodeBackend,
    'claude_code': ClaudeCodeBackend,
    'opencode': OpenCodeBackend,
    'aider_native': _LazyAiderNative(),
}


def get_available_backends() -> Dict[str, CodingToolBackend]:
    """Return instantiated backends for installed tools only."""
    result = {}
    for name, cls in BACKENDS.items():
        instance = cls()
        if instance is not None and instance.is_installed():
            result[name] = instance
    return result


def get_all_backends() -> Dict[str, CodingToolBackend]:
    """Return all backend instances regardless of installation."""
    result = {}
    for name, cls in BACKENDS.items():
        instance = cls()
        if instance is not None:
            result[name] = instance
    return result
