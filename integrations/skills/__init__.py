"""
HART Skill Ingestion - ingest any agent skill into the Hevolve (HARTmind) pipeline.

Supports:
- Claude Code skills (SKILL.md - YAML frontmatter + Markdown)
- Agent Skills open standard (agentskills.io)
- Raw Markdown/text skill definitions
- GitHub-hosted skill repos

Skills become LangChain tools available to every HART agent, making thought
experiments executable through the hivemind.
"""

from integrations.skills.registry import skill_registry, SkillInfo

__all__ = ["skill_registry", "SkillInfo"]
