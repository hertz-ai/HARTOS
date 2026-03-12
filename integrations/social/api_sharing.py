"""
HevolveSocial - Sharing API Blueprint
Short URL token management, OG metadata resolution, view tracking, consent-gated private sharing.
"""
import hashlib
import json
import logging
import secrets
from datetime import datetime

from flask import Blueprint, request, jsonify, g
from sqlalchemy import func

from .auth import require_auth, optional_auth
from .models import (
    get_db, User, Post, Comment, Community, ShareableLink, ShareEvent,
)

logger = logging.getLogger('hevolve_social')

sharing_bp = Blueprint('sharing', __name__, url_prefix='/api/social')


def _ok(data=None, meta=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    if meta is not None:
        r['meta'] = meta
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _get_json():
    return request.get_json(force=True, silent=True) or {}


def _generate_token(length=8):
    """Generate a URL-safe short token."""
    return secrets.token_urlsafe(length)[:length]


def _get_og_metadata(db, resource_type, resource_id):
    """Fetch OG metadata for a resource. Returns dict with title, description, image, type."""
    og = {
        'title': 'Nunba',
        'description': 'A community-driven social network for humans and AI agents.',
        'image': '',
        'type': 'website',
    }

    if resource_type == 'post':
        post = db.query(Post).filter_by(id=resource_id).first()
        if post:
            content = post.content or ''
            og['title'] = content[:60].strip() or 'Thought Experiment'
            og['description'] = content[:200].strip()
            og['type'] = 'article'
            if post.media_urls:
                urls = post.media_urls if isinstance(post.media_urls, list) else [post.media_urls]
                if urls:
                    og['image'] = urls[0]

    elif resource_type == 'comment':
        comment = db.query(Comment).filter_by(id=resource_id).first()
        if comment:
            content = comment.content or ''
            og['title'] = f'Comment: {content[:50].strip()}'
            og['description'] = content[:200].strip()
            og['type'] = 'article'

    elif resource_type == 'profile':
        user = db.query(User).filter_by(id=resource_id).first()
        if user:
            og['title'] = f'{user.display_name or user.username}'
            og['description'] = f'{user.bio or "Member of Nunba community"}'[:200]
            og['type'] = 'profile'
            if user.avatar_url:
                og['image'] = user.avatar_url

    elif resource_type == 'community':
        comm = db.query(Community).filter_by(id=resource_id).first()
        if comm:
            og['title'] = f'h/{comm.name}'
            og['description'] = (comm.description or f'Join the {comm.name} community')[:200]
            if comm.banner_url:
                og['image'] = comm.banner_url

    elif resource_type in ('agent', 'recipe', 'game', 'kids_game'):
        og['title'] = f'{resource_type.replace("_", " ").title()}'
        og['description'] = f'Check out this {resource_type.replace("_", " ")} on Nunba'

    return og


def _resource_route(resource_type, resource_id):
    """Map resource type to its SPA route."""
    routes = {
        'post': f'/social/post/{resource_id}',
        'comment': f'/social/post/{resource_id}',
        'profile': f'/social/profile/{resource_id}',
        'community': f'/social/h/{resource_id}',
        'agent': f'/social/agents/{resource_id}',
        'recipe': f'/social/recipes/{resource_id}',
        'game': f'/social/games/{resource_id}',
        'kids_game': f'/social/kids/game/{resource_id}',
        'challenge': f'/social/challenges/{resource_id}',
        'media': f'/api/media/asset?id={resource_id}',
    }
    return routes.get(resource_type, f'/social')


# ═══════════════════════════════════════════════════════════════
# CREATE / GET SHARE LINK
# ═══════════════════════════════════════════════════════════════

@sharing_bp.route('/share/link', methods=['POST'])
@require_auth
def create_share_link():
    """Create or retrieve a shareable short link for a resource."""
    db = get_db()
    try:
        data = _get_json()
        resource_type = data.get('resource_type', '').strip()
        resource_id = str(data.get('resource_id', '')).strip()
        is_private = bool(data.get('is_private', False))

        if not resource_type or not resource_id:
            return _err("resource_type and resource_id required")

        # DLP scan outbound content (best-effort)
        try:
            from security.dlp_engine import get_dlp_engine
            dlp = get_dlp_engine()
            content_to_check = data.get('title', '') + ' ' + data.get('description', '')
            allowed, reason = dlp.check_outbound(content_to_check)
            if not allowed:
                return _err("Content blocked by DLP policy: contains sensitive data", 403)
        except (ImportError, Exception):
            pass

        valid_types = ('post', 'comment', 'profile', 'community', 'agent',
                       'recipe', 'game', 'kids_game', 'challenge', 'chat', 'media')
        if resource_type not in valid_types:
            return _err(f"Invalid resource_type. Must be one of: {', '.join(valid_types)}")

        # Check for existing canonical link (one per user per resource)
        existing = db.query(ShareableLink).filter_by(
            resource_type=resource_type,
            resource_id=resource_id,
            created_by=g.user_id,
        ).first()

        if existing:
            og = json.loads(existing.metadata_json) if existing.metadata_json else {}
            existing.share_count = (existing.share_count or 0) + 1
            db.commit()
            return _ok({
                'token': existing.token,
                'url': f'/s/{existing.token}',
                'og': og,
                'view_count': existing.view_count,
                'share_count': existing.share_count,
                'is_private': existing.is_private,
            })

        # Get user's referral code
        user = db.query(User).filter_by(id=g.user_id).first()
        referral_code = getattr(user, 'referral_code', None) or ''

        # Generate OG metadata
        og = _get_og_metadata(db, resource_type, resource_id)

        # Generate consent token for private links
        consent_token = secrets.token_urlsafe(24) if is_private else None

        # Create new link
        token = _generate_token()
        # Ensure uniqueness (retry on collision)
        for _ in range(5):
            if not db.query(ShareableLink).filter_by(token=token).first():
                break
            token = _generate_token()

        link = ShareableLink(
            token=token,
            resource_type=resource_type,
            resource_id=resource_id,
            created_by=g.user_id,
            referral_code=referral_code,
            is_private=is_private,
            consent_token=consent_token,
            metadata_json=json.dumps(og),
        )
        db.add(link)

        # Log share event
        ip_raw = request.remote_addr or ''
        ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:16]
        event = ShareEvent(
            link_id=link.id,
            event_type='share',
            viewer_id=g.user_id,
            ip_hash=ip_hash,
        )
        db.add(event)

        # Award Resonance for sharing
        try:
            from .resonance_engine import ResonanceService
            ResonanceService.award(db, g.user_id, 'content_shared', 5,
                                   reason=f'Shared {resource_type} {resource_id}')
        except Exception as e:
            logger.debug(f"Resonance award for share failed: {e}")

        db.commit()

        return _ok({
            'token': token,
            'url': f'/s/{token}',
            'og': og,
            'view_count': 0,
            'share_count': 1,
            'is_private': is_private,
        }, status=201)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# RESOLVE SHARE TOKEN
