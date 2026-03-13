"""
Agent Lightning Store

Persistence layer for spans and training data.
Supports multiple backends: Redis, JSON, and in-memory.
"""

import logging
import json
import os
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict

from .config import AGENT_LIGHTNING_CONFIG
from .tracer import Span

logger = logging.getLogger(__name__)


class LightningStore:
    """
    Persistence layer for Agent Lightning data

    Stores:
    - Spans (traces of agent interactions)
    - Training data (for continuous improvement)
    - Statistics and metrics

    Supports multiple backends:
    - redis: Redis backend (recommended for production)
    - json: JSON file backend (for development)
    - memory: In-memory only (for testing)
    """

    def __init__(
        self,
        agent_id: str,
        backend: str = None
    ):
        self.agent_id = agent_id
        self.backend = backend or AGENT_LIGHTNING_CONFIG.get('store_backend', 'json')

        # Backend-specific initialization
        self._backend_client = None
        self._init_backend()

        # In-memory cache
        self._cache = {
            'spans': {},
            'stats': defaultdict(float),
        }

        logger.info(f"LightningStore initialized for {agent_id} with backend: {self.backend}")

    def _init_backend(self):
        """Initialize storage backend"""
        if self.backend == 'redis':
            try:
                import redis
                redis_config = AGENT_LIGHTNING_CONFIG.get('redis', {})
                self._backend_client = redis.Redis(
                    host=redis_config.get('host', 'localhost'),
                    port=redis_config.get('port', 6379),
                    db=redis_config.get('db', 0),
                    decode_responses=True,
                    socket_connect_timeout=1, socket_timeout=2,
                    retry_on_timeout=False,
                )
                # Test connection
                self._backend_client.ping()
                logger.info("Connected to Redis backend")
            except Exception as e:
                logger.warning(f"Redis backend failed: {e}. Falling back to JSON.")
                self.backend = 'json'
                self._init_backend()

        elif self.backend == 'json':
            # Ensure storage directory exists
            self.storage_path = AGENT_LIGHTNING_CONFIG.get(
                'traces_path',
                './agent_data/lightning_traces'
            )
            os.makedirs(self.storage_path, exist_ok=True)
            logger.info(f"Using JSON backend at {self.storage_path}")

        elif self.backend == 'memory':
            logger.info("Using in-memory backend (data will not persist)")

        else:
            logger.warning(f"Unknown backend: {self.backend}. Using memory.")
            self.backend = 'memory'

    def save_span(self, span: Span) -> bool:
        """
        Save span to storage

        Args:
            span: Span to save

        Returns:
            Success status
        """
        try:
            span_dict = span.to_dict()
            span_key = f"span:{self.agent_id}:{span.span_id}"

            # Save to backend
            if self.backend == 'redis':
                self._backend_client.set(
                    span_key,
                    json.dumps(span_dict)
                )
                # Add to agent's span list
                self._backend_client.sadd(
                    f"spans:{self.agent_id}",
                    span.span_id
                )

            elif self.backend == 'json':
                filename = os.path.join(
                    self.storage_path,
                    f"{span.span_id}.json"
                )
                with open(filename, 'w') as f:
                    json.dump(span_dict, f, indent=2)

            # Always cache in memory
            self._cache['spans'][span.span_id] = span_dict

            logger.debug(f"Saved span: {span.span_id}")
            return True

        except Exception as e:
            logger.error(f"Error saving span: {e}")
            return False

    def load_span(self, span_id: str) -> Optional[Dict]:
        """
        Load span from storage

        Args:
            span_id: Span ID

        Returns:
            Span dictionary or None
        """
        # Check cache first
        if span_id in self._cache['spans']:
            return self._cache['spans'][span_id]

        try:
            if self.backend == 'redis':
                span_key = f"span:{self.agent_id}:{span_id}"
                span_json = self._backend_client.get(span_key)
                if span_json:
                    span_dict = json.loads(span_json)
                    self._cache['spans'][span_id] = span_dict
                    return span_dict

            elif self.backend == 'json':
                filename = os.path.join(
                    self.storage_path,
                    f"{span_id}.json"
                )
                if os.path.exists(filename):
                    with open(filename, 'r') as f:
                        span_dict = json.load(f)
                        self._cache['spans'][span_id] = span_dict
                        return span_dict

            return None

        except Exception as e:
            logger.error(f"Error loading span: {e}")
            return None

    def list_spans(
        self,
        limit: int = 100,
        span_type: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict]:
        """
        List spans with optional filtering

        Args:
            limit: Maximum number of spans to return
            span_type: Filter by span type
            status: Filter by status

        Returns:
            List of span dictionaries
        """
        try:
            spans = []

            if self.backend == 'redis':
                span_ids = self._backend_client.smembers(f"spans:{self.agent_id}")
                for span_id in span_ids:
                    span = self.load_span(span_id)
                    if span:
                        # Apply filters
                        if span_type and span.get('span_type') != span_type:
                            continue
                        if status and span.get('status') != status:
                            continue
                        spans.append(span)

                    if len(spans) >= limit:
                        break

            elif self.backend == 'json':
                for filename in os.listdir(self.storage_path):
                    if not filename.endswith('.json'):
                        continue

                    span_id = filename[:-5]  # Remove .json
                    span = self.load_span(span_id)
                    if span:
                        # Apply filters
                        if span_type and span.get('span_type') != span_type:
                            continue
                        if status and span.get('status') != status:
                            continue
                        spans.append(span)

                    if len(spans) >= limit:
                        break

            elif self.backend == 'memory':
                for span_id, span in self._cache['spans'].items():
                    # Apply filters
                    if span_type and span.get('span_type') != span_type:
                        continue
                    if status and span.get('status') != status:
                        continue
                    spans.append(span)

                    if len(spans) >= limit:
                        break

            # Sort by start_time (most recent first)
            spans.sort(key=lambda s: s.get('start_time', 0), reverse=True)

            return spans[:limit]

        except Exception as e:
            logger.error(f"Error listing spans: {e}")
            return []

    def get_training_data(
        self,
        limit: int = 1000,
        min_reward: Optional[float] = None,
        max_reward: Optional[float] = None
    ) -> List[Dict]:
        """
        Get training data for continuous improvement

        Args:
            limit: Maximum samples to return
            min_reward: Minimum reward threshold
            max_reward: Maximum reward threshold

        Returns:
            List of training samples
        """
        spans = self.list_spans(limit=limit)
        training_data = []

        for span in spans:
            # Extract reward events
            rewards = [
                event for event in span.get('events', [])
                if event.get('type') == 'reward'
            ]

            if not rewards:
                continue

            # Calculate total reward
            total_reward = sum(
                event.get('data', {}).get('reward', 0)
                for event in rewards
            )

            # Apply reward filters
            if min_reward is not None and total_reward < min_reward:
                continue
            if max_reward is not None and total_reward > max_reward:
                continue

            # Extract prompt and response
            prompt_events = [
                event for event in span.get('events', [])
                if event.get('type') == 'prompt'
            ]
            response_events = [
                event for event in span.get('events', [])
                if event.get('type') == 'response'
            ]

            if prompt_events and response_events:
                training_sample = {
                    'span_id': span.get('span_id'),
                    'agent_id': span.get('agent_id'),
                    'prompt': prompt_events[0].get('data', {}).get('prompt', ''),
                    'response': response_events[0].get('data', {}).get('response', ''),
                    'reward': total_reward,
                    'duration': span.get('duration'),
                    'status': span.get('status'),
                    'timestamp': span.get('start_time')
                }
                training_data.append(training_sample)

        return training_data

    def delete_span(self, span_id: str) -> bool:
        """
        Delete span from storage

        Args:
            span_id: Span ID

        Returns:
            Success status
        """
        try:
            if self.backend == 'redis':
                span_key = f"span:{self.agent_id}:{span_id}"
                self._backend_client.delete(span_key)
                self._backend_client.srem(f"spans:{self.agent_id}", span_id)

            elif self.backend == 'json':
                filename = os.path.join(
                    self.storage_path,
                    f"{span_id}.json"
                )
                if os.path.exists(filename):
                    os.remove(filename)

            # Remove from cache
            self._cache['spans'].pop(span_id, None)

            logger.debug(f"Deleted span: {span_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting span: {e}")
            return False

    def cleanup_old_spans(self, days: int = 30) -> int:
        """
        Delete spans older than specified days

        Args:
            days: Age threshold in days

        Returns:
            Number of spans deleted
        """
        try:
            threshold = datetime.now().timestamp() - (days * 86400)
            deleted_count = 0

            spans = self.list_spans(limit=10000)
            for span in spans:
                if span.get('start_time', 0) < threshold:
                    if self.delete_span(span.get('span_id')):
                        deleted_count += 1

            logger.info(f"Cleaned up {deleted_count} old spans")
            return deleted_count

        except Exception as e:
            logger.error(f"Error cleaning up spans: {e}")
            return 0

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get storage statistics

        Returns:
            Statistics dictionary
        """
        stats = {
            'agent_id': self.agent_id,
            'backend': self.backend,
            'cached_spans': len(self._cache['spans'])
        }

        try:
            if self.backend == 'redis':
                stats['total_spans'] = self._backend_client.scard(f"spans:{self.agent_id}")

            elif self.backend == 'json':
                json_files = [
                    f for f in os.listdir(self.storage_path)
                    if f.endswith('.json')
                ]
                stats['total_spans'] = len(json_files)

            elif self.backend == 'memory':
                stats['total_spans'] = len(self._cache['spans'])

        except Exception as e:
            logger.error(f"Error getting statistics: {e}")

        return stats


__all__ = [
    'LightningStore',
]
