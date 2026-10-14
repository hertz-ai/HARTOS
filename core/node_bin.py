"""core.node_bin -- THE Node.js locator.

WHY THIS EXISTS
---------------
Two places answered "where is Node.js?" before 2026-10-14 (amendment A14):
the coding-tool launcher (integrations/coding_agent/installer.launch_argv
looked for the node.exe npm installs beside its shims, else PATH) and the
WhatsApp gateway supervisor (integrations/social/whatsapp_supervisor's
ensure_node / ensure_npm, PATH only).  Reel Studio's animated overlays
(integrations/reel_studio/motion, set up on demand through
integrations.service_tools.runtime_manager) are a third caller.  This module
is the one answer; those callers import it.

WHERE NODE COMES FROM (first hit wins)
  1. ``beside``: a directory the caller already holds, when it has a
     ``node`` (``node.exe`` on Windows) file in it.  npm's Windows installer
     puts node.exe in the folder of its .cmd shims, and the shim's script is
     meant for that Node.
  2. ``shutil.which('node')`` on this process's PATH.
npm is ``shutil.which('npm')`` (on Windows the npm.cmd shim, which
installer.launch_argv knows how to launch).

Nothing is cached: a lookup is a PATH walk, and Node may be installed while
the process runs.  Nothing here installs Node; a caller that gets None says
that Node.js is missing.  core never imports integrations.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Optional


def _node_name() -> str:
    return 'node.exe' if sys.platform == 'win32' else 'node'


def find_node(beside: Optional[str] = None) -> Optional[str]:
    """The Node.js executable to run, or None when there is none.

    ``beside``: a directory to look in first (an npm shim's folder).
    """
    if beside:
        local = os.path.join(beside, _node_name())
        if os.path.isfile(local):
            return local
    return shutil.which('node')


def find_npm() -> Optional[str]:
    """The npm executable (on Windows its npm.cmd shim), or None."""
    return shutil.which('npm')


__all__ = ['find_node', 'find_npm']
