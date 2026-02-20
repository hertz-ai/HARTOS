"""
Distributed Agent — cross-host task coordination for ANY agent type.

Composes existing SmartLedger (agent_ledger), TaskDelegationBridge (internal_comm),
and AgentSkillRegistry (internal_comm) with pluggable coordination backends.

Backend-agnostic: Redis when available, in-memory when not, gossip for
multi-node without Redis. No external dependency required for single-node.

Vision: "When all AI in the world work on a single repo" — task delegation
and compute (capex) shared by regional hosts.
"""

from .host_registry import RegionalHostRegistry
from .task_coordinator import DistributedTaskCoordinator
from .verification_protocol import VerificationProtocol
from .coordinator_backends import (
    InMemoryTaskLock,
    InMemoryHostRegistry,
    GossipTaskBridge,
    create_coordinator,
)
from .api import distributed_agent_bp, get_coordinator_backend_type
from .worker_loop import DistributedWorkerLoop, worker_loop

__all__ = [
    "RegionalHostRegistry",
    "DistributedTaskCoordinator",
    "VerificationProtocol",
    "InMemoryTaskLock",
    "InMemoryHostRegistry",
    "GossipTaskBridge",
    "create_coordinator",
    "distributed_agent_bp",
    "get_coordinator_backend_type",
    "DistributedWorkerLoop",
    "worker_loop",
]
