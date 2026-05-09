"""
Minimal run_cmd shim — replaces Aider's run_cmd.py (which needs pexpect/psutil).

Provides subprocess execution using only stdlib.
"""
import os
import subprocess
import sys


def run_cmd_subprocess(cmd, cwd=None, env=None, timeout=60):
    """Run a command via subprocess, returning (exit_code, stdout).

    Simplified version of Aider's run_cmd that doesn't require pexpect or psutil.
    """
    try:
        # Hide the cmd console on Windows when aider runs flake8/black/etc
        # — those binaries pop a brief console per invocation otherwise.
        try:
            from core.subprocess_safe import hidden_popen_kwargs
            _hide = hidden_popen_kwargs()
        except Exception:
            _hide = {}
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env or os.environ.copy(),
            timeout=timeout,
            **_hide,
        )
        combined = result.stdout
        if result.stderr:
            combined += '\n' + result.stderr
        return result.returncode, combined
    except subprocess.TimeoutExpired:
        return 1, f"Command timed out after {timeout}s"
    except (OSError, FileNotFoundError) as e:
        return 1, str(e)
