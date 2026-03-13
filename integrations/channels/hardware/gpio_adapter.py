"""
GPIO Channel Adapter — Input/output pin control for embedded Linux boards.

Input pins generate Message events (button presses, sensors).
Output pins respond to agent commands (LEDs, relays, PWM servos).

Supports gpiod (modern Linux, preferred) and RPi.GPIO (legacy Raspberry Pi).

Usage:
    from integrations.channels.hardware.gpio_adapter import GPIOAdapter
    adapter = GPIOAdapter(input_pins=[17, 27], output_pins=[22, 23])
    adapter.on_message(handler)
    await adapter.start()
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Any

from integrations.channels.base import (
    ChannelAdapter, ChannelConfig, ChannelStatus,
    Message, SendResult,
)

logger = logging.getLogger(__name__)

# Debounce interval (ms) to prevent spurious triggers
DEFAULT_DEBOUNCE_MS = 200


class GPIOAdapter(ChannelAdapter):
    """Channel adapter for GPIO pin I/O on embedded Linux boards.

    Input pins: trigger Message events on state change (HIGH→LOW or LOW→HIGH).
    Output pins: controlled via send_message (text = 'on'/'off'/'pwm:50').
    """

    def __init__(
        self,
        input_pins: List[int] = None,
        output_pins: List[int] = None,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        config: ChannelConfig = None,
    ):
        super().__init__(config or ChannelConfig())
        # Parse from env if not provided
        self._input_pins = input_pins or _parse_pin_list(
            os.environ.get('HEVOLVE_GPIO_INPUT_PINS', ''))
        self._output_pins = output_pins or _parse_pin_list(
            os.environ.get('HEVOLVE_GPIO_OUTPUT_PINS', ''))
        self._debounce_ms = debounce_ms
        self._gpio_lib = None  # 'gpiod' or 'rpigpio'
        self._gpio = None
        self._poll_thread = None
        self._pin_states = {}  # pin -> last known state
        self._pin_last_event = {}  # pin -> timestamp of last event

    @property
    def name(self) -> str:
        return 'gpio'

    @staticmethod
    def is_available() -> bool:
        """Check if GPIO hardware is available on this system."""
        try:
            import gpiod
            return True
        except ImportError:
            pass
        try:
            import RPi.GPIO
            return True
        except ImportError:
            pass
        return os.path.isdir('/sys/class/gpio')

    async def connect(self) -> bool:
        """Initialize GPIO library and configure pins."""
        # Try gpiod first (modern), then RPi.GPIO (legacy)
        try:
            import gpiod
            self._gpio_lib = 'gpiod'
            self._gpio = gpiod
            logger.info("GPIO adapter: using gpiod (modern Linux GPIO)")
        except ImportError:
            try:
                import RPi.GPIO as GPIO
                self._gpio_lib = 'rpigpio'
                self._gpio = GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                logger.info("GPIO adapter: using RPi.GPIO")
            except ImportError:
                logger.error("GPIO adapter: no GPIO library available")
                return False

        # Configure pins
        try:
            self._setup_pins()
        except Exception as e:
            logger.error(f"GPIO adapter: pin setup failed: {e}")
            return False

        # Start polling thread for input pins
        if self._input_pins:
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True)
            self._poll_thread.start()

        self.status = ChannelStatus.CONNECTED
        logger.info(
            f"GPIO adapter: inputs={self._input_pins}, "
            f"outputs={self._output_pins}"
        )
        return True

    async def disconnect(self) -> None:
        """Release GPIO resources."""
        self._running = False
        if self._gpio_lib == 'rpigpio' and self._gpio:
            try:
                self._gpio.cleanup()
            except Exception:
                pass

    async def send_message(
        self, chat_id: str, text: str,
        reply_to: Optional[str] = None,
        media: Optional[List] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Control an output pin.

        chat_id: pin number as string (e.g. "22")
        text: "on", "off", "toggle", or "pwm:0-100"
        """
        try:
            pin = int(chat_id)
        except ValueError:
            return SendResult(success=False, error=f"Invalid pin: {chat_id}")

        if pin not in self._output_pins:
            return SendResult(success=False, error=f"Pin {pin} not configured as output")

        cmd = text.strip().lower()
        try:
            if cmd == 'on':
                self._set_pin(pin, True)
            elif cmd == 'off':
                self._set_pin(pin, False)
            elif cmd == 'toggle':
                current = self._pin_states.get(pin, False)
                self._set_pin(pin, not current)
            elif cmd.startswith('pwm:'):
                duty = int(cmd.split(':')[1])
                self._set_pwm(pin, max(0, min(100, duty)))
            else:
                return SendResult(success=False, error=f"Unknown command: {cmd}")

            return SendResult(success=True, message_id=f"gpio_{pin}")
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def edit_message(self, chat_id: str, message_id: str,
                           text: str, buttons=None) -> SendResult:
        return await self.send_message(chat_id, text)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return False

    async def send_typing(self, chat_id: str) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        try:
            pin = int(chat_id)
            return {
                'pin': pin,
                'type': 'input' if pin in self._input_pins else 'output',
                'state': self._pin_states.get(pin),
            }
        except ValueError:
            return None

    # ─── Internal ───

    def _setup_pins(self):
        """Configure input and output pins."""
        if self._gpio_lib == 'rpigpio':
            for pin in self._input_pins:
                self._gpio.setup(pin, self._gpio.IN, pull_up_down=self._gpio.PUD_UP)
                self._pin_states[pin] = self._gpio.input(pin)
            for pin in self._output_pins:
                self._gpio.setup(pin, self._gpio.OUT, initial=self._gpio.LOW)
                self._pin_states[pin] = False
        else:
            # gpiod — just initialize state tracking
            for pin in self._input_pins:
                self._pin_states[pin] = None
            for pin in self._output_pins:
                self._pin_states[pin] = False

    def _set_pin(self, pin: int, high: bool):
        """Set an output pin high or low."""
        if self._gpio_lib == 'rpigpio':
            self._gpio.output(pin, self._gpio.HIGH if high else self._gpio.LOW)
        self._pin_states[pin] = high

    def _set_pwm(self, pin: int, duty: int):
        """Set PWM duty cycle (0-100) on a pin."""
        if self._gpio_lib == 'rpigpio':
            pwm = self._gpio.PWM(pin, 1000)  # 1kHz
            pwm.start(duty)
        self._pin_states[pin] = duty

    def _read_pin(self, pin: int) -> bool:
        """Read current state of an input pin."""
        if self._gpio_lib == 'rpigpio':
            return bool(self._gpio.input(pin))
        return self._pin_states.get(pin, False)

    def _poll_loop(self):
        """Poll input pins for state changes with debounce."""
        poll_ms = int(os.environ.get('HEVOLVE_GPIO_POLL_MS', '50'))
        while self._running:
            for pin in self._input_pins:
                try:
                    current = self._read_pin(pin)
                    previous = self._pin_states.get(pin)

                    if current != previous:
                        # Debounce check
                        now = time.time() * 1000
                        last = self._pin_last_event.get(pin, 0)
                        if now - last < self._debounce_ms:
                            continue

                        self._pin_states[pin] = current
                        self._pin_last_event[pin] = now
                        self._dispatch_gpio_event(pin, current)
                except Exception as e:
                    logger.debug(f"GPIO poll error pin {pin}: {e}")

            time.sleep(poll_ms / 1000.0)

    def _dispatch_gpio_event(self, pin: int, state: bool):
        """Create Message from GPIO state change and dispatch."""
        msg = Message(
            id=str(uuid.uuid4())[:8],
            channel='gpio',
            sender_id=f'gpio:{pin}',
            sender_name=f'GPIO pin {pin}',
            chat_id=str(pin),
            text=f'pin:{pin} state:{"HIGH" if state else "LOW"}',
            raw={'pin': pin, 'state': state, 'timestamp': time.time()},
        )

        for handler in self._message_handlers:
            try:
                handler(msg)
            except Exception as e:
                logger.error(f"GPIO handler error: {e}")


def _parse_pin_list(env_value: str) -> List[int]:
    """Parse comma-separated pin numbers from env var."""
    if not env_value:
        return []
    try:
        return [int(p.strip()) for p in env_value.split(',') if p.strip()]
    except ValueError:
        return []
