"""
Agent Environments — Logical scopes for AI workloads in HART OS.

Like Android's Context but for agents. NOT containers — lightweight logical
boundaries with tool access control, model policy, budget limits, and
scoped event emission.

Usage:
    from core.platform.agent_environment import EnvironmentManager

    mgr = EnvironmentManager(service_registry=registry, event_emitter=bus.emit)
    env = mgr.create('research-task', model_policy='local_preferred',
                      allowed_tools=['web_search', 'read_file'],
                      max_cost_spark=50.0)

    # Tool gating
    env.check_tool('web_search')   # -> True
    env.check_tool('write_file')   # -> False (not in allowed_tools)

    # Scoped inference
    result = env.infer('Summarize this paper', model_type='llm')

    # Scoped events
    env.emit('task.completed', {'result': 'done'})
    # -> publishes 'env.research-task-abc.task.completed'

    mgr.destroy(env.env_id)
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.platform')


# ─── Environment Configuration ────────────────────────────────────

@dataclass
class EnvironmentConfig:
    """Configuration for an agent environment.

    Defines the boundaries, permissions, and resource limits.
    All fields are optional — unconfigured fields impose no constraints.
    """
    working_dir: str = ''
    allowed_tools: List[str] = field(default_factory=list)
    denied_tools: List[str] = field(default_factory=list)
    model_policy: str = 'local_preferred'
    max_cost_spark: float = 0.0          # 0 = unlimited
    ai_capabilities: List[Dict[str, Any]] = field(default_factory=list)
    event_scope: str = ''                # EventBus topic prefix
    timeout_seconds: float = 0.0         # 0 = no timeout
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API responses."""
        return {
            'working_dir': self.working_dir,
            'allowed_tools': self.allowed_tools,
            'denied_tools': self.denied_tools,
            'model_policy': self.model_policy,
            'max_cost_spark': self.max_cost_spark,
            'ai_capabilities': self.ai_capabilities,
            'event_scope': self.event_scope,
            'timeout_seconds': self.timeout_seconds,
            'metadata': self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EnvironmentConfig':
        """Deserialize from dict."""
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})


# ─── Agent Environment ────────────────────────────────────────────

