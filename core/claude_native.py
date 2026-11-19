"""Claude's own Messages API for the chat/completions calls HARTOS sends to Claude.

Owner, 2026-11-11 (relayed by the investor session): "implement prompt
caching properly without degradation in agentic skill".

Every Claude call HARTOS makes (the configured expert on central) is an
OpenAI-shaped chat/completions request, because autogen and langchain speak
nothing else, sent to Anthropic's OpenAI compatibility layer at
``api.anthropic.com/v1/chat/completions``.  That layer does not cache prompts
(its own documentation: "Prompt caching is not supported").  Measured on
central 2026-11-11: about 17k input tokens per call, 55 calls in 8 daemon
turns, every token billed at the full input price.

This module turns such a request into the Messages API request the layer
would make from it, adds cache breakpoints, and turns the reply back into the
chat completion the caller reads.  The wire hook (core.llm_outbound_logger)
calls it, for the node's configured endpoints only; no caller changes.

What the model sees does not change.  The compatibility layer's documented
rules are followed one by one:
  * system and developer messages, wherever they sit, are joined with one
    newline into the top-level system prompt;
  * thinking stays at the model's default (on, for Claude 5 models) and the
    reply carries no thinking;
  * the fields the layer ignores are dropped, temperature is capped at 1, and
    a ``tool`` message becomes a ``tool_result`` block in a user turn.
Two things the layer cannot do are added: the effort the owner set for the
configured expert (HEVOLVE_EXPERT_LLM_EFFORT, as output_config.effort), and
leaving out a temperature or top_p the model would refuse (_SAMPLING_FIXED_MODELS).
Anything not mapped one to one here (streaming, ``response_format``, audio or
file parts, legacy ``function_call`` messages, ``n`` other than 1, a field this
module does not know, a conversation that does not start with a user turn)
raises Untranslatable, and the hook sends the request to the compatibility
layer exactly as it came: that call loses caching and the owner's effort
setting, nothing else.

Two turn shapes that autogen's group chats produce are refused by the
Messages API AND by the compatibility layer, with a 400 that failed the turn
(central, 2026-11-19, claude-haiku-5-5).  They are repaired here, and only
here, so the node's other models keep receiving what they always did
(_paired_tool_turns):
  * a tool call must be answered in the very next user turn, its results
    first there, and a result needs its call in the turn before it ('tool_use
    ids were found without tool_result blocks immediately after').  A call
    with no result right after it is left out; a result with no call right
    before it goes as text; split results are put ahead of the rest;
  * the conversation must end on a user turn ('This model does not support
    assistant message prefill', every current Claude model).  The model's
    own last turn stays where it is, and core.constants.WIRE_USER_SEED_TEXT,
    the one user turn the wire already adds where a body has none
    (llm_outbound_logger.ensure_user_turn), follows it.

Where the cache breakpoints go (a cache hit is a byte-identical prefix of
tools, then system, then messages):
  * the end of the system prompt, when every system message leads the
    conversation: the agent's fixed instructions plus its tools, the bulk of
    every call, cached together;
  * the last tool instead, when a system message sits later in the
    conversation, because the joined system prompt then changes with it;
  * the request's automatic breakpoint (top-level ``cache_control``) on the
    growing conversation, so each call of a tool loop reads the previous
    call's prefix.
Both are 5-minute entries (Anthropic's default): the calls of one turn are
seconds apart, and a 5-minute write costs 1.25x the input price where a
1-hour write costs 2x.
"""
import base64
import json
import logging
import time

logger = logging.getLogger('llm_outbound')

ANTHROPIC_HOST = 'api.anthropic.com'
COMPAT_SUFFIX = '/chat/completions'
MESSAGES_SUFFIX = '/messages'

# The version whose request rules the compatibility layer keeps: it passes
# temperature and top_p through (2026-10-21 refuses both on every request).
API_VERSION = '2023-06-01'

