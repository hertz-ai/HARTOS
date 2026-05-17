"""Neuro / biometric provider registry — one source of truth for every
brainwave, EEG, EMG, HRV, and wearable device HARTOS can talk to.

Kept distinct from providers/registry.py (compute APIs: OpenAI, Replicate)
because the axes are different.  A Muse headset doesn't have
`context_length` or `pricing_per_1k_tokens`; it has form factor,
transport, signal types, sample rate, channel count.  Forcing hardware
into the compute-API dataclass would have made a parallel path.

This registry + neuro_adapter.py + neuro_discovery.py together are the
surface Nunba uses so developers can plug an agent into biometric
hardware without re-implementing discovery per-vendor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ─── Enums ────────────────────────────────────────────────────────────

class FormFactor(str, Enum):
    HEADSET_EEG = 'headset_eeg'
    EARPIECE_EEG = 'earpiece_eeg'
    WRISTBAND_PPG = 'wristband_ppg'
    RING_PPG = 'ring_ppg'
    CAP_EEG = 'cap_eeg'
    IMPLANT_ECOG = 'implant_ecog'
    GLASSES_EOG = 'glasses_eog'
    CHEST_HRV = 'chest_hrv'
    UNKNOWN = 'unknown'


class Transport(str, Enum):
    BLE = 'ble'
    USB_HID = 'usb_hid'
    USB_SERIAL = 'usb_serial'
    WEBSOCKET = 'websocket'
    REST = 'rest'
    LSL = 'lsl'  # Lab Streaming Layer — de-facto neuro transport standard
    UNKNOWN = 'unknown'


class SignalType(str, Enum):
    EEG = 'eeg'
    EMG = 'emg'
    ECG = 'ecg'
    PPG = 'ppg'              # photoplethysmography -> heart rate / HRV
    EOG = 'eog'              # eye movement
    ACC = 'acc'              # accelerometer
    GYRO = 'gyro'
    HRV = 'hrv'
    SLEEP = 'sleep'
    TEMPERATURE = 'temperature'
    BRAINWAVE_FINGERPRINT = 'brainwave_fingerprint'


class ProviderStatus(str, Enum):
    PUBLIC_SDK = 'public_sdk'              # docs + pypi/npm package available
    CONTACT_REQUIRED = 'contact_required'  # e.g. Yneuro — email them
    LICENSE_GATED = 'license_gated'        # SDK exists but needs paid license
    INVITE_ONLY = 'invite_only'            # closed beta
    DISCONTINUED = 'discontinued'
    RESEARCH_ONLY = 'research_only'


# ─── Dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NeuroProvider:
    """Static specification of a neural / biometric provider."""
    id: str                          # slug: 'muse', 'yneuro', 'neurosity'
    name: str                        # display name
    form_factor: FormFactor
    transport: Tuple[Transport, ...]  # primary first — fallbacks after
    signals: Tuple[SignalType, ...]
    status: ProviderStatus
    sample_rate_hz: int = 0          # 0 = unknown or variable
    channels: int = 0                # EEG channel count, where applicable
    sdk_python: str = ''             # pip package name
    sdk_node: str = ''               # npm package name
    docs_url: str = ''
    website: str = ''
    contact_email: str = ''          # populated when status=CONTACT_REQUIRED
    ble_service_uuid: str = ''       # BLE service UUID, for discovery matching
    notes: str = ''


# ─── Seeded registry ──────────────────────────────────────────────────

NEURO_REGISTRY: Dict[str, NeuroProvider] = {
    # ── Yneuro — contact-gated brainwave authentication ──────────────
    # Intentionally first so it's visible as the headline provider the
    # registry was built around.  No public SDK as of 2026-04; email
    # them for access.  The adapter surface is already wired so the
    # day they reply, implementing the client is a ~30 LOC drop-in.
    'yneuro': NeuroProvider(
        id='yneuro',
        name='Yneuro (Neuro ID)',
        form_factor=FormFactor.UNKNOWN,
        transport=(Transport.UNKNOWN,),
        signals=(SignalType.BRAINWAVE_FINGERPRINT,),
        status=ProviderStatus.CONTACT_REQUIRED,
        website='https://www.yneuro.com/',
        contact_email='hello@yneuro.com',
        notes=(
            'Brainwave-signature authentication.  No public SDK or '
            'API documented as of 2026-04.  Email for developer '
            'access; the ContactGatedAdapter raises a clear '
            'PermissionError pointing there until credentials land.'
        ),
    ),

    # ── Muse (Choose Muse) — BLE headset EEG, public ─────────────────
    'muse': NeuroProvider(
        id='muse',
        name='Muse (2 / S)',
        form_factor=FormFactor.HEADSET_EEG,
        transport=(Transport.BLE, Transport.LSL),
        signals=(SignalType.EEG, SignalType.PPG, SignalType.ACC),
        status=ProviderStatus.PUBLIC_SDK,
        sample_rate_hz=256,
        channels=4,
        sdk_python='muselsl',
        docs_url='https://github.com/alexandrebarachant/muse-lsl',
        website='https://choosemuse.com/',
        ble_service_uuid='0000fe8d-0000-1000-8000-00805f9b34fb',
    ),

    # ── Neurosity Crown — REST + WebSocket ──────────────────────────
    'neurosity': NeuroProvider(
        id='neurosity',
        name='Neurosity Crown',
        form_factor=FormFactor.HEADSET_EEG,
        transport=(Transport.WEBSOCKET, Transport.REST),
        signals=(SignalType.EEG,),
        status=ProviderStatus.PUBLIC_SDK,
        sample_rate_hz=256,
        channels=8,
        sdk_python='neurosity',
        sdk_node='@neurosity/sdk',
        docs_url='https://docs.neurosity.co/',
        website='https://neurosity.co/',
    ),

    # ── OpenBCI — open hardware (Cyton / Ganglion) via BrainFlow ────
    'openbci': NeuroProvider(
        id='openbci',
        name='OpenBCI (Cyton / Ganglion)',
        form_factor=FormFactor.CAP_EEG,
        transport=(Transport.USB_SERIAL, Transport.BLE, Transport.LSL),
        signals=(SignalType.EEG, SignalType.EMG, SignalType.ECG),
        status=ProviderStatus.PUBLIC_SDK,
        sample_rate_hz=250,
        channels=8,
        sdk_python='brainflow',
        docs_url='https://brainflow.readthedocs.io/',
        website='https://openbci.com/',
    ),

    # ── Emotiv (EPOC / Insight) — Cortex WebSocket API ──────────────
    'emotiv': NeuroProvider(
        id='emotiv',
        name='Emotiv (EPOC / Insight)',
        form_factor=FormFactor.HEADSET_EEG,
        transport=(Transport.WEBSOCKET,),
        signals=(SignalType.EEG,),
        status=ProviderStatus.LICENSE_GATED,
        sample_rate_hz=256,
        channels=14,
        sdk_python='cortex-v2-example',
        docs_url='https://emotiv.gitbook.io/cortex-api/',
        website='https://www.emotiv.com/',
        notes='Requires an EMOTIV license for raw EEG stream access.',
    ),

    # ── NextSense — earpiece EEG, invite-only beta ──────────────────
    'nextsense': NeuroProvider(
        id='nextsense',
        name='NextSense',
        form_factor=FormFactor.EARPIECE_EEG,
        transport=(Transport.BLE,),
        signals=(SignalType.EEG,),
        status=ProviderStatus.INVITE_ONLY,
        website='https://nextsense.io/',
    ),

    # ── Whoop — wristband, HRV + sleep via REST OAuth ───────────────
    'whoop': NeuroProvider(
        id='whoop',
        name='Whoop',
        form_factor=FormFactor.WRISTBAND_PPG,
        transport=(Transport.REST,),
        signals=(SignalType.HRV, SignalType.SLEEP, SignalType.PPG),
        status=ProviderStatus.PUBLIC_SDK,
        docs_url='https://developer.whoop.com/',
        website='https://www.whoop.com/',
    ),

    # ── Oura — ring, HRV + sleep + temperature via REST OAuth ───────
    'oura': NeuroProvider(
        id='oura',
        name='Oura',
        form_factor=FormFactor.RING_PPG,
        transport=(Transport.REST,),
        signals=(
            SignalType.HRV, SignalType.SLEEP,
            SignalType.PPG, SignalType.TEMPERATURE,
        ),
        status=ProviderStatus.PUBLIC_SDK,
        docs_url='https://cloud.ouraring.com/v2/docs',
        website='https://ouraring.com/',
    ),
}


# ─── Public API ───────────────────────────────────────────────────────

def get_provider(provider_id: str) -> Optional[NeuroProvider]:
    return NEURO_REGISTRY.get(provider_id)


def list_providers(
    form_factor: Optional[FormFactor] = None,
    status: Optional[ProviderStatus] = None,
    signal: Optional[SignalType] = None,
    transport: Optional[Transport] = None,
) -> List[NeuroProvider]:
    """Filtered view of the registry — any combination of filters."""
    out = list(NEURO_REGISTRY.values())
    if form_factor is not None:
        out = [p for p in out if p.form_factor == form_factor]
    if status is not None:
        out = [p for p in out if p.status == status]
    if signal is not None:
        out = [p for p in out if signal in p.signals]
    if transport is not None:
        out = [p for p in out if transport in p.transport]
    return out
