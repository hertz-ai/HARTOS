"""
Hive Signal Bridge -- Every channel message is a hive signal.

Hooks into ALL channel adapters via on_message(). Classifies signals and
routes them to the appropriate hive agent:

Signal Types:
  - COMPUTE_INTEREST: Someone mentions GPUs, idle hardware, mining, earning
    -> Route to compute_recruiter agent
  - MODEL_REQUEST: Someone asks about a model, wants inference
    -> Route to model_provisioner agent
  - BUG_REPORT: Someone reports a bug, error, issue
    -> Create HiveTask for connected Claude Code sessions
  - FEATURE_REQUEST: Someone wants a new feature
    -> Queue in instruction_queue for next idle agent
  - SUPPORT_NEEDED: Someone needs help
    -> Route to /chat pipeline for immediate response
  - SENTIMENT: General sentiment signal
    -> Feed to resonance tuner for community health tracking
  - RECRUITMENT_LEAD: Someone expresses interest in contributing
    -> Route to compute_recruiter with personalized onboarding
  - OPEN_SOURCE_SIGNAL: New model release, paper, benchmark mentioned
    -> Route to opensource_evangelist agent

Every signal earns micro-Spark for the channel where it originated.
This incentivizes active communities.
"""

import asyncio
import collections
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =====================================================================
# Signal Type Constants
# =====================================================================

COMPUTE_INTEREST = 'COMPUTE_INTEREST'
MODEL_REQUEST = 'MODEL_REQUEST'
BUG_REPORT = 'BUG_REPORT'
FEATURE_REQUEST = 'FEATURE_REQUEST'
SUPPORT_NEEDED = 'SUPPORT_NEEDED'
SENTIMENT = 'SENTIMENT'
RECRUITMENT_LEAD = 'RECRUITMENT_LEAD'
OPEN_SOURCE_SIGNAL = 'OPEN_SOURCE_SIGNAL'

ALL_SIGNAL_TYPES = (
    COMPUTE_INTEREST, MODEL_REQUEST, BUG_REPORT, FEATURE_REQUEST,
    SUPPORT_NEEDED, SENTIMENT, RECRUITMENT_LEAD, OPEN_SOURCE_SIGNAL,
)


# =====================================================================
# Keyword Sets for Heuristic Classification
# =====================================================================

_COMPUTE_KEYWORDS = frozenset([
    'gpu', 'cuda', 'vram', 'rtx', 'nvidia', 'amd', 'idle', 'mining',
    'earn', 'compute', 'hardware', 'server', 'rack', 'hosting',
])

_MODEL_KEYWORDS = frozenset([
    'model', 'inference', 'llm', 'qwen', 'llama', 'mistral', 'gemma',
    'phi', 'gpt', 'claude', 'api', 'endpoint', 'chat', 'completion',
])

_BUG_KEYWORDS = frozenset([
    'bug', 'error', 'crash', 'broken', 'fail', 'exception', 'traceback',
    'issue', '500', 'timeout', 'hang', 'freeze',
])

_FEATURE_KEYWORDS = frozenset([
    'feature', 'request', 'wish', 'could you', 'would be nice',
    'suggestion', 'idea', 'propose', 'enhancement',
])

_SUPPORT_KEYWORDS = frozenset([
    'help', 'stuck', 'how do i', 'how to', 'cant', "can't", 'unable',
    'confused', 'documentation', 'tutorial',
])

_RECRUIT_KEYWORDS = frozenset([
    'contribute', 'join', 'volunteer', 'participate', 'help out',
    'open source', 'community', 'developer', 'contributor',
])

_OPENSOURCE_KEYWORDS = frozenset([
    'huggingface', 'arxiv', 'paper', 'release', 'benchmark', 'gguf',
    'quantize', 'fine-tune', 'lora', 'unsloth', 'new model',
])

_POSITIVE_WORDS = frozenset([
    'thanks', 'great', 'love', 'perfect', 'excellent', 'amazing',
    'good', 'nice', 'helpful', 'wonderful', 'appreciate', 'awesome',
])

_NEGATIVE_WORDS = frozenset([
    'bad', 'wrong', 'terrible', 'hate', 'awful', 'worse', 'useless',
    'frustrated', 'confused', 'disappointed', 'annoying',
])

