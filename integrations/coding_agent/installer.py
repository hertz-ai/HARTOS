"""
Coding Agent Tool Installer — Detection and installation of external CLI coding tools.

Detects and installs KiloCode, Claude Code, and OpenCode.
All tools are installed via npm on the user's machine (never bundled/redistributed).

Licenses:
    KiloCode   — Apache 2.0 (npm: @kilocode/cli)
    Claude Code — Proprietary/Anthropic Commercial ToS (npm: @anthropic-ai/claude-code)
    OpenCode   — MIT (npm: opencode-ai)
"""
import logging
import os
import shutil
import subprocess
from typing import Dict, Optional

logger = logging.getLogger('hevolve.coding_agent')

# Tool registry: name → (binary_name, package, license)
# binary_name='' means in-process (no external binary)
TOOL_REGISTRY = {
    'kilocode': ('kilocode', '@kilocode/cli', 'Apache-2.0'),
    'claude_code': ('claude', '@anthropic-ai/claude-code', 'Proprietary'),
    'opencode': ('opencode', 'opencode-ai', 'MIT'),
    'aider_native': ('', 'tree-sitter tree-sitter-language-pack grep-ast diskcache diff-match-patch gitpython', 'Apache-2.0'),
}


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
                [binary, '--version'],
                capture_output=True, text=True, timeout=10,
            )
            versions[name] = result.stdout.strip() or result.stderr.strip() or 'unknown'
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            versions[name] = 'installed (version unknown)'
    return versions


def install(tool_name: str) -> Dict:
    """Install a coding tool via npm install -g.

    The user is installing the tool on their own machine.
    HARTOS never bundles or redistributes these tools.
    """
    if tool_name not in TOOL_REGISTRY:
        return {'success': False, 'error': f'Unknown tool: {tool_name}'}

    binary, package, license_type = TOOL_REGISTRY[tool_name]

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
            ['npm', 'install', '-g', package],
            capture_output=True, text=True, timeout=120,
        )
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
        )
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
        # CLI tool — use npm
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
