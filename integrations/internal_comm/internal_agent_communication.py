"""
A2A (Agent-to-Agent) Protocol Module

This module enables direct agent-to-agent communication, context exchange,
and skill-based task delegation within the multi-agent system.

Features:
- Agent skill registry and discovery
- Context sharing between agents
- Task delegation to specialist agents
- Inter-agent messaging
- Collaborative execution tracking
"""

import json
import logging
import threading
from typing import Dict, List, Any, Optional, Callable
from datetime import datetime
from collections import deque
import uuid

logger = logging.getLogger(__name__)


class AgentSkill:
    """Represents a skill that an agent possesses"""

    def __init__(self, name: str, description: str, proficiency: float = 1.0, metadata: Optional[Dict] = None):
        """
        Initialize an agent skill

        Args:
            name: Skill identifier
            description: Human-readable skill description
            proficiency: Skill proficiency level (0.0 to 1.0)
            metadata: Additional skill metadata
        """
        self.name = name
        self.description = description
        self.proficiency = max(0.0, min(1.0, proficiency))
        self.metadata = metadata or {}
        self.usage_count = 0
        self.success_count = 0

    def record_usage(self, success: bool = True):
        """Record skill usage for tracking"""
        self.usage_count += 1
        if success:
            self.success_count += 1

    def get_success_rate(self) -> float:
        """Calculate skill success rate"""
        if self.usage_count == 0:
            return 0.0
        return self.success_count / self.usage_count

    def to_dict(self) -> Dict[str, Any]:
        """Convert skill to dictionary"""
        return {
            'name': self.name,
            'description': self.description,
            'proficiency': self.proficiency,
            'usage_count': self.usage_count,
            'success_rate': self.get_success_rate(),
            'metadata': self.metadata
        }


class AgentSkillRegistry:
    """Registry for tracking agent skills and capabilities"""

    def __init__(self):
        """Initialize the skill registry"""
        self.agents: Dict[str, Dict[str, AgentSkill]] = {}  # agent_id -> {skill_name -> AgentSkill}
        self.lock = threading.Lock()

    def register_agent(self, agent_id: str, skills: List[Dict[str, Any]]):
        """
        Register an agent with its skills

        Args:
            agent_id: Unique agent identifier
            skills: List of skill definitions
        """
        with self.lock:
            if agent_id not in self.agents:
                self.agents[agent_id] = {}

            for skill_def in skills:
                skill = AgentSkill(
                    name=skill_def.get('name'),
                    description=skill_def.get('description', ''),
                    proficiency=skill_def.get('proficiency', 1.0),
                    metadata=skill_def.get('metadata', {})
                )
                self.agents[agent_id][skill.name] = skill

            logger.info(f"Registered agent {agent_id} with {len(skills)} skills")

    def find_agents_with_skill(self, skill_name: str, min_proficiency: float = 0.0) -> List[tuple]:
        """
        Find all agents that have a specific skill

        Args:
            skill_name: Name of the skill to find
            min_proficiency: Minimum proficiency level required

        Returns:
            List of (agent_id, skill) tuples sorted by proficiency
        """
        with self.lock:
            matches = []
            for agent_id, skills in self.agents.items():
                if skill_name in skills:
                    skill = skills[skill_name]
                    if skill.proficiency >= min_proficiency:
                        matches.append((agent_id, skill))

            # Sort by proficiency (highest first)
            matches.sort(key=lambda x: x[1].proficiency, reverse=True)
            return matches

    def get_agent_skills(self, agent_id: str) -> Dict[str, AgentSkill]:
        """Get all skills for an agent"""
        with self.lock:
            return self.agents.get(agent_id, {})

    def record_skill_usage(self, agent_id: str, skill_name: str, success: bool = True):
        """Record usage of a skill by an agent"""
        with self.lock:
            if agent_id in self.agents and skill_name in self.agents[agent_id]:
                self.agents[agent_id][skill_name].record_usage(success)

    def get_best_agent_for_skill(self, skill_name: str) -> Optional[str]:
        """
        Get the best agent for a specific skill

        Args:
            skill_name: Name of the skill

        Returns:
            Agent ID of the best agent, or None if no agent has the skill
        """
        matches = self.find_agents_with_skill(skill_name)
        if matches:
            return matches[0][0]  # Return agent_id of highest proficiency
        return None


