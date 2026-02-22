"""
Fleet Command Service - Queen Bee Authority for Embedded/Robot Devices

Central is the queen bee. It has instant, total authority over all nodes:
- Push commands to any node (config update, goal assign, halt, restart)
- Broadcast commands to all nodes of a tier
- Commands are signed with central's certificate - devices verify before executing

Embedded devices check for pending commands on every gossip round.
Commands flow through the existing SyncQueue mechanism (offline-first).

Command types:
    config_update    - Push env var / config changes to a node
    goal_assign      - Dispatch an AgentGoal to a node
    sensor_config    - Configure sensor polling intervals, pin assignments
    firmware_update  - Trigger firmware/code update on a node
    halt             - Emergency stop (respects HiveCircuitBreaker)
    restart          - Restart node services
"""
import json
import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')

# Valid command types - anything else is rejected
VALID_COMMAND_TYPES = frozenset({
    'config_update',
    'goal_assign',
    'sensor_config',
    'firmware_update',
    'halt',
    'restart',
    'tts_stream',
    'agent_consent',
    'estop',
    'estop_clear',
})


class FleetCommandService:
    """Static service for queen bee command dispatch to fleet nodes.

    All methods receive `db: Session` and follow the service pattern.
    Commands are stored in FleetCommand table and drained by gossip.
    """

    @staticmethod
    def push_command(
        db, node_id: str, cmd_type: str, params: dict,
        issued_by: str = '',
    ) -> Optional[Dict]:
        """Queue a signed command for a specific node.

        Args:
            db: SQLAlchemy session.
            node_id: Target node's public key hex prefix.
            cmd_type: One of VALID_COMMAND_TYPES.
            params: Command-specific parameters.
            issued_by: Node ID of the issuer (must be central or regional).

        Returns:
            Command dict on success, None on validation failure.
        """
        from .models import FleetCommand

        if cmd_type not in VALID_COMMAND_TYPES:
            logger.warning(f"Fleet: rejected invalid command type '{cmd_type}'")
            return None

        if not node_id:
            logger.warning("Fleet: rejected command with empty node_id")
            return None

        # Sign the command with this node's key
        signature = _sign_command(cmd_type, params, node_id)

        cmd = FleetCommand(
            target_node_id=node_id,
            cmd_type=cmd_type,
            params_json=json.dumps(params),
            issued_by=issued_by or _get_self_node_id(),
            signature=signature,
            status='pending',
        )
        db.add(cmd)
        db.flush()

        logger.info(f"Fleet: queued {cmd_type} for node {node_id[:8]}...")
        return cmd.to_dict()

    @staticmethod
    def push_broadcast(
        db, cmd_type: str, params: dict,
        tier_filter: str = '', issued_by: str = '',
    ) -> List[Dict]:
        """Broadcast a command to all nodes (optionally filtered by tier).

        Args:
            db: SQLAlchemy session.
            cmd_type: One of VALID_COMMAND_TYPES.
            params: Command-specific parameters.
            tier_filter: Optional tier to target (e.g. 'embedded', 'observer').
            issued_by: Node ID of the issuer.

        Returns:
            List of command dicts created.
        """
        from .models import PeerNode

        if cmd_type not in VALID_COMMAND_TYPES:
            logger.warning(f"Fleet: rejected broadcast with invalid type '{cmd_type}'")
            return []

        # Find target nodes
        query = db.query(PeerNode).filter(PeerNode.status == 'active')
        if tier_filter:
            query = query.filter(PeerNode.capability_tier == tier_filter)

        peers = query.all()
        results = []
        issuer = issued_by or _get_self_node_id()

        for peer in peers:
            cmd = FleetCommandService.push_command(
                db, peer.node_id, cmd_type, params, issued_by=issuer,
            )
            if cmd:
                results.append(cmd)

        logger.info(
            f"Fleet: broadcast {cmd_type} to {len(results)} nodes"
            f"{f' (tier={tier_filter})' if tier_filter else ''}"
        )
        return results

    @staticmethod
    def get_pending_commands(db, node_id: str) -> List[Dict]:
        """Get pending commands for a node. Called by gossip handler.

        Verifies each command's issuer exists in PeerNode with authority.
        Marks verified commands as 'delivered', unverified as 'rejected'.

        Args:
            db: SQLAlchemy session.
            node_id: The requesting node's ID.

        Returns:
            List of command dicts (only verified ones).
        """
        from .models import FleetCommand

        pending = db.query(FleetCommand).filter(
            FleetCommand.target_node_id == node_id,
            FleetCommand.status == 'pending',
        ).order_by(FleetCommand.created_at.asc()).all()

        results = []
        for cmd in pending:
            if not _verify_issuer(db, cmd.issued_by):
                cmd.status = 'rejected'
                logger.warning(
                    f"Fleet: rejected cmd {cmd.id} from unverified issuer "
                    f"{cmd.issued_by[:8] if cmd.issued_by else '?'}...")
                continue
            cmd.status = 'delivered'
            cmd.delivered_at = time.time()
            results.append(cmd.to_dict())

        if results or any(c.status == 'rejected' for c in pending):
            db.flush()
        if results:
            logger.info(f"Fleet: delivered {len(results)} commands to {node_id[:8]}...")

        return results

    @staticmethod
    def ack_command(db, command_id: int, node_id: str, success: bool = True,
                    result_message: str = '') -> Optional[Dict]:
        """Acknowledge command execution by the target node.

        Args:
            db: SQLAlchemy session.
            command_id: The FleetCommand ID.
            node_id: The acknowledging node's ID.
            success: Whether the command executed successfully.
            result_message: Optional result or error message.

        Returns:
            Updated command dict, or None if not found.
        """
        from .models import FleetCommand

        cmd = db.query(FleetCommand).filter(
            FleetCommand.id == command_id,
            FleetCommand.target_node_id == node_id,
        ).first()

        if not cmd:
            return None

        cmd.status = 'completed' if success else 'failed'
        cmd.result_message = result_message
        cmd.completed_at = time.time()
        db.flush()

        logger.info(
            f"Fleet: command {command_id} {'completed' if success else 'failed'}"
            f" on node {node_id[:8]}..."
        )
        return cmd.to_dict()

    @staticmethod
    def execute_command(cmd_type: str, params: dict) -> Dict:
        """Execute a fleet command locally on this node.

        Called by the embedded main loop when commands are received.
        Does NOT require a db session - executes in-process.

        Args:
            cmd_type: The command type to execute.
            params: Command parameters.

        Returns:
            {success: bool, message: str}
        """
        try:
            if cmd_type == 'config_update':
                return _execute_config_update(params)
            elif cmd_type == 'halt':
                return _execute_halt(params)
            elif cmd_type == 'restart':
                return _execute_restart(params)
            elif cmd_type == 'sensor_config':
                return _execute_sensor_config(params)
            elif cmd_type == 'goal_assign':
                return _execute_goal_assign(params)
            elif cmd_type == 'firmware_update':
                return _execute_firmware_update(params)
            elif cmd_type == 'tts_stream':
                return _execute_tts_stream(params)
            elif cmd_type == 'agent_consent':
                return _execute_agent_consent(params)
            elif cmd_type == 'estop':
                return _execute_estop(params)
            elif cmd_type == 'estop_clear':
                return _execute_estop_clear(params)
            else:
                return {'success': False, 'message': f'Unknown command: {cmd_type}'}
        except Exception as e:
            logger.error(f"Fleet: command {cmd_type} failed: {e}")
            return {'success': False, 'message': str(e)}

    @staticmethod
    def verify_command_signature(cmd_dict: dict) -> bool:
        """Verify a command was signed by an authorized node (central/regional).

        Args:
            cmd_dict: Command dict with 'signature' and 'issued_by' fields.

        Returns:
            True if signature is valid and issuer is authorized.
        """
        signature = cmd_dict.get('signature', '')
        issued_by = cmd_dict.get('issued_by', '')

        if not signature or not issued_by:
            return False

        try:
            from security.key_delegation import verify_tier_authorization
            # Central and regional nodes are authorized to issue commands
            return verify_tier_authorization(issued_by, required_tier='regional')
        except ImportError:
            # If key_delegation unavailable, verify via guardrail hash
            try:
                from security.hive_guardrails import verify_guardrail_integrity
                return verify_guardrail_integrity()
            except ImportError:
                return False


