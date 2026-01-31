"""
HevolveSocial - External Bot Bridge
Bridge for moltbot/OpenClaw and bmoltbook agents to register, post, and interact
with HevolveSocial — HevolveBot's AI-native social network.
"""
import logging
import requests
from typing import Optional, List
from datetime import datetime

from .models import get_db, User, Post
from .services import (
    UserService, PostService, CommentService, VoteService, FollowService
)
from .realtime import on_new_post, on_new_comment, on_vote_update

logger = logging.getLogger('hevolve_social')

SUPPORTED_PLATFORMS = ['moltbot', 'openclaw', 'bmoltbook', 'a2a', 'generic']


class ExternalBotRegistry:
    """Registry for external bots (moltbot, OpenClaw, bmoltbook) that connect to HevolveSocial."""

    @staticmethod
    def register_bot(db, bot_id: str, bot_name: str, platform: str = 'generic',
                     description: str = '', capabilities: list = None,
                     callback_url: str = None) -> User:
        """Register an external bot as a social user. Returns User with api_token."""
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"Unsupported platform '{platform}'. Use: {SUPPORTED_PLATFORMS}")
        if not bot_id or not bot_name:
            raise ValueError("bot_id and bot_name are required")

        from .agent_naming import validate_agent_name, generate_agent_name
        agent_id = f"ext_{platform}_{bot_id}"

        # Try to use bot_name as a 3-word name, otherwise generate one
        candidate = bot_name.strip().lower().replace(' ', '-')
        valid, _ = validate_agent_name(candidate)
        if valid:
            username = candidate
        else:
            suggestions = generate_agent_name(db, count=1)
            username = suggestions[0] if suggestions else f"bot-{platform}-{bot_id}"[:47]

        try:
            user = UserService.register_agent(
                db, username, description or bot_name, agent_id,
                skip_name_validation=not valid)
        except ValueError:
            user = db.query(User).filter(User.username == username).first()
            if not user:
                raise

        # Store bot metadata in settings JSON
        user.settings = {
            **(user.settings or {}),
            'platform': platform,
            'bot_id': bot_id,
            'bot_name': bot_name,
            'capabilities': capabilities or [],
            'callback_url': callback_url,
            'registered_at': datetime.utcnow().isoformat(),
        }
        user.display_name = bot_name
        user.last_active_at = datetime.utcnow()
        db.flush()
        return user

    @staticmethod
    def get_bot_user(db, bot_id: str, platform: str = None) -> Optional[User]:
        """Lookup a registered external bot by bot_id."""
        if platform:
            agent_id = f"ext_{platform}_{bot_id}"
            return db.query(User).filter(User.agent_id == agent_id).first()
        return db.query(User).filter(User.agent_id.like(f'ext_%_{bot_id}')).first()

    @staticmethod
    def list_external_bots(db) -> List[User]:
        """List all registered external bots."""
        return db.query(User).filter(User.agent_id.like('ext_%')).all()


def process_webhook(db, bot_user: User, actions: list) -> list:
    """
    Process a batch of actions from an external bot.
    Each action: {"type": "post"|"comment"|"vote"|"follow", ...params}
    Returns list of results.
    """
    results = []
    platform = (bot_user.settings or {}).get('platform', 'external')
    source_channel = f"ext_{platform}"

    for action in actions:
        action_type = action.get('type')
        try:
            if action_type == 'post':
                result = _handle_post(db, bot_user, action, source_channel)
            elif action_type == 'comment':
                result = _handle_comment(db, bot_user, action)
            elif action_type == 'vote':
                result = _handle_vote(db, bot_user, action)
            elif action_type == 'follow':
                result = _handle_follow(db, bot_user, action)
            else:
                result = {'action': action_type, 'status': 'error', 'error': f'Unknown action: {action_type}'}

            results.append(result)
        except Exception as e:
            results.append({'action': action_type, 'status': 'error', 'error': str(e)})

    bot_user.last_active_at = datetime.utcnow()
    return results


def _handle_post(db, bot_user, action, source_channel):
    title = action.get('title', '')
    if not title:
        raise ValueError("title is required for post action")

    message_id = action.get('message_id')
    if message_id:
        existing = db.query(Post).filter(
            Post.source_channel == source_channel,
            Post.source_message_id == message_id
        ).first()
        if existing:
            return {'action': 'post', 'status': 'duplicate', 'id': existing.id}

    post = PostService.create(
        db, bot_user, title,
        content=action.get('content', ''),
        content_type=action.get('content_type', 'text'),
        submolt_name=action.get('submolt'),
        media_urls=action.get('media_urls'),
        link_url=action.get('link_url'),
        source_channel=source_channel,
        source_message_id=message_id,
    )

    on_new_post(post.to_dict(include_author=True))
    return {'action': 'post', 'status': 'created', 'id': post.id}


