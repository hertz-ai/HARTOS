"""
Minimal IO adapter replacing Aider's InputOutput class.

Aider's repomap.py and repo.py call self.io.read_text(), self.io.tool_output(),
self.io.tool_warning(), self.io.tool_error(). This adapter provides those methods
using standard Python I/O, no terminal dependencies.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger('hevolve.coding_agent.aider_core')


class SimpleIO:
    """Minimal IO adapter for vendored Aider modules.

    Replaces Aider's rich InputOutput class with plain logging and file reads.
    """

    def __init__(self, encoding='utf-8'):
        self.encoding = encoding

    def read_text(self, fname):
        """Read a file's text content."""
        try:
            return Path(fname).read_text(encoding=self.encoding, errors='replace')
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"Could not read {fname}: {e}")
            return None

    def tool_output(self, msg='', log_only=False):
        """Log a tool output message."""
        logger.info(msg)

    def tool_warning(self, msg=''):
        """Log a warning."""
        logger.warning(msg)

    def tool_error(self, msg=''):
        """Log an error."""
        logger.error(msg)

    def get_input(self, *args, **kwargs):
        """Not used in headless mode."""
        return ''

    def confirm_ask(self, *args, **kwargs):
        """Auto-confirm in headless mode."""
        return True