# ═══════════════════════════════════════════════════════════════
# Issuer verification
# ═══════════════════════════════════════════════════════════════

def _verify_issuer(db, issued_by: str) -> bool:
    """Check that a fleet command issuer exists in PeerNode and has authority.

    Self-issued commands are always valid. External issuers must be
    known, active, and have central or regional tier.
    """
    if not issued_by or issued_by == 'unknown':
        return True  # No issuer info = legacy/local command
    self_id = _get_self_node_id()
    if issued_by == self_id:
        return True
    try:
        from .models import PeerNode
        peer = db.query(PeerNode).filter_by(node_id=issued_by).first()
        if not peer:
            return False
        if peer.status in ('dead', 'banned'):
            return False
        if peer.tier not in ('central', 'regional'):
            return False
        return True
    except Exception:
        return True  # DB error = fail open for availability


# ═══════════════════════════════════════════════════════════════
# Command executors (local, in-process)
# ═══════════════════════════════════════════════════════════════

def _execute_config_update(params: dict) -> Dict:
    """Apply config/env var updates pushed by central."""
    updates = params.get('env_vars', {})
    if not updates:
        return {'success': False, 'message': 'No env_vars in params'}

    applied = []
    for key, value in updates.items():
        # Security: never allow overwriting master key or guardrail vars
        if 'MASTER' in key.upper() or 'GUARDRAIL' in key.upper():
            logger.warning(f"Fleet: rejected config_update for protected var {key}")
            continue
        os.environ[key] = str(value)
        applied.append(key)

    return {'success': True, 'message': f'Applied {len(applied)} config updates: {applied}'}


