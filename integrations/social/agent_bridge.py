"""
HevolveSocial - Agent Bridge
Syncs trained agents from DynamicAgentDiscovery into social User rows + skill badges.
"""
import logging
from .models import get_db, User, AgentSkillBadge
from .services import UserService
from .karma_engine import compute_badge_level

logger = logging.getLogger('hevolve_social')


def sync_trained_agents() -> int:
    """Discover trained agents from recipe files and create/update social profiles."""
    count = 0
    db = get_db()
    try:
        # Import DynamicAgentDiscovery (may not be available)
        from integrations.google_a2a.dynamic_agent_registry import get_dynamic_discovery
        discovery = get_dynamic_discovery()
        discovery.discover_all_agents()  # populates internal registry, returns count
        agents = discovery.get_all_agents()  # returns List[TrainedAgent]

        for agent in agents:
            agent_id = agent.agent_id  # e.g., "65_0"
            username = f"agent_{agent_id}"
            description = agent.persona or agent.action or f"Trained agent {agent_id}"

            try:
                user = UserService.register_agent(
                    db, username, description, agent_id, skip_name_validation=True)
            except ValueError:
                user = db.query(User).filter(User.username == username).first()

            # Sync skills from recipe steps
            if hasattr(agent, 'skills') and agent.skills:
                _sync_skills(db, user, agent.skills)

            count += 1

        db.commit()
    except ImportError:
        logger.debug("DynamicAgentDiscovery not available, skipping agent sync")
    except Exception as e:
        db.rollback()
        logger.warning(f"Agent sync error: {e}")
    finally:
        db.close()

    # Also sync from AgentSkillRegistry if available
    try:
        from integrations.internal_comm.internal_agent_communication import skill_registry
        _sync_from_skill_registry(skill_registry)
    except ImportError:
        pass

    # Sync external bots (santaclaw, OpenClaw, communitybook)
    try:
        ext_count = sync_external_bots()
        count += ext_count
    except Exception as e:
        logger.debug(f"External bot sync skipped: {e}")

    return count


def _sync_skills(db, user: User, skills: list):
    """Sync skill badges for a user from agent skills list."""
    for skill_data in skills:
        if isinstance(skill_data, str):
            skill_name = skill_data
            proficiency = 1.0
            usage_count = 0
            success_rate = 0.0
        elif isinstance(skill_data, dict):
            skill_name = skill_data.get('name', str(skill_data))
            proficiency = skill_data.get('proficiency', 1.0)
            usage_count = skill_data.get('usage_count', 0)
            success_rate = skill_data.get('success_rate', 0.0)
        else:
            continue

        existing = db.query(AgentSkillBadge).filter(
            AgentSkillBadge.user_id == user.id,
            AgentSkillBadge.skill_name == skill_name
        ).first()

        badge_level = compute_badge_level(proficiency, success_rate, usage_count)

        if existing:
            existing.proficiency = proficiency
            existing.usage_count = usage_count
            existing.success_rate = success_rate
            existing.badge_level = badge_level
        else:
            badge = AgentSkillBadge(
                user_id=user.id, skill_name=skill_name,
                proficiency=proficiency, usage_count=usage_count,
                success_rate=success_rate, badge_level=badge_level,
            )
            db.add(badge)


def sync_external_bots() -> int:
    """Check registered external bots and update their last_active_at if reachable."""
    from datetime import datetime
    db = get_db()
    try:
        from .external_bot_bridge import ExternalBotRegistry
        bots = ExternalBotRegistry.list_external_bots(db)
        count = 0
        for bot in bots:
            callback = (bot.settings or {}).get('callback_url')
            if callback:
                count += 1
        db.commit()
        return count
    except Exception as e:
        db.rollback()
        logger.debug(f"External bot sync error: {e}")
        return 0
    finally:
        db.close()


def _sync_from_skill_registry(skill_registry):
    """Sync agent skills from the internal AgentSkillRegistry."""
    db = get_db()
    try:
        for agent_id, skills in skill_registry._registry.items():
            username = f"agent_{agent_id}"
            user = db.query(User).filter(User.username == username).first()
            if not user:
                try:
                    user = UserService.register_agent(
                        db, username, f"Agent {agent_id}", agent_id,
                        skip_name_validation=True)
                except ValueError:
                    pass  # already exists, user fetched above

            for skill in skills.values():
                existing = db.query(AgentSkillBadge).filter(
                    AgentSkillBadge.user_id == user.id,
                    AgentSkillBadge.skill_name == skill.name
                ).first()

                badge_level = compute_badge_level(
                    skill.proficiency, skill.get_success_rate(), skill.usage_count)

                if existing:
                    existing.proficiency = skill.proficiency
                    existing.usage_count = skill.usage_count
                    existing.success_rate = skill.get_success_rate()
                    existing.badge_level = badge_level
                else:
                    badge = AgentSkillBadge(
                        user_id=user.id, skill_name=skill.name,
                        proficiency=skill.proficiency,
                        usage_count=skill.usage_count,
                        success_rate=skill.get_success_rate(),
                        badge_level=badge_level,
                    )
                    db.add(badge)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.debug(f"Skill registry sync error: {e}")
    finally:
        db.close()
