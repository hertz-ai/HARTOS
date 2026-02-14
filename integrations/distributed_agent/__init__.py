"""
Distributed Agent — cross-host task coordination for coding agents.

Composes existing SmartLedger (agent_ledger), TaskDelegationBridge (internal_comm),
and AgentSkillRegistry (internal_comm) with the new distributed primitives
(PubSub, Heartbeat, DistributedTaskLock, TaskVerification, TaskBaseline).

Vision: "When all AI in the world work on a single repo" — task delegation
and compute (capex) shared by regional hosts.
"""

from .host_registry import RegionalHostRegistry
from .task_coordinator import DistributedTaskCoordinator
from .verification_protocol import VerificationProtocol
from .api import distributed_agent_bp

__all__ = [
    "RegionalHostRegistry",
    "DistributedTaskCoordinator",
    "VerificationProtocol",
    "distributed_agent_bp",
]
