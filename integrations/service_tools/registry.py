"""
Service Tool Registry — follows MCPToolRegistry pattern (mcp/mcp_integration.py)
but for any HTTP microservice (not just MCP protocol servers).

Design:
- ServiceToolInfo describes a tool's endpoints, auth, and health check
- ServiceToolRegistry manages discovery, health, and function generation
- Global singleton: service_tool_registry (mirrors mcp_registry)
- Uses core.http_pool for connection pooling (same as MCP)
"""

import inspect
import json
import keyword
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable

logger = logging.getLogger(__name__)


# ── JSON-Schema → Python type map for autogen tool-schema synthesis ──
# When an endpoint's params_schema specifies a JSON-Schema 'type', map it
# to a real Python type so inspect.Signature.replace() can attach typed
# parameters to endpoint_executor.  Autogen reads inspect.signature() to
# generate the JSON tool schema given to the LLM — typed params produce
# rich `{"properties": {"url": {"type": "string", ...}}, "required": [...]}`
# instead of the empty `{}` that `**kwargs: Any` produces.  Unknown types
# fall back to Any (graceful degradation — same behavior as today's
# `**kwargs: Any` for that one param, never worse).
_JSON_SCHEMA_TO_PY_TYPE: Dict[str, Any] = {
    'string': str, 'str': str,
    'integer': int, 'int': int,
    'number': float, 'float': float,
    'boolean': bool, 'bool': bool,
    'object': dict, 'dict': dict,
    'array': list, 'list': list,
}


def _synthesize_signature_from_schema(
    schema: Any,
    func_name: str = '<tool>',
) -> Optional[inspect.Signature]:
    """Build inspect.Signature from a JSON-Schema-style params dict.

    Accepts either canonical JSON-Schema:
        {'properties': {'url': {'type': 'string', ...}}, 'required': ['url']}
    OR the flat params_schema style used by the registry's create_tool_info:
        {'url': {'type': 'string', 'description': '...'}}

    Returns None on ANY error so the caller falls back to `**kwargs: Any`
    — strictly additive (Option A in 2026-05-15 design discussion):
    successful synthesis is a win, failure is a no-op vs current state.
    """
    try:
        if not isinstance(schema, dict) or not schema:
            return None

        # Normalize to (properties, required_set)
        if 'properties' in schema and isinstance(schema['properties'], dict):
            properties = schema['properties']
            required = set(schema.get('required') or [])
        else:
            # Flat dict — treat each top-level key as a property.  No
            # 'required' info available, so mark all optional (default=None)
            # to match today's liberal **kwargs behavior — the LLM can omit
            # them and the endpoint still runs.
            properties = schema
            required = set()

        if not isinstance(properties, dict) or not properties:
            return None

        params = []
        for prop_name, prop_spec in properties.items():
            if not isinstance(prop_name, str) or not prop_name.isidentifier():
                # Skip names that can't be valid Python parameters
                # (autogen would also reject them).
                continue
            if keyword.iskeyword(prop_name):
                # `'class'.isidentifier()` returns True but
                # `inspect.Parameter('class', ...)` raises ValueError.
                # Skip Python hard keywords so they fall back to **kwargs
                # without poisoning the rest of the signature.  Self-
                # review v1 (2026-05-15) caught this latent bug —
                # without the skip, one keyword-named param would raise
                # mid-loop, the outer try/except would catch it, and
                # the ENTIRE tool would lose typed signature (degrading
                # to `**kwargs: Any`).  Per-param skip is strictly
                # better: kept params stay typed.
                continue
            if isinstance(prop_spec, dict):
                json_type = (prop_spec.get('type') or '').lower()
                py_type = _JSON_SCHEMA_TO_PY_TYPE.get(json_type, Any)
            else:
                py_type = Any
            if prop_name in required:
                params.append(inspect.Parameter(
                    prop_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    annotation=py_type,
                ))
            else:
                params.append(inspect.Parameter(
                    prop_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=Optional[py_type],
                ))

        if not params:
            return None
        return inspect.Signature(parameters=params, return_annotation=str)
    except Exception as _sig_err:
        logger.debug(
            f"[tool-schema] could not synthesize signature for {func_name!r}: "
            f"{type(_sig_err).__name__}: {_sig_err} — falling back to "
            "**kwargs: Any (no regression vs prior state)")
        return None

