"""
HevolveSocial - Service Layer
All business logic for posts, comments, votes, users, communities, follows, notifications.
"""
import re
import uuid
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

logger = logging.getLogger('hevolve_social')

from sqlalchemy import desc, asc, func, event
from sqlalchemy.orm import Session, joinedload

from .models import (
    User, Post, Comment, Vote, Follow, Community, CommunityMembership,
    Notification, Report, TaskRequest, RecipeShare, AgentSkillBadge
)
from .auth import hash_password, verify_password, generate_api_token, generate_jwt


def _uuid():
    return str(uuid.uuid4())


# ─── User Service ───

class UserService:

    @staticmethod
    def register(db: Session, username: str, password: str, email: str = None,
                 display_name: str = None, user_type: str = 'human') -> User:
        if db.query(User).filter(User.username == username).first():
            raise ValueError("Registration failed - username or email may already be in use")
        if email:
            # Basic email format validation
            if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
                raise ValueError("Invalid email address format")
            if db.query(User).filter(User.email == email).first():
                raise ValueError("Registration failed - username or email may already be in use")

        user = User(
            id=_uuid(), username=username, display_name=display_name or username,
            email=email, password_hash=hash_password(password),
            user_type=user_type, api_token=generate_api_token(),
            is_verified=True,
        )
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def register_agent(db: Session, name: str, description: str = '',
                       agent_id: str = None, owner_id: str = None,
                       skip_name_validation: bool = False) -> User:
        name = name.strip().lower()

        # Validate 3-word name format (skip for legacy/internal callers)
        if not skip_name_validation:
            from .agent_naming import validate_agent_name
            valid, error = validate_agent_name(name)
            if not valid:
                raise ValueError(error)

        existing = db.query(User).filter(User.username == name).first()
        if existing:
            raise ValueError(f"Name '{name}' is already taken globally")

        user = User(
            id=_uuid(), username=name, display_name=name,
            bio=description, user_type='agent', agent_id=agent_id,
            owner_id=owner_id,
            api_token=generate_api_token(), is_verified=True,
        )
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def get_owned_agents(db: Session, owner_id: str) -> list:
        """Get all agents owned by a user."""
        return db.query(User).filter(
            User.owner_id == owner_id,
            User.user_type == 'agent',
        ).all()

    # Account lockout: track failed attempts per username
    _login_attempts = {}  # username -> (count, first_attempt_at)
    _login_lock = threading.Lock()
    _MAX_ATTEMPTS = 5
    _LOCKOUT_MINUTES = 15
    _LOGIN_ATTEMPT_TTL = timedelta(minutes=30)

    @staticmethod
    def _cleanup_login_attempts():
        """Remove expired login attempt entries (called under _login_lock)."""
        now = datetime.utcnow()
        expired = [k for k, (_, first_at) in UserService._login_attempts.items()
                   if now - first_at > UserService._LOGIN_ATTEMPT_TTL]
        for k in expired:
            del UserService._login_attempts[k]

    @staticmethod
    def login(db: Session, username: str, password: str) -> Tuple[User, str]:
        # Periodically purge stale login attempt records
        with UserService._login_lock:
            UserService._cleanup_login_attempts()
        # Check lockout
        with UserService._login_lock:
            entry = UserService._login_attempts.get(username)
            if entry:
                count, first_at = entry
                elapsed = (datetime.utcnow() - first_at).total_seconds()
                if count >= UserService._MAX_ATTEMPTS and elapsed < UserService._LOCKOUT_MINUTES * 60:
                    remaining = int(UserService._LOCKOUT_MINUTES - elapsed / 60)
                    raise ValueError(f"Account temporarily locked. Try again in {remaining} minutes")
                if elapsed >= UserService._LOCKOUT_MINUTES * 60:
                    del UserService._login_attempts[username]

        user = db.query(User).filter(User.username == username).first()
        # Always run password verification to prevent timing-based user enumeration
        _dummy_hash = "0" * 32 + ":" + "0" * 64
        if not verify_password(password, user.password_hash if user else _dummy_hash):
            # Record failed attempt
            with UserService._login_lock:
                entry = UserService._login_attempts.get(username)
                if entry:
                    UserService._login_attempts[username] = (entry[0] + 1, entry[1])
                else:
                    UserService._login_attempts[username] = (1, datetime.utcnow())
            raise ValueError("Invalid username or password")
        if not user:
            raise ValueError("Invalid username or password")
        if user.is_banned:
            raise ValueError("Account is banned")
        # Clear lockout on successful login
        with UserService._login_lock:
            UserService._login_attempts.pop(username, None)
        token = generate_jwt(user.id, user.username, getattr(user, 'role', None) or 'flat')
        user.last_active_at = datetime.utcnow()
        db.flush()
        return user, token

    @staticmethod
    def set_user_role(db: Session, user, role: str):
        """Set user role and sync legacy boolean flags."""
        user.role = role
        user.is_admin = (role == 'central')
        user.is_moderator = (role in ('central', 'regional'))
        db.flush()

    @staticmethod
    def get_by_id(db: Session, user_id: str) -> Optional[User]:
        return db.query(User).filter(User.id == user_id).first()

    @staticmethod
    def get_by_username(db: Session, username: str) -> Optional[User]:
        return db.query(User).filter(User.username == username).first()

    @staticmethod
    def list_users(db: Session, user_type: str = None, limit: int = 25,
                   offset: int = 0) -> Tuple[List[User], int]:
        q = db.query(User).filter(User.is_banned == False)
        if user_type:
            q = q.filter(User.user_type == user_type)
        total = q.count()
        users = q.order_by(desc(User.karma_score)).offset(offset).limit(limit).all()
        return users, total

    @staticmethod
    def set_handle(db: Session, user: User, handle: str) -> User:
        """Set a user's unique creator handle (used as suffix in global agent names)."""
        from .agent_naming import validate_handle, is_handle_available
        handle = handle.strip().lower()
        valid, error = validate_handle(handle)
        if not valid:
            raise ValueError(error)
        if not is_handle_available(db, handle):
            raise ValueError(f"Handle '{handle}' is already taken")
        user.handle = handle
        user.updated_at = datetime.utcnow()
        db.flush()
        return user

    @staticmethod
    def register_agent_local(db: Session, local_name: str, description: str = '',
                              agent_id: str = None, owner: User = None) -> User:
        """
        Register an agent using a 2-word local name + owner's handle.
        The global username becomes: {local_name}.{handle} (e.g. swift.falcon.sathi).
        """
        from .agent_naming import (validate_local_name, compose_global_name,
                                    check_global_availability)

        if not owner:
            raise ValueError("Owner is required")
        if not owner.handle:
            raise ValueError("You need to set a handle before creating agents")

        local_name = local_name.strip().lower()
        valid, error = validate_local_name(local_name)
        if not valid:
            raise ValueError(error)

        # Check local uniqueness (same owner can't have two agents with same local name)
        existing_local = db.query(User).filter(
            User.owner_id == owner.id,
            User.local_name == local_name,
        ).first()
        if existing_local:
            raise ValueError(f"You already have an agent named '{local_name}'")

        # Check global availability
        available, global_name, err = check_global_availability(db, local_name, owner.handle)
        if not available:
            raise ValueError(
                f"'{global_name}' is already taken globally. "
                f"Please choose a different name for your agent."
            )

        user = User(
            id=_uuid(), username=global_name, display_name=local_name,
            local_name=local_name, bio=description, user_type='agent',
            agent_id=agent_id, owner_id=owner.id,
            api_token=generate_api_token(), is_verified=True,
        )
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def update_profile(db: Session, user: User, display_name: str = None,
                       bio: str = None, avatar_url: str = None,
                       handle: str = None) -> User:
        if display_name is not None:
            user.display_name = display_name
        if bio is not None:
            user.bio = bio
        if avatar_url is not None:
            user.avatar_url = avatar_url
        if handle is not None and user.user_type == 'human':
            UserService.set_handle(db, user, handle)
        user.updated_at = datetime.utcnow()
        db.flush()
        return user


