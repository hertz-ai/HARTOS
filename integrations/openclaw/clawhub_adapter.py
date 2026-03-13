"""
ClawHub Adapter — Parse, install, and execute OpenClaw skills in HART OS.

ClawHub skills are simple: a SKILL.md with YAML frontmatter + instructions.
We parse them into HART-compatible actions that agents can invoke.

Flow:
  1. `clawhub install <slug>` downloads skill to ~/.hevolve/openclaw/skills/
  2. SkillParser reads SKILL.md frontmatter (name, description, metadata.openclaw)
  3. Skill requirements are checked (bins, env vars, config)
  4. Instructions body is loaded as agent context (like a mini recipe)
  5. HART agents can invoke the skill as a tool via ClawHubToolProvider
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────

OPENCLAW_HOME = Path(os.environ.get(
    'OPENCLAW_SKILLS_DIR',
    os.path.expanduser('~/.hevolve/openclaw/skills')
))

CLAWHUB_REGISTRY = 'https://registry.clawhub.ai'


# ── Skill Schema ───────────────────────────────────────────────────

@dataclass
class OpenClawRequirements:
    """Requirements declared in metadata.openclaw.requires."""
    bins: List[str] = field(default_factory=list)
    any_bins: List[str] = field(default_factory=list)
    env: List[str] = field(default_factory=list)
    config: List[str] = field(default_factory=list)


@dataclass
class OpenClawInstallSpec:
    """An install directive (brew, node, go, uv, nix)."""
    id: str = ''
    kind: str = ''          # brew, node, go, uv, nix, pip
    formula: str = ''       # package name
    bins: List[str] = field(default_factory=list)
    label: str = ''
    os_filter: List[str] = field(default_factory=lambda: ['all'])


@dataclass
class OpenClawSkill:
    """Parsed representation of a SKILL.md file."""
    name: str = ''
    description: str = ''
    version: str = '0.0.0'
    homepage: str = ''
    emoji: str = ''
    user_invocable: bool = True
    disable_model_invocation: bool = False
    command_dispatch: Optional[str] = None    # 'tool' or None
    command_tool: Optional[str] = None
    command_arg_mode: str = 'raw'
    primary_env: str = ''
    os_filter: List[str] = field(default_factory=lambda: ['all'])
    requirements: OpenClawRequirements = field(default_factory=OpenClawRequirements)
    install_specs: List[OpenClawInstallSpec] = field(default_factory=list)
    instructions: str = ''                    # The body of SKILL.md
    skill_dir: str = ''                       # Local path
    source: str = 'clawhub'                   # 'clawhub', 'local', 'workspace'


# ── Parser ─────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)


def _parse_yaml_simple(text: str) -> Dict[str, Any]:
    """Minimal YAML-like parser for SKILL.md frontmatter.

    ClawHub frontmatter is constrained: single-line keys, JSON metadata.
    We avoid a PyYAML dependency by parsing the subset we need.
    """
    result = {}
    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        colon_idx = line.find(':')
        if colon_idx < 0:
            continue
        key = line[:colon_idx].strip()
        value = line[colon_idx + 1:].strip()
        # Try JSON parse for metadata objects
        if value.startswith('{') or value.startswith('['):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        elif value.lower() in ('true', 'yes'):
            value = True
        elif value.lower() in ('false', 'no'):
            value = False
        elif value.isdigit():
            value = int(value)
        # Strip quotes
        elif isinstance(value, str) and len(value) >= 2:
            if (value[0] == '"' and value[-1] == '"') or \
               (value[0] == "'" and value[-1] == "'"):
                value = value[1:-1]
        result[key] = value
    return result


def parse_skill_md(skill_path: str) -> OpenClawSkill:
    """Parse a SKILL.md file into an OpenClawSkill object."""
    path = Path(skill_path)
    if path.is_dir():
        path = path / 'SKILL.md'

    content = path.read_text(encoding='utf-8')
    skill = OpenClawSkill(skill_dir=str(path.parent))

    # Parse frontmatter
    fm_match = _FRONTMATTER_RE.match(content)
    if fm_match:
        fm = _parse_yaml_simple(fm_match.group(1))
        skill.name = fm.get('name', '')
        skill.description = fm.get('description', '')
        skill.version = str(fm.get('version', '0.0.0'))
        skill.homepage = fm.get('homepage', '')
        skill.user_invocable = fm.get('user-invocable', True)
        skill.disable_model_invocation = fm.get('disable-model-invocation', False)
        skill.command_dispatch = fm.get('command-dispatch')
        skill.command_tool = fm.get('command-tool')
        skill.command_arg_mode = fm.get('command-arg-mode', 'raw')

        # Parse metadata.openclaw
        meta = fm.get('metadata', {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        oc = meta.get('openclaw', {}) if isinstance(meta, dict) else {}

        skill.emoji = oc.get('emoji', '')
        skill.primary_env = oc.get('primaryEnv', '')
        skill.os_filter = oc.get('os', ['all'])

        # Requirements
        req = oc.get('requires', {})
        if isinstance(req, dict):
            skill.requirements = OpenClawRequirements(
                bins=req.get('bins', []),
                any_bins=req.get('anyBins', []),
                env=req.get('env', []),
                config=req.get('config', []),
            )

        # Install specs
        for spec_data in oc.get('install', []):
            if isinstance(spec_data, dict):
                skill.install_specs.append(OpenClawInstallSpec(
                    id=spec_data.get('id', ''),
                    kind=spec_data.get('kind', ''),
                    formula=spec_data.get('formula', ''),
                    bins=spec_data.get('bins', []),
                    label=spec_data.get('label', ''),
                    os_filter=spec_data.get('os', ['all']),
                ))

        # Instructions = everything after frontmatter
        skill.instructions = content[fm_match.end():].strip()
    else:
        # No frontmatter — entire file is instructions
        skill.instructions = content.strip()

    return skill


# ── Requirements Check ─────────────────────────────────────────────

def check_requirements(skill: OpenClawSkill) -> Dict[str, Any]:
    """Check if skill requirements are satisfied on this system.

    Returns dict with 'satisfied' bool and 'missing' details.
    """
    missing_bins = []
    missing_env = []
    req = skill.requirements

    for b in req.bins:
        if not shutil.which(b):
            missing_bins.append(b)

    if req.any_bins:
        if not any(shutil.which(b) for b in req.any_bins):
            missing_bins.append(f"any of: {', '.join(req.any_bins)}")

    for e in req.env:
        if not os.environ.get(e):
            missing_env.append(e)

    satisfied = len(missing_bins) == 0 and len(missing_env) == 0
    return {
        'satisfied': satisfied,
        'missing_bins': missing_bins,
        'missing_env': missing_env,
    }


# ── Install / Uninstall ───────────────────────────────────────────

def install_skill(slug: str, version: Optional[str] = None,
                  force: bool = False) -> Optional[OpenClawSkill]:
    """Install a ClawHub skill by slug.

    Downloads from registry or uses clawhub CLI if available.
    Skills are stored in ~/.hevolve/openclaw/skills/<slug>/
    """
    dest = OPENCLAW_HOME / slug
    if dest.exists() and not force:
        logger.info("Skill %s already installed at %s", slug, dest)
        return parse_skill_md(str(dest))

    dest.mkdir(parents=True, exist_ok=True)

    # Strategy 1: Use clawhub CLI if available
    clawhub_bin = shutil.which('clawhub')
    if clawhub_bin:
        cmd = [clawhub_bin, 'install', slug]
        if version:
            cmd.extend(['--version', version])
        if force:
            cmd.append('--force')
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                env={**os.environ, 'CLAWHUB_SKILLS_DIR': str(OPENCLAW_HOME)}
            )
            if result.returncode == 0:
                logger.info("Installed skill %s via clawhub CLI", slug)
                return parse_skill_md(str(dest))
            logger.warning("clawhub install failed: %s", result.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("clawhub CLI error: %s", e)

    # Strategy 2: Direct HTTP download from registry
    try:
        from core.http_pool import pooled_get
    except ImportError:
        import requests
        pooled_get = requests.get

    url = f"{CLAWHUB_REGISTRY}/api/skills/{slug}"
    if version:
        url += f"/versions/{version}"
    url += "/download"

    try:
        resp = pooled_get(url, timeout=30)
        if hasattr(resp, 'status_code'):
            status = resp.status_code
        else:
            status = getattr(resp, 'status', 200)

        if status == 200:
            # Registry returns a tarball or SKILL.md content
            content_type = ''
            if hasattr(resp, 'headers'):
                content_type = resp.headers.get('content-type', '')

            if 'application/json' in content_type:
                data = resp.json() if hasattr(resp, 'json') else json.loads(resp.text)
                skill_md = data.get('skill_md', data.get('content', ''))
                (dest / 'SKILL.md').write_text(skill_md, encoding='utf-8')
            else:
                text = resp.text if hasattr(resp, 'text') else str(resp)
                (dest / 'SKILL.md').write_text(text, encoding='utf-8')

            logger.info("Installed skill %s from registry", slug)
            return parse_skill_md(str(dest))
        else:
            logger.error("Registry returned %d for skill %s", status, slug)
    except Exception as e:
        logger.error("Failed to download skill %s: %s", slug, e)

    # Clean up empty dir on failure
    if dest.exists() and not any(dest.iterdir()):
        dest.rmdir()
    return None


def uninstall_skill(slug: str) -> bool:
    """Remove an installed skill."""
    dest = OPENCLAW_HOME / slug
    if dest.exists():
        shutil.rmtree(dest)
        logger.info("Uninstalled skill %s", slug)
        return True
    return False


def list_installed_skills() -> List[OpenClawSkill]:
    """List all installed OpenClaw skills."""
    skills = []
    if not OPENCLAW_HOME.exists():
        return skills
    for d in sorted(OPENCLAW_HOME.iterdir()):
        skill_md = d / 'SKILL.md'
        if d.is_dir() and skill_md.exists():
            try:
                skills.append(parse_skill_md(str(d)))
            except Exception as e:
                logger.warning("Failed to parse skill at %s: %s", d, e)
    return skills


# ── Skill → HART Tool Conversion ──────────────────────────────────

def skill_to_autogen_tool(skill: OpenClawSkill) -> Dict[str, Any]:
    """Convert an OpenClaw skill to an AutoGen-compatible tool definition.

    The tool wraps the skill's instructions as a prompt-based action
    that HART agents can invoke like any other tool.
    """
    tool_name = f"openclaw_{skill.name.replace('-', '_')}"

    def tool_fn(command: str = '', **kwargs) -> str:
        """Execute an OpenClaw skill with the given command/args."""
        # Replace {baseDir} placeholder with actual skill dir
        instructions = skill.instructions.replace('{baseDir}', skill.skill_dir)

        # If command-dispatch: tool, forward directly
        if skill.command_dispatch == 'tool' and skill.command_tool:
            return json.dumps({
                'tool': skill.command_tool,
                'command': command,
                'skill': skill.name,
                'instructions': instructions,
            })

        # Otherwise, return instructions as context for the agent
        return json.dumps({
            'skill': skill.name,
            'description': skill.description,
            'instructions': instructions,
            'command': command,
        })

    return {
        'name': tool_name,
        'description': skill.description or f"OpenClaw skill: {skill.name}",
        'function': tool_fn,
        'parameters': {
            'command': {
                'type': 'string',
                'description': 'Command or input to pass to the skill',
            }
        },
        'source': 'openclaw_clawhub',
    }


class ClawHubToolProvider:
    """Provides all installed ClawHub skills as HART agent tools.

    Usage:
        provider = ClawHubToolProvider()
        tools = provider.get_tools()
        # Register with AutoGen agent
        for tool in tools:
            agent.register_function(tool['function'], tool['name'], tool['description'])
    """

    def __init__(self, skills_dir: Optional[str] = None):
        self._skills_dir = Path(skills_dir) if skills_dir else OPENCLAW_HOME
        self._cache: Optional[List[Dict[str, Any]]] = None

    def get_tools(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Get all installed skills as AutoGen tool definitions."""
        if self._cache is not None and not refresh:
            return self._cache

        tools = []
        if self._skills_dir.exists():
            for d in sorted(self._skills_dir.iterdir()):
                skill_md = d / 'SKILL.md'
                if d.is_dir() and skill_md.exists():
                    try:
                        skill = parse_skill_md(str(d))
                        if skill.disable_model_invocation:
                            continue
                        req_check = check_requirements(skill)
                        if not req_check['satisfied']:
                            logger.debug("Skipping skill %s: missing %s",
                                         skill.name, req_check)
                            continue
                        tools.append(skill_to_autogen_tool(skill))
                    except Exception as e:
                        logger.warning("Failed to load skill %s: %s", d.name, e)

        self._cache = tools
        return tools

    def invalidate(self):
        """Clear cached tools (call after install/uninstall)."""
        self._cache = None


# ── Singleton ──────────────────────────────────────────────────────

_provider: Optional[ClawHubToolProvider] = None


def get_clawhub_provider() -> ClawHubToolProvider:
    """Get the singleton ClawHub tool provider."""
    global _provider
    if _provider is None:
        _provider = ClawHubToolProvider()
    return _provider
