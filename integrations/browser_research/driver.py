"""Browser Research — driver abstraction.

Two modes, one interface:
  B2 (default) — attach to user's running Chrome via CDP at port 9222.
                 Zero re-auth burden; agent literally drives a tab in the
                 user's logged-in session.  "Like a human" — IS the human's
                 session.
  B1 (fallback) — Obscura launches its own stealth Chrome profile under
                  <data_dir>/browser_profile/.  Used when B2's CDP endpoint
                  is unreachable.

Lazy import: Obscura/Playwright bindings are NOT imported at module load.
A T3 tool (YouTube, web fetch) never touches this module's get_driver(); only
T2 tools (C4+) call into it.

C1 ships the interface + B2 probe only.  Actual Obscura subprocess launch
lands in C4 when the first T2 platform script needs it.
"""
import logging
import os
import socket
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger('browser_research.driver')

# B2 CDP attach: standard Chrome remote-debugging port.
CDP_PORT_DEFAULT = 9222
CDP_HOST_DEFAULT = '127.0.0.1'
CDP_PROBE_TIMEOUT_S = 1.5


def cdp_endpoint_reachable(host: str = CDP_HOST_DEFAULT, port: int = CDP_PORT_DEFAULT) -> bool:
    """Cheap TCP probe — is there a Chrome listening on the CDP port?

    Used to decide B2 vs B1 at driver init.  Does NOT speak CDP — just opens
    a TCP socket and closes it.  Sub-2-second timeout so it never blocks.
    """
    try:
        with socket.create_connection((host, port), timeout=CDP_PROBE_TIMEOUT_S):
            return True
    except (OSError, socket.timeout):
        return False


class BrowserDriver(ABC):
    """Abstract driver.  Concrete: ObscuraB2Driver (CDP), ObscuraB1Driver (headless)."""

    connection_mechanism: str = 'unknown'

    @abstractmethod
    def goto(self, url: str) -> None: ...

    @abstractmethod
    def evaluate(self, script: str) -> object: ...

    @abstractmethod
    def close(self) -> None: ...


class ObscuraB2Driver(BrowserDriver):
    """Attach to user's running Chrome via CDP at port 9222.

    Stub in C1 — concrete CDP-client wiring lands in C4 when first needed.
    """
    connection_mechanism = 'obscura_b2_cdp_user_chrome'

    def __init__(self, host: str = CDP_HOST_DEFAULT, port: int = CDP_PORT_DEFAULT) -> None:
        self.host = host
        self.port = port
        # Concrete obscura/CDP client init lands in C4.

    def goto(self, url: str) -> None:
        raise NotImplementedError('ObscuraB2Driver lands in C4 (first T2 platform).')

    def evaluate(self, script: str) -> object:
        raise NotImplementedError('ObscuraB2Driver lands in C4 (first T2 platform).')

    def close(self) -> None:
        pass


class ObscuraB1Driver(BrowserDriver):
    """Launch a Nunba-managed Obscura headless profile.

    Stub in C1 — concrete subprocess + profile dir lands in C4.
    """
    connection_mechanism = 'obscura_b1_headless_profile'

    def __init__(self, profile_dir: Optional[str] = None) -> None:
        if profile_dir is None:
            try:
                from core.platform_paths import get_data_dir
                profile_dir = os.path.join(get_data_dir(), 'browser_profile')
            except Exception:
                profile_dir = os.path.join(os.getcwd(), '.browser_profile')
        self.profile_dir = profile_dir

    def goto(self, url: str) -> None:
        raise NotImplementedError('ObscuraB1Driver lands in C4 (first T2 platform).')

    def evaluate(self, script: str) -> object:
        raise NotImplementedError('ObscuraB1Driver lands in C4 (first T2 platform).')

    def close(self) -> None:
        pass


def get_driver(mode: str = 'auto') -> BrowserDriver:
    """Resolve the right driver for current host state.

    mode='auto' (default): try B2 (attach to user Chrome); fall back to B1.
    mode='b2': force B2 (raises if no CDP endpoint).
    mode='b1': force B1 (Obscura headless).
    """
    if mode == 'b2':
        if not cdp_endpoint_reachable():
            raise RuntimeError(
                'B2 driver requested but no Chrome listening on CDP port. '
                'Start Chrome with --remote-debugging-port=9222 or use mode="b1".'
            )
        return ObscuraB2Driver()
    if mode == 'b1':
        return ObscuraB1Driver()
    if mode == 'auto':
        if cdp_endpoint_reachable():
            logger.debug('B2 reachable, using ObscuraB2Driver')
            return ObscuraB2Driver()
        logger.debug('B2 unreachable, falling back to ObscuraB1Driver')
        return ObscuraB1Driver()
    raise ValueError(f'unknown driver mode: {mode!r}')
