"""
Unified Agent Goal Engine - Self-Build AutoGen Tools

Tools for the coding agent to modify the OS at runtime via NixOS
self-build. **Sandbox-first**: every change must pass a dry-run
build before it can be applied to the live system.

Pipeline: stage change -> dry-run (sandbox) -> apply (switch)
                                   |
                                   v
                              fail? -> rollback staged changes

Same registration pattern as finance_tools.py / upgrade_tools.py.
"""
import json
import logging
import os
import re
import subprocess
from typing import Annotated

logger = logging.getLogger('hevolve_social')

# Allowed package name pattern — prevents injection
_PKG_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$')
_RUNTIME_NIX = '/etc/hart/runtime.nix'
_BUILDS_LOG = '/var/lib/hart/ota/history/builds.jsonl'


def _is_nixos() -> bool:
    """Check if we're running on NixOS."""
    try:
        result = subprocess.run(
            ['nixos-version'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _read_runtime_packages() -> list:
    """Parse package list from runtime.nix."""
    packages = []
    if not os.path.isfile(_RUNTIME_NIX):
        return packages
    try:
        with open(_RUNTIME_NIX) as f:
            in_packages = False
            for line in f:
                stripped = line.strip()
                if 'systemPackages' in stripped:
                    in_packages = True
                    continue
                if in_packages and stripped == '];':
                    break
                if in_packages and stripped and not stripped.startswith('#'):
                    packages.append(stripped)
    except Exception:
        pass
    return packages


def _stage_package(package: str) -> dict:
    """Add a package to runtime.nix (staged, not yet built)."""
    if not os.path.isfile(_RUNTIME_NIX):
        return {'success': False, 'error': 'runtime.nix not found'}
    try:
        with open(_RUNTIME_NIX) as f:
            content = f.read()
        if package in content:
            return {'success': True, 'status': 'already_present', 'package': package}
        content = content.replace(
            '# Packages added at runtime appear here',
            f'# Packages added at runtime appear here\n    {package}')
        with open(_RUNTIME_NIX, 'w') as f:
            f.write(content)
        return {'success': True, 'status': 'staged', 'package': package}
    except PermissionError:
        return {'success': False, 'error': 'Permission denied writing runtime.nix'}


def _unstage_package(package: str) -> dict:
    """Remove a package from runtime.nix."""
    if not os.path.isfile(_RUNTIME_NIX):
        return {'success': False, 'error': 'runtime.nix not found'}
    try:
        with open(_RUNTIME_NIX) as f:
            lines = f.readlines()
        new_lines = [l for l in lines
                     if package not in l.strip() or l.strip().startswith('#')]
        if len(new_lines) == len(lines):
            return {'success': True, 'status': 'not_found', 'package': package}
        with open(_RUNTIME_NIX, 'w') as f:
            f.writelines(new_lines)
        return {'success': True, 'status': 'removed', 'package': package}
    except PermissionError:
        return {'success': False, 'error': 'Permission denied writing runtime.nix'}


def _run_build(mode: str, timeout: int = 600) -> dict:
    """Run hart-self-build with the given mode."""
    try:
        result = subprocess.run(
            ['hart-self-build', mode],
            capture_output=True, text=True, timeout=timeout)
        return {
            'success': result.returncode == 0,
            'mode': mode,
            'returncode': result.returncode,
            'output': result.stdout[-3000:] if result.stdout else '',
            'errors': result.stderr[-1500:] if result.stderr else '',
        }
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': f'Build timed out ({timeout}s)'}
    except FileNotFoundError:
        return {'success': False, 'error': 'hart-self-build not available'}


def register_self_build_tools(helper, assistant, user_id: str):
    """Register self-build tools with an AutoGen agent (Tier 2).

    All mutating tools enforce sandbox-first: dry-run must pass
    before switch is allowed.
    """

    def get_self_build_status() -> str:
        """Get current OS self-build status: NixOS version, generation,
        runtime packages, and recent build history."""
        info = {'self_build_available': _is_nixos()}
        try:
            result = subprocess.run(
                ['nixos-version'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                info['nixos_version'] = result.stdout.strip()
        except Exception:
            pass

        gen_link = '/nix/var/nix/profiles/system'
        if os.path.islink(gen_link):
            info['current_generation'] = os.readlink(gen_link)

        info['runtime_config_exists'] = os.path.isfile(_RUNTIME_NIX)
        info['runtime_packages'] = _read_runtime_packages()

        if os.path.isfile(_BUILDS_LOG):
            try:
                with open(_BUILDS_LOG) as f:
                    lines = f.readlines()
                info['recent_builds'] = [
                    json.loads(l) for l in lines[-5:] if l.strip()]
            except Exception:
                pass

        # Check allowAgentBuilds flag
        info['agent_builds_allowed'] = os.environ.get(
            'HART_ALLOW_AGENT_BUILDS', 'false').lower() == 'true'

        return json.dumps(info)

    def install_package(
        package: Annotated[str, "NixOS package name (e.g. 'htop', 'nodejs_20')"],
    ) -> str:
        """Stage a package for installation in the OS runtime config.

        This ONLY stages the change in runtime.nix. You MUST call
        sandbox_test_build() to verify it builds, then apply_build()
        to make it live. Never skip the sandbox step.
        """
        if not _PKG_RE.match(package):
            return json.dumps({'error': f'Invalid package name: {package}'})
        if not _is_nixos():
            return json.dumps({'error': 'Not running on NixOS'})

        result = _stage_package(package)
        if result['success']:
            result['next_step'] = 'Call sandbox_test_build() to verify this builds correctly'
        return json.dumps(result)

    def remove_package(
        package: Annotated[str, "NixOS package name to remove"],
    ) -> str:
        """Stage a package removal from the OS runtime config.

        This ONLY removes it from runtime.nix. You MUST call
        sandbox_test_build() to verify, then apply_build() to make live.
        """
        if not package or not package.strip():
            return json.dumps({'error': 'Package name required'})
        if not _is_nixos():
            return json.dumps({'error': 'Not running on NixOS'})

        result = _unstage_package(package.strip())
        if result['success']:
            result['next_step'] = 'Call sandbox_test_build() to verify this builds correctly'
        return json.dumps(result)

    def sandbox_test_build() -> str:
        """Run a dry-run build to test staged changes in a sandbox.

        This is MANDATORY before apply_build(). It evaluates the full
        NixOS configuration without actually switching, catching any
        errors (missing packages, syntax errors, dependency conflicts).

        Returns success=true only if the build would succeed.
        """
        if not _is_nixos():
            return json.dumps({'error': 'Not running on NixOS'})

        result = _run_build('dry-run', timeout=300)
        if result['success']:
            result['sandbox_passed'] = True
            result['next_step'] = 'Safe to call apply_build() to make changes live'
        else:
            result['sandbox_passed'] = False
            result['next_step'] = (
                'Fix the errors, then call sandbox_test_build() again. '
                'Do NOT call apply_build() until sandbox passes.'
            )
        return json.dumps(result)

    def apply_build() -> str:
        """Apply staged changes by rebuilding the OS (nixos-rebuild switch).

        REQUIRES: sandbox_test_build() must have passed first.
        This tool checks the build log to verify a recent successful
        dry-run before proceeding.

        Creates a new NixOS generation with instant rollback available.
        """
        if not _is_nixos():
            return json.dumps({'error': 'Not running on NixOS'})

        # Check agent builds are allowed
        if os.environ.get('HART_ALLOW_AGENT_BUILDS', 'false').lower() != 'true':
            return json.dumps({
                'error': 'Agent-triggered builds are disabled. '
                         'Set HART_ALLOW_AGENT_BUILDS=true or enable '
                         'selfBuild.allowAgentBuilds in NixOS config.'
            })

        # Verify a recent dry-run passed (within last 10 minutes)
        dry_run_verified = False
        if os.path.isfile(_BUILDS_LOG):
            try:
                import time
                with open(_BUILDS_LOG) as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get('mode') == 'dry-run' and entry.get('success'):
                        # Check timestamp (within 600 seconds)
                        ts = entry.get('timestamp', '')
                        if ts:
                            from datetime import datetime
                            try:
                                build_time = datetime.fromisoformat(ts)
                                age = (datetime.now() - build_time).total_seconds()
                                if age < 600:
                                    dry_run_verified = True
                            except Exception:
                                pass
                        break
            except Exception:
                pass

        if not dry_run_verified:
            return json.dumps({
                'error': 'No recent successful dry-run found. '
                         'You MUST call sandbox_test_build() first and it must pass. '
                         'This is a safety requirement — never skip the sandbox.',
                'sandbox_passed': False,
            })

        result = _run_build('switch', timeout=600)
        if result['success']:
            result['message'] = (
                'OS rebuilt successfully. New generation active. '
                'Previous generation available for instant rollback.'
            )
        else:
            result['message'] = (
                'Build failed during apply. The system is unchanged — '
                'NixOS builds are atomic. Check errors and try again.'
            )
        return json.dumps(result)

    def show_build_diff() -> str:
        """Show what would change between current system and staged config.

        Runs `nixos-rebuild build --dry-run` diff output to show exactly
        which packages/services would be added, removed, or changed.
        """
        if not _is_nixos():
            return json.dumps({'error': 'Not running on NixOS'})
        return json.dumps(_run_build('diff', timeout=120))

    def list_generations() -> str:
        """List available NixOS generations for rollback.

        Each generation is a complete, bootable system snapshot.
        Rollback is instant and risk-free.
        """
        generations = []
        profile_dir = '/nix/var/nix/profiles'
        if os.path.isdir(profile_dir):
            try:
                for entry in sorted(os.listdir(profile_dir), reverse=True):
                    if entry.startswith('system-') and entry.endswith('-link'):
                        gen_num = entry.replace('system-', '').replace('-link', '')
                        target = os.readlink(os.path.join(profile_dir, entry))
                        generations.append({
                            'generation': gen_num, 'path': target})
            except Exception:
                pass
        current = ''
        if os.path.islink(os.path.join(profile_dir, 'system')):
            current = os.readlink(os.path.join(profile_dir, 'system'))
        return json.dumps({
            'current': current,
            'generations': generations[:20],
            'rollback_available': len(generations) > 1,
        })

    def rollback_build(
        reason: Annotated[str, "Why rollback is needed"] = '',
    ) -> str:
        """Rollback to the previous NixOS generation.

        Instant and risk-free — the previous generation is a complete
        system that was already proven to work.
        """
        if not _is_nixos():
            return json.dumps({'error': 'Not running on NixOS'})

        logger.warning(f"Agent-triggered rollback: {reason}")

        try:
            result = subprocess.run(
                ['sudo', 'nixos-rebuild', 'switch', '--rollback'],
                capture_output=True, text=True, timeout=300)
            return json.dumps({
                'success': result.returncode == 0,
                'status': 'rolled_back' if result.returncode == 0 else 'failed',
                'reason': reason,
                'output': result.stdout[-2000:] if result.stdout else '',
            })
        except FileNotFoundError:
            return json.dumps({'error': 'nixos-rebuild not available'})

    tools = [
        ('get_self_build_status',
         'Get OS self-build status: version, generation, packages, build history',
         get_self_build_status),
        ('install_package',
         'Stage a NixOS package for installation (must sandbox_test_build before applying)',
         install_package),
        ('remove_package',
         'Stage a NixOS package removal (must sandbox_test_build before applying)',
         remove_package),
        ('sandbox_test_build',
         'MANDATORY: Test staged changes in sandbox (dry-run build) before applying',
         sandbox_test_build),
        ('apply_build',
         'Apply staged changes to live OS (requires prior sandbox_test_build pass)',
         apply_build),
        ('show_build_diff',
         'Show what would change between current system and staged config',
         show_build_diff),
        ('list_generations',
         'List available NixOS generations for rollback',
         list_generations),
        ('rollback_build',
         'Rollback to previous NixOS generation (instant, risk-free)',
         rollback_build),
    ]

    for name, desc, func in tools:
        helper.register_for_llm(name=name, description=desc)(func)
        assistant.register_for_execution(name=name)(func)

    logger.info(f"Registered {len(tools)} self-build tools for user {user_id}")


# Tool descriptors for non-AutoGen registration (e.g. upgrade_tools.py pattern)
SELF_BUILD_TOOLS = [
    {'name': 'get_self_build_status',
     'description': 'Get OS self-build status: version, generation, packages, build history.',
     'function': lambda: get_self_build_status_standalone()},
    {'name': 'sandbox_test_build',
     'description': 'Test staged changes in sandbox before applying.',
     'function': lambda: sandbox_test_build_standalone()},
]


def get_self_build_status_standalone() -> dict:
    """Standalone version for non-AutoGen contexts."""
    info = {'self_build_available': _is_nixos()}
    info['runtime_packages'] = _read_runtime_packages()
    info['agent_builds_allowed'] = os.environ.get(
        'HART_ALLOW_AGENT_BUILDS', 'false').lower() == 'true'
    return info


def sandbox_test_build_standalone() -> dict:
    """Standalone version for non-AutoGen contexts."""
    if not _is_nixos():
        return {'success': False, 'error': 'Not running on NixOS'}
    return _run_build('dry-run', timeout=300)
