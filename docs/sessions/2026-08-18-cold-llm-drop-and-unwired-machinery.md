# Cold-LLM messages are dropped, and the machinery to fix it is unwired

2026-08-18. Recorded here because TaskCreate/TaskUpdate are disabled this
session; these are otherwise only in chat scrollback.

## The report

Steward's screenshot, live:

```
Starting the local AI engine for you now. Give it a few seconds and send your message again.
User: Hi!
Assistant: I couldn't process that request - Loading model
User: Hi!
Assistant: I couldn't process that request - Loading model
User: Yeah
Assistant: Hi there! Ready to help with whatever you need.
```

Their challenge about my commit `15ee516a`: *"is this a fix or hiding the root
cause to user?"*

**It was hiding it.** That commit changed the wording only. The message is
still discarded and the user is still told to retype it. The third turn
working is the proof the state was transient.

## What actually happens

llama-server answers **HTTP 200** with `{"error": {"message": "Loading model"}}`
while weights load. `routes/chatbot_routes.py:3480` (inside `_resolve_agent()`,
line 2733) calls `get_orchestrator().ensure_loaded_async('llm', ...)` and then
returns a stub asking the user to send the message again. The user's message
is thrown away.

## The machinery already exists — with zero consumers

`integrations/channels/queue/message_queue.py` — `MessageQueue` with
`QueuePolicy` (DROP/LATEST/BACKLOG/PRIORITY/COLLECT), `DropPolicy`,
`DedupeMode`, expiration, per-channel/user queues, stats, debounce. Tested by
`tests/unit/test_message_queue.py`.

`integrations/channels/queue/pipeline.py` — `MessagePipeline`, and the package
docstring advertises *"Retry: Handle transient failures with backoff"*.

**Consumers outside their own package: zero.** Verified by `git grep` for
`from integrations.channels.queue`, `MessageQueue(`, `MessagePipeline`,
`queue.pipeline`, excluding tests — only `message_queue.py:123/502` and
`pipeline.py:230/698`, i.e. self-references.

BACKLOG + dedupe is exactly this case: "Hi!" was sent twice and both were
dropped; dedupe collapses that to one queued message, answered once.

## The structured signal also exists and is also ignored

`chatbot_routes.py:3486-3489` already returns:

```python
'error': 'local_llm_starting', 'llm_starting': True,
'retry_hint_seconds': 6, 'success': False
```

The web client references **none** of them — grep across `landing-page/src`
(excluding tests) returns nothing. A transient-state contract implemented on
the server side only. Same shape as `ensure_wamp_running()` having no callers.

**Precedent the codebase already set:** `tests/unit/test_voice_setup_pending_message.py`
(#7) fixed this exact class of bug for voice and pinned both directions — no
models → setup-pending message **and** a `setup_pending` flag; models present
but call failed → original message unchanged. Its docstring is the argument
for doing it properly: *"A fresh offline box must not blame the user for its
own missing model."*

## The one missing link

`integrations/service_tools/model_orchestrator.py:198` — `ensure_loaded_async`
is fire-and-forget: returns `None`, spawns a daemon thread, no future, no
callback. Its docstring calls it *"THE single 'bring me a model that can do X'
entry point for every caller (chat fallback on cold LLM, TTS synth on cold
engine, VLM request on cold vision)"*. So there is no signal to drain a queue
on.

| Link | Status |
|---|---|
| enqueue on cold LLM instead of dropping | `MessageQueue` — exists, unwired |
| know when the model is ready | **missing** — callback, or drain polling `is_loaded()` |
| deliver the late answer | SSE — verified live (`/api/social/events/stream` 200 `text/event-stream`) |

## Design calls to settle before coding

1. **Readiness signal.** Add a completion callback to `ensure_loaded_async`
   (benefits every cold-start caller: TTS, VLM) versus keeping it
   fire-and-forget and polling `is_loaded()` in the drain. The callback is
   cleaner but edits a shared hot path.
2. **Queue scope.** Per-user BACKLOG + dedupe; and what happens if the model
   never loads — expire with the setup-pending copy, or hold.

## Related, also unfixed

- The error reply is returned as a normal assistant turn, so it enters
  conversation history. `llm_outbound.jsonl` shows the model being fed
  `Assistant: I couldn't process that request - Loading model` as its own
  prior turn. Deliberately excluded from `15ee516a`; needs its own change.
- **Not investigated:** why the model was unloaded while the user was typing —
  cold start, or VRAM eviction mid-session.

## Other unwired machinery found the same day

- `ensure_wamp_running()` (`wamp_router.py:801`) — zero callers. Its docstring
  and `main.py:5474` both claim the deferred router "wakes on-demand when a
  channel adapter registers or a mobile peer is discovered". Nothing calls it,
  so once deferred at boot the router cannot start for the process lifetime.
  Affects non-web channels and mobile peers only; the web SPA is fine because
  SSE is live and carries the same events.
- The draft-first standby (`speculative_dispatcher.py:225`,
  `"Let me check that for you…"`) persists into conversation history — 38
  occurrences inside prompt bodies in `llm_outbound.jsonl`.
- `whisperx` / `pyannote.audio` are declared in no manifest, so
  `DiarizationService` can never start; and even packaged it needs a per-user
  HF token, which both the service and server require and neither surfaces.
