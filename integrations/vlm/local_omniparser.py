"""
local_omniparser.py — Lazy-loaded OmniParser for screen parsing.

Tier 'inprocess': imports OmniParser's Omniparser class directly (singleton).
Tier 'http': HTTP POST to localhost:8080/parse/ (existing FastAPI endpoint).

OmniParser detects UI elements (buttons, text fields, icons) from a screenshot
using YOLO detection + Florence captioning, returning labeled bounding boxes.
"""

import os
import sys
import logging

logger = logging.getLogger('hevolve.vlm.omniparser')

# Singleton for in-process OmniParser (models are GPU-heavy, load once)
_parser_instance = None
_parser_lock = None


def _get_lock():
    """Lazy-init threading lock (avoid import-time side effects)."""
    global _parser_lock
    if _parser_lock is None:
        import threading
        _parser_lock = threading.Lock()
    return _parser_lock


def parse_screen(screenshot_b64: str, tier: str) -> dict:
    """
    Parse a screenshot into structured UI elements.

    Args:
        screenshot_b64: Base64-encoded PNG screenshot
        tier: 'inprocess' (direct import) or 'http' (localhost:8080)
    Returns:
        dict with keys:
        - 'screen_info': str — formatted ID→label text for LLM consumption
        - 'parsed_content_list': list — [{type, content, bbox, idx}, ...]
        - 'som_image_base64': str — labeled screenshot with bounding boxes
        - 'original_screenshot_base64': str — original screenshot
        - 'width': int, 'height': int — screen dimensions
        - 'latency': float — parse time in seconds
    """
    if tier == 'inprocess':
        return _parse_inprocess(screenshot_b64)
    else:
        return _parse_http(screenshot_b64)


def _parse_inprocess(screenshot_b64: str) -> dict:
    """Parse using direct OmniParser import."""
    global _parser_instance
    import time

    with _get_lock():
        if _parser_instance is None:
            _parser_instance = _load_omniparser()

    start = time.time()
    result = _parser_instance.parse(screenshot_b64)
    latency = time.time() - start

    # Ensure consistent keys
    if 'latency' not in result:
        result['latency'] = latency
    if 'original_screenshot_base64' not in result:
        result['original_screenshot_base64'] = screenshot_b64

    return result


def _parse_http(screenshot_b64: str) -> dict:
    """Parse via HTTP POST to OmniParser FastAPI server."""
    import time
    import requests

    omni_url = os.environ.get('OMNIPARSER_URL', 'http://localhost:8080')
    start = time.time()

    resp = requests.post(
        f'{omni_url.rstrip("/")}/parse/',
        json={'base64_image': screenshot_b64},
        timeout=30
    )
    resp.raise_for_status()
    result = resp.json()
    latency = time.time() - start

    if 'latency' not in result:
        result['latency'] = latency
    if 'original_screenshot_base64' not in result:
        result['original_screenshot_base64'] = screenshot_b64

    return result


def _load_omniparser():
    """
    Load OmniParser singleton.

    Searches for OmniParser in:
    1. OMNIPARSER_PATH env var
    2. ~/.hevolve/models/omniparser/
    3. Sibling directory ../OmniParser (dev layout)
    """
    search_paths = [
        os.environ.get('OMNIPARSER_PATH', ''),
        os.path.join(os.path.expanduser('~'), '.hevolve', 'models', 'omniparser'),
        os.path.join(os.path.dirname(__file__), '..', '..', '..', 'OmniParser'),
    ]

    for path in search_paths:
        if not path:
            continue
        path = os.path.abspath(path)
        util_path = os.path.join(path, 'util', 'omniparser.py')
        if os.path.exists(util_path):
            logger.info(f"Loading OmniParser from {path}")
            if path not in sys.path:
                sys.path.insert(0, path)
            from util.omniparser import Omniparser
            return Omniparser(path)

    raise ImportError(
        "OmniParser not found. Set OMNIPARSER_PATH or install to "
        "~/.hevolve/models/omniparser/"
    )
