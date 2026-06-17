"""Phase 7: the screen kill-switch as a CROSS-PROCESS authority.

In-process allowed() is per-process memory — a separate screencast portal can't
read it, so adding a screencast surface would let the AI capture a screen the
human cut. start_authority_server exposes the gate over a Unix socket;
query_authority is the FAIL-CLOSED client the portal MUST consult.

Behavioural: the round-trip reflects the human's gate; an unreachable authority
denies (the portal must not capture).

    python -m pytest tests/unit/test_ai_sensing_authority.py --noconftest -p no:capture -q
"""
import os
import socket
import tempfile

import pytest

import core.ai_sensing as s


def test_query_fails_closed_when_authority_unreachable():
    # No server bound here -> a portal consulting the gate must be DENIED.
    assert s.query_authority(
        'screen', '/tmp/hart-nonexistent-authority-xyz.sock') is False


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'),
                    reason='AF_UNIX unavailable on this platform')
def test_authority_reflects_the_human_gate():
    path = os.path.join(tempfile.mkdtemp(), 'sense.sock')
    s.set_sense('screen', False)                 # sensing on
    if not s.start_authority_server(path):
        pytest.skip('AF_UNIX bind unsupported here — Linux deployment path')
    try:
        assert s.query_authority('screen', path) is True
        s.set_sense('screen', True)              # human cuts the screen
        assert s.query_authority('screen', path) is False   # propagates cross-proc
    finally:
        s.set_sense('screen', False)             # restore
