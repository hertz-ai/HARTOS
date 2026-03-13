"""
HevolveSocial - Game Service
Multiplayer game session management: create, join, play, complete.
Ties into Resonance (rewards), Encounters (bonds), and Gamification (achievements).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from .models import GameSession, GameParticipant, User
from .resonance_engine import ResonanceService
from .encounter_service import EncounterService

logger = logging.getLogger('hevolve_social')

# ─── Constants ───

VALID_STATUSES = ('waiting', 'active', 'completed', 'expired', 'cancelled')
DEFAULT_EXPIRY_MINUTES = 30
MIN_PLAYERS = 2
MAX_PLAYERS_CAP = 8


class GameService:

    # ─── Session Lifecycle ───

    @staticmethod
    def create_session(db: Session, host_user_id: str, game_type: str,
                       config: Optional[Dict] = None,
                       encounter_id: str = None,
                       community_id: str = None,
                       challenge_id: str = None,
                       max_players: int = 4,
                       total_rounds: int = 5,
                       expiry_minutes: int = DEFAULT_EXPIRY_MINUTES) -> Dict:
        """Create a new game session in 'waiting' state. Host auto-joins."""
        from .game_types import is_valid_game_type
        if not is_valid_game_type(game_type):
            raise ValueError(f"Invalid game_type '{game_type}'")

        # Resolve catalog entry → merge engine_config into session config
        resolved_type = game_type
        merged_config = config or {}
        try:
            from .game_catalog import get_catalog_entry, get_config_for_catalog_entry
            catalog_entry = get_catalog_entry(game_type)
            if catalog_entry:
                resolved_type = catalog_entry['engine']
                merged_config = get_config_for_catalog_entry(game_type, config)
                # Use catalog defaults if not specified
                if total_rounds == 5 and catalog_entry.get('default_rounds'):
                    total_rounds = catalog_entry['default_rounds']
                if max_players == 4 and catalog_entry.get('max_players'):
                    max_players = catalog_entry['max_players']
        except ImportError:
            pass

        max_players = min(max(MIN_PLAYERS, max_players), MAX_PLAYERS_CAP)

        session = GameSession(
            game_type=resolved_type,
            host_user_id=host_user_id,
            encounter_id=encounter_id,
            community_id=community_id,
            challenge_id=challenge_id,
            max_players=max_players,
            total_rounds=total_rounds,
            config=merged_config,
            game_state={'round_data': [], 'moves': [],
                        'catalog_id': game_type if game_type != resolved_type else None},
            expires_at=datetime.utcnow() + timedelta(minutes=expiry_minutes),
        )
        db.add(session)
        db.flush()

        # Host auto-joins
        participant = GameParticipant(
            game_session_id=session.id,
            user_id=host_user_id,
            is_ready=True,
        )
        db.add(participant)
        db.flush()

        logger.info("Game session created: %s (%s) by %s", session.id, game_type, host_user_id)
        return session.to_dict()

    @staticmethod
    def join_session(db: Session, session_id: str, user_id: str) -> Dict:
        """Join an existing waiting game session."""
        session = db.query(GameSession).filter_by(id=session_id).first()
        if not session:
            raise ValueError("Game session not found")
        if session.status != 'waiting':
            raise ValueError(f"Cannot join — session is '{session.status}'")
        if len(session.participants) >= session.max_players:
            raise ValueError("Game is full")

        existing = db.query(GameParticipant).filter_by(
            game_session_id=session_id, user_id=user_id
        ).first()
        if existing:
            raise ValueError("Already in this game")

        participant = GameParticipant(
            game_session_id=session_id,
            user_id=user_id,
        )
        db.add(participant)
        db.flush()

        logger.info("User %s joined game %s", user_id, session_id)
        return session.to_dict()

    @staticmethod
    def set_ready(db: Session, session_id: str, user_id: str) -> Dict:
        """Mark a participant as ready."""
        participant = db.query(GameParticipant).filter_by(
            game_session_id=session_id, user_id=user_id
        ).first()
        if not participant:
            raise ValueError("Not in this game")
        participant.is_ready = True
        db.flush()
        session = db.query(GameSession).filter_by(id=session_id).first()
        return session.to_dict()

    @staticmethod
    def start_session(db: Session, session_id: str, host_user_id: str) -> Dict:
        """Host starts the game. All players must be ready, minimum 2 players."""
        session = db.query(GameSession).filter_by(id=session_id).first()
        if not session:
            raise ValueError("Game session not found")
        if session.host_user_id != host_user_id:
            raise ValueError("Only the host can start the game")
        if session.status != 'waiting':
            raise ValueError(f"Cannot start — session is '{session.status}'")
        if len(session.participants) < MIN_PLAYERS:
            raise ValueError(f"Need at least {MIN_PLAYERS} players to start")
        not_ready = [p for p in session.participants if not p.is_ready]
        if not_ready:
            raise ValueError(f"{len(not_ready)} player(s) not ready")

        session.status = 'active'
        session.started_at = datetime.utcnow()
        session.current_round = 1

        # Initialize game state via game type handler
        from .game_types import get_game_type
        handler = get_game_type(session.game_type)
        session.game_state = handler.initialize(session.config, session.total_rounds,
                                                [p.user_id for p in session.participants])
        db.flush()

        logger.info("Game %s started (%s, %d players)",
                     session_id, session.game_type, len(session.participants))
        return session.to_dict()

    @staticmethod
    def submit_move(db: Session, session_id: str, user_id: str,
                    move_data: Dict) -> Dict:
        """Submit a move in an active game. Delegates to game type handler."""
        session = db.query(GameSession).filter_by(id=session_id).first()
        if not session:
            raise ValueError("Game session not found")
        if session.status != 'active':
            raise ValueError(f"Cannot move — session is '{session.status}'")

        participant = db.query(GameParticipant).filter_by(
            game_session_id=session_id, user_id=user_id
        ).first()
        if not participant:
            raise ValueError("Not in this game")

        from .game_types import get_game_type
        handler = get_game_type(session.game_type)

        # Validate move
        valid, reason = handler.validate_move(session.game_state, user_id, move_data)
        if not valid:
            raise ValueError(f"Invalid move: {reason}")

        # Apply move
        new_state, score_delta = handler.apply_move(
            session.game_state, user_id, move_data)
        session.game_state = new_state
        participant.score += score_delta

        # Check round/game end
        if handler.check_round_end(session.game_state):
            session.current_round += 1
            if handler.check_game_end(session.game_state, session.current_round, session.total_rounds):
                return GameService._complete_session(db, session)

        db.flush()
        return session.to_dict()

    @staticmethod
    def _complete_session(db: Session, session: GameSession) -> Dict:
        """Finalize game: calculate results, award Resonance, record encounters."""
        from .game_types import get_game_type
        handler = get_game_type(session.game_type)

        results = handler.calculate_results(session.game_state, session.participants)

        session.status = 'completed'
        session.ended_at = datetime.utcnow()

        for participant in session.participants:
            result = results.get(participant.user_id, {})
            participant.result = result.get('result', 'draw')
            participant.finished_at = datetime.utcnow()

            # Award Resonance based on result
            if participant.result == 'win':
                award = ResonanceService.award_action(
                    db, participant.user_id, 'game_win', source_id=session.id)
            else:
                award = ResonanceService.award_action(
                    db, participant.user_id, 'game_participate', source_id=session.id)

            if award:
                participant.spark_earned = award.get('spark', 0)
                participant.xp_earned = award.get('xp', 0)

        # Record encounters between all participant pairs
        user_ids = [p.user_id for p in session.participants]
        for i, uid_a in enumerate(user_ids):
            for uid_b in user_ids[i + 1:]:
                EncounterService.record_encounter(
                    db, uid_a, uid_b,
                    context_type='game',
                    context_id=session.id,
                )
                # Also award multiplayer_encounter
                ResonanceService.award_action(
                    db, uid_a, 'multiplayer_encounter', source_id=session.id)
                ResonanceService.award_action(
                    db, uid_b, 'multiplayer_encounter', source_id=session.id)

        db.flush()

        logger.info("Game %s completed (%s). Results: %s",
                     session.id, session.game_type,
                     {p.user_id[:8]: p.result for p in session.participants})
        return session.to_dict()

    @staticmethod
    def leave_session(db: Session, session_id: str, user_id: str) -> Dict:
        """Leave a game. If host leaves a waiting game, cancel it."""
        session = db.query(GameSession).filter_by(id=session_id).first()
        if not session:
            raise ValueError("Game session not found")

        participant = db.query(GameParticipant).filter_by(
            game_session_id=session_id, user_id=user_id
        ).first()
        if not participant:
            raise ValueError("Not in this game")

        if session.status == 'waiting':
            if session.host_user_id == user_id:
                session.status = 'cancelled'
                session.ended_at = datetime.utcnow()
            else:
                db.delete(participant)
        elif session.status == 'active':
            participant.result = 'abandoned'
            participant.finished_at = datetime.utcnow()
            # If only 1 player left, auto-complete
            active_players = [p for p in session.participants
                              if p.result != 'abandoned' and p.user_id != user_id]
            if len(active_players) < MIN_PLAYERS:
                for p in active_players:
                    p.result = 'win'
                    p.finished_at = datetime.utcnow()
                    ResonanceService.award_action(
                        db, p.user_id, 'game_win', source_id=session.id)
                session.status = 'completed'
                session.ended_at = datetime.utcnow()

        db.flush()
        return session.to_dict()

    # ─── Discovery & History ───

    @staticmethod
    def find_open_sessions(db: Session, user_id: str,
                           game_type: str = None,
                           community_id: str = None,
                           limit: int = 20) -> List[Dict]:
        """List joinable game sessions (waiting, not full, not expired)."""
        query = db.query(GameSession).filter(
            GameSession.status == 'waiting',
            GameSession.expires_at > datetime.utcnow(),
        )
        if game_type:
            query = query.filter(GameSession.game_type == game_type)
        if community_id:
            query = query.filter(GameSession.community_id == community_id)

        sessions = query.order_by(desc(GameSession.created_at)).limit(limit).all()

        # Filter out full sessions and sessions user is already in
        result = []
        for s in sessions:
            if len(s.participants) < s.max_players:
                already_in = any(p.user_id == user_id for p in s.participants)
                d = s.to_dict()
                d['already_joined'] = already_in
                result.append(d)
        return result

    @staticmethod
    def get_session(db: Session, session_id: str) -> Optional[Dict]:
        """Get game session by ID."""
        session = db.query(GameSession).filter_by(id=session_id).first()
        return session.to_dict() if session else None

    @staticmethod
    def get_history(db: Session, user_id: str,
                    limit: int = 20, offset: int = 0) -> List[Dict]:
        """Get user's game history (completed/cancelled games)."""
        sessions = db.query(GameSession).join(GameParticipant).filter(
            GameParticipant.user_id == user_id,
            GameSession.status.in_(['completed', 'cancelled']),
        ).order_by(desc(GameSession.ended_at)).offset(offset).limit(limit).all()
        return [s.to_dict() for s in sessions]

    @staticmethod
    def create_from_encounter(db: Session, encounter_id: str, user_id: str,
                              game_type: str, config: Optional[Dict] = None) -> Dict:
        """Create a game session linked to an existing encounter."""
        return GameService.create_session(
            db, host_user_id=user_id, game_type=game_type,
            config=config, encounter_id=encounter_id,
            max_players=2, total_rounds=3,
            expiry_minutes=15,
        )

    @staticmethod
    def quick_match(db: Session, user_id: str,
                    game_type: str = 'trivia') -> Dict:
        """Auto-matchmake: join an open session or create a new one."""
        # Resolve catalog ID to engine name for open session search
        search_type = game_type
        try:
            from .game_catalog import get_catalog_entry
            entry = get_catalog_entry(game_type)
            if entry:
                search_type = entry['engine']
        except ImportError:
            pass

        open_sessions = GameService.find_open_sessions(
            db, user_id, game_type=search_type, limit=5)
        # Prefer sessions we haven't joined yet
        for s in open_sessions:
            if not s.get('already_joined'):
                return GameService.join_session(db, s['id'], user_id)
        # No open sessions — create one
        return GameService.create_session(db, user_id, game_type)

    # ─── Cleanup ───

    @staticmethod
    def expire_stale_sessions(db: Session) -> int:
        """Mark expired waiting sessions. Called periodically."""
        now = datetime.utcnow()
        stale = db.query(GameSession).filter(
            GameSession.status == 'waiting',
            GameSession.expires_at <= now,
        ).all()
        for s in stale:
            s.status = 'expired'
            s.ended_at = now
        db.flush()
        if stale:
            logger.info("Expired %d stale game sessions", len(stale))
        return len(stale)
