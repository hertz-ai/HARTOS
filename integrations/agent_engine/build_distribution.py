"""
Linux Build Distribution — Licensed commercial builds gated by payment.

Purchase → signed license → time-limited download URL.
All compute should fall under one basket — treading carefully in a cautious market.

Service Pattern: static methods, db: Session, db.flush() not db.commit().
Blueprint Pattern: Blueprint('build_distribution', __name__).
"""
import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from flask import Blueprint, g, jsonify, request
from sqlalchemy.orm import Session

logger = logging.getLogger('hevolve_social')

# Build type → (max_downloads, validity_days)
BUILD_CONFIG = {
    'community':  (3,   365),       # 3 downloads, 1 year
    'pro':        (10,  730),       # 10 downloads, 2 years
    'enterprise': (100, 36500),     # 100 downloads, ~perpetual (100 years)
}

# HMAC secret for signed download URLs (rotated at boot)
_DOWNLOAD_SECRET = secrets.token_bytes(32)


class BuildDistributionService:
    """Static service for licensed Linux build distribution."""

    @staticmethod
    def create_build_license(db: Session, user_id: str,
                              build_type: str = 'community',
                              platform: str = 'linux_x64',
                              payment_reference: str = None) -> Dict:
        """Create a signed build license after payment verification."""
        from integrations.social.models import BuildLicense

        if build_type not in BUILD_CONFIG:
            return {'error': f'Invalid build_type: {build_type}. Valid: {list(BUILD_CONFIG.keys())}'}

        if platform not in ('linux_x64', 'linux_arm64'):
            return {'error': f'Invalid platform: {platform}. Valid: linux_x64, linux_arm64'}

        max_downloads, validity_days = BUILD_CONFIG[build_type]
        license_key = secrets.token_urlsafe(32)

        # Sign with node key if available
        signed_by = None
        sig_hex = None
        try:
            from security.node_integrity import get_public_key_hex, sign_message
            signed_by = get_public_key_hex()
            sig_hex = sign_message(license_key.encode('utf-8')).hex()
        except Exception:
            pass

        bl = BuildLicense(
            user_id=user_id,
            license_key=license_key,
            build_type=build_type,
            platform=platform,
            payment_reference=payment_reference,
            max_downloads=max_downloads,
            signed_by=signed_by,
            signature_hex=sig_hex,
            expires_at=datetime.utcnow() + timedelta(days=validity_days),
        )
        db.add(bl)
        db.flush()
        return bl.to_dict()

    @staticmethod
    def verify_build_license(db: Session, license_key: str) -> Dict:
        """Verify a license key. Returns {valid: bool, license: dict, reason: str}."""
        from integrations.social.models import BuildLicense

        bl = db.query(BuildLicense).filter_by(license_key=license_key).first()
        if not bl:
            return {'valid': False, 'license': None, 'reason': 'License not found'}
        if not bl.is_active:
            return {'valid': False, 'license': bl.to_dict(), 'reason': 'License deactivated'}
        if bl.expires_at and bl.expires_at < datetime.utcnow():
            return {'valid': False, 'license': bl.to_dict(), 'reason': 'License expired'}
        if bl.download_count >= bl.max_downloads:
            return {'valid': False, 'license': bl.to_dict(), 'reason': 'Download limit reached'}

        return {'valid': True, 'license': bl.to_dict(), 'reason': 'ok'}

    @staticmethod
    def record_download(db: Session, license_id: str) -> Dict:
        """Record a download. Returns error if limit reached."""
        from integrations.social.models import BuildLicense

        bl = db.query(BuildLicense).filter_by(id=license_id).first()
        if not bl:
            return {'error': 'License not found'}
        if bl.download_count >= bl.max_downloads:
            return {'error': 'Download limit reached'}
        if not bl.is_active:
            return {'error': 'License deactivated'}

        bl.download_count = (bl.download_count or 0) + 1
        db.flush()
        return bl.to_dict()

    @staticmethod
    def get_download_url(db: Session, license_id: str) -> Dict:
        """Generate a time-limited signed download URL (1 hour)."""
        from integrations.social.models import BuildLicense

        bl = db.query(BuildLicense).filter_by(id=license_id).first()
        if not bl:
            return {'error': 'License not found'}

        # Verify validity
        if not bl.is_active:
            return {'error': 'License deactivated'}
        if bl.expires_at and bl.expires_at < datetime.utcnow():
            return {'error': 'License expired'}
        if bl.download_count >= bl.max_downloads:
            return {'error': 'Download limit reached'}

        # Increment download count
        bl.download_count = (bl.download_count or 0) + 1
        db.flush()

        # Generate signed URL (HMAC-based, 1 hour validity)
        expires = int(time.time()) + 3600
        payload = f"{license_id}:{bl.platform}:{bl.build_type}:{expires}"
        signature = hmac.new(
            _DOWNLOAD_SECRET, payload.encode(), hashlib.sha256
        ).hexdigest()[:32]

        url = (f"/api/v1/builds/files/{bl.platform}/{bl.build_type}"
               f"?license={license_id}&expires={expires}&sig={signature}")

        return {
            'url': url,
            'expires_at': datetime.utcfromtimestamp(expires).isoformat(),
            'platform': bl.platform,
            'build_type': bl.build_type,
            'downloads_remaining': bl.max_downloads - bl.download_count,
        }

    @staticmethod
    def list_licenses(db: Session, user_id: str) -> List[Dict]:
        """List all licenses for a user."""
        from integrations.social.models import BuildLicense
        licenses = db.query(BuildLicense).filter_by(
            user_id=user_id).order_by(BuildLicense.created_at.desc()).all()
        return [bl.to_dict() for bl in licenses]