def _handle_comment(db, bot_user, action):
    post_id = action.get('post_id')
    content = action.get('content', '')
    if not post_id or not content:
        raise ValueError("post_id and content are required for comment action")

    post = PostService.get_by_id(db, post_id)
    if not post:
        raise ValueError(f"Post {post_id} not found")

    comment = CommentService.create(
        db, post, bot_user, content,
        parent_id=action.get('parent_id'),
    )

    on_new_comment(comment.to_dict(include_author=True), post_id)
    return {'action': 'comment', 'status': 'created', 'id': comment.id}


def _handle_vote(db, bot_user, action):
    target_type = action.get('target_type', 'post')
    target_id = action.get('target_id')
    value = action.get('value', 1)
    if not target_id:
        raise ValueError("target_id is required for vote action")
    if value not in (1, -1):
        raise ValueError("value must be 1 (upvote) or -1 (downvote)")

    result = VoteService.vote(db, bot_user, target_type, target_id, value)
    on_vote_update(target_type, target_id, result.get('score', 0))
    return {'action': 'vote', 'status': result.get('action', 'voted'), 'score': result.get('score', 0)}


def _handle_follow(db, bot_user, action):
    target_id = action.get('user_id')
    if not target_id:
        raise ValueError("user_id is required for follow action")

    target = UserService.get_by_id(db, target_id)
    if not target:
        target = UserService.get_by_username(db, target_id)
    if not target:
        raise ValueError(f"User {target_id} not found")

    created = FollowService.follow(db, bot_user, target.id)
    return {'action': 'follow', 'status': 'followed' if created else 'already_following'}


# ─── Outbound: Discover moltbot/OpenClaw agents ───

def discover_moltbot_agents(gateway_url: str, timeout: int = 10) -> list:
    """
    Discover available agents from a moltbot/OpenClaw gateway.
    Tries HTTP endpoint for session listing.
    """
    agents = []
    try:
        # OpenClaw exposes session info via HTTP when running
        # Try common endpoints
        for path in ['/sessions', '/api/sessions', '/v1/sessions']:
            try:
                resp = requests.get(f"{gateway_url}{path}", timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    sessions = data if isinstance(data, list) else data.get('sessions', [])
                    for s in sessions:
                        agents.append({
                            'session_id': s.get('id') or s.get('session_id', ''),
                            'name': s.get('name') or s.get('label', ''),
                            'platform': 'moltbot',
                            'gateway_url': gateway_url,
                        })
                    break
            except (requests.RequestException, ValueError):
                continue

        # Also try .well-known/agent.json for A2A-compatible bots
        try:
            resp = requests.get(f"{gateway_url}/.well-known/agent.json", timeout=timeout)
            if resp.status_code == 200:
                card = resp.json()
                agents.append({
                    'session_id': card.get('name', 'unknown'),
                    'name': card.get('name', ''),
                    'description': card.get('description', ''),
                    'skills': [s.get('name') for s in card.get('skills', [])],
                    'platform': 'a2a',
                    'gateway_url': gateway_url,
                })
        except (requests.RequestException, ValueError):
            pass

    except Exception as e:
        logger.debug(f"Moltbot discovery failed for {gateway_url}: {e}")

    return agents


def send_to_moltbot(gateway_url: str, session_id: str, message: str,
                    timeout: int = 30) -> dict:
    """Send a message to a moltbot/OpenClaw agent session."""
    try:
        for path in [f'/sessions/{session_id}/send', f'/api/sessions/{session_id}/messages']:
            try:
                resp = requests.post(
                    f"{gateway_url}{path}",
                    json={'message': message, 'content': message},
                    timeout=timeout,
                )
                if resp.status_code in (200, 201):
                    return {'status': 'sent', 'response': resp.json()}
            except (requests.RequestException, ValueError):
                continue
        return {'status': 'error', 'error': 'No reachable endpoint'}
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def auto_register_discovered_agents(db, agents: list) -> int:
    """Auto-register discovered external agents as HevolveSocial users."""
    registry = ExternalBotRegistry()
    count = 0
    for agent in agents:
        try:
            platform = agent.get('platform', 'generic')
            bot_id = agent.get('session_id', '')
            bot_name = agent.get('name', '') or f"{platform}_{bot_id}"
            description = agent.get('description', f'Discovered from {agent.get("gateway_url", "")}')

            registry.register_bot(
                db, bot_id=bot_id, bot_name=bot_name,
                platform=platform, description=description,
                callback_url=agent.get('gateway_url'),
            )
            count += 1
        except Exception as e:
            logger.debug(f"Failed to register discovered agent {agent}: {e}")
    return count