try:
    from core.labeled_tool import labeled_tool
except ImportError:  # cx_Freeze / degraded test env
    def labeled_tool(name, func, description, *, ui_label):  # type: ignore
        from langchain.agents import Tool as _Tool
        return _Tool(name=name, func=func, description=description)


# Friendly UI status labels per known service tool name fragment.
# Used by `get_langchain_tools()` to register Tool() display strings.
_SERVICE_TOOL_LABEL_HINTS = {
    "crawl4ai": "Crawling web content…",
    "acestep": "Generating music…",
    "ace_step": "Generating music…",
    "ace-step": "Generating music…",
    "diffrhythm": "Generating song…",
    "rembg": "Removing background…",
    "omniparser": "Parsing screen UI…",
    "wan2gp": "Generating video…",
    "f5": "Speaking with F5 voice…",
    "kokoro": "Speaking with Kokoro voice…",
    "cosyvoice": "Speaking with CosyVoice…",
    "chatterbox": "Speaking with Chatterbox…",
    "indic_parler": "Speaking Indic voice…",
    "indic-parler": "Speaking Indic voice…",
    "melotts": "Speaking with MeloTTS…",
    "mms_tts": "Speaking with MMS TTS…",
    "mms-tts": "Speaking with MMS TTS…",
    "neutts": "Speaking with NeuTTS…",
    "luxtts": "Speaking with LuxTTS…",
    "xtts": "Speaking with XTTS…",
    "omnivoice": "Speaking with OmniVoice…",
    "pocket_tts": "Speaking with PocketTTS…",
    "pocket-tts": "Speaking with PocketTTS…",
    "tts_audio_suite": "Synthesising speech…",
    "whisper": "Transcribing audio…",
}


def _derive_service_tool_label(tool_name: str, ep_name: str) -> str:
    """Resolve a ≤60-char human label for a service-tool endpoint."""
    key = (tool_name or "").lower()
    for hint, label in _SERVICE_TOOL_LABEL_HINTS.items():
        if hint in key:
            return label[:60]
    return (f"Calling {tool_name} service…")[:60]


@dataclass
class ServiceToolInfo:
    """Metadata for a registered service tool."""
    name: str
    description: str
    base_url: str
    endpoints: Dict[str, Dict[str, Any]]  # endpoint_name -> {path, method, description, params_schema}
    health_endpoint: str = "/health"
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    timeout: int = 30
    is_healthy: bool = False
    registered_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "base_url": self.base_url,
            "endpoints": self.endpoints,
            "health_endpoint": self.health_endpoint,
            "version": self.version,
            "tags": self.tags,
            "api_key_header": self.api_key_header,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ServiceToolInfo':
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            base_url=data["base_url"],
            endpoints=data.get("endpoints", {}),
            health_endpoint=data.get("health_endpoint", "/health"),
            version=data.get("version", "1.0.0"),
            tags=data.get("tags", []),
            api_key=data.get("api_key"),
            api_key_header=data.get("api_key_header", "Authorization"),
            timeout=data.get("timeout", 30),
        )


