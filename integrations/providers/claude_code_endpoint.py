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
    invoke_claude, classify_failure, claude_code_available,
    DEFAULT_INFERENCE_TIMEOUT_S,
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
    # 'notfound' means the `claude` binary is not on this node at all. That is
    # the SAME situation as a lapsed subscription, which this module already
    # degrades rather than errors ("a lapsed subscription must not error the
    # OS; it degrades to local"). It was mapped to 502, so a node without the
    # binary returned an api_error instead of riding the fallback ladder down
    # to a local model.
    #
    # Measured on central 2026-09-01: POST /chat/completions ->
    # 502 {"message": "claude not on PATH"}. Central never registers the
    # backend (model_registry gates on claude_code_available(), which is False
    # there) so nothing in-process routed to it, but anything probing the
    # endpoint directly got a hard error where a degrade was intended.
    'notfound': 503,
    'other': 502,
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


def _tools_to_prompt(tools):
    """OpenAI tools[] -> an instruction block appended to the prompt.

    In autogen the LLM never EXECUTES a tool: it names one, and the executor
    agent runs the registered Python closure in HARTOS's own process.  So a
    text-completion engine can serve a tool turn perfectly well — it only has
    to name the call.  `--allowedTools ''` (claude_code_backend, mode
    'inference') stays exactly as it is: that flag governs Claude's OWN tools
    (file edits, bash, web), which must remain off.  These are HARTOS's.
    """
    lines = []
    for t in tools or []:
        fn = (t or {}).get('function') or {}
        name = fn.get('name')
        if not name:
            continue
        params = ', '.join((fn.get('parameters') or {}).get('properties') or {})
        desc = (fn.get('description') or '').strip()
        lines.append('- %s(%s)%s' % (name, params, ' — ' + desc if desc else ''))
    if not lines:
        return ''
    return (
        '\n\nYou may call ONE of the tools below. To call one, reply with ONLY '
        'a JSON object and nothing else:\n'
        '{"name": "<tool name>", "arguments": {<arguments>}}\n'
        'No prose before or after it. If no tool is needed, just answer '
        'normally in plain text.\n\nTools:\n' + '\n'.join(lines))


def _parse_tool_call(text, tools):
    """Model reply -> OpenAI tool_calls list, or None when it is plain prose.

    Argument repair is autogen's OWN helper — the same
    ConversableAgent._format_json_str that execute_function uses before
    json.loads — so a reply this endpoint accepts is exactly a reply autogen
    can execute.  Nothing bespoke is parsed here.
    """
    offered = {((t or {}).get('function') or {}).get('name')
               for t in (tools or [])}
    offered.discard(None)
    if not offered:
        return None
    blob = (text or '').strip()
    if blob.startswith('```'):                      # ```json … ``` fence
        blob = blob.split('```')[1] if '```' in blob[3:] else blob[3:]
        if blob.lstrip().startswith('json'):
            blob = blob.lstrip()[4:]
        blob = blob.strip()
    if not blob.startswith('{'):
        return None
    try:
        call = json.loads(blob)
    except ValueError:
        try:
            from autogen.agentchat.conversable_agent import ConversableAgent
            call = json.loads(ConversableAgent._format_json_str(blob))
        except Exception:
            return None
    if not isinstance(call, dict):
        return None
    name = call.get('name')
    # A name the caller never offered is prose, not a call: emitting it would
    # hand autogen a tool_call it cannot execute, and the turn would die
    # holding an unanswerable tool_call_id.
    if name not in offered:
        return None
    args = call.get('arguments')
    if not isinstance(args, str):
        args = json.dumps(args if isinstance(args, dict) else {})
    return [{
        'id': 'call_claudecode_%d' % int(time.time() * 1000),
        'type': 'function',
        'function': {'name': name, 'arguments': args},
    }]


@claude_code_bp.route('/chat/completions', methods=['POST'])
def chat_completions():
    data = request.get_json(silent=True) or {}
    messages = data.get('messages') or []
    model = data.get('model') or 'claude-code'
    system_text, prompt = _messages_to_prompt(messages)
    if not prompt:
        return jsonify({'error': {'message': 'no user content in messages',
                                  'type': 'invalid_request_error'}}), 400

    # A tool turn is served here like on any other OpenAI endpoint: carry
    # tools[] into the prompt so the model can NAME a call.  It never executes
    # one — autogen's executor runs the registered closure in HARTOS's process,
    # exactly as it does for the local model.
    #
    # This replaces a guard that 503'd every request carrying tools[].  That
    # guard read `--allowedTools ''` (which correctly disables Claude's OWN
    # tools) as "this tier cannot do tools at all" and refused work it could
    # do.  Measured live 2026-09-05 (CREATE of agent 88601674818): the 503 was
    # classified by agent_lightning/wrapper as a recoverable GENERATION failure
    # (status >= 500), re-sampled twice against a deterministic refusal, then
    # answered with a canned reply — burning the create loop's retry budget in
    # 212 ms and stalling the HITL ask forever (3/3, ~0.3 s each).
    #
    # The 2026-09-03 symptom the guard was reacting to ("web search is blocked
    # … here's from my knowledge instead") was this shim never sending tools[]
    # at all: the model was asked to use tools it had never been shown.
    tools = data.get('tools') or []
    if tools:
        prompt += _tools_to_prompt(tools)

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
    # A named tool becomes the standard tool_calls shape, so autogen pairs the
    # result by tool_call_id and executes it like any other provider's call.
    _calls = _parse_tool_call(text, tools)
    if _calls:
        logger.info('claude-code endpoint: model named tool %s',
                    _calls[0]['function']['name'])
        _message = {'role': 'assistant', 'content': None, 'tool_calls': _calls}
        _finish = 'tool_calls'
    else:
        _message = {'role': 'assistant', 'content': text}
        _finish = 'stop'
    return jsonify({
        'id': 'chatcmpl-claudecode-%d' % int(time.time() * 1000),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': _message,
            'finish_reason': _finish,
        }],
        'usage': {'prompt_tokens': pt, 'completion_tokens': ct,
                  'total_tokens': pt + ct},
    }), 200


@claude_code_bp.route('/models', methods=['GET'])
def list_models():
    """Minimal OpenAI /models so a client that probes the endpoint gets a sane
    answer (some SDKs call it before the first completion).

    Reports what this NODE can actually serve. The list used to be a hardcoded
    constant, so a node with no `claude` binary answered 200 with claude-code
    listed while every completion failed — a probe of this route said "healthy"
    about a backend that could not run.

    That is not hypothetical: it cost me an hour on 2026-09-01. central answers
    this route 200 with the model listed, and POST /chat/completions returns
    "claude not on PATH". I read the 200 as the backend working and reasoned
    from it. The blueprint mounts unconditionally (blueprint_registry) while
    model_registry gates registration on claude_code_available(), so the two
    can legitimately disagree — but the ROUTE should not claim capability the
    node lacks.
    """
    if not claude_code_available():
        return jsonify({'object': 'list', 'data': []}), 200
    return jsonify({'object': 'list', 'data': [
        {'id': 'claude-code', 'object': 'model', 'owned_by': 'hartos'}]}), 200