# The Messages API requires max_tokens and autogen never sends it.  How the
# compatibility layer fills it in is not documented; 32,000 is within every
# current Claude model's output limit and well above an agent turn's reply
# plus its thinking.
MAX_TOKENS_WHEN_UNSET = 32000

_EPHEMERAL = {'type': 'ephemeral'}

# Models that answer 400 to a sampling parameter (Anthropic's reference):
# a non-default temperature or top_p on Claude Sonnet 5 / 5.5 and Haiku 5.5,
# any value on Claude Opus 4.7 and later and on every Fable and Mythos.  The
# compatibility layer passes both through, so there such a call fails; here
# they are left out and the model samples at its default, which is what an
# omitted value means.  Matched by substring, like core.autogen_config's
# model_family.
_SAMPLING_FIXED_MODELS = ('claude-sonnet-5', 'claude-haiku-5', 'claude-opus-4-7',
                          'claude-opus-4-8', 'claude-opus-5', 'claude-fable',
                          'claude-mythos')

# Request fields the compatibility layer documents as ignored.
_IGNORED_FIELDS = frozenset({
    'logprobs', 'metadata', 'prediction', 'presence_penalty',
    'frequency_penalty', 'seed', 'service_tier', 'audio', 'logit_bias',
    'store', 'user', 'modalities', 'top_logprobs', 'reasoning_effort',
})
_MAPPED_FIELDS = frozenset({
    'model', 'messages', 'max_tokens', 'max_completion_tokens', 'stream',
    'temperature', 'top_p', 'stop', 'tools', 'tool_choice',
    'parallel_tool_calls', 'n',
})

# stop_reason -> finish_reason.  A reason not listed reads as 'stop'.
_FINISH_REASONS = {
    'end_turn': 'stop',
    'stop_sequence': 'stop',
    'pause_turn': 'stop',
    'max_tokens': 'length',
    'model_context_window_exceeded': 'length',
    'tool_use': 'tool_calls',
    'refusal': 'content_filter',
}

# Response headers that describe the native body, not the one sent back.
_BODY_HEADERS = frozenset({
    'content-length', 'content-encoding', 'transfer-encoding',
    'content-type', 'connection',
})
# Request headers the native call sets itself.
_REPLACED_REQUEST_HEADERS = frozenset({
    'authorization', 'x-api-key', 'anthropic-version', 'content-length',
    'content-type', 'host',
})


class Untranslatable(ValueError):
    """The request uses something this module does not map one to one; it
    goes to the compatibility layer as it came."""


def is_compat_call(url) -> bool:
    """Is ``url`` (an httpx URL) Anthropic's chat/completions endpoint?"""
    host = str(getattr(url, 'host', '') or '').lower()
    path = str(getattr(url, 'path', '') or '')
    return host == ANTHROPIC_HOST and path.endswith(COMPAT_SUFFIX)


def messages_path(compat_path: str) -> str:
    """``/v1/chat/completions`` -> ``/v1/messages``."""
    return compat_path[:-len(COMPAT_SUFFIX)] + MESSAGES_SUFFIX


# ─── Request ─────────────────────────────────────────────────────────────

