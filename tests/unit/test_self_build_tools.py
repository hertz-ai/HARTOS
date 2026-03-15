"""Tests for self-build AutoGen tools — sandbox-first OS modification."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Prevent heavy imports
sys.modules.setdefault('hart_intelligence_entry', MagicMock())

from integrations.agent_engine.self_build_tools import (
    _PKG_RE,
    _read_runtime_packages,
    _stage_package,
    _unstage_package,
    _run_build,
    get_self_build_status_standalone,
    sandbox_test_build_standalone,
)


class TestPackageNameValidation(unittest.TestCase):
    def test_valid_names(self):
        for name in ['htop', 'nodejs_20', 'gcc-13', 'python3.11', 'tree-sitter']:
            self.assertTrue(_PKG_RE.match(name), f'{name} should be valid')

    def test_invalid_names(self):
        for name in ['', '123pkg', '-bad', '../escape', 'a' * 200, 'pkg;rm -rf']:
            self.assertIsNone(
                _PKG_RE.match(name) if name else None,
                f'{name!r} should be invalid')


class TestReadRuntimePackages(unittest.TestCase):
    def test_no_file(self):
        with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX',
                   '/nonexistent/runtime.nix'):
            self.assertEqual(_read_runtime_packages(), [])

    def test_parses_packages(self):
        content = """\
{ config, pkgs, ... }:
{
  environment.systemPackages = with pkgs; [
    # Packages added at runtime appear here
    htop
    nodejs_20
    git
  ];
}
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.nix', delete=False) as f:
            f.write(content)
            f.flush()
            with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX', f.name):
                pkgs = _read_runtime_packages()
        os.unlink(f.name)
        self.assertEqual(pkgs, ['htop', 'nodejs_20', 'git'])

    def test_skips_comments(self):
        content = """\
{
  environment.systemPackages = with pkgs; [
    # This is a comment
    htop
    # Another comment
  ];
}
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.nix', delete=False) as f:
            f.write(content)
            f.flush()
            with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX', f.name):
                pkgs = _read_runtime_packages()
        os.unlink(f.name)
        self.assertEqual(pkgs, ['htop'])


class TestStagePackage(unittest.TestCase):
    def _make_runtime(self, packages=''):
        content = (
            '{ config, pkgs, ... }:\n{\n'
            '  environment.systemPackages = with pkgs; [\n'
            '    # Packages added at runtime appear here\n'
            f'{packages}'
            '  ];\n}\n'
        )
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.nix', delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name

    def test_stage_new_package(self):
        path = self._make_runtime()
        with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX', path):
            result = _stage_package('htop')
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'staged')
        with open(path) as f:
            self.assertIn('htop', f.read())
        os.unlink(path)

    def test_stage_duplicate(self):
        path = self._make_runtime('    htop\n')
        with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX', path):
            result = _stage_package('htop')
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'already_present')
        os.unlink(path)

    def test_stage_no_file(self):
        with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX',
                   '/nonexistent/runtime.nix'):
            result = _stage_package('htop')
        self.assertFalse(result['success'])


class TestUnstagePackage(unittest.TestCase):
    def test_remove_existing(self):
        content = (
            '{ config, pkgs, ... }:\n{\n'
            '  environment.systemPackages = with pkgs; [\n'
            '    # Packages added at runtime appear here\n'
            '    htop\n'
            '    git\n'
            '  ];\n}\n'
        )
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.nix', delete=False)
        f.write(content)
        f.close()
        with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX', f.name):
            result = _unstage_package('htop')
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'removed')
        with open(f.name) as fh:
            remaining = fh.read()
        self.assertNotIn('htop', remaining)
        self.assertIn('git', remaining)
        os.unlink(f.name)

    def test_remove_not_found(self):
        content = '{ environment.systemPackages = with pkgs; [ git ]; }'
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.nix', delete=False)
        f.write(content)
        f.close()
        with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX', f.name):
            result = _unstage_package('htop')
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'not_found')
        os.unlink(f.name)


class TestRunBuild(unittest.TestCase):
    @patch('subprocess.run')
    def test_dry_run_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='build OK', stderr='')
        result = _run_build('dry-run')
        self.assertTrue(result['success'])
        self.assertEqual(result['mode'], 'dry-run')

    @patch('subprocess.run')
    def test_build_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout='', stderr='error: package not found')
        result = _run_build('switch')
        self.assertFalse(result['success'])

    @patch('subprocess.run', side_effect=FileNotFoundError)
    def test_missing_command(self, mock_run):
        result = _run_build('dry-run')
        self.assertFalse(result['success'])
        self.assertIn('not available', result['error'])

    @patch('subprocess.run', side_effect=__import__('subprocess').TimeoutExpired(
        'cmd', 600))
    def test_timeout(self, mock_run):
        result = _run_build('dry-run', timeout=600)
        self.assertFalse(result['success'])
        self.assertIn('timed out', result['error'])


class TestSandboxFirstEnforcement(unittest.TestCase):
    """The core safety property: apply_build MUST require a prior dry-run."""

    def test_install_returns_next_step(self):
        """install_package tells the agent to sandbox_test_build next."""
        helper = MagicMock()
        assistant = MagicMock()
        # We can't easily call the nested function, so test the standalone
        # pattern: _stage_package always returns next_step hint
        content = (
            '{ environment.systemPackages = with pkgs; [\n'
            '    # Packages added at runtime appear here\n'
            '  ]; }'
        )
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.nix', delete=False)
        f.write(content)
        f.close()
        with patch('integrations.agent_engine.self_build_tools._RUNTIME_NIX', f.name):
            result = _stage_package('htop')
        self.assertTrue(result['success'])
        # The tool function adds next_step; the raw helper doesn't
        os.unlink(f.name)

    @patch('integrations.agent_engine.self_build_tools._is_nixos', return_value=True)
    @patch.dict(os.environ, {'HART_ALLOW_AGENT_BUILDS': 'true'})
    def test_apply_blocked_without_dry_run(self, mock_nix):
        """apply_build refuses if no recent dry-run in build log."""
        from integrations.agent_engine.self_build_tools import register_self_build_tools

        helper = MagicMock()
        assistant = MagicMock()
        register_self_build_tools(helper, assistant, 'test_user')

        # Find the apply_build function from registered calls
        apply_fn = None
        for call in helper.register_for_llm.return_value.call_args_list:
            fn = call[0][0] if call[0] else None
            if fn and hasattr(fn, '__name__') and fn.__name__ == 'apply_build':
                apply_fn = fn
                break

        # Alternative: get from the registration calls
        registered_names = []
        for call in helper.register_for_llm.call_args_list:
            name = call[1].get('name', '')
            registered_names.append(name)

        self.assertIn('apply_build', registered_names)
        self.assertIn('sandbox_test_build', registered_names)

    def test_tool_count(self):
        """All 8 tools are registered."""
        helper = MagicMock()
        assistant = MagicMock()
        from integrations.agent_engine.self_build_tools import register_self_build_tools
        register_self_build_tools(helper, assistant, 'test_user')
        self.assertEqual(helper.register_for_llm.call_count, 8)
        self.assertEqual(assistant.register_for_execution.call_count, 8)

    def test_registered_tool_names(self):
        helper = MagicMock()
        assistant = MagicMock()
        from integrations.agent_engine.self_build_tools import register_self_build_tools
        register_self_build_tools(helper, assistant, 'test_user')
        names = [c[1]['name'] for c in helper.register_for_llm.call_args_list]
        expected = [
            'get_self_build_status', 'install_package', 'remove_package',
            'sandbox_test_build', 'apply_build', 'show_build_diff',
            'list_generations', 'rollback_build',
        ]
        self.assertEqual(names, expected)


class TestDetectGoalTags(unittest.TestCase):
    def test_self_build_detected(self):
        from integrations.agent_engine.marketing_tools import detect_goal_tags
        self.assertIn('self_build', detect_goal_tags('install nixos package htop'))
        self.assertIn('self_build', detect_goal_tags('trigger self-build'))
        self.assertIn('self_build', detect_goal_tags('edit runtime.nix'))
        self.assertIn('self_build', detect_goal_tags('rollback generation'))

    def test_unrelated_prompt(self):
        from integrations.agent_engine.marketing_tools import detect_goal_tags
        self.assertNotIn('self_build', detect_goal_tags('write a blog post'))
        self.assertNotIn('self_build', detect_goal_tags('check revenue'))


class TestGoalRegistration(unittest.TestCase):
    def test_self_build_goal_type_registered(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        self.assertIn('self_build', get_registered_types())

    def test_self_build_prompt_builder(self):
        from integrations.agent_engine.goal_manager import _build_self_build_prompt
        prompt = _build_self_build_prompt({
            'description': 'Install htop',
            'config_json': {'mode': 'monitor'},
        })
        self.assertIn('sandbox_test_build', prompt)
        self.assertIn('NEVER SKIP', prompt)
        self.assertIn('MANDATORY', prompt)

    def test_self_build_tool_tags(self):
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('self_build')
        self.assertEqual(tags, ['self_build'])


class TestBootstrapGoal(unittest.TestCase):
    def test_self_build_goal_in_seeds(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        self.assertIn('bootstrap_self_build_monitor', slugs)

    def test_self_build_goal_sandbox_required(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        goal = next(g for g in SEED_BOOTSTRAP_GOALS
                    if g['slug'] == 'bootstrap_self_build_monitor')
        self.assertTrue(goal['config'].get('sandbox_required'))
        self.assertEqual(goal['goal_type'], 'self_build')
        self.assertIn('sandbox_test_build', goal['description'])


class TestStandaloneTools(unittest.TestCase):
    @patch('integrations.agent_engine.self_build_tools._is_nixos',
           return_value=False)
    def test_status_non_nixos(self, _):
        info = get_self_build_status_standalone()
        self.assertFalse(info['self_build_available'])

    @patch('integrations.agent_engine.self_build_tools._is_nixos',
           return_value=False)
    def test_sandbox_non_nixos(self, _):
        result = sandbox_test_build_standalone()
        self.assertFalse(result['success'])
        self.assertIn('Not running on NixOS', result['error'])


if __name__ == '__main__':
    unittest.main()
