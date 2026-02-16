"""
TTS-Audio-Suite tool wrapper — text-to-speech with multiple engines.

Service: TTS-Audio-Suite (https://github.com/diodiogod/TTS-Audio-Suite)
Port: Dynamic (assigned by RuntimeToolManager)
"""

from .registry import ServiceToolInfo, service_tool_registry


class TTSAudioSuiteTool:
    """Thin wrapper to register TTS-Audio-Suite with the ServiceToolRegistry."""

    REPO_URL = "https://github.com/diodiogod/TTS-Audio-Suite"

    @classmethod
    def create_tool_info(cls, base_url: str) -> ServiceToolInfo:
        return ServiceToolInfo(
            name="tts_audio_suite",
            description=(
                "Text-to-speech with multiple TTS engines. Provides high-quality "
                "speech synthesis with support for various models including "
                "Coqui TTS, XTTS, and more. Supports voice cloning and "
                "multiple languages."
            ),
            base_url=base_url,
            endpoints={
                "synthesize": {
                    "path": "/synthesize",
                    "method": "POST",
                    "description": (
                        "Generate speech audio from text. "
                        "Input: JSON with 'text' (string to speak), "
                        "'model' (optional model name), "
                        "'language' (optional language code). "
                        "Returns audio file URL."
                    ),
                    "params_schema": {
                        "text": {"type": "string", "description": "Text to synthesize"},
                        "model": {"type": "string", "description": "TTS model name (optional)"},
                        "language": {"type": "string", "description": "Language code (optional)"},
                    },
                },
                "list_models": {
                    "path": "/models",
                    "method": "GET",
                    "description": "List available TTS models and their capabilities.",
                    "params_schema": {},
                },
            },
            health_endpoint="/health",
            tags=["tts", "speech", "audio", "voice", "synthesis"],
            timeout=120,
        )

    @classmethod
    def register(cls, base_url: str) -> bool:
        """Register TTS-Audio-Suite with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