# ═══════════════════════════════════════════════════════════════

@sharing_bp.route('/share/<token>', methods=['GET'])
@optional_auth
def resolve_share_token(token):
    """Resolve a share token to resource metadata (OG data + redirect info)."""
    db = get_db()
    try:
        link = db.query(ShareableLink).filter_by(token=token).first()
        if not link:
            return _err("Share link not found", 404)

        # Check expiry
        if link.expires_at and link.expires_at < datetime.utcnow():
            return _err("Share link has expired", 410)

        og = json.loads(link.metadata_json) if link.metadata_json else {}
        redirect_url = _resource_route(link.resource_type, link.resource_id)

        # Add referral code to redirect
        if link.referral_code:
            separator = '&' if '?' in redirect_url else '?'
            redirect_url = f'{redirect_url}{separator}ref={link.referral_code}'

        result = {
            'token': link.token,
            'resource_type': link.resource_type,
            'resource_id': link.resource_id,
            'og': og,
            'redirect_url': redirect_url,
            'is_private': link.is_private,
            'requires_consent': link.is_private,
            'view_count': link.view_count,
            'share_count': link.share_count,
        }

        if link.is_private:
            # Don't include full OG data for private links until consent
            result['og'] = {
                'title': 'Private content shared with you',
                'description': 'You need to grant consent to view this content.',
                'image': '',
                'type': 'website',
            }
            # Include sharer info
            if link.created_by:
                creator = db.query(User).filter_by(id=link.created_by).first()
                if creator:
                    result['shared_by'] = {
                        'username': creator.username,
                        'display_name': creator.display_name,
                        'avatar_url': creator.avatar_url,
                    }

        return _ok(result)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# VIEW TRACKING
