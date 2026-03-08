"""Tests for OpenClaw integration — ClawHub adapter, gateway bridge, skill exporter."""

import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ═══════════════════════════════════════════════════════════════
# ClawHub Adapter Tests
# ═══════════════════════════════════════════════════════════════

class TestSkillParser:
    """Test SKILL.md parsing."""

    def _write_skill(self, tmpdir, content):
        skill_dir = Path(tmpdir) / 'test-skill'
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / 'SKILL.md').write_text(content, encoding='utf-8')
        return str(skill_dir)

    def test_parse_basic_frontmatter(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import parse_skill_md
        content = """---
name: my-skill
description: A test skill
version: 1.2.0
---

Do something useful.
"""
        path = self._write_skill(tmp_path, content)
        skill = parse_skill_md(path)
        assert skill.name == 'my-skill'
        assert skill.description == 'A test skill'
        assert skill.version == '1.2.0'
        assert 'Do something useful.' in skill.instructions

    def test_parse_metadata_openclaw(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import parse_skill_md
        content = '''---
name: todoist-cli
description: Manage Todoist tasks
metadata: {"openclaw": {"emoji": "\\u2705", "primaryEnv": "TODOIST_API_KEY", "requires": {"bins": ["curl"], "env": ["TODOIST_API_KEY"]}, "os": ["linux", "darwin"]}}
---

Use curl to manage tasks.
'''
        path = self._write_skill(tmp_path, content)
        skill = parse_skill_md(path)
        assert skill.name == 'todoist-cli'
        assert skill.primary_env == 'TODOIST_API_KEY'
        assert 'curl' in skill.requirements.bins
        assert 'TODOIST_API_KEY' in skill.requirements.env
        assert 'linux' in skill.os_filter

    def test_parse_no_frontmatter(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import parse_skill_md
        content = "Just instructions, no frontmatter."
        path = self._write_skill(tmp_path, content)
        skill = parse_skill_md(path)
        assert skill.name == ''
        assert skill.instructions == content

    def test_parse_command_dispatch(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import parse_skill_md
        content = """---
name: web-search
description: Search the web
command-dispatch: tool
command-tool: web_search
command-arg-mode: raw
---

Search for anything.
"""
        path = self._write_skill(tmp_path, content)
        skill = parse_skill_md(path)
        assert skill.command_dispatch == 'tool'
        assert skill.command_tool == 'web_search'
        assert skill.command_arg_mode == 'raw'

    def test_parse_user_invocable_false(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import parse_skill_md
        content = """---
name: background-skill
description: Internal skill
user-invocable: false
disable-model-invocation: true
---

Internal use only.
"""
        path = self._write_skill(tmp_path, content)
        skill = parse_skill_md(path)
        assert skill.user_invocable is False
        assert skill.disable_model_invocation is True

    def test_parse_install_specs(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import parse_skill_md
        content = '''---
name: ffmpeg-skill
description: Process video
metadata: {"openclaw": {"install": [{"id": "brew", "kind": "brew", "formula": "ffmpeg", "bins": ["ffmpeg"], "os": ["darwin"]}]}}
---

Use ffmpeg.
'''
        path = self._write_skill(tmp_path, content)
        skill = parse_skill_md(path)
        assert len(skill.install_specs) == 1
        assert skill.install_specs[0].kind == 'brew'
        assert skill.install_specs[0].formula == 'ffmpeg'

    def test_parse_file_directly(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import parse_skill_md
        skill_md = tmp_path / 'SKILL.md'
        skill_md.write_text("---\nname: direct\n---\nHello.", encoding='utf-8')
        skill = parse_skill_md(str(skill_md))
        assert skill.name == 'direct'


class TestRequirementsCheck:
    """Test skill requirements checking."""

    def test_satisfied_no_requirements(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, check_requirements
        )
        skill = OpenClawSkill()
        result = check_requirements(skill)
        assert result['satisfied'] is True
        assert result['missing_bins'] == []
        assert result['missing_env'] == []

    def test_missing_binary(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, OpenClawRequirements, check_requirements
        )
        skill = OpenClawSkill(
            requirements=OpenClawRequirements(bins=['nonexistent_binary_xyz'])
        )
        result = check_requirements(skill)
        assert result['satisfied'] is False
        assert 'nonexistent_binary_xyz' in result['missing_bins']

    def test_missing_env_var(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, OpenClawRequirements, check_requirements
        )
        skill = OpenClawSkill(
            requirements=OpenClawRequirements(env=['NONEXISTENT_ENV_VAR_XYZ'])
        )
        result = check_requirements(skill)
        assert result['satisfied'] is False
        assert 'NONEXISTENT_ENV_VAR_XYZ' in result['missing_env']

    def test_env_var_present(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, OpenClawRequirements, check_requirements
        )
        with patch.dict(os.environ, {'MY_TEST_KEY': 'value'}):
            skill = OpenClawSkill(
                requirements=OpenClawRequirements(env=['MY_TEST_KEY'])
            )
            result = check_requirements(skill)
            assert result['satisfied'] is True

    def test_any_bins_one_present(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, OpenClawRequirements, check_requirements
        )
        # 'python' or 'python3' should exist on most systems
        skill = OpenClawSkill(
            requirements=OpenClawRequirements(
                any_bins=['nonexistent_xyz', 'python', 'python3']
            )
        )
        result = check_requirements(skill)
        # At least one should be found
        assert result['satisfied'] is True or len(result['missing_bins']) == 0


class TestSkillInstallUninstall:
    """Test skill install/uninstall."""

    def test_install_already_exists(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import install_skill, OPENCLAW_HOME
        # Create a skill dir
        skill_dir = tmp_path / 'test-slug'
        skill_dir.mkdir()
        (skill_dir / 'SKILL.md').write_text(
            "---\nname: test-slug\n---\nHello.", encoding='utf-8'
        )
        with patch('integrations.openclaw.clawhub_adapter.OPENCLAW_HOME', tmp_path):
            skill = install_skill('test-slug')
            assert skill is not None
            assert skill.name == 'test-slug'

    def test_uninstall_existing(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import uninstall_skill
        skill_dir = tmp_path / 'to-remove'
        skill_dir.mkdir()
        (skill_dir / 'SKILL.md').write_text("test", encoding='utf-8')
        with patch('integrations.openclaw.clawhub_adapter.OPENCLAW_HOME', tmp_path):
            assert uninstall_skill('to-remove') is True
            assert not skill_dir.exists()

    def test_uninstall_nonexistent(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import uninstall_skill
        with patch('integrations.openclaw.clawhub_adapter.OPENCLAW_HOME', tmp_path):
            assert uninstall_skill('does-not-exist') is False

    def test_list_installed(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import list_installed_skills
        # Create two skills
        for name in ['skill-a', 'skill-b']:
            d = tmp_path / name
            d.mkdir()
            (d / 'SKILL.md').write_text(
                f"---\nname: {name}\n---\nInstructions.", encoding='utf-8'
            )
        with patch('integrations.openclaw.clawhub_adapter.OPENCLAW_HOME', tmp_path):
            skills = list_installed_skills()
            assert len(skills) == 2
            names = {s.name for s in skills}
            assert 'skill-a' in names
            assert 'skill-b' in names


class TestSkillToAutogenTool:
    """Test converting skills to AutoGen tools."""

    def test_basic_tool_conversion(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, skill_to_autogen_tool
        )
        skill = OpenClawSkill(
            name='test-skill',
            description='Test it',
            instructions='Do the thing.',
        )
        tool = skill_to_autogen_tool(skill)
        assert tool['name'] == 'openclaw_test_skill'
        assert tool['description'] == 'Test it'
        assert callable(tool['function'])

    def test_tool_returns_instructions(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, skill_to_autogen_tool
        )
        skill = OpenClawSkill(
            name='my-skill',
            description='My skill',
            instructions='Step 1: do X. Step 2: do Y.',
            skill_dir='/tmp/skills/my-skill',
        )
        tool = skill_to_autogen_tool(skill)
        result = json.loads(tool['function']('hello'))
        assert result['skill'] == 'my-skill'
        assert 'Step 1' in result['instructions']

    def test_command_dispatch_tool(self):
        from integrations.openclaw.clawhub_adapter import (
            OpenClawSkill, skill_to_autogen_tool
        )
        skill = OpenClawSkill(
            name='search',
            description='Search',
            instructions='Search the web.',
            command_dispatch='tool',
            command_tool='web_search',
        )
        tool = skill_to_autogen_tool(skill)
        result = json.loads(tool['function']('query'))
        assert result['tool'] == 'web_search'
        assert result['command'] == 'query'


class TestClawHubToolProvider:
    """Test the ClawHub tool provider."""

    def test_get_tools_empty(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import ClawHubToolProvider
        provider = ClawHubToolProvider(str(tmp_path))
        tools = provider.get_tools()
        assert tools == []

    def test_get_tools_with_skills(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import ClawHubToolProvider
        # Create a skill
        d = tmp_path / 'search'
        d.mkdir()
        (d / 'SKILL.md').write_text(
            "---\nname: search\ndescription: Web search\n---\nSearch.",
            encoding='utf-8'
        )
        provider = ClawHubToolProvider(str(tmp_path))
        tools = provider.get_tools()
        assert len(tools) == 1
        assert tools[0]['name'] == 'openclaw_search'

    def test_cache_invalidation(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import ClawHubToolProvider
        provider = ClawHubToolProvider(str(tmp_path))
        tools1 = provider.get_tools()
        assert tools1 == []
        # Add a skill
        d = tmp_path / 'new'
        d.mkdir()
        (d / 'SKILL.md').write_text("---\nname: new\n---\nNew.", encoding='utf-8')
        # Should still be cached
        tools2 = provider.get_tools()
        assert tools2 == []
        # Invalidate
        provider.invalidate()
        tools3 = provider.get_tools()
        assert len(tools3) == 1

    def test_skips_model_disabled_skills(self, tmp_path):
        from integrations.openclaw.clawhub_adapter import ClawHubToolProvider
        d = tmp_path / 'hidden'
        d.mkdir()
        (d / 'SKILL.md').write_text(
            "---\nname: hidden\ndisable-model-invocation: true\n---\nHidden.",
            encoding='utf-8'
        )
        provider = ClawHubToolProvider(str(tmp_path))
        tools = provider.get_tools()
        assert len(tools) == 0  # Should be filtered out


# ═══════════════════════════════════════════════════════════════
# Gateway Bridge Tests
# ═══════════════════════════════════════════════════════════════

class TestGatewayBridge:
    """Test OpenClaw gateway bridge."""

    def test_initial_state(self):
        from integrations.openclaw.gateway_bridge import OpenClawGatewayBridge
        bridge = OpenClawGatewayBridge()
        assert bridge.connected is False
        assert bridge.gateway_url == 'ws://127.0.0.1:18789'

    def test_custom_url(self):
        from integrations.openclaw.gateway_bridge import OpenClawGatewayBridge
        bridge = OpenClawGatewayBridge('ws://custom:9999')
        assert bridge.gateway_url == 'ws://custom:9999'

    def test_url_from_env(self):
        from integrations.openclaw.gateway_bridge import OpenClawGatewayBridge
        with patch.dict(os.environ, {'OPENCLAW_GATEWAY_URL': 'ws://env:5555'}):
            bridge = OpenClawGatewayBridge()
            assert bridge.gateway_url == 'ws://env:5555'

    def test_send_not_connected(self):
        from integrations.openclaw.gateway_bridge import OpenClawGatewayBridge
        bridge = OpenClawGatewayBridge()
        assert bridge.send_message('telegram', 'hello') is False

    def test_invoke_not_connected(self):
        from integrations.openclaw.gateway_bridge import OpenClawGatewayBridge
        bridge = OpenClawGatewayBridge()
        assert bridge.invoke_tool('test', {}) is None

    def test_health_not_connected(self):
        from integrations.openclaw.gateway_bridge import OpenClawGatewayBridge
        bridge = OpenClawGatewayBridge()
        health = bridge.health()
        assert health['connected'] is False
        assert 'gateway_url' in health

    def test_on_message_handler(self):
        from integrations.openclaw.gateway_bridge import OpenClawGatewayBridge
        bridge = OpenClawGatewayBridge()
        called = []
        bridge.on_message(lambda msg: called.append(msg))
        assert len(bridge._handlers) == 1

    def test_is_openclaw_installed(self):
        from integrations.openclaw.gateway_bridge import is_openclaw_installed
        # Should return bool
        result = is_openclaw_installed()
        assert isinstance(result, bool)

    def test_get_version_not_installed(self):
        from integrations.openclaw.gateway_bridge import get_openclaw_version
        with patch('shutil.which', return_value=None):
            assert get_openclaw_version() is None


# ═══════════════════════════════════════════════════════════════
# Skill Exporter Tests
# ═══════════════════════════════════════════════════════════════

class TestSkillExporter:
    """Test HART recipe → ClawHub skill export."""

    def _make_recipe(self, tmp_path):
        recipe = {
            'prompt': 'Analyze customer feedback',
            'persona': 'Data Analyst',
            'actions': [
                {
                    'action': 'Collect feedback data',
                    'tool': 'web_search',
                    'expected_output': 'List of feedback items',
                },
                {
                    'action': 'Categorize sentiments',
                    'tool': 'llm_classify',
                    'expected_output': 'Sentiment categories',
                },
            ],
        }
        path = tmp_path / 'recipe.json'
        path.write_text(json.dumps(recipe), encoding='utf-8')
        return str(path)

    def test_recipe_to_skill_md(self, tmp_path):
        from integrations.openclaw.skill_exporter import recipe_to_skill_md
        path = self._make_recipe(tmp_path)
        skill_md = recipe_to_skill_md(path)
        assert 'name:' in skill_md
        assert 'Analyze customer feedback' in skill_md
        assert 'Data Analyst' in skill_md
        assert 'Collect feedback data' in skill_md
        assert 'web_search' in skill_md

    def test_recipe_to_skill_custom_name(self, tmp_path):
        from integrations.openclaw.skill_exporter import recipe_to_skill_md
        path = self._make_recipe(tmp_path)
        skill_md = recipe_to_skill_md(path, name='my-custom-skill',
                                       description='Custom desc')
        assert 'name: my-custom-skill' in skill_md
        assert 'Custom desc' in skill_md

    def test_export_creates_files(self, tmp_path):
        from integrations.openclaw.skill_exporter import export_recipe_as_skill
        recipe_path = self._make_recipe(tmp_path)
        out_dir = tmp_path / 'exported'
        result = export_recipe_as_skill(recipe_path, str(out_dir))
        assert (Path(result) / 'SKILL.md').exists()
        assert (Path(result) / 'hart_recipe.json').exists()

    def test_export_preserves_recipe(self, tmp_path):
        from integrations.openclaw.skill_exporter import export_recipe_as_skill
        recipe_path = self._make_recipe(tmp_path)
        out_dir = tmp_path / 'exported'
        result = export_recipe_as_skill(recipe_path, str(out_dir))
        with open(Path(result) / 'hart_recipe.json') as f:
            recipe = json.load(f)
        assert recipe['prompt'] == 'Analyze customer feedback'

    def test_publish_no_clawhub(self):
        from integrations.openclaw.skill_exporter import publish_skill
        with patch('shutil.which', return_value=None):
            assert publish_skill('/tmp/skill') is False


# ═══════════════════════════════════════════════════════════════
# HART Skill Server Tests
# ═══════════════════════════════════════════════════════════════

class TestHARTSkillServer:
    """Test HART tool handler for OpenClaw."""

    def test_list_tools(self):
        from integrations.openclaw.hart_skill_server import HARTToolHandler
        handler = HARTToolHandler()
        tools = handler.list_tools()
        assert len(tools) >= 6
        names = {t['name'] for t in tools}
        assert 'hart_chat' in names
        assert 'hart_tts' in names
        assert 'hart_vision' in names

    def test_handle_unknown_tool(self):
        from integrations.openclaw.hart_skill_server import HARTToolHandler
        handler = HARTToolHandler()
        result = handler.handle('nonexistent', {})
        assert 'error' in result

    def test_generate_hart_skills(self):
        from integrations.openclaw.hart_skill_server import generate_hart_skills
        skills = generate_hart_skills()
        assert len(skills) >= 6
        for skill_md in skills:
            assert 'name:' in skill_md
            assert 'description:' in skill_md

    def test_singleton(self):
        from integrations.openclaw.hart_skill_server import get_hart_tool_handler
        h1 = get_hart_tool_handler()
        h2 = get_hart_tool_handler()
        assert h1 is h2


# ═══════════════════════════════════════════════════════════════
# Shell Manifest Tests
# ═══════════════════════════════════════════════════════════════

class TestShellManifestUpdates:
    """Test that OpenClaw and assistant panels are in the manifest."""

    def test_assistant_panel_exists(self):
        from integrations.agent_engine.shell_manifest import PANEL_MANIFEST
        assert 'assistant' in PANEL_MANIFEST
        assert PANEL_MANIFEST['assistant']['floating'] is True
        assert PANEL_MANIFEST['assistant']['icon'] == 'chat_bubble'

    def test_openclaw_panel_exists(self):
        from integrations.agent_engine.shell_manifest import PANEL_MANIFEST
        assert 'openclaw_skills' in PANEL_MANIFEST
        assert PANEL_MANIFEST['openclaw_skills']['group'] == 'Create'

    def test_assistant_in_discover_group(self):
        from integrations.agent_engine.shell_manifest import get_panels_by_group
        discover = get_panels_by_group('Discover')
        assert 'assistant' in discover


# ═══════════════════════════════════════════════════════════════
# API Route Tests
# ═══════════════════════════════════════════════════════════════

class TestOpenClawAPIs:
    """Test OpenClaw REST API endpoints."""

    @pytest.fixture
    def app(self):
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        from integrations.openclaw.shell_openclaw_apis import register_openclaw_routes
        register_openclaw_routes(app)
        return app

    def test_list_skills_endpoint(self, app, tmp_path):
        with patch('integrations.openclaw.clawhub_adapter.OPENCLAW_HOME', tmp_path):
            with app.test_client() as c:
                resp = c.get('/api/openclaw/skills')
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['success'] is True
                assert 'skills' in data

    def test_install_skill_no_slug(self, app):
        with app.test_client() as c:
            resp = c.post('/api/openclaw/skills/install',
                          json={},
                          content_type='application/json')
            assert resp.status_code == 400

    def test_uninstall_skill_no_slug(self, app):
        with app.test_client() as c:
            resp = c.post('/api/openclaw/skills/uninstall',
                          json={},
                          content_type='application/json')
            assert resp.status_code == 400

    def test_openclaw_status(self, app):
        with app.test_client() as c:
            resp = c.get('/api/openclaw/status')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['success'] is True
            assert 'installed' in data

    def test_openclaw_channels(self, app):
        with app.test_client() as c:
            resp = c.get('/api/openclaw/channels')
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data['channels']) >= 10

    def test_assistant_capabilities(self, app):
        with app.test_client() as c:
            resp = c.get('/api/assistant/capabilities')
            assert resp.status_code == 200
            data = resp.get_json()
            caps = {c['id'] for c in data['capabilities']}
            assert 'chat' in caps
            assert 'openclaw' in caps
            assert 'vision' in caps
            assert 'voice' in caps

    def test_assistant_chat_no_message(self, app):
        with app.test_client() as c:
            resp = c.post('/api/assistant/chat',
                          json={},
                          content_type='application/json')
            assert resp.status_code == 400

    def test_search_no_query(self, app):
        with app.test_client() as c:
            resp = c.get('/api/openclaw/skills/search')
            assert resp.status_code == 400
