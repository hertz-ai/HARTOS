# HevolveBot Integration System - Complete Usage Guide

A comprehensive multi-channel messaging integration system with 30 channel adapters, message queuing, automation, and more.

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Channel Adapters](#2-channel-adapters)
3. [Message Queue & Pipeline](#3-message-queue--pipeline)
4. [Command System](#4-command-system)
5. [Response Handling](#5-response-handling)
6. [Media Processing](#6-media-processing)
7. [Automation](#7-automation)
8. [Channel Bridge (WAMP)](#8-channel-bridge-wamp)
9. [Memory & Search](#9-memory--search)
10. [Identity & Preferences](#10-identity--preferences)
11. [Plugin System](#11-plugin-system)
12. [Gateway Protocol](#12-gateway-protocol)
13. [Admin Dashboard](#13-admin-dashboard)
14. [Docker Deployment](#14-docker-deployment)
15. [Complete Examples](#15-complete-examples)

---

## 1. Quick Start

### Minimal Setup (3 Lines)

```python
from integrations.channels.flask_integration import init_channels

channels = init_channels()
channels.register_telegram()  # Uses TELEGRAM_BOT_TOKEN env var
channels.start()
```

### With Flask App

```python
from flask import Flask
from integrations.channels.flask_integration import init_channels

app = Flask(__name__)

# Initialize with your Flask app
channels = init_channels(app, config={
    "agent_api_url": "http://localhost:6777/chat",
    "default_user_id": 10077,
    "default_prompt_id": 8888,
})

# Register channels (tokens from env vars)
channels.register_telegram()
channels.register_discord()

# Start receiving messages
channels.start()

if __name__ == "__main__":
    app.run(port=6777)
```

### Environment Variables

```bash
# Channel Tokens
export TELEGRAM_BOT_TOKEN="your-telegram-token"
export DISCORD_BOT_TOKEN="your-discord-token"
export SLACK_BOT_TOKEN="xoxb-your-slack-token"

# WAMP Bridge (optional)
export CBURL="ws://localhost:8088/ws"
export CBREALM="realm1"
```

---

## 2. Channel Adapters

### Available Adapters (30 Total)

| Category | Adapters |
|----------|----------|
| **Core** | `telegram`, `discord`, `slack`, `whatsapp`, `web` |
| **Extended** | `signal`, `imessage`, `google_chat` |
| **Enterprise** | `matrix`, `teams`, `mattermost`, `nextcloud`, `rocketchat` |
| **Social** | `twitch`, `messenger`, `instagram`, `twitter` |
| **Regional** | `line`, `wechat`, `viber`, `zalo` |
| **Specialized** | `nostr`, `bluebubbles`, `voice`, `email`, `tlon`, `openprose` |
| **User Accounts** | `telegram_user`, `discord_user`, `zalo_user` |

### Using Core Adapters

```python
from integrations.channels.telegram_adapter import create_telegram_adapter
from integrations.channels.discord_adapter import create_discord_adapter
from integrations.channels.slack_adapter import create_slack_adapter

# Telegram
telegram = create_telegram_adapter(token="BOT_TOKEN")

# Discord
discord = create_discord_adapter(token="BOT_TOKEN", command_prefix="!")

# Slack
slack = create_slack_adapter(
    bot_token="xoxb-...",
    app_token="xapp-...",  # For Socket Mode
)
```

### Using Extension Adapters

```python
from integrations.channels.extensions import (
    # Enterprise
    create_matrix_adapter,
    create_teams_adapter,
    create_mattermost_adapter,

    # Social
    create_twitch_adapter,
    create_messenger_adapter,

    # Regional
    create_line_adapter,
    create_wechat_adapter,

    # User Accounts (access groups bots can't)
    create_telegram_user_adapter,
    create_discord_user_adapter,
)

# Matrix with E2E encryption
matrix = create_matrix_adapter(
    homeserver_url="https://matrix.org",
    user_id="@bot:matrix.org",
    access_token="...",
    enable_encryption=True,
)

# Microsoft Teams
teams = create_teams_adapter(
    app_id="...",
    app_password="...",
)

# Telegram User Account (not bot)
tg_user = create_telegram_user_adapter(
    api_id=12345,
    api_hash="...",
    session_string="...",  # From first login
)
```

### Handling Messages

```python
from integrations.channels import Message

adapter = create_telegram_adapter(token="...")

# Register message handler
@adapter.on_message
async def handle(message: Message):
    print(f"From: {message.sender_name}")
    print(f"Text: {message.text}")
    print(f"Chat: {message.chat_id}")
    print(f"Is Group: {message.is_group}")

    # Reply
    await adapter.send_message(
        chat_id=message.chat_id,
        text=f"You said: {message.text}",
        reply_to=message.id,
    )

# Start the adapter
await adapter.start()
```

### Sending Media

```python
from integrations.channels.base import MediaAttachment, MessageType

# Send image
await adapter.send_message(
    chat_id="123456",
    text="Check this out!",
    media=[MediaAttachment(
        type=MessageType.IMAGE,
        url="https://example.com/image.jpg",
        caption="A cool image",
    )],
)

# Send document
await adapter.send_message(
    chat_id="123456",
    text="Here's the file",
    media=[MediaAttachment(
        type=MessageType.DOCUMENT,
        file_path="/app/data/report.pdf",
        file_name="report.pdf",
    )],
)
```

### Channel Registry

```python
from integrations.channels import ChannelRegistry, get_registry

# Get global registry
registry = get_registry()

# Register multiple adapters
registry.register(telegram_adapter)
registry.register(discord_adapter)
registry.register(slack_adapter)

# Set agent handler (receives all messages)
registry.set_agent_handler(my_agent_function)

# Start all channels
await registry.start_all()

# Send to specific channel
await registry.send_to_channel(
    channel="telegram",
    chat_id="123456",
    text="Hello!",
)

# Broadcast to multiple channels
await registry.broadcast(
    text="Important announcement!",
    channels=["telegram", "discord"],
    chat_ids={
        "telegram": "-1001234567890",
        "discord": "123456789012345678",
    }
)

# Check status
status = registry.get_status()
# {'telegram': 'connected', 'discord': 'connected', ...}
```

---

## 3. Message Queue & Pipeline

### Rate Limiting

```python
from integrations.channels.queue import RateLimiter, RateLimitConfig

# Configure rate limits
limiter = RateLimiter(RateLimitConfig(
    requests_per_minute=60,    # 60 requests per minute
    requests_per_hour=1000,    # 1000 per hour
    burst_limit=10,            # Max 10 in quick succession
    burst_window_seconds=1,    # Burst window of 1 second
))

async def handle_message(message):
    # Check rate limit (sync check)
    result = limiter.check(message.channel, message.chat_id)

    if not result.allowed:
        await adapter.send_message(
            message.chat_id,
            f"Slow down! Try again in {result.retry_after_seconds}s"
        )
        return

    # Consume the slot
    limiter.consume(message.channel, message.chat_id)
    # Process message...
```

### Retry with Backoff

```python
from integrations.channels.queue import RetryHandler, RetryConfig

retry = RetryHandler(RetryConfig(
    max_retries=3,
    initial_delay_ms=1000,    # Start with 1 second (in milliseconds)
    max_delay_ms=30000,       # Cap at 30 seconds
    exponential_base=2.0,     # Double each time
    jitter=True,              # Add randomization to prevent thundering herd
))

# Wrap flaky operations (async)
result = await retry.with_retry_async(
    call_external_api,
    data,
    on_retry=lambda attempt: print(f"Retry {attempt.attempt}: {attempt.error}")
)

# Or synchronous version
result = retry.with_retry(
    sync_function,
    arg1, arg2
)
```

### Debouncing (Collect Rapid Messages)

```python
from integrations.channels.queue import InboundDebouncer, DebounceConfig

# Collect messages within 500ms window
debouncer = InboundDebouncer(DebounceConfig(
    window_ms=500,
    max_messages=10,
))

@adapter.on_message
async def handle(message):
    # Returns None until window closes, then returns all collected
    batch = await debouncer.add(message)

    if batch:
        # Process all messages at once
        combined = "\n".join(m.text for m in batch)
        response = await get_agent_response(combined)
        await adapter.send_message(message.chat_id, response)
```

### Deduplication

```python
from integrations.channels.queue import MessageDeduplicator, DedupeConfig

deduper = MessageDeduplicator(DedupeConfig(
    ttl_seconds=300,  # Remember for 5 minutes
))

@adapter.on_message
async def handle(message):
    if deduper.is_duplicate(message.id):
        return  # Skip duplicate

    deduper.mark_seen(message.id)
    # Process...
```

### Concurrency Control

```python
from integrations.channels.queue import ConcurrencyController, ConcurrencyLimits

controller = ConcurrencyController(ConcurrencyLimits(
    max_global=100,        # 100 total concurrent
    max_per_channel=20,    # 20 per channel
    max_per_chat=2,        # 2 per chat
    max_per_user=4,        # 4 per user
    timeout_seconds=300,   # Auto-release after 5 minutes
))

@adapter.on_message
async def handle(message):
    # Acquire a slot (returns slot_id or None)
    slot_id = await controller.acquire(
        channel=message.channel,
        chat_id=message.chat_id,
        user_id=message.sender_id,
        wait=True,         # Wait if at limit
        timeout=30.0,      # Max wait time
    )

    if slot_id:
        try:
            # Process with slot held
            await process_message(message)
        finally:
            # Always release
            controller.release(slot_id=slot_id)
    else:
        await adapter.send_message(message.chat_id, "Too busy, try later")
```

### Full Pipeline

```python
from integrations.channels.queue import (
    MessageQueue, QueueConfig, QueuePolicy,
    RateLimiter, RateLimitConfig,
    RetryHandler, RetryConfig,
    ConcurrencyController, ConcurrencyLimits,
)

# Build pipeline components
queue = MessageQueue(QueueConfig(
    max_size=1000,
    policy=QueuePolicy.BACKLOG,
))

rate_limiter = RateLimiter(RateLimitConfig(requests_per_minute=60))
concurrency = ConcurrencyController(ConcurrencyLimits(max_global=100))
retry = RetryHandler(RetryConfig(max_retries=3))

async def pipeline(message):
    # 1. Queue
    await queue.put(message)

    # 2. Rate limit
    result = rate_limiter.check(message.channel, message.chat_id)
    if not result.allowed:
        return

    rate_limiter.consume(message.channel, message.chat_id)

    # 3. Concurrency
    slot_id = await concurrency.acquire(
        message.channel, message.chat_id, message.sender_id
    )
    if not slot_id:
        return

    try:
        # 4. Retry with backoff
        result = await retry.with_retry_async(
            process_with_agent, message
        )
    finally:
        concurrency.release(slot_id=slot_id)

    return result
```

---

## 4. Command System

### Registering Commands

```python
from integrations.channels.commands import (
    CommandRegistry,
    CommandDefinition,
    CommandScope,
    CommandCategory,
    get_command_registry,
)

registry = get_command_registry()

# Simple command
registry.register(CommandDefinition(
    key="ping",                      # Unique identifier
    description="Check if bot is alive",
    handler=lambda ctx: "Pong!",
    aliases=["/ping", "/p"],         # Text aliases (auto-adds /key if empty)
))

# Command with arguments
registry.register(CommandDefinition(
    key="remind",
    description="Set a reminder",
    handler=remind_handler,
    aliases=["/remind", "/r"],
    accepts_args=True,               # Command accepts arguments
    scope=CommandScope.BOTH,         # Available in text and native forms
    category=CommandCategory.TOOLS,  # For help grouping
))
```

### Command Detection

```python
from integrations.channels.commands import (
    CommandDetector,
    get_command_detector,
)

detector = get_command_detector()

@adapter.on_message
async def handle(message):
    # Check if message is a command
    detected = detector.detect(message.text)

    if detected:
        print(f"Command: {detected.name}")
        print(f"Args: {detected.args}")

        # Execute command
        result = await registry.execute(
            detected.name,
            args=detected.args,
            message=message,
        )
        await adapter.send_message(message.chat_id, result)
    else:
        # Regular message, send to agent
        response = await get_agent_response(message.text)
        await adapter.send_message(message.chat_id, response)
```

### Argument Parsing

```python
from integrations.channels.commands import (
    ArgumentParser,
    ArgumentDefinition,
    ArgumentType,
)

parser = ArgumentParser()

# Define arguments
parser.add_argument(ArgumentDefinition(
    name="time",
    type=ArgumentType.STRING,
    required=True,
    description="When to remind (e.g., '10m', '1h')",
))

parser.add_argument(ArgumentDefinition(
    name="message",
    type=ArgumentType.STRING,
    required=True,
    description="Reminder message",
))

# Parse command
result = parser.parse("/remind 10m Take a break")
# result.args = {"time": "10m", "message": "Take a break"}
```

### Mention Gating

```python
from integrations.channels.commands import (
    MentionGate,
    MentionMode,
    get_mention_gate,
)

gate = get_mention_gate()

# Configure per channel
gate.configure("telegram", MentionMode.REQUIRED_IN_GROUPS)
gate.configure("discord", MentionMode.OPTIONAL)

@adapter.on_message
async def handle(message):
    # Check if bot should respond
    result = gate.check(
        channel=message.channel,
        text=message.text,
        is_group=message.is_group,
        is_mentioned=message.is_bot_mentioned,
    )

    if not result.should_respond:
        return  # Ignore message

    # Process message...
```

### Built-in Commands

```python
from integrations.channels.commands import (
    get_builtin_commands,
    register_builtin_commands,
)

# Register all built-in commands
register_builtin_commands()

# Available commands:
# /help - Show available commands
# /start - Welcome message
# /status - Bot status
# /ping - Latency check
# /version - Bot version
# /settings - User settings
# /language - Set language
# /timezone - Set timezone
# /clear - Clear conversation
# /export - Export chat history
# /feedback - Send feedback
# /report - Report issue
# /subscribe - Subscribe to updates
# /unsubscribe - Unsubscribe
# /admin - Admin commands (restricted)
# ... and more
```

---

## 5. Response Handling

### Typing Indicators

```python
from integrations.channels.response import TypingManager, TypingConfig

typing = TypingManager(TypingConfig(
    interval_seconds=3,  # Re-send every 3s for long operations
))

@adapter.on_message
async def handle(message):
    # Start typing (auto-refreshes)
    async with typing.typing(adapter, message.chat_id):
        # Long operation...
        response = await slow_ai_call(message.text)

    await adapter.send_message(message.chat_id, response)
```

### Acknowledgment Reactions

```python
from integrations.channels.response import AckManager, AckConfig

ack = AckManager(AckConfig(
    thinking_emoji="🤔",
    success_emoji="✅",
    error_emoji="❌",
))

@adapter.on_message
async def handle(message):
    # React with thinking emoji
    await ack.acknowledge(adapter, message.chat_id, message.id)

    try:
        response = await get_response(message.text)
        await ack.success(adapter, message.chat_id, message.id)
    except Exception:
        await ack.error(adapter, message.chat_id, message.id)
```

### Response Templates

```python
from integrations.channels.response import TemplateEngine, TemplateConfig, TemplateContext

templates = TemplateEngine(TemplateConfig())

# Register named templates (use {var} syntax, not {{var}})
templates.register_template("welcome", """
Hello {user.name}! 👋

I'm {identity.name}, your AI assistant.
How can I help you today?
""")

templates.register_template("error", """
Sorry {user.name}, something went wrong.
Error: {error_message}

Please try again or type /help for assistance.
""")

# Set context for variable substitution
templates.set_model("gpt-4")
templates.set_channel("telegram")
templates.set_variable("error_message", "Connection timeout")

# Render template with context
response = templates.render_named("welcome", extra_vars={
    "user.name": message.sender_name,
    "identity.name": "HevolveBot",
})

# Or create context explicitly
ctx = templates.create_context(
    model="gpt-4",
    identity_name="HevolveBot",
    user_name=message.sender_name,
    channel="telegram",
)
response = templates.render("{identity.name} says hello, {user.name}!", context=ctx)
```

### Streaming Responses

```python
from integrations.channels.response import (
    StreamingResponse,
    create_streaming_response,
)

@adapter.on_message
async def handle(message):
    # Create streaming response
    stream = create_streaming_response(
        adapter=adapter,
        chat_id=message.chat_id,
    )

    # Start with initial message
    await stream.start("Thinking...")

    # Stream chunks
    async for chunk in get_ai_stream(message.text):
        await stream.update(chunk)

    # Finalize
    await stream.finish()
```

---

## 6. Media Processing

### Image Analysis (Vision)

```python
from integrations.channels.media import VisionProcessor, VisionProvider

vision = VisionProcessor(provider=VisionProvider.OPENAI)

@adapter.on_message
async def handle(message):
    if message.has_media:
        for media in message.media:
            if media.type == MessageType.IMAGE:
                # Analyze image
                analysis = await vision.analyze(
                    image_url=media.url,
                    prompt="Describe this image",
                )

                await adapter.send_message(
                    message.chat_id,
                    f"I see: {analysis.description}"
                )
```

### Audio Transcription (Speech-to-Text)

```python
from integrations.channels.media import AudioProcessor, AudioProvider

audio = AudioProcessor(provider=AudioProvider.WHISPER)

@adapter.on_message
async def handle(message):
    if message.has_media:
        for media in message.media:
            if media.type in (MessageType.AUDIO, MessageType.VOICE):
                # Transcribe audio
                result = await audio.transcribe(
                    audio_url=media.url,
                    language="auto",  # Auto-detect
                )

                await adapter.send_message(
                    message.chat_id,
                    f"You said: {result.text}"
                )
```

### Text-to-Speech

```python
from integrations.channels.media import TTSEngine, TTSProvider, AudioFormat

tts = TTSEngine(provider=TTSProvider.ELEVENLABS)

# Get available voices
voices = await tts.list_voices()

# Generate speech
result = await tts.synthesize(
    text="Hello! How can I help you today?",
    voice_id="rachel",
    format=AudioFormat.MP3,
)

# Send as voice message
await adapter.send_message(
    chat_id=message.chat_id,
    media=[MediaAttachment(
        type=MessageType.VOICE,
        file_path=result.file_path,
    )],
)
```

### Image Generation

```python
from integrations.channels.media import (
    ImageGenerator,
    ImageProvider,
    ImageSize,
    ImageStyle,
)

generator = ImageGenerator(provider=ImageProvider.DALLE)

# Generate image
result = await generator.generate(
    prompt="A futuristic city at sunset",
    size=ImageSize.LARGE,
    style=ImageStyle.VIVID,
)

await adapter.send_message(
    chat_id=message.chat_id,
    text="Here's your image!",
    media=[MediaAttachment(
        type=MessageType.IMAGE,
        url=result.url,
    )],
)
```

### Link Processing

```python
from integrations.channels.media import LinkProcessor

links = LinkProcessor()

@adapter.on_message
async def handle(message):
    # Detect links in message
    detected = links.detect(message.text)

    for link in detected:
        # Fetch and summarize
        preview = await links.get_preview(link.url)

        await adapter.send_message(
            message.chat_id,
            f"📎 {preview.title}\n{preview.description}"
        )
```

### File Management

```python
from integrations.channels.media import FileManager, StorageBackend

files = FileManager(
    backend=StorageBackend.LOCAL,
    base_path="/app/data/files",
)

# Download from channel
result = await files.download(
    url=message.media[0].url,
    filename="document.pdf",
)

# Upload to channel
await files.upload(
    adapter=adapter,
    chat_id=message.chat_id,
    file_path="/app/data/report.pdf",
)
```

---

## 7. Automation

### Webhooks

```python
from integrations.channels.automation import WebhookManager, WebhookConfig

webhooks = WebhookManager()

# Register webhook endpoint
webhooks.register(WebhookConfig(
    id="github",
    secret="webhook_secret",
    events=["push", "pull_request"],
))

# Flask route for webhook
@app.route("/webhooks/github", methods=["POST"])
async def github_webhook():
    event = request.headers.get("X-GitHub-Event")
    payload = request.json

    # Verify signature
    if not webhooks.verify("github", request):
        return "Invalid", 401

    # Forward to channels
    await registry.broadcast(
        text=f"GitHub: {event} - {payload.get('action')}",
        channels=["discord", "slack"],
        chat_ids={"discord": "...", "slack": "..."},
    )

    return "OK"
```

### Scheduled Jobs (Cron)

```python
from integrations.channels.automation import (
    CronManager,
    CronJob,
    IntervalUnit,
)

cron = CronManager()

# Daily standup reminder at 9 AM
cron.add_job(CronJob(
    id="standup",
    cron_expression="0 9 * * 1-5",  # Weekdays at 9 AM
    handler=lambda: registry.send_to_channel(
        "slack", "C123", "Time for standup! 🧍"
    ),
))

# Every 30 minutes
cron.add_job(CronJob(
    id="health-check",
    interval=30,
    interval_unit=IntervalUnit.MINUTES,
    handler=check_system_health,
))

# Start scheduler
await cron.start()
```

### Scheduled Messages

```python
from integrations.channels.automation import (
    ScheduledMessageManager,
    ScheduledMessage,
    RecurrenceType,
)

scheduler = ScheduledMessageManager(registry)

# One-time message
scheduler.schedule(ScheduledMessage(
    id="meeting-reminder",
    channel="telegram",
    chat_id="123456",
    text="Meeting starts in 10 minutes!",
    send_at=datetime.now() + timedelta(minutes=50),
))

# Weekly message
scheduler.schedule(ScheduledMessage(
    id="weekly-summary",
    channel="slack",
    chat_id="C123",
    text="Weekly summary time!",
    recurrence=RecurrenceType.WEEKLY,
    day_of_week=5,  # Friday
    hour=17,
))

await scheduler.start()
```

### Event Triggers

```python
from integrations.channels.automation import (
    TriggerManager,
    TriggerType,
    TriggerCondition,
    TriggerPriority,
)

triggers = TriggerManager()

# Register keyword trigger
triggers.register(
    trigger_type=TriggerType.KEYWORD,
    callback=lambda data: notify_admins(data),
    name="urgent-alert",
    keywords=["URGENT", "CRITICAL", "EMERGENCY"],  # Required for KEYWORD type
    priority=TriggerPriority.HIGH,
    cooldown_seconds=60,    # Don't trigger more than once per minute
)

# Register regex trigger
triggers.register(
    trigger_type=TriggerType.REGEX,
    callback=handle_order_number,
    name="order-lookup",
    pattern=r"order\s*#?\s*(\d{6,})",  # Required for REGEX type
)

# Register with conditions
triggers.register(
    trigger_type=TriggerType.MESSAGE_RECEIVED,
    callback=send_welcome_message,
    name="welcome-new-user",
    conditions=[
        TriggerCondition(field="is_new_user", operator="eq", value=True),
    ],
    channel_filter=["telegram", "discord"],  # Only these channels
    max_triggers=1,  # Only trigger once per user
)

# Evaluate triggers for a message
@adapter.on_message
async def handle(message):
    # Convenience method for messages
    results = triggers.evaluate_message(
        message=message.text,
        channel_id=message.chat_id,
        user_id=message.sender_id,
    )
    for result in results:
        if result.triggered:
            print(f"Trigger {result.trigger_name} fired")
```

### Workflows

```python
from integrations.channels.automation import (
    WorkflowEngine,
    Workflow,
    WorkflowStep,
    StepType,
)

engine = WorkflowEngine()

# Define onboarding workflow
onboarding = Workflow(
    id="onboarding",
    name="User Onboarding",
    steps=[
        WorkflowStep(
            id="welcome",
            type=StepType.MESSAGE,
            config={"text": "Welcome! Let's get you set up."},
        ),
        WorkflowStep(
            id="ask-name",
            type=StepType.INPUT,
            config={
                "prompt": "What should I call you?",
                "variable": "name",
            },
        ),
        WorkflowStep(
            id="ask-role",
            type=StepType.CHOICE,
            config={
                "prompt": "What's your role?",
                "options": ["Developer", "Designer", "Manager", "Other"],
                "variable": "role",
            },
        ),
        WorkflowStep(
            id="complete",
            type=StepType.MESSAGE,
            config={"text": "All set, {{name}}! You're registered as {{role}}."},
        ),
    ],
)

engine.register(onboarding)

# Start workflow for user
await engine.start(
    workflow_id="onboarding",
    user_id=message.sender_id,
    channel=message.channel,
    chat_id=message.chat_id,
)
```

---

## 8. Channel Bridge (WAMP)

Cross-channel message routing using your existing Crossbar infrastructure.

### Basic Setup

```python
from integrations.channels.bridge import (
    ChannelBridge,
    BridgeConfig,
    BridgeRule,
    RouteType,
    create_channel_bridge,
)

# Create bridge
bridge = create_channel_bridge(
    registry=registry,
    crossbar_url="ws://localhost:8088/ws",
    realm="realm1",
)

# Connect to Crossbar
await bridge.connect()
```

### Forwarding Rules

```python
# Forward Telegram messages to Discord
bridge.add_rule(BridgeRule(
    id="tg-to-discord",
    name="Telegram to Discord",
    source_channel="telegram",
    source_chat_id="-1001234567890",  # Specific group
    target_channel="discord",
    target_chat_id="123456789012345678",
    route_type=RouteType.FORWARD,
    include_source_info=True,  # Add [From: telegram/user] header
))

# Mirror to multiple channels
bridge.add_rule(BridgeRule(
    id="announcements",
    name="Mirror Announcements",
    source_channel="slack",
    source_chat_id="C-announcements",
    route_type=RouteType.BROADCAST,  # Send to all registered channels
))

# Filter-based routing
bridge.add_rule(BridgeRule(
    id="urgent-only",
    name="Forward Urgent Messages",
    source_channel="email",
    target_channel="telegram",
    target_chat_id="admin-group-id",
    route_type=RouteType.FILTER,
    filter_keywords=["urgent", "critical", "emergency"],
))
```

### Rate Limiting & Loop Prevention

```python
bridge.add_rule(BridgeRule(
    id="rate-limited",
    name="Rate Limited Forward",
    source_channel="twitch",
    target_channel="discord",
    target_chat_id="...",
    rate_limit=10,        # Max 10 forwards per minute
    cooldown_seconds=5,   # 5s between forwards
))

# Bridge automatically prevents infinite loops
# max_forward_chain=3 by default
```

### WAMP Integration

```python
# Publish channel events to WAMP
@adapter.on_message
async def handle(message):
    await bridge.publish_to_wamp(message.channel, message)

# Subscribe to bridge events in other services
# Topic: com.hertzai.hevolve.channel.message
# Topic: com.hertzai.hevolve.bridge.forwarded
```

---

## 9. Memory & Search

### Conversation Memory

```python
from integrations.channels.memory import MemoryStore, MemoryItem

memory = MemoryStore(db_path="/app/data/memory.db")

# Store conversation
await memory.add(MemoryItem(
    user_id=message.sender_id,
    channel=message.channel,
    role="user",
    content=message.text,
    metadata={"chat_id": message.chat_id},
))

await memory.add(MemoryItem(
    user_id=message.sender_id,
    channel=message.channel,
    role="assistant",
    content=response,
))

# Retrieve context
history = await memory.get_context(
    user_id=message.sender_id,
    limit=20,
)
```

### Semantic Search with Embeddings

```python
from integrations.channels.memory import (
    MemorySearch,
    EmbeddingCache,
    SearchConfig,
)

# Cache embeddings
cache = EmbeddingCache(cache_dir="/app/data/embeddings")

# Search memory
search = MemorySearch(
    memory_store=memory,
    embedding_cache=cache,
)

# Find relevant context
results = await search.search(
    query="What did we discuss about the project?",
    user_id=message.sender_id,
    limit=5,
)

# Use as context for AI
context = "\n".join(r.content for r in results)
```

### File Tracking

```python
from integrations.channels.memory import FileTracker, WatchConfig

tracker = FileTracker(db_path="/app/data/files.db")

# Watch directory for changes
tracker.watch(WatchConfig(
    path="/app/data/documents",
    patterns=["*.pdf", "*.docx"],
    recursive=True,
))

# Sync changes
changes = await tracker.sync()
for change in changes:
    print(f"{change.type}: {change.path}")

# Search files
files = await tracker.search("quarterly report")
```

---

## 10. Identity & Preferences

### Agent Identity

```python
from integrations.channels.identity import (
    AgentIdentity,
    IdentityManager,
)

identity_manager = IdentityManager()

# Define agent identity
support_bot = AgentIdentity(
    name="HevolveAI Support",           # Display name
    description="I help with technical support",
    avatar_url="/avatars/support.png",
    emoji="🤖",
    personality={                        # Dict, not string
        "tone": "friendly",
        "formality": "professional",
        "verbosity": "concise",
    },
    capabilities=["support", "troubleshooting", "faq"],
)

# Register the identity
identity_manager.set_identity(support_bot)

# Set per-channel identity
identity_manager.set_identity_for_channel("telegram", support_bot)

# Get identity for channel
identity = identity_manager.get_identity_for_channel("telegram")
response = f"{ai_response}\n\n— {identity.name} {identity.emoji}"

# Update identity
identity.update(description="Updated description")
identity.add_capability("live_chat")
```

### Avatar Management

```python
from integrations.channels.identity import (
    AvatarManager,
    Avatar,
    AvatarType,
)

avatars = AvatarManager(storage_path="/app/data/avatars")

# Upload avatar
avatar = await avatars.upload(
    file_path="/tmp/avatar.png",
    name="default",
    type=AvatarType.IMAGE,
)

# Set channel-specific avatar
await avatars.set_for_channel(
    adapter=telegram,
    avatar_id=avatar.id,
)
```

### User Preferences

```python
from integrations.channels.identity import (
    PreferenceManager,
    UserPreferences,
    ResponseStyle,
    Theme,
    get_preference_manager,
)

prefs = get_preference_manager()

# Get user preferences
user_prefs = await prefs.get(message.sender_id)

# Update preferences
user_prefs.language = "es"
user_prefs.timezone = "America/New_York"
user_prefs.response_style = ResponseStyle.CONCISE
user_prefs.theme = Theme.DARK

await prefs.save(user_prefs)

# Use in responses
if user_prefs.response_style == ResponseStyle.CONCISE:
    response = get_short_response()
else:
    response = get_detailed_response()
```

### Sender Identity Mapping

```python
from integrations.channels.identity import SenderIdentityMapper

mapper = SenderIdentityMapper()

# Map channel user to internal user
mapper.map(
    channel="telegram",
    channel_user_id="123456",
    internal_user_id=10077,
    prompt_id=8888,
)

# Get mapping
internal = mapper.get_internal(
    channel="telegram",
    channel_user_id="123456",
)
# internal.user_id = 10077
# internal.prompt_id = 8888
```

---

## 11. Plugin System

### Creating a Plugin

```python
from integrations.channels.plugins import (
    Plugin,
    PluginMetadata,
    Request,
    Response,
)

class WeatherPlugin(Plugin):
    metadata = PluginMetadata(
        name="weather",
        version="1.0.0",
        description="Get weather information",
        author="HevolveAI",
    )

    async def on_load(self):
        """Called when plugin is loaded."""
        self.api_key = os.getenv("WEATHER_API_KEY")

    async def on_message(self, message):
        """Handle messages."""
        if "weather" in message.text.lower():
            weather = await self.get_weather("New York")
            return f"Current weather: {weather}"

    def register_routes(self):
        """Register HTTP endpoints."""
        return [
            ("/weather/<city>", self.weather_endpoint, ["GET"]),
        ]

    async def weather_endpoint(self, request: Request) -> Response:
        city = request.path_params["city"]
        weather = await self.get_weather(city)
        return Response(json={"city": city, "weather": weather})
```

### Plugin Manager

```python
from integrations.channels.plugins import PluginManager

manager = PluginManager(plugin_dir="/app/plugins")

# Load all plugins
await manager.load_all()

# Load specific plugin
await manager.load("weather")

# Enable/disable
await manager.enable("weather")
await manager.disable("weather")

# List plugins
for plugin in manager.list():
    print(f"{plugin.name} v{plugin.version} - {plugin.state}")
```

### Plugin Registry

```python
from integrations.channels.plugins import PluginRegistry

registry = PluginRegistry()

# Search available plugins
plugins = await registry.search("weather")

# Install from registry
await registry.install("weather-plugin", version="1.0.0")

# Update plugin
await registry.update("weather-plugin")
```

---

## 12. Gateway Protocol

JSON-RPC 2.0 based inter-service communication.

### Gateway Server

```python
from integrations.channels.gateway import (
    GatewayProtocol,
    GatewayConfig,
    get_gateway,
)

gateway = get_gateway()

# Register methods
@gateway.method("chat.send")
async def send_chat(channel: str, chat_id: str, text: str):
    return await registry.send_to_channel(channel, chat_id, text)

@gateway.method("channel.status")
async def get_status():
    return registry.get_status()

# Start gateway
await gateway.start(host="0.0.0.0", port=8080)
```

### Gateway Client

```python
import aiohttp

async def call_gateway(method: str, params: dict):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://gateway:8080/rpc",
            json={
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": 1,
            }
        ) as resp:
            result = await resp.json()
            return result.get("result")

# Call from another service
await call_gateway("chat.send", {
    "channel": "telegram",
    "chat_id": "123456",
    "text": "Hello from another service!",
})
```

---

## 13. Admin Dashboard

### REST API Endpoints

```python
from integrations.channels.admin import create_admin_blueprint

# Add to Flask app
admin_bp = create_admin_blueprint(registry)
app.register_blueprint(admin_bp, url_prefix="/admin")

# Available endpoints:
# GET  /admin/channels          - List channels
# GET  /admin/channels/:name    - Get channel details
# POST /admin/channels/:name/send - Send message
# GET  /admin/stats             - Get statistics
# GET  /admin/queue             - Queue status
# POST /admin/commands          - Register command
# GET  /admin/webhooks          - List webhooks
# POST /admin/webhooks          - Create webhook
# GET  /admin/workflows         - List workflows
# POST /admin/workflows         - Create workflow
```

### TypeScript Dashboard

The admin dashboard is at `hevolvebot-admin-dashboard/`:

```bash
cd hevolvebot-admin-dashboard
npm install
npm run dev
```

Features:
- Real-time channel status
- Message queue visualization
- Command management
- Webhook configuration
- Workflow builder
- Live event feed
- Metrics dashboard

---

## 14. Docker Deployment

### docker-compose.yml

```yaml
version: '3.8'

services:
  hevolvebot:
    build: .
    ports:
      - "6777:6777"
    environment:
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
      - CBURL=ws://crossbar:8088/ws
      - CBREALM=realm1
      - DATABASE_URL=postgresql://postgres:password@db:5432/hevolve
    volumes:
      - ./data:/app/data
    depends_on:
      - crossbar
      - db
      - redis

  crossbar:
    image: crossbario/crossbar:latest
    ports:
      - "8088:8088"
    volumes:
      - ./crossbar:/node/.crossbar

  db:
    image: postgres:15
    environment:
      - POSTGRES_PASSWORD=password
      - POSTGRES_DB=hevolve
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:alpine
    ports:
      - "6379:6379"

  dashboard:
    build: ./hevolvebot-admin-dashboard
    ports:
      - "3000:3000"
    depends_on:
      - hevolvebot

volumes:
  postgres_data:
```

### Dockerfile

```dockerfile
FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directories
RUN mkdir -p /app/data /app/data/files /app/data/memory

ENV PYTHONUNBUFFERED=1

EXPOSE 6777

CMD ["python", "hart_intelligence_entry.py"]
```

---

## 15. Complete Examples

### Full Bot Setup

```python
"""Complete HevolveBot setup with all features."""

import asyncio
from flask import Flask
from integrations.channels.flask_integration import init_channels
from integrations.channels import get_registry
from integrations.channels.commands import register_builtin_commands, get_command_registry
from integrations.channels.queue import RateLimiter, RateLimitConfig
from integrations.channels.response import TypingManager
from integrations.channels.bridge import create_channel_bridge
from integrations.channels.automation import CronManager, CronJob, TriggerManager
from integrations.channels.memory import MemoryStore

app = Flask(__name__)

# Initialize channels
channels = init_channels(app)
channels.register_telegram()
channels.register_discord()

# Get registry
registry = get_registry()

# Setup components
rate_limiter = RateLimiter(RateLimitConfig(requests_per_minute=60))
typing = TypingManager()
memory = MemoryStore(db_path="/app/data/memory.db")
commands = get_command_registry()
register_builtin_commands()

# Setup bridge
bridge = create_channel_bridge(registry)

# Setup automation
cron = CronManager()
triggers = TriggerManager()

# Custom message handler
async def handle_message(message):
    # Rate limit
    result = rate_limiter.check_and_consume(message.channel, message.chat_id)
    if not result.allowed:
        return f"Please slow down! Try again in {result.retry_after_seconds}s"

    # Check for commands
    if message.text.startswith("/"):
        return await commands.execute_from_message(message)

    # Store in memory
    await memory.add_message(message)

    # Get AI response
    adapter = registry.get(message.channel)
    async with typing.typing(adapter, message.chat_id):
        response = await get_ai_response(
            message.text,
            context=await memory.get_context(message.sender_id),
        )

    # Store response
    await memory.add_response(message.sender_id, response)

    return response

registry.set_agent_handler(handle_message)

# Start everything
async def start():
    await bridge.connect()
    await cron.start()
    channels.start()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(start())
    app.run(host="0.0.0.0", port=6777)
```

### Multi-Channel Notification System

```python
"""Send notifications across multiple channels."""

from integrations.channels import get_registry
from integrations.channels.automation import ScheduledMessageManager, ScheduledMessage

registry = get_registry()

async def notify_all(message: str, priority: str = "normal"):
    """Send notification to all configured channels."""

    # Channel-specific targets
    targets = {
        "telegram": ["-1001234567890", "-1009876543210"],
        "discord": ["123456789", "987654321"],
        "slack": ["C-general", "C-announcements"],
    }

    results = {}
    for channel, chat_ids in targets.items():
        for chat_id in chat_ids:
            result = await registry.send_to_channel(
                channel=channel,
                chat_id=chat_id,
                text=f"{'🚨 ' if priority == 'high' else ''}{message}",
            )
            results[f"{channel}:{chat_id}"] = result.success

    return results

# Schedule daily notifications
scheduler = ScheduledMessageManager(registry)

scheduler.schedule(ScheduledMessage(
    id="daily-report",
    channel="slack",
    chat_id="C-reports",
    text="Daily report is ready!",
    cron="0 9 * * 1-5",  # Weekdays at 9 AM
))
```

### AI-Powered Support Bot

```python
"""Support bot with memory and multi-channel support."""

from integrations.channels import get_registry, Message
from integrations.channels.memory import MemoryStore, MemorySearch
from integrations.channels.commands import CommandRegistry
from integrations.channels.response import TemplateEngine

registry = get_registry()
memory = MemoryStore()
search = MemorySearch(memory)
templates = TemplateEngine()
commands = CommandRegistry()

# Templates
templates.register_template("greeting", "Hello {user}! How can I help you today?")
templates.register_template("escalate", "I'm connecting you with a human agent. Please wait...")
templates.register_template("resolved", "Glad I could help! Is there anything else?")

async def support_handler(message: Message) -> str:
    # Search for similar past issues
    similar = await search.search(
        query=message.text,
        user_id=message.sender_id,
        limit=3,
    )

    # Build context
    context = f"User: {message.sender_name}\n"
    context += f"Channel: {message.channel}\n"
    if similar:
        context += f"Similar past issues:\n"
        for item in similar:
            context += f"- {item.content[:100]}...\n"

    # Get AI response
    response = await call_ai(
        system="You are a helpful support agent.",
        context=context,
        query=message.text,
    )

    # Store interaction
    await memory.add(message.sender_id, message.text, response)

    # Check if escalation needed
    if "human" in message.text.lower() or "agent" in message.text.lower():
        await notify_support_team(message)
        return templates.render_named("escalate")

    return response

registry.set_agent_handler(support_handler)
```

---

## Summary

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| **channels** | Channel adapters | `ChannelAdapter`, `ChannelRegistry` |
| **queue** | Message pipeline | `RateLimiter`, `RetryHandler`, `MessageQueue` |
| **commands** | Command handling | `CommandRegistry`, `ArgumentParser` |
| **response** | Response formatting | `TypingManager`, `StreamingResponse` |
| **media** | Media processing | `VisionProcessor`, `TTSEngine`, `ImageGenerator` |
| **automation** | Automation | `CronManager`, `WorkflowEngine`, `TriggerManager` |
| **bridge** | Cross-channel | `ChannelBridge`, `BridgeRule` |
| **memory** | Conversation memory | `MemoryStore`, `MemorySearch` |
| **identity** | User/agent identity | `AgentIdentity`, `PreferenceManager` |
| **plugins** | Plugin system | `PluginManager`, `Plugin` |
| **gateway** | Inter-service comm | `GatewayProtocol` |

For more details, see the source code in `integrations/channels/`.
