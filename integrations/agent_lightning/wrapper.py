"""
Agent Lightning Wrapper

Wraps AutoGen agents with Agent Lightning instrumentation for training and optimization.
Provides minimal-change integration with automatic tracing.
"""

import logging
import time
import json
from typing import Any, Dict, List, Optional, Callable
from datetime import datetime
from functools import wraps

from .config import get_agent_config, is_enabled
from .tracer import LightningTracer
from .rewards import RewardCalculator, RewardType

logger = logging.getLogger(__name__)


class AgentLightningWrapper:
    """
    Wraps an AutoGen agent with Agent Lightning instrumentation

    Provides:
    - Automatic tracing of agent interactions
    - Reward tracking for reinforcement learning
    - Performance monitoring
    - Zero impact on agent behavior (transparent wrapper)
    """

    def __init__(
        self,
        agent: Any,
        agent_id: str,
        track_rewards: bool = True,
        auto_trace: bool = True
    ):
        """
        Initialize wrapper

        Args:
            agent: AutoGen agent to wrap
            agent_id: Unique identifier for this agent
            track_rewards: Enable reward tracking
            auto_trace: Enable automatic tracing
        """
        self.agent = agent
        self.agent_id = agent_id
        self.track_rewards = track_rewards
        self.auto_trace = auto_trace

        # Get agent-specific configuration
        self.config = get_agent_config(agent_id)

        # Initialize components
        self.tracer = LightningTracer(agent_id) if auto_trace else None
        self.reward_calculator = RewardCalculator(agent_id) if track_rewards else None

        # Execution tracking
        self.execution_count = 0
        self.start_time = None
        self.current_span_id = None

        # Wrap agent methods
        self._wrap_agent_methods()

        logger.info(f"AgentLightningWrapper initialized for {agent_id}")

    def _wrap_agent_methods(self):
        """Wrap key agent methods for instrumentation"""
        if not is_enabled():
            logger.info("Agent Lightning disabled, skipping method wrapping")
            return

        # Wrap generate_reply if it exists (AutoGen pattern)
        if hasattr(self.agent, 'generate_reply'):
            original_generate_reply = self.agent.generate_reply
            self.agent.generate_reply = self._wrap_generate_reply(original_generate_reply)

        # Wrap _execute_function if it exists (tool execution)
        if hasattr(self.agent, '_execute_function'):
            original_execute = self.agent._execute_function
            self.agent._execute_function = self._wrap_tool_execution(original_execute)

    def _wrap_generate_reply(self, original_func: Callable) -> Callable:
        """Wrap generate_reply method"""
        @wraps(original_func)
        def wrapped(*args, **kwargs):
            # Start span
            span_id = None
            if self.tracer:
                span_id = self.tracer.start_span(
                    span_type='generate_reply',
                    context={'args': str(args)[:200], 'kwargs': str(kwargs)[:200]}
                )
                self.current_span_id = span_id

            start_time = time.time()

            try:
                # Execute original function
                result = original_func(*args, **kwargs)

                # Calculate execution time
                execution_time = time.time() - start_time

                # Emit events
                if self.tracer and span_id:
                    self.tracer.emit_prompt(
                        span_id=span_id,
                        prompt=str(args)[:500],
                        context={'execution_time': execution_time}
                    )

                    self.tracer.emit_response(
                        span_id=span_id,
                        response=str(result)[:500],
                        context={'execution_time': execution_time}
                    )

                    # End span
                    self.tracer.end_span(
                        span_id=span_id,
                        status='success',
                        result={'execution_time': execution_time}
                    )

                # Calculate reward
                if self.reward_calculator:
                    reward = self.reward_calculator.calculate_reward(
                        reward_type=RewardType.TASK_COMPLETION,
                        context={
                            'execution_time': execution_time,
                            'success': True
                        }
                    )

                    if self.tracer and span_id:
                        self.tracer.emit_reward(span_id, reward)

                self.execution_count += 1
                return result

            except Exception as e:
                logger.error(f"Error in generate_reply: {e}")

                # Track failure
                if self.tracer and span_id:
                    self.tracer.end_span(
                        span_id=span_id,
                        status='error',
                        result={'error': str(e)}
                    )

                # Negative reward for failure
                if self.reward_calculator:
                    reward = self.reward_calculator.calculate_reward(
                        reward_type=RewardType.TASK_FAILURE,
                        context={'error': str(e)}
                    )

                    if self.tracer and span_id:
                        self.tracer.emit_reward(span_id, reward)

                raise

        return wrapped

    def _wrap_tool_execution(self, original_func: Callable) -> Callable:
        """Wrap tool execution method"""
        @wraps(original_func)
        def wrapped(*args, **kwargs):
            # Emit tool call event
            if self.tracer and self.current_span_id:
                self.tracer.emit_tool_call(
                    span_id=self.current_span_id,
                    tool_name=str(args[0]) if args else 'unknown',
                    tool_args=str(args[1:])[:200] if len(args) > 1 else '',
                    context=kwargs
                )

            start_time = time.time()

            try:
                # Execute original function
                result = original_func(*args, **kwargs)

                execution_time = time.time() - start_time

                # Tool execution reward
                if self.reward_calculator:
                    reward = self.reward_calculator.calculate_reward(
                        reward_type=RewardType.TOOL_USE_EFFICIENCY,
                        context={
                            'execution_time': execution_time,
                            'success': True
                        }
                    )

                    if self.tracer and self.current_span_id:
                        self.tracer.emit_reward(self.current_span_id, reward)

                return result

            except Exception as e:
                logger.error(f"Error in tool execution: {e}")

                # Negative reward for tool failure
                if self.reward_calculator:
                    reward = self.reward_calculator.calculate_reward(
                        reward_type=RewardType.TASK_FAILURE,
                        context={'error': str(e), 'tool': True}
                    )

                    if self.tracer and self.current_span_id:
                        self.tracer.emit_reward(self.current_span_id, reward)

                raise

        return wrapped

    def emit_custom_reward(self, reward_value: float, context: Optional[Dict] = None):
        """
        Emit custom reward value

        Args:
            reward_value: Reward value
            context: Optional context
        """
        if self.tracer and self.current_span_id:
            self.tracer.emit_reward(self.current_span_id, reward_value, context)

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get agent statistics

        Returns:
            Dictionary with statistics
        """
        stats = {
            'agent_id': self.agent_id,
            'execution_count': self.execution_count,
            'config': self.config
        }

        if self.tracer:
            stats['tracer_stats'] = self.tracer.get_statistics()

        if self.reward_calculator:
            stats['reward_stats'] = self.reward_calculator.get_statistics()

        return stats

    def __getattr__(self, name: str):
        """Delegate attribute access to wrapped agent"""
        return getattr(self.agent, name)

    def __repr__(self) -> str:
        return f"AgentLightningWrapper({self.agent_id}, wrapped={self.agent.__class__.__name__})"


def instrument_autogen_agent(
    agent: Any,
    agent_id: str,
    track_rewards: bool = True,
    auto_trace: bool = True
) -> AgentLightningWrapper:
    """
    Convenience function to instrument an AutoGen agent

    Args:
        agent: AutoGen agent
        agent_id: Agent identifier
        track_rewards: Enable reward tracking
        auto_trace: Enable automatic tracing

    Returns:
        Wrapped agent
    """
    if not is_enabled():
        logger.info("Agent Lightning disabled, returning unwrapped agent")
        return agent

    return AgentLightningWrapper(
        agent=agent,
        agent_id=agent_id,
        track_rewards=track_rewards,
        auto_trace=auto_trace
    )


__all__ = [
    'AgentLightningWrapper',
    'instrument_autogen_agent',
]