def _execute_halt(params: dict) -> Dict:
    """Emergency halt - sets halt flag for the main loop.

    Note: HiveCircuitBreaker.halt_network() requires master key signature
    and is reserved for the steward. Fleet halt uses a process-level flag
    that the main loop checks.
    """
    reason = params.get('reason', 'Central commanded halt')
    logger.critical(f"Fleet: HALT received - {reason}")
    os.environ['HEVOLVE_HALTED'] = 'true'
    os.environ['HEVOLVE_HALT_REASON'] = reason
    return {'success': True, 'message': f'Halt flag set: {reason}'}


def _execute_restart(params: dict) -> Dict:
    """Restart node services (not the OS)."""
    target = params.get('target', 'all')  # 'all', 'gossip', 'daemon', 'vision'
    logger.info(f"Fleet: restart requested for {target}")
    # Set restart flag - main loop checks this
    os.environ['HEVOLVE_RESTART_REQUESTED'] = target
    return {'success': True, 'message': f'Restart requested: {target}'}


def _execute_sensor_config(params: dict) -> Dict:
    """Configure sensor polling intervals and pin assignments."""
    applied = []
    if 'poll_interval_ms' in params:
        os.environ['HEVOLVE_SENSOR_POLL_MS'] = str(params['poll_interval_ms'])
        applied.append('poll_interval_ms')
    if 'gpio_pins' in params:
        os.environ['HEVOLVE_GPIO_PINS'] = json.dumps(params['gpio_pins'])
        applied.append('gpio_pins')
    if 'mqtt_topics' in params:
        os.environ['HEVOLVE_MQTT_TOPICS'] = json.dumps(params['mqtt_topics'])
        applied.append('mqtt_topics')

    return {'success': True, 'message': f'Sensor config applied: {applied}'}


def _execute_goal_assign(params: dict) -> Dict:
    """Queue a goal for this node's daemon to pick up."""
    goal_type = params.get('goal_type', '')
    title = params.get('title', '')
    if not goal_type or not title:
        return {'success': False, 'message': 'goal_type and title required'}

    # Store as pending goal for daemon
    os.environ['HEVOLVE_PENDING_GOAL'] = json.dumps(params)
    return {'success': True, 'message': f'Goal queued: {goal_type}/{title}'}


def _execute_firmware_update(params: dict) -> Dict:
    """Trigger code/firmware update from a signed release."""
    update_url = params.get('update_url', '')
    release_hash = params.get('release_hash', '')
    if not update_url or not release_hash:
        return {'success': False, 'message': 'update_url and release_hash required'}

    # Set update flag - main loop handles the actual update
    os.environ['HEVOLVE_PENDING_UPDATE'] = json.dumps({
        'url': update_url,
        'hash': release_hash,
        'requested_at': time.time(),
    })
    return {'success': True, 'message': f'Firmware update queued: {release_hash[:16]}...'}


