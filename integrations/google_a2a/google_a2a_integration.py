"""
Google A2A (Agent2Agent) Protocol Integration

This module implements Google's official A2A protocol for cross-platform agent communication.
Uses JSON-RPC 2.0 over HTTP(S) with Agent Cards for discovery.

Official Spec: https://a2a-protocol.org/latest/
SDK: https://github.com/a2aproject/a2a-python
"""

import inspect
import json
import os
import threading
import uuid
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from flask import Flask, request, jsonify, Response
from enum import Enum

from core.event_loop import run_async

logger = logging.getLogger(__name__)

# A2A Protocol Version
A2A_PROTOCOL_VERSION = "0.2.6"

# The task table is bounded (review finding M5: it grew by one entry per
# message/send and was never pruned).  A FINISHED task is kept this long
# after its last update so its caller can read the verdict, and past
# _TASK_MAX entries the oldest finished tasks go first.  A task still
# running is never evicted: its caller may be polling it.
_TASK_TTL_S = float(os.environ.get('HEVOLVE_A2A_TASK_TTL_S', '600'))
_TASK_MAX = int(os.environ.get('HEVOLVE_A2A_TASK_MAX', '1024'))
# Running tasks are never evicted, so the table is bounded per caller too: a
# caller holding this many unfinished tasks is told 'busy' (review of
# 436580009: one admitted peer could grow memory and threads without limit).
_OPEN_TASKS_PER_CALLER = int(os.environ.get('HEVOLVE_A2A_OPEN_TASKS_PER_CALLER', '4'))
# ...and for the node: every unfinished task holds a thread (non-blocking) or
# a request worker (blocking), so the whole table of running turns is capped
# by the same admission check (review of a4ea04651: 60 sends, +60 threads).
_OPEN_TASKS_MAX = int(os.environ.get('HEVOLVE_A2A_OPEN_TASKS_MAX', '16'))


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
        # The identity admitted for its message/send ('peer:<node_id>',
        # 'user:<id>', 'api_key', 'addr:<ip>'); message/get and task/cancel
        # answer only that caller.  None: no identity was given (a direct,
        # in-process caller), and then any caller is answered, as before.
        self.owner = None
        # Set by task/cancel.  The executor reads it (when it takes a
        # cancel_event) to give the LLM permit back before its turn starts.
        self.cancel_event = threading.Event()
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
        self._tasks_lock = threading.Lock()

    def _prune(self) -> None:
        """Evict finished tasks past _TASK_TTL_S, then the oldest finished
        ones while the table is over _TASK_MAX.  Running tasks stay."""
        open_states = (TaskState.SUBMITTED, TaskState.WORKING)
        now = datetime.now()
        with self._tasks_lock:
            done = [(t.updated_at, tid) for tid, t in self.tasks.items()
                    if t.state not in open_states]
            for updated, tid in done:
                if (now - updated).total_seconds() > _TASK_TTL_S:
                    self.tasks.pop(tid, None)
            if len(self.tasks) > _TASK_MAX:
                for updated, tid in sorted(done):
                    if len(self.tasks) <= _TASK_MAX:
                        break
                    self.tasks.pop(tid, None)

    def _admission_refusal(self, caller) -> Optional[str]:
        """The ONE bound on running turns: 'busy: ...' when this caller
        (_OPEN_TASKS_PER_CALLER) or the node (_OPEN_TASKS_MAX) already holds
        that many unfinished tasks, else None.  Finished tasks are evicted
        by _prune; running ones only end."""
        self._prune()
        open_states = (TaskState.SUBMITTED, TaskState.WORKING)
        with self._tasks_lock:
            running = [t for t in self.tasks.values() if t.state in open_states]
        mine = sum(1 for t in running if caller is not None and t.owner == caller)
        if caller is not None and mine >= _OPEN_TASKS_PER_CALLER:
            logger.info(f"A2A: {caller!r} holds {mine} unfinished tasks; busy")
            return f"busy: {mine} tasks of this caller are still running"
        if len(running) >= _OPEN_TASKS_MAX:
            logger.info(f"A2A: {len(running)} unfinished tasks on this node; busy")
            return f"busy: {len(running)} tasks are running on this node"
        return None

    def _task_for(self, task_id, caller):
        """The task, when ``caller`` may see it; else None.  A task bound to
        another caller reads as absent: its existence is not disclosed."""
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if task.owner is not None and task.owner != caller:
            logger.info(f"A2A task {str(task_id)[:12]}: refused to "
                        f"{caller!r} (bound to another caller)")
            return None
        return task

    async def handle_message_send(self, params: Dict[str, Any],
                                  caller: Optional[str] = None) -> Dict[str, Any]:
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

        # Extract message content. A2A 0.2.x names a part's discriminator
        # "kind"; this node's own clients (peer_reuse, hart CLI) still send
        # the pre-0.2 "type". Read both, or a spec-conformant peer's text
        # reaches the executor as an empty prompt.
        parts = message.get("parts", [])
        message_text = ""
        for part in parts:
            if part.get("kind", part.get("type")) == "text":
                message_text += part.get("text", "")

        busy = self._admission_refusal(caller)
        if busy:
            return {"error": {"code": -32000, "message": busy}}
        # Create task, bound to the caller that was admitted for it.
        task = A2ATask(task_id=message_id, message=message, context_id=context_id)
        task.owner = caller
        with self._tasks_lock:
            existing = self.tasks.get(message_id)
            if existing is not None and existing.owner != caller:
                # Another caller's messageId: never overwrite its task.
                message_id = str(uuid.uuid4())
                task.task_id = message_id
            self.tasks[message_id] = task
        self._prune()

        # configuration.blocking (A2A MessageSendParams): false returns the
        # task now and runs it on a thread of its own; the caller polls
        # message/get and sends task/cancel when it stops waiting.  Absent or
        # true keeps the old contract (the reply carries the finished task).
        # Review finding M2: a blocking send let a caller that timed out
        # leave a whole /chat turn running here, holding the one LLM permit.
        config = params.get("configuration") or {}
        if isinstance(config, dict) and config.get("blocking") is False:
            threading.Thread(
                target=lambda: run_async(self._run(task, message_text)),
                name=f"a2a-task-{message_id[:8]}", daemon=True).start()
            return task.to_dict()
        await self._run(task, message_text)
        return task.to_dict()

    def _takes_cancel_event(self) -> bool:
        try:
            return 'cancel_event' in inspect.signature(
                self.agent_executor).parameters
        except (TypeError, ValueError):
            return False

    async def _run(self, task: A2ATask, message_text: str) -> None:
        """Run the executor for ``task`` and record the verdict, unless the
        task was cancelled meanwhile: a cancelled task keeps its verdict,
        nobody is waiting for the answer any more."""
        message_id = task.task_id
        try:
            if task.cancel_event.is_set():
                return
            task.update_state(TaskState.WORKING)
            logger.info(f"Executing A2A task {message_id}: {message_text[:100]}")
            if self._takes_cancel_event():
                result = await self.agent_executor(
                    message_text, task.context_id,
                    cancel_event=task.cancel_event)
            else:
                result = await self.agent_executor(message_text, task.context_id)
            if task.cancel_event.is_set():
                logger.info(f"A2A task {message_id} finished after its "
                            f"cancel; result dropped")
                return
            task.update_state(TaskState.COMPLETED, result=result)
            logger.info(f"A2A task {message_id} completed successfully")
        except Exception as e:
            if task.cancel_event.is_set():
                return
            logger.error(f"A2A task {message_id} failed: {e}")
            task.update_state(TaskState.FAILED, error=str(e))

    async def handle_message_get(self, params: Dict[str, Any],
                                 caller: Optional[str] = None) -> Dict[str, Any]:
        """
        Handle message/get JSON-RPC method

        Args:
            params: JSON-RPC params containing task_id

        Returns:
            Task status
        """
        task_id = params.get("taskId")
        task = self._task_for(task_id, caller)
        if task is None:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Task {task_id} not found"
                }
            }
        return task.to_dict()

    async def handle_task_cancel(self, params: Dict[str, Any],
                                 caller: Optional[str] = None) -> Dict[str, Any]:
        """
        Handle task/cancel JSON-RPC method

        Args:
            params: JSON-RPC params containing task_id

        Returns:
            Cancellation confirmation
        """
        task_id = params.get("taskId")
        task = self._task_for(task_id, caller)
        if task is None:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Task {task_id} not found"
                }
            }

        # Only cancel if not already completed/failed
        if task.state in [TaskState.SUBMITTED, TaskState.WORKING]:
            # Set BEFORE the verdict, so _run (which checks the event after
            # its executor returns) can never overwrite it.  Reaches the
            # executor too: a turn still waiting for the LLM permit gives it
            # back and never starts (dispatch.local_chat_dispatch).
            task.cancel_event.set()
            task.update_state(TaskState.FAILED, error="Task cancelled by client")
            return {"success": True, "taskId": task_id}
        else:
            return {
                "error": {
                    "code": -32600,
                    "message": f"Cannot cancel task in state {task.state}"
                }
            }


