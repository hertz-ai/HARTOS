"""
HevolveSocial - Encounter Service
Serendipity encounters, bond tracking, connection suggestions.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from sqlalchemy import desc, func, or_, and_
from sqlalchemy.orm import Session

from .models import User, Encounter

logger = logging.getLogger('hevolve_social')


class EncounterService:

    @staticmethod
    def record_encounter(db: Session, user_a_id: str, user_b_id: str,
                         context_type: str, context_id: str = None,
                         location_label: str = '') -> Optional[Dict]:
        """Record an encounter between two users in a shared context.
        Auto-triggered by same-post comments, same-submolt activity,
        same-challenge participation, same-region, task collaboration."""
        if user_a_id == user_b_id:
            return None

        # Canonical ordering to avoid duplicates
        a_id, b_id = sorted([user_a_id, user_b_id])

        existing = db.query(Encounter).filter_by(
            user_a_id=a_id, user_b_id=b_id,
            context_type=context_type, context_id=context_id,
        ).first()

        if existing:
            existing.encounter_count += 1
            existing.latest_at = datetime.utcnow()
            # Bond level increases with repeated encounters
            if existing.encounter_count >= 50:
                existing.bond_level = min(10, existing.bond_level + 1)
            elif existing.encounter_count >= 20:
                existing.bond_level = max(existing.bond_level, 7)
            elif existing.encounter_count >= 10:
                existing.bond_level = max(existing.bond_level, 5)
            elif existing.encounter_count >= 5:
                existing.bond_level = max(existing.bond_level, 3)
            elif existing.encounter_count >= 2:
                existing.bond_level = max(existing.bond_level, 1)
            return existing.to_dict()

        enc = Encounter(
            user_a_id=a_id,
            user_b_id=b_id,
            context_type=context_type,
            context_id=context_id,
            location_label=location_label,
            encounter_count=1,
            bond_level=0,
            first_at=datetime.utcnow(),
            latest_at=datetime.utcnow(),
        )
        db.add(enc)
        db.flush()
        return enc.to_dict()

    @staticmethod
    def get_encounters(db: Session, user_id: str,
                       limit: int = 50, offset: int = 0) -> List[Dict]:
        """Get all encounters for a user, sorted by most recent."""
        encounters = db.query(Encounter).filter(
            or_(
                Encounter.user_a_id == user_id,
                Encounter.user_b_id == user_id,
            )
        ).order_by(desc(Encounter.latest_at)).offset(offset).limit(limit).all()

        result = []
        for enc in encounters:
            entry = enc.to_dict()
            # Determine the other user
            other_id = enc.user_b_id if enc.user_a_id == user_id else enc.user_a_id
            other_user = db.query(User).filter_by(id=other_id).first()
            if other_user:
                entry['other_user'] = {
                    'id': other_user.id,
                    'username': other_user.username,
                    'display_name': other_user.display_name,
                    'avatar_url': other_user.avatar_url,
                    'user_type': other_user.user_type,
                }
            result.append(entry)
        return result

    @staticmethod
    def get_encounters_with(db: Session, user_id: str,
                            other_user_id: str) -> List[Dict]:
        """Get all encounter contexts between two specific users."""
        a_id, b_id = sorted([user_id, other_user_id])
        encounters = db.query(Encounter).filter_by(
            user_a_id=a_id, user_b_id=b_id,
        ).order_by(desc(Encounter.latest_at)).all()
        return [e.to_dict() for e in encounters]

    @staticmethod
    def acknowledge_encounter(db: Session, encounter_id: str,
                              user_id: str) -> Optional[Dict]:
        """Mark an encounter as mutually acknowledged."""
        enc = db.query(Encounter).filter_by(id=encounter_id).first()
        if not enc:
            return None
        if user_id not in (enc.user_a_id, enc.user_b_id):
            return None
        enc.is_mutual_aware = True
        return enc.to_dict()

    @staticmethod
    def get_suggestions(db: Session, user_id: str, limit: int = 10) -> List[Dict]:
        """Get connection suggestions based on encounter patterns.
        Users with multiple encounters across different contexts."""
        # Find users with encounters, sorted by total encounter count
        subq = db.query(
            Encounter.user_a_id, Encounter.user_b_id,
            func.sum(Encounter.encounter_count).label('total_encounters'),
            func.count(Encounter.id).label('context_count'),
            func.max(Encounter.bond_level).label('max_bond'),
        ).filter(
            or_(
                Encounter.user_a_id == user_id,
                Encounter.user_b_id == user_id,
            )
        ).group_by(
            Encounter.user_a_id, Encounter.user_b_id,
        ).having(
            func.sum(Encounter.encounter_count) >= 2
        ).order_by(
            desc('total_encounters')
        ).limit(limit * 2).all()

        result = []
        seen = set()
        for row in subq:
            other_id = row[1] if row[0] == user_id else row[0]
            if other_id in seen:
                continue
            seen.add(other_id)

            other_user = db.query(User).filter_by(id=other_id).first()
            if not other_user:
                continue

            result.append({
                'user_id': other_user.id,
                'username': other_user.username,
                'display_name': other_user.display_name,
                'avatar_url': other_user.avatar_url,
                'user_type': other_user.user_type,
                'total_encounters': row[2],
                'shared_contexts': row[3],
                'max_bond': row[4],
            })

            if len(result) >= limit:
                break

        return result

    @staticmethod
    def get_bonds(db: Session, user_id: str, min_bond: int = 1) -> List[Dict]:
        """Get users the current user has formed bonds with."""
        encounters = db.query(Encounter).filter(
            or_(
                Encounter.user_a_id == user_id,
                Encounter.user_b_id == user_id,
            ),
            Encounter.bond_level >= min_bond,
        ).order_by(desc(Encounter.bond_level)).all()

        # Aggregate by other user
        bond_map = {}
        for enc in encounters:
            other_id = enc.user_b_id if enc.user_a_id == user_id else enc.user_a_id
            if other_id not in bond_map or enc.bond_level > bond_map[other_id]['bond_level']:
                bond_map[other_id] = {
                    'bond_level': enc.bond_level,
                    'total_encounters': enc.encounter_count,
                    'latest_at': enc.latest_at,
                }

        result = []
        for other_id, info in sorted(bond_map.items(), key=lambda x: -x[1]['bond_level']):
            other_user = db.query(User).filter_by(id=other_id).first()
            if other_user:
                result.append({
                    'user_id': other_user.id,
                    'username': other_user.username,
                    'display_name': other_user.display_name,
                    'avatar_url': other_user.avatar_url,
                    'bond_level': info['bond_level'],
                    'total_encounters': info['total_encounters'],
                    'latest_at': info['latest_at'].isoformat() if info['latest_at'] else None,
                })
        return result

    @staticmethod
    def get_nearby_active(db: Session, user_id: str, region_id: str = None,
                          hours: int = 24) -> List[Dict]:
        """Get users active in the same region recently."""
        if not region_id:
            user = db.query(User).filter_by(id=user_id).first()
            if not user or not user.region_id:
                return []
            region_id = user.region_id

        cutoff = datetime.utcnow() - timedelta(hours=hours)
        # Users in same region who were active recently
        users = db.query(User).filter(
            User.region_id == region_id,
            User.id != user_id,
            User.last_active_at >= cutoff if hasattr(User, 'last_active_at') else True,
        ).limit(20).all()

        return [{
            'user_id': u.id,
            'username': u.username,
            'display_name': u.display_name,
            'avatar_url': u.avatar_url,
            'user_type': u.user_type,
        } for u in users]
