"""
Google A2A (Agent2Agent) Protocol Integration

This module implements Google's official A2A protocol for cross-platform agent communication.
Uses JSON-RPC 2.0 over HTTP(S) with Agent Cards for discovery.

Official Spec: https://a2a-protocol.org/latest/
SDK: https://github.com/a2aproject/a2a-python
"""

import json
import uuid
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from flask import Flask, request, jsonify, Response
import asyncio
from enum import Enum

logger = logging.getLogger(__name__)

# A2A Protocol Version
A2A_PROTOCOL_VERSION = "0.2.6"


class TaskState(str, Enum):
    """A2A Task lifecycle states"""
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentCard:
    """
    Agent Card for A2A discovery
    Published at /.well-known/agent.json
    """

    def __init__(
        self,
        name: str,
        description: str,
        url: str,
        version: str,
        skills: List[Dict[str, Any]],
        capabilities: Optional[Dict[str, Any]] = None,
        default_input_modes: Optional[List[str]] = None,
        default_output_modes: Optional[List[str]] = None
    ):
        self.name = name
        self.description = description
        self.url = url
        self.version = version
        self.skills = skills
        self.capabilities = capabilities or {"streaming": False}
        self.default_input_modes = default_input_modes or ["text", "text/plain"]
        self.default_output_modes = default_output_modes or ["text", "text/plain"]

    def to_dict(self) -> Dict[str, Any]:
        """Convert Agent Card to JSON-compatible dict"""
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "protocolVersion": A2A_PROTOCOL_VERSION,
            "capabilities": self.capabilities,
            "defaultInputModes": self.default_input_modes,
            "defaultOutputModes": self.default_output_modes,
            "skills": self.skills
        }


class A2ATask:
    """Represents an A2A task with full lifecycle management"""

    def __init__(self, task_id: str, message: Dict[str, Any], context_id: Optional[str] = None):
        self.task_id = task_id
        self.message = message
        self.context_id = context_id or str(uuid.uuid4())
        self.state = TaskState.SUBMITTED
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        self.result = None
        self.error = None
        self.metadata = {
            "prompt_token_count": 0,
            "candidates_token_count": 0,
            "total_token_count": 0
        }

    def update_state(self, new_state: TaskState, result: Optional[Any] = None, error: Optional[str] = None):
        """Update task state"""
        self.state = new_state
        self.updated_at = datetime.now()
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error

    def to_dict(self) -> Dict[str, Any]:
        """Convert task to JSON-compatible dict"""
        response = {
            "id": self.task_id,
            "contextId": self.context_id,
            "state": self.state.value,
            "timestamp": self.created_at.timestamp(),
            "usage_metadata": self.metadata
        }

        if self.result is not None:
            response["content"] = self.result

        if self.error is not None:
            response["error"] = self.error

        return response


