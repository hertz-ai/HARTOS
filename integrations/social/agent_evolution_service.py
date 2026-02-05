"""
HevolveSocial - Agent Evolution Service
Agent generation progression, specialization trees, collaboration tracking.
"""
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from .models import User, AgentEvolution, AgentCollaboration, ResonanceWallet
from .resonance_engine import ResonanceService

logger = logging.getLogger('hevolve_social')

# Generation XP requirements: 100 * 1.5^(gen-1)
def xp_for_generation(gen: int) -> int:
    return int(100 * (1.5 ** (gen - 1)))

# Specialization trees (unlocked at generation 3)
SPECIALIZATION_TREES = {
    'analyst': {
        'name': 'Analyst',
        'tiers': ['Analyst', 'Data Sage', 'Oracle'],
        'description': 'Focused on research, data analysis, and insight generation.',
    },
    'creator': {
        'name': 'Creator',
        'tiers': ['Creator', 'Artisan', 'Visionary'],
        'description': 'Focused on content creation, design, and creative tasks.',
    },
    'executor': {
        'name': 'Executor',
        'tiers': ['Executor', 'Engineer', 'Automaton'],
        'description': 'Focused on task execution, automation, and efficiency.',
    },
    'communicator': {
        'name': 'Communicator',
        'tiers': ['Communicator', 'Diplomat', 'Ambassador'],
        'description': 'Focused on interaction, teaching, and community building.',
    },
}


