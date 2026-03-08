"""
ResonanceTuner — Continuous personality frequency tuning.

HARTOS is the agentic orchestration layer. All actual learning (Hebbian,
Bayesian, probabilistic, gradient descent) lives in the HevolveAI sibling
repo. This module:
  1. Extracts interaction signals (pure heuristics, no LLM)
  2. Streams them to HevolveAI via WorldModelBridge for learning
  3. Applies corrections received from HevolveAI
  4. Uses EMA for immediate blending (fast local response while
     HevolveAI does the deep learning in the background)
  5. Exports anonymized resonance deltas for federation

Integration:
  - Called after every /chat response (post-response hook)
  - DialogueStreamProcessor: continuous in-conversation tuning
  - WorldModelBridge: signals flow downstream to HevolveAI
  - FederatedAggregator: anonymized deltas across nodes
"""

import logging
import math
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .resonance_profile import (
    UserResonanceProfile, save_resonance_profile,
    get_or_create_profile, load_resonance_profile,
    DEFAULT_TUNING, TUNING_DIM_KEYS, TUNING_DIM_COUNT,
    RESONANCE_STORAGE_DIR,
)

logger = logging.getLogger(__name__)

# EMA decay factor: higher = more weight on new observations
EMA_ALPHA = float(os.environ.get('RESONANCE_EMA_ALPHA', '0.15'))

# Minimum interactions before tuning starts affecting personality
MIN_INTERACTIONS_FOR_TUNING = 3

# Confidence growth rate (asymptotic toward 1.0)
CONFIDENCE_GROWTH_RATE = 0.05

# Tuning history length for oscillation detection
TUNING_HISTORY_MAXLEN = 20

# Oscillation threshold: if any dim variance exceeds this, flag for HevolveAI
OSCILLATION_VARIANCE_THRESHOLD = float(os.environ.get(
    'RESONANCE_OSCILLATION_THRESHOLD', '0.02'))


# =====================================================================
# Interaction Signals
# =====================================================================

@dataclass
class InteractionSignals:
    """Extracted signals from a single user<->agent exchange."""
    user_message_length: int = 0
    agent_response_length: int = 0
    formality_markers: float = 0.0     # 0.0=casual, 1.0=formal
    question_count: int = 0
    exclamation_count: int = 0
    technical_term_count: int = 0
    positive_sentiment: float = 0.5    # 0.0=negative, 1.0=positive
    response_time_ms: float = 0.0
    vocabulary_richness: float = 0.5   # type-token ratio proxy


