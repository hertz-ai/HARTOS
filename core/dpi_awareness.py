"""
core.dpi_awareness — single source of truth for Windows per-monitor DPI awareness.

Why this module exists:
    Without explicit DPI awareness on Windows, the OS returns LOGICAL coords
    from the high-level APIs (pyautogui.size, EnumDisplayMonitors,
    GetWindowRect) but PHYSICAL pixels from low-level capture
    (pyautogui.screenshot uses BitBlt of the desktop DC at physical res).

    On a 150%-scaled 2560x1440 display:
        - pyautogui.size()        → (1707, 960)   (logical)
        - pyautogui.screenshot()  → 2560x1440     (physical)
        - EnumDisplayMonitors     → (0,0,1707,960) (logical)
        - VLM grounds coordinates in IMAGE space (physical) → caller scales
          to (logical) for pyautogui.click → click misses by ~1.5x.

    Setting DPI awareness once per process makes every API return PHYSICAL
    coords consistently.  Idempotent: calling twice with the same value is
    a no-op; calling with a different value silently fails (so existing
    DPI-aware processes aren't disturbed).

Why a dedicated module:
    Two call sites needed it (integrations/vlm/local_computer_tool.py for
    screenshot/click DPI, integrations/remote_desktop/window_capture.py for
    EnumDisplayMonitors / GetWindowRect).  A second copy was added in the
    Phase 1 VLM commit (693ccad7) and immediately flagged as a DRY
    violation.  Promoting here so there is exactly ONE place that knows
    about SetProcessDpiAwareness and the (Win 8.1+ shcore vs Win 7 user32)
    branch, and exactly ONE place to update if Microsoft adds a new tier
    (e.g. PER_MONITOR_DPI_AWARE_V2 already exists at value 4).

Use:
    from core.dpi_awareness import ensure_dpi_aware
    ensure_dpi_aware()  # safe at module-load OR lazy first-call

    # Optional: query whether we successfully set it
    from core.dpi_awareness import is_dpi_aware
    if is_dpi_aware():
        ...
"""
import logging
import sys

logger = logging.getLogger('hevolve.dpi_awareness')

# PROCESS_PER_MONITOR_DPI_AWARE — value passed to SetProcessDpiAwareness.
# Win 8.1+ accepts 0 (UNAWARE), 1 (SYSTEM_DPI_AWARE), 2 (PER_MONITOR_DPI_AWARE).
# Win 10 1607+ also has SetProcessDpiAwarenessContext for V2 (DPI scale changes
# without process restart) but value 2 is the most portable + sufficient for
# VLM screenshot/click coordinate consistency.
_PER_MONITOR_DPI_AWARE = 2

_dpi_aware_set: bool = False


def ensure_dpi_aware() -> None:
    """Make this process DPI-aware on Windows.  No-op on macOS / Linux.

    Idempotent and crash-proof: any failure is logged at debug level and
    swallowed.  Safe to call from module-load AND from lazy-first-call
    paths (the second call is a Win32 no-op when awareness is already set).
    """
    global _dpi_aware_set
    if sys.platform != 'win32':
        return
    if _dpi_aware_set:
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(_PER_MONITOR_DPI_AWARE)
        except (AttributeError, OSError):
            # Pre-Win-8.1: shcore.dll missing or function absent.  Fall back
            # to the older system-wide DPI awareness API.  Better than
            # nothing; coords stay consistent within a single monitor at
            # the cost of multi-monitor coord weirdness on mixed-DPI setups.
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        # Don't blow up the importer of any module that calls this — log
        # and continue.  Worst case: VLM clicks land slightly off, which
        # the loop's verify-and-retry path will detect.
        logger.debug(f"DPI awareness setup skipped: {e}")
        return
    _dpi_aware_set = True


def is_dpi_aware() -> bool:
    """True if :func:`ensure_dpi_aware` has successfully run.

    Note: returns False on non-Windows (where the concept doesn't apply)
    AND when the call ran but failed (logged at debug).  Callers that
    need to know "are coordinates physical?" should use this OR explicit
    platform checks.
    """
    return _dpi_aware_set
