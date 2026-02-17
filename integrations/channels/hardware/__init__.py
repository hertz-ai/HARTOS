"""
Hardware I/O Channel Adapters — Serial, GPIO, WAMP IoT, ROS bridge.

Auto-detects available hardware at boot and registers appropriate adapters.
All follow the ChannelAdapter base pattern from integrations/channels/base.py.

IoT pub/sub uses existing Crossbar/WAMP infrastructure (not MQTT).
"""
import logging
import os

logger = logging.getLogger(__name__)


def auto_register_hardware_adapters(registry=None):
    """Detect hardware at boot and register appropriate adapters.

    Args:
        registry: Optional ChannelRegistry to register adapters with.
            If None, returns a list of adapter classes that are available.

    Returns:
        List of (adapter_name, adapter_class) tuples for available hardware.
    """
    available = []

    # Serial adapter — available if pyserial installed or /dev/tty* exists
    try:
        from .serial_adapter import SerialAdapter
        available.append(('serial', SerialAdapter))
        logger.info("Hardware adapter available: serial")
    except ImportError:
        pass

    # GPIO adapter — available on Linux with gpiod or RPi.GPIO
    try:
        from .gpio_adapter import GPIOAdapter
        if GPIOAdapter.is_available():
            available.append(('gpio', GPIOAdapter))
            logger.info("Hardware adapter available: gpio")
    except ImportError:
        pass

    # WAMP IoT adapter — uses existing Crossbar router for IoT pub/sub
    # Registered when CBURL is set or IoT topics are configured
    crossbar_url = os.environ.get('CBURL', '')
    iot_topics = os.environ.get('HEVOLVE_IOT_TOPICS', '')
    if crossbar_url or iot_topics:
        try:
            from .wamp_iot_adapter import WAMPIoTAdapter
            available.append(('wamp_iot', WAMPIoTAdapter))
            logger.info(f"Hardware adapter available: wamp_iot (crossbar={crossbar_url or 'default'})")
        except ImportError:
            logger.debug("WAMP IoT configured but autobahn not installed")

    # ROS bridge — only if explicitly enabled (pulls ~500MB deps)
    if os.environ.get('HEVOLVE_ROS_BRIDGE_ENABLED', '').lower() == 'true':
        try:
            from .ros_bridge import ROSBridgeAdapter
            available.append(('ros', ROSBridgeAdapter))
            logger.info("Hardware adapter available: ros")
        except ImportError:
            logger.debug("ROS bridge enabled but rclpy not installed")

    if registry is not None:
        for name, adapter_class in available:
            try:
                registry.register(name, adapter_class)
            except Exception as e:
                logger.warning(f"Failed to register {name} adapter: {e}")

    return available