def messages_body(body) -> dict:
    """The Messages API body for a chat/completions ``body``, with its cache
    breakpoints.  Raises Untranslatable for anything not mapped one to one."""
    if not isinstance(body, dict):
        raise Untranslatable('the body is not a JSON object')
    unknown = sorted(set(body) - _MAPPED_FIELDS - _IGNORED_FIELDS)
    if unknown:
        raise Untranslatable('field %s is not mapped' % ', '.join(unknown))
    if body.get('stream'):
        raise Untranslatable('streaming')
    if body.get('n') not in (None, 1):
        raise Untranslatable('n other than 1')
    model = body.get('model')
    if not isinstance(model, str) or not model:
        raise Untranslatable('no model')

    system_texts, system_leads, turns = _conversation(body.get('messages'))
    native = {'model': model, 'max_tokens': _max_tokens(body)}
    tools = _tools(body.get('tools'))
    if tools:
        native['tools'] = tools
        choice = _tool_choice(body.get('tool_choice'), body.get('parallel_tool_calls'))
        if choice is not None:
            native['tool_choice'] = choice

    system = '\n'.join(system_texts)
    if system.strip() and system_leads:
        native['system'] = [{'type': 'text', 'text': system,
                             'cache_control': dict(_EPHEMERAL)}]
    else:
        if system.strip():
            native['system'] = system
        if tools:
            tools[-1]['cache_control'] = dict(_EPHEMERAL)
    native['messages'] = turns
    native['cache_control'] = dict(_EPHEMERAL)

    from core.autogen_config import expert_effort
    effort = expert_effort(model)
    if effort:
        native['output_config'] = {'effort': effort}

    sampling_fixed = any(name in model.lower() for name in _SAMPLING_FIXED_MODELS)
    temperature = body.get('temperature')
    if temperature is not None and not sampling_fixed:
        native['temperature'] = min(float(temperature), 1.0)
    if body.get('top_p') is not None and not sampling_fixed:
        native['top_p'] = body['top_p']
    stop = body.get('stop')
    if isinstance(stop, str) and stop:
        native['stop_sequences'] = [stop]
    elif isinstance(stop, list) and stop:
        native['stop_sequences'] = list(stop)
    return native


def _max_tokens(body) -> int:
    for key in ('max_completion_tokens', 'max_tokens'):
        value = body.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise Untranslatable('%s is not a positive integer' % key)
        return value
    return MAX_TOKENS_WHEN_UNSET


def _conversation(messages):
    """(system texts in order, whether every system message leads the
    conversation, the user/assistant turns with same-role neighbours merged,
    each tool call paired with its results and the last turn the user's)."""
    if not isinstance(messages, list) or not messages:
        raise Untranslatable('no messages')
    system_texts, system_leads, seen_turn, turns = [], True, False, []
    for message in messages:
        if not isinstance(message, dict):
            raise Untranslatable('a message is not an object')
        role = message.get('role')
        if role in ('system', 'developer'):
            content = message.get('content')
            if not isinstance(content, str):
                raise Untranslatable('a %s message whose content is not text' % role)
            system_texts.append(content)
            if seen_turn:
                system_leads = False
            continue
        seen_turn = True
        if role == 'user':
            native_role, blocks = 'user', _user_blocks(message.get('content'))
        elif role == 'assistant':
            native_role, blocks = 'assistant', _assistant_blocks(message)
        elif role == 'tool':
            native_role, blocks = 'user', [_tool_result(message)]
        else:
            raise Untranslatable('role %r' % (role,))
        if turns and turns[-1]['role'] == native_role:
            turns[-1]['content'].extend(blocks)
        else:
            turns.append({'role': native_role, 'content': blocks})
    turns = _paired_tool_turns(turns)
    if not turns:
        raise Untranslatable('no user or assistant message')
    # The Messages API answers 400 to a first message that is not the user's.
    # Sent as it came, the compatibility layer is the one to judge it: 114 of
    # 1,101 real request bodies on the owner's desktop started with an
    # assistant turn (review of 2fc216a38, MF-1).
    if turns[0]['role'] != 'user':
        raise Untranslatable('the conversation does not start with a user turn')
    # A last assistant turn is a prefill, refused by every current Claude
    # model on both endpoints (central 2026-11-19 20:58:22: the verifier,
    # asked to speak after an empty turn the role-order guard dropped, ended
    # on its own verdict).  The model's turn stays; a user turn follows it.
    if turns[-1]['role'] == 'assistant':
        from core.constants import WIRE_USER_SEED_TEXT
        turns.append({'role': 'user', 'content': [_text_block(WIRE_USER_SEED_TEXT)]})
        logger.info("[claude-native] the conversation ended on the model's own "
                    "turn; a user turn (WIRE_USER_SEED_TEXT) follows it")
    return system_texts, system_leads, turns


def _blocks_of(turn, kind):
    return [b for b in turn['content'] if b['type'] == kind]


