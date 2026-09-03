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

    Registered as a virtual subclass of autogen.Agent so isinstance()
    checks pass in GroupChat speaker selection and transition validation.
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
        # Ensure the autogen.Agent ABC registration is in place before this
        # wrapper participates in any GroupChat isinstance() check (deferred
        # off module-load to keep autogen out of the boot import — see the
        # _ensure_autogen_agent_registration docstring below).
        _ensure_autogen_agent_registration()

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

                # ── HIGH #5: llama.cpp tool-call JSON parse 500 graceful fallback ──
                # When the model emits a tool-call whose `arguments` field
                # contains unescaped quotes / emojis / truncated strings,
                # llama.cpp rejects the response with HTTP 500 and the
                # signature `Failed to parse tool call arguments as JSON`
                # (live evidence langchain.log 2026-05-14: 22 occurrences).
                # Today the exception propagates up → get_response_group
                # transitions the action to `error` → lifecycle FSM rejects
                # the transition repeatedly (600+ "Invalid transition"
                # lines) → the STUCK-LOOP guard at create_recipe.py:1954
                # eventually fires after 5 stuck iterations and emits a
                # generic fallback.  Net cost: ~20-30 wasted llama.cpp
                # completions before recovery.
                #
                # Short-circuit here instead: detect the parse-500 by its
                # canonical error-message signature, return a coherent
                # fallback string immediately, and let autogen treat it as
                # a normal reply.  No lifecycle churn, no extra LLM
                # completions, user gets a single clean reply within the
                # same turn.  Same fallback pattern as the loop-break
                # guard so the user-visible UX is consistent across both
                # failure modes.
                _err_str = str(e)
                _is_toolcall_parse_500 = (
                    '500' in _err_str
                    and 'Failed to parse tool call arguments' in _err_str
                )
                if _is_toolcall_parse_500:
                    # A malformed-JSON tool call is almost always a
                    # SAMPLING FLUKE — one bad token (unescaped quote,
                    # emoji, mid-string truncation) in an otherwise-valid
                    # structure.  Autogen agents sample at temp>0, so
                    # simply re-running generate_reply re-samples and the
                    # fluke usually clears.  Retry up to _TOOLCALL_500_RETRIES
                    # times BEFORE giving up — this converts "tool call
                    # always lost on a bad sample" into "tool call
                    # recovered when it's a fluke (the common case)".
                    _TOOLCALL_500_RETRIES = 2
                    for _attempt in range(1, _TOOLCALL_500_RETRIES + 1):
                        logger.warning(
                            "[TOOLCALL-PARSE-500] malformed tool-call JSON "
                            "(attempt %d/%d) — re-sampling generate_reply. "
                            "Error: %s",
                            _attempt, _TOOLCALL_500_RETRIES, _err_str[:200])
                        try:
                            _retry_result = original_func(*args, **kwargs)
                            logger.info(
                                "[TOOLCALL-PARSE-500] recovered on retry %d "
                                "— re-sample produced parseable output",
                                _attempt)
                            return _retry_result
                        except Exception as _retry_exc:
                            _err_str = str(_retry_exc)
                            if not ('500' in _err_str and
                                    'Failed to parse tool call arguments'
                                    in _err_str):
                                # A DIFFERENT failure on retry — stop
                                # retrying, fall through to report+fallback.
                                break

                    # Retries exhausted with the SAME (tools-bearing) request.
                    # Final recovery: the model keeps botching tool-call JSON,
                    # but the conversation already carries the tool RESULTS it
                    # needs — measured 2026-09-03 on the Auto Research agent,
                    # google_search returned real 2024 articles into history and
                    # then the synthesis turn 500'd on its OWN malformed tool
                    # call.  Re-sampling with tools present just reproduces the
                    # bad call.  Force ONE tool-less resample so the model
                    # synthesises PROSE from what it already has instead of
                    # emitting another unparseable call.  Reuses AutoGen's own
                    # client construction (OpenAIWrapper), restored in finally;
                    # only fires after the tools-bearing retries are exhausted,
                    # so the happy path and the "recovered on retry" path above
                    # are untouched.
                    try:
                        _cfg = getattr(self.agent, 'llm_config', None)
                        if isinstance(_cfg, dict) and _cfg.get('tools'):
                            from autogen import OpenAIWrapper
                            _saved_cfg = _cfg
                            _saved_client = getattr(self.agent, 'client', None)
                            try:
                                _cfg_notools = {k: v for k, v in _cfg.items()
                                                if k != 'tools'}
                                self.agent.llm_config = _cfg_notools
                                self.agent.client = OpenAIWrapper(**_cfg_notools)
                                _text = original_func(*args, **kwargs)
                            finally:
                                self.agent.llm_config = _saved_cfg
                                if _saved_client is not None:
                                    self.agent.client = _saved_client
                            if _text:
                                logger.info(
                                    "[TOOLCALL-PARSE-500] recovered via "
                                    "tool-less synthesis — model produced a "
                                    "reply from the tool results already in "
                                    "history instead of a fresh tool call")
                                return _text
                    except Exception as _tl_e:
                        logger.warning(
                            "[TOOLCALL-PARSE-500] tool-less synthesis retry "
                            "failed (%s) — falling back to the apology string",
                            _tl_e)

                    # Route to the canonical self-heal pipeline so a
                    # SUSTAINED pattern of toolcall-500s on this agent's
                    # model creates a self_heal goal (e.g. "enable
                    # grammar-constrained / json_schema tool output on the
                    # llama-server request for <model>").  Same helper
                    # every subsystem uses — pattern_key
                    # 'RuntimeError::llm.<agent_id>::generate_reply' lets
                    # SelfHealingDispatcher cluster repeated occurrences.
                    try:
                        from hartos.exception_collector import report_subsystem_failure
                        report_subsystem_failure(
                            subsystem='llm',
                            identifier=str(self.agent_id),
                            exc=e,
                            function='generate_reply',
                            failure_kind='toolcall_json_parse_500',
                            retries_exhausted=_TOOLCALL_500_RETRIES,
                        )
                    except Exception:
                        pass

                    logger.error(
                        "[TOOLCALL-PARSE-500] llama.cpp rejected the "
                        "model's tool-call output after %d retries "
                        "(malformed JSON in arguments — unescaped quotes / "
                        "emojis / mid-string truncation).  Returning "
                        "graceful fallback string instead of propagating "
                        "the 500, to avoid lifecycle FSM churn.  Error: "
                        "%s", _TOOLCALL_500_RETRIES, _err_str[:300])
                    return (
                        "I had trouble producing the structured response "
                        "for that — the underlying model emitted output "
                        "that couldn't be parsed.  Could you rephrase the "
                        "request, or try breaking it into smaller steps?"
                    )

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


