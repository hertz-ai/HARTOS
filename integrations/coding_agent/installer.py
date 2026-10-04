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
import re
import shutil
import sys
from typing import Dict, List, Optional

from core.subprocess_safe import run_bounded

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


# npm cmd-shim's last line: "%dp0%\<target>" %*  (target = a .js run by node,
# or an .exe).  The node.exe probe line has no %* after it, so it never matches.
_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+)"\s+%\*')
# cmd.exe re-parses a batch file's arguments: these break out of or rewrite
# an argument no matter how Python quotes it (measured: '" & echo X > f'
# created f; a newline truncated the task; %PATH% expanded).
_CMD_UNSAFE = set('"&|<>^%!\r\n')


def launch_argv(cmd: List[str]) -> List[str]:
    """The argv that launches cmd[0] with cmd[1:] reaching it verbatim.

    Every coding-tool launch goes through this: installer.py,
    tool_backends.CodingToolBackend.execute and claude_code_backend._spawn
    (which resolves its binary first, then launches through here).

    On Windows npm installs npm-based tools as .cmd shims.  A bare name is
    FileNotFoundError (CreateProcess ignores PATHEXT), and launching the .cmd
    hands the arguments to cmd.exe.  So a shim is launched as its target —
    node + script, or the .exe — and a batch file that is not a recognisable
    npm shim may only receive arguments with no cmd.exe metacharacters.

    Raises ValueError for an empty cmd or an argument cmd.exe would rewrite.
    """
    if not cmd:
        raise ValueError('empty command')
    path = shutil.which(cmd[0]) or cmd[0]
    args = list(cmd[1:])
    if sys.platform != 'win32' or not path.lower().endswith(('.cmd', '.bat')):
        return [path] + args
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            match = _SHIM_TARGET.search(f.read())
    except OSError:
        match = None
    if match:
        shim_dir = os.path.dirname(os.path.abspath(path))
        target = os.path.join(shim_dir, match.group(1))
        if target.lower().endswith('.exe'):
            return [target] + args
        local_node = os.path.join(shim_dir, 'node.exe')
        node = local_node if os.path.isfile(local_node) else shutil.which('node')
        if node:
            return [node, target] + args
    if any(_CMD_UNSAFE & set(a) for a in args):
        raise ValueError(f'{os.path.basename(path)} runs through cmd.exe, which '
                         f'would rewrite this argument; refusing to launch it')
    return [path] + args


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
            result = run_bounded(launch_argv([binary, '--version']), timeout=10)
        except (ValueError, OSError):
            result = None
        if result is None or result.timed_out:
            versions[name] = 'installed (version unknown)'
        else:
            versions[name] = result.stdout.strip() or result.stderr.strip() or 'unknown'
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
        result = run_bounded(launch_argv(['npm', 'install', '-g', package]),
                             timeout=120)
    except (ValueError, OSError) as e:
        return {'success': False, 'error': str(e)}
    if result.timed_out:
        return {'success': False, 'error': 'Installation timed out (120s)'}
    if result.returncode == 0:
        return {'success': True, 'message': f'{tool_name} installed successfully'}
    return {'success': False, 'error': result.stderr.strip()}


def pip_install(packages: str) -> Dict:
    """Install Python packages via pip.

    Used for in-process backends (aider_native) that need pip dependencies
    rather than npm.

    In a frozen host (Nunba) sys.executable is the app itself, so
    `sys.executable -m pip` would start a second app, not pip; the host's
    own runner (tts.package_installer._run_pip: python-embed, user-site
    --target, HARTOS pins) installs instead.
    """
    pkg_list = packages.split()
    logger.info(f"pip installing: {pkg_list}")
    if getattr(sys, 'frozen', False):
        try:
            from tts.package_installer import _run_pip  # type: ignore
        except ImportError:
            return {'success': False,
                    'error': 'pip is not available in this frozen build'}
        ok, msg = _run_pip(['install'] + pkg_list, timeout=600)
        if ok:
            return {'success': True, 'message': f'Installed: {", ".join(pkg_list)}'}
        return {'success': False, 'error': msg}
    try:
        result = run_bounded([sys.executable, '-m', 'pip', 'install'] + pkg_list,
                             timeout=120)
    except OSError as e:
        return {'success': False, 'error': str(e)}
    if result.timed_out:
        return {'success': False, 'error': 'pip install timed out (120s)'}
    if result.returncode == 0:
        return {'success': True, 'message': f'Installed: {", ".join(pkg_list)}'}
    return {'success': False, 'error': result.stderr.strip()}


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
