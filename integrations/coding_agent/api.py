"""
HevolveSocial - Distributed Coding Agent API

Thin API layer. All actual coding work flows through the existing
/chat endpoint (CREATE/REUSE pipeline). This just manages:
- Goals: what repo/objective to work on (admin, central-only)
- Opt-in: which agents contribute idle compute (user self-service)
- Stats: idle agent counts

Security:
- Admin + central-only for goal management
- Auth required for all endpoints
- Repo allowlist via HEVOLVE_CODING_ALLOWED_REPOS
- Users can only opt themselves in/out (admin can do anyone)
"""
import os
import re
import logging
from functools import wraps
from typing import Optional
from flask import Blueprint, request, jsonify, g

from integrations.social.auth import require_auth, require_admin

logger = logging.getLogger('hevolve_social')

coding_agent_bp = Blueprint('coding_agent', __name__)

_IS_CENTRAL = os.environ.get('HEVOLVE_NODE_TIER') == 'central'

ALLOWED_REPOS = [r.strip() for r in os.environ.get(
    'HEVOLVE_CODING_ALLOWED_REPOS', '').split(',') if r.strip()]


def _require_central(f):
    """Decorator: rejects request if node is not central."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _IS_CENTRAL:
            return jsonify({'success': False, 'error': 'Central node only'}), 403
        return f(*args, **kwargs)
    return decorated


def _validate_repo(repo_url: str) -> Optional[str]:
    """Returns error string if repo is not allowed, None if OK."""
    if not repo_url:
        return 'repo_url is required'
    if '/' not in repo_url or len(repo_url.split('/')) != 2:
        return 'repo_url must be in owner/repo format'
    if not re.match(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$', repo_url):
        return 'repo_url contains invalid characters'
    if ALLOWED_REPOS and repo_url not in ALLOWED_REPOS:
        return 'Repository not in allowlist'
    return None


# ─── Goals (admin + central only) ───

@coding_agent_bp.route('/api/coding/goals', methods=['POST'])
@require_admin
@_require_central
def create_goal():
    from .goal_manager import CodingGoalManager

    data = request.get_json() or {}
    repo_url = data.get('repo_url', '')
    error = _validate_repo(repo_url)
    if error:
        return jsonify({'success': False, 'error': error}), 400

    result = CodingGoalManager.create_goal(
        g.db,
        title=data.get('title', ''),
        description=data.get('description', ''),
        repo_url=repo_url,
        branch=data.get('branch', 'main'),
        target_path=data.get('target_path', ''),
        created_by=str(g.user.id),
    )
    return jsonify({'success': True, 'goal': result})


@coding_agent_bp.route('/api/coding/goals', methods=['GET'])
@require_auth
def list_goals():
    from .goal_manager import CodingGoalManager

    status = request.args.get('status')
    goals = CodingGoalManager.list_goals(g.db, status=status)
    return jsonify({'success': True, 'goals': goals})


@coding_agent_bp.route('/api/coding/goals/<goal_id>', methods=['GET'])
@require_auth
def get_goal(goal_id):
    from .goal_manager import CodingGoalManager

    result = CodingGoalManager.get_goal(g.db, goal_id)
    return jsonify(result)


@coding_agent_bp.route('/api/coding/goals/<goal_id>', methods=['PATCH'])
@require_admin
@_require_central
def update_goal(goal_id):
    from .goal_manager import CodingGoalManager

    data = request.get_json() or {}
    result = CodingGoalManager.update_goal_status(g.db, goal_id, data.get('status', 'active'))
    return jsonify(result)


# ─── Opt-In / Opt-Out (self-service) ───

@coding_agent_bp.route('/api/coding/opt-in', methods=['POST'])
@require_auth
def opt_in():
    from .idle_detection import IdleDetectionService

    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'user_id required'}), 400

    if not g.user.is_admin and str(g.user.id) != str(user_id):
        return jsonify({'success': False, 'error': 'Can only opt in yourself'}), 403

    result = IdleDetectionService.opt_in(g.db, user_id)
    return jsonify(result)


@coding_agent_bp.route('/api/coding/opt-out', methods=['POST'])
@require_auth
def opt_out():
    from .idle_detection import IdleDetectionService

    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'user_id required'}), 400

    if not g.user.is_admin and str(g.user.id) != str(user_id):
        return jsonify({'success': False, 'error': 'Can only opt out yourself'}), 403

    result = IdleDetectionService.opt_out(g.db, user_id)
    return jsonify(result)


# ─── Stats ───

@coding_agent_bp.route('/api/coding/idle-stats', methods=['GET'])
@require_admin
def idle_stats():
    from .idle_detection import IdleDetectionService

    stats = IdleDetectionService.get_idle_stats(g.db)
    return jsonify({'success': True, **stats})


# ─── Distributed Execution Endpoint (receives shards from peers) ───

@coding_agent_bp.route('/coding/execute', methods=['POST'])
def execute_shard():
    """Receive an encrypted coding shard from a peer, execute locally, return result.

    Auth: The encrypted envelope IS the auth — only peers who know our X25519
    public key (exchanged during PeerLink handshake) can encrypt to us.
    Decryption failure = unauthorized. No additional token needed.
    """
    import time

    start = time.time()
    data = request.get_json(silent=True) or {}
    encrypted = data.get('encrypted')

    if not encrypted:
        return jsonify({'error': 'Missing encrypted payload'}), 400

    try:
        from security.channel_encryption import (
            decrypt_json_from_peer, encrypt_json_for_peer
        )

        # Decrypt the shard — this IS the auth check.
        # Only peers with our public key (from PeerLink handshake) can produce
        # a valid envelope. Failed decryption = unauthorized peer.
        payload = decrypt_json_from_peer(encrypted)
        if not payload:
            logger.warning("[SHARD-EXEC] Decryption failed — unauthorized peer")
            return jsonify({'error': 'Unauthorized'}), 403

        task = payload.get('task', '')
        task_type = payload.get('task_type', 'feature')
        preferred_tool = payload.get('preferred_tool', '')
        model = payload.get('model', '')
        file_content = payload.get('file_content', {})

        # Write shard files to a temporary working directory
        import tempfile
        with tempfile.TemporaryDirectory(prefix='hart_shard_') as tmpdir:
            for rel_path, content in file_content.items():
                # Security: prevent path traversal
                safe_path = os.path.normpath(rel_path)
                if safe_path.startswith('..') or os.path.isabs(safe_path):
                    logger.warning(f"[SHARD-EXEC] Path traversal blocked: {rel_path}")
                    continue
                fpath = os.path.join(tmpdir, safe_path)
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(content)

            # Execute via local orchestrator (always local — we ARE the peer)
            from .orchestrator import get_coding_orchestrator
            orchestrator = get_coding_orchestrator()
            result = orchestrator._execute_local(
                task=task,
                task_type=task_type,
                preferred_tool=preferred_tool,
                user_id='peer',
                model=model,
                working_dir=tmpdir,
            )

        result['execution_time_s'] = time.time() - start

        # Encrypt result back to sender using their public key (from INSIDE the
        # encrypted envelope — not from the plaintext request, which MITM could replace)
        sender_pub = payload.get('sender_public_key')
        if sender_pub:
            encrypted_result = encrypt_json_for_peer(result, sender_pub)
            return jsonify({'encrypted': encrypted_result})
        else:
            # SAME_USER trust — no encryption needed for user's own machines
            return jsonify({'encrypted': None, 'result': result})

    except Exception as e:
        logger.error(f"[SHARD-EXEC] Error: {e}")
        return jsonify({'error': 'Execution failed'}), 500