class SignalExtractor:
    """Extract interaction signals from raw text. No LLM calls -- pure heuristics."""

    _FORMAL_WORDS = frozenset([
        'please', 'kindly', 'regarding', 'therefore', 'furthermore',
        'accordingly', 'shall', 'hereby', 'pursuant', 'respectfully',
        'dear', 'sincerely', 'appreciate', 'would',
    ])

    _CASUAL_WORDS = frozenset([
        'hey', 'yo', 'sup', 'gonna', 'wanna', 'lol', 'haha', 'bruh',
        'cool', 'awesome', 'yeah', 'nah', 'ok', 'k', 'thx', 'ty',
        'omg', 'btw', 'imo', 'tbh', 'ngl',
    ])

    _TECH_WORDS = frozenset([
        'api', 'endpoint', 'function', 'class', 'variable', 'database',
        'algorithm', 'deployment', 'configuration', 'infrastructure',
        'repository', 'dependency', 'microservice', 'container', 'pipeline',
        'latency', 'throughput', 'schema', 'query', 'regex',
    ])

    _POSITIVE_WORDS = frozenset([
        'thanks', 'great', 'love', 'perfect', 'excellent', 'amazing',
        'good', 'nice', 'helpful', 'wonderful', 'appreciate',
    ])

    _NEGATIVE_WORDS = frozenset([
        'bad', 'wrong', 'terrible', 'hate', 'awful', 'worse', 'useless',
        'broken', 'frustrated', 'confused', 'disappointed',
    ])

    @classmethod
    def extract(cls, user_message: str, agent_response: str,
                response_time_ms: float = 0.0) -> InteractionSignals:
        """Extract signals from a single exchange."""
        words = user_message.lower().split()
        unique_words = set(words)
        word_count = max(len(words), 1)

        # Formality score
        formal_count = sum(1 for w in words if w in cls._FORMAL_WORDS)
        casual_count = sum(1 for w in words if w in cls._CASUAL_WORDS)
        total_markers = formal_count + casual_count
        if total_markers > 0:
            formality = formal_count / total_markers
        else:
            formality = min(1.0, word_count / 50.0) * 0.5 + 0.25

        tech_count = sum(1 for w in words if w in cls._TECH_WORDS)

        pos = sum(1 for w in words if w in cls._POSITIVE_WORDS)
        neg = sum(1 for w in words if w in cls._NEGATIVE_WORDS)
        if pos + neg > 0:
            sentiment = pos / (pos + neg)
        else:
            sentiment = 0.5

        ttr = len(unique_words) / word_count if word_count > 5 else 0.5

        return InteractionSignals(
            user_message_length=len(user_message),
            agent_response_length=len(agent_response),
            formality_markers=formality,
            question_count=user_message.count('?'),
            exclamation_count=user_message.count('!'),
            technical_term_count=tech_count,
            positive_sentiment=sentiment,
            response_time_ms=response_time_ms,
            vocabulary_richness=ttr,
        )

    @classmethod
    def signals_to_scores(cls, signals: InteractionSignals) -> List[float]:
        """Convert signals to 8-dim vector matching TUNING_DIM_KEYS order."""
        verbosity_signal = min(1.0, signals.user_message_length / 300.0)
        tech_signal = min(1.0, signals.technical_term_count / 5.0)
        warmth_signal = (signals.positive_sentiment * 0.6 +
                         min(1.0, signals.exclamation_count / 3.0) * 0.4)
        pace_signal = 1.0 - min(1.0, signals.question_count / 3.0) * 0.5
        if signals.user_message_length < 30:
            pace_signal = min(pace_signal + 0.2, 1.0)

        enc_signal = 0.6 if signals.positive_sentiment > 0.6 else max(0.4, signals.positive_sentiment)

        return [
            signals.formality_markers,                                   # formality_score
            verbosity_signal,                                            # verbosity_score
            warmth_signal,                                               # warmth_score
            pace_signal,                                                 # pace_score
            tech_signal,                                                 # technical_depth
            enc_signal,                                                  # encouragement_level
            min(1.0, signals.exclamation_count / 5.0) * 0.3 + 0.2,      # humor_receptivity
            0.5,                                                         # autonomy_preference
        ]


# =====================================================================
# Dialogue Stream Processor — Continuous In-Conversation Tuning
# =====================================================================

@dataclass
class _StreamState:
    """Per-user conversation stream state."""
    messages: List[Tuple[str, str, bool]] = field(default_factory=list)
    started_at: float = 0.0
    last_message_at: float = 0.0


class DialogueStreamProcessor:
    """Processes dialogue as a continuous stream, not just post-response.

    Within a CREATE/REUSE execution, the AutoGen GroupChat exchanges many
    messages. Each user message is a tuning signal. Accumulated and
    streamed to HevolveAI for continuous learning.
    """

    def __init__(self, tuner: 'ResonanceTuner'):
        self._tuner = tuner
        self._streams: Dict[str, _StreamState] = {}
        self._lock = threading.Lock()

    def on_message(self, user_id: str, speaker: str, text: str,
                   is_user_message: bool = False,
                   base_dir: str = None):
        """Called for every message in the GroupChat.

        Only user messages are tuning signals.
        """
        with self._lock:
            if user_id not in self._streams:
                self._streams[user_id] = _StreamState(started_at=time.time())
            stream = self._streams[user_id]
            stream.messages.append((speaker, text, is_user_message))
            stream.last_message_at = time.time()

        if is_user_message and len(text.strip()) > 5:
            agent_response = ""
            with self._lock:
                for spk, txt, is_usr in reversed(stream.messages[:-1]):
                    if not is_usr:
                        agent_response = txt
                        break

            if agent_response:
                self._tuner.analyze_and_tune_async(
                    user_id, text, agent_response, base_dir=base_dir)

    def on_stream_end(self, user_id: str):
        """Clean up stream state when conversation ends."""
        with self._lock:
            self._streams.pop(user_id, None)

    def get_stream_length(self, user_id: str) -> int:
        """Number of messages in active stream."""
        with self._lock:
            stream = self._streams.get(user_id)
            return len(stream.messages) if stream else 0


# =====================================================================
# Core Tuning Engine
# =====================================================================

