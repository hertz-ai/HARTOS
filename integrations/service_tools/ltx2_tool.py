"""
LTX-Video tool wrapper — text-to-video generation.

Service: integrations/vision/ltx2_server.py (diffusers LTXPipeline)
Port: Dynamic (assigned by RuntimeToolManager, read from the server's
      PORT= line — never a literal).

Unlike wan2gp, /generate here is SYNCHRONOUS: one call blocks until the
mp4 is written and returns its path, so there is no task_id to poll.  The
sibling exists because _register_tool_at_port had no ltx2 branch at all,
so a started LTX server logged "No tool wrapper for ltx2" and never
entered the ServiceToolRegistry — the agent could not see the one video
tool the box had running.
"""

from .registry import ServiceToolInfo, service_tool_registry


class Ltx2Tool:
    """Registers the LTX-Video server with the ServiceToolRegistry."""

    # A CPU-only render of a ~1 s clip is minutes, not seconds, and a
    # cpu_offload render still pays the text-encoder swap on every call.
    # The registry default would time out on a working server.
    TIMEOUT_S = 1800

    @classmethod
    def create_tool_info(cls, base_url: str) -> ServiceToolInfo:
        return ServiceToolInfo(
            name="ltx2",
            description=(
                "AI video generation from text prompts (LTX-Video). Creates "
                "short video clips from text descriptions. Returns the "
                "finished mp4 in one call — no polling."
            ),
            base_url=base_url,
            endpoints={
                "generate": {
                    "path": "/generate",
                    "method": "POST",
                    "description": (
                        "Generate a short video from a text prompt. "
                        "Input: JSON with 'prompt' (text description), "
                        "'num_frames' (int, default 49, capped at 97, "
                        "rounded to 8n+1), 'width'/'height' (ints, rounded "
                        "down to a multiple of 32), 'num_inference_steps' "
                        "(int, default 30), 'fps' (int, default 24). "
                        "Returns video_path, video_url and "
                        "generation_time_seconds."
                    ),
                    "params_schema": {
                        "prompt": {"type": "string", "description": "Video description prompt"},
                        "num_frames": {"type": "integer", "description": "Number of frames (8n+1, max 97)", "default": 49},
                        "width": {"type": "integer", "description": "Video width (multiple of 32)", "default": 704},
                        "height": {"type": "integer", "description": "Video height (multiple of 32)", "default": 480},
                        "num_inference_steps": {"type": "integer", "description": "Denoising steps", "default": 30},
                        "fps": {"type": "integer", "description": "Frames per second", "default": 24},
                    },
                },
                "list": {
                    "path": "/list",
                    "method": "GET",
                    "description": "List previously generated videos with their sizes.",
                    "params_schema": {},
                },
            },
            health_endpoint="/health",
            tags=["video", "generation", "text-to-video", "ai"],
            timeout=cls.TIMEOUT_S,
        )

    @classmethod
    def register(cls, base_url: str) -> bool:
        """Register LTX-Video with the global service_tool_registry."""
        tool_info = cls.create_tool_info(base_url)
        return service_tool_registry.register_tool(tool_info)
