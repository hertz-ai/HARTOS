"""Site Pages API — publish content through an endpoint, not a redeploy.

Publishing a blog post used to mean editing four frontend files and shipping
a build. These endpoints make a page a row instead: an admin drafts it,
moves it through review, and publishes it, and the SPA renders whatever is
published at runtime. No rebuild anywhere in the loop.

POST /api/social/pages                     — create or update a draft by slug (admin)
GET  /api/social/pages                     — list pages (published for everyone,
                                      any status for admins via ?status=)
GET  /api/social/pages/<slug>              — fetch one (published for everyone,
                                      drafts visible to admins only)
POST /api/social/pages/<slug>/status       — move draft|in_review|published (admin)

Same shape as the sibling blueprints: thin routes over a service class, the
service takes a db session so tests drive it against sqlite directly.
"""
import logging
from datetime import datetime

from flask import Blueprint, g, jsonify, request

from .auth import require_admin, optional_auth

logger = logging.getLogger('hevolve_social')

pages_bp = Blueprint('site_pages', __name__)

_ALLOWED_UPSERT_FIELDS = ('title', 'description', 'content')


class PagesService:
    """CRUD + publish-state transitions for SitePage rows."""

    @staticmethod
    def upsert(db, slug, author_id, **fields):
        """Create a page as a draft, or update an existing page's fields.

        Editing a published page does not silently change the live copy's
        status; the content updates and the page stays in whatever state it
        was. Returns the page dict.
        """
        from ._models_local import SitePage

        slug = (slug or '').strip().lower()
        if not slug or any(c for c in slug if not (c.isalnum() or c in '-_')):
            raise ValueError('slug must be non-empty, alphanumeric with - or _')

        page = db.query(SitePage).filter_by(slug=slug).first()
        if page is None:
            page = SitePage(slug=slug, author_id=author_id, title='')
            db.add(page)
        for key in _ALLOWED_UPSERT_FIELDS:
            if key in fields and fields[key] is not None:
                setattr(page, key, fields[key])
        if not page.title:
            raise ValueError('title is required')
        db.flush()
        return page.to_dict()

    @staticmethod
    def set_status(db, slug, status):
        """Move a page between draft, in_review and published."""
        from ._models_local import SitePage

        if status not in SitePage.STATUSES:
            raise ValueError('status must be one of %s' % ', '.join(SitePage.STATUSES))
        page = db.query(SitePage).filter_by(slug=(slug or '').strip().lower()).first()
        if page is None:
            return None
        page.status = status
        if status == 'published' and page.published_at is None:
            page.published_at = datetime.utcnow()
        db.flush()
        return page.to_dict()

    @staticmethod
    def get(db, slug, include_unpublished=False):
        from ._models_local import SitePage

        page = db.query(SitePage).filter_by(slug=(slug or '').strip().lower()).first()
        if page is None:
            return None
        if page.status != 'published' and not include_unpublished:
            return None
        return page.to_dict()

    @staticmethod
    def list(db, status='published'):
        """List pages without content bodies, newest published first."""
        from ._models_local import SitePage

        q = db.query(SitePage)
        if status:
            q = q.filter_by(status=status)
        rows = q.order_by(SitePage.published_at.desc().nullslast(),
                          SitePage.updated_at.desc()).all()
        return [p.to_dict(include_content=False) for p in rows]


def _is_admin():
    user = getattr(g, 'user', None)
    return bool(user is not None and getattr(user, 'is_admin', False))


@pages_bp.route('/api/social/pages', methods=['POST'])
@require_admin
def upsert_page():
    from .models import get_db

    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        page = PagesService.upsert(
            db, body.get('slug'), str(g.user.id),
            title=body.get('title'), description=body.get('description'),
            content=body.get('content'),
        )
        db.commit()
        return jsonify({'success': True, 'page': page}), 200
    except ValueError as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        db.rollback()
        logger.error('Page upsert failed: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@pages_bp.route('/api/social/pages', methods=['GET'])
@optional_auth
def list_pages():
    from .models import get_db

    status = request.args.get('status', 'published')
    if status != 'published' and not _is_admin():
        status = 'published'
    db = get_db()
    try:
        return jsonify({'success': True,
                        'pages': PagesService.list(db, status=status)}), 200
    finally:
        db.close()


@pages_bp.route('/api/social/pages/<slug>', methods=['GET'])
@optional_auth
def get_page(slug):
    from .models import get_db

    db = get_db()
    try:
        page = PagesService.get(db, slug, include_unpublished=_is_admin())
        if page is None:
            return jsonify({'success': False, 'error': 'not found'}), 404
        return jsonify({'success': True, 'page': page}), 200
    finally:
        db.close()


@pages_bp.route('/api/social/pages/<slug>/status', methods=['POST'])
@require_admin
def set_page_status(slug):
    from .models import get_db

    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        page = PagesService.set_status(db, slug, body.get('status'))
        if page is None:
            return jsonify({'success': False, 'error': 'not found'}), 404
        db.commit()
        return jsonify({'success': True, 'page': page}), 200
    except ValueError as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 400
    finally:
        db.close()