# ─── Post Service ───

class PostService:

    @staticmethod
    def create(db: Session, author: User, title: str, content: str = '',
               content_type: str = 'text', community_name: str = None,
               code_language: str = None, media_urls: list = None,
               link_url: str = None, source_channel: str = None,
               source_message_id: str = None,
               intent_category: str = None, hypothesis: str = None,
               expected_outcome: str = None, is_thought_experiment: bool = False,
               dynamic_layout: dict = None) -> Post:
        community_id = None
        if community_name:
            community = db.query(Community).filter(Community.name == community_name).first()
            if community:
                community_id = community.id
                community.post_count += 1

        post = Post(
            id=_uuid(), author_id=author.id, community_id=community_id,
            title=title, content=content, content_type=content_type,
            code_language=code_language, media_urls=media_urls or [],
            link_url=link_url, source_channel=source_channel,
            source_message_id=source_message_id,
            intent_category=intent_category, hypothesis=hypothesis,
            expected_outcome=expected_outcome,
            is_thought_experiment=is_thought_experiment or False,
            dynamic_layout=dynamic_layout,
        )
        db.add(post)
        author.post_count += 1
        db.flush()

        # Resonance: award Spark + Signal + XP for creating a post
        try:
            from .resonance_engine import ResonanceService
            ResonanceService.award_action(db, author.id, 'create_post', post.id)
        except Exception:
            pass

        try:
            from .gamification_service import GamificationService
            GamificationService.check_achievements(db, author.id)
        except Exception:
            pass

        try:
            from .onboarding_service import OnboardingService
            OnboardingService.auto_advance(db, author.id, 'post')
        except Exception:
            pass

        return post

    @staticmethod
    def get_by_id(db: Session, post_id: str) -> Optional[Post]:
        return db.query(Post).options(
            joinedload(Post.author)
        ).filter(Post.id == post_id, Post.is_deleted == False).first()

    @staticmethod
    def list_posts(db: Session, sort: str = 'new', community_name: str = None,
                   author_id: str = None, limit: int = 25, offset: int = 0
                   ) -> Tuple[List[Post], int]:
        q = db.query(Post).options(joinedload(Post.author)).filter(
            Post.is_deleted == False, Post.is_hidden == False
        )
        if community_name:
            community = db.query(Community).filter(Community.name == community_name).first()
            if community:
                q = q.filter(Post.community_id == community.id)
        if author_id:
            q = q.filter(Post.author_id == author_id)

        if sort == 'top':
            q = q.order_by(desc(Post.score), desc(Post.created_at))
        elif sort == 'hot':
            # Hot = score weighted by recency
            q = q.order_by(desc(Post.score + Post.comment_count), desc(Post.created_at))
        elif sort == 'discussed':
            q = q.order_by(desc(Post.comment_count), desc(Post.created_at))
        else:  # 'new'
            q = q.order_by(desc(Post.created_at))

        total = q.count()
        posts = q.offset(offset).limit(limit).all()
        return posts, total

    @staticmethod
    def update(db: Session, post: Post, title: str = None, content: str = None,
               intent_category: str = None, hypothesis: str = None,
               expected_outcome: str = None, is_thought_experiment: bool = None,
               dynamic_layout: dict = None) -> Post:
        if title is not None:
            post.title = title
        if content is not None:
            post.content = content
        if intent_category is not None:
            post.intent_category = intent_category
        if hypothesis is not None:
            post.hypothesis = hypothesis
        if expected_outcome is not None:
            post.expected_outcome = expected_outcome
        if is_thought_experiment is not None:
            post.is_thought_experiment = is_thought_experiment
        if dynamic_layout is not None:
            post.dynamic_layout = dynamic_layout
        post.updated_at = datetime.utcnow()
        db.flush()
        return post

    @staticmethod
    def delete(db: Session, post: Post):
        post.is_deleted = True
        post.author.post_count = max(0, post.author.post_count - 1)
        if post.community_id:
            community = db.query(Community).filter_by(id=post.community_id).first()
            if community:
                community.post_count = max(0, (community.post_count or 0) - 1)
        db.flush()

    @staticmethod
    def increment_view(db: Session, post: Post):
        post.view_count += 1
        db.flush()


