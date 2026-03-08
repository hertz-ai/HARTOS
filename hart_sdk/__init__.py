"""
HART SDK — Developer toolkit for building AI-native apps on HART OS.

Like Android SDK but for an AI-native operating system. Zero new dependencies.

Quick start:
    from hart_sdk import HartApp, ai, events, config, environments

    app = HartApp('my-translator', version='1.0.0')
    app.needs_ai('llm', min_accuracy=0.7)
    app.needs_ai('tts', required=False)
    app.permissions(['network', 'audio'])
    manifest = app.manifest()

    result = ai.infer('Translate to French: Hello')
    events.emit('translation.done', {'text': result})
"""

from hart_sdk.app_builder import HartApp
from hart_sdk.ai_client import ai
from hart_sdk.event_client import events
from hart_sdk.config_client import config
from hart_sdk.environment_client import environments
from hart_sdk.platform_detect import detect_platform

__all__ = ['HartApp', 'ai', 'events', 'config', 'environments',
           'detect_platform']

__version__ = '0.1.0'
