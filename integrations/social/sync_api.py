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


def _ok(data=None, status=200):
    r = {'success': True}
    if data is not None:
        r['data'] = data
    return jsonify(r), status


def _err(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


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
        return _ok(result, 201)
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
    backup_id = data.get('backup_id')  # optional — defaults to latest
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

    db = get_db()
    try:
        existing = db.query(DeviceBinding).filter_by(
            user_id=g.user.id, device_id=device_id).first()
        if existing:
            existing.last_sync_at = datetime.utcnow()
            existing.is_active = True
            existing.device_name = data.get('device_name', existing.device_name)
            db.commit()
            return _ok(existing.to_dict())

        binding = DeviceBinding(
            user_id=g.user.id,
            device_id=device_id,
            device_name=data.get('device_name', ''),
            platform=data.get('platform', 'web'),
        )
        db.add(binding)
        db.commit()
        return _ok(binding.to_dict(), 201)
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