# ─── Comment Service ───

class CommentService:

    @staticmethod
    def create(db: Session, post: Post, author: User, content: str,
               parent_id: str = None) -> Comment:
        depth = 0
        if parent_id:
            parent = db.query(Comment).filter(Comment.id == parent_id).first()
            if parent:
                depth = parent.depth + 1

        comment = Comment(
            id=_uuid(), post_id=post.id, author_id=author.id,
            parent_id=parent_id, content=content, depth=depth,
        )
        db.add(comment)
        post.comment_count += 1
        author.comment_count += 1
        db.flush()

        # Notify post author
        if post.author_id != author.id:
            NotificationService.create(
                db, post.author_id, 'comment', author.id, 'post', post.id,
                f"{author.display_name} commented on your post"
            )
        # Notify parent comment author
        if parent_id:
            parent = db.query(Comment).filter(Comment.id == parent_id).first()
            if parent and parent.author_id != author.id:
                NotificationService.create(
                    db, parent.author_id, 'reply', author.id, 'comment', parent.id,
                    f"{author.display_name} replied to your comment"
                )

        # Resonance: award Spark + Signal + XP for creating a comment
        try:
            from .resonance_engine import ResonanceService
            ResonanceService.award_action(db, author.id, 'create_comment', comment.id)
        except Exception:
            pass

        try:
            from .gamification_service import GamificationService
            GamificationService.check_achievements(db, author.id)
        except Exception:
            pass

        try:
            from .onboarding_service import OnboardingService
            OnboardingService.auto_advance(db, author.id, 'comment')
        except Exception:
            pass

        try:
            from .encounter_service import EncounterService
            # Record encounter between commenter and post author
            if post and post.author_id and post.author_id != author.id:
                EncounterService.record_encounter(
                    db, author.id, post.author_id,
                    'post', post.id)
        except Exception:
            pass

        return comment

    @staticmethod
    def get_by_post(db: Session, post_id: str, sort: str = 'new'
                    ) -> List[Comment]:
        q = db.query(Comment).options(joinedload(Comment.author)).filter(
            Comment.post_id == post_id, Comment.is_deleted == False,
            Comment.is_hidden == False
        )
        if sort == 'top':
            q = q.order_by(desc(Comment.score), desc(Comment.created_at))
        else:
            q = q.order_by(asc(Comment.created_at))
        return q.all()

    @staticmethod
    def delete(db: Session, comment: Comment):
        comment.is_deleted = True
        comment.content = '[deleted]'
        db.flush()


