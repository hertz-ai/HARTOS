"""
Agent Lightning Tracer

Automatic tracing for agent interactions.
Captures prompts, tool calls, rewards, and outcomes.
"""

import logging
import time
import uuid
from typing import Any, Dict, Optional, List
from datetime import datetime
from collections import defaultdict

from .config import AGENT_LIGHTNING_CONFIG

logger = logging.getLogger(__name__)

# Global tracing state
_global_tracing_enabled = False
_active_spans = {}


class Span:
    """Represents a single traced interaction"""

    def __init__(
        self,
        span_id: str,
        agent_id: str,
        span_type: str,
        context: Optional[Dict] = None
    ):
        self.span_id = span_id
        self.agent_id = agent_id
        self.span_type = span_type
        self.context = context or {}
        self.start_time = time.time()
        self.end_time = None
        self.status = 'in_progress'
        self.events = []
        self.result = None

    def add_event(self, event_type: str, data: Dict):
        """Add event to span"""
        self.events.append({
            'type': event_type,
            'timestamp': time.time(),
            'data': data
        })

    def end(self, status: str, result: Any = None):
        """End the span"""
        self.end_time = time.time()
        self.status = status
        self.result = result

    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'span_id': self.span_id,
            'agent_id': self.agent_id,
            'span_type': self.span_type,
            'context': self.context,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration': self.end_time - self.start_time if self.end_time else None,
            'status': self.status,
            'events': self.events,
            'result': str(self.result)[:500] if self.result else None
        }


class LightningTracer:
    """
    Automatic tracer for agent interactions

    Captures:
    - Prompts sent to LLM
    - Tool executions
    - Rewards and outcomes
    - Performance metrics
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.spans = {}
        self.stats = defaultdict(int)
        self.enabled = AGENT_LIGHTNING_CONFIG['monitoring']['enabled']

        logger.info(f"LightningTracer initialized for {agent_id}")

    def start_span(
        self,
        span_type: str,
        context: Optional[Dict] = None
    ) -> str:
        """
        Start a new span

        Args:
            span_type: Type of span (e.g., 'generate_reply', 'tool_call')
            context: Optional context data

        Returns:
            Span ID
        """
        if not self.enabled:
            return None

        span_id = f"{self.agent_id}_{uuid.uuid4().hex[:12]}"

        span = Span(
            span_id=span_id,
            agent_id=self.agent_id,
            span_type=span_type,
            context=context
        )

        self.spans[span_id] = span
        _active_spans[span_id] = span

        self.stats['spans_started'] += 1

        logger.debug(f"Started span: {span_id} (type: {span_type})")

        return span_id

    def end_span(
        self,
        span_id: str,
        status: str,
        result: Any = None
    ):
        """
        End a span

        Args:
            span_id: Span ID
            status: Status ('success', 'error', etc.)
            result: Optional result data
        """
        if not self.enabled or not span_id:
            return

        span = self.spans.get(span_id)
        if not span:
            logger.warning(f"Span not found: {span_id}")
            return

        span.end(status, result)

        # Remove from active spans
        _active_spans.pop(span_id, None)

        self.stats['spans_completed'] += 1
        self.stats[f'spans_{status}'] += 1

        logger.debug(f"Ended span: {span_id} (status: {status})")

        # Save span if configured
        if AGENT_LIGHTNING_CONFIG['monitoring']['save_traces']:
            self._save_span(span)

    def emit_prompt(
        self,
        span_id: str,
        prompt: str,
        context: Optional[Dict] = None
    ):
        """
        Emit prompt event

        Args:
            span_id: Span ID
            prompt: Prompt text
            context: Optional context
        """
        if not self.enabled or not span_id:
            return

        span = self.spans.get(span_id)
        if not span:
            return

        span.add_event('prompt', {
            'prompt': prompt[:500],  # Truncate long prompts
            'context': context or {}
        })

        self.stats['prompts_emitted'] += 1

    def emit_response(
        self,
        span_id: str,
        response: str,
        context: Optional[Dict] = None
    ):
        """
        Emit response event

        Args:
            span_id: Span ID
            response: Response text
            context: Optional context
        """
        if not self.enabled or not span_id:
            return

        span = self.spans.get(span_id)
        if not span:
            return

        span.add_event('response', {
            'response': response[:500],  # Truncate long responses
            'context': context or {}
        })

        self.stats['responses_emitted'] += 1

    def emit_tool_call(
        self,
        span_id: str,
        tool_name: str,
        tool_args: str,
        context: Optional[Dict] = None
    ):
        """
        Emit tool call event

        Args:
            span_id: Span ID
            tool_name: Tool name
            tool_args: Tool arguments
            context: Optional context
        """
        if not self.enabled or not span_id:
            return

        span = self.spans.get(span_id)
        if not span:
            return

        span.add_event('tool_call', {
            'tool_name': tool_name,
            'tool_args': tool_args[:200],
            'context': context or {}
        })

        self.stats['tool_calls_emitted'] += 1
        self.stats[f'tool_{tool_name}'] += 1

    def emit_reward(
        self,
        span_id: str,
        reward: float,
        context: Optional[Dict] = None
    ):
        """
        Emit reward event

        Args:
            span_id: Span ID
            reward: Reward value
            context: Optional context
        """
        if not self.enabled or not span_id:
            return

        span = self.spans.get(span_id)
        if not span:
            return

        span.add_event('reward', {
            'reward': reward,
            'context': context or {}
        })

        self.stats['rewards_emitted'] += 1
        self.stats['total_reward'] = self.stats.get('total_reward', 0) + reward

    def _save_span(self, span: Span):
        """Save span to storage"""
        try:
            import os
            import json

            # Get traces path from config
            traces_path = AGENT_LIGHTNING_CONFIG.get('traces_path', './agent_data/lightning_traces')
            os.makedirs(traces_path, exist_ok=True)

            # Save span as JSON
            filename = f"{traces_path}/{span.span_id}.json"
            with open(filename, 'w') as f:
                json.dump(span.to_dict(), f, indent=2)

            logger.debug(f"Saved span to {filename}")

        except Exception as e:
            logger.error(f"Error saving span: {e}")

    def get_span(self, span_id: str) -> Optional[Span]:
        """Get span by ID"""
        return self.spans.get(span_id)

    def get_active_spans(self) -> List[Span]:
        """Get all active spans"""
        return [s for s in self.spans.values() if s.status == 'in_progress']

    def get_statistics(self) -> Dict:
        """Get tracer statistics"""
        return dict(self.stats)

    def clear(self):
        """Clear all spans"""
        self.spans.clear()
        logger.info(f"Cleared all spans for {self.agent_id}")


# Global functions for auto-tracing

def enable_auto_tracing():
    """Enable automatic tracing globally"""
    global _global_tracing_enabled
    _global_tracing_enabled = True
    logger.info("Global auto-tracing enabled")


def disable_auto_tracing():
    """Disable automatic tracing globally"""
    global _global_tracing_enabled
    _global_tracing_enabled = False
    logger.info("Global auto-tracing disabled")


def is_auto_tracing_enabled() -> bool:
    """Check if auto-tracing is enabled"""
    return _global_tracing_enabled


def get_active_span() -> Optional[Span]:
    """Get currently active span (if any)"""
    if not _active_spans:
        return None
    # Return most recent span
    return list(_active_spans.values())[-1]


__all__ = [
    'LightningTracer',
    'Span',
    'enable_auto_tracing',
    'disable_auto_tracing',
    'is_auto_tracing_enabled',
    'get_active_span',
]
