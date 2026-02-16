"""
Wan2GP tool wrapper — video generation from text prompts.

Service: Wan2GP (https://github.com/deepbeepmeep/Wan2GP)
Port: Dynamic (assigned by RuntimeToolManager)
Async task pattern: submit → poll (same as ACE-Step)
"""

from .registry import ServiceToolInfo, service_tool_registry


class Wan2GPTool:
    """Thin wrapper to register Wan2GP video generation with the ServiceToolRegistry."""

    REPO_URL = "https://github.com/deepbeepmeep/Wan2GP"

    @classmethod
    def create_tool_info(cls, base_url: str) -> ServiceToolInfo:
        return ServiceToolInfo(
            name="wan2gp",
            description=(
                "AI video generation from text prompts. Creates short video clips "
                "from text descriptions. Supports text-to-video and image-to-video "
                "modes with various resolution and duration settings."
            ),
            base_url=base_url,
            endpoints={
                "generate": {
                    "path": "/generate",
                    "method": "POST",
                    "description": (
                        "Submit a video generation task. "
                        "Input: JSON with 'prompt' (text description of video), "
                        "'num_frames' (int, default 49), 'width' (int, default 512), "
                        "'height' (int, default 320), 'num_inference_steps' (int, default 25). "
                        "Returns task_id to check result."
                    ),
                    "params_schema": {
                        "prompt": {"type": "string", "description": "Video description prompt"},
                        "num_frames": {"type": "integer", "description": "Number of frames", "default": 49},
                        "width": {"type": "integer", "description": "Video width", "default": 512},
                        "height": {"type": "integer", "description": "Video height", "default": 320},
                        "num_inference_steps": {"type": "integer", "description": "Inference steps", "default": 25},
                    },
                },
                "check_result": {
                    "path": "/check_result",
                    "method": "POST",
                    "description": (
                        "Check status of a video generation task. "
                        "Input: JSON with 'task_id' (string from generate). "
                        "Returns status and video URL when complete."
                    ),
                    "params_schema": {
                        "task_id": {"type": "string", "description": "Task ID from generate"},
                    },
                },
            },
            health_endpoint="/health",
            tags=["video", "generation", "text-to-video", "ai"],
            timeout=300,
        )

    @classmethod
    def register(cls, base_url: str) -> bool:
        """Register Wan2GP with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