# ─── Vote Service ───

# Note: with_for_update() is a no-op on SQLite (no SELECT ... FOR UPDATE support).
# Use Python-level lock as SQLite workaround for concurrent vote operations.
# The with_for_update() calls are kept for PostgreSQL compatibility.
_vote_lock = threading.Lock()


class VoteService:

    @staticmethod
    def vote(db: Session, user: User, target_type: str, target_id: str,
             value: int) -> dict:
        """Cast or change a vote. value: +1 (upvote) or -1 (downvote)."""
        with _vote_lock:
            return VoteService._vote_inner(db, user, target_type, target_id, value)

    @staticmethod
    def _vote_inner(db: Session, user: User, target_type: str, target_id: str,
                    value: int) -> dict:
        """Inner vote logic, must be called under _vote_lock."""
        existing = db.query(Vote).filter(
            Vote.user_id == user.id, Vote.target_type == target_type,
            Vote.target_id == target_id
        ).first()

        # Get target object (with_for_update prevents concurrent vote race on PostgreSQL;
        # no-op on SQLite — Python _vote_lock provides safety there)
        if target_type == 'post':
            target = db.query(Post).filter(Post.id == target_id).with_for_update().first()
        else:
            target = db.query(Comment).filter(Comment.id == target_id).with_for_update().first()
        if not target:
            raise ValueError(f"{target_type} not found")

        if existing:
            if existing.value == value:
                # Remove vote (toggle off)
                if value == 1:
                    target.upvotes -= 1
                else:
                    target.downvotes -= 1
                target.score = target.upvotes - target.downvotes
                db.delete(existing)
                db.flush()
                return {'action': 'removed', 'score': target.score}
            else:
                # Change vote direction
                if existing.value == 1:
                    target.upvotes -= 1
                else:
                    target.downvotes -= 1
                existing.value = value
                if value == 1:
                    target.upvotes += 1
                else:
                    target.downvotes += 1
                target.score = target.upvotes - target.downvotes
                db.flush()
                return {'action': 'changed', 'score': target.score}
        else:
            vote = Vote(id=_uuid(), user_id=user.id, target_type=target_type,
                        target_id=target_id, value=value)
            db.add(vote)
            if value == 1:
                target.upvotes += 1
            else:
                target.downvotes += 1
            target.score = target.upvotes - target.downvotes
            db.flush()

            # Notify author on upvote
            if value == 1:
                author_id = target.author_id
                if author_id != user.id:
                    NotificationService.create(
                        db, author_id, 'upvote', user.id, target_type, target_id,
                        f"{user.display_name} upvoted your {target_type}"
                    )
                    # Resonance: award Pulse to author for receiving upvote
                    try:
                        from .resonance_engine import ResonanceService
                        ResonanceService.award_action(db, author_id, 'post_upvote', target_id)
                    except Exception:
                        pass

            try:
                from .gamification_service import GamificationService
                GamificationService.check_achievements(db, user.id)
            except Exception:
                pass

            try:
                from .onboarding_service import OnboardingService
                OnboardingService.auto_advance(db, user.id, 'vote')
            except Exception:
                pass

            return {'action': 'voted', 'score': target.score}

    @staticmethod
    def remove_vote(db: Session, user: User, target_type: str, target_id: str):
        existing = db.query(Vote).filter(
            Vote.user_id == user.id, Vote.target_type == target_type,
            Vote.target_id == target_id
        ).first()
        if existing:
            if target_type == 'post':
                target = db.query(Post).filter(Post.id == target_id).first()
            else:
                target = db.query(Comment).filter(Comment.id == target_id).first()
            if target:
                if existing.value == 1:
                    target.upvotes -= 1
                else:
                    target.downvotes -= 1
                target.score = target.upvotes - target.downvotes
            db.delete(existing)
            db.flush()

    @staticmethod
    def get_voters(db: Session, target_type: str, target_id: str) -> List[dict]:
        """Get list of users who voted (for RN compatibility: like_bypost)."""
        votes = db.query(Vote, User).join(User, Vote.user_id == User.id).filter(
            Vote.target_type == target_type, Vote.target_id == target_id,
            Vote.value == 1
        ).all()
        return [{'user_id': u.id, 'name': u.display_name,
                 'profilePic': u.avatar_url} for v, u in votes]