# ═══════════════════════════════════════════════════════════════

@sharing_bp.route('/share/<token>/view', methods=['POST'])
@optional_auth
def track_share_view(token):
    """Increment view count for a share link (fire-and-forget)."""
    db = get_db()
    try:
        link = db.query(ShareableLink).filter_by(token=token).first()
        if not link:
            return _err("Share link not found", 404)

        link.view_count = (link.view_count or 0) + 1

        # Log view event
        ip_raw = request.remote_addr or ''
        ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:16]
        viewer_id = getattr(g, 'user_id', None)
        event = ShareEvent(
            link_id=link.id,
            event_type='view',
            viewer_id=viewer_id,
            ip_hash=ip_hash,
        )
        db.add(event)

        # Check viral milestones
        view_count = link.view_count
        if link.created_by:
            try:
                from .resonance_engine import ResonanceService
                if view_count == 10:
                    ResonanceService.award(db, link.created_by, 'content_viral_10', 25,
                                           reason=f'Share link reached 10 views')
                elif view_count == 50:
                    ResonanceService.award(db, link.created_by, 'content_viral_50', 100,
                                           reason=f'Share link reached 50 views')
            except Exception as e:
                logger.debug(f"Resonance viral award failed: {e}")

        db.commit()
        return _ok({'view_count': link.view_count})
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# CONSENT (Private Sharing)
# ═══════════════════════════════════════════════════════════════

@sharing_bp.route('/share/<token>/check-consent', methods=['GET'])
@optional_auth
def check_consent(token):
    """Check if a private share link requires consent."""
    db = get_db()
    try:
        link = db.query(ShareableLink).filter_by(token=token).first()
        if not link:
            return _err("Share link not found", 404)

        if not link.is_private:
            return _ok({'requires_consent': False, 'is_private': False})

        # Check if user already consented
        viewer_id = getattr(g, 'user_id', None)
        already_consented = False
        if viewer_id:
            already_consented = db.query(ShareEvent).filter_by(
                link_id=link.id,
                event_type='consent',
                viewer_id=viewer_id,
            ).first() is not None

        # Sharer info
        shared_by = None
        if link.created_by:
            creator = db.query(User).filter_by(id=link.created_by).first()
            if creator:
                shared_by = {
                    'username': creator.username,
                    'display_name': creator.display_name,
                }

        return _ok({
            'requires_consent': not already_consented,
            'is_private': True,
            'already_consented': already_consented,
            'resource_type': link.resource_type,
            'shared_by': shared_by,
        })
    finally:
        db.close()


@sharing_bp.route('/share/<token>/consent', methods=['POST'])
@require_auth
def grant_consent(token):
    """Grant consent to view a private share link."""
    db = get_db()
    try:
        link = db.query(ShareableLink).filter_by(token=token).first()
        if not link:
            return _err("Share link not found", 404)

        if not link.is_private:
            return _err("This link is not private")

        # Log consent event
        ip_raw = request.remote_addr or ''
        ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:16]
        event = ShareEvent(
            link_id=link.id,
            event_type='consent',
            viewer_id=g.user_id,
            ip_hash=ip_hash,
        )
        db.add(event)
        db.commit()

        # Return full OG data and redirect
        og = json.loads(link.metadata_json) if link.metadata_json else {}
        redirect_url = _resource_route(link.resource_type, link.resource_id)
        if link.referral_code:
            separator = '&' if '?' in redirect_url else '?'
            redirect_url = f'{redirect_url}{separator}ref={link.referral_code}'

        return _ok({
            'og': og,
            'redirect_url': redirect_url,
            'resource_type': link.resource_type,
            'resource_id': link.resource_id,
        })
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# OG IMAGE (dynamic per-resource)
# ═══════════════════════════════════════════════════════════════

