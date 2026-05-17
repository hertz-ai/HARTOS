"""Unified discovery — finds connected neuro / biometric devices across
every transport in one call.

scan() is the single entrypoint.  Each transport block is wrapped in
its own try/except so a missing optional dependency (bleak, pylsl,
pyserial) degrades to "skip this transport", never crashes discovery.

Each entry in the returned list is a (provider_id_or_none, info_dict)
tuple:
  - provider_id is the registry slug if the device matched a seeded
    entry by name or BLE service UUID;
  - None if discovery found an unknown device (still surfaced so a dev
    can decide to add a NeuroProvider entry for it).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from integrations.providers.neuro_providers import (
    NEURO_REGISTRY,
    NeuroProvider,
    Transport,
)

logger = logging.getLogger(__name__)


# ─── Matching helpers ─────────────────────────────────────────────────

def _match_ble_service(service_uuid: str) -> Optional[NeuroProvider]:
    if not service_uuid:
        return None
    target = service_uuid.lower()
    for p in NEURO_REGISTRY.values():
        if p.ble_service_uuid and p.ble_service_uuid.lower() == target:
            return p
    return None


def _match_name(name: str) -> Optional[NeuroProvider]:
    if not name:
        return None
    low = name.lower()
    for p in NEURO_REGISTRY.values():
        if p.id in low:
            return p
        first_word = p.name.lower().split()[0] if p.name else ''
        if first_word and first_word in low:
            return p
    return None


# ─── Per-transport scans (all fail-soft on missing deps) ──────────────

def _scan_ble(timeout_s: float = 4.0) -> List[Tuple[Optional[str], Dict[str, Any]]]:
    try:
        import asyncio
        from bleak import BleakScanner  # type: ignore
    except ImportError:
        logger.debug('bleak not installed — skipping BLE scan')
        return []

    async def _go():
        return await BleakScanner.discover(timeout=timeout_s)

    try:
        devices = asyncio.run(_go())
    except Exception as e:
        logger.debug(f'BLE scan failed: {e}')
        return []

    out: List[Tuple[Optional[str], Dict[str, Any]]] = []
    for d in devices:
        info = {
            'transport': Transport.BLE.value,
            'address': getattr(d, 'address', None),
            'name': getattr(d, 'name', None),
        }
        matched = _match_name(info['name'] or '')
        out.append((matched.id if matched else None, info))
    return out


def _scan_lsl(timeout_s: float = 2.0) -> List[Tuple[Optional[str], Dict[str, Any]]]:
    try:
        from pylsl import resolve_streams  # type: ignore
    except ImportError:
        logger.debug('pylsl not installed — skipping LSL scan')
        return []
    try:
        streams = resolve_streams(wait_time=timeout_s)
    except Exception as e:
        logger.debug(f'LSL resolve failed: {e}')
        return []

    out: List[Tuple[Optional[str], Dict[str, Any]]] = []
    for s in streams:
        try:
            info = {
                'transport': Transport.LSL.value,
                'name': s.name(),
                'type': s.type(),
                'channel_count': s.channel_count(),
                'sample_rate_hz': int(s.nominal_srate() or 0),
            }
        except Exception:
            continue
        matched = _match_name(info.get('name') or '')
        out.append((matched.id if matched else None, info))
    return out


def _scan_usb_serial() -> List[Tuple[Optional[str], Dict[str, Any]]]:
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError:
        logger.debug('pyserial not installed — skipping USB-serial scan')
        return []
    out: List[Tuple[Optional[str], Dict[str, Any]]] = []
    try:
        ports = list_ports.comports()
    except Exception as e:
        logger.debug(f'USB-serial scan failed: {e}')
        return []
    for port in ports:
        info = {
            'transport': Transport.USB_SERIAL.value,
            'device': port.device,
            'description': port.description,
            'vid': getattr(port, 'vid', None),
            'pid': getattr(port, 'pid', None),
        }
        matched = _match_name(info.get('description') or '')
        out.append((matched.id if matched else None, info))
    return out


# ─── Public entrypoint ────────────────────────────────────────────────

def scan(
    include_ble: bool = True,
    include_lsl: bool = True,
    include_usb: bool = True,
    ble_timeout_s: float = 4.0,
) -> List[Tuple[Optional[str], Dict[str, Any]]]:
    """Aggregate discovery across every enabled transport.  Fail-soft:
    missing optional deps (bleak, pylsl, pyserial) log-debug and
    return [] for that transport rather than crashing the scan.
    """
    found: List[Tuple[Optional[str], Dict[str, Any]]] = []
    if include_ble:
        found.extend(_scan_ble(timeout_s=ble_timeout_s))
    if include_lsl:
        found.extend(_scan_lsl())
    if include_usb:
        found.extend(_scan_usb_serial())
    return found


def known_providers_summary() -> List[Dict[str, Any]]:
    """Static overview of the full registry — no hardware touched.

    Admin UI renders this alongside scan() results so users see
    "what's theoretically supported" and "what's connected right now"
    side by side.
    """
    out: List[Dict[str, Any]] = []
    for p in NEURO_REGISTRY.values():
        out.append({
            'id': p.id,
            'name': p.name,
            'form_factor': p.form_factor.value,
            'status': p.status.value,
            'signals': [s.value for s in p.signals],
            'transports': [t.value for t in p.transport],
            'sample_rate_hz': p.sample_rate_hz,
            'channels': p.channels,
            'docs_url': p.docs_url,
            'website': p.website,
            'contact_email': p.contact_email,
            'notes': p.notes,
        })
    return out