def _gate_caller() -> str:
    """Who the /chat gate admitted for THIS request, as a task owner: the
    signed-in user (a JWT or an owner-allowed device token), the API-key
    holder, else the client address (core.auth_local.client_address; a
    desktop's own callers and LAN-trusted tiers carry no other identity)."""
    from flask import g
    try:
        payload = getattr(g, 'jwt_payload', None) or {}
        uid = payload.get('user_id') or payload.get('sub')
        if uid:
            return f'user:{uid}'
    except Exception:
        pass
    if getattr(g, 'auth_source', None) == 'api_key':
        # Set by the gate only when the key MATCHED; a header alone is not
        # an identity (review of 436580009).
        return 'api_key'
    from core.auth_local import client_key
    return f'addr:{client_key()}'


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

    def _jsonrpc_refusal(self, agent_id, rpc_request):
        """(http_code, message) when refused, else None; see _admit."""
        refused, _who = self._admit(agent_id, rpc_request)
        return refused

    def _admit(self, agent_id, rpc_request):
        """(refusal, caller): refusal is (http_code, message) or None, and
        caller the identity admitted (the task owner, M5): 'peer:<node_id>'
        for a signed peer, else the /chat gate's (_gate_caller)."""
        refused, who = self._admit_checks(agent_id, rpc_request)
        return refused, (None if refused else who)

    def _admit_checks(self, agent_id, rpc_request):
        """(http_code, message) when this request may not run the agent, read
        its tasks or cancel them, else None.  Called inside the jsonrpc view's
        request for message/send, message/get and task/cancel.

        message/send runs a /chat turn as the agent's owner (autonomous, with
        its tools), so a caller is admitted when EITHER
          (a) /chat would admit it: the one API gate, security.middleware's
              check_api_auth, asked about '/chat' (the desktop's own callers,
              LAN-trusted tiers, a key or JWT, an allowed phone).  /a2a/ is an
              exempt prefix for the peer protocol's discovery half, and that
              exemption let an unauthenticated caller on another machine run
              a turn (review of 309bcd032); or
          (b) the body is signed by a node this node has VERIFIED (answered
              its integrity challenge), for THIS node and THIS agent
              (discovery.admitted_peer_sender; owner ruling 2026-09-26: "only
              a hash verified node is enough").  peer_reuse.invoke_peer_agent
              signs; without (b) every peer invoke of a bundled, central or
              keyed node was a 401.
        A refusal carries the gate's own status (a phone's consent_pending is
        403, not 401).  message/get and task/cancel read and end those turns,
        so they take the same admission.  And, like the recipe pull, only an
        agent this node would export is served (peer_reuse.export_allowed).
        Fail closed on any error."""
        who = None
        try:
            from security.middleware import _apply_api_auth
            refused = _apply_api_auth(self.app, register=False)(as_path='/chat')
        except Exception as e:
            logger.warning(f'A2A jsonrpc auth check failed: {e}')
            return (503, 'authorization unavailable'), None
        if refused is None:
            who = _gate_caller()
        else:
            body = rpc_request if isinstance(rpc_request, dict) else {}
            if 'signature' in body or 'sender' in body:
                try:
                    from integrations.social.discovery import admitted_peer_sender
                    from integrations.social.models import db_session
                    from integrations.social.sync_engine import SyncEngine
                    with db_session(commit=False) as db:
                        peer, why = admitted_peer_sender(
                            db, body, audience=SyncEngine.canonical_node_id())
                except Exception as e:
                    logger.warning(f'A2A peer admission check failed: {e}')
                    return (503, 'authorization unavailable'), None
                if peer is None:
                    logger.info(f'A2A {agent_id}: signed request refused ({why})')
                    return (401, f'peer not admitted: {why}'), None
                if body.get('agent_id') != agent_id:
                    logger.info(f'A2A {agent_id}: peer {peer[:8]} signed for '
                                f'{body.get("agent_id")!r}; refused')
                    return (401, 'peer not admitted: signed for another agent'), None
                logger.info(f'A2A {agent_id}: admitted peer {peer[:8]}')
                who = f'peer:{peer}'
            else:
                resp, status = refused if isinstance(refused, tuple) else (
                    refused, getattr(refused, 'status_code', 401))
                try:
                    error = (resp.get_json(silent=True) or {}).get('error')
                except Exception:
                    error = None
                return (status, error or 'authentication required to run an agent'), None
        try:
            from .peer_reuse import export_allowed
            prompt_id = agent_id.rsplit('_', 1)[0] if '_' in agent_id else agent_id
            if not export_allowed(prompt_id):
                return (403, 'agent not shared with peers'), None
        except Exception as e:
            logger.warning(f'A2A jsonrpc export gate failed: {e}')
            return (503, 'authorization unavailable'), None
        return None, who

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
        def handle_jsonrpc(agent_id):
            """JSON-RPC endpoint for A2A messages.

            A SYNC view on purpose. Flask runs an ``async def`` view through
            asgiref's async_to_sync, and asgiref is not installed (not in
            any requirements file, not frozen), so an async view raised
            before its body ran and every POST -- the 404 / 400 JSON-RPC
            error branches included -- answered Flask's HTML 500. The
            handler coroutines are driven by core.event_loop.run_async, the
            canonical sync->async runner; the WSGI server calls this view on
            a worker thread (waitress thread / hypercorn run_in_executor),
            which has no running loop of its own.
            """
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
                caller = None
                if method in ("message/send", "message/get", "task/cancel"):
                    refused, caller = self._admit(agent_id, rpc_request)
                    if refused is not None:
                        code, message = refused
                        return jsonify({"jsonrpc": "2.0", "error": {
                            "code": -32001, "message": message},
                            "id": rpc_id}), code
                if method == "message/send":
                    result = run_async(handler.handle_message_send(
                        params, caller=caller))
                elif method == "message/get":
                    result = run_async(handler.handle_message_get(
                        params, caller=caller))
                elif method == "task/cancel":
                    result = run_async(handler.handle_task_cancel(
                        params, caller=caller))
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