# ═══════════════════════════════════════════════════════════════
# Blueprint
# ═══════════════════════════════════════════════════════════════

build_distribution_bp = Blueprint('build_distribution', __name__)


@build_distribution_bp.route('/api/v1/builds/purchase', methods=['POST'])
def purchase_build():
    """Purchase a build license (requires JWT auth)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        data = request.get_json() or {}
        result = BuildDistributionService.create_build_license(
            db, str(user.id),
            build_type=data.get('build_type', 'community'),
            platform=data.get('platform', 'linux_x64'),
            payment_reference=data.get('payment_reference'),
        )
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 400
        db.commit()
        return jsonify({'success': True, 'license': result}), 201
    finally:
        db.close()


@build_distribution_bp.route('/api/v1/builds/download/<license_id>', methods=['GET'])
def download_build(license_id):
    """Get signed download URL for a license (requires JWT auth)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        result = BuildDistributionService.get_download_url(db, license_id)
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 400
        db.commit()
        return jsonify({'success': True, 'download': result})
    finally:
        db.close()


@build_distribution_bp.route('/api/v1/builds/licenses', methods=['GET'])
def list_build_licenses():
    """List user's build licenses (requires JWT auth)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'error': 'Authorization required'}), 401

    from integrations.social.auth import _get_user_from_token
    token = auth_header[7:]
    user, db = _get_user_from_token(token)
    if not user:
        if db:
            db.close()
        return jsonify({'success': False, 'error': 'Invalid token'}), 401

    try:
        licenses = BuildDistributionService.list_licenses(db, str(user.id))
        return jsonify({'success': True, 'licenses': licenses})
    finally:
        db.close()


@build_distribution_bp.route('/api/v1/builds/verify', methods=['POST'])
def verify_license():
    """Public license verification endpoint."""
    from integrations.social.models import get_db

    data = request.get_json() or {}
    license_key = data.get('license_key', '')
    if not license_key:
        return jsonify({'success': False, 'error': 'license_key required'}), 400

    db = get_db()
    try:
        result = BuildDistributionService.verify_build_license(db, license_key)
        return jsonify({'success': True, 'verification': result})
    finally:
        db.close()