@sharing_bp.route('/og-image/<resource_type>/<resource_id>', methods=['GET'])
@optional_auth
def og_image_endpoint(resource_type, resource_id):
    """Generate or serve cached OG preview image (1200x630)."""
    valid_types = ('post', 'comment', 'profile', 'community')
    if resource_type not in valid_types:
        return _err("Unsupported resource type", 400)
    try:
        from .og_image import generate_og_image
        image_path = generate_og_image(resource_type, resource_id)
        if image_path:
            from flask import send_file
            return send_file(image_path, mimetype='image/png',
                             max_age=3600)
    except Exception as e:
        logger.debug(f"OG image generation failed: {e}")

    # Fallback: return a redirect to static default OG image
    return _err("OG image not available", 404)


# ═══════════════════════════════════════════════════════════════
# SHARE STATS (for admin / dashboard)
# ═══════════════════════════════════════════════════════════════

@sharing_bp.route('/share/stats', methods=['GET'])
@require_auth
def share_stats():
    """Get sharing statistics for the authenticated user."""
    db = get_db()
    try:
        links = db.query(ShareableLink).filter_by(created_by=g.user_id).all()
        total_shares = len(links)
        total_views = sum(l.view_count or 0 for l in links)
        total_share_clicks = sum(l.share_count or 0 for l in links)

        top_links = sorted(links, key=lambda l: l.view_count or 0, reverse=True)[:5]
        top = []
        for l in top_links:
            og = json.loads(l.metadata_json) if l.metadata_json else {}
            top.append({
                'token': l.token,
                'resource_type': l.resource_type,
                'og_title': og.get('title', ''),
                'view_count': l.view_count,
                'share_count': l.share_count,
                'created_at': l.created_at.isoformat() if l.created_at else None,
            })

        return _ok({
            'total_links': total_shares,
            'total_views': total_views,
            'total_share_clicks': total_share_clicks,
            'top_links': top,
        })
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# EMBEDDABLE CONTENT CARD
# ═══════════════════════════════════════════════════════════════

def _embed_html(title, description, author_name, votes, resource_url, resource_type):
    """Return a self-contained HTML page for an embeddable content card."""
    # Escape HTML entities
    import html as _html
    title = _html.escape(title or 'Untitled')
    description = _html.escape(description or '')
    author_name = _html.escape(author_name or 'Unknown')
    resource_type_label = _html.escape(resource_type.replace('_', ' ').title())
    resource_url = _html.escape(resource_url or '#')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#0F0E17;color:#FFFFFE;display:flex;justify-content:center;align-items:center;
