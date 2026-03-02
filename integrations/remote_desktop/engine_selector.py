"""
Engine Selector — Auto-selects RustDesk or Sunshine/Moonlight based on context.

Selection logic:
  - File transfer needed → RustDesk (Sunshine has no file transfer)
  - VLM agent / high-FPS needed → Sunshine+Moonlight (hardware encoding, <10ms)
  - Gaming → Sunshine+Moonlight
  - General remote support → RustDesk (full-featured AnyDesk replacement)
  - Fallback → HARTOS native (frame_capture + transport) when neither installed

Also provides unified status across all engines.
"""

import logging
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


class Engine(Enum):
    RUSTDESK = 'rustdesk'
    SUNSHINE = 'sunshine'      # Host side
    MOONLIGHT = 'moonlight'    # Viewer side
    NATIVE = 'native'          # HARTOS built-in fallback


class UseCase(Enum):
    REMOTE_SUPPORT = 'remote_support'
    FILE_TRANSFER = 'file_transfer'
    VLM_COMPUTER_USE = 'vlm_computer_use'
    GAMING = 'gaming'
    GENERAL = 'general'


# ── Engine availability cache ───────────────────────────────────

_availability_cache: Optional[Dict[str, bool]] = None


def _detect_engines() -> Dict[str, bool]:
    """Detect which remote desktop engines are installed."""
    global _availability_cache
    if _availability_cache is not None:
        return _availability_cache

    result = {}

    try:
        from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
        result['rustdesk'] = get_rustdesk_bridge().available
    except Exception:
        result['rustdesk'] = False

    try:
        from integrations.remote_desktop.sunshine_bridge import (
            get_sunshine_bridge, get_moonlight_bridge,
        )
        result['sunshine'] = get_sunshine_bridge().available
        result['moonlight'] = get_moonlight_bridge().available
    except Exception:
        result['sunshine'] = False
        result['moonlight'] = False

    # Native fallback is always available
    result['native'] = True

    _availability_cache = result
    logger.info(f"Remote desktop engines: {result}")
    return result


def reset_cache() -> None:
    """Reset engine detection cache (e.g., after install)."""
    global _availability_cache
    _availability_cache = None


def select_engine(use_case: UseCase = UseCase.GENERAL,
                  role: str = 'viewer',
                  prefer: Optional[Engine] = None) -> Engine:
    """Select the best remote desktop engine for the given context.

    Args:
        use_case: What the remote desktop will be used for
        role: 'host' (sharing screen) or 'viewer' (connecting)
        prefer: User preference override

    Returns:
        Best available Engine for the context.
    """
    engines = _detect_engines()

    # User preference takes priority
    if prefer:
        if prefer == Engine.RUSTDESK and engines.get('rustdesk'):
            return Engine.RUSTDESK
        if prefer == Engine.SUNSHINE and engines.get('sunshine'):
            return Engine.SUNSHINE
        if prefer == Engine.MOONLIGHT and engines.get('moonlight'):
            return Engine.MOONLIGHT

    # Use-case-based selection
    if use_case == UseCase.FILE_TRANSFER:
        # RustDesk has file transfer; Sunshine does not
        if engines.get('rustdesk'):
            return Engine.RUSTDESK
        return Engine.NATIVE

    if use_case in (UseCase.VLM_COMPUTER_USE, UseCase.GAMING):
        # Sunshine+Moonlight have best streaming quality
        if role == 'host' and engines.get('sunshine'):
            return Engine.SUNSHINE
        if role == 'viewer' and engines.get('moonlight'):
            return Engine.MOONLIGHT
        # Fall back to RustDesk
        if engines.get('rustdesk'):
            return Engine.RUSTDESK
        return Engine.NATIVE

    if use_case == UseCase.REMOTE_SUPPORT:
        # RustDesk is the full AnyDesk replacement
        if engines.get('rustdesk'):
            return Engine.RUSTDESK
        return Engine.NATIVE

    # General: prefer RustDesk (most complete)
    if engines.get('rustdesk'):
        return Engine.RUSTDESK
    # Then Sunshine/Moonlight
    if role == 'host' and engines.get('sunshine'):
        return Engine.SUNSHINE
    if role == 'viewer' and engines.get('moonlight'):
        return Engine.MOONLIGHT

    return Engine.NATIVE


def get_all_status() -> dict:
    """Get status of all remote desktop engines."""
    status = {'engines': {}}

    try:
        from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
        status['engines']['rustdesk'] = get_rustdesk_bridge().get_status()
    except Exception as e:
        status['engines']['rustdesk'] = {'available': False, 'error': str(e)}

    try:
        from integrations.remote_desktop.sunshine_bridge import (
            get_sunshine_bridge, get_moonlight_bridge,
        )
        status['engines']['sunshine'] = get_sunshine_bridge().get_status()
        status['engines']['moonlight'] = get_moonlight_bridge().get_status()
    except Exception as e:
        status['engines']['sunshine'] = {'available': False, 'error': str(e)}
        status['engines']['moonlight'] = {'available': False, 'error': str(e)}

    status['engines']['native'] = {
        'available': True,
        'engine': 'native',
        'description': 'HARTOS built-in (frame_capture + transport)',
    }

    # Recommend engines based on availability
    engines = _detect_engines()
    recommendations = []
    if not engines.get('rustdesk'):
        try:
            from integrations.remote_desktop.rustdesk_bridge import RustDeskBridge
            recommendations.append({
                'engine': 'rustdesk',
                'reason': 'General remote desktop (AnyDesk replacement)',
                'install': RustDeskBridge().get_install_command(),
            })
        except Exception:
            pass
    if not engines.get('sunshine'):
        try:
            from integrations.remote_desktop.sunshine_bridge import SunshineBridge
            recommendations.append({
                'engine': 'sunshine',
                'reason': 'High-fidelity streaming (gaming, VLM, creative)',
                'install': SunshineBridge().get_install_command(),
            })
        except Exception:
            pass
    if not engines.get('moonlight'):
        try:
            from integrations.remote_desktop.sunshine_bridge import MoonlightBridge
            recommendations.append({
                'engine': 'moonlight',
                'reason': 'Viewer for Sunshine streams (4K@120fps)',
                'install': MoonlightBridge().get_install_command(),
            })
        except Exception:
            pass

    status['install_recommendations'] = recommendations
    return status


def get_available_engines() -> List[Engine]:
    """Get list of available engines."""
    engines = _detect_engines()
    result = []
    if engines.get('rustdesk'):
        result.append(Engine.RUSTDESK)
    if engines.get('sunshine'):
        result.append(Engine.SUNSHINE)
    if engines.get('moonlight'):
        result.append(Engine.MOONLIGHT)
    result.append(Engine.NATIVE)
    return result
