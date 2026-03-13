"""
HART Skill Server — Expose HART OS agents/recipes as OpenClaw-compatible tools.

When OpenClaw is running alongside HART OS, this server makes HART capabilities
available as OpenClaw tools. OpenClaw agents can then:
  - Execute HART recipes (CREATE/REUSE)
  - Query HART agents
  - Use Model Bus for inference
  - Access compute mesh
  - Use vision/TTS services

The server registers as an OpenClaw tool provider via the gateway.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# HART capabilities exposed to OpenClaw
HART_TOOLS = {
    'hart_chat': {
        'name': 'hart_chat',
        'description': 'Send a task to HART OS agent for execution',
        'parameters': {
            'prompt': {'type': 'string', 'description': 'The task to execute'},
            'user_id': {'type': 'string', 'description': 'User ID', 'default': '1'},
            'create_agent': {'type': 'boolean', 'description': 'Create new agent', 'default': False},
        },
    },
    'hart_recipe_run': {
        'name': 'hart_recipe_run',
        'description': 'Execute a trained HART recipe (fast, no LLM calls)',
        'parameters': {
            'prompt_id': {'type': 'string', 'description': 'Recipe prompt ID'},
            'flow_id': {'type': 'string', 'description': 'Flow ID', 'default': '0'},
        },
    },
    'hart_model_bus': {
        'name': 'hart_model_bus',
        'description': 'Run inference via HART Model Bus (local or cloud models)',
        'parameters': {
            'prompt': {'type': 'string', 'description': 'Inference prompt'},
            'model': {'type': 'string', 'description': 'Model name', 'default': 'auto'},
        },
    },
    'hart_tts': {
        'name': 'hart_tts',
        'description': 'Generate speech using HART TTS (LuxTTS/Pocket TTS, offline)',
        'parameters': {
            'text': {'type': 'string', 'description': 'Text to speak'},
            'voice': {'type': 'string', 'description': 'Voice name', 'default': 'alba'},
        },
    },
    'hart_vision': {
        'name': 'hart_vision',
        'description': 'Analyze image using HART vision (MiniCPM)',
        'parameters': {
            'image_path': {'type': 'string', 'description': 'Path to image'},
            'question': {'type': 'string', 'description': 'Question about the image'},
        },
    },
    'hart_expert': {
        'name': 'hart_expert',
        'description': 'Query HART expert agents network (96 specialists)',
        'parameters': {
            'domain': {'type': 'string', 'description': 'Expert domain'},
            'query': {'type': 'string', 'description': 'Query for the expert'},
        },
    },
}


class HARTToolHandler:
    """Handles tool invocations from OpenClaw by dispatching to HART services."""

    def __init__(self, backend_url: str = 'http://localhost:6777'):
        self._backend_url = backend_url

    def handle(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a tool call to the appropriate HART service."""
        handler = getattr(self, f'_handle_{tool_name}', None)
        if handler:
            return handler(args)
        return {'error': f'Unknown tool: {tool_name}'}

    def _handle_hart_chat(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from core.http_pool import pooled_post
        except ImportError:
            import requests
            pooled_post = requests.post

        resp = pooled_post(
            f"{self._backend_url}/chat",
            json={
                'user_id': args.get('user_id', '1'),
                'prompt_id': args.get('prompt_id', '99999'),
                'prompt': args['prompt'],
                'create_agent': args.get('create_agent', False),
            },
            timeout=120,
        )
        if hasattr(resp, 'json'):
            return resp.json()
        return {'result': str(resp)}

    def _handle_hart_recipe_run(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from reuse_recipe import reuse_recipe
            result = reuse_recipe(
                prompt_id=args['prompt_id'],
                flow_id=args.get('flow_id', '0'),
            )
            return {'result': result}
        except Exception as e:
            return {'error': str(e)}

    def _handle_hart_model_bus(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from integrations.agent_engine.model_bus_service import get_model_bus
            bus = get_model_bus()
            result = bus.infer(args['prompt'], model=args.get('model', 'auto'))
            return {'result': result}
        except Exception as e:
            return {'error': str(e)}

    def _handle_hart_tts(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from integrations.audio.tts import get_tts_engine
            engine = get_tts_engine()
            path = engine.synthesize(args['text'], voice=args.get('voice', 'alba'))
            return {'audio_path': path}
        except Exception as e:
            return {'error': str(e)}

    def _handle_hart_vision(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from integrations.vision.vision_service import get_vision_service
            svc = get_vision_service()
            result = svc.analyze(args['image_path'], args.get('question', 'Describe this image'))
            return {'result': result}
        except Exception as e:
            return {'error': str(e)}

    def _handle_hart_expert(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from integrations.expert_agents.agent_network import get_expert_network
            network = get_expert_network()
            result = network.query(args['domain'], args['query'])
            return {'result': result}
        except Exception as e:
            return {'error': str(e)}

    def list_tools(self) -> List[Dict[str, Any]]:
        """List all HART tools available to OpenClaw."""
        return list(HART_TOOLS.values())


def generate_hart_skills() -> List[str]:
    """Generate SKILL.md files for all HART tools.

    These can be installed in OpenClaw to give it native HART access.
    Returns list of generated SKILL.md content strings.
    """
    skills = []
    for tool_name, tool_def in HART_TOOLS.items():
        params_doc = []
        for pname, pinfo in tool_def.get('parameters', {}).items():
            default = pinfo.get('default', '')
            params_doc.append(
                f"- `{pname}` ({pinfo['type']}): {pinfo['description']}"
                + (f" (default: {default})" if default != '' else '')
            )

        skill_md = f"""---
name: {tool_name}
description: {tool_def['description']}
version: 1.0.0
homepage: https://github.com/hertz-ai/HARTOS
metadata: {{"openclaw": {{"emoji": "\\U0001f916", "requires": {{"env": ["HART_BACKEND_URL"]}}}}}}
---

# {tool_def['name']}

{tool_def['description']}

## Usage

This tool connects to a running HART OS instance. Set `HART_BACKEND_URL`
to the HART backend address (default: http://localhost:6777).

## Parameters

{chr(10).join(params_doc)}

## How it works

1. Receives the request from OpenClaw
2. Forwards to HART OS backend via HTTP
3. Returns the result to the OpenClaw agent

This is a bridge skill — the actual work is done by HART OS services.
"""
        skills.append(skill_md)
    return skills


# ── Singleton ──────────────────────────────────────────────────────

_handler: Optional[HARTToolHandler] = None


def get_hart_tool_handler() -> HARTToolHandler:
    """Get the singleton HART tool handler."""
    global _handler
    if _handler is None:
        _handler = HARTToolHandler()
    return _handler
