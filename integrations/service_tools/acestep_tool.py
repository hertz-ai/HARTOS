"""
AceStep 1.5 tool wrapper — AI music generation.

Service: ACE-Step 1.5 (https://github.com/ace-step/ACE-Step-1.5)
Default port: 8001
Deployment: uv run acestep-api --port 8001
Note: Must run with workers=1 (in-memory job queue not shared across workers)
"""

from .registry import ServiceToolInfo, service_tool_registry


class AceStepTool:
    """Thin wrapper to register AceStep 1.5 with the ServiceToolRegistry."""

    DEFAULT_URL = "http://localhost:8001"

    @classmethod
    def create_tool_info(cls, base_url: str = None) -> ServiceToolInfo:
        return ServiceToolInfo(
            name="acestep",
            description=(
                "AI music generation. Creates songs from text prompts with lyrics, "
                "genre, tempo, and instrumentation control. Generates full songs "
                "in under 10 seconds on consumer hardware."
            ),
            base_url=base_url or cls.DEFAULT_URL,
            endpoints={
                "generate": {
                    "path": "/release_task",
                    "method": "POST",
                    "description": (
                        "Submit a music generation task. "
                        "Input: JSON with 'prompt' (lyrics/description), "
                        "'genre' (pop/rock/jazz/etc), 'tempo' (BPM, default 120), "
                        "'duration' (seconds, default 30). "
                        "Returns task_id to check result with query_result endpoint."
                    ),
                    "params_schema": {
                        "prompt": {"type": "string", "description": "Music prompt with lyrics and style"},
                        "genre": {"type": "string", "description": "Music genre (pop, rock, jazz, classical, etc.)"},
                        "tempo": {"type": "integer", "description": "BPM tempo", "default": 120},
                        "duration": {"type": "integer", "description": "Duration in seconds", "default": 30},
                    },
                },
                "check_result": {
                    "path": "/query_result",
                    "method": "POST",
                    "description": (
                        "Check status and get result of a music generation task. "
                        "Input: JSON with 'task_id' (string from release_task). "
                        "Returns generation status and audio URL when complete."
                    ),
                    "params_schema": {
                        "task_id": {"type": "string", "description": "Task ID from generate endpoint"},
                    },
                },
            },
            health_endpoint="/health",
            tags=["music", "audio", "generation", "singing"],
            timeout=120,
        )

    @classmethod
    def register(cls, base_url: str = None) -> bool:
        """Register AceStep with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