class ServiceToolRegistry:
    """
    Registry for HTTP microservice tools.

    Mirrors MCPToolRegistry (mcp/mcp_integration.py:185-315):
    - add_server → register_tool (with health check)
    - create_tool_function → create_endpoint_function
    - get_all_tool_functions → same signature
    - Global singleton: service_tool_registry
    """

    def __init__(self, config_file: str = "service_tools.json"):
        self._tools: Dict[str, ServiceToolInfo] = {}
        self._config_file = config_file

    def register_tool(self, tool_info: ServiceToolInfo) -> bool:
        """Register a tool. Health-checks first; skips if service is down."""
        if tool_info.name in self._tools:
            logger.info(f"Service tool '{tool_info.name}' already registered, skipping")
            return True

        tool_info.is_healthy = self._health_check(tool_info)
        tool_info.registered_at = datetime.now().isoformat()
        self._tools[tool_info.name] = tool_info

        status = "healthy" if tool_info.is_healthy else "unhealthy (registered anyway)"
        logger.info(f"Registered service tool: {tool_info.name} [{status}]")
        return True

    def unregister_tool(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            logger.info(f"Unregistered service tool: {name}")
            return True
        return False

    def _health_check(self, tool_info: ServiceToolInfo) -> bool:
        """Check if service is reachable."""
        try:
            from core.http_pool import pooled_get
            headers = {}
            if tool_info.api_key:
                headers[tool_info.api_key_header] = f"Bearer {tool_info.api_key}"

            response = pooled_get(
                f"{tool_info.base_url.rstrip('/')}{tool_info.health_endpoint}",
                headers=headers,
                timeout=5,
            )
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"Health check failed for {tool_info.name}: {e}")
            return False

    def health_check(self, name: str) -> bool:
        """Re-check health for a specific tool."""
        tool = self._tools.get(name)
        if not tool:
            return False
        tool.is_healthy = self._health_check(tool)
        return tool.is_healthy

    def health_check_all(self) -> Dict[str, bool]:
        """Re-check health for all registered tools."""
        return {name: self.health_check(name) for name in self._tools}

    def create_endpoint_function(self, tool_name: str, endpoint_name: str) -> Optional[Callable]:
        """
        Create a callable for a specific endpoint.

        Mirrors MCPToolRegistry.create_tool_function (mcp_integration.py:262-295):
        returns a function with __name__ and __doc__ set for autogen registration.
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return None

        endpoint = tool.endpoints.get(endpoint_name)
        if not endpoint:
            return None

        path = endpoint["path"]
        method = endpoint.get("method", "POST").upper()
        description = endpoint.get("description", f"{tool_name} {endpoint_name}")
        timeout = tool.timeout

        # Capture in closure
        base_url = tool.base_url.rstrip("/")
        api_key = tool.api_key
        api_key_header = tool.api_key_header

        # If endpoint has a native handler, use it directly (no HTTP)
        native_handler = endpoint.get("native_handler")

        def endpoint_executor(**kwargs: Any) -> str:
            """Execute the service tool endpoint.

            NOTE: `**kwargs: Any` annotation is REQUIRED.  Autogen's
            `register_for_llm` strict-mode rejects unannotated non-default
            parameters with `TypeError: All parameters of the function
            'crawl4ai_crawl' without default values must be annotated`.
            Live evidence langchain.log 2026-05-13/14: 180×/session warnings
            from create_recipe.py:1670 because the unannotated `**kwargs`
            broke every service-tool registration after crawl4ai (Crawl4AI,
            AceStep, omniparser, plus anything in service_tools.json all
            silently dropped — confirmed real capability loss per the
            source comment at create_recipe.py:1651-1652).
            """
            try:
                if native_handler is not None:
                    return native_handler(json.dumps(kwargs))

                from core.http_pool import pooled_get, pooled_post

                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers[api_key_header] = f"Bearer {api_key}"

                url = f"{base_url}{path}"

                if method == "GET":
                    resp = pooled_get(url, params=kwargs, headers=headers, timeout=timeout)
                else:
                    resp = pooled_post(url, json=kwargs, headers=headers, timeout=timeout)

                if resp.status_code == 200:
                    return json.dumps(resp.json())
                else:
                    return json.dumps({"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"})
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)})

        # Set function metadata (same as MCPToolRegistry.create_tool_function)
        # Single-function tools that name their one endpoint after the tool
        # itself (seo_audit_score, gh_pr_open) keep the flat name — the
        # doubled "seo_audit_score_seo_audit_score" would break the
        # prompt↔tool-name contract goal prompts rely on.  Every other
        # tool (crawl4ai_crawl, whisper_transcribe, …) is unchanged.
        func_name = (tool_name if endpoint_name == tool_name
                     else f"{tool_name}_{endpoint_name}")
        endpoint_executor.__name__ = func_name
        endpoint_executor.__doc__ = description

        # ── Attach typed __signature__ derived from endpoint params_schema ──
        # Autogen's register_for_llm reads inspect.signature(func) to build
        # the JSON tool schema sent to the LLM.  By default our closure
        # exposes `(**kwargs: Any)` (added in #541 to satisfy autogen's
        # "all params must be annotated" rule) → JSON schema = empty
        # properties → LLM has to guess param names from the docstring.
        #
        # When params_schema is present (e.g. crawl4ai_tool.py:54-56:
        # {'url': {'type': 'string', 'description': 'URL to crawl'}}),
        # _synthesize_signature_from_schema() builds a real Signature with
        # named, typed parameters.  Autogen then emits a proper
        # `{"properties": {"url": {"type": "string"}}, "required": [...]}`
        # schema, the LLM picks correct param names, and llama.cpp's
        # tool-call grammar can constrain output (companion structural
        # fix for HIGH #5).
        #
        # Strictly additive: any failure path inside the helper returns
        # None → the closure stays on `**kwargs: Any` (identical to
        # today's behavior — no regression possible).
        params_schema = endpoint.get("params_schema") if endpoint else None
        if params_schema:
            new_sig = _synthesize_signature_from_schema(
                params_schema, func_name=func_name)
            if new_sig is not None:
                try:
                    endpoint_executor.__signature__ = new_sig
                except Exception as _attach_err:
                    logger.debug(
                        f"[tool-schema] could not attach __signature__ "
                        f"to {func_name!r}: {type(_attach_err).__name__}: "
                        f"{_attach_err} — keeping **kwargs: Any fallback")

        return endpoint_executor

    def get_all_tool_functions(self) -> Dict[str, Callable]:
        """
        Get all tools as executable functions.

        Mirrors MCPToolRegistry.get_all_tool_functions (mcp_integration.py:297-311).
        Creates one function per endpoint for each registered tool.
        """
        functions = {}
        for tool_name, tool in self._tools.items():
            for endpoint_name in tool.endpoints:
                func = self.create_endpoint_function(tool_name, endpoint_name)
                if func:
                    functions[func.__name__] = func
        return functions

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        Get tool definitions in autogen-compatible format.

        Mirrors MCPToolRegistry.get_tool_definitions (mcp_integration.py:241-260).
        """
        defs = []
        for tool_name, tool in self._tools.items():
            for ep_name, ep in tool.endpoints.items():
                defs.append({
                    # Mirror create_endpoint_function's flat-name rule for
                    # single-function tools (endpoint named after the tool).
                    "name": (tool_name if ep_name == tool_name
                             else f"{tool_name}_{ep_name}"),
                    "description": ep.get("description", f"{tool_name} {ep_name}"),
                    "parameters": ep.get("params_schema", {}),
                    "service_tool": tool_name,
                    "endpoint": ep_name,
                })
        return defs

    def get_langchain_tools(self) -> list:
        """
        Get healthy tools as LangChain Tool() objects for get_tools().

        Plugs into hart_intelligence get_tools().
        LangChain Tool func receives a single string — we route it to
        the first parameter defined in the endpoint's params_schema.
        """
        tools = []
        for tool_name, tool in self._tools.items():
            if not tool.is_healthy:
                continue
            for ep_name, ep in tool.endpoints.items():
                func = self.create_endpoint_function(tool_name, ep_name)
                if func:
                    # Determine the primary parameter name from params_schema
                    # so the single LangChain string input maps correctly.
                    params = ep.get("params_schema", {})
                    primary_param = next(iter(params), "query") if params else "query"

                    tools.append(labeled_tool(
                        name=func.__name__,
                        func=lambda query, _f=func, _p=primary_param: _f(**{_p: query}),
                        description=ep.get("description", f"{tool_name} {ep_name}"),
                        ui_label=_derive_service_tool_label(tool_name, ep_name),
                    ))
        return tools

    def save_config(self) -> None:
        """Persist registry to JSON config file."""
        data = {"tools": [t.to_dict() for t in self._tools.values()]}
        try:
            with open(self._config_file, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved {len(self._tools)} tools to {self._config_file}")
        except Exception as e:
            logger.warning(f"Failed to save service tools config: {e}")

    def load_config(self) -> int:
        """Load registry from JSON config file. Returns count loaded."""
        if not os.path.exists(self._config_file):
            logger.info(f"No service tools config at {self._config_file}")
            return 0

        try:
            with open(self._config_file, "r") as f:
                data = json.load(f)

            loaded = 0
            for tool_data in data.get("tools", []):
                tool_info = ServiceToolInfo.from_dict(tool_data)
                if self.register_tool(tool_info):
                    loaded += 1

            logger.info(f"Loaded {loaded} service tools from {self._config_file}")
            return loaded
        except Exception as e:
            logger.warning(f"Failed to load service tools config: {e}")
            return 0


# Global singleton (mirrors mcp_registry in mcp/mcp_integration.py:315)
service_tool_registry = ServiceToolRegistry()
