"""Adapter protocol for neuro / biometric providers.

One abstract base (`NeuroAdapter`), one factory (`choose_adapter`) that
routes by the provider's primary transport, concrete adapter stubs per
transport that raise a clear "install <sdk> and implement X" error when
the vendor SDK isn't present.

Why stubs instead of full implementations:

  - Each vendor's wire protocol is different; shipping concrete clients
    for all of them would pull in ~10 optional dependency chains.
  - The stub error message includes the exact pip package name and docs
    URL so a developer can go from "I want to talk to this device" to
    working code in one sitting.
  - Contact-gated providers (Yneuro) get a separate ContactGatedAdapter
    that raises PermissionError with the vendor's contact email — clear
    that the block is at the business layer, not the wire.

Cross-network sensor reads reuse PeerLink's existing `sensor` channel
(core/peer_link/channels.py) — E2E-encrypted.  Zero new wire protocol.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional

from integrations.providers.neuro_providers import (
    NeuroProvider,
    ProviderStatus,
    SignalType,
    Transport,
)

logger = logging.getLogger(__name__)


# ─── Reading shape ────────────────────────────────────────────────────

@dataclass
class Reading:
    """One chunk of sensor output from a connected provider."""
    provider_id: str
    signal: SignalType
    data: Any                   # numpy array / dict / list — transport-dependent
    sample_rate_hz: int
    timestamp_start: float
    duration_s: float
    metadata: Dict[str, Any] = None  # channel names, units, etc.


# ─── Abstract base ────────────────────────────────────────────────────

class NeuroAdapter(ABC):
    """Every concrete adapter implements these three methods."""

    def __init__(self, provider: NeuroProvider):
        self.provider = provider
        self._connected = False

    @abstractmethod
    def connect(self, **kwargs) -> bool:
        """Open the underlying transport.  Returns True on success.
        May raise NotImplementedError (SDK missing) or PermissionError
        (contact-gated provider)."""

    @abstractmethod
    def read_signal(
        self, signal: SignalType, duration_s: float = 1.0,
    ) -> Reading:
        """Read `duration_s` seconds of the requested signal."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the transport cleanly."""

    def is_connected(self) -> bool:
        return self._connected


# ─── Concrete / stub adapters ─────────────────────────────────────────

class _SDKUnavailable(NeuroAdapter):
    """Base stub — every transport-specific stub overrides _install_hint.
    Raises NotImplementedError on connect so the dev knows exactly
    which pip package to install and where the vendor docs live."""

    _install_hint = 'No adapter registered for this provider/transport.'

    def connect(self, **kwargs) -> bool:
        raise NotImplementedError(
            f'{self.provider.name}: {self._install_hint}  '
            f'Docs: {self.provider.docs_url or self.provider.website}.  '
            f'Implement a NeuroAdapter subclass in '
            f'integrations/providers/neuro_adapter.py and register it '
            f'in _ADAPTER_BY_TRANSPORT.'
        )

    def read_signal(self, signal, duration_s=1.0):
        raise NotImplementedError('connect() first')

    def disconnect(self) -> None:
        self._connected = False


class ContactGatedAdapter(NeuroAdapter):
    """For providers whose SDK/API is not public (e.g. Yneuro).
    Raises PermissionError with the vendor's contact email so the
    developer reaches out instead of assuming an integration exists."""

    def connect(self, **kwargs) -> bool:
        contact = self.provider.contact_email or self.provider.website
        raise PermissionError(
            f'{self.provider.name} does not publish a public SDK '
            f'or API.  Contact {contact} for developer access.  '
            f'Once you receive credentials, swap this adapter for a '
            f'concrete one — registry entry is already wired.'
        )

    def read_signal(self, signal, duration_s=1.0):
        raise PermissionError('provider contact-gated — connect() first')

    def disconnect(self) -> None:
        self._connected = False


class BLEAdapter(_SDKUnavailable):
    _install_hint = (
        'BLE transport requires `bleak` (pip install bleak) plus the '
        'vendor SDK — `muselsl` for Muse, `brainflow` for OpenBCI.'
    )


class RESTAdapter(_SDKUnavailable):
    _install_hint = (
        'REST transport needs the vendor OAuth flow.  Implement a '
        'NeuroAdapter subclass that calls requests.get() against the '
        'documented base URL and caches the access token.'
    )


class WebSocketAdapter(_SDKUnavailable):
    _install_hint = (
        'WebSocket transport requires `websockets` (pip install '
        'websockets) plus the vendor protocol — Emotiv Cortex, '
        'Neurosity subscription topics, etc.'
    )


class LSLAdapter(_SDKUnavailable):
    _install_hint = (
        'LSL (Lab Streaming Layer) is the de-facto neuro transport '
        'standard.  Install `pylsl` (pip install pylsl) and resolve a '
        'stream by type ("EEG", "PPG", ...) — works for every headset '
        'that exposes LSL (Muse via muselsl, OpenBCI, Emotiv via '
        'LSL-Apps).'
    )


class USBSerialAdapter(_SDKUnavailable):
    _install_hint = (
        'USB-serial transport requires `pyserial` (pip install '
        'pyserial) plus the vendor framing — OpenBCI Cyton uses a '
        '33-byte packet format.  `brainflow` wraps this if you want '
        'to skip the framing work.'
    )


class USBHIDAdapter(_SDKUnavailable):
    _install_hint = (
        'USB-HID transport requires `hidapi` (pip install hidapi).  '
        'Match by vendor_id/product_id; each vendor has a different '
        'report descriptor.'
    )


# ─── Factory ──────────────────────────────────────────────────────────

_ADAPTER_BY_TRANSPORT = {
    Transport.BLE: BLEAdapter,
    Transport.REST: RESTAdapter,
    Transport.WEBSOCKET: WebSocketAdapter,
    Transport.LSL: LSLAdapter,
    Transport.USB_SERIAL: USBSerialAdapter,
    Transport.USB_HID: USBHIDAdapter,
    Transport.UNKNOWN: _SDKUnavailable,
}


def choose_adapter(provider: NeuroProvider) -> NeuroAdapter:
    """Return the best adapter instance for `provider`.

    Contact-gated providers always get ContactGatedAdapter regardless
    of transport — the block is at the business layer, not the wire.
    Other providers map by their primary (first) transport entry.
    """
    if provider.status == ProviderStatus.CONTACT_REQUIRED:
        return ContactGatedAdapter(provider)

    primary = provider.transport[0] if provider.transport else Transport.UNKNOWN
    adapter_cls = _ADAPTER_BY_TRANSPORT.get(primary, _SDKUnavailable)
    return adapter_cls(provider)


def register_adapter(transport: Transport, adapter_cls) -> None:
    """Override the adapter class for a transport.  Call this from a
    concrete implementation module once you've written a real adapter
    (e.g. in integrations/providers/muse_adapter.py) so `choose_adapter`
    picks it up automatically."""
    if not issubclass(adapter_cls, NeuroAdapter):
        raise TypeError('adapter_cls must subclass NeuroAdapter')
    _ADAPTER_BY_TRANSPORT[transport] = adapter_cls