# ─── Follow Service ───

class FollowService:

    @staticmethod
    def follow(db: Session, follower: User, following_id: str) -> bool:
        if follower.id == following_id:
            raise ValueError("Cannot follow yourself")
        target = db.query(User).filter(User.id == following_id).first()
        if not target:
            raise ValueError("User not found")

        existing = db.query(Follow).filter(
            Follow.follower_id == follower.id, Follow.following_id == following_id
        ).first()
        if existing:
            return False  # already following

        follow = Follow(id=_uuid(), follower_id=follower.id, following_id=following_id)
        db.add(follow)
        db.flush()
        NotificationService.create(
            db, following_id, 'follow', follower.id, 'profile', follower.id,
            f"{follower.display_name} started following you"
        )
        return True

    @staticmethod
    def unfollow(db: Session, follower: User, following_id: str):
        existing = db.query(Follow).filter(
            Follow.follower_id == follower.id, Follow.following_id == following_id
        ).first()
        if existing:
            db.delete(existing)
            db.flush()

    @staticmethod
    def get_followers(db: Session, user_id: str, limit: int = 50, offset: int = 0
                      ) -> Tuple[List[User], int]:
        q = db.query(User).join(Follow, Follow.follower_id == User.id).filter(
            Follow.following_id == user_id)
        total = q.count()
        users = q.offset(offset).limit(limit).all()
        return users, total

    @staticmethod
    def get_following(db: Session, user_id: str, limit: int = 50, offset: int = 0
                      ) -> Tuple[List[User], int]:
        q = db.query(User).join(Follow, Follow.following_id == User.id).filter(
            Follow.follower_id == user_id)
        total = q.count()
        users = q.offset(offset).limit(limit).all()
        return users, total

    @staticmethod
    def is_following(db: Session, follower_id: str, following_id: str) -> bool:
        return db.query(Follow).filter(
            Follow.follower_id == follower_id, Follow.following_id == following_id
        ).first() is not None


# ─── Community Service ───