# Multi-word phrases extracted for substring matching (cannot be caught
# by single-word tokenization)
_MULTI_WORD_PHRASES: Dict[str, List[str]] = {
    FEATURE_REQUEST: ['could you', 'would be nice'],
    SUPPORT_NEEDED: ['how do i', 'how to'],
    RECRUITMENT_LEAD: ['help out', 'open source'],
    OPEN_SOURCE_SIGNAL: ['fine-tune', 'new model'],
}

# Single-word lookup: signal_type -> keywords (excludes multi-word phrases)
_SINGLE_WORD_MAP: Dict[str, frozenset] = {
    COMPUTE_INTEREST: _COMPUTE_KEYWORDS,
    MODEL_REQUEST: _MODEL_KEYWORDS,
    BUG_REPORT: _BUG_KEYWORDS,
    FEATURE_REQUEST: frozenset(k for k in _FEATURE_KEYWORDS if ' ' not in k),
    SUPPORT_NEEDED: frozenset(k for k in _SUPPORT_KEYWORDS if ' ' not in k),
    RECRUITMENT_LEAD: frozenset(k for k in _RECRUIT_KEYWORDS if ' ' not in k),
    OPEN_SOURCE_SIGNAL: frozenset(k for k in _OPENSOURCE_KEYWORDS if ' ' not in k),
}


# =====================================================================
# Signal Feed Entry
# =====================================================================

def _make_feed_entry(message, signals: List[str]) -> dict:
    """Create a lightweight feed entry from a message and its signals."""
    content = ''
    try:
        content = message.content if hasattr(message, 'content') else ''
        if callable(content):
            content = content()
    except Exception:
        content = getattr(message, 'text', '') or ''

    return {
        'message_id': getattr(message, 'id', ''),
        'channel': getattr(message, 'channel', ''),
        'sender_id': getattr(message, 'sender_id', ''),
        'sender_name': getattr(message, 'sender_name', ''),
        'text_preview': (content[:120] + '...') if len(content) > 120 else content,
        'signals': list(signals),
        'is_group': getattr(message, 'is_group', False),
        'timestamp': time.time(),
    }


# =====================================================================
# HiveSignalBridge
# =====================================================================