def _paired_tool_turns(turns):
    """``turns`` with every tool call answered in the very next user turn,
    its results ahead of anything else there, and no result without its call
    in the turn before it: the Messages API refuses anything else (central
    2026-11-19 21:01:14, 'tool_use ids were found without tool_result blocks
    immediately after').  A call whose result is not right after it is left
    out; a result whose call is not right before it goes as a text block
    (ToolMessageHandler's own rule for an orphaned tool message: its content
    as text); a turn left with nothing is dropped and its neighbours merged.
    Repeated until nothing changes, since a merge can move a result."""
    left_out = as_text = 0
    while True:
        changed = False
        for i, turn in enumerate(turns):
            before = turns[i - 1] if i else None
            after = turns[i + 1] if i + 1 < len(turns) else None
            if turn['role'] == 'assistant':
                answered = ({b['tool_use_id'] for b in _blocks_of(after, 'tool_result')}
                            if after is not None else set())
                kept = [b for b in turn['content']
                        if b['type'] != 'tool_use' or b['id'] in answered]
                if len(kept) != len(turn['content']):
                    left_out += len(turn['content']) - len(kept)
                    turn['content'], changed = kept, True
                continue
            called = ({b['id'] for b in _blocks_of(before, 'tool_use')}
                      if before is not None and before['role'] == 'assistant' else set())
            results, rest = [], []
            for block in turn['content']:
                if block['type'] != 'tool_result':
                    rest.append(block)
                elif block['tool_use_id'] in called:
                    results.append(block)
                else:
                    as_text += 1
                    changed = True
                    if block.get('content'):
                        rest.append({'type': 'text', 'text': block['content']})
            turn['content'] = results + rest
        if not changed:
            break
        merged = []
        for turn in turns:
            if not any(b['type'] != 'text' or b['text'].strip() for b in turn['content']):
                continue                     # nothing left but blank text
            if merged and merged[-1]['role'] == turn['role']:
                merged[-1]['content'].extend(turn['content'])
            else:
                merged.append(turn)
        turns = merged
    if left_out or as_text:
        logger.info("[claude-native] %d tool call(s) with no result right after them "
                    "left out; %d result(s) with no call right before them sent as text",
                    left_out, as_text)
    return turns


def _text_block(text):
    if not isinstance(text, str) or not text:
        raise Untranslatable('an empty text part')
    return {'type': 'text', 'text': text}


def _user_blocks(content):
    if isinstance(content, str):
        return [_text_block(content)]
    if not isinstance(content, list) or not content:
        raise Untranslatable('a user message with no content')
    blocks = []
    for part in content:
        kind = part.get('type') if isinstance(part, dict) else None
        if kind == 'text':
            blocks.append(_text_block(part.get('text')))
        elif kind == 'image_url':
            blocks.append(_image_block((part.get('image_url') or {}).get('url')))
        else:
            raise Untranslatable('a user content part of type %r' % (kind,))
    return blocks


def _image_block(url):
    if not isinstance(url, str):
        raise Untranslatable('an image part without a url')
    if url.startswith('data:'):
        head, _, data = url.partition(',')
        media_type, _, encoding = head[len('data:'):].partition(';')
        if encoding != 'base64' or not media_type or not data:
            raise Untranslatable('an image data url that is not base64')
        try:
            base64.b64decode(data, validate=True)
        except Exception:
            raise Untranslatable('an image data url that is not base64')
        return {'type': 'image', 'source': {'type': 'base64',
                                            'media_type': media_type, 'data': data}}
    if url.startswith('https://') or url.startswith('http://'):
        return {'type': 'image', 'source': {'type': 'url', 'url': url}}
    raise Untranslatable('an image url that is neither data: nor http(s)')


