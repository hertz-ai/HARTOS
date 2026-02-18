"""
Skill Registry - follows ServiceToolRegistry / MCPToolRegistry pattern.

Ingests agent skills (Claude Code SKILL.md, plain Markdown, JSON) into the
Hevolve pipeline so any Hyve agent can execute them during thought experiments.

Design:
- SkillInfo describes a skill's metadata, instructions, and capabilities
- SkillRegistry manages discovery, storage, and LangChain tool generation
- Global singleton: skill_registry (mirrors service_tool_registry)
- Persists to skills.json for startup reload
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YAML frontmatter parser (no PyYAML dependency - keep it self-contained)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple:
    """Parse YAML frontmatter from SKILL.md content.

    Returns (frontmatter_dict, body_markdown).
    If no frontmatter found, returns ({}, full_text).
    """
    text = text.lstrip()
    if not text.startswith("---"):
        return {}, text

    end = text.find("---", 3)
    if end == -1:
        return {}, text

    yaml_block = text[3:end].strip()
    body = text[end + 3:].strip()

    # Minimal YAML parser - handles key: value, key: [list], key: "quoted"
    meta: Dict[str, Any] = {}
    for line in yaml_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][\w-]*)\s*:\s*(.*)', line)
        if not m:
            continue
        key = m.group(1).strip()
        val = m.group(2).strip()

        # Strip quotes
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]

        # Inline list [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]

        # Boolean
        if isinstance(val, str):
            if val.lower() in ("true", "yes"):
                val = True
            elif val.lower() in ("false", "no"):
                val = False

        meta[key] = val

    return meta, body


# ---------------------------------------------------------------------------
# SkillInfo
# ---------------------------------------------------------------------------

@dataclass
class SkillInfo:
    """Metadata + content for an ingested skill."""
    name: str
    description: str
    instructions: str           # Markdown body - the actual skill prompt
    source: str = "local"       # local | github | http | builtin
    source_path: str = ""       # File path or URL it was loaded from
    allowed_tools: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    user_invocable: bool = True
    context: str = "inline"     # inline | fork
    version: str = "1.0.0"
    author: str = ""
    registered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "source": self.source,
            "source_path": self.source_path,
            "allowed_tools": self.allowed_tools,
            "tags": self.tags,
            "user_invocable": self.user_invocable,
            "context": self.context,
            "version": self.version,
            "author": self.author,
            "registered_at": self.registered_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SkillInfo':
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            instructions=data.get("instructions", ""),
            source=data.get("source", "local"),
            source_path=data.get("source_path", ""),
            allowed_tools=data.get("allowed_tools", []),
            tags=data.get("tags", []),
            user_invocable=data.get("user_invocable", True),
            context=data.get("context", "inline"),
            version=data.get("version", "1.0.0"),
            author=data.get("author", ""),
            registered_at=data.get("registered_at"),
        )

    @classmethod
    def from_skill_md(cls, content: str, source: str = "local",
                      source_path: str = "") -> 'SkillInfo':
        """Parse a SKILL.md file into a SkillInfo."""
        meta, body = _parse_frontmatter(content)

        name = meta.get("name", "")
        if not name and source_path:
            # Derive name from path: .claude/skills/my-skill/SKILL.md → my-skill
            parts = source_path.replace("\\", "/").split("/")
            for i, p in enumerate(parts):
                if p == "skills" and i + 1 < len(parts):
                    name = parts[i + 1]
                    break
            if not name:
                name = os.path.splitext(os.path.basename(source_path))[0]

        allowed = meta.get("allowed-tools", meta.get("allowed_tools", []))
        if isinstance(allowed, str):
            allowed = [t.strip() for t in allowed.split(",")]

        tags_raw = meta.get("tags", [])
        if isinstance(tags_raw, str):
            tags_raw = [t.strip() for t in tags_raw.split(",")]

        return cls(
            name=name,
            description=meta.get("description", body[:120].replace("\n", " ").strip()),
            instructions=body,
            source=source,
            source_path=source_path,
            allowed_tools=allowed,
            tags=tags_raw,
            user_invocable=not meta.get("disable-model-invocation", False),
            context=meta.get("context", "inline"),
            version=meta.get("version", "1.0.0"),
            author=meta.get("author", ""),
        )


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------

class SkillRegistry:
    """
    Registry for agent skills - any skill definition becomes a Hyve tool.

    Mirrors ServiceToolRegistry pattern:
    - register_skill / unregister_skill
    - discover_local / discover_github
    - get_langchain_tools → plugs into langchain_gpt_api get_tools()
    - Global singleton: skill_registry
    """

    def __init__(self, config_file: str = "skills.json"):
        self._skills: Dict[str, SkillInfo] = {}
        self._config_file = config_file
        self._lock = threading.Lock()

    # ---- Registration ----

    def register_skill(self, skill: SkillInfo) -> bool:
        """Register a skill. Returns True if new, False if already exists."""
        with self._lock:
            if skill.name in self._skills:
                logger.debug(f"Skill '{skill.name}' already registered, updating")
            skill.registered_at = datetime.now().isoformat()
            self._skills[skill.name] = skill
            logger.info(f"Registered skill: {skill.name} (source={skill.source})")
            return True

    def unregister_skill(self, name: str) -> bool:
        with self._lock:
            if name in self._skills:
                del self._skills[name]
                logger.info(f"Unregistered skill: {name}")
                return True
            return False

    def get_skill(self, name: str) -> Optional[SkillInfo]:
        return self._skills.get(name)

    def list_skills(self) -> List[Dict[str, Any]]:
        """List all registered skills (summary view)."""
        return [
            {
                "name": s.name,
                "description": s.description,
                "source": s.source,
                "tags": s.tags,
                "user_invocable": s.user_invocable,
                "registered_at": s.registered_at,
            }
            for s in self._skills.values()
        ]

    # ---- Discovery ----

    def discover_local(self, search_paths: Optional[List[str]] = None) -> int:
        """Discover skills from local filesystem.

        Searches:
        - ~/.claude/skills/*/SKILL.md  (Claude Code user skills)
        - .claude/skills/*/SKILL.md    (project skills)
        - Custom paths
        """
        if search_paths is None:
            home = os.path.expanduser("~")
            search_paths = [
                os.path.join(home, ".claude", "skills"),
                os.path.join(os.getcwd(), ".claude", "skills"),
            ]

        count = 0
        for base in search_paths:
            if not os.path.isdir(base):
                continue
            for entry in os.listdir(base):
                skill_dir = os.path.join(base, entry)
                skill_md = os.path.join(skill_dir, "SKILL.md")
                if not os.path.isfile(skill_md):
                    # Also check for skill.md (lowercase)
                    skill_md = os.path.join(skill_dir, "skill.md")
                    if not os.path.isfile(skill_md):
                        continue
                try:
                    with open(skill_md, "r", encoding="utf-8") as f:
                        content = f.read()
                    skill = SkillInfo.from_skill_md(content, source="local",
                                                    source_path=skill_md)
                    if skill.name:
                        self.register_skill(skill)
                        count += 1
                except Exception as e:
                    logger.warning(f"Failed to parse {skill_md}: {e}")

        logger.info(f"Discovered {count} local skills from {len(search_paths)} paths")
        return count

    def discover_github(self, repo_url: str, branch: str = "main",
                        skills_path: str = ".claude/skills") -> int:
        """Discover skills from a GitHub repository.

        Fetches the repo tree and downloads SKILL.md files.
        """
        import urllib.request
        import urllib.error

        # Parse owner/repo from URL
        # Supports: https://github.com/owner/repo or owner/repo
        repo_url = repo_url.rstrip("/")
        parts = repo_url.replace("https://github.com/", "").split("/")
        if len(parts) < 2:
            logger.error(f"Invalid repo URL: {repo_url}")
            return 0
        owner, repo = parts[0], parts[1]

        # Fetch directory listing via GitHub API
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{skills_path}?ref={branch}"
        try:
            req = urllib.request.Request(api_url, headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Hevolve-Hyvemind/1.0",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                entries = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"GitHub discovery failed for {repo_url}: {e}")
            return 0

        count = 0
        for entry in entries:
            if entry.get("type") != "dir":
                continue
            skill_name = entry["name"]
            raw_url = (f"https://raw.githubusercontent.com/{owner}/{repo}"
                       f"/{branch}/{skills_path}/{skill_name}/SKILL.md")
            try:
                req = urllib.request.Request(raw_url, headers={
                    "User-Agent": "Hevolve-Hyvemind/1.0"
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    content = resp.read().decode("utf-8")
                skill = SkillInfo.from_skill_md(content, source="github",
                                                source_path=raw_url)
                if skill.name:
                    self.register_skill(skill)
                    count += 1
            except Exception as e:
                logger.debug(f"Skipping {skill_name}: {e}")

        logger.info(f"Discovered {count} skills from GitHub {owner}/{repo}")
        return count

    def ingest_markdown(self, name: str, markdown: str,
                        description: str = "", tags: Optional[List[str]] = None) -> bool:
        """Ingest a raw Markdown string as a skill (for API / UI uploads)."""
        # Check if it has frontmatter
        if markdown.lstrip().startswith("---"):
            skill = SkillInfo.from_skill_md(markdown, source="api")
            if not skill.name:
                skill.name = name
        else:
            skill = SkillInfo(
                name=name,
                description=description or markdown[:120].replace("\n", " ").strip(),
                instructions=markdown,
                source="api",
                tags=tags or [],
            )
        return self.register_skill(skill)

    # ---- LangChain integration ----

    def get_langchain_tools(self) -> list:
        """
        Get all skills as LangChain Tool objects.

        Plugs into langchain_gpt_api.py get_tools() alongside service_tool_registry.
        Each skill becomes a tool that returns the skill's instructions with the
        user's query injected, letting the LLM execute the skill as a thought experiment.
        """
        from langchain.agents import Tool

        tools = []
        for name, skill in self._skills.items():
            if not skill.user_invocable:
                continue

            # Capture in closure
            _skill = skill

            def execute_skill(query: str, _s=_skill) -> str:
                """Execute a Hyve skill by applying its instructions to the query."""
                # Build the skill execution prompt
                result = f"## Skill: {_s.name}\n\n"
                result += f"{_s.instructions}\n\n"
                result += f"## User Request\n\n{query}\n"
                return result

            tools.append(Tool(
                name=f"hyve_skill_{name}",
                func=execute_skill,
                description=(
                    f"[Hyve Skill] {skill.description}. "
                    f"Use this skill to help with: {', '.join(skill.tags) if skill.tags else skill.name}"
                ),
            ))

        return tools

    def get_autogen_tools(self) -> List[Dict[str, Any]]:
        """Get all skills as autogen function descriptions.

        Mirrors ServiceToolRegistry.get_all_tool_functions() for autogen registration.
        """
        functions = {}
        for name, skill in self._skills.items():
            if not skill.user_invocable:
                continue

            _skill = skill

            def execute(query: str, _s=_skill) -> str:
                return f"## Skill: {_s.name}\n\n{_s.instructions}\n\n## User Request\n\n{query}\n"

            func_name = f"hyve_skill_{name}"
            execute.__name__ = func_name
            execute.__doc__ = skill.description
            functions[func_name] = execute

        return functions

    # ---- Persistence ----

    def save_config(self) -> None:
        """Persist registry to JSON."""
        data = {name: skill.to_dict() for name, skill in self._skills.items()}
        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug(f"Saved {len(data)} skills to {self._config_file}")
        except Exception as e:
            logger.warning(f"Failed to save skills config: {e}")

    def load_config(self) -> int:
        """Load registry from JSON. Returns number of skills loaded."""
        if not os.path.exists(self._config_file):
            return 0
        try:
            with open(self._config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = 0
            for name, skill_data in data.items():
                skill = SkillInfo.from_dict(skill_data)
                self._skills[name] = skill
                count += 1
            logger.info(f"Loaded {count} skills from {self._config_file}")
            return count
        except Exception as e:
            logger.warning(f"Failed to load skills config: {e}")
            return 0

    @property
    def count(self) -> int:
        return len(self._skills)


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

skill_registry = SkillRegistry()