class CommunityService:

    @staticmethod
    def create(db: Session, creator: User, name: str, display_name: str = '',
               description: str = '', rules: str = '', is_private: bool = False) -> Community:
        if db.query(Community).filter(Community.name == name).first():
            raise ValueError(f"Community '{name}' already exists")

        community = Community(
            id=_uuid(), name=name, display_name=display_name or name,
            description=description, rules=rules, creator_id=creator.id,
            is_private=is_private, member_count=1,
        )
        db.add(community)
        db.flush()
        # Auto-join creator as admin
        membership = CommunityMembership(
            id=_uuid(), user_id=creator.id, community_id=community.id, role='admin')
        db.add(membership)
        db.flush()
        return community

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Community]:
        return db.query(Community).filter(Community.name == name).first()

    @staticmethod
    def list_communities(db: Session, limit: int = 50, offset: int = 0
                         ) -> Tuple[List[Community], int]:
        q = db.query(Community).order_by(desc(Community.member_count))
        total = q.count()
        communities = q.offset(offset).limit(limit).all()
        return communities, total

    @staticmethod
    def join(db: Session, user: User, community: Community) -> bool:
        existing = db.query(CommunityMembership).filter(
            CommunityMembership.user_id == user.id,
            CommunityMembership.community_id == community.id
        ).first()
        if existing:
            return False
        membership = CommunityMembership(
            id=_uuid(), user_id=user.id, community_id=community.id)
        db.add(membership)
        community.member_count += 1
        db.flush()
        return True

    @staticmethod
    def leave(db: Session, user: User, community: Community):
        existing = db.query(CommunityMembership).filter(
            CommunityMembership.user_id == user.id,
            CommunityMembership.community_id == community.id
        ).first()
        if existing:
            db.delete(existing)
            community.member_count = max(0, community.member_count - 1)
            db.flush()

    @staticmethod
    def get_members(db: Session, community_id: str, limit: int = 50, offset: int = 0
                    ) -> Tuple[List[dict], int]:
        q = db.query(CommunityMembership, User).join(
            User, CommunityMembership.user_id == User.id
        ).filter(CommunityMembership.community_id == community_id)
        total = q.count()
        results = q.offset(offset).limit(limit).all()
        return [{'user': u.to_dict(), 'role': m.role} for m, u in results], total

    @staticmethod
    def get_user_role(db: Session, user_id: str, community_id: str) -> Optional[str]:
        m = db.query(CommunityMembership).filter(
            CommunityMembership.user_id == user_id,
            CommunityMembership.community_id == community_id
        ).first()
        return m.role if m else None


# ─── Notification Service ───

class NotificationService:

    @staticmethod
    def create(db: Session, user_id: str, type: str, source_user_id: str = None,
               target_type: str = None, target_id: str = None, message: str = ''):
        notif = Notification(
            id=_uuid(), user_id=user_id, type=type,
            source_user_id=source_user_id, target_type=target_type,
            target_id=target_id, message=message,
        )
        db.add(notif)
        db.flush()
        # Push to SSE + WAMP in real-time AFTER commit (fire-and-forget)
        # Defer notification to after_commit to ensure data consistency
        notif_dict = notif.to_dict()
        _uid = user_id
        def _push_after_commit(session):
            try:
                from .realtime import on_notification
                on_notification(_uid, notif_dict)
            except Exception:
                pass
        event.listen(db, 'after_commit', _push_after_commit, once=True)
        return notif

    @staticmethod
    def get_for_user(db: Session, user_id: str, unread_only: bool = False,
                     limit: int = 50, offset: int = 0) -> Tuple[List[Notification], int]:
        q = db.query(Notification).filter(Notification.user_id == user_id)
        if unread_only:
            q = q.filter(Notification.is_read == False)
        total = q.count()
        notifs = q.order_by(desc(Notification.created_at)).offset(offset).limit(limit).all()
        return notifs, total

    @staticmethod
    def mark_read(db: Session, notification_ids: List[str], user_id: str):
        db.query(Notification).filter(
            Notification.id.in_(notification_ids), Notification.user_id == user_id
        ).update({Notification.is_read: True}, synchronize_session=False)
        db.flush()

    @staticmethod
    def mark_all_read(db: Session, user_id: str):
        db.query(Notification).filter(
            Notification.user_id == user_id, Notification.is_read == False
        ).update({Notification.is_read: True}, synchronize_session=False)
        db.flush()


# ─── Report Service ───

class ReportService:

    @staticmethod
    def create(db: Session, reporter: User, target_type: str, target_id: str,
               reason: str, details: str = '') -> Report:
        report = Report(
            id=_uuid(), reporter_id=reporter.id, target_type=target_type,
            target_id=target_id, reason=reason, details=details,
        )
        db.add(report)
        db.flush()
        return report

    @staticmethod
    def list_reports(db: Session, status: str = None, limit: int = 50, offset: int = 0
                     ) -> Tuple[List[Report], int]:
        q = db.query(Report)
        if status:
            q = q.filter(Report.status == status)
        total = q.count()
        reports = q.order_by(desc(Report.created_at)).offset(offset).limit(limit).all()
        return reports, total

    @staticmethod
    def review(db: Session, report: Report, moderator_id: str, status: str):
        report.status = status
        report.moderator_id = moderator_id
        db.flush()
