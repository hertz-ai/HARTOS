"""Claude Code as an OpenAI-compatible completion endpoint — HARTOS's frontier
(EXPERT) inference tier, flow #3.

Autogen agents make a synchronous blocking POST to a config_list `base_url`
via the openai SDK — the SAME path used for the local llama.cpp servers. This
serves that endpoint and, per request, runs the authorized `claude -p` (via the
SHARED claude_code_backend primitive, mode='inference') and returns the
completion in OpenAI response shape. So autogen uses Claude Code "like any
other LLM" without knowing it is a subprocess.

Resilience is DELEGATED to dispatch.py's existing fallback ladder — this adds
none of its own. On a Claude failure we return the HTTP status that ladder
already treats as transient, so it degrades to the in-house LLM and re-queues:

    overload (Anthropic 529)  -> 503   (circuit breaker + fall to local)
    auth expired              -> 503   (a lapsed subscription must not error
                                        the OS; it degrades to local)
    timeout                   -> 504
    not-found / other         -> 502
    at-capacity (semaphore)   -> 503

Concurrency is capped: each call spawns a `claude -p` process, so a burst of
EXPERT escalations must not fork unboundedly. Over the cap we 503 immediately,
which the ladder reads as "try local" — better a fast local answer than a
piled-up frontier queue.

Mounted by hart_intelligence_entry alongside the other blueprints. The EXPERT
ModelBackend (model_registry) points its base_url here.
"""
import json
import logging
import threading
import time

from flask import Blueprint, request, jsonify

from integrations.coding_agent.claude_code_backend import (
    invoke_claude, classify_failure, DEFAULT_INFERENCE_TIMEOUT_S,
)

logger = logging.getLogger('hartos_claude_code')

# url_prefix ends at /v1 so the openai SDK's base_url = ".../api/claude/v1"
# resolves POST .../chat/completions exactly as it does for any OpenAI host.
claude_code_bp = Blueprint('claude_code', __name__, url_prefix='/api/claude/v1')

import os as _os
_MAX_CONCURRENT = max(1, int(_os.environ.get('HART_CLAUDE_MAX_CONCURRENT', '2')))
_sem = threading.BoundedSemaphore(_MAX_CONCURRENT)

_FAIL_STATUS = {
    'overload': 503, 'auth': 503, 'timeout': 504,
    'notfound': 502, 'other': 502,
}


def _flatten(content):
    """OpenAI message content may be a string or a list of parts. -> text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                out.append(part.get('text') or part.get('content') or '')
            else:
                out.append(str(part))
        return '\n'.join(p for p in out if p)
    return '' if content is None else str(content)


def _messages_to_prompt(messages):
    """OpenAI messages[] -> (system_text, user_prompt) for `claude -p`."""
    systems, convo = [], []
    for m in messages or []:
        role = (m.get('role') or 'user')
        text = _flatten(m.get('content'))
        if not text:
            continue
        if role == 'system':
            systems.append(text)
        else:
            convo.append((role, text))
    system_text = '\n\n'.join(systems) if systems else None
    if len(convo) == 1 and convo[0][0] == 'user':
        prompt = convo[0][1]                       # the common single-turn case
    else:
        # Multi-turn: hand claude the transcript and let it continue as the
        # assistant (the system preamble already tells it to answer directly).
        lines = ['%s: %s' % (r, t) for r, t in convo]
        lines.append('assistant:')
        prompt = '\n'.join(lines)
    return system_text, prompt


@claude_code_bp.route('/chat/completions', methods=['POST'])
def chat_completions():
    data = request.get_json(silent=True) or {}
    messages = data.get('messages') or []
    model = data.get('model') or 'claude-code'
    system_text, prompt = _messages_to_prompt(messages)
    if not prompt:
        return jsonify({'error': {'message': 'no user content in messages',
                                  'type': 'invalid_request_error'}}), 400

    # Cap concurrent claude -p processes. Non-blocking acquire: at capacity we
    # 503 so the caller's fallback picks a local model instead of queueing.
    if not _sem.acquire(blocking=False):
        logger.warning('claude-code endpoint at capacity (%d) — 503 to fallback',
                       _MAX_CONCURRENT)
        return jsonify({'error': {'message': 'claude-code at capacity',
                                  'type': 'overloaded_error'}}), 503
    try:
        result = invoke_claude(prompt, mode='inference', system=system_text,
                               timeout_s=DEFAULT_INFERENCE_TIMEOUT_S)
    finally:
        _sem.release()

    cat = classify_failure(result)
    if cat is not None:
        status = _FAIL_STATUS.get(cat, 502)
        logger.warning('claude-code inference failed (%s) -> HTTP %d; caller '
                       'falls back to local', cat, status)
        return jsonify({'error': {
            'message': result.get('error') or (result.get('stderr') or '')[-300:],
            'type': 'overloaded_error' if cat in ('overload', 'auth') else 'api_error',
            'category': cat,
        }}), status

    text = (result.get('stdout') or '').strip()
    # OpenAI chat.completion response shape. Token counts are rough (len/4) so
    # cost/telemetry consumers do not choke; price is [0,0] in the config so
    # the number never turns into a charge.
    pt = max(1, len(prompt) // 4)
    ct = max(1, len(text) // 4)
    return jsonify({
        'id': 'chatcmpl-claudecode-%d' % int(time.time() * 1000),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': {'role': 'assistant', 'content': text},
            'finish_reason': 'stop',
        }],
        'usage': {'prompt_tokens': pt, 'completion_tokens': ct,
                  'total_tokens': pt + ct},
    }), 200


@claude_code_bp.route('/models', methods=['GET'])
def list_models():
    """Minimal OpenAI /models so a client that probes the endpoint gets a sane
    answer (some SDKs call it before the first completion)."""
    return jsonify({'object': 'list', 'data': [
        {'id': 'claude-code', 'object': 'model', 'owned_by': 'hartos'}]}), 200
