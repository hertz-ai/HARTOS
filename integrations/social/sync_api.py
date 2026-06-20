"""
HevolveSocial - Sync & Backup API Blueprint
Endpoints for encrypted backup/restore and device management.
"""
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, g

from .auth import require_auth
from .models import get_db, DeviceBinding

logger = logging.getLogger('hevolve_social')

sync_bp = Blueprint('sync', __name__, url_prefix='/api/social/sync')


from .api_common import _ok, _err  # single-sourced envelope helpers (#97)


# ─── Backup ───

@sync_bp.route('/backup', methods=['POST'])
@require_auth
def create_backup():
    """Create an encrypted backup of user data."""
    data = request.get_json(force=True, silent=True) or {}
    passphrase = data.get('passphrase', '').strip()
    if not passphrase or len(passphrase) < 8:
        return _err("Passphrase must be at least 8 characters")

    db = get_db()
    try:
        from .backup_service import create_backup as _create
        result = _create(db, g.user.id, passphrase)
        return _ok(result, status=201)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        logger.error(f"Backup creation failed: {e}")
        return _err("Backup creation failed")
    finally:
        db.close()


@sync_bp.route('/backup/metadata', methods=['GET'])
@require_auth
def get_backup_metadata():
    """List all backup metadata for the current user."""
    db = get_db()
    try:
        from .backup_service import list_backups
        backups = list_backups(db, g.user.id)
        return _ok(backups)
    finally:
        db.close()


@sync_bp.route('/restore', methods=['POST'])
@require_auth
def restore_backup():
    """Restore user data from an encrypted backup."""
    data = request.get_json(force=True, silent=True) or {}
    passphrase = data.get('passphrase', '').strip()
    backup_id = data.get('backup_id')  # optional - defaults to latest
    if not passphrase:
        return _err("Passphrase required")

    db = get_db()
    try:
        from .backup_service import restore_backup as _restore
        result = _restore(db, g.user.id, passphrase, backup_id)
        return _ok(result)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        logger.error(f"Backup restore failed: {e}")
        return _err("Restore failed")
    finally:
        db.close()


# ─── Device Management ───

@sync_bp.route('/link-device', methods=['POST'])
@require_auth
def link_device():
    """Link a device to the current user for sync."""
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get('device_id', '').strip()
    if not device_id:
        return _err("device_id required")

    # #117: re-home a guest session's memory onto this account when the client
    # supplies the prior guest_user_id at login/link. Only an UNCLAIMED anonymous
    # guest id is eligible (is_claimable_guest) — never an existing account — so
    # this can't be used to absorb another user's chat history. Best-effort:
    # never blocks the link itself.
    guest_user_id = (data.get('guest_user_id') or '').strip()
    if guest_user_id and guest_user_id != str(g.user.id):
        try:
            from core.user_memory_migration import (
                is_claimable_guest, migrate_user_memory)
            if is_claimable_guest(guest_user_id):
                migrate_user_memory(guest_user_id, str(g.user.id))
            else:
                logger.info("link-device: guest_user_id %s not claimable; skip migrate",
                            guest_user_id)
        except Exception as e:
            logger.warning("link-device: guest memory migration skipped: %s", e)

    # Profile down-sync (#2): the authenticated both-ids hook fcm_sync documents
    # but nothing wired.  g.user.id is the local UUID; the client supplies its
    # central account id here (same call already carries device metadata).  Pull
    # the central profile + FCM token DOWN into the local social store, which
    # also populates User.settings['central_user_id'] so #90 FCM resolution
    # starts working for real central accounts.  GATE: only the logged-in user's
    # OWN profile (the central id is bound to this session).  Best-effort —
    # never blocks the link (same posture as the #117 guest migration above).
    central_user_id = (data.get('central_user_id') or '')
    central_user_id = str(central_user_id).strip()
    if central_user_id:
        try:
            from core.profile_sync import sync_profile
            sync_profile(str(g.user.id), central_user_id)
        except Exception as e:
            logger.warning("link-device: profile down-sync skipped: %s", e)

    db = get_db()
    try:
        existing = db.query(DeviceBinding).filter_by(
            user_id=g.user.id, device_id=device_id).first()
        if existing:
            import json as _json
            existing.last_sync_at = datetime.utcnow()
            existing.is_active = True
            existing.device_name = data.get('device_name', existing.device_name)
            if 'form_factor' in data:
                existing.form_factor = data['form_factor']
            caps = data.get('capabilities')
            if isinstance(caps, dict):
                existing.capabilities_json = _json.dumps(caps)
            db.commit()
            return _ok(existing.to_dict())

        import json as _json
        caps = data.get('capabilities')
        caps_json = _json.dumps(caps) if isinstance(caps, dict) else '{}'
        binding = DeviceBinding(
            user_id=g.user.id,
            device_id=device_id,
            device_name=data.get('device_name', ''),
            platform=data.get('platform', 'web'),
            form_factor=data.get('form_factor', 'phone'),
            capabilities_json=caps_json,
        )
        db.add(binding)
        db.commit()
        return _ok(binding.to_dict(), status=201)
    except Exception as e:
        db.rollback()
        logger.error(f"Device link failed: {e}")
        return _err("Device link failed")
    finally:
        db.close()


@sync_bp.route('/devices', methods=['GET'])
@require_auth
def list_devices():
    """List all devices linked to the current user."""
    db = get_db()
    try:
        devices = db.query(DeviceBinding).filter_by(
            user_id=g.user.id, is_active=True).all()
        return _ok([d.to_dict() for d in devices])
    finally:
        db.close()


@sync_bp.route('/devices/<device_id>', methods=['DELETE'])
@require_auth
def unlink_device(device_id):
    """Unlink a device from the current user."""
    db = get_db()
    try:
        binding = db.query(DeviceBinding).filter_by(
            id=device_id, user_id=g.user.id).first()
        if not binding:
            return _err("Device not found", 404)
        binding.is_active = False
        db.commit()
        return _ok({'message': 'Device unlinked'})
    except Exception as e:
        db.rollback()
        logger.error(f"Device unlink failed: {e}")
        return _err("Device unlink failed")
    finally:
        db.close()