class ResonanceTuner:
    """Orchestration-layer tuner: EMA blending + signal dispatch to HevolveAI.

    All actual learning (Hebbian, Bayesian, probabilistic, gradient descent)
    happens in HevolveAI. HARTOS extracts signals, applies fast EMA locally,
    and streams everything to HevolveAI for deep learning.
    """

    def __init__(self, alpha: float = EMA_ALPHA,
                 auto_save: bool = True):
        self._alpha = alpha
        self._auto_save = auto_save
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='resonance_tune')
        self._lock = threading.Lock()
        self._stream_processor = DialogueStreamProcessor(self)
        self._stats = {
            'total_tunings': 0,
            'total_identifications': 0,
            'total_oscillations_detected': 0,
            'total_hevolveai_dispatches': 0,
            'total_hevolveai_corrections': 0,
            'total_stream_messages': 0,
        }

    @property
    def stream(self) -> DialogueStreamProcessor:
        """Access the dialogue stream processor."""
        return self._stream_processor

    def analyze_and_tune(self, user_id: str, user_message: str,
                         agent_response: str, response_time_ms: float = 0.0,
                         base_dir: str = None) -> UserResonanceProfile:
        """Full pipeline: extract -> EMA blend -> dispatch to HevolveAI -> save.

        Thread-safe. Called after every agent response.
        """
        profile = get_or_create_profile(user_id, base_dir)
        signals = SignalExtractor.extract(
            user_message, agent_response, response_time_ms)
        signal_scores = SignalExtractor.signals_to_scores(signals)
        profile = self._tune_profile(profile, signals, signal_scores)

        # Dispatch signals to HevolveAI for deep learning (fire-and-forget)
        self._dispatch_to_hevolveai(profile, signal_scores, user_message, agent_response)

        if self._auto_save:
            save_resonance_profile(profile, base_dir)
        with self._lock:
            self._stats['total_tunings'] += 1

        # Broadcast resonance tuning to EventBus
        try:
            from core.platform.events import emit_event
            emit_event('resonance.tuned', {
                'user_id': user_id,
                'confidence': profile.resonance_confidence,
            })
        except Exception:
            pass

        return profile

    def analyze_and_tune_async(self, user_id: str, user_message: str,
                               agent_response: str,
                               response_time_ms: float = 0.0,
                               base_dir: str = None) -> None:
        """Fire-and-forget background tuning (zero latency on response path)."""
        self._executor.submit(
            self.analyze_and_tune, user_id, user_message,
            agent_response, response_time_ms, base_dir)

    def _tune_profile(self, profile: UserResonanceProfile,
                      signals: InteractionSignals,
                      signal_scores: List[float]) -> UserResonanceProfile:
        """EMA blending for fast local response.

        This is the immediate, lightweight tuning that happens in HARTOS.
        The deep learning (Hebbian, Bayesian, etc.) happens asynchronously
        in HevolveAI and corrections flow back via apply_hevolveai_corrections().
        """
        a = self._alpha
        current_vector = [profile.tuning[k] for k in TUNING_DIM_KEYS]

        # EMA blend each dimension
        for i, key in enumerate(TUNING_DIM_KEYS):
            profile.tuning[key] = self._ema(current_vector[i], signal_scores[i], a)

        # Track tuning history for oscillation detection
        snapshot = [profile.tuning[k] for k in TUNING_DIM_KEYS]
        profile.tuning_history.append(snapshot)
        if len(profile.tuning_history) > TUNING_HISTORY_MAXLEN:
            profile.tuning_history = profile.tuning_history[-TUNING_HISTORY_MAXLEN:]

        # Detect oscillation -> flag for HevolveAI correction
        was_oscillating = profile.gradient_active
        profile.gradient_active = self._detect_oscillation(profile.tuning_history)
        if profile.gradient_active and not was_oscillating:
            with self._lock:
                self._stats['total_oscillations_detected'] += 1

        # Metadata
        profile.vocabulary_complexity = self._ema(
            profile.vocabulary_complexity, signals.vocabulary_richness, a)
        profile.total_interactions += 1
        profile.avg_message_length = self._ema(
            profile.avg_message_length, signals.user_message_length, a)
        if signals.response_time_ms > 0:
            profile.avg_response_time_ms = self._ema(
                profile.avg_response_time_ms, signals.response_time_ms, a)
        profile.last_interaction_at = time.time()
        profile.updated_at = time.time()
        profile.resonance_confidence = 1.0 - math.exp(
            -CONFIDENCE_GROWTH_RATE * profile.total_interactions)

        return profile

    def _dispatch_to_hevolveai(self, profile: UserResonanceProfile,
                               signal_scores: List[float],
                               user_message: str, agent_response: str):
        """Stream resonance signals to HevolveAI for deep learning.

        HevolveAI activates its full learning stack (Hebbian, Bayesian,
        probabilistic, gradient descent) on these signals. Corrections
        flow back via apply_hevolveai_corrections().
        """
        try:
            from integrations.agent_engine.world_model_bridge import get_world_model_bridge
            bridge = get_world_model_bridge()

            # Embed resonance metadata in the experience payload
            # HevolveAI's learning pipeline picks this up automatically
            bridge.record_interaction(
                user_id=profile.user_id,
                prompt_id='resonance_tuning',
                prompt=user_message[:500],
                response=agent_response[:500],
                model_id='resonance_signal_stream',
                latency_ms=0,
                node_id=None,
                goal_id=None,
            )

            # If oscillation detected, request explicit correction
            if profile.gradient_active:
                bridge.submit_correction(
                    original_response=str({k: profile.tuning[k] for k in TUNING_DIM_KEYS}),
                    corrected_response='',
                    expert_id='resonance_oscillation_detector',
                    confidence=0.5,
                    explanation='Resonance tuning oscillation detected',
                    context={
                        'type': 'resonance_oscillation_correction',
                        'user_id': profile.user_id,
                        'signal_scores': signal_scores,
                        'current_tuning': [profile.tuning[k] for k in TUNING_DIM_KEYS],
                        'tuning_history': profile.tuning_history,
                        'confidence': profile.resonance_confidence,
                    },
                )

            with self._lock:
                self._stats['total_hevolveai_dispatches'] += 1
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"HevolveAI dispatch skipped: {e}")

    def apply_hevolveai_corrections(self, user_id: str, corrections: dict,
                                    base_dir: str = None):
        """Apply learning corrections from HevolveAI back to profile.

        Closes the loop: signals -> HevolveAI learning -> corrections -> profile.
        Called when WorldModelBridge receives feedback from HevolveAI.
        """
        tuning_corrections = corrections.get('tuning_corrections')
        if not tuning_corrections or not isinstance(tuning_corrections, list):
            return
        if len(tuning_corrections) != TUNING_DIM_COUNT:
            return

        profile = get_or_create_profile(user_id, base_dir)

        for i, key in enumerate(TUNING_DIM_KEYS):
            corrected = max(0.0, min(1.0, tuning_corrections[i]))
            # Blend: 70% current (local), 30% HevolveAI correction
            profile.tuning[key] = profile.tuning[key] * 0.7 + corrected * 0.3

        profile.gradient_active = False
        profile.updated_at = time.time()

        if self._auto_save:
            save_resonance_profile(profile, base_dir)

        with self._lock:
            self._stats['total_hevolveai_corrections'] += 1
        logger.debug(f"Applied HevolveAI corrections for user {user_id}")

    @staticmethod
    def _detect_oscillation(tuning_history: List[List[float]]) -> bool:
        """Check if tuning is oscillating (not converging).

        Flags for HevolveAI gradient correction when variance exceeds threshold.
        """
        if len(tuning_history) < 5:
            return False

        recent = tuning_history[-TUNING_HISTORY_MAXLEN:]
        n_dims = len(recent[0]) if recent else 0

        for d in range(n_dims):
            values = [snap[d] for snap in recent]
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            if variance > OSCILLATION_VARIANCE_THRESHOLD:
                return True
        return False

    # ─── Federation: Export / Import ────────────────────────────────

    def export_resonance_delta(self, base_dir: str = None) -> dict:
        """Export anonymized local resonance stats for federation.

        No individual user IDs or biometric data cross node boundaries.
        Only aggregated tuning distributions and interaction counts.
        """
        base_dir = base_dir or RESONANCE_STORAGE_DIR
        if not os.path.isdir(base_dir):
            return {}

        n = TUNING_DIM_COUNT
        tuning_sums = [0.0] * n
        tuning_sq_sums = [0.0] * n
        user_count = 0
        total_interactions = 0
        oscillation_count = 0

        try:
            for fname in os.listdir(base_dir):
                if not fname.endswith('_resonance.json'):
                    continue
                uid = fname.replace('_resonance.json', '')
                profile = load_resonance_profile(uid, base_dir)
                if profile is None or profile.total_interactions < MIN_INTERACTIONS_FOR_TUNING:
                    continue

                user_count += 1
                total_interactions += profile.total_interactions
                if profile.gradient_active:
                    oscillation_count += 1

                for i, key in enumerate(TUNING_DIM_KEYS):
                    val = profile.tuning.get(key, 0.5)
                    tuning_sums[i] += val
                    tuning_sq_sums[i] += val * val
        except Exception as e:
            logger.debug(f"Resonance delta export error: {e}")
            return {}

        if user_count == 0:
            return {}

        avg_tuning = [s / user_count for s in tuning_sums]
        tuning_variance = [
            tuning_sq_sums[i] / user_count - avg_tuning[i] ** 2
            for i in range(n)
        ]

        return {
            'type': 'resonance_delta',
            'user_count': user_count,
            'total_interactions': total_interactions,
            'oscillation_count': oscillation_count,
            'avg_tuning': avg_tuning,
            'tuning_variance': tuning_variance,
            'dim_keys': list(TUNING_DIM_KEYS),
            'timestamp': time.time(),
        }

    def import_hive_resonance(self, aggregated: dict,
                              base_dir: str = None):
        """Apply hive-aggregated tuning insights to local profiles.

        Nudges local profiles toward hive consensus for dimensions that
        have high local variance (uncertain). Well-tuned local dims
        are preserved (70% local, 30% hive).
        """
        hive_avg = aggregated.get('avg_tuning')
        if not hive_avg or len(hive_avg) != TUNING_DIM_COUNT:
            return

        base_dir = base_dir or RESONANCE_STORAGE_DIR
        if not os.path.isdir(base_dir):
            return

        try:
            for fname in os.listdir(base_dir):
                if not fname.endswith('_resonance.json'):
                    continue
                uid = fname.replace('_resonance.json', '')
                profile = load_resonance_profile(uid, base_dir)
                if profile is None or profile.total_interactions < MIN_INTERACTIONS_FOR_TUNING:
                    continue

                for i, key in enumerate(TUNING_DIM_KEYS):
                    local_val = profile.tuning.get(key, 0.5)
                    hive_val = hive_avg[i]
                    profile.tuning[key] = local_val * 0.7 + hive_val * 0.3

                profile.updated_at = time.time()
                save_resonance_profile(profile, base_dir)
        except Exception as e:
            logger.debug(f"Hive resonance import error: {e}")

    @staticmethod
    def _ema(current: float, new_value: float, alpha: float) -> float:
        """Exponential moving average."""
        return current * (1 - alpha) + new_value * alpha

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)


