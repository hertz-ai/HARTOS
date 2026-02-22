"""
Robot Boot — Initializes all robotics subsystems.

Called from embedded_main.py when HEVOLVE_ROBOT_ENABLED=true.
Connects:
  - SafetyMonitor (from Batch 1)
  - SensorStore + adapters (from Batch 2)
  - WorldModelBridge embodied extensions (from Batch 3)
  - ControlLoopBridge timing (from Batch 3)
  - CapabilityAdvertiser (from Batch 4)

Does NOT start any intelligence.  Just wires the routing layer.
Hevolve-Core starts its own intelligence when it receives data.
"""
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve_robotics')


def boot_robotics(caps: Any = None) -> Dict:
    """Initialize all robotics subsystems.

    Args:
        caps: SystemCheckResult from system_requirements.run_system_check()
              (optional — used for hardware detection hints)

    Returns:
        Dict with initialization status for each subsystem.
    """
    status: Dict[str, Any] = {
        'safety': False,
        'sensor_store': False,
        'control_loop': False,
        'capability_advertiser': False,
        'bridge_ready': False,
    }

    # 1. Safety monitor (may already be running from _boot_safety)
    try:
        from integrations.robotics.safety_monitor import get_safety_monitor
        monitor = get_safety_monitor()
        status['safety'] = True
        logger.info("Robot boot: safety monitor ready")
    except Exception as e:
        logger.warning(f"Robot boot: safety monitor failed: {e}")

    # 2. Sensor store
    try:
        from integrations.robotics.sensor_store import get_sensor_store
        store = get_sensor_store()
        status['sensor_store'] = True
        logger.info("Robot boot: sensor store ready")
    except Exception as e:
        logger.warning(f"Robot boot: sensor store failed: {e}")

    # 3. Connect hardware adapters to sensor store
    _connect_sensor_adapters(caps)

    # 4. Control loop bridge
    try:
        from integrations.robotics.control_loop import ControlLoopBridge
        loop = ControlLoopBridge()

        # Register sensor ingestion callback
        hz = float(os.environ.get('HEVOLVE_SENSOR_INGEST_HZ', '10'))
        loop.register_callback('sensor_ingest', _sensor_ingest_tick, hz=hz)

        # Register learning feedback poll
        feedback_hz = float(os.environ.get('HEVOLVE_FEEDBACK_HZ', '1'))
        loop.register_callback('feedback_poll', _feedback_poll_tick,
                               hz=feedback_hz)

        loop.start()
        status['control_loop'] = True
        logger.info(f"Robot boot: control loop started "
                    f"(sensor={hz}Hz, feedback={feedback_hz}Hz)")
    except Exception as e:
        logger.warning(f"Robot boot: control loop failed: {e}")

    # 5. Capability advertiser
    try:
        from integrations.robotics.capability_advertiser import (
            get_capability_advertiser,
        )
        adv = get_capability_advertiser()
        adv.detect_capabilities()
        status['capability_advertiser'] = True
        caps_summary = adv.get_gossip_payload()
        logger.info(f"Robot boot: capabilities detected: {caps_summary}")
    except Exception as e:
        logger.warning(f"Robot boot: capability detection failed: {e}")

    # 6. Verify WorldModelBridge is operational
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        stats = bridge.get_stats()
        status['bridge_ready'] = True
        logger.info("Robot boot: world model bridge ready")
    except Exception as e:
        logger.warning(f"Robot boot: bridge check failed: {e}")

    booted = sum(1 for v in status.values() if v)
    total = len(status)
    logger.info(f"Robot boot complete: {booted}/{total} subsystems ready")

    return status


def _connect_sensor_adapters(caps: Any = None):
    """Wire hardware adapters to SensorStore based on available hardware."""
    hw = getattr(caps, 'hardware', None) if caps else None

    # Serial adapter
    if hw and getattr(hw, 'has_serial', False):
        try:
            from integrations.robotics.sensor_adapters import (
                SerialSensorBridge,
            )
            port = os.environ.get('HEVOLVE_SERIAL_PORT', '')
            if port:
                bridge = SerialSensorBridge(port=port)
                logger.info(f"Robot boot: serial sensor bridge on {port}")
        except Exception as e:
            logger.debug(f"Serial sensor bridge skipped: {e}")

    # GPIO adapter
    if hw and getattr(hw, 'has_gpio', False):
        try:
            from integrations.robotics.sensor_adapters import (
                GPIOSensorBridge,
            )
            pins_raw = os.environ.get('HEVOLVE_GPIO_SENSOR_PINS', '')
            if pins_raw:
                pins = [int(p.strip()) for p in pins_raw.split(',')
                        if p.strip().isdigit()]
                bridge = GPIOSensorBridge(pins=pins)
                logger.info(f"Robot boot: GPIO sensor bridge on pins {pins}")
        except Exception as e:
            logger.debug(f"GPIO sensor bridge skipped: {e}")


def _sensor_ingest_tick():
    """Periodic callback: flush sensor store readings to Hevolve-Core."""
    try:
        from integrations.robotics.sensor_store import get_sensor_store
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        store = get_sensor_store()
        bridge = get_world_model_bridge()

        # Get all latest readings and batch-send to bridge
        latest = store.get_all_latest()
        if latest:
            readings = [r.to_dict() for r in latest.values()]
            bridge.ingest_sensor_batch(readings)
    except Exception:
        pass  # Silently skip — control loop handles errors


def _feedback_poll_tick():
    """Periodic callback: poll Hevolve-Core for learning feedback."""
    try:
        from integrations.agent_engine.world_model_bridge import (
            get_world_model_bridge,
        )
        bridge = get_world_model_bridge()
        feedback = bridge.get_learning_feedback()
        if feedback:
            logger.debug(f"Learning feedback: {feedback}")
    except Exception:
        pass