def _assistant_blocks(message):
    if message.get('function_call'):
        raise Untranslatable('a legacy function_call message')
    blocks = []
    content = message.get('content')
    if isinstance(content, str):
        if content:
            blocks.append({'type': 'text', 'text': content})
    elif isinstance(content, list):
        for part in content:
            kind = part.get('type') if isinstance(part, dict) else None
            if kind == 'text':
                if part.get('text'):
                    blocks.append(_text_block(part.get('text')))
            elif kind != 'refusal':           # the layer ignores refusal parts
                raise Untranslatable('an assistant content part of type %r' % (kind,))
    elif content is not None:
        raise Untranslatable('assistant content that is neither text nor parts')
    for call in message.get('tool_calls') or []:
        function = call.get('function') if isinstance(call, dict) else None
        if call.get('type', 'function') != 'function' or not isinstance(function, dict):
            raise Untranslatable('a tool call that is not a function call')
        try:
            arguments = json.loads(function.get('arguments') or '{}')
        except (TypeError, ValueError):
            raise Untranslatable('tool call arguments that are not JSON')
        if not isinstance(arguments, dict) or not call.get('id') or not function.get('name'):
            raise Untranslatable('a tool call without an id, a name or object arguments')
        blocks.append({'type': 'tool_use', 'id': call['id'],
                       'name': function['name'], 'input': arguments})
    if not blocks:
        raise Untranslatable('an assistant message with nothing in it')
    return blocks


def _tool_result(message):
    call_id = message.get('tool_call_id')
    if not isinstance(call_id, str) or not call_id:
        raise Untranslatable('a tool message without tool_call_id')
    content = message.get('content')
    if isinstance(content, list):
        texts = []
        for part in content:
            if not (isinstance(part, dict) and part.get('type') == 'text'):
                raise Untranslatable('a tool result part that is not text')
            texts.append(part.get('text') or '')
        content = ''.join(texts)
    elif content is None:
        content = ''
    elif not isinstance(content, str):
        raise Untranslatable('tool result content that is neither text nor parts')
    block = {'type': 'tool_result', 'tool_use_id': call_id}
    if content:
        block['content'] = content
    return block


def _tools(tools):
    if not tools:
        return []
    if not isinstance(tools, list):
        raise Untranslatable('tools is not a list')
    native = []
    for tool in tools:
        function = tool.get('function') if isinstance(tool, dict) else None
        if tool.get('type', 'function') != 'function' or not isinstance(function, dict):
            raise Untranslatable('a tool that is not a function')
        if not function.get('name'):
            raise Untranslatable('a function without a name')
        entry = {'name': function['name']}
        if function.get('description'):
            entry['description'] = function['description']
        entry['input_schema'] = function.get('parameters') or {
            'type': 'object', 'properties': {}}
        native.append(entry)
    return native


def _tool_choice(choice, parallel):
    if choice is None:
        native = {'type': 'auto'} if parallel is False else None
    elif choice == 'auto':
        native = {'type': 'auto'}
    elif choice == 'none':
        return {'type': 'none'}
    elif choice == 'required':
        native = {'type': 'any'}
    elif (isinstance(choice, dict) and choice.get('type') == 'function'
          and isinstance(choice.get('function'), dict) and choice['function'].get('name')):
        native = {'type': 'tool', 'name': choice['function']['name']}
    else:
        raise Untranslatable('tool_choice %r' % (choice,))
    if native is not None and parallel is False:
        native['disable_parallel_tool_use'] = True
    return native


def native_request(httpx_module, request, native_body):
    """The Messages API request for ``request`` (an httpx Request to the
    compatibility layer): same host, the same headers less the OpenAI
    credential (sent as ``x-api-key``), the same timeout extensions."""
    headers = [(k, v) for k, v in request.headers.multi_items()
               if k.lower() not in _REPLACED_REQUEST_HEADERS]
    auth = request.headers.get('authorization') or ''
    if auth.lower().startswith('bearer '):
        headers.append(('x-api-key', auth[len('bearer '):].strip()))
    headers.append(('anthropic-version', API_VERSION))
    headers.append(('content-type', 'application/json'))
    url = request.url.copy_with(path=messages_path(request.url.path))
    return httpx_module.Request(
        'POST', url, headers=headers,
        content=json.dumps(native_body, ensure_ascii=False).encode('utf-8'),
        extensions=dict(request.extensions or {}))