# Register as virtual subclass of autogen.Agent so isinstance() checks pass
# in GroupChat (speaker selection, transition validation, graph validity).
# This is the ABC way to say "this class IS-A Agent" without inheriting.
#
# Deferred (was a module-level `import autogen; autogen.Agent.register(...)`):
# importing autogen at module-load time drags google.api_core (~7.6s) +
# the contrib->llmlingua->torch (~4.2s) chain onto the backend-boot path
# (this module is imported transitively via hart_intelligence_entry ->
# create_recipe).  The registration only needs to be in place BEFORE the
# first `isinstance(wrapper, autogen.Agent)` check in GroupChat — and by
# the time ANY wrapper is constructed autogen is already imported (the
# agent being wrapped is itself an autogen agent).  So we register once,
# lazily, from __init__.  IS-A semantics are byte-for-byte identical.
_AUTOGEN_ABC_REGISTERED = False


def _ensure_autogen_agent_registration() -> None:
    """Idempotently register this class as a virtual subclass of
    ``autogen.Agent``.  Safe no-op if autogen is missing or its Agent ABC
    doesn't support ``register()``.  Called from __init__ (autogen is
    guaranteed loaded by then)."""
    global _AUTOGEN_ABC_REGISTERED
    if _AUTOGEN_ABC_REGISTERED:
        return
    try:
        import autogen
        autogen.Agent.register(AgentLightningWrapper)
        _AUTOGEN_ABC_REGISTERED = True
    except (ImportError, AttributeError):
        # autogen not installed or Agent doesn't support register().
        # Mark done so we don't retry on every construction.
        _AUTOGEN_ABC_REGISTERED = True


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