class A2AMessageHandler:
    """Handles A2A JSON-RPC messages and task execution"""

    def __init__(self, agent_executor_func):
        """
        Initialize message handler

        Args:
            agent_executor_func: Function that executes agent tasks
                                Should accept (message_content: str, context_id: str)
                                Should return: {"role": "model", "parts": [{"text": "..."}]}
        """
        self.agent_executor = agent_executor_func
        self.tasks: Dict[str, A2ATask] = {}

    async def handle_message_send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle message/send JSON-RPC method

        Args:
            params: JSON-RPC params containing message

        Returns:
            Task response
        """
        message = params.get("message", {})
        message_id = message.get("messageId", str(uuid.uuid4()))
        context_id = message.get("contextId", str(uuid.uuid4()))

        # Extract message content
        parts = message.get("parts", [])
        message_text = ""
        for part in parts:
            if part.get("type") == "text":
                message_text += part.get("text", "")

        # Create task
        task = A2ATask(task_id=message_id, message=message, context_id=context_id)
        self.tasks[message_id] = task

        try:
            # Update to working state
            task.update_state(TaskState.WORKING)

            # Execute agent
            logger.info(f"Executing A2A task {message_id}: {message_text[:100]}")
            result = await self.agent_executor(message_text, context_id)

            # Update to completed state
            task.update_state(TaskState.COMPLETED, result=result)

            logger.info(f"A2A task {message_id} completed successfully")

        except Exception as e:
            logger.error(f"A2A task {message_id} failed: {e}")
            task.update_state(TaskState.FAILED, error=str(e))

        return task.to_dict()

    async def handle_message_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle message/get JSON-RPC method

        Args:
            params: JSON-RPC params containing task_id

        Returns:
            Task status
        """
        task_id = params.get("taskId")

        if task_id not in self.tasks:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Task {task_id} not found"
                }
            }

        task = self.tasks[task_id]
        return task.to_dict()

    async def handle_task_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle task/cancel JSON-RPC method

        Args:
            params: JSON-RPC params containing task_id

        Returns:
            Cancellation confirmation
        """
        task_id = params.get("taskId")

        if task_id not in self.tasks:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Task {task_id} not found"
                }
            }

        task = self.tasks[task_id]

        # Only cancel if not already completed/failed
        if task.state in [TaskState.SUBMITTED, TaskState.WORKING]:
            task.update_state(TaskState.FAILED, error="Task cancelled by client")
            return {"success": True, "taskId": task_id}
        else:
            return {
                "error": {
                    "code": -32600,
                    "message": f"Cannot cancel task in state {task.state}"
                }
            }


class A2AProtocolServer:
    """Google A2A Protocol Server"""

    def __init__(self, app: Flask, base_url: str):
        """
        Initialize A2A server

        Args:
            app: Flask application
            base_url: Base URL where this agent is hosted
        """
        self.app = app
        self.base_url = base_url.rstrip('/')
        self.agent_cards: Dict[str, AgentCard] = {}
        self.message_handlers: Dict[str, A2AMessageHandler] = {}

    def register_agent(
        self,
        agent_id: str,
        name: str,
        description: str,
        skills: List[Dict[str, Any]],
        executor_func,
        capabilities: Optional[Dict[str, Any]] = None
    ):
        """
        Register an agent with A2A protocol

        Args:
            agent_id: Unique agent identifier
            name: Agent name
            description: Agent description
            skills: List of agent skills
            executor_func: Async function to execute agent tasks
            capabilities: Optional agent capabilities
        """
        # Create Agent Card
        agent_card = AgentCard(
            name=name,
            description=description,
            url=f"{self.base_url}/a2a/{agent_id}",
            version="1.0.0",
            skills=skills,
            capabilities=capabilities
        )

        self.agent_cards[agent_id] = agent_card

        # Create message handler
        self.message_handlers[agent_id] = A2AMessageHandler(executor_func)

        logger.info(f"Registered A2A agent: {agent_id} ({name})")

    def setup_routes(self):
        """Setup Flask routes for A2A protocol"""

        @self.app.route('/a2a/agents', methods=['GET'])
        def list_a2a_agents():
            """Directory of this node's exportable trained agents.

            Serves the same dynamic-registry data as the per-agent
            cards, LIVE-rescanned (recipes banked after boot appear
            without a restart) and joined with the local goal identity
            (bootstrap_slug / goal_type / title) so peers can match
            agents across the md5-local prompt_id boundary. Used by
            peer_reuse.discover_peer_agent on other nodes.
            """
            try:
                from .peer_reuse import build_agent_directory, \
                    peer_reuse_enabled
                if not peer_reuse_enabled():
                    return jsonify({'agents': [], 'count': 0,
                                    'disabled': True})
                agents = build_agent_directory()
            except Exception as e:
                logger.warning(f'A2A agent directory failed: {e}')
                return jsonify({'agents': [], 'count': 0,
                                'error': str(e)}), 500
            return jsonify({'agents': agents, 'count': len(agents)})

        @self.app.route('/a2a/<agent_id>/recipe', methods=['GET'])
        def export_agent_recipe(agent_id):
            """Serve the recipe BUNDLE behind an advertised agent.

            Mirrors the skill-packet export/pull pattern: the peer
            pulls bytes and banks them locally so its own REUSE path
            replays the proven recipe. Payload is the canonical
            core.recipe_sync envelope (schema_version 1), built from
            the live prompts dir. Fail-closed gates: feature knob +
            export_allowed (goal-linked or broadcast_agent opt-in),
            plus the same basename hardening the /prompts/sync routes
            use (Flask permits '..' after URL-decode).
            """
            try:
                from core.recipe_sync import build_envelope, _safe_filename
                from core.platform_paths import get_recipe_prompts_dir
                from .peer_reuse import export_allowed
            except Exception as e:
                logger.warning(f'A2A recipe export imports failed: {e}')
                return jsonify({'error': 'recipe export unavailable'}), 503

            # agent_id is {prompt_id}_{flow_id}; the bundle covers the
            # whole prompt (all flows), which is what REUSE loads.
            prompt_id = agent_id.rsplit('_', 1)[0] if '_' in agent_id \
                else agent_id
            if not _safe_filename(f'{prompt_id}.json'):
                return jsonify({'error': 'unsafe agent_id'}), 400
            if not export_allowed(prompt_id):
                logger.info(f'A2A recipe export refused for '
                            f'prompt_id={prompt_id} (not a hive goal '
                            f'recipe / no broadcast opt-in / disabled)')
                return jsonify({'error': 'recipe not exportable',
                                'code': 'export_refused'}), 403
            try:
                envelope = build_envelope(
                    get_recipe_prompts_dir(), prompt_id)
            except Exception as e:
                logger.warning(f'A2A recipe export build failed for '
                               f'{prompt_id}: {e}')
                return jsonify({'error': f'export failed: {e}'}), 500
            if envelope is None:
                return jsonify({'error': 'not_found'}), 404
            return jsonify(envelope)

        @self.app.route('/a2a/<agent_id>/.well-known/agent.json', methods=['GET'])
        def get_agent_card(agent_id):
            """Agent Card discovery endpoint"""
            if agent_id not in self.agent_cards:
                return jsonify({"error": f"Agent {agent_id} not found"}), 404

            agent_card = self.agent_cards[agent_id]
            return jsonify(agent_card.to_dict())

        @self.app.route('/a2a/<agent_id>/jsonrpc', methods=['POST'])
        async def handle_jsonrpc(agent_id):
            """JSON-RPC endpoint for A2A messages"""
            if agent_id not in self.message_handlers:
                return jsonify({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32602,
                        "message": f"Agent {agent_id} not found"
                    },
                    "id": None
                }), 404

            # Bind before the try so the except handler can safely read
            # rpc_request even when request.json itself raises (malformed
            # body / wrong Content-Type -> 415) before the assignment.
            rpc_request = None
            try:
                rpc_request = request.json
                method = rpc_request.get("method")
                params = rpc_request.get("params", {})
                rpc_id = rpc_request.get("id")

                handler = self.message_handlers[agent_id]

                # Route to appropriate handler
                if method == "message/send":
                    result = await handler.handle_message_send(params)
                elif method == "message/get":
                    result = await handler.handle_message_get(params)
                elif method == "task/cancel":
                    result = await handler.handle_task_cancel(params)
                else:
                    return jsonify({
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32601,
                            "message": f"Method {method} not found"
                        },
                        "id": rpc_id
                    }), 400

                # Return JSON-RPC response
                return jsonify({
                    "jsonrpc": "2.0",
                    "result": result,
                    "id": rpc_id
                })

            except Exception as e:
                logger.error(f"A2A JSON-RPC error: {e}")
                return jsonify({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32603,
                        "message": f"Internal error: {str(e)}"
                    },
                    "id": rpc_request.get("id") if rpc_request else None
                }), 500

        logger.info("A2A protocol routes configured")


# Global A2A server instance
_a2a_server: Optional[A2AProtocolServer] = None


def initialize_a2a_server(app: Flask, base_url: str) -> A2AProtocolServer:
    """
    Initialize Google A2A protocol server

    Args:
        app: Flask application
        base_url: Base URL where agents are hosted

    Returns:
        A2AProtocolServer instance
    """
    global _a2a_server
    _a2a_server = A2AProtocolServer(app, base_url)
    _a2a_server.setup_routes()
    return _a2a_server


def get_a2a_server() -> Optional[A2AProtocolServer]:
    """Get the global A2A server instance"""
    return _a2a_server