class A2AMessage:
    """Represents a message between agents"""

    def __init__(self, from_agent: str, to_agent: str, message_type: str, content: Any, metadata: Optional[Dict] = None):
        """
        Initialize an A2A message

        Args:
            from_agent: Sender agent ID
            to_agent: Recipient agent ID
            message_type: Type of message (request, response, broadcast, etc.)
            content: Message content
            metadata: Additional metadata
        """
        self.message_id = str(uuid.uuid4())
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.message_type = message_type
        self.content = content
        self.metadata = metadata or {}
        self.timestamp = datetime.now()
        self.status = 'pending'

    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary"""
        return {
            'message_id': self.message_id,
            'from_agent': self.from_agent,
            'to_agent': self.to_agent,
            'message_type': self.message_type,
            'content': self.content,
            'metadata': self.metadata,
            'timestamp': self.timestamp.isoformat(),
            'status': self.status
        }


class A2AContextExchange:
    """Manages context exchange and task delegation between agents"""

    def __init__(self, skill_registry: AgentSkillRegistry):
        """
        Initialize A2A context exchange

        Args:
            skill_registry: Agent skill registry
        """
        self.skill_registry = skill_registry
        self.message_queues: Dict[str, deque] = {}  # agent_id -> message queue
        self.shared_context: Dict[str, Any] = {}  # Shared context across agents
        self.delegations: Dict[str, Dict[str, Any]] = {}  # delegation_id -> delegation info
        self.lock = threading.Lock()

    def register_agent(self, agent_id: str):
        """Register an agent for A2A communication"""
        with self.lock:
            if agent_id not in self.message_queues:
                self.message_queues[agent_id] = deque(maxlen=100)
                logger.info(f"Registered agent {agent_id} for A2A communication")

    def send_message(self, from_agent: str, to_agent: str, message_type: str, content: Any, metadata: Optional[Dict] = None):
        """
        Send a message from one agent to another

        Args:
            from_agent: Sender agent ID
            to_agent: Recipient agent ID
            message_type: Type of message
            content: Message content
            metadata: Additional metadata

        Returns:
            Message ID
        """
        # Security: Encrypt message content if A2ACrypto available
        try:
            from security.crypto import A2ACrypto
            crypto = A2ACrypto()
            if isinstance(content, str):
                content = crypto.encrypt_message(content)
            elif isinstance(content, dict):
                content = crypto.encrypt_payload(content)
        except ImportError:
            pass  # Send unencrypted (backward compat)
        except Exception:
            pass  # Encryption failed, send unencrypted

        message = A2AMessage(from_agent, to_agent, message_type, content, metadata)

        with self.lock:
            if to_agent not in self.message_queues:
                self.register_agent(to_agent)

            self.message_queues[to_agent].append(message)
            logger.info(f"Message sent from {from_agent} to {to_agent}: {message_type}")

        return message.message_id

    def get_messages(self, agent_id: str, message_type: Optional[str] = None) -> List[A2AMessage]:
        """
        Get pending messages for an agent

        Args:
            agent_id: Agent ID
            message_type: Optional filter by message type

        Returns:
            List of pending messages
        """
        with self.lock:
            if agent_id not in self.message_queues:
                return []

            messages = list(self.message_queues[agent_id])

            if message_type:
                messages = [m for m in messages if m.message_type == message_type]

            # Clear processed messages
            self.message_queues[agent_id].clear()

            return messages

    def share_context(self, agent_id: str, context_key: str, context_value: Any):
        """
        Share context with other agents

        Args:
            agent_id: Agent sharing the context
            context_key: Context key
            context_value: Context value
        """
        with self.lock:
            self.shared_context[context_key] = {
                'value': context_value,
                'shared_by': agent_id,
                'timestamp': datetime.now().isoformat()
            }
            logger.info(f"Agent {agent_id} shared context: {context_key}")

    def get_shared_context(self, context_key: str) -> Optional[Any]:
        """
        Get shared context

        Args:
            context_key: Context key

        Returns:
            Context value or None
        """
        with self.lock:
            context = self.shared_context.get(context_key)
            if context:
                return context.get('value')
            return None

    def delegate_task(self, from_agent: str, task: str, required_skills: List[str], context: Optional[Dict] = None) -> Optional[str]:
        """
        Delegate a task to the most suitable agent

        Args:
            from_agent: Agent delegating the task
            task: Task description
            required_skills: List of required skills
            context: Optional task context

        Returns:
            Delegation ID or None if no suitable agent found
        """
        # Find best agent for the first required skill
        if not required_skills:
            logger.warning("No required skills specified for task delegation")
            return None

        # Find agents with all required skills
        suitable_agents = None
        for skill in required_skills:
            agents_with_skill = set(agent_id for agent_id, _ in self.skill_registry.find_agents_with_skill(skill))

            if suitable_agents is None:
                suitable_agents = agents_with_skill
            else:
                suitable_agents = suitable_agents.intersection(agents_with_skill)

        if not suitable_agents:
            logger.warning(f"No agents found with all required skills: {required_skills}")
            return None

        # Get the best agent (highest average proficiency across required skills)
        best_agent = None
        best_proficiency = 0.0

        for agent_id in suitable_agents:
            if agent_id == from_agent:  # Don't delegate to self
                continue

            avg_proficiency = 0.0
            agent_skills = self.skill_registry.get_agent_skills(agent_id)

            for skill in required_skills:
                if skill in agent_skills:
                    avg_proficiency += agent_skills[skill].proficiency

            avg_proficiency /= len(required_skills)

            if avg_proficiency > best_proficiency:
                best_proficiency = avg_proficiency
                best_agent = agent_id

        if not best_agent:
            logger.warning("No suitable agent found for delegation")
            return None

        # Create delegation
        delegation_id = str(uuid.uuid4())

        with self.lock:
            self.delegations[delegation_id] = {
                'from_agent': from_agent,
                'to_agent': best_agent,
                'task': task,
                'required_skills': required_skills,
                'context': context or {},
                'status': 'delegated',
                'created_at': datetime.now().isoformat(),
                'result': None
            }

        # Send delegation message
        self.send_message(
            from_agent=from_agent,
            to_agent=best_agent,
            message_type='task_delegation',
            content=task,
            metadata={
                'delegation_id': delegation_id,
                'required_skills': required_skills,
                'context': context
            }
        )

        logger.info(f"Task delegated from {from_agent} to {best_agent}: {delegation_id}")
        return delegation_id

    def complete_delegation(self, delegation_id: str, result: Any):
        """
        Mark a delegation as complete with result

        Args:
            delegation_id: Delegation ID
            result: Delegation result
        """
        with self.lock:
            if delegation_id in self.delegations:
                self.delegations[delegation_id]['status'] = 'completed'
                self.delegations[delegation_id]['result'] = result
                self.delegations[delegation_id]['completed_at'] = datetime.now().isoformat()

                # Send completion message back to delegator
                delegation = self.delegations[delegation_id]
                self.send_message(
                    from_agent=delegation['to_agent'],
                    to_agent=delegation['from_agent'],
                    message_type='delegation_complete',
                    content=result,
                    metadata={'delegation_id': delegation_id}
                )

                logger.info(f"Delegation completed: {delegation_id}")

    def get_delegation_status(self, delegation_id: str) -> Optional[Dict[str, Any]]:
        """Get delegation status"""
        with self.lock:
            return self.delegations.get(delegation_id)


# Global instances
skill_registry = AgentSkillRegistry()
a2a_context = A2AContextExchange(skill_registry)


def register_agent_with_skills(agent_id: str, skills: List[Dict[str, Any]]):
    """
    Register an agent with its skills

    Args:
        agent_id: Agent identifier
        skills: List of skill definitions
    """
    skill_registry.register_agent(agent_id, skills)
    a2a_context.register_agent(agent_id)


def create_delegation_function(from_agent_id: str) -> Callable:
    """
    Create a delegation function for an agent

    Args:
        from_agent_id: Agent ID

    Returns:
        Delegation function
    """
    def delegate_to_specialist(task: str, required_skills: List[str], context: Optional[Dict] = None) -> str:
        """
        Delegate a task to a specialist agent

        Args:
            task: Task description
            required_skills: Required skills for the task
            context: Optional task context

        Returns:
            Delegation ID or error message
        """
        delegation_id = a2a_context.delegate_task(from_agent_id, task, required_skills, context)

        if delegation_id:
            return json.dumps({
                'success': True,
                'delegation_id': delegation_id,
                'message': f'Task delegated successfully to specialist agent'
            })
        else:
            return json.dumps({
                'success': False,
                'error': 'No suitable agent found with required skills',
                'required_skills': required_skills
            })

    return delegate_to_specialist


def create_context_sharing_function(agent_id: str) -> Callable:
    """
    Create a context sharing function for an agent

    Args:
        agent_id: Agent ID

    Returns:
        Context sharing function
    """
    def share_context_with_agents(context_key: str, context_value: Any) -> str:
        """
        Share context with other agents

        Args:
            context_key: Context identifier
            context_value: Context value

        Returns:
            Success message
        """
        a2a_context.share_context(agent_id, context_key, context_value)

        return json.dumps({
            'success': True,
            'message': f'Context "{context_key}" shared successfully',
            'shared_by': agent_id
        })

    return share_context_with_agents


def create_context_retrieval_function() -> Callable:
    """
    Create a context retrieval function

    Returns:
        Context retrieval function
    """
    def get_shared_context(context_key: str) -> str:
        """
        Retrieve shared context from other agents

        Args:
            context_key: Context identifier

        Returns:
            Context value or error
        """
        context = a2a_context.get_shared_context(context_key)

        if context is not None:
            return json.dumps({
                'success': True,
                'context_key': context_key,
                'context_value': context
            })
        else:
            return json.dumps({
                'success': False,
                'error': f'Context "{context_key}" not found'
            })

    return get_shared_context
