"""
Serial Channel Adapter — PySerial-based UART/USB-to-serial communication.

Bridges serial devices (Arduino, microcontrollers, sensors) to HART agents.
Supports text_line (default), json_line, and binary_frame protocols.

Usage:
    from integrations.channels.hardware.serial_adapter import SerialAdapter
    adapter = SerialAdapter(port='/dev/ttyUSB0', baud_rate=115200)
    adapter.on_message(handler)
    await adapter.start()
"""
import asyncio
import json
import logging
import os
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Any

from integrations.channels.base import (
    ChannelAdapter, ChannelConfig, ChannelStatus,
    Message, SendResult, MessageType,
)

logger = logging.getLogger(__name__)

# Frame protocol constants
STX = 0x02  # Start of text
ETX = 0x03  # End of text


class SerialAdapter(ChannelAdapter):
    """Channel adapter for serial port communication.

    Protocols:
        text_line: Newline-delimited text (default, Arduino Serial.println)
        json_line: Newline-delimited JSON objects
        binary_frame: STX + length(2) + payload + ETX framing
    """

    def __init__(
        self,
        port: str = '',
        baud_rate: int = 115200,
        protocol: str = 'text_line',
        encoding: str = 'utf-8',
        reconnect_interval: int = 5,
        config: ChannelConfig = None,
    ):
        super().__init__(config or ChannelConfig())
        self._port = port or os.environ.get('HEVOLVE_SERIAL_PORT', '')
        self._baud_rate = int(os.environ.get('HEVOLVE_SERIAL_BAUD', str(baud_rate)))
        self._protocol = os.environ.get('HEVOLVE_SERIAL_PROTOCOL', protocol)
        self._encoding = encoding
        self._reconnect_interval = reconnect_interval
        self._serial = None
        self._read_thread = None
        self._executor = ThreadPoolExecutor(max_workers=1)

    @property
    def name(self) -> str:
        return 'serial'

    async def connect(self) -> bool:
        """Open serial port connection."""
        try:
            import serial
        except ImportError:
            logger.error("Serial adapter: pyserial not installed")
            return False

        if not self._port:
            # Auto-detect first available port
            from serial.tools import list_ports
            ports = list(list_ports.comports())
            if not ports:
                logger.error("Serial adapter: no ports found")
                return False
            self._port = ports[0].device
            logger.info(f"Serial adapter: auto-detected port {self._port}")

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                timeout=1.0,
            )
            logger.info(f"Serial adapter: connected to {self._port} @ {self._baud_rate}")
            self.status = ChannelStatus.CONNECTED

            # Start read thread
            self._read_thread = threading.Thread(
                target=self._read_loop, daemon=True)
            self._read_thread.start()
            return True
        except Exception as e:
            logger.error(f"Serial adapter: connect failed: {e}")
            self.status = ChannelStatus.ERROR
            return False

    async def disconnect(self) -> None:
        """Close serial port."""
        self._running = False
        if self._serial and self._serial.is_open:
            self._serial.close()
        self._executor.shutdown(wait=False)

    async def send_message(
        self, chat_id: str, text: str,
        reply_to: Optional[str] = None,
        media: Optional[List] = None,
        buttons: Optional[List[Dict]] = None,
    ) -> SendResult:
        """Write text to serial port."""
        if not self._serial or not self._serial.is_open:
            return SendResult(success=False, error="Serial port not open")

        try:
            if self._protocol == 'json_line':
                data = json.dumps({'text': text, 'chat_id': chat_id}) + '\n'
                self._serial.write(data.encode(self._encoding))
            elif self._protocol == 'binary_frame':
                payload = text.encode(self._encoding)
                frame = struct.pack('>BH', STX, len(payload)) + payload + bytes([ETX])
                self._serial.write(frame)
            else:  # text_line
                self._serial.write((text + '\n').encode(self._encoding))

            return SendResult(success=True, message_id=str(uuid.uuid4())[:8])
        except Exception as e:
            logger.error(f"Serial write failed: {e}")
            return SendResult(success=False, error=str(e))

    async def edit_message(self, chat_id: str, message_id: str,
                           text: str, buttons=None) -> SendResult:
        """Serial is write-only stream — edit not supported."""
        return await self.send_message(chat_id, text)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return False  # Not applicable

    async def send_typing(self, chat_id: str) -> None:
        pass  # Not applicable

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        return {'port': self._port, 'baud_rate': self._baud_rate}

    def _read_loop(self):
        """Background thread: read from serial port and dispatch messages."""
        buffer = b''
        while self._running and self._serial and self._serial.is_open:
            try:
                data = self._serial.read(self._serial.in_waiting or 1)
                if not data:
                    continue

                if self._protocol == 'binary_frame':
                    buffer += data
                    while len(buffer) >= 4:  # STX + 2-byte len + ETX minimum
                        if buffer[0] != STX:
                            buffer = buffer[1:]
                            continue
                        payload_len = struct.unpack('>H', buffer[1:3])[0]
                        frame_len = 3 + payload_len + 1
                        if len(buffer) < frame_len:
                            break
                        if buffer[frame_len - 1] != ETX:
                            buffer = buffer[1:]
                            continue
                        payload = buffer[3:3 + payload_len]
                        buffer = buffer[frame_len:]
                        self._dispatch_serial_message(
                            payload.decode(self._encoding, errors='replace'))
                else:
                    # Line-based protocols
                    buffer += data
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        text = line.decode(self._encoding, errors='replace').strip()
                        if text:
                            self._dispatch_serial_message(text)

            except Exception as e:
                if self._running:
                    logger.warning(f"Serial read error: {e}")
                    time.sleep(self._reconnect_interval)
                    self._try_reconnect()

    def _dispatch_serial_message(self, text: str):
        """Create a Message and dispatch to handlers."""
        if self._protocol == 'json_line':
            try:
                data = json.loads(text)
                text = data.get('text', text)
            except json.JSONDecodeError:
                pass

        msg = Message(
            id=str(uuid.uuid4())[:8],
            channel='serial',
            sender_id=self._port,
            sender_name=f'serial:{self._port}',
            chat_id=self._port,
            text=text,
        )

        # Dispatch in event loop if available, else synchronously
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._dispatch_message(msg), loop)
            else:
                loop.run_until_complete(self._dispatch_message(msg))
        except RuntimeError:
            # No event loop — call handlers synchronously
            for handler in self._message_handlers:
                try:
                    handler(msg)
                except Exception as e:
                    logger.error(f"Serial handler error: {e}")

    def _try_reconnect(self):
        """Attempt to reconnect to serial port after disconnect."""
        try:
            import serial
            if self._serial:
                self._serial.close()
            self._serial = serial.Serial(
                port=self._port, baudrate=self._baud_rate, timeout=1.0)
            logger.info(f"Serial adapter: reconnected to {self._port}")
        except Exception as e:
            logger.debug(f"Serial reconnect failed: {e}")