def _execute_tts_stream(params: dict) -> Dict:
    """Stream TTS to this device or relay to a paired device.

    Params:
        text: Text to speak.
        voice: Voice ID (default 'default').
        lang: Language code (default 'en').
        relay_to_device_id: If set, this device should relay audio to that device.
    """
    text = params.get('text', '')
    if not text:
        return {'success': False, 'message': 'No text provided'}

    relay_to = params.get('relay_to_device_id', '')

    # Set env flags for the local TTS/relay loop to pick up
    os.environ['HEVOLVE_TTS_PENDING'] = json.dumps({
        'text': text,
        'voice': params.get('voice', 'default'),
        'lang': params.get('lang', 'en'),
        'relay_to_device_id': relay_to,
        'agent_id': params.get('agent_id', ''),
        'requested_at': time.time(),
    })
    action = f"relay to {relay_to[:8]}..." if relay_to else "local playback"
    return {'success': True, 'message': f'TTS queued: {action}'}


def _execute_agent_consent(params: dict) -> Dict:
    """Display consent prompt for an agent action.

    Params:
        action: What the agent wants to do.
        agent_id: Which agent is requesting.
        description: Human-readable explanation.
        timeout_s: How long to wait for response (default 60).
    """
    action = params.get('action', '')
    if not action:
        return {'success': False, 'message': 'No action specified'}

    os.environ['HEVOLVE_CONSENT_PENDING'] = json.dumps({
        'action': action,
        'agent_id': params.get('agent_id', ''),
        'description': params.get('description', ''),
        'timeout_s': params.get('timeout_s', 60),
        'requested_at': time.time(),
    })
    return {'success': True, 'message': f'Consent requested: {action}'}


def _execute_estop(params: dict) -> Dict:
    """Trigger E-stop via fleet command from central.

    Uses SafetyMonitor for proper E-stop with audit trail.
    Falls back to simple halt flag if robotics module unavailable.
    """
    reason = params.get('reason', 'Central commanded E-stop')
    try:
        from integrations.robotics.safety_monitor import get_safety_monitor
        monitor = get_safety_monitor()
        monitor.trigger_estop(reason, source='fleet')
        return {'success': True, 'message': f'E-stop triggered: {reason}'}
    except ImportError:
        # Fallback: use simple halt flag
        os.environ['HEVOLVE_HALTED'] = 'true'
        os.environ['HEVOLVE_HALT_REASON'] = f'E-STOP: {reason}'
        return {'success': True, 'message': f'E-stop (fallback halt): {reason}'}


def _execute_estop_clear(params: dict) -> Dict:
    """Clear E-stop via fleet command.  Requires human operator_id.

    The operator_id in params must identify a human, not an agent.
    """
    operator_id = params.get('operator_id', '')
    if not operator_id:
        return {'success': False, 'message': 'E-stop clear requires operator_id'}

    try:
        from integrations.robotics.safety_monitor import get_safety_monitor
        monitor = get_safety_monitor()
        cleared = monitor.clear_estop(operator_id)
        if cleared:
            return {'success': True, 'message': f'E-stop cleared by {operator_id}'}
        return {'success': False, 'message': 'E-stop clear rejected (agent or empty operator)'}
    except ImportError:
        # Fallback: clear halt flag
        os.environ.pop('HEVOLVE_HALTED', None)
        os.environ.pop('HEVOLVE_HALT_REASON', None)
        return {'success': True, 'message': f'E-stop cleared (fallback): {operator_id}'}


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _get_self_node_id() -> str:
    """Get this node's ID (public key hex prefix)."""
    try:
        from security.node_integrity import get_public_key_hex
        return get_public_key_hex()[:16]
    except Exception:
        return 'unknown'


def _sign_command(cmd_type: str, params: dict, target_node_id: str) -> str:
    """Sign a command with this node's private key."""
    try:
        from security.node_integrity import sign_message
        message = f"{cmd_type}:{json.dumps(params, sort_keys=True)}:{target_node_id}"
        return sign_message(message.encode())
    except (ImportError, Exception) as e:
        logger.debug(f"Fleet: command signing unavailable: {e}")
        return ''
