"""
Agent Lightning Reward Calculator

Calculates rewards for agent actions to enable reinforcement learning.
"""

import logging
from enum import Enum
from typing import Dict, Optional, Any
from collections import defaultdict

from .config import get_reward_value

logger = logging.getLogger(__name__)


class RewardType(str, Enum):
    """Types of rewards"""
    TASK_COMPLETION = "task_completion"
    TASK_FAILURE = "task_failure"
    TOOL_USE_EFFICIENCY = "tool_use_efficiency"
    RESPONSE_QUALITY = "response_quality"
    EXECUTION_TIME = "execution_time"
    USER_FEEDBACK = "user_feedback"
    CUSTOM = "custom"


class RewardCalculator:
    """
    Calculates rewards for agent actions

    Supports multiple reward types:
    - Task completion/failure
    - Tool usage efficiency
    - Response quality
    - Execution time penalties
    - User feedback
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.stats = defaultdict(float)
        self.reward_history = []

        logger.info(f"RewardCalculator initialized for {agent_id}")

    def calculate_reward(
        self,
        reward_type: RewardType,
        context: Optional[Dict[str, Any]] = None
    ) -> float:
        """
        Calculate reward based on type and context

        Args:
            reward_type: Type of reward
            context: Context data for reward calculation

        Returns:
            Reward value
        """
        context = context or {}

        # Get base reward value from config
        base_reward = get_reward_value(reward_type.value)

        # Apply context-based modifications
        reward = self._apply_context_modifiers(reward_type, base_reward, context)

        # Track statistics
        self._track_reward(reward_type, reward, context)

        logger.debug(f"Calculated reward: {reward} (type: {reward_type}, context: {context})")

        return reward

    def _apply_context_modifiers(
        self,
        reward_type: RewardType,
        base_reward: float,
        context: Dict[str, Any]
    ) -> float:
        """
        Apply context-based modifications to base reward

        Args:
            reward_type: Reward type
            base_reward: Base reward value
            context: Context data

        Returns:
            Modified reward
        """
        reward = base_reward

        # Task completion rewards
        if reward_type == RewardType.TASK_COMPLETION:
            # Bonus for fast completion
            exec_time = context.get('execution_time', 0)
            if exec_time > 0 and exec_time < 1.0:  # Under 1 second
                reward *= 1.2

            # Success multiplier
            if context.get('success', False):
                reward *= 1.0
            else:
                reward *= 0.5

        # Task failure penalties
        elif reward_type == RewardType.TASK_FAILURE:
            # More severe penalty for errors vs timeouts
            if 'error' in context:
                reward *= 1.5  # More negative
            if context.get('tool', False):
                reward *= 0.8  # Less negative for tool failures

        # Tool use efficiency
        elif reward_type == RewardType.TOOL_USE_EFFICIENCY:
            exec_time = context.get('execution_time', 0)

            # Reward fast tool execution
            if exec_time < 0.5:
                reward *= 1.5
            elif exec_time > 5.0:
                reward *= 0.5

            # Penalty for tool failures
            if not context.get('success', True):
                reward = -abs(reward)

        # Response quality (based on metrics if available)
        elif reward_type == RewardType.RESPONSE_QUALITY:
            quality_score = context.get('quality_score', 0.5)
            reward *= (quality_score * 2)  # Scale by quality

            # Length penalty for very long responses
            response_length = context.get('response_length', 0)
            if response_length > 2000:
                reward *= 0.9

        # Execution time penalty
        elif reward_type == RewardType.EXECUTION_TIME:
            exec_time = context.get('execution_time', 0)
            # Penalize slow execution
            if exec_time > 10.0:
                reward *= (exec_time / 10.0)  # More negative for slower

        # User feedback
        elif reward_type == RewardType.USER_FEEDBACK:
            feedback_score = context.get('feedback_score', 0)
            # User feedback overrides base reward
            reward = feedback_score

        # Custom rewards pass through
        elif reward_type == RewardType.CUSTOM:
            custom_value = context.get('reward_value', base_reward)
            reward = custom_value

        return reward

    def _track_reward(
        self,
        reward_type: RewardType,
        reward: float,
        context: Dict[str, Any]
    ):
        """Track reward statistics"""
        self.stats[f'total_{reward_type.value}'] += reward
        self.stats[f'count_{reward_type.value}'] += 1
        self.stats['total_reward'] += reward
        self.stats['reward_count'] += 1

        # Track history
        self.reward_history.append({
            'type': reward_type.value,
            'value': reward,
            'context': context
        })

        # Keep only last 1000 rewards
        if len(self.reward_history) > 1000:
            self.reward_history = self.reward_history[-1000:]

    def calculate_task_completion_reward(
        self,
        success: bool,
        execution_time: float,
        quality_metrics: Optional[Dict] = None
    ) -> float:
        """
        Convenience method for task completion rewards

        Args:
            success: Task succeeded
            execution_time: Time to complete
            quality_metrics: Optional quality metrics

        Returns:
            Reward value
        """
        if success:
            context = {
                'success': True,
                'execution_time': execution_time,
                **(quality_metrics or {})
            }
            return self.calculate_reward(RewardType.TASK_COMPLETION, context)
        else:
            context = {
                'success': False,
                'execution_time': execution_time
            }
            return self.calculate_reward(RewardType.TASK_FAILURE, context)

    def calculate_tool_reward(
        self,
        tool_name: str,
        success: bool,
        execution_time: float
    ) -> float:
        """
        Convenience method for tool execution rewards

        Args:
            tool_name: Tool name
            success: Tool succeeded
            execution_time: Execution time

        Returns:
            Reward value
        """
        if success:
            context = {
                'tool_name': tool_name,
                'success': True,
                'execution_time': execution_time
            }
            return self.calculate_reward(RewardType.TOOL_USE_EFFICIENCY, context)
        else:
            context = {
                'tool_name': tool_name,
                'success': False,
                'execution_time': execution_time,
                'tool': True
            }
            return self.calculate_reward(RewardType.TASK_FAILURE, context)

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get reward statistics

        Returns:
            Statistics dictionary
        """
        stats = dict(self.stats)

        # Calculate averages
        if stats.get('reward_count', 0) > 0:
            stats['average_reward'] = stats['total_reward'] / stats['reward_count']

        for reward_type in RewardType:
            count_key = f'count_{reward_type.value}'
            total_key = f'total_{reward_type.value}'

            if stats.get(count_key, 0) > 0:
                avg_key = f'average_{reward_type.value}'
                stats[avg_key] = stats[total_key] / stats[count_key]

        return stats

    def get_recent_rewards(self, count: int = 10) -> list:
        """
        Get recent rewards

        Args:
            count: Number of recent rewards

        Returns:
            List of recent rewards
        """
        return self.reward_history[-count:]

    def reset_statistics(self):
        """Reset all statistics"""
        self.stats.clear()
        self.reward_history.clear()
        logger.info(f"Reset reward statistics for {self.agent_id}")


__all__ = [
    'RewardCalculator',
    'RewardType',
]
