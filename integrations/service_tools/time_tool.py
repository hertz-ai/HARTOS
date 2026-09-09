"""
Time tool — real current date/time, in-process (no external service).

2026-08-19: 'get_time' had been listed as an allowed fast-tier tool in
tool_allowlist.py since before this file existed, but was never actually
implemented anywhere in the codebase (confirmed by exhaustive grep). Live
consequence: asked "what time is it right now" at 13:27 IST, the model
answered "10:42 AM" -- fabricated with full confidence, no tool attempted,
because there was nothing real to call. This closes that gap the same way
Crawl4AITool closes the web-fetch gap: a native_handler, no HTTP server.
"""

import json
from datetime import datetime
from typing import Optional

from .registry import ServiceToolInfo, service_tool_registry

# Server-local zone used when the caller doesn't ask for a specific one.
# This deployment's real-world usage has been India-based throughout this
# investigation (Chennai weather/news questions) -- IST is the sane default,
# not a guess pulled from nowhere. UTC is always included alongside it so
# the answer is unambiguous regardless of the reader's own zone.
_DEFAULT_ZONE = 'Asia/Kolkata'


def _native_get_time(params_json: str) -> str:
    """Execute in-process. Returns a plain, unambiguous time string."""
    from zoneinfo import ZoneInfo

    try:
        params = json.loads(params_json) if isinstance(params_json, str) else params_json
    except (json.JSONDecodeError, TypeError):
        params = {}
    if not isinstance(params, dict):
        params = {}

    zone_name = params.get('timezone') or _DEFAULT_ZONE

    try:
        local_str = datetime.now(ZoneInfo(zone_name)).strftime('%Y-%m-%d %H:%M:%S %A')
    except Exception as e:
        return (f"Could not resolve timezone {zone_name!r} ({e}). "
                f"Use an IANA name like 'Asia/Kolkata' or 'UTC'.")

    utc_str = datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S')

    return (f"The current date and time is {local_str} ({zone_name}). "
            f"(UTC: {utc_str})")


class TimeTool:
    """Register real current-time lookup as a native tool (in-process)."""

    NAME = 'get_time'

    @classmethod
    def create_tool_info(cls, base_url: Optional[str] = None) -> ServiceToolInfo:
        return ServiceToolInfo(
            name=cls.NAME,
            description=(
                "Get the REAL current date and time. Always call this "
                "instead of guessing or estimating the time -- the model "
                "has no internal clock and any time it states without "
                "calling this tool is a fabrication."
            ),
            base_url=base_url or 'native://in-process',
            endpoints={
                cls.NAME: {
                    'path': f'/{cls.NAME}',
                    'method': 'POST',
                    'description': (
                        'Return the real current date and time. Optional '
                        '"timezone" (IANA name, e.g. "Asia/Kolkata", '
                        '"America/New_York", "UTC"); defaults to '
                        f'{_DEFAULT_ZONE!r} with UTC also included.'
                    ),
                    'params_schema': {
                        'timezone': {
                            'type': 'string',
                            'description': 'IANA timezone name (optional)',
                        },
                    },
                    'native_handler': _native_get_time,
                },
            },
            health_endpoint=None,  # No external service to check
            tags=['time', 'clock', 'date'],
            timeout=5,
        )

    @classmethod
    def register(cls, base_url: Optional[str] = None) -> bool:
        """Register the time tool with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
