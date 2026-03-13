"""
Device Routing Service — Cross-Device Agent Communication

Routes agent actions (TTS, consent requests) to the best device for a user.
Uses existing FleetCommandService for delivery and NotificationService for alerts.

Device selection priority for TTS:
  1. Phone (has speaker + local TTS capability)
  2. Desktop/tablet (STANDARD+ tier, full TTS)
  3. Cloud fallback (no local device available)

When target is a watch: find phone as relay → push tts_stream with relay_to_device_id.
"""
import json
import logging
from typing import Dict, List, Optional

from .fleet_command import FleetCommandService
from .models import DeviceBinding
from .services import NotificationService

logger = logging.getLogger('hevolve_social')

# Form factor priority for TTS (lower index = preferred)
# 'robot' added — robots with speakers can receive TTS (lowest priority)
_TTS_PRIORITY = ['phone', 'desktop', 'tablet', 'tv', 'embedded', 'robot']


class DeviceRoutingService:
    """Static service for routing agent actions to the right user device."""

    @staticmethod
    def get_user_device_map(db, user_id: str) -> List[Dict]:
        """List all active devices for a user with parsed capabilities.

        Args:
            db: SQLAlchemy session.
            user_id: Target user.

        Returns:
            List of DeviceBinding.to_dict() with parsed capabilities.
        """
        devices = db.query(DeviceBinding).filter_by(
            user_id=user_id, is_active=True,
        ).all()
        return [d.to_dict() for d in devices]

    @staticmethod
    def pick_device(db, user_id: str, required_capability: str = 'tts') -> Optional[Dict]:
        """Find the best device for a capability.

        Args:
            db: SQLAlchemy session.
            user_id: Target user.
            required_capability: Capability key to look for (e.g. 'tts', 'mic', 'speaker').

        Returns:
            DeviceBinding.to_dict() of the best device, or None.
        """
        devices = db.query(DeviceBinding).filter_by(
            user_id=user_id, is_active=True,
        ).all()

        candidates = []
        for d in devices:
            caps = d.capabilities
            if caps.get(required_capability):
                try:
                    priority = _TTS_PRIORITY.index(d.form_factor)
                except ValueError:
                    priority = len(_TTS_PRIORITY)
                candidates.append((priority, d))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        return candidates[0][1].to_dict()

    @staticmethod
    def route_tts(
        db, user_id: str, text: str,
        agent_id: str = '', voice: str = 'default', lang: str = 'en',
    ) -> Dict:
        """Route TTS to the best device for this user.

        If user has a watch as their only device, find a phone to relay through.
        Falls back to cloud notification if no TTS device available.

        Args:
            db: SQLAlchemy session.
            user_id: Target user.
            text: Text to speak.
            agent_id: Agent requesting TTS.
            voice: TTS voice ID.
            lang: Language code.

        Returns:
            {success, device_id, method} dict.
        """
        devices = db.query(DeviceBinding).filter_by(
            user_id=user_id, is_active=True,
        ).all()

        if not devices:
            return {'success': False, 'error': 'No devices linked for user'}

        # Find TTS-capable device
        tts_device = None
        relay_target = None
        best_priority = len(_TTS_PRIORITY) + 1

        for d in devices:
            caps = d.capabilities
            if caps.get('tts'):
                try:
                    prio = _TTS_PRIORITY.index(d.form_factor)
                except ValueError:
                    prio = len(_TTS_PRIORITY)
                if prio < best_priority:
                    best_priority = prio
                    tts_device = d

        # Check if there's a watch that needs relay
        watch_devices = [d for d in devices if d.form_factor == 'watch']

        params = {
            'text': text,
            'voice': voice,
            'lang': lang,
            'agent_id': agent_id,
        }

        if tts_device:
            # If there's a watch, relay through the TTS device
            if watch_devices:
                params['relay_to_device_id'] = watch_devices[0].device_id

            FleetCommandService.push_command(
                db, tts_device.device_id, 'tts_stream', params,
            )
            db.flush()
            return {
                'success': True,
                'device_id': tts_device.device_id,
                'method': 'fleet_command',
                'relay_to': params.get('relay_to_device_id', ''),
            }

        # No TTS device — fall back to notification
        try:
            NotificationService.create(
                db, user_id, 'agent_tts_fallback',
                source_user_id=agent_id,
                message=text,
            )
            db.flush()
            return {'success': True, 'device_id': '', 'method': 'notification_fallback'}
        except Exception as e:
            logger.debug(f"DeviceRouting: TTS fallback failed: {e}")
            return {'success': False, 'error': str(e)}

    @staticmethod
    def request_consent(
        db, user_id: str, action: str, agent_id: str,
        description: str = '', timeout_s: int = 60,
    ) -> Dict:
        """Push consent request to user's primary device.

        Creates both a Notification (persistent, cross-device visible) and
        a FleetCommand (real-time push to best device).

        Args:
            db: SQLAlchemy session.
            user_id: Target user.
            action: What the agent wants to do.
            agent_id: Which agent is requesting.
            description: Human-readable explanation.
            timeout_s: How long to wait for response.

        Returns:
            {success, command_id, device_id} dict.
        """
        # Create persistent notification (visible on all devices)
        NotificationService.create(
            db, user_id, 'agent_consent_request',
            source_user_id=agent_id,
            message=f"[{action}] {description}",
        )

        # Find primary device (phone > desktop > tablet > any)
        devices = db.query(DeviceBinding).filter_by(
            user_id=user_id, is_active=True,
        ).order_by(DeviceBinding.last_sync_at.desc()).all()

        if not devices:
            db.flush()
            return {'success': True, 'command_id': None, 'device_id': '',
                    'method': 'notification_only'}

        # Pick best device — prefer phone, then by recency
        target = devices[0]
        for d in devices:
            if d.form_factor == 'phone':
                target = d
                break

        params = {
            'action': action,
            'agent_id': agent_id,
            'description': description,
            'timeout_s': timeout_s,
        }
        cmd = FleetCommandService.push_command(
            db, target.device_id, 'agent_consent', params,
        )
        db.flush()

        cmd_id = cmd.get('id') if cmd else None
        return {
            'success': True,
            'command_id': cmd_id,
            'device_id': target.device_id,
            'method': 'fleet_command',
        }
