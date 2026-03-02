"""
Base class for peripheral forwarding backends.

Each backend handles a specific peripheral type (USB, Bluetooth, Gamepad).
Backends detect available peripherals, forward them over HARTOS transport,
and handle cleanup on disconnect.
"""

from abc import ABC, abstractmethod
from typing import List

from integrations.remote_desktop.peripheral_bridge import PeripheralInfo


class PeripheralBackend(ABC):
    """Abstract base for peripheral forwarding backends."""

    @abstractmethod
    def discover(self) -> List[PeripheralInfo]:
        """Discover connected peripherals of this type."""
        ...

    @abstractmethod
    def forward(self, peripheral: PeripheralInfo, transport) -> bool:
        """Start forwarding a peripheral over the transport.

        Args:
            peripheral: The peripheral to forward.
            transport: TransportChannel to send events on.

        Returns:
            True if forwarding started successfully.
        """
        ...

    @abstractmethod
    def stop(self, peripheral_id: str) -> bool:
        """Stop forwarding a specific peripheral."""
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this backend's system dependencies are available."""
        ...

    @property
    @abstractmethod
    def peripheral_type_name(self) -> str:
        """The peripheral type this backend handles (e.g., 'usb', 'bluetooth')."""
        ...
