"""
embedded_main.py — Headless entry point for HART on embedded/robot devices.

Boots with minimal imports: security + gossip + sync + fleet commands.
Does NOT import Flask, langchain, autogen, torch, numpy, opencv, PIL, redis.

Usage:
    HEVOLVE_HEADLESS=true python3 embedded_main.py

Environment variables:
    HEVOLVE_HEADLESS=true           Required. Enables headless mode.
    HEVOLVE_CODE_HASH_PRECOMPUTED   Skip code hash computation (ROM/SD card).
    HEVOLVE_TAMPER_CHECK_SKIP=true  Skip periodic tamper checks (read-only FS).
    HEVOLVE_FORCE_TIER=embedded     Force embedded tier (optional, auto-detected).
    HEVOLVE_GOSSIP_BANDWIDTH        Override gossip bandwidth profile (minimal/constrained/full).
    HEVOLVE_MQTT_BROKER             MQTT broker for sensor bridge (optional).
    HEVOLVE_DB_PATH                 SQLite DB path (default: agent_data/hevolve.db).

Queen Bee Authority:
    Central has instant, total authority over this node. Fleet commands arrive
    via MessageBus subscription (WAMP push — instant delivery, no polling).
    On startup, DB is drained once for commands queued while offline.
    Commands are signed with central's certificate and executed immediately:
        config_update, goal_assign, sensor_config, firmware_update, halt, restart
"""
import logging
import os
import signal
import sys
import time

# Set headless mode before any other imports
os.environ.setdefault('HEVOLVE_HEADLESS', 'true')

logger = logging.getLogger('hevolve_embedded')


