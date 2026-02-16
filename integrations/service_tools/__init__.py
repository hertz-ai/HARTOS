"""
Service Tools - Dynamic HTTP tool registry for external microservices.

Extends the MCP integration pattern to support any HTTP-based tool service
(Crawl4AI, AceStep, Wan2GP, TTS-Audio-Suite, etc.) with health checking,
auto-discovery, and autogen/langchain compatible function generation.

Runtime tools are managed by RuntimeToolManager which handles:
- Model download (git clone / HuggingFace)
- VRAM-aware start/stop of sidecar servers (dynamic ports)
- State persistence (skip re-download on restart)
- Auto-registration with service_tool_registry → AutoGen + LangChain
"""

from .registry import ServiceToolRegistry, ServiceToolInfo, service_tool_registry
from .crawl4ai_tool import Crawl4AITool
from .acestep_tool import AceStepTool
from .wan2gp_tool import Wan2GPTool
from .tts_audio_suite_tool import TTSAudioSuiteTool
from .whisper_tool import WhisperTool
from .omniparser_tool import OmniParserTool
from .model_storage import ModelStorageManager, model_storage
from .vram_manager import VRAMManager, vram_manager
from .runtime_manager import RuntimeToolManager, runtime_tool_manager
from .media_agent import generate_media, check_media_status, register_media_tools

__all__ = [
    "ServiceToolRegistry",
    "ServiceToolInfo",
    "service_tool_registry",
    "CrawlAITool",
    "AceStepTool",
    "Wan2GPTool",
    "TTSAudioSuiteTool",
    "WhisperTool",
    "OmniParserTool",
    "ModelStorageManager",
    "model_storage",
    "VRAMManager",
    "vram_manager",
    "RuntimeToolManager",
    "runtime_tool_manager",
    "generate_media",
    "check_media_status",
    "register_media_tools",
]