# ─── Response ────────────────────────────────────────────────────────────

def chat_completion(native, now=None) -> dict:
    """The chat completion for a Messages API reply.  Thinking blocks are
    dropped, as the compatibility layer drops them.  ``usage.prompt_tokens``
    counts every input token, cached or not, as OpenAI's does;
    ``cache_creation_input_tokens`` and ``cache_read_input_tokens`` say how
    many of them were written to and read from the cache, which the dollar
    meter prices differently.  Raises ValueError when ``native`` is not a
    message."""
    if not isinstance(native, dict) or native.get('type') != 'message':
        raise ValueError('not a message')
    texts, calls = [], []
    for block in native.get('content') or []:
        kind = block.get('type') if isinstance(block, dict) else None
        if kind == 'text':
            texts.append(block.get('text') or '')
        elif kind == 'tool_use':
            calls.append({'id': block.get('id'), 'type': 'function',
                          'function': {'name': block.get('name'),
                                       'arguments': json.dumps(block.get('input') or {},
                                                               ensure_ascii=False)}})
    text = ''.join(texts)
    message = {'role': 'assistant', 'content': text if (text or not calls) else None}
    if calls:
        message['tool_calls'] = calls
    usage = native.get('usage') or {}
    fresh = int(usage.get('input_tokens') or 0)
    written = int(usage.get('cache_creation_input_tokens') or 0)
    read = int(usage.get('cache_read_input_tokens') or 0)
    output = int(usage.get('output_tokens') or 0)
    prompt = fresh + written + read
    return {
        'id': native.get('id'),
        'object': 'chat.completion',
        'created': int(now if now is not None else time.time()),
        'model': native.get('model'),
        'choices': [{'index': 0, 'message': message, 'logprobs': None,
                     'finish_reason': _FINISH_REASONS.get(native.get('stop_reason'), 'stop')}],
        'usage': {'prompt_tokens': prompt, 'completion_tokens': output,
                  'total_tokens': prompt + output,
                  'prompt_tokens_details': {'cached_tokens': read},
                  'cache_creation_input_tokens': written,
                  'cache_read_input_tokens': read},
    }


def chat_error(native) -> dict:
    """The OpenAI error body for a Messages API error body: the same type and
    the same words."""
    error = native.get('error') if isinstance(native, dict) else None
    if isinstance(error, dict):
        kind = str(error.get('type') or 'api_error')
        words = str(error.get('message') or kind)
    else:
        kind, words = 'api_error', json.dumps(native, ensure_ascii=False)
    return {'error': {'type': kind, 'code': kind, 'message': words, 'param': None}}


def chat_response(httpx_module, request, native_response):
    """The chat/completions response for an already-read Messages API
    response, bound to the caller's own ``request``: same status, same
    headers (rate limits, ``retry-after``, ``x-should-retry``), the request id
    under ``x-request-id`` as the compatibility layer names it.  A 2xx body
    that is not a message comes back with no choices, which the wire hook
    answers as a failed call; a body that is not JSON comes back as it came."""
    status = native_response.status_code
    raw = native_response.content
    headers = [(k, v) for k, v in native_response.headers.multi_items()
               if k.lower() not in _BODY_HEADERS]
    request_id = native_response.headers.get('request-id')
    if request_id and 'x-request-id' not in native_response.headers:
        headers.append(('x-request-id', request_id))
    try:
        native = json.loads(raw)
    except ValueError:
        native = None
    if not isinstance(native, dict):
        return httpx_module.Response(status, headers=headers, content=raw, request=request)
    if 200 <= status < 300:
        try:
            shaped = chat_completion(native)
        except ValueError:
            shaped = {'error': native}
    else:
        shaped = chat_error(native)
    headers.append(('content-type', 'application/json'))
    return httpx_module.Response(
        status, headers=headers,
        content=json.dumps(shaped, ensure_ascii=False).encode('utf-8'),
        request=request)