# =====================================================================
# Prompt Builder
# =====================================================================

def build_resonance_prompt(profile: UserResonanceProfile) -> str:
    """Generate system_message addon reflecting current tuning state."""
    if profile.total_interactions < MIN_INTERACTIONS_FOR_TUNING:
        return ""

    t = profile.tuning
    confidence_pct = int(profile.resonance_confidence * 100)

    formality = _score_to_label(t.get('formality_score', 0.5),
                                ['very casual', 'casual', 'neutral',
                                 'somewhat formal', 'very formal'])
    verbosity = _score_to_label(t.get('verbosity_score', 0.5),
                                ['extremely brief', 'concise', 'balanced',
                                 'detailed', 'very thorough'])
    warmth = _score_to_label(t.get('warmth_score', 0.5),
                             ['professionally distant', 'polite', 'friendly',
                              'warm', 'very warm and personal'])
    pace = _score_to_label(t.get('pace_score', 0.5),
                           ['very thorough/slow', 'thorough', 'balanced pace',
                            'brisk', 'fast and action-oriented'])
    tech = _score_to_label(t.get('technical_depth', 0.5),
                           ['very simple language', 'simple', 'moderate',
                            'technical', 'highly technical'])
    encouragement = _score_to_label(t.get('encouragement_level', 0.5),
                                     ['matter-of-fact', 'light encouragement',
                                      'encouraging', 'warmly encouraging',
                                      'highly celebratory'])

    return f"""
RESONANCE TUNING (learned from {profile.total_interactions} interactions, {confidence_pct}% confidence):
This user prefers:
- Formality: {formality}
- Detail level: {verbosity}
- Warmth: {warmth}
- Pace: {pace}
- Technical depth: {tech}
- Encouragement style: {encouragement}
Adapt your responses to match these preferences naturally. Do not mention this tuning to the user.
"""


def _score_to_label(score: float, labels: list) -> str:
    """Map a 0.0-1.0 score to one of N labels."""
    idx = min(int(score * len(labels)), len(labels) - 1)
    return labels[idx]


# =====================================================================
# Singleton
# =====================================================================

_tuner = None
_tuner_lock = threading.Lock()


def get_resonance_tuner() -> ResonanceTuner:
    """Get or create the singleton ResonanceTuner."""
    global _tuner
    if _tuner is None:
        with _tuner_lock:
            if _tuner is None:
                _tuner = ResonanceTuner()
    return _tuner