def _setup_logging():
    """Minimal logging for embedded devices."""
    level_name = os.environ.get('HEVOLVE_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def _boot_identity():
    """Initialize Ed25519 keypair and compute code hash."""
    from security.node_integrity import get_or_create_keypair, compute_code_hash

    priv, pub = get_or_create_keypair()
    from security.node_integrity import get_public_key_hex
    node_id = get_public_key_hex()[:16]
    logger.info(f"Node identity: {node_id}")

    code_hash = compute_code_hash()
    logger.info(f"Code hash: {code_hash[:16]}...")

    return node_id, code_hash


def _boot_guardrails():
    """Verify guardrail integrity (required for federation)."""
    try:
        from security.hive_guardrails import (
            compute_guardrail_hash, verify_guardrail_integrity,
        )
        gh = compute_guardrail_hash()
        ok = verify_guardrail_integrity()
        logger.info(f"Guardrail hash: {gh[:16]}... (verified={ok})")
        return gh
    except ImportError:
        logger.warning("Guardrail module unavailable — running unverified")
        return ''


def _boot_system_check():
    """Run hardware detection and tier classification."""
    from security.system_requirements import run_system_check
    caps = run_system_check()
    logger.info(
        f"Tier: {caps.tier.value}, "
        f"Features: {caps.enabled_features}, "
        f"Hardware: cpu={caps.hardware.cpu_cores}, "
        f"ram={caps.hardware.ram_gb}GB, "
        f"gpio={caps.hardware.has_gpio}, "
        f"serial={caps.hardware.has_serial}"
    )
    return caps


def _boot_safety(caps):
    """Initialize safety monitor if hardware E-stop sources are configured."""
    estop_pins_raw = os.environ.get('HEVOLVE_ESTOP_PINS', '')
    estop_serial_raw = os.environ.get('HEVOLVE_ESTOP_SERIAL', '')

    if not estop_pins_raw and not estop_serial_raw:
        if caps.hardware.has_gpio or caps.hardware.has_serial:
            logger.info("Safety: GPIO/serial detected but no E-stop sources configured "
                        "(set HEVOLVE_ESTOP_PINS or HEVOLVE_ESTOP_SERIAL)")
        return

    try:
        from integrations.robotics.safety_monitor import get_safety_monitor
        monitor = get_safety_monitor()

        # Register GPIO E-stop pins (comma-separated)
        if estop_pins_raw:
            for pin_str in estop_pins_raw.split(','):
                pin_str = pin_str.strip()
                if pin_str.isdigit():
                    monitor.register_estop_pin(int(pin_str))

        # Register serial E-stop sources (format: port:pattern,port:pattern)
        if estop_serial_raw:
            for entry in estop_serial_raw.split(','):
                entry = entry.strip()
                if ':' in entry:
                    port, pattern = entry.split(':', 1)
                    monitor.register_estop_serial(port.strip(), pattern.strip())
                elif entry:
                    monitor.register_estop_serial(entry, 'ESTOP')

        # Register workspace limits from env
        limits_raw = os.environ.get('HEVOLVE_WORKSPACE_LIMITS', '')
        if limits_raw:
            try:
                import json
                limits = json.loads(limits_raw)
                monitor.register_workspace_limits(limits)
            except Exception as e:
                logger.warning(f"Safety: invalid HEVOLVE_WORKSPACE_LIMITS: {e}")

        monitor.start()
        logger.info("Safety monitor started")
    except ImportError:
        logger.debug("Safety: robotics module not available")
    except Exception as e:
        logger.warning(f"Safety monitor boot failed: {e}")


def _boot_db():
    """Initialize SQLite database for fleet commands + sync queue."""
    db_path = os.environ.get('HEVOLVE_DB_PATH',
                             os.environ.get('SOCIAL_DB_PATH', 'agent_data/hevolve.db'))
    os.environ['HEVOLVE_DB_PATH'] = db_path

    try:
        from integrations.social.models import get_engine, Base
        engine = get_engine()
        Base.metadata.create_all(engine)
        logger.info(f"Database ready: {db_path}")
        return True
    except Exception as e:
        logger.warning(f"Database init failed (gossip-only mode): {e}")
        return False


def _drain_fleet_commands(node_id: str):
    """Drain pending fleet commands from DB (startup only — catches offline-queued commands)."""
    try:
        from integrations.social.models import get_db
        from integrations.social.fleet_command import FleetCommandService

        db = get_db()
        try:
            commands = FleetCommandService.get_pending_commands(db, node_id)
            for cmd in commands:
                _execute_fleet_command(cmd, node_id, db)
            if commands:
                db.commit()
                logger.info(f"Fleet: drained {len(commands)} offline-queued commands")
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Fleet command drain failed: {e}")


def _execute_fleet_command(cmd: dict, node_id: str, db=None):
    """Execute a single fleet command (shared by DB drain + MessageBus handler)."""
    from integrations.social.fleet_command import FleetCommandService

    logger.info(f"Fleet command: {cmd['cmd_type']} from {cmd.get('issued_by', '?')[:8]}...")

    # Verify signature before executing
    if not FleetCommandService.verify_command_signature(cmd):
        logger.warning(f"Fleet command {cmd.get('id', '?')}: invalid signature, skipping")
        if db and cmd.get('id'):
            FleetCommandService.ack_command(
                db, cmd['id'], node_id, success=False,
                result_message='Invalid signature',
            )
        return

    # Execute the command
    result = FleetCommandService.execute_command(
        cmd['cmd_type'], cmd.get('params', {}),
    )
    if db and cmd.get('id'):
        FleetCommandService.ack_command(
            db, cmd['id'], node_id,
            success=result.get('success', False),
            result_message=result.get('message', ''),
        )


def _subscribe_fleet_commands(node_id: str):
    """Subscribe to fleet.command via MessageBus for instant WAMP-pushed commands."""
    try:
        from core.peer_link.message_bus import get_message_bus
        bus = get_message_bus()

        def _on_fleet_command(topic, data):
            """Handle fleet command received via WAMP push (instant, no polling)."""
            if not isinstance(data, dict):
                return
            target = data.get('target_node_id', '')
            if target and target != node_id:
                return  # Not for this node
            try:
                _execute_fleet_command(data, node_id)
            except Exception as e:
                logger.error(f"Fleet command execution failed: {e}")

        bus.subscribe('fleet.command', _on_fleet_command)
        logger.info("Fleet: subscribed to MessageBus (instant delivery)")
    except Exception as e:
        logger.warning(f"Fleet: MessageBus subscription failed: {e}")


def _check_halt():
    """Check if halt has been requested (by fleet command or circuit breaker)."""
    if os.environ.get('HEVOLVE_HALTED', '').lower() == 'true':
        reason = os.environ.get('HEVOLVE_HALT_REASON', 'Unknown')
        logger.critical(f"HALT: {reason}")
        return True
    return False


def _check_restart():
    """Check if restart has been requested by fleet command."""
    target = os.environ.get('HEVOLVE_RESTART_REQUESTED', '')
    if target:
        os.environ.pop('HEVOLVE_RESTART_REQUESTED', None)
        logger.info(f"Restart requested: {target}")
        return target
    return ''


def _main_loop(node_id: str, gossip_interval: int = 60):
    """Main loop: gossip + halt/restart checks.

    Fleet commands arrive via MessageBus subscription (instant, event-driven).
    DB drain runs once at startup for commands queued while offline.
    Loop only handles halt/restart flag checks and gossip heartbeat.
    """
    # One-time: drain commands queued in DB while we were offline
    _drain_fleet_commands(node_id)

    # Subscribe to MessageBus for instant fleet command delivery
    _subscribe_fleet_commands(node_id)

    tick = 0
    logger.info(f"Main loop started (interval={gossip_interval}s)")

    while True:
        tick += 1

        # Check halt flag (queen bee authority)
        if _check_halt():
            logger.critical("Halted by central. Exiting.")
            break

        # Check restart flag
        restart_target = _check_restart()
        if restart_target:
            logger.info(f"Restarting {restart_target}...")
            # For now, just log. Future: restart specific subsystems.

        # Sleep for gossip interval (fleet commands arrive via MessageBus, not polling)
        try:
            time.sleep(gossip_interval)
        except KeyboardInterrupt:
            logger.info("Interrupted. Shutting down.")
            break


def main():
    """Boot sequence for embedded/headless HART node."""
    _setup_logging()
    logger.info("=" * 50)
    logger.info("HART Embedded Node — Starting")
    logger.info("=" * 50)

    # Graceful shutdown on SIGTERM
    def _sigterm_handler(signum, frame):
        logger.info("SIGTERM received. Shutting down.")
        os.environ['HEVOLVE_HALTED'] = 'true'
        os.environ['HEVOLVE_HALT_REASON'] = 'SIGTERM'

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Step 1: System check (tier, hardware detection)
    caps = _boot_system_check()

    # Step 2: Identity (Ed25519 keypair, code hash)
    node_id, code_hash = _boot_identity()

    # Step 3: Guardrails (required for federation)
    guardrail_hash = _boot_guardrails()

    # Step 3.5: Safety monitor (E-stop, workspace limits)
    _boot_safety(caps)

    # Step 3.6: Platform EventBus + MessageBus
    try:
        from core.platform.bootstrap import bootstrap_platform
        bootstrap_platform()
        logger.info("Platform bootstrapped (EventBus + ServiceRegistry)")
    except Exception as e:
        logger.warning(f"Platform bootstrap failed: {e}")

    # Step 3.7: Local Crossbar subscribers (confirmation, progress, exceptions)
    try:
        from core.peer_link.local_subscribers import bootstrap_local_subscribers
        bootstrap_local_subscribers()
        logger.info("Local subscribers bootstrapped")
    except Exception as e:
        logger.warning(f"Local subscribers bootstrap failed: {e}")

    # Step 4: Database (fleet commands, sync queue)
    db_ok = _boot_db()

    # Step 4.5: Robot subsystems (if enabled)
    robot_enabled = os.environ.get('HEVOLVE_ROBOT_ENABLED', '').lower() == 'true'
    robot_status = None
    if robot_enabled:
        try:
            from integrations.robotics.robot_boot import boot_robotics
            robot_status = boot_robotics(caps)
        except ImportError:
            logger.warning("Robot boot: robotics package not available")
        except Exception as e:
            logger.warning(f"Robot boot failed: {e}")
    else:
        logger.info("Robot subsystems disabled (set HEVOLVE_ROBOT_ENABLED=true)")

    # Step 5: Determine gossip interval from tier/bandwidth profile
    bandwidth = os.environ.get('HEVOLVE_GOSSIP_BANDWIDTH', '')
    if not bandwidth:
        # Auto-select from tier
        tier_name = caps.tier.value
        bandwidth = {
            'embedded': 'minimal',
            'observer': 'constrained',
            'lite': 'constrained',
        }.get(tier_name, 'full')

    gossip_intervals = {
        'minimal': 900,      # 15 minutes
        'constrained': 300,  # 5 minutes
        'full': 60,          # 1 minute
    }
    interval = gossip_intervals.get(bandwidth, 60)

    logger.info(f"Gossip bandwidth: {bandwidth} (interval={interval}s)")

    # Step 6: Enter main loop
    try:
        _main_loop(node_id, gossip_interval=interval)
    except Exception as e:
        logger.critical(f"Main loop crashed: {e}")
        sys.exit(1)

    logger.info("HART Embedded Node - Stopped")


if __name__ == '__main__':
    main()