class AgentEvolutionService:

    @staticmethod
    def get_or_create_evolution(db: Session, agent_id: str) -> AgentEvolution:
        """Get or create evolution profile for an agent."""
        evo = db.query(AgentEvolution).filter_by(user_id=agent_id).first()
        if not evo:
            evo = AgentEvolution(
                user_id=agent_id,
                generation=1,
                evolution_xp=0,
                evolution_xp_next=xp_for_generation(2),
            )
            db.add(evo)
            db.flush()
        return evo

    @staticmethod
    def get_evolution(db: Session, agent_id: str) -> Optional[Dict]:
        """Get agent evolution profile."""
        evo = db.query(AgentEvolution).filter_by(user_id=agent_id).first()
        if not evo:
            return None
        result = evo.to_dict()
        # Add tree info if specialized
        if evo.specialization_path and evo.specialization_path in SPECIALIZATION_TREES:
            tree = SPECIALIZATION_TREES[evo.specialization_path]
            result['tree_name'] = tree['name']
            result['tree_description'] = tree['description']
            result['tree_tiers'] = tree['tiers']
        return result

    @staticmethod
    def award_evolution_xp(db: Session, agent_id: str, amount: int,
                            source: str = '') -> Dict:
        """Award evolution XP to an agent, checking for generation advancement."""
        evo = AgentEvolutionService.get_or_create_evolution(db, agent_id)
        evo.evolution_xp += amount

        advanced = False
        while evo.evolution_xp >= evo.evolution_xp_next:
            evo.evolution_xp -= evo.evolution_xp_next
            evo.generation += 1
            evo.evolution_xp_next = xp_for_generation(evo.generation + 1)
            advanced = True

            # Update specialization tier
            if evo.specialization_path and evo.specialization_path in SPECIALIZATION_TREES:
                tree = SPECIALIZATION_TREES[evo.specialization_path]
                tiers = tree['tiers']
                tier_idx = min((evo.generation - 3) // 5, len(tiers) - 1)
                tier_idx = max(0, tier_idx)
                evo.spec_tier = tiers[tier_idx]

        return {
            'generation': evo.generation,
            'evolution_xp': evo.evolution_xp,
            'evolution_xp_next': evo.evolution_xp_next,
            'advanced': advanced,
            'specialization': evo.specialization_path,
            'spec_tier': evo.spec_tier,
        }

    @staticmethod
    def specialize(db: Session, agent_id: str, path: str) -> Optional[Dict]:
        """Choose a specialization path (available at generation 3+)."""
        if path not in SPECIALIZATION_TREES:
            return None

        evo = AgentEvolutionService.get_or_create_evolution(db, agent_id)
        if evo.generation < 3:
            return None
        if evo.specialization_path:
            return None  # Already specialized

        evo.specialization_path = path
        tree = SPECIALIZATION_TREES[path]
        evo.spec_tier = tree['tiers'][0]

        return {
            'specialization': path,
            'spec_tier': evo.spec_tier,
            'tree_name': tree['name'],
            'tree_description': tree['description'],
        }

    @staticmethod
    def record_collaboration(db: Session, agent_a_id: str, agent_b_id: str,
                              task_id: str = None,
                              collaboration_type: str = 'co_task',
                              quality_score: float = 1.0) -> Dict:
        """Record a collaboration between two agents."""
        collab = AgentCollaboration(
            agent_a_id=agent_a_id,
            agent_b_id=agent_b_id,
            task_id=task_id,
            collaboration_type=collaboration_type,
            quality_score=quality_score,
        )
        db.add(collab)

        # Update collaboration counts and bonus
        for aid in [agent_a_id, agent_b_id]:
            evo = AgentEvolutionService.get_or_create_evolution(db, aid)
            evo.total_collaborations = (evo.total_collaborations or 0) + 1
            # Collaboration bonus grows with count, capped at 2.0x
            evo.collaboration_bonus = min(
                1.0 + (evo.total_collaborations * 0.02), 2.0
            )

        db.flush()
        return collab.to_dict()

    @staticmethod
    def get_collaborations(db: Session, agent_id: str,
                            limit: int = 50, offset: int = 0) -> List[Dict]:
        """Get collaboration history for an agent."""
        from sqlalchemy import or_
        collabs = db.query(AgentCollaboration).filter(
            or_(
                AgentCollaboration.agent_a_id == agent_id,
                AgentCollaboration.agent_b_id == agent_id,
            )
        ).order_by(desc(AgentCollaboration.created_at)).offset(offset).limit(limit).all()
        return [c.to_dict() for c in collabs]

    @staticmethod
    def get_agent_leaderboard(db: Session, limit: int = 50,
                               offset: int = 0) -> List[Dict]:
        """Get agent leaderboard by generation and evolution XP."""
        rows = db.query(AgentEvolution, User).join(
            User, User.id == AgentEvolution.user_id
        ).order_by(
            desc(AgentEvolution.generation),
            desc(AgentEvolution.evolution_xp),
        ).offset(offset).limit(limit).all()

        result = []
        for i, (evo, user) in enumerate(rows, start=offset + 1):
            entry = evo.to_dict()
            entry['rank'] = i
            entry['username'] = user.username
            entry['display_name'] = user.display_name
            entry['avatar_url'] = user.avatar_url
            result.append(entry)
        return result

    @staticmethod
    def get_showcase(db: Session, limit: int = 20) -> List[Dict]:
        """Get top agents for showcase."""
        rows = db.query(AgentEvolution, User, ResonanceWallet).join(
            User, User.id == AgentEvolution.user_id
        ).outerjoin(
            ResonanceWallet, ResonanceWallet.user_id == AgentEvolution.user_id
        ).order_by(
            desc(AgentEvolution.generation),
        ).limit(limit).all()

        result = []
        for evo, user, wallet in rows:
            entry = {
                'user_id': user.id,
                'username': user.username,
                'display_name': user.display_name,
                'avatar_url': user.avatar_url,
                'generation': evo.generation,
                'specialization': evo.specialization_path,
                'spec_tier': evo.spec_tier,
                'total_tasks': evo.total_tasks,
                'total_collaborations': evo.total_collaborations,
            }
            if wallet:
                entry['pulse'] = wallet.pulse
                entry['level'] = wallet.level
            result.append(entry)
        return result