min-height:100vh;padding:12px}}
.card{{background:#1A1932;border:1px solid rgba(108,99,255,0.25);border-radius:16px;
padding:20px;max-width:480px;width:100%;position:relative;overflow:hidden}}
.card::before{{content:"";position:absolute;top:0;left:0;right:0;height:3px;
background:linear-gradient(90deg,#6C63FF,#FF6B6B)}}
.type{{display:inline-block;font-size:11px;font-weight:600;text-transform:uppercase;
letter-spacing:0.06em;color:#6C63FF;background:rgba(108,99,255,0.12);
border-radius:6px;padding:3px 8px;margin-bottom:10px}}
.title{{font-size:18px;font-weight:700;line-height:1.35;margin-bottom:8px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.desc{{font-size:14px;color:rgba(255,255,254,0.6);line-height:1.55;margin-bottom:14px;
display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
.meta{{display:flex;align-items:center;gap:12px;font-size:12px;color:rgba(255,255,254,0.45);
margin-bottom:14px}}
.votes{{color:#6C63FF;font-weight:700}}
.brand{{display:flex;align-items:center;justify-content:space-between;padding-top:12px;
border-top:1px solid rgba(108,99,255,0.12)}}
.brand-name{{font-size:13px;font-weight:700;
background:linear-gradient(135deg,#6C63FF,#FF6B6B);-webkit-background-clip:text;
-webkit-text-fill-color:transparent}}
.view-btn{{display:inline-block;font-size:12px;font-weight:600;color:#6C63FF;
text-decoration:none;padding:6px 14px;border:1px solid rgba(108,99,255,0.35);
border-radius:8px;transition:all 0.2s ease}}
.view-btn:hover{{background:rgba(108,99,255,0.12);border-color:#6C63FF}}
</style>
</head>
<body>
<div class="card">
  <span class="type">{resource_type_label}</span>
  <div class="title">{title}</div>
  <div class="desc">{description}</div>
  <div class="meta">
    <span>by {author_name}</span>
    <span class="votes">{votes} votes</span>
  </div>
  <div class="brand">
    <span class="brand-name">Nunba</span>
    <a class="view-btn" href="{resource_url}" target="_blank" rel="noopener">View on Nunba</a>
  </div>
</div>
</body>
</html>'''


@sharing_bp.route('/embed/<resource_type>/<resource_id>', methods=['GET'])
@optional_auth
def embed_card(resource_type, resource_id):
    """Return an embeddable HTML content card (like a tweet embed).

    Supports: post, comment, profile, community.
    Designed to be loaded in an iframe.
    """
    from flask import Response

    valid_types = ('post', 'comment', 'profile', 'community')
    if resource_type not in valid_types:
        return Response(
            f'<html><body style="background:#0F0E17;color:#fff;font-family:sans-serif;'
            f'display:flex;justify-content:center;align-items:center;height:100vh">'
            f'<p>Unsupported resource type</p></body></html>',
            status=400, content_type='text/html; charset=utf-8',
        )

    db = get_db()
    try:
        title = 'Nunba'
        description = ''
        author_name = ''
        votes = 0
        resource_url = _resource_route(resource_type, resource_id)

        if resource_type == 'post':
            post = db.query(Post).filter_by(id=resource_id).first()
            if not post:
                return Response(
                    '<html><body style="background:#0F0E17;color:#fff;font-family:sans-serif;'
                    'display:flex;justify-content:center;align-items:center;height:100vh">'
                    '<p>Post not found</p></body></html>',
                    status=404, content_type='text/html; charset=utf-8',
                )
            title = (post.title or (post.content or '')[:60]).strip() or 'Thought Experiment'
            description = (post.content or '')[:300].strip()
            votes = (post.upvotes or 0) - (post.downvotes or 0)
            author = db.query(User).filter_by(id=post.author_id).first()
            author_name = (author.display_name or author.username) if author else 'Unknown'

        elif resource_type == 'comment':
            comment = db.query(Comment).filter_by(id=resource_id).first()
            if not comment:
                return Response(
                    '<html><body style="background:#0F0E17;color:#fff;font-family:sans-serif;'
                    'display:flex;justify-content:center;align-items:center;height:100vh">'
                    '<p>Comment not found</p></body></html>',
                    status=404, content_type='text/html; charset=utf-8',
                )
            title = f'Comment: {(comment.content or "")[:60].strip()}'
            description = (comment.content or '')[:300].strip()
            votes = (getattr(comment, 'upvotes', 0) or 0) - (getattr(comment, 'downvotes', 0) or 0)
            author = db.query(User).filter_by(id=comment.author_id).first()
            author_name = (author.display_name or author.username) if author else 'Unknown'

        elif resource_type == 'profile':
            user = db.query(User).filter_by(id=resource_id).first()
            if not user:
                return Response(
                    '<html><body style="background:#0F0E17;color:#fff;font-family:sans-serif;'
                    'display:flex;justify-content:center;align-items:center;height:100vh">'
                    '<p>User not found</p></body></html>',
                    status=404, content_type='text/html; charset=utf-8',
                )
            title = user.display_name or user.username
            description = (user.bio or 'Member of Nunba community')[:300]
            author_name = user.username
            votes = 0

        elif resource_type == 'community':
            comm = db.query(Community).filter_by(id=resource_id).first()
            if not comm:
                return Response(
                    '<html><body style="background:#0F0E17;color:#fff;font-family:sans-serif;'
                    'display:flex;justify-content:center;align-items:center;height:100vh">'
                    '<p>Community not found</p></body></html>',
                    status=404, content_type='text/html; charset=utf-8',
                )
            title = f'h/{comm.name}'
            description = (comm.description or f'Join the {comm.name} community')[:300]
            author_name = f'{comm.member_count or 0} members'
            votes = 0

        html = _embed_html(title, description, author_name, votes, resource_url, resource_type)
        return Response(html, status=200, content_type='text/html; charset=utf-8',
                        headers={
                            'X-Frame-Options': 'ALLOWALL',
                            'Content-Security-Policy': "frame-ancestors *",
                        })
    finally:
        db.close()
