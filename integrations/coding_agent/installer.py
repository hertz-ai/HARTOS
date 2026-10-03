"""
Coding Agent Tool Installer — Detection and installation of external CLI coding tools.

Detects and installs KiloCode, Claude Code, OpenCode, pi and Hermes.
All tools are installed on the user's machine (never bundled/redistributed):
npm for most, the official install script for Hermes (see SCRIPT_INSTALLS).

Licenses:
    KiloCode   — Apache 2.0 (npm: @kilocode/cli)
    Claude Code — Proprietary/Anthropic Commercial ToS (npm: @anthropic-ai/claude-code)
    OpenCode   — MIT (npm: opencode-ai)
    pi         — MIT (npm: @mariozechner/pi-coding-agent)
    Hermes     — MIT (Nous Research; official install script)
"""
import logging
import os
from core.subprocess_safe import no_window_kwargs
import shutil
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.coding_agent')

# Tool registry: name → (binary_name, package, license)
# binary_name='' means in-process (no external binary); for a tool in
# SCRIPT_INSTALLS, package is a display label, not an npm name
TOOL_REGISTRY = {
    'kilocode': ('kilocode', '@kilocode/cli', 'Apache-2.0'),
    'claude_code': ('claude', '@anthropic-ai/claude-code', 'Proprietary'),
    'opencode': ('opencode', 'opencode-ai', 'MIT'),
    'pi': ('pi', '@mariozechner/pi-coding-agent', 'MIT'),
    'hermes': ('hermes', 'hermes-agent (official installer)', 'MIT'),
    'aider_native': ('', 'tree-sitter tree-sitter-language-pack grep-ast diskcache diff-match-patch gitpython', 'Apache-2.0'),
}


# Tools published only as an official install script, not an npm package.
# HARTOS does not pipe a remote script into a shell on the user's behalf;
# install() returns the command so the user runs it themselves.
SCRIPT_INSTALLS = {
    'hermes': {
        'win32': 'iex (irm https://hermes-agent.nousresearch.com/install.ps1)',
        'other': 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash',
    },
}


def resolved_argv(cmd: List[str]) -> List[str]:
    """cmd with argv[0] replaced by the path shutil.which resolves.

    npm installs npm itself and kilocode/opencode/pi as .cmd shims; on
    Windows a bare name in an argv list is FileNotFoundError (CreateProcess
    ignores PATHEXT).  Every coding-tool launch goes through this.
    """
    return [shutil.which(cmd[0]) or cmd[0]] + list(cmd[1:])


def detect_installed() -> Dict[str, bool]:
    """Check which coding tools are available."""
    result = {}
    for name, (binary, _, _) in TOOL_REGISTRY.items():
        if not binary:
            # In-process backend — check Python import
            try:
                from .aider_native_backend import _check_aider_core
                result[name] = _check_aider_core()
            except ImportError:
                result[name] = False
        else:
            result[name] = shutil.which(binary) is not None
    return result


def get_versions() -> Dict[str, Optional[str]]:
    """Get version strings for installed tools."""
    versions = {}
    for name, (binary, _, _) in TOOL_REGISTRY.items():
        if not shutil.which(binary):
            versions[name] = None
            continue
        try:
            result = subprocess.run(
                resolved_argv([binary, '--version']),
                capture_output=True, text=True, timeout=10,
             **no_window_kwargs())
            versions[name] = result.stdout.strip() or result.stderr.strip() or 'unknown'
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            versions[name] = 'installed (version unknown)'
    return versions


def install(tool_name: str) -> Dict:
    """Install a CLI coding tool: npm install -g, or for a tool in
    SCRIPT_INSTALLS, return its official install command for the user.

    The user is installing the tool on their own machine.
    HARTOS never bundles or redistributes these tools.
    """
    if tool_name not in TOOL_REGISTRY:
        return {'success': False, 'error': f'Unknown tool: {tool_name}'}

    binary, package, license_type = TOOL_REGISTRY[tool_name]

    if tool_name in SCRIPT_INSTALLS:
        if shutil.which(binary):
            return {'success': True, 'message': f'{tool_name} already installed'}
        import sys
        script = SCRIPT_INSTALLS[tool_name]
        command = script['win32'] if sys.platform == 'win32' else script['other']
        return {
            'success': False,
            'error': f'{tool_name} is installed with its official installer. '
                     f'Run: {command}',
        }

    # Check npm availability
    if not shutil.which('npm'):
        return {
            'success': False,
            'error': 'npm not found. Install Node.js first: https://nodejs.org/',
        }

    # Already installed?
    if shutil.which(binary):
        return {'success': True, 'message': f'{tool_name} already installed'}

    logger.info(f"Installing {tool_name} ({package}, license: {license_type})")
    try:
        result = subprocess.run(
            resolved_argv(['npm', 'install', '-g', package]),
            capture_output=True, text=True, timeout=120,
         **no_window_kwargs())
        if result.returncode == 0:
            return {'success': True, 'message': f'{tool_name} installed successfully'}
        else:
            return {'success': False, 'error': result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'Installation timed out (120s)'}
    except OSError as e:
        return {'success': False, 'error': str(e)}


def pip_install(packages: str) -> Dict:
    """Install Python packages via pip.

    Used for in-process backends (aider_native) that need pip dependencies
    rather than npm.
    """
    import sys
    pkg_list = packages.split()
    logger.info(f"pip installing: {pkg_list}")
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install'] + pkg_list,
            capture_output=True, text=True, timeout=120,
         **no_window_kwargs())
        if result.returncode == 0:
            return {'success': True, 'message': f'Installed: {", ".join(pkg_list)}'}
        else:
            return {'success': False, 'error': result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'pip install timed out (120s)'}
    except OSError as e:
        return {'success': False, 'error': str(e)}


def install_tool(tool_name: str) -> Dict:
    """Install a coding tool — routes to npm or pip based on tool type."""
    if tool_name not in TOOL_REGISTRY:
        return {'success': False, 'error': f'Unknown tool: {tool_name}'}

    binary, package, _ = TOOL_REGISTRY[tool_name]
    if not binary:
        # In-process tool — use pip
        return pip_install(package)
    else:
        # CLI tool — npm, or the official script command (SCRIPT_INSTALLS)
        return install(tool_name)


def get_tool_info() -> Dict:
    """Full tool information for API / Nunba settings UI."""
    installed = detect_installed()
    versions = get_versions()
    info = {}
    for name, (binary, package, license_type) in TOOL_REGISTRY.items():
        info[name] = {
            'installed': installed.get(name, False),
            'version': versions.get(name),
            'binary': binary or '(in-process)',
            'package': package,
            'license': license_type,
            'type': 'native' if not binary else 'subprocess',
        }
    return info
