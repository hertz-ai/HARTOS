"""
AceStep 1.5 tool wrapper — AI music generation.

Service: ACE-Step 1.5 (https://github.com/ace-step/ACE-Step-1.5)
Deployment: RuntimeToolManager.setup_tool('acestep') launches
    servers/acestep_server.py, which binds an OS-assigned port and
    registers this wrapper at it via _register_tool_at_port.
Note: Must run with workers=1 (in-memory job queue not shared across workers)

DEFAULT_URL is only the pre-start fallback — create_recipe.py:1793 and
reuse_recipe.py:2623 call register() with no base_url while building an
agent, before any sidecar exists.  It is NOT where the server runs.

The parameter names below are the fields of ACE-Step's own
``GenerateMusicRequest`` (acestep/api/http/release_task_models.py).
They MUST match: the registry posts the declared kwargs straight through
as the JSON body (registry.py:340 ``pooled_post(url, json=kwargs)``), and
that pydantic model ignores unknown fields.  MEASURED 2026-09-21, this
wrapper previously declared 'genre', 'tempo' and 'duration', none of
which exist on the model — so every generation silently ran at the
default tempo and length, and 'check_result' sent 'task_id' while
/query_result reads 'task_id_list' (query_result_route.py:55), parsing a
missing key as '[]' and returning no results for a task that had in fact
completed.
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
                        "Submit a music generation task (asynchronous). "
                        "Input: JSON with 'prompt' (style/genre/instrumentation "
                        "description), 'lyrics' (text to sing, empty for "
                        "instrumental), 'bpm' (tempo), 'audio_duration' "
                        "(seconds, 10-600), 'vocal_language'. "
                        "Returns task_id; poll it with the check_result endpoint."
                    ),
                    "params_schema": {
                        "prompt": {"type": "string", "description": "Style/genre/instrumentation description, e.g. 'upbeat acoustic folk, guitar and light percussion'"},
                        "lyrics": {"type": "string", "description": "Lyrics to sing; leave empty for an instrumental"},
                        "bpm": {"type": "integer", "description": "Tempo in beats per minute (30-300)"},
                        "audio_duration": {"type": "number", "description": "Length in seconds (10-600)"},
                        "vocal_language": {"type": "string", "description": "Language code for vocals, e.g. 'en'"},
                        "key_scale": {"type": "string", "description": "Musical key, e.g. 'C major', 'F# minor'"},
                        "inference_steps": {"type": "integer", "description": "Diffusion steps; 8 suits the turbo model"},
                        "model": {"type": "string", "description": "DiT checkpoint, e.g. 'acestep-v15-turbo'"},
                        # ACE-Step's own default is 'mp3', and its mp3 writer
                        # shells out to ffmpeg.  MEASURED 2026-09-21 on a box
                        # without ffmpeg on PATH: a 10s clip generated fine
                        # (DiT + VAE decode both completed) and then
                        # AudioSaver raised "ffmpeg executable not found";
                        # inference.py swallows that into audio_path = "",
                        # so /query_result reported status=succeeded with an
                        # EMPTY file and nothing was written to disk.  'wav'
                        # and 'flac' go through soundfile/torchaudio and need
                        # no external binary.
                        "audio_format": {"type": "string", "description": "Output format: wav or flac (no external encoder needed); mp3/opus/aac require ffmpeg on PATH"},
                    },
                },
                "check_result": {
                    "path": "/query_result",
                    "method": "POST",
                    "description": (
                        "Check status and get results of music generation tasks. "
                        "Input: JSON with 'task_id_list' (LIST of task_id strings "
                        "from release_task). Returns status per task and the "
                        "audio path when complete."
                    ),
                    "params_schema": {
                        "task_id_list": {"type": "array", "description": "List of task IDs from the generate endpoint"},
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