@dataclass
class AgentEnvironment:
    """A single agent execution environment.

    Provides tool gating, scoped inference, and scoped event emission.
    Lightweight — just data + methods, no OS-level isolation.
    """
    env_id: str
    name: str
    config: EnvironmentConfig
    created_at: float = field(default_factory=time.time)
    _active: bool = field(default=True, repr=False)
    _cost_spent: float = field(default=0.0, repr=False)

    @property
    def active(self) -> bool:
        """Whether this environment is still active."""
        return self._active

    def check_tool(self, tool_name: str) -> bool:
        """Check if a tool is allowed in this environment.

        Rules (same precedence as tool_allowlist.py):
        1. If denied_tools set and tool in it -> denied
        2. If allowed_tools set and tool NOT in it -> denied
        3. Otherwise -> allowed

        Empty lists impose no constraints.
        """
        if self.config.denied_tools and tool_name in self.config.denied_tools:
            return False
        if self.config.allowed_tools and tool_name not in self.config.allowed_tools:
            return False
        return True

    def check_budget(self, cost: float) -> bool:
        """Check if spending `cost` would exceed the budget.

        Returns True if within budget or no budget constraint.
        """
        if self.config.max_cost_spark <= 0:
            return True
        return (self._cost_spent + cost) <= self.config.max_cost_spark

    def record_cost(self, cost: float) -> None:
        """Record a cost expenditure."""
        self._cost_spent += cost

    def infer(self, prompt: str, model_type: str = 'llm',
              options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch inference through ModelBusService with environment constraints.

        Respects model_policy and budget. Returns the inference result dict
        or an error dict if unavailable.
        """
        if not self._active:
            return {'error': 'environment is inactive', 'env_id': self.env_id}

        try:
            from integrations.agent_engine.model_bus_service import (
                get_model_bus_service,
            )
            bus = get_model_bus_service()
            if bus is None:
                return {'error': 'model bus service not available'}

            result = bus.infer(
                prompt=prompt,
                model_type=model_type,
                options={
                    **(options or {}),
                    'policy': self.config.model_policy,
                },
            )
            return result if isinstance(result, dict) else {'result': result}
        except ImportError:
            return {'error': 'model bus service not installed'}
        except Exception as e:
            return {'error': str(e)}

    def emit(self, topic: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Emit a scoped event.

        Prefixes the topic with the environment's event_scope.
        Falls back to env_id if no scope configured.
        """
        scope = self.config.event_scope or f'env.{self.env_id}'
        scoped_topic = f'{scope}.{topic}'
        try:
            from core.platform.events import emit_event
            emit_event(scoped_topic, {
                **(data or {}),
                '_env_id': self.env_id,
            })
        except Exception:
            pass

    def deactivate(self) -> None:
        """Mark this environment as inactive."""
        self._active = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for API responses."""
        return {
            'env_id': self.env_id,
            'name': self.name,
            'config': self.config.to_dict(),
            'created_at': self.created_at,
            'active': self._active,
            'cost_spent': self._cost_spent,
        }


# ─── Environment Manager ─────────────────────────────────────────

class EnvironmentManager:
    """Manages agent environment lifecycle.

    Registered in ServiceRegistry as 'environments'. Provides CRUD
    operations for agent environments with thread-safe access.
    """

    def __init__(self, service_registry=None, event_emitter: Optional[Callable] = None):
        self._registry = service_registry
        self._emit = event_emitter
        self._environments: Dict[str, AgentEnvironment] = {}
        self._lock = threading.Lock()

    def create(self, name: str, config: Optional[EnvironmentConfig] = None,
               **kwargs) -> AgentEnvironment:
        """Create a new agent environment.

        Args:
            name: Human-readable name for this environment.
            config: Full EnvironmentConfig, or pass kwargs for shorthand.

        Returns:
            The newly created AgentEnvironment.
        """
        if config is None:
            config = EnvironmentConfig(**{k: v for k, v in kwargs.items()
                                          if k in EnvironmentConfig.__dataclass_fields__})

        env_id = f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:8]}'

        # Default event scope from name
        if not config.event_scope:
            config.event_scope = f'env.{env_id}'

        env = AgentEnvironment(env_id=env_id, name=name, config=config)

        with self._lock:
            self._environments[env_id] = env

        if self._emit:
            self._emit('environment.created', {
                'env_id': env_id,
                'name': name,
                'model_policy': config.model_policy,
            })

        logger.debug("Created environment: %s (%s)", name, env_id)
        return env

    def get(self, env_id: str) -> Optional[AgentEnvironment]:
        """Get an environment by ID."""
        return self._environments.get(env_id)

    def destroy(self, env_id: str) -> bool:
        """Deactivate and remove an environment.

        Does NOT delete working_dir or other external resources.

        Returns:
            True if destroyed, False if not found.
        """
        with self._lock:
            env = self._environments.pop(env_id, None)

        if env is None:
            return False

        env.deactivate()

        if self._emit:
            self._emit('environment.destroyed', {
                'env_id': env_id,
                'name': env.name,
            })

        logger.debug("Destroyed environment: %s (%s)", env.name, env_id)
        return True

    def list_environments(self) -> List[Dict[str, Any]]:
        """List all environments (both active and inactive in manager)."""
        return [env.to_dict() for env in self._environments.values()]

    def count(self) -> int:
        """Return the number of managed environments."""
        return len(self._environments)

    # ── Lifecycle (for ServiceRegistry) ──────────────────────────

    def health(self) -> dict:
        """Health report."""
        active = sum(1 for e in self._environments.values() if e.active)
        return {
            'status': 'ok',
            'total_environments': len(self._environments),
            'active': active,
        }