class HiveSignalBridge:
    """Captures signals from all channel adapters and feeds them into the hive.

    Every message across every channel is a signal -- for recruitment,
    support, demand detection, sentiment, and hive growth.

    The _on_message handler is designed to be fast (<5ms): no LLM calls,
    no database writes, no network I/O. All heavy work (goal dispatch,
    instruction queuing, etc.) is offloaded to background threads.
    """

    def __init__(self):
        self._lock = threading.Lock()

        # Stats: signal counts by type and by channel
        self._signal_counts: Dict[str, int] = {st: 0 for st in ALL_SIGNAL_TYPES}
        self._channel_counts: Dict[str, int] = {}
        self._total_messages: int = 0

        # Bounded signal feed for dashboard display
        self._signal_feed: collections.deque = collections.deque(maxlen=1000)

        # Background executor for routing (keeps _on_message fast)
        self._executor: Optional[Any] = None
        self._executor_lock = threading.Lock()

        # Attached adapter names (for diagnostics)
        self._attached_adapters: List[str] = []

    # ── Lazy background executor ───────────────────────────────────

    def _get_executor(self):
        """Lazily create the thread pool executor for routing."""
        if self._executor is None:
            with self._executor_lock:
                if self._executor is None:
                    from concurrent.futures import ThreadPoolExecutor
                    self._executor = ThreadPoolExecutor(
                        max_workers=2,
                        thread_name_prefix='hive_signal',
                    )
        return self._executor

    # ── Adapter Attachment ─────────────────────────────────────────

    def attach_to_adapter(self, adapter) -> None:
        """Register self as a message handler on a channel adapter.

        Call this for every ChannelAdapter at boot time.

        Args:
            adapter: A ChannelAdapter instance (from integrations.channels.base).
        """
        try:
            adapter.on_message(self._on_message)
            name = getattr(adapter, 'name', str(type(adapter).__name__))
            self._attached_adapters.append(name)
            logger.info("HiveSignalBridge attached to channel: %s", name)
        except Exception as e:
            logger.warning("Failed to attach HiveSignalBridge to adapter: %s", e)

    def attach_to_all(self, adapters: dict) -> None:
        """Attach to all adapters in a dict (e.g., ChannelRegistry._adapters).

        Args:
            adapters: Dict mapping channel name -> ChannelAdapter instance.
        """
        for name, adapter in adapters.items():
            self.attach_to_adapter(adapter)

    # ── Core Message Handler ───────────────────────────────────────

    def _on_message(self, message) -> None:
        """Core handler. Called for every message on every channel.

        MUST be fast (<5ms). No LLM calls, no DB writes, no network I/O.
        Classification is pure heuristic. Routing is offloaded to background.
        """
        try:
            # Extract text content (Message.content is a property in base.py)
            text = ''
            try:
                text = message.content if hasattr(message, 'content') else ''
                if callable(text):
                    text = text()
            except Exception:
                text = getattr(message, 'text', '') or ''

            if not text or len(text.strip()) < 2:
                return

            channel_type = getattr(message, 'channel', '')
            is_group = getattr(message, 'is_group', False)

            # Fast heuristic classification (no LLM)
            signals = self.classify_signal(text, channel_type, is_group)

            if not signals:
                return

            # Update stats (thread-safe)
            with self._lock:
                self._total_messages += 1
                for sig in signals:
                    self._signal_counts[sig] = self._signal_counts.get(sig, 0) + 1
                self._channel_counts[channel_type] = (
                    self._channel_counts.get(channel_type, 0) + 1
                )

            # Add to feed
            entry = _make_feed_entry(message, signals)
            self._signal_feed.append(entry)

            # Emit to EventBus (best-effort, non-blocking)
            self._emit_signal_event(message, signals, channel_type)

            # Emit micro-Spark reward event for the originating channel
            self._emit_spark_event(message, signals, channel_type)

            # Route to appropriate hive agents (background thread)
            self._get_executor().submit(
                self._route_signals, message, signals
            )

        except Exception as e:
            # Handler must never raise -- would break the channel adapter
            logger.debug("HiveSignalBridge._on_message error: %s", e)

    # ── Signal Classification ──────────────────────────────────────

    def classify_signal(self, text: str, channel_type: str = '',
                        is_group: bool = False) -> List[str]:
        """Fast heuristic classifier. No LLM calls -- pure keyword matching.

        A single message can match multiple signal types.

        Args:
            text: Message text content.
            channel_type: Channel name (e.g., 'discord', 'telegram').
            is_group: Whether the message is from a group chat.

        Returns:
            List of matched signal type strings.
        """
        signals: List[str] = []
        text_lower = text.lower()
        words = set(text_lower.split())

        # Single-word keyword matching
        for signal_type, keywords in _SINGLE_WORD_MAP.items():
            if words & keywords:
                signals.append(signal_type)

        # Multi-word phrase matching (substring search)
        for signal_type, phrases in _MULTI_WORD_PHRASES.items():
            if signal_type not in signals:
                for phrase in phrases:
                    if phrase in text_lower:
                        signals.append(signal_type)
                        break

        # URL-based signals
        if 'huggingface.co' in text_lower or 'arxiv.org' in text_lower:
            if OPEN_SOURCE_SIGNAL not in signals:
                signals.append(OPEN_SOURCE_SIGNAL)

        # Sentiment detection (always check -- provides community health data)
        pos_count = len(words & _POSITIVE_WORDS)
        neg_count = len(words & _NEGATIVE_WORDS)
        if pos_count > 0 or neg_count > 0:
            if SENTIMENT not in signals:
                signals.append(SENTIMENT)

        return signals

    # ── Event Emission ─────────────────────────────────────────────

    def _emit_signal_event(self, message, signals: List[str],
                           channel_type: str) -> None:
        """Emit 'hive.signal.received' to EventBus."""
        try:
            from core.platform.events import emit_event
            emit_event('hive.signal.received', {
                'message_id': getattr(message, 'id', ''),
                'channel': channel_type,
                'sender_id': getattr(message, 'sender_id', ''),
                'signals': signals,
                'is_group': getattr(message, 'is_group', False),
                'timestamp': time.time(),
            })
        except Exception:
            pass  # EventBus emission is best-effort

    def _emit_spark_event(self, message, signals: List[str],
                          channel_type: str) -> None:
        """Emit 'hive.signal.spark' for micro-Spark channel reward.

        Actual Spark accounting happens elsewhere (revenue_aggregator).
        This event simply declares that a signal-worthy message occurred
        on a given channel, so the reward system can credit it.
        """
        try:
            from core.platform.events import emit_event
            emit_event('hive.signal.spark', {
                'channel': channel_type,
                'sender_id': getattr(message, 'sender_id', ''),
                'signal_count': len(signals),
                'signals': signals,
                'timestamp': time.time(),
            })
        except Exception:
            pass

    # ── Signal Routing ─────────────────────────────────────────────

    def _route_signals(self, message, signals: List[str]) -> None:
        """Route classified signals to the appropriate hive subsystem.

        Runs in a background thread so the message handler stays fast.
        Each router method is best-effort -- failures are logged, never raised.
        """
        router_map = {
            COMPUTE_INTEREST: self._route_compute_interest,
            MODEL_REQUEST: self._route_model_request,
            BUG_REPORT: self._route_bug_report,
            FEATURE_REQUEST: self._route_feature_request,
            SUPPORT_NEEDED: self._route_support_needed,
            RECRUITMENT_LEAD: self._route_recruitment_lead,
            OPEN_SOURCE_SIGNAL: self._route_open_source_signal,
            SENTIMENT: self._route_sentiment,
        }
        for signal_type in signals:
            handler = router_map.get(signal_type)
            if handler:
                try:
                    handler(message, signals)
                except Exception as e:
                    logger.debug("Signal routing error (%s): %s", signal_type, e)

    def _route_compute_interest(self, message, signals: List[str]) -> None:
        """Queue recruitment task for compute_recruiter agent.

        Dispatches a goal that triggers the bootstrap_compute_recruiter
        seed (from goal_seeding.py) to reach out with personalized
        onboarding content.
        """
        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            text = _extract_text(message)
            sender = getattr(message, 'sender_name', '') or getattr(message, 'sender_id', 'unknown')
            channel = getattr(message, 'channel', 'unknown')
            dispatch_goal(
                prompt=(
                    f"Compute recruitment lead detected on {channel} from {sender}: "
                    f"\"{text[:300]}\". "
                    "Evaluate interest level and prepare personalized onboarding message. "
                    "Explain how to contribute idle compute to the hive and earn Spark."
                ),
                user_id='hive_signal_bridge',
                goal_id=f"sig_compute_{getattr(message, 'id', '')}",
                goal_type='hive_growth',
            )
            logger.debug("Routed COMPUTE_INTEREST signal from %s", channel)
        except ImportError:
            logger.debug("dispatch module not available for compute routing")
        except Exception as e:
            logger.debug("COMPUTE_INTEREST routing failed: %s", e)

    def _route_model_request(self, message, signals: List[str]) -> None:
        """Check if requested model is available; if not, queue provisioning.

        Routes to model_provisioner goal to check hive model registry
        and potentially onboard the requested model.
        """
        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            text = _extract_text(message)
            channel = getattr(message, 'channel', 'unknown')
            dispatch_goal(
                prompt=(
                    f"Model request detected on {channel}: \"{text[:300]}\". "
                    "Check if the requested model is available in the hive model registry. "
                    "If not, evaluate feasibility and queue for onboarding."
                ),
                user_id='hive_signal_bridge',
                goal_id=f"sig_model_{getattr(message, 'id', '')}",
                goal_type='hive_growth',
            )
            logger.debug("Routed MODEL_REQUEST signal from %s", channel)
        except ImportError:
            logger.debug("dispatch module not available for model routing")
        except Exception as e:
            logger.debug("MODEL_REQUEST routing failed: %s", e)

    def _route_bug_report(self, message, signals: List[str]) -> None:
        """Create HiveTask (BUG_FIX type) for connected Claude Code sessions.

        If the hive_task_protocol is available, create a task. Otherwise
        falls back to instruction_queue for the next idle agent.
        """
        text = _extract_text(message)
        channel = getattr(message, 'channel', 'unknown')
        sender = getattr(message, 'sender_name', '') or getattr(message, 'sender_id', 'unknown')

        # Try HiveTaskDispatcher first (for live Claude Code sessions)
        try:
            from integrations.coding_agent.hive_task_protocol import get_dispatcher
            get_dispatcher().create_task(
                task_type='bug_fix',
                title=f"Bug report from {channel}",
                description=f"Bug report from {sender} on {channel}: {text[:500]}",
                instructions=f"Investigate and fix: {text[:1000]}",
            )
            logger.debug("Routed BUG_REPORT to HiveTaskDispatcher from %s", channel)
            return
        except (ImportError, AttributeError):
            pass
        except Exception as e:
            logger.debug("HiveTask dispatch failed, falling back: %s", e)

        # Fallback: queue in instruction_queue
        try:
            from integrations.agent_engine.instruction_queue import enqueue_instruction
            enqueue_instruction(
                user_id='hive_signal_bridge',
                text=(
                    f"Bug report from {sender} on {channel}: {text[:500]}. "
                    "Investigate and fix if possible."
                ),
                priority=7,
                tags=['bug', 'signal_bridge', channel],
            )
            logger.debug("Routed BUG_REPORT to instruction_queue from %s", channel)
        except ImportError:
            logger.debug("instruction_queue not available for bug routing")
        except Exception as e:
            logger.debug("BUG_REPORT instruction queue failed: %s", e)

    def _route_feature_request(self, message, signals: List[str]) -> None:
        """Queue feature request in instruction_queue for next idle agent."""
        try:
            from integrations.agent_engine.instruction_queue import enqueue_instruction
            text = _extract_text(message)
            channel = getattr(message, 'channel', 'unknown')
            sender = getattr(message, 'sender_name', '') or getattr(message, 'sender_id', 'unknown')
            enqueue_instruction(
                user_id='hive_signal_bridge',
                text=(
                    f"Feature request from {sender} on {channel}: {text[:500]}. "
                    "Evaluate feasibility and create implementation plan if viable."
                ),
                priority=4,
                tags=['feature_request', 'signal_bridge', channel],
            )
            logger.debug("Routed FEATURE_REQUEST to instruction_queue from %s", channel)
        except ImportError:
            logger.debug("instruction_queue not available for feature routing")
        except Exception as e:
            logger.debug("FEATURE_REQUEST routing failed: %s", e)

    def _route_support_needed(self, message, signals: List[str]) -> None:
        """Dispatch to /chat for immediate response.

        Uses dispatch_goal with high priority so the user gets help fast.
        """
        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            text = _extract_text(message)
            channel = getattr(message, 'channel', 'unknown')
            sender = getattr(message, 'sender_name', '') or getattr(message, 'sender_id', 'unknown')
            dispatch_goal(
                prompt=(
                    f"Support request from {sender} on {channel}: \"{text[:300]}\". "
                    "Provide helpful, clear guidance. Be patient and thorough."
                ),
                user_id=getattr(message, 'sender_id', 'hive_signal_bridge'),
                goal_id=f"sig_support_{getattr(message, 'id', '')}",
                goal_type='support',
            )
            logger.debug("Routed SUPPORT_NEEDED to /chat from %s", channel)
        except ImportError:
            logger.debug("dispatch module not available for support routing")
        except Exception as e:
            logger.debug("SUPPORT_NEEDED routing failed: %s", e)

    def _route_recruitment_lead(self, message, signals: List[str]) -> None:
        """High-priority: queue personalized onboarding message.

        Someone expressed interest in contributing -- this is the most
        valuable signal. Route to compute_recruiter with high priority.
        """
        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            text = _extract_text(message)
            sender = getattr(message, 'sender_name', '') or getattr(message, 'sender_id', 'unknown')
            channel = getattr(message, 'channel', 'unknown')
            dispatch_goal(
                prompt=(
                    f"HIGH PRIORITY recruitment lead on {channel} from {sender}: "
                    f"\"{text[:300]}\". "
                    "This person wants to contribute to the hive. "
                    "Prepare a warm, personalized onboarding message. "
                    "Explain the mission, how to get started, and the Spark rewards. "
                    "Make them feel welcome and valued."
                ),
                user_id='hive_signal_bridge',
                goal_id=f"sig_recruit_{getattr(message, 'id', '')}",
                goal_type='hive_growth',
            )
            logger.debug("Routed RECRUITMENT_LEAD from %s", channel)
        except ImportError:
            logger.debug("dispatch module not available for recruitment routing")
        except Exception as e:
            logger.debug("RECRUITMENT_LEAD routing failed: %s", e)

    def _route_open_source_signal(self, message, signals: List[str]) -> None:
        """Queue model onboarding task for opensource_evangelist agent."""
        try:
            from integrations.agent_engine.dispatch import dispatch_goal
            text = _extract_text(message)
            channel = getattr(message, 'channel', 'unknown')
            dispatch_goal(
                prompt=(
                    f"Open source signal detected on {channel}: \"{text[:300]}\". "
                    "Evaluate if this is a new model, paper, or benchmark worth "
                    "onboarding to the hive. If so, create an integration plan."
                ),
                user_id='hive_signal_bridge',
                goal_id=f"sig_oss_{getattr(message, 'id', '')}",
                goal_type='hive_growth',
            )
            logger.debug("Routed OPEN_SOURCE_SIGNAL from %s", channel)
        except ImportError:
            logger.debug("dispatch module not available for OSS routing")
        except Exception as e:
            logger.debug("OPEN_SOURCE_SIGNAL routing failed: %s", e)

    def _route_sentiment(self, message, signals: List[str]) -> None:
        """Feed sentiment signal to resonance tuner for community health.

        Uses SignalExtractor to compute sentiment and feeds it as a
        lightweight community-level signal. No per-user profile update
        (that happens through the normal /chat path).
        """
        try:
            from core.resonance_tuner import SignalExtractor
            text = _extract_text(message)
            extracted = SignalExtractor.extract(text, '', 0.0)
            channel = getattr(message, 'channel', 'unknown')

            try:
                from core.platform.events import emit_event
                emit_event('hive.signal.sentiment', {
                    'channel': channel,
                    'sender_id': getattr(message, 'sender_id', ''),
                    'positive_sentiment': extracted.positive_sentiment,
                    'formality': extracted.formality_markers,
                    'is_group': getattr(message, 'is_group', False),
                    'timestamp': time.time(),
                })
            except Exception:
                pass
        except ImportError:
            pass
        except Exception as e:
            logger.debug("SENTIMENT routing failed: %s", e)

    # ── Stats & Feed ───────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Signal counts by type, by channel, and total messages processed.

        Returns:
            Dict with 'by_type', 'by_channel', 'total_messages',
            and 'attached_adapters'.
        """
        with self._lock:
            return {
                'by_type': dict(self._signal_counts),
                'by_channel': dict(self._channel_counts),
                'total_messages': self._total_messages,
                'attached_adapters': list(self._attached_adapters),
            }

    def get_signal_feed(self, limit: int = 50) -> List[dict]:
        """Recent signals for dashboard display.

        Args:
            limit: Maximum number of entries to return (default 50).

        Returns:
            List of signal feed entries, most recent first.
        """
        limit = max(1, min(limit, 1000))
        entries = list(self._signal_feed)
        # Return most recent first
        return list(reversed(entries[-limit:]))


# =====================================================================
# Helpers
# =====================================================================

def _extract_text(message) -> str:
    """Safely extract text content from a Message object."""
    try:
        content = message.content if hasattr(message, 'content') else ''
        if callable(content):
            content = content()
        return content or getattr(message, 'text', '') or ''
    except Exception:
        return getattr(message, 'text', '') or ''


# =====================================================================
# Singleton
# =====================================================================

_bridge: Optional[HiveSignalBridge] = None
_bridge_lock = threading.Lock()


def get_signal_bridge() -> HiveSignalBridge:
    """Get or create the singleton HiveSignalBridge."""
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = HiveSignalBridge()
    return _bridge


# =====================================================================
# Flask Blueprint (lazy)
# =====================================================================

def create_signal_blueprint():
    """Create a Flask Blueprint for signal bridge API endpoints.

    Endpoints:
        GET  /api/hive/signals/stats    - Signal counts by type and channel
        GET  /api/hive/signals/feed     - Recent signals (limit query param)
        POST /api/hive/signals/classify - Test classifier on arbitrary text

    Returns:
        Flask Blueprint instance.
    """
    try:
        from flask import Blueprint, jsonify, request
    except ImportError:
        logger.debug("Flask not available -- signal blueprint not created")
        return None

    bp = Blueprint('hive_signals', __name__, url_prefix='/api/hive/signals')

    @bp.route('/stats', methods=['GET'])
    def signal_stats():
        bridge = get_signal_bridge()
        return jsonify(bridge.get_stats())

    @bp.route('/feed', methods=['GET'])
    def signal_feed():
        bridge = get_signal_bridge()
        limit = request.args.get('limit', 50, type=int)
        return jsonify(bridge.get_signal_feed(limit=limit))

    @bp.route('/classify', methods=['POST'])
    def classify_text():
        bridge = get_signal_bridge()
        data = request.get_json(silent=True) or {}
        text = data.get('text', '')
        channel_type = data.get('channel_type', '')
        is_group = data.get('is_group', False)
        if not text:
            return jsonify({'error': 'text is required'}), 400
        signals = bridge.classify_signal(text, channel_type, is_group)
        return jsonify({
            'text': text,
            'signals': signals,
            'channel_type': channel_type,
            'is_group': is_group,
        })

    return bp
