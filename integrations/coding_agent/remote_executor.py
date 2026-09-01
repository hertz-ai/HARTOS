"""
Remote Desktop Executor — CLI bridge to Nunba /execute and /screenshot endpoints.

Handles:
- HTTP dispatch to remote Nunba instance
- Security pre-checks (action_classifier, DLP)
- Screenshot capture
- Local VLM desktop automation (via existing local_loop)
"""
import json
import logging
import os
from typing import Dict, Optional

from core.http_pool import pooled_get, pooled_post
from core.port_registry import get_port

logger = logging.getLogger('hevolve.coding_agent')


class RemoteDesktopExecutor:
    """Bridge CLI commands to Nunba /execute and /screenshot endpoints."""

    def __init__(self, nunba_url: str = f'http://localhost:{get_port("backend")}'):
        self.base_url = nunba_url.rstrip('/')

    def execute(self, command: str, timeout: int = 120,
                force: bool = False) -> Dict:
        """Execute a command on a remote machine via Nunba /execute endpoint.

        Args:
            command: The command to execute
            timeout: Execution timeout in seconds
            force: If True, bypass destructive command check

        Returns:
            {success, output, returncode?, error?}
        """
        # Security pre-check: destructive command detection
        if not force:
            blocked = self._check_security(command)
            if blocked:
                return blocked

        import requests

        try:
            resp = pooled_post(
                f'{self.base_url}/execute',
                json={'command': command, 'timeout': timeout},
                timeout=timeout + 10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    'success': data.get('returncode', 1) == 0,
                    'output': data.get('output', ''),
                    'returncode': data.get('returncode', -1),
                }
            else:
                return {
                    'success': False,
                    'output': '',
                    'error': f'HTTP {resp.status_code}: {resp.text[:200]}',
                }
        except requests.ConnectionError:
            return {
                'success': False,
                'output': '',
                'error': f'Cannot connect to Nunba at {self.base_url}',
            }
        except requests.Timeout:
            return {
                'success': False,
                'output': '',
                'error': f'Request timed out after {timeout}s',
            }
        except Exception as e:
            return {
                'success': False,
                'output': '',
                'error': str(e),
            }

    def screenshot(self) -> Dict:
        """Capture screenshot from remote Nunba /screenshot endpoint.

        Returns:
            {success, image_base64?, content_type?, error?}
        """
        import requests

        try:
            resp = pooled_get(
                f'{self.base_url}/screenshot',
                timeout=30,
            )
            if resp.status_code == 200:
                content_type = resp.headers.get('Content-Type', '')
                if 'json' in content_type:
                    data = resp.json()
                    return {
                        'success': True,
                        'image_base64': data.get('image', data.get('screenshot', '')),
                        'content_type': 'image/png',
                    }
                else:
                    import base64
                    return {
                        'success': True,
                        'image_base64': base64.b64encode(resp.content).decode(),
                        'content_type': content_type,
                    }
            else:
                return {
                    'success': False,
                    'error': f'HTTP {resp.status_code}: {resp.text[:200]}',
                }
        except requests.ConnectionError:
            return {
                'success': False,
                'error': f'Cannot connect to Nunba at {self.base_url}',
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
            }

    def execute_desktop_task(self, instruction: str, target: str = 'local',
                              nunba_url: str = '') -> Dict:
        """Execute a desktop automation task (VLM agentic loop).

        Args:
            instruction: Natural language instruction (e.g., "open Chrome")
            target: 'local' for in-process VLM loop, 'remote' for Nunba dispatch
            nunba_url: Override Nunba URL for remote execution

        Returns:
            {success, output, error?}
        """
        if target == 'remote':
            url = nunba_url or self.base_url
            executor = RemoteDesktopExecutor(url)
            return executor.execute(instruction)

        # Local execution via the SAME canonical entry the other
        # production callers use (reuse/create/hie all go through
        # vlm_adapter, which owns tier selection + circuit breakers).
        # The old direct call run_local_agentic_loop(instruction) was
        # broken twice over: it omitted the required `tier` arg
        # (TypeError) and passed a str where the loop expects the
        # message dict — the bare except below swallowed it, so this
        # leg had NEVER executed locally.
        try:
            from integrations.vlm.vlm_adapter import execute_vlm_instruction
            result = execute_vlm_instruction({
                'instruction_to_vlm_agent': instruction,
                'enhanced_instruction': instruction,
                'user_id': 'coding_agent',
                'prompt_id': 'desktop_task',
                'os_to_control': 'windows',
            })
            if result is None:
                return {
                    'success': False,
                    'output': '',
                    'error': 'no local VLM tier available (Crossbar-only topology)',
                }
            return {
                'success': result.get('status') == 'success',
                'output': json.dumps(result, default=str),
            }
        except ImportError:
            return {
                'success': False,
                'output': '',
                'error': 'VLM pipeline not available (pyautogui not installed)',
            }
        except Exception as e:
            return {
                'success': False,
                'output': '',
                'error': str(e),
            }

    def _check_security(self, command: str) -> Optional[Dict]:
        """Run security checks on command before remote dispatch.

        Returns error dict if blocked, None if OK.

        FAIL SAFE: this gate stands between the coding agent and a remote
        machine's /execute endpoint, so if a security module cannot be
        imported we CANNOT verify the command is non-destructive / PII-free.
        In that case we REFUSE to dispatch rather than silently forwarding an
        unchecked command (the old ``except ImportError: pass`` was fail-OPEN).
        A caller that genuinely needs to bypass the gate passes ``force=True``
        to :meth:`execute`.
        """
        # Action classifier — detect destructive patterns
        try:
            from security.action_classifier import classify_action
            classification = classify_action(command)
            if classification == 'destructive':
                return {
                    'success': False,
                    'output': '',
                    'error': (
                        f'Destructive command detected: {command[:100]}. '
                        'Use --force to override.'
                    ),
                }
        except ImportError as e:
            logger.error(
                'Security pre-check unavailable (action_classifier import '
                'failed: %s); refusing remote dispatch', e)
            return {
                'success': False,
                'output': '',
                'error': (
                    'Security pre-check unavailable: action_classifier could '
                    'not be imported. Refusing to dispatch command. '
                    'Use --force to override.'
                ),
            }

        # DLP — scan for PII before sending to remote
        try:
            from security.dlp_engine import get_dlp_engine
            dlp = get_dlp_engine()
            allowed, reason = dlp.check_outbound(command)
            if not allowed:
                return {
                    'success': False,
                    'output': '',
                    'error': f'DLP blocked: {reason or "PII detected"}',
                }
        except ImportError as e:
            logger.error(
                'Security pre-check unavailable (dlp_engine import failed: '
                '%s); refusing remote dispatch', e)
            return {
                'success': False,
                'output': '',
                'error': (
                    'Security pre-check unavailable: dlp_engine could not be '
                    'imported. Refusing to dispatch command. '
                    'Use --force to override.'
                ),
            }

        return None
