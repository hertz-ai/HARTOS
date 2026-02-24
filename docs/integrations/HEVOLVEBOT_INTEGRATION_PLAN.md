# HevolveBot Full Parity Integration Plan

## Autonomous Porting Roadmap for Complete Feature Parity

This document provides a complete, step-by-step plan for achieving **full parity** with HevolveBot - all 30+ channels and all features.

---

## Table of Contents

1. [Parity Target Summary](#parity-target-summary)
2. [Phase Overview](#phase-overview)
3. [Phase 1: Foundation (COMPLETE)](#phase-1-foundation-complete)
4. [Phase 2: Message Infrastructure](#phase-2-message-infrastructure)
5. [Phase 3: Command System](#phase-3-command-system)
6. [Phase 4: Core Channels Expansion](#phase-4-core-channels-expansion)
7. [Phase 5: Response Enhancement](#phase-5-response-enhancement)
8. [Phase 6: Media Pipeline](#phase-6-media-pipeline)
9. [Phase 7: Extension Channels Batch 1](#phase-7-extension-channels-batch-1)
10. [Phase 8: Extension Channels Batch 2](#phase-8-extension-channels-batch-2)
11. [Phase 9: Advanced Automation](#phase-9-advanced-automation)
12. [Phase 10: Memory & Knowledge](#phase-10-memory--knowledge)
13. [Phase 11: Identity & Personalization](#phase-11-identity--personalization)
14. [Phase 12: Gateway & Plugins](#phase-12-gateway--plugins)
15. [Testing Strategy](#testing-strategy)
16. [Execution Instructions](#execution-instructions)

---

## Parity Target Summary

### Channels: 30 Total

#### Core Channels (8)
| # | Channel | HevolveBot Source | Priority |
|---|---------|----------------|----------|
| 1 | Telegram | `src/telegram/` | ✅ Phase 1 |
| 2 | Discord | `src/discord/` | ✅ Phase 1 |
| 3 | WhatsApp | `src/whatsapp/` | Phase 4 |
| 4 | Slack | `src/slack/` | Phase 4 |
| 5 | Signal | `src/signal/` | Phase 4 |
| 6 | iMessage | `src/imessage/` | Phase 4 |
| 7 | Google Chat | `src/google-chat/` | Phase 4 |
| 8 | Web/Browser | `src/web/` | Phase 4 |

#### Extension Channels (22)
| # | Channel | HevolveBot Source | Priority |
|---|---------|----------------|----------|
| 9 | Matrix | `extensions/matrix/` | Phase 7 |
| 10 | Microsoft Teams | `extensions/msteams/` | Phase 7 |
| 11 | LINE | `extensions/line/` | Phase 7 |
| 12 | Mattermost | `extensions/mattermost/` | Phase 7 |
| 13 | Nextcloud Talk | `extensions/nextcloud-talk/` | Phase 7 |
| 14 | Twitch | `extensions/twitch/` | Phase 8 |
| 15 | Zalo | `extensions/zalo/` | Phase 8 |
| 16 | Zalo User | `extensions/zalouser/` | Phase 8 |
| 17 | Nostr | `extensions/nostr/` | Phase 8 |
| 18 | BlueBubbles | `extensions/bluebubbles/` | Phase 8 |
| 19 | Voice Call | `extensions/voice-call/` | Phase 8 |
| 20 | Tlon (Urbit) | `extensions/tlon/` | Phase 8 |
| 21 | Open Prose | `extensions/open-prose/` | Phase 8 |
| 22 | Rocket.Chat | `extensions/rocketchat/` | Phase 8 |
| 23 | Telegram User | `extensions/telegram-user/` | Phase 8 |
| 24 | Discord User | `extensions/discord-user/` | Phase 8 |
| 25 | WeChat | `extensions/wechat/` | Phase 8 |
| 26 | Viber | `extensions/viber/` | Phase 8 |
| 27 | Messenger | `extensions/messenger/` | Phase 8 |
| 28 | Instagram | `extensions/instagram/` | Phase 8 |
| 29 | Twitter/X | `extensions/twitter/` | Phase 8 |
| 30 | Email | `extensions/email/` | Phase 8 |

### Features: 75+ Capabilities

| Category | Count | Phases |
|----------|-------|--------|
| Message Infrastructure | 8 | Phase 2 |
| Command System | 6 | Phase 3 |
| Channel Adapters | 30 | Phases 1,4,7,8 |
| Response Enhancement | 5 | Phase 5 |
| Media Pipeline | 8 | Phase 6 |
| Automation | 6 | Phase 9 |
| Memory & Knowledge | 5 | Phase 10 |
| Identity & Personalization | 5 | Phase 11 |
| Gateway & Plugins | 7 | Phase 12 |

---

## Phase Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FULL PARITY ROADMAP                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Phase 1  ✅ COMPLETE ────────────────────────────────────── 115 tests      │
│  ├── Telegram Adapter                                                        │
│  ├── Discord Adapter                                                         │
│  ├── DM Pairing Security                                                     │
│  ├── Session Isolation                                                       │
│  └── Integration Tests                                                       │
│                                                                              │
│  Phase 2  Message Infrastructure ─────────────────────────── ~60 tests      │
│  ├── 2.1 Message Queue (drop/latest/backlog/priority)                       │
│  ├── 2.2 Inbound Debouncing                                                 │
│  ├── 2.3 Deduplication (content/id/combined)                                │
│  ├── 2.4 Concurrency Control                                                │
│  ├── 2.5 Rate Limiting                                                      │
│  ├── 2.6 Retry Logic with Backoff                                           │
│  ├── 2.7 Message Batching                                                   │
│  └── 2.8 Integration Tests                                                  │
│                                                                              │
│  Phase 3  Command System ─────────────────────────────────── ~50 tests      │
│  ├── 3.1 Command Registry                                                   │
│  ├── 3.2 Command Detection                                                  │
│  ├── 3.3 Argument Parsing                                                   │
│  ├── 3.4 Mention Gating                                                     │
│  ├── 3.5 Built-in Commands (20+)                                            │
│  └── 3.6 Integration Tests                                                  │
│                                                                              │
│  Phase 4  Core Channels Expansion ────────────────────────── ~80 tests      │
│  ├── 4.1 WhatsApp Adapter                                                   │
│  ├── 4.2 Slack Adapter                                                      │
│  ├── 4.3 Signal Adapter                                                     │
│  ├── 4.4 iMessage Adapter                                                   │
│  ├── 4.5 Google Chat Adapter                                                │
│  ├── 4.6 Web/Browser Adapter                                                │
│  └── 4.7 Integration Tests                                                  │
│                                                                              │
│  Phase 5  Response Enhancement ───────────────────────────── ~40 tests      │
│  ├── 5.1 Typing Indicators                                                  │
│  ├── 5.2 Ack Reactions                                                      │
│  ├── 5.3 Response Templates                                                 │
│  ├── 5.4 Response Streaming                                                 │
│  └── 5.5 Integration Tests                                                  │
│                                                                              │
│  Phase 6  Media Pipeline ─────────────────────────────────── ~60 tests      │
│  ├── 6.1 Media Understanding (Vision)                                       │
│  ├── 6.2 Audio Transcription (ASR)                                          │
│  ├── 6.3 Link Understanding                                                 │
│  ├── 6.4 Media Size Limits                                                  │
│  ├── 6.5 TTS System (OpenAI/ElevenLabs/Edge)                               │
│  ├── 6.6 Image Generation                                                   │
│  ├── 6.7 File Upload/Download                                               │
│  └── 6.8 Integration Tests                                                  │
│                                                                              │
│  Phase 7  Extension Channels Batch 1 ─────────────────────── ~60 tests      │
│  ├── 7.1 Matrix Adapter                                                     │
│  ├── 7.2 Microsoft Teams Adapter                                            │
│  ├── 7.3 LINE Adapter                                                       │
│  ├── 7.4 Mattermost Adapter                                                 │
│  ├── 7.5 Nextcloud Talk Adapter                                             │
│  └── 7.6 Integration Tests                                                  │
│                                                                              │
│  Phase 8  Extension Channels Batch 2 ─────────────────────── ~100 tests     │
│  ├── 8.1 Twitch Adapter                                                     │
│  ├── 8.2 Zalo Adapter                                                       │
│  ├── 8.3 Nostr Adapter                                                      │
│  ├── 8.4 BlueBubbles Adapter                                                │
│  ├── 8.5 Voice Call Adapter                                                 │
│  ├── 8.6 Rocket.Chat Adapter                                                │
│  ├── 8.7 WeChat Adapter                                                     │
│  ├── 8.8 Viber Adapter                                                      │
│  ├── 8.9 Messenger Adapter                                                  │
│  ├── 8.10 Instagram Adapter                                                 │
│  ├── 8.11 Twitter/X Adapter                                                 │
│  ├── 8.12 Email Adapter                                                     │
│  └── 8.13 Integration Tests                                                 │
│                                                                              │
│  Phase 9  Advanced Automation ────────────────────────────── ~50 tests      │
│  ├── 9.1 Webhook Management                                                 │
│  ├── 9.2 Enhanced Cron System                                               │
│  ├── 9.3 Event Triggers                                                     │
│  ├── 9.4 Workflow Automation                                                │
│  ├── 9.5 Scheduled Messages                                                 │
│  └── 9.6 Integration Tests                                                  │
│                                                                              │
│  Phase 10 Memory & Knowledge ─────────────────────────────── ~40 tests      │
│  ├── 10.1 Memory System (FTS5 + Embeddings)                                 │
│  ├── 10.2 File Tracking                                                     │
│  ├── 10.3 Embedding Cache                                                   │
│  ├── 10.4 Memory Search                                                     │
│  └── 10.5 Integration Tests                                                 │
│                                                                              │
│  Phase 11 Identity & Personalization ─────────────────────── ~30 tests      │
│  ├── 11.1 Agent Identity Management                                         │
│  ├── 11.2 Avatar System                                                     │
│  ├── 11.3 Sender Identity Mapping                                           │
│  ├── 11.4 Per-User Preferences                                              │
│  └── 11.5 Integration Tests                                                 │
│                                                                              │
│  Phase 12 Gateway & Plugins ──────────────────────────────── ~50 tests      │
│  ├── 12.1 Plugin System                                                     │
│  ├── 12.2 Plugin HTTP Server                                                │
│  ├── 12.3 Plugin Registry                                                   │
│  ├── 12.4 Gateway Protocol                                                  │
│  ├── 12.5 Admin Dashboard                                                   │
│  ├── 12.6 Metrics & Monitoring                                              │
│  └── 12.7 Integration Tests                                                 │
│                                                                              │
│  ═══════════════════════════════════════════════════════════════════════    │
│  TOTAL: 12 Phases │ 75+ Features │ 30 Channels │ ~700 Tests                 │
│  ═══════════════════════════════════════════════════════════════════════    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Foundation (COMPLETE)

### Status: ✅ COMPLETE - 115 Tests Passing

| Task | File | Tests | Status |
|------|------|-------|--------|
| 1.1 Telegram Adapter | `telegram_adapter.py` | 20 | ✅ |
| 1.2 Discord Adapter | `discord_adapter.py` | 10 | ✅ |
| 1.3 DM Pairing Security | `security.py` | 29 | ✅ |
| 1.4 Session Isolation | `session_manager.py` | 31 | ✅ |
| 1.5 Integration Tests | `test_channel_integration.py` | 25 | ✅ |

---

## Phase 2: Message Infrastructure

### Objective
Implement robust message handling infrastructure ported from HevolveBot's `src/auto-reply/` and `src/infra/`.

### Tasks

#### 2.1 Message Queue System
**HevolveBot Reference:** `src/auto-reply/reply/queue/`

**File:** `integrations/channels/queue/message_queue.py`

```python
class QueuePolicy(Enum):
    DROP = "drop"              # Drop new messages when busy
    LATEST = "latest"          # Keep only latest message
    BACKLOG = "backlog"        # Process all in order (FIFO)
    PRIORITY = "priority"      # Priority-based ordering

@dataclass
class QueueConfig:
    policy: QueuePolicy = QueuePolicy.BACKLOG
    max_size: int = 100
    max_age_seconds: int = 300
    priority_boost_mentions: bool = True
    priority_boost_replies: bool = True

class MessageQueue:
    def __init__(self, config: QueueConfig): ...
    def enqueue(self, message: Message, priority: int = 0) -> bool: ...
    def dequeue(self) -> Optional[Message]: ...
    def peek(self) -> Optional[Message]: ...
    def size(self) -> int: ...
    def clear(self) -> int: ...
    def get_stats(self) -> QueueStats: ...

class QueueManager:
    """Manages per-channel/user queues"""
    def get_queue(self, channel: str, chat_id: str) -> MessageQueue: ...
    def process_all(self) -> int: ...
    def cleanup_stale(self) -> int: ...
```

**Tests:** `tests/test_message_queue.py`
- [ ] Queue creation with each policy
- [ ] DROP policy behavior
- [ ] LATEST policy behavior
- [ ] BACKLOG policy behavior
- [ ] PRIORITY policy behavior
- [ ] Max size enforcement
- [ ] Max age expiration
- [ ] Priority boost for mentions
- [ ] Concurrent access safety
- [ ] Queue statistics

---

#### 2.2 Inbound Debouncing
**HevolveBot Reference:** `src/auto-reply/inbound-debounce.ts`

**File:** `integrations/channels/queue/debounce.py`

```python
@dataclass
class DebounceConfig:
    window_ms: int = 1000
    max_messages: int = 10
    channel_overrides: Dict[str, int] = field(default_factory=dict)

class InboundDebouncer:
    def __init__(self, config: DebounceConfig): ...
    async def debounce(self, message: Message) -> List[Message]: ...
    def flush(self, channel: str, chat_id: str) -> List[Message]: ...
    def flush_all(self) -> Dict[str, List[Message]]: ...
    def get_pending_count(self) -> int: ...
```

**Tests:** `tests/test_debounce.py`
- [ ] Single message passthrough
- [ ] Multiple messages collected
- [ ] Window expiration flush
- [ ] Max messages limit
- [ ] Channel-specific windows
- [ ] Manual flush
- [ ] Concurrent sessions

---

#### 2.3 Deduplication
**HevolveBot Reference:** `src/infra/dedupe.ts`, `src/auto-reply/reply/inbound-dedupe.ts`

**File:** `integrations/channels/queue/dedupe.py`

```python
class DedupeMode(Enum):
    CONTENT_HASH = "content"   # Hash message content
    MESSAGE_ID = "id"          # Platform message ID
    COMBINED = "combined"      # Both content + ID
    SEMANTIC = "semantic"      # Embedding-based similarity

@dataclass
class DedupeConfig:
    mode: DedupeMode = DedupeMode.COMBINED
    ttl_seconds: int = 300
    similarity_threshold: float = 0.95  # For semantic mode

class MessageDeduplicator:
    def __init__(self, config: DedupeConfig): ...
    def is_duplicate(self, message: Message) -> bool: ...
    def mark_seen(self, message: Message) -> str: ...  # Returns hash
    def cleanup_expired(self) -> int: ...
    def get_stats(self) -> DedupeStats: ...
```

**Tests:** `tests/test_dedupe.py`
- [ ] First message passes
- [ ] Exact duplicate blocked
- [ ] TTL expiration allows reprocess
- [ ] Content hash mode
- [ ] Message ID mode
- [ ] Combined mode
- [ ] High-volume handling

---

#### 2.4 Concurrency Control
**HevolveBot Reference:** `src/config/agent-limits.ts`

**File:** `integrations/channels/queue/concurrency.py`

```python
@dataclass
class ConcurrencyLimits:
    max_per_user: int = 4
    max_per_channel: int = 20
    max_per_chat: int = 2
    max_global: int = 100
    queue_when_limited: bool = True

class ConcurrencyController:
    def __init__(self, limits: ConcurrencyLimits): ...
    async def acquire(self, channel: str, chat_id: str, user_id: str) -> bool: ...
    def release(self, channel: str, chat_id: str, user_id: str) -> None: ...
    def get_usage(self) -> ConcurrencyStats: ...
    def is_available(self, channel: str, chat_id: str, user_id: str) -> bool: ...
```

**Tests:** `tests/test_concurrency.py`
- [ ] Slot acquisition
- [ ] Slot release
- [ ] Per-user limits
- [ ] Per-channel limits
- [ ] Per-chat limits
- [ ] Global limits
- [ ] Queue when limited
- [ ] Proper cleanup on errors

---

#### 2.5 Rate Limiting
**HevolveBot Reference:** `src/channels/rate-limit.ts`

**File:** `integrations/channels/queue/rate_limit.py`

```python
@dataclass
class RateLimitConfig:
    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    burst_limit: int = 10
    per_channel_limits: Dict[str, int] = field(default_factory=dict)

class RateLimiter:
    def __init__(self, config: RateLimitConfig): ...
    def check(self, channel: str, chat_id: str) -> RateLimitResult: ...
    def consume(self, channel: str, chat_id: str) -> bool: ...
    def get_remaining(self, channel: str, chat_id: str) -> int: ...
    def reset(self, channel: str, chat_id: str) -> None: ...
```

**Tests:** `tests/test_rate_limit.py`
- [ ] Under limit passes
- [ ] Over limit blocked
- [ ] Burst handling
- [ ] Per-channel limits
- [ ] Sliding window
- [ ] Reset functionality

---

#### 2.6 Retry Logic with Backoff
**HevolveBot Reference:** `src/infra/retry.ts`

**File:** `integrations/channels/queue/retry.py`

```python
@dataclass
class RetryConfig:
    max_retries: int = 3
    initial_delay_ms: int = 1000
    max_delay_ms: int = 30000
    exponential_base: float = 2.0
    jitter: bool = True

class RetryHandler:
    def __init__(self, config: RetryConfig): ...
    async def with_retry(self, func: Callable, *args, **kwargs) -> Any: ...
    def calculate_delay(self, attempt: int) -> int: ...
    def should_retry(self, error: Exception, attempt: int) -> bool: ...
```

**Tests:** `tests/test_retry.py`
- [ ] Success on first try
- [ ] Retry on failure
- [ ] Max retries respected
- [ ] Exponential backoff
- [ ] Jitter applied
- [ ] Non-retryable errors

---

#### 2.7 Message Batching
**HevolveBot Reference:** `src/auto-reply/reply/batch.ts`

**File:** `integrations/channels/queue/batching.py`

```python
@dataclass
class BatchConfig:
    max_batch_size: int = 10
    max_wait_ms: int = 500
    batch_by: str = "chat_id"  # chat_id, user_id, channel

class MessageBatcher:
    def __init__(self, config: BatchConfig): ...
    async def add(self, message: Message) -> Optional[List[Message]]: ...
    def flush(self, key: str) -> List[Message]: ...
    def flush_all(self) -> Dict[str, List[Message]]: ...
```

**Tests:** `tests/test_batching.py`
- [ ] Single message no batch
- [ ] Batch formation
- [ ] Max size triggers flush
- [ ] Timeout triggers flush
- [ ] Batch by different keys

---

#### 2.8 Phase 2 Integration

**File:** `integrations/channels/queue/__init__.py`

```python
from .message_queue import MessageQueue, QueueManager, QueuePolicy
from .debounce import InboundDebouncer, DebounceConfig
from .dedupe import MessageDeduplicator, DedupeMode
from .concurrency import ConcurrencyController, ConcurrencyLimits
from .rate_limit import RateLimiter, RateLimitConfig
from .retry import RetryHandler, RetryConfig
from .batching import MessageBatcher, BatchConfig

class MessagePipeline:
    """Unified message processing pipeline"""

    def __init__(
        self,
        debouncer: InboundDebouncer,
        deduplicator: MessageDeduplicator,
        rate_limiter: RateLimiter,
        concurrency: ConcurrencyController,
        queue_manager: QueueManager,
    ): ...

    async def process(self, message: Message) -> PipelineResult:
        """
        Full pipeline:
        1. Debounce → collect rapid messages
        2. Dedupe → filter duplicates
        3. Rate limit → check limits
        4. Concurrency → acquire slot
        5. Queue → enqueue if needed
        6. Return for processing
        """
        ...
```

**Tests:** `tests/test_message_pipeline.py`
- [ ] Full pipeline integration
- [ ] Pipeline with all features enabled
- [ ] Pipeline with selective features
- [ ] Error handling at each stage
- [ ] Regression: existing 115 tests pass

---

## Phase 3: Command System

### Objective
Implement full command system from HevolveBot's `src/auto-reply/commands-registry.ts`.

### Tasks

#### 3.1 Command Registry
**File:** `integrations/channels/commands/registry.py`

```python
@dataclass
class CommandDefinition:
    name: str
    aliases: List[str] = field(default_factory=list)
    description: str = ""
    usage: str = ""
    examples: List[str] = field(default_factory=list)
    arguments: List[ArgumentDef] = field(default_factory=list)
    handler: Callable = None
    channels: List[str] = field(default_factory=list)  # Empty = all
    require_pairing: bool = False
    require_admin: bool = False
    cooldown_seconds: int = 0
    hidden: bool = False

class CommandRegistry:
    def register(self, command: CommandDefinition) -> None: ...
    def unregister(self, name: str) -> bool: ...
    def get(self, name: str) -> Optional[CommandDefinition]: ...
    def list_all(self, include_hidden: bool = False) -> List[CommandDefinition]: ...
    def list_for_channel(self, channel: str) -> List[CommandDefinition]: ...
    async def execute(self, name: str, context: CommandContext) -> CommandResult: ...
```

---

#### 3.2 Command Detection
**File:** `integrations/channels/commands/detection.py`

```python
@dataclass
class DetectionConfig:
    prefixes: List[str] = field(default_factory=lambda: ["/", "!"])
    case_sensitive: bool = False
    allow_in_middle: bool = False  # /cmd in middle of message

class CommandDetector:
    def __init__(self, config: DetectionConfig, registry: CommandRegistry): ...
    def detect(self, text: str) -> Optional[ParsedCommand]: ...
    def extract_args(self, text: str, command: str) -> str: ...
```

---

#### 3.3 Argument Parsing
**File:** `integrations/channels/commands/arguments.py`

```python
class ArgumentType(Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    USER_MENTION = "user"
    CHANNEL_MENTION = "channel"
    URL = "url"
    DURATION = "duration"  # 1h, 30m, etc.
    DATETIME = "datetime"

@dataclass
class ArgumentDef:
    name: str
    type: ArgumentType
    required: bool = False
    default: Any = None
    choices: List[str] = None
    min_value: float = None
    max_value: float = None
    min_length: int = None
    max_length: int = None
    pattern: str = None  # Regex pattern
    description: str = ""

class ArgumentParser:
    def parse(self, text: str, definitions: List[ArgumentDef]) -> ParsedArgs: ...
    def validate(self, args: Dict, definitions: List[ArgumentDef]) -> List[str]: ...
    def format_usage(self, definitions: List[ArgumentDef]) -> str: ...
```

---

#### 3.4 Mention Gating
**File:** `integrations/channels/commands/mention_gating.py`

```python
class MentionMode(Enum):
    ALWAYS = "always"
    MENTION_ONLY = "mention"
    REPLY_ONLY = "reply"
    MENTION_OR_REPLY = "both"
    NEVER = "never"

@dataclass
class MentionConfig:
    default_mode: MentionMode = MentionMode.MENTION_ONLY
    dm_mode: MentionMode = MentionMode.ALWAYS
    channel_overrides: Dict[str, MentionMode] = field(default_factory=dict)
    chat_overrides: Dict[str, MentionMode] = field(default_factory=dict)

class MentionGate:
    def __init__(self, config: MentionConfig): ...
    def should_respond(self, message: Message) -> bool: ...
    def set_mode(self, channel: str, chat_id: str, mode: MentionMode) -> None: ...
    def get_mode(self, channel: str, chat_id: str) -> MentionMode: ...
    def is_mentioned(self, message: Message, bot_names: List[str]) -> bool: ...
    def is_reply_to_bot(self, message: Message) -> bool: ...
```

---

#### 3.5 Built-in Commands (20+)

**File:** `integrations/channels/commands/builtin.py`

```python
# User Commands
/help [command]          # Show help for command or list all
/start                   # Start interaction (welcome message)
/stop                    # Stop receiving messages
/status                  # Show bot and session status
/pair <code>             # Pair account with code
/unpair                  # Remove account pairing
/clear                   # Clear conversation history
/history [n]             # Show last n messages
/model [name]            # Show or set current model
/language [lang]         # Set preferred language
/timezone [tz]           # Set timezone
/feedback <text>         # Send feedback

# Group Commands
/mention <on|off|reply>  # Set mention mode for group
/quiet                   # Disable bot in group temporarily
/resume                  # Resume bot in group

# Admin Commands
/broadcast <message>     # Send to all users
/stats                   # Show usage statistics
/users                   # List active users
/ban <user>              # Block user
/unban <user>            # Unblock user
/config get <key>        # Get config value
/config set <key> <val>  # Set config value
/reload                  # Reload configuration
/debug <on|off>          # Toggle debug mode
```

---

#### 3.6 Phase 3 Integration

**Tests Required:**
- [ ] Command registration and lookup
- [ ] Alias resolution
- [ ] Command detection with prefixes
- [ ] Argument parsing all types
- [ ] Argument validation
- [ ] Mention gating modes
- [ ] Built-in command execution
- [ ] Admin command authorization
- [ ] Cooldown enforcement
- [ ] Regression: all previous tests pass

---

## Phase 4: Core Channels Expansion

### Objective
Implement remaining 6 core channels from HevolveBot.

### Tasks

#### 4.1 WhatsApp Adapter
**HevolveBot Reference:** `src/whatsapp/`

**File:** `integrations/channels/whatsapp_adapter.py`

**Features:**
- Web-based integration (Puppeteer/Playwright)
- QR code pairing
- Group support
- Media messages
- Reactions
- Status updates
- Business API option

**Dependencies:**
```
playwright>=1.40.0
whatsapp-web.js (via subprocess)
```

---

#### 4.2 Slack Adapter
**HevolveBot Reference:** `src/slack/`

**File:** `integrations/channels/slack_adapter.py`

**Features:**
- Socket Mode (recommended)
- Bolt framework integration
- Slash commands
- Interactive components
- Thread support
- File sharing
- App Home

**Dependencies:**
```
slack-bolt>=1.18.0
slack-sdk>=3.23.0
```

---

#### 4.3 Signal Adapter
**HevolveBot Reference:** `src/signal/`

**File:** `integrations/channels/signal_adapter.py`

**Features:**
- signal-cli REST API
- Linked device support
- Group V2 support
- Attachments
- Reactions
- Typing indicators

**Dependencies:**
```
# Requires signal-cli running as REST API
requests>=2.31.0
```

---

#### 4.4 iMessage Adapter
**HevolveBot Reference:** `src/imessage/`

**File:** `integrations/channels/imessage_adapter.py`

**Features:**
- macOS only (AppleScript bridge)
- BlueBubbles alternative for cross-platform
- Group chats
- Tapbacks (reactions)
- Attachments

**Dependencies:**
```
# macOS: pyobjc
# Cross-platform: BlueBubbles API
```

---

#### 4.5 Google Chat Adapter
**HevolveBot Reference:** `src/google-chat/`

**File:** `integrations/channels/google_chat_adapter.py`

**Features:**
- Webhook-based
- Card messages
- Slash commands
- Thread support
- Spaces (rooms)

**Dependencies:**
```
google-auth>=2.20.0
google-api-python-client>=2.90.0
```

---

#### 4.6 Web/Browser Adapter
**HevolveBot Reference:** `src/web/`

**File:** `integrations/channels/web_adapter.py`

**Features:**
- WebSocket real-time
- REST API fallback
- Session management
- File upload/download
- Typing indicators
- Read receipts

**Dependencies:**
```
websockets>=11.0
flask-socketio>=5.3.0
```

---

#### 4.7 Phase 4 Integration

**Tests per adapter:** ~12 each
- [ ] Adapter initialization
- [ ] Connection/authentication
- [ ] Send text message
- [ ] Receive text message
- [ ] Send media
- [ ] Receive media
- [ ] Group handling
- [ ] Mention detection
- [ ] Error handling
- [ ] Reconnection
- [ ] Rate limiting
- [ ] Integration with pipeline

---

## Phase 5: Response Enhancement

### Tasks

#### 5.1 Typing Indicators
**File:** `integrations/channels/response/typing.py`

```python
class TypingManager:
    async def start(self, channel: str, chat_id: str) -> None: ...
    async def stop(self, channel: str, chat_id: str) -> None: ...
    async def pulse(self, channel: str, chat_id: str) -> None:  # Keep alive
    @asynccontextmanager
    async def typing(self, channel: str, chat_id: str): ...
```

---

#### 5.2 Ack Reactions
**File:** `integrations/channels/response/reactions.py`

```python
class AckManager:
    received_emoji: str = "👀"
    processing_emoji: str = "⏳"
    complete_emoji: str = "✅"
    error_emoji: str = "❌"

    async def ack_received(self, message: Message) -> None: ...
    async def ack_processing(self, message: Message) -> None: ...
    async def ack_complete(self, message: Message, success: bool) -> None: ...
    async def remove_acks(self, message: Message) -> None: ...
```

---

#### 5.3 Response Templates
**File:** `integrations/channels/response/templates.py`

```python
class TemplateEngine:
    """
    Variables: {model}, {provider}, {identity.name}, {thinking_level},
               {user.name}, {channel}, {timestamp}, {session_id}
    """
    def render(self, template: str, context: Dict) -> str: ...
    def set_prefix(self, template: str) -> None: ...
    def set_suffix(self, template: str) -> None: ...
    def format_response(self, response: str, context: Dict) -> str: ...
```

---

#### 5.4 Response Streaming
**File:** `integrations/channels/response/streaming.py`

```python
class StreamingResponse:
    async def stream(self, channel: str, chat_id: str, generator: AsyncGenerator): ...
    async def update_message(self, message_id: str, content: str) -> None: ...
    async def finalize(self, message_id: str, final_content: str) -> None: ...
```

---

## Phase 6: Media Pipeline

### Tasks

#### 6.1 Media Understanding (Vision)
**File:** `integrations/channels/media/vision.py`

```python
class VisionProcessor:
    providers: List[str] = ["openai", "anthropic", "google", "local"]

    async def analyze_image(self, image: bytes, prompt: str = None) -> str: ...
    async def extract_text(self, image: bytes) -> str:  # OCR
    async def describe(self, image: bytes) -> str: ...
    async def detect_objects(self, image: bytes) -> List[Detection]: ...
```

---

#### 6.2 Audio Transcription
**File:** `integrations/channels/media/audio.py`

```python
class AudioProcessor:
    providers: List[str] = ["openai", "deepgram", "whisper-local"]

    async def transcribe(self, audio: bytes, language: str = None) -> str: ...
    async def detect_language(self, audio: bytes) -> str: ...
    async def get_duration(self, audio: bytes) -> float: ...
```

---

#### 6.3 Link Understanding
**File:** `integrations/channels/media/links.py`

```python
class LinkProcessor:
    async def detect(self, text: str) -> List[str]: ...
    async def fetch(self, url: str) -> LinkContent: ...
    async def preview(self, url: str) -> LinkPreview: ...
    async def summarize(self, url: str, max_length: int = 500) -> str: ...
```

---

#### 6.4 Media Limits
**File:** `integrations/channels/media/limits.py`

```python
@dataclass
class MediaLimits:
    max_image_bytes: int = 10 * 1024 * 1024
    max_video_bytes: int = 50 * 1024 * 1024
    max_audio_bytes: int = 25 * 1024 * 1024
    max_document_bytes: int = 20 * 1024 * 1024
    allowed_image_types: List[str] = field(default_factory=lambda: ["jpg", "png", "gif", "webp"])
    allowed_video_types: List[str] = field(default_factory=lambda: ["mp4", "webm", "mov"])

class MediaLimiter:
    def check(self, attachment: MediaAttachment) -> LimitResult: ...
    def get_limits(self, channel: str) -> MediaLimits: ...
    def set_limits(self, channel: str, limits: MediaLimits) -> None: ...
```

---

#### 6.5 TTS System
**File:** `integrations/channels/media/tts.py`

```python
class TTSProvider(Enum):
    OPENAI = "openai"
    ELEVENLABS = "elevenlabs"
    EDGE = "edge"
    GOOGLE = "google"
    AMAZON = "amazon"

class TTSEngine:
    async def synthesize(self, text: str, voice: str = None) -> bytes: ...
    async def list_voices(self) -> List[VoiceInfo]: ...
    def get_optimal_format(self, channel: str) -> str:  # opus, mp3, wav
    async def synthesize_ssml(self, ssml: str) -> bytes: ...
```

---

#### 6.6 Image Generation
**File:** `integrations/channels/media/image_gen.py`

```python
class ImageGenerator:
    providers: List[str] = ["openai", "stability", "midjourney"]

    async def generate(self, prompt: str, size: str = "1024x1024") -> bytes: ...
    async def edit(self, image: bytes, prompt: str, mask: bytes = None) -> bytes: ...
    async def variations(self, image: bytes, n: int = 1) -> List[bytes]: ...
```

---

#### 6.7 File Upload/Download
**File:** `integrations/channels/media/files.py`

```python
class FileManager:
    async def download(self, url: str, destination: str = None) -> str: ...
    async def upload(self, file_path: str, channel: str) -> str:  # Returns URL
    async def get_info(self, file_id: str, channel: str) -> FileInfo: ...
    def cleanup_temp(self, max_age_hours: int = 24) -> int: ...
```

---

## Phase 7: Extension Channels Batch 1

### Tasks

#### 7.1 Matrix Adapter
**HevolveBot Reference:** `extensions/matrix/`

**Features:**
- Matrix protocol (matrix-nio)
- E2EE support
- Room management
- Reactions
- Threads

---

#### 7.2 Microsoft Teams Adapter
**HevolveBot Reference:** `extensions/msteams/`

**Features:**
- Bot Framework
- Adaptive Cards
- Tabs
- Meeting integration
- File sharing

---

#### 7.3 LINE Adapter
**HevolveBot Reference:** `extensions/line/`

**Features:**
- Messaging API
- Rich menus
- Flex messages
- LIFF integration
- Webhooks

---

#### 7.4 Mattermost Adapter
**HevolveBot Reference:** `extensions/mattermost/`

**Features:**
- WebSocket API
- Slash commands
- Interactive messages
- File attachments
- Threads

---

#### 7.5 Nextcloud Talk Adapter
**HevolveBot Reference:** `extensions/nextcloud-talk/`

**Features:**
- REST API
- WebSocket for real-time
- File sharing
- Reactions

---

## Phase 8: Extension Channels Batch 2

### Tasks (12 Adapters)

| # | Adapter | Key Features |
|---|---------|--------------|
| 8.1 | Twitch | Chat, whispers, commands, bits |
| 8.2 | Zalo | Vietnamese platform, OA API |
| 8.3 | Nostr | Decentralized, NIP support |
| 8.4 | BlueBubbles | iMessage bridge, cross-platform |
| 8.5 | Voice Call | Twilio/Vonage, IVR |
| 8.6 | Rocket.Chat | REST + Realtime API |
| 8.7 | WeChat | Official Account API |
| 8.8 | Viber | Bot API, keyboards |
| 8.9 | Messenger | Meta Graph API |
| 8.10 | Instagram | Direct API |
| 8.11 | Twitter/X | DMs, mentions |
| 8.12 | Email | IMAP/SMTP, threading |

---

## Phase 9: Advanced Automation

### Tasks

#### 9.1 Webhook Management
**File:** `integrations/channels/automation/webhooks.py`

```python
class WebhookManager:
    def register(self, name: str, url: str, events: List[str]) -> Webhook: ...
    def unregister(self, name: str) -> bool: ...
    def list_webhooks(self) -> List[Webhook]: ...
    async def trigger(self, event: str, payload: Dict) -> List[WebhookResult]: ...
    def verify_signature(self, payload: bytes, signature: str) -> bool: ...
```

---

#### 9.2 Enhanced Cron System
**File:** `integrations/channels/automation/cron.py`

```python
class CronManager:
    """Enhanced scheduling beyond APScheduler"""
    def schedule_at(self, time: datetime, job: Job) -> str: ...
    def schedule_every(self, interval: timedelta, job: Job, anchor: datetime = None) -> str: ...
    def schedule_cron(self, expression: str, job: Job, timezone: str = None) -> str: ...
    def pause(self, job_id: str) -> bool: ...
    def resume(self, job_id: str) -> bool: ...
    def list_jobs(self) -> List[ScheduledJob]: ...
```

---

#### 9.3 Event Triggers
**File:** `integrations/channels/automation/triggers.py`

```python
class TriggerType(Enum):
    MESSAGE_RECEIVED = "message"
    USER_JOINED = "user_joined"
    USER_LEFT = "user_left"
    REACTION_ADDED = "reaction"
    FILE_SHARED = "file"
    MENTION = "mention"
    KEYWORD = "keyword"
    REGEX = "regex"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"

class TriggerManager:
    def register(self, trigger_type: TriggerType, condition: Dict, action: Callable) -> str: ...
    def unregister(self, trigger_id: str) -> bool: ...
    async def evaluate(self, event: Event) -> List[TriggerResult]: ...
```

---

#### 9.4 Workflow Automation
**File:** `integrations/channels/automation/workflows.py`

```python
class WorkflowStep:
    action: str
    params: Dict
    conditions: List[Condition]
    on_success: str  # Next step ID
    on_failure: str

class Workflow:
    steps: List[WorkflowStep]
    triggers: List[TriggerType]

    async def execute(self, context: Dict) -> WorkflowResult: ...

class WorkflowEngine:
    def register(self, workflow: Workflow) -> str: ...
    def list_workflows(self) -> List[Workflow]: ...
    async def run(self, workflow_id: str, context: Dict) -> WorkflowResult: ...
```

---

#### 9.5 Scheduled Messages
**File:** `integrations/channels/automation/scheduled_messages.py`

```python
class ScheduledMessage:
    channel: str
    chat_id: str
    content: str
    media: List[MediaAttachment]
    schedule: Union[datetime, str]  # datetime or cron
    repeat: bool

class ScheduledMessageManager:
    def schedule(self, message: ScheduledMessage) -> str: ...
    def cancel(self, message_id: str) -> bool: ...
    def list_pending(self, channel: str = None) -> List[ScheduledMessage]: ...
```

---

## Phase 10: Memory & Knowledge

### Tasks

#### 10.1 Memory System
**HevolveBot Reference:** `src/memory/`

**File:** `integrations/channels/memory/memory_store.py`

```python
class MemoryStore:
    """SQLite FTS5 + Embedding-based memory"""

    def __init__(self, db_path: str): ...
    async def add(self, content: str, metadata: Dict) -> str: ...
    async def search(self, query: str, limit: int = 10) -> List[MemoryItem]: ...
    async def search_semantic(self, query: str, limit: int = 10) -> List[MemoryItem]: ...
    async def delete(self, memory_id: str) -> bool: ...
    async def clear(self, filter: Dict = None) -> int: ...
```

---

#### 10.2 File Tracking
**File:** `integrations/channels/memory/file_tracker.py`

```python
class FileTracker:
    """Monitor and index file changes"""

    def watch(self, path: str, patterns: List[str] = None) -> None: ...
    def unwatch(self, path: str) -> None: ...
    async def sync(self, path: str) -> SyncResult: ...
    def get_changes(self, since: datetime) -> List[FileChange]: ...
```

---

#### 10.3 Embedding Cache
**File:** `integrations/channels/memory/embeddings.py`

```python
class EmbeddingCache:
    """Cache embeddings with TTL"""

    async def get_embedding(self, text: str, model: str = None) -> List[float]: ...
    async def get_batch(self, texts: List[str]) -> List[List[float]]: ...
    def invalidate(self, text_hash: str) -> bool: ...
    def cleanup(self, max_age_days: int = 30) -> int: ...
```

---

#### 10.4 Memory Search
**File:** `integrations/channels/memory/search.py`

```python
class MemorySearch:
    """Unified search across memory sources"""

    async def search(self, query: str, sources: List[str] = None) -> SearchResults: ...
    async def search_context(self, query: str, session_id: str) -> ContextResults: ...
    def add_source(self, name: str, source: MemorySource) -> None: ...
```

---

## Phase 11: Identity & Personalization

### Tasks

#### 11.1 Agent Identity
**File:** `integrations/channels/identity/agent_identity.py`

```python
@dataclass
class AgentIdentity:
    id: str
    name: str
    description: str
    avatar_url: str
    emoji: str
    personality: str
    capabilities: List[str]

class IdentityManager:
    def get_identity(self, agent_id: str) -> AgentIdentity: ...
    def set_identity(self, agent_id: str, identity: AgentIdentity) -> None: ...
    def get_identity_for_channel(self, agent_id: str, channel: str) -> AgentIdentity: ...
```

---

#### 11.2 Avatar System
**File:** `integrations/channels/identity/avatars.py`

```python
class AvatarManager:
    def get_avatar(self, agent_id: str) -> bytes: ...
    def set_avatar(self, agent_id: str, image: bytes) -> str: ...
    def generate_avatar(self, prompt: str) -> bytes: ...
    def get_avatar_url(self, agent_id: str, channel: str) -> str: ...
```

---

#### 11.3 Sender Identity Mapping
**File:** `integrations/channels/identity/sender_mapping.py`

```python
class SenderIdentityMapper:
    """Map channel users to internal identities"""

    def map(self, channel: str, sender_id: str) -> UserIdentity: ...
    def set_mapping(self, channel: str, sender_id: str, identity: UserIdentity) -> None: ...
    def get_cross_channel(self, user_id: str) -> List[ChannelIdentity]: ...
```

---

#### 11.4 User Preferences
**File:** `integrations/channels/identity/preferences.py`

```python
@dataclass
class UserPreferences:
    language: str = "en"
    timezone: str = "UTC"
    model: str = None
    response_style: str = "balanced"
    notifications: bool = True
    theme: str = "auto"

class PreferenceManager:
    def get(self, user_id: str) -> UserPreferences: ...
    def set(self, user_id: str, prefs: UserPreferences) -> None: ...
    def update(self, user_id: str, **kwargs) -> UserPreferences: ...
```

---

## Phase 12: Gateway & Plugins

### Tasks

#### 12.1 Plugin System
**File:** `integrations/channels/plugins/plugin_system.py`

```python
class Plugin:
    name: str
    version: str
    description: str

    def on_load(self) -> None: ...
    def on_unload(self) -> None: ...
    def on_message(self, message: Message) -> Optional[Message]: ...
    def on_response(self, response: str) -> Optional[str]: ...

class PluginManager:
    def load(self, plugin_path: str) -> Plugin: ...
    def unload(self, plugin_name: str) -> bool: ...
    def list_plugins(self) -> List[PluginInfo]: ...
    def enable(self, plugin_name: str) -> bool: ...
    def disable(self, plugin_name: str) -> bool: ...
```

---

#### 12.2 Plugin HTTP Server
**File:** `integrations/channels/plugins/http_server.py`

```python
class PluginHTTPServer:
    """Expose plugin endpoints"""

    def register_route(self, plugin: str, path: str, handler: Callable) -> None: ...
    def unregister_routes(self, plugin: str) -> None: ...
    def start(self, port: int = 8080) -> None: ...
    def stop(self) -> None: ...
```

---

#### 12.3 Plugin Registry
**File:** `integrations/channels/plugins/registry.py`

```python
class PluginRegistry:
    def search(self, query: str) -> List[PluginInfo]: ...
    def install(self, plugin_id: str) -> bool: ...
    def uninstall(self, plugin_id: str) -> bool: ...
    def update(self, plugin_id: str) -> bool: ...
    def check_updates(self) -> List[PluginUpdate]: ...
```

---

#### 12.4 Gateway Protocol
**File:** `integrations/channels/gateway/protocol.py`

```python
class GatewayProtocol:
    """JSON-RPC 2.0 based gateway"""

    def register_method(self, name: str, handler: Callable) -> None: ...
    async def handle_request(self, request: Dict) -> Dict: ...
    async def send_notification(self, method: str, params: Dict) -> None: ...
```

---

#### 12.5 Admin Dashboard
**File:** `integrations/channels/admin/dashboard.py`

```python
class AdminDashboard:
    """Web-based admin interface"""

    def get_stats(self) -> DashboardStats: ...
    def get_active_sessions(self) -> List[SessionInfo]: ...
    def get_channel_status(self) -> Dict[str, ChannelStatus]: ...
    def get_queue_stats(self) -> QueueStats: ...
    def get_error_log(self, limit: int = 100) -> List[ErrorEntry]: ...
```

---

#### 12.6 Metrics & Monitoring
**File:** `integrations/channels/admin/metrics.py`

```python
class MetricsCollector:
    def record_message(self, channel: str, direction: str) -> None: ...
    def record_latency(self, channel: str, latency_ms: float) -> None: ...
    def record_error(self, channel: str, error_type: str) -> None: ...
    def get_metrics(self, period: str = "1h") -> Metrics: ...
    def export_prometheus(self) -> str: ...
```

---

## Testing Strategy

### Test Counts by Phase

| Phase | Unit | Integration | Regression | Total |
|-------|------|-------------|------------|-------|
| Phase 1 ✅ | 80 | 25 | 10 | 115 |
| Phase 2 | 50 | 15 | 10 | 75 |
| Phase 3 | 40 | 15 | 10 | 65 |
| Phase 4 | 60 | 20 | 10 | 90 |
| Phase 5 | 30 | 10 | 5 | 45 |
| Phase 6 | 50 | 15 | 10 | 75 |
| Phase 7 | 50 | 15 | 10 | 75 |
| Phase 8 | 80 | 25 | 15 | 120 |
| Phase 9 | 40 | 15 | 10 | 65 |
| Phase 10 | 35 | 10 | 5 | 50 |
| Phase 11 | 25 | 10 | 5 | 40 |
| Phase 12 | 40 | 15 | 10 | 65 |
| **Total** | **580** | **190** | **110** | **~880** |

### Test Commands

```bash
# Run specific phase
pytest tests/test_phase2_*.py -v --noconftest

# Run all channel tests
pytest tests/test_*_adapter.py -v --noconftest

# Run full regression
pytest tests/ -v --noconftest

# Run with coverage
pytest tests/ --cov=integrations/channels --cov-report=html
```

---

## Execution Instructions

### Autonomous Execution Protocol

For each task:

```
1. READ hevolvebot reference files
   └── hevolvebot-review/src/... or hevolvebot-review/extensions/...

2. CREATE implementation file
   └── integrations/channels/...

3. CREATE test file
   └── tests/test_...

4. IMPLEMENT component
   └── Follow hevolvebot patterns
   └── Adapt for Python/existing architecture

5. RUN unit tests
   └── pytest tests/test_<component>.py -v --noconftest

6. INTEGRATE with existing system
   └── Update __init__.py exports
   └── Hook into pipeline/registry

7. RUN integration tests
   └── pytest tests/test_<phase>_integration.py -v --noconftest

8. RUN full regression
   └── pytest tests/ -v --noconftest
   └── ALL previous tests must pass

9. UPDATE this document
   └── Mark task complete
   └── Update test counts

10. COMMIT changes
    └── git add . && git commit -m "Phase X.Y: <description>"
```

### Phase Completion Checklist

```
[ ] All tasks implemented
[ ] All unit tests passing
[ ] Integration tests passing
[ ] Full regression passing
[ ] Documentation updated
[ ] Code committed
[ ] Ready for next phase
```

---

## File Structure (Complete)

```
integrations/channels/
├── __init__.py
├── base.py                          # Base classes
├── registry.py                      # Channel registry
│
├── # Phase 1 (Complete)
├── telegram_adapter.py
├── discord_adapter.py
├── security.py
├── session_manager.py
├── flask_integration.py
│
├── # Phase 2: Message Infrastructure
├── queue/
│   ├── __init__.py
│   ├── message_queue.py
│   ├── debounce.py
│   ├── dedupe.py
│   ├── concurrency.py
│   ├── rate_limit.py
│   ├── retry.py
│   ├── batching.py
│   └── pipeline.py
│
├── # Phase 3: Commands
├── commands/
│   ├── __init__.py
│   ├── registry.py
│   ├── detection.py
│   ├── arguments.py
│   ├── mention_gating.py
│   └── builtin.py
│
├── # Phase 4: Core Channels
├── whatsapp_adapter.py
├── slack_adapter.py
├── signal_adapter.py
├── imessage_adapter.py
├── google_chat_adapter.py
├── web_adapter.py
│
├── # Phase 5: Response
├── response/
│   ├── __init__.py
│   ├── typing.py
│   ├── reactions.py
│   ├── templates.py
│   └── streaming.py
│
├── # Phase 6: Media
├── media/
│   ├── __init__.py
│   ├── vision.py
│   ├── audio.py
│   ├── links.py
│   ├── limits.py
│   ├── tts.py
│   ├── image_gen.py
│   └── files.py
│
├── # Phase 7-8: Extension Channels
├── extensions/
│   ├── __init__.py
│   ├── matrix_adapter.py
│   ├── teams_adapter.py
│   ├── line_adapter.py
│   ├── mattermost_adapter.py
│   ├── nextcloud_adapter.py
│   ├── twitch_adapter.py
│   ├── zalo_adapter.py
│   ├── nostr_adapter.py
│   ├── bluebubbles_adapter.py
│   ├── voice_adapter.py
│   ├── rocketchat_adapter.py
│   ├── wechat_adapter.py
│   ├── viber_adapter.py
│   ├── messenger_adapter.py
│   ├── instagram_adapter.py
│   ├── twitter_adapter.py
│   └── email_adapter.py
│
├── # Phase 9: Automation
├── automation/
│   ├── __init__.py
│   ├── webhooks.py
│   ├── cron.py
│   ├── triggers.py
│   ├── workflows.py
│   └── scheduled_messages.py
│
├── # Phase 10: Memory
├── memory/
│   ├── __init__.py
│   ├── memory_store.py
│   ├── file_tracker.py
│   ├── embeddings.py
│   └── search.py
│
├── # Phase 11: Identity
├── identity/
│   ├── __init__.py
│   ├── agent_identity.py
│   ├── avatars.py
│   ├── sender_mapping.py
│   └── preferences.py
│
├── # Phase 12: Gateway & Plugins
├── plugins/
│   ├── __init__.py
│   ├── plugin_system.py
│   ├── http_server.py
│   └── registry.py
├── gateway/
│   ├── __init__.py
│   └── protocol.py
└── admin/
    ├── __init__.py
    ├── dashboard.py
    └── metrics.py
```

---

## Success Criteria

### Full Parity Achieved When:

- [ ] All 30 channels implemented and tested
- [ ] All 75+ features implemented and tested
- [ ] ~880 tests passing
- [ ] 90%+ code coverage
- [ ] All hevolvebot capabilities available
- [ ] Existing langchain functionality preserved
- [ ] Documentation complete
- [ ] Production-ready

---

## Estimated Timeline

| Phase | Tasks | Estimated Tests |
|-------|-------|-----------------|
| Phase 1 | ✅ Complete | 115 |
| Phase 2 | 8 tasks | +75 = 190 |
| Phase 3 | 6 tasks | +65 = 255 |
| Phase 4 | 7 tasks | +90 = 345 |
| Phase 5 | 5 tasks | +45 = 390 |
| Phase 6 | 8 tasks | +75 = 465 |
| Phase 7 | 6 tasks | +75 = 540 |
| Phase 8 | 13 tasks | +120 = 660 |
| Phase 9 | 6 tasks | +65 = 725 |
| Phase 10 | 5 tasks | +50 = 775 |
| Phase 11 | 5 tasks | +40 = 815 |
| Phase 12 | 7 tasks | +65 = 880 |

---

## Detailed Step-by-Step Execution Guide

### Context Recovery Information

**If context is lost, read these files first:**
1. This file: `HEVOLVEBOT_INTEGRATION_PLAN.md`
2. HevolveBot source: `C:\Users\sathi\PycharmProjects\hevolvebot-review\`
3. Current project: `C:\Users\sathi\PycharmProjects\HARTOS\`
4. Existing channels: `integrations/channels/`
5. Existing tests: `tests/test_*.py`

**Run to check current state:**
```bash
cd /c/Users/sathi/PycharmProjects/HARTOS
./venv310/Scripts/python.exe -m pytest tests/test_telegram_adapter.py tests/test_discord_adapter.py tests/test_channel_security.py tests/test_session_manager.py tests/test_channel_integration.py -v --noconftest
```

---

### Phase 2 Detailed Steps

#### Step 2.1.1: Read HevolveBot Queue Source
```
READ: hevolvebot-review/src/auto-reply/reply/queue/enqueue.ts
READ: hevolvebot-review/src/auto-reply/reply/queue/drain.ts
READ: hevolvebot-review/src/auto-reply/reply/queue/settings.ts
```

#### Step 2.1.2: Create Queue Directory
```bash
mkdir -p integrations/channels/queue
touch integrations/channels/queue/__init__.py
```

#### Step 2.1.3: Create Message Queue Implementation
```
CREATE: integrations/channels/queue/message_queue.py

Contents should include:
- QueuePolicy enum (DROP, LATEST, BACKLOG, PRIORITY)
- QueueConfig dataclass
- QueueStats dataclass
- MessageQueue class with methods:
  - __init__(config: QueueConfig)
  - enqueue(message: Message, priority: int = 0) -> bool
  - dequeue() -> Optional[Message]
  - peek() -> Optional[Message]
  - size() -> int
  - clear() -> int
  - get_stats() -> QueueStats
- QueueManager class with methods:
  - get_queue(channel: str, chat_id: str) -> MessageQueue
  - process_all() -> int
  - cleanup_stale() -> int
```

#### Step 2.1.4: Create Queue Tests
```
CREATE: tests/test_message_queue.py

Test cases:
- test_queue_creation_backlog_policy
- test_queue_creation_drop_policy
- test_queue_creation_latest_policy
- test_queue_creation_priority_policy
- test_enqueue_dequeue_basic
- test_drop_policy_at_capacity
- test_latest_policy_replaces
- test_priority_ordering
- test_max_size_enforcement
- test_message_expiration
- test_queue_stats
- test_concurrent_access
```

#### Step 2.1.5: Run Queue Tests
```bash
./venv310/Scripts/python.exe -m pytest tests/test_message_queue.py -v --noconftest
```

---

#### Step 2.2.1: Read HevolveBot Debounce Source
```
READ: hevolvebot-review/src/auto-reply/inbound-debounce.ts
```

#### Step 2.2.2: Create Debounce Implementation
```
CREATE: integrations/channels/queue/debounce.py

Contents:
- DebounceConfig dataclass (window_ms, max_messages, channel_overrides)
- InboundDebouncer class with methods:
  - __init__(config: DebounceConfig)
  - async debounce(message: Message) -> List[Message]
  - flush(channel: str, chat_id: str) -> List[Message]
  - flush_all() -> Dict[str, List[Message]]
  - get_pending_count() -> int
```

#### Step 2.2.3: Create Debounce Tests
```
CREATE: tests/test_debounce.py

Test cases:
- test_single_message_immediate
- test_multiple_messages_collected
- test_window_expiration_triggers_flush
- test_max_messages_triggers_flush
- test_channel_specific_windows
- test_manual_flush
- test_concurrent_sessions
```

#### Step 2.2.4: Run Debounce Tests
```bash
./venv310/Scripts/python.exe -m pytest tests/test_debounce.py -v --noconftest
```

---

#### Step 2.3.1: Read HevolveBot Dedupe Source
```
READ: hevolvebot-review/src/infra/dedupe.ts
READ: hevolvebot-review/src/auto-reply/reply/inbound-dedupe.ts
```

#### Step 2.3.2: Create Dedupe Implementation
```
CREATE: integrations/channels/queue/dedupe.py

Contents:
- DedupeMode enum (CONTENT_HASH, MESSAGE_ID, COMBINED, SEMANTIC)
- DedupeConfig dataclass
- DedupeStats dataclass
- MessageDeduplicator class with methods:
  - __init__(config: DedupeConfig)
  - is_duplicate(message: Message) -> bool
  - mark_seen(message: Message) -> str
  - cleanup_expired() -> int
  - get_stats() -> DedupeStats
```

#### Step 2.3.3: Create Dedupe Tests
```
CREATE: tests/test_dedupe.py
```

#### Step 2.3.4: Run Dedupe Tests
```bash
./venv310/Scripts/python.exe -m pytest tests/test_dedupe.py -v --noconftest
```

---

#### Step 2.4.1: Read HevolveBot Concurrency Source
```
READ: hevolvebot-review/src/config/agent-limits.ts
```

#### Step 2.4.2: Create Concurrency Implementation
```
CREATE: integrations/channels/queue/concurrency.py

Contents:
- ConcurrencyLimits dataclass
- ConcurrencyStats dataclass
- ConcurrencyController class
```

#### Step 2.4.3: Create and Run Tests
```bash
CREATE: tests/test_concurrency.py
./venv310/Scripts/python.exe -m pytest tests/test_concurrency.py -v --noconftest
```

---

#### Step 2.5.1: Create Rate Limit Implementation
```
CREATE: integrations/channels/queue/rate_limit.py
CREATE: tests/test_rate_limit.py
```

#### Step 2.6.1: Create Retry Implementation
```
CREATE: integrations/channels/queue/retry.py
CREATE: tests/test_retry.py
```

#### Step 2.7.1: Create Batching Implementation
```
CREATE: integrations/channels/queue/batching.py
CREATE: tests/test_batching.py
```

---

#### Step 2.8.1: Create Pipeline Integration
```
CREATE: integrations/channels/queue/pipeline.py

Contents:
- PipelineResult dataclass
- MessagePipeline class combining all queue components
```

#### Step 2.8.2: Update Queue Package Exports
```
EDIT: integrations/channels/queue/__init__.py

Add exports for all classes
```

#### Step 2.8.3: Update Main Channel Exports
```
EDIT: integrations/channels/__init__.py

Add queue module exports
```

#### Step 2.8.4: Run Full Phase 2 Regression
```bash
./venv310/Scripts/python.exe -m pytest tests/test_message_queue.py tests/test_debounce.py tests/test_dedupe.py tests/test_concurrency.py tests/test_rate_limit.py tests/test_retry.py tests/test_batching.py -v --noconftest

# Then run ALL tests including Phase 1
./venv310/Scripts/python.exe -m pytest tests/test_telegram_adapter.py tests/test_discord_adapter.py tests/test_channel_security.py tests/test_session_manager.py tests/test_channel_integration.py tests/test_message_queue.py tests/test_debounce.py tests/test_dedupe.py tests/test_concurrency.py tests/test_rate_limit.py tests/test_retry.py tests/test_batching.py -v --noconftest
```

---

### Phase 3 Detailed Steps

#### Step 3.1.1: Read HevolveBot Command Registry
```
READ: hevolvebot-review/src/auto-reply/commands-registry.ts
```

#### Step 3.1.2: Create Commands Directory
```bash
mkdir -p integrations/channels/commands
touch integrations/channels/commands/__init__.py
```

#### Step 3.1.3: Create Registry Implementation
```
CREATE: integrations/channels/commands/registry.py

Contents:
- CommandDefinition dataclass
- CommandContext dataclass
- CommandResult dataclass
- CommandRegistry class
```

#### Step 3.2.1: Create Detection Implementation
```
READ: hevolvebot-review/src/auto-reply/command-detection.ts
CREATE: integrations/channels/commands/detection.py
CREATE: tests/test_command_detection.py
```

#### Step 3.3.1: Create Arguments Implementation
```
CREATE: integrations/channels/commands/arguments.py
CREATE: tests/test_command_arguments.py
```

#### Step 3.4.1: Create Mention Gating
```
READ: hevolvebot-review/src/channels/mention-gating.ts
CREATE: integrations/channels/commands/mention_gating.py
CREATE: tests/test_mention_gating.py
```

#### Step 3.5.1: Create Built-in Commands
```
CREATE: integrations/channels/commands/builtin.py

Implement these commands:
- /help, /start, /stop, /status
- /pair, /unpair, /clear, /history
- /model, /language, /timezone, /feedback
- /mention, /quiet, /resume
- /broadcast, /stats, /users, /ban, /unban
- /config, /reload, /debug
```

#### Step 3.6.1: Run Phase 3 Regression
```bash
./venv310/Scripts/python.exe -m pytest tests/ -v --noconftest -k "channel or telegram or discord or command or mention"
```

---

### Phase 4 Detailed Steps

#### Step 4.1.1: WhatsApp Adapter
```
READ: hevolvebot-review/src/whatsapp/
CREATE: integrations/channels/whatsapp_adapter.py
CREATE: tests/test_whatsapp_adapter.py

Dependencies to add to requirements.txt:
playwright>=1.40.0
```

#### Step 4.2.1: Slack Adapter
```
READ: hevolvebot-review/src/slack/
CREATE: integrations/channels/slack_adapter.py
CREATE: tests/test_slack_adapter.py

Dependencies:
slack-bolt>=1.18.0
slack-sdk>=3.23.0
```

#### Step 4.3.1: Signal Adapter
```
READ: hevolvebot-review/src/signal/
CREATE: integrations/channels/signal_adapter.py
CREATE: tests/test_signal_adapter.py
```

#### Step 4.4.1: iMessage Adapter
```
READ: hevolvebot-review/src/imessage/
CREATE: integrations/channels/imessage_adapter.py
CREATE: tests/test_imessage_adapter.py
```

#### Step 4.5.1: Google Chat Adapter
```
READ: hevolvebot-review/src/google-chat/
CREATE: integrations/channels/google_chat_adapter.py
CREATE: tests/test_google_chat_adapter.py
```

#### Step 4.6.1: Web Adapter
```
READ: hevolvebot-review/src/web/
CREATE: integrations/channels/web_adapter.py
CREATE: tests/test_web_adapter.py

Dependencies:
flask-socketio>=5.3.0
```

---

### Phase 5-12 Quick Reference

#### Phase 5: Response Enhancement
```
CREATE: integrations/channels/response/typing.py
CREATE: integrations/channels/response/reactions.py
CREATE: integrations/channels/response/templates.py
CREATE: integrations/channels/response/streaming.py
```

#### Phase 6: Media Pipeline
```
CREATE: integrations/channels/media/vision.py
CREATE: integrations/channels/media/audio.py
CREATE: integrations/channels/media/links.py
CREATE: integrations/channels/media/limits.py
CREATE: integrations/channels/media/tts.py
CREATE: integrations/channels/media/image_gen.py
CREATE: integrations/channels/media/files.py
```

#### Phase 7: Extension Channels Batch 1
```
READ: hevolvebot-review/extensions/matrix/
READ: hevolvebot-review/extensions/msteams/
READ: hevolvebot-review/extensions/line/
READ: hevolvebot-review/extensions/mattermost/
READ: hevolvebot-review/extensions/nextcloud-talk/

CREATE: integrations/channels/extensions/matrix_adapter.py
CREATE: integrations/channels/extensions/teams_adapter.py
CREATE: integrations/channels/extensions/line_adapter.py
CREATE: integrations/channels/extensions/mattermost_adapter.py
CREATE: integrations/channels/extensions/nextcloud_adapter.py
```

#### Phase 8: Extension Channels Batch 2
```
CREATE: integrations/channels/extensions/twitch_adapter.py
CREATE: integrations/channels/extensions/zalo_adapter.py
CREATE: integrations/channels/extensions/nostr_adapter.py
CREATE: integrations/channels/extensions/bluebubbles_adapter.py
CREATE: integrations/channels/extensions/voice_adapter.py
CREATE: integrations/channels/extensions/rocketchat_adapter.py
CREATE: integrations/channels/extensions/wechat_adapter.py
CREATE: integrations/channels/extensions/viber_adapter.py
CREATE: integrations/channels/extensions/messenger_adapter.py
CREATE: integrations/channels/extensions/instagram_adapter.py
CREATE: integrations/channels/extensions/twitter_adapter.py
CREATE: integrations/channels/extensions/email_adapter.py
```

#### Phase 9: Automation
```
CREATE: integrations/channels/automation/webhooks.py
CREATE: integrations/channels/automation/cron.py
CREATE: integrations/channels/automation/triggers.py
CREATE: integrations/channels/automation/workflows.py
CREATE: integrations/channels/automation/scheduled_messages.py
```

#### Phase 10: Memory
```
CREATE: integrations/channels/memory/memory_store.py
CREATE: integrations/channels/memory/file_tracker.py
CREATE: integrations/channels/memory/embeddings.py
CREATE: integrations/channels/memory/search.py
```

#### Phase 11: Identity
```
CREATE: integrations/channels/identity/agent_identity.py
CREATE: integrations/channels/identity/avatars.py
CREATE: integrations/channels/identity/sender_mapping.py
CREATE: integrations/channels/identity/preferences.py
```

#### Phase 12: Gateway & Plugins
```
CREATE: integrations/channels/plugins/plugin_system.py
CREATE: integrations/channels/plugins/http_server.py
CREATE: integrations/channels/plugins/registry.py
CREATE: integrations/channels/gateway/protocol.py
CREATE: integrations/channels/admin/dashboard.py
CREATE: integrations/channels/admin/metrics.py
```

---

### Critical Paths and File Locations

**HevolveBot Source (TypeScript):**
```
C:\Users\sathi\PycharmProjects\hevolvebot-review\
├── src\
│   ├── auto-reply\          # Message handling, commands, queue
│   ├── channels\            # Channel utilities
│   ├── telegram\            # Telegram adapter
│   ├── discord\             # Discord adapter
│   ├── whatsapp\            # WhatsApp adapter
│   ├── slack\               # Slack adapter
│   ├── signal\              # Signal adapter
│   ├── imessage\            # iMessage adapter
│   ├── google-chat\         # Google Chat adapter
│   ├── web\                 # Web adapter
│   ├── infra\               # Infrastructure (dedupe, retry)
│   ├── memory\              # Memory system
│   ├── tts\                 # Text-to-speech
│   ├── media-understanding\ # Vision, audio processing
│   ├── link-understanding\  # URL processing
│   └── cron\                # Scheduling
└── extensions\              # Additional channel adapters
```

**Our Python Implementation:**
```
C:\Users\sathi\PycharmProjects\HARTOS\
├── integrations\channels\
│   ├── __init__.py
│   ├── base.py              # ChannelAdapter, Message, etc.
│   ├── registry.py          # ChannelRegistry
│   ├── telegram_adapter.py  # ✅ Phase 1
│   ├── discord_adapter.py   # ✅ Phase 1
│   ├── security.py          # ✅ Phase 1 (pairing)
│   ├── session_manager.py   # ✅ Phase 1
│   ├── flask_integration.py # ✅ Phase 1
│   ├── queue\               # Phase 2
│   ├── commands\            # Phase 3
│   ├── response\            # Phase 5
│   ├── media\               # Phase 6
│   ├── extensions\          # Phases 7-8
│   ├── automation\          # Phase 9
│   ├── memory\              # Phase 10
│   ├── identity\            # Phase 11
│   ├── plugins\             # Phase 12
│   ├── gateway\             # Phase 12
│   └── admin\               # Phase 12
└── tests\
    └── test_*.py
```

---

### Verification Commands

**After each implementation:**
```bash
# Run specific test
./venv310/Scripts/python.exe -m pytest tests/test_<component>.py -v --noconftest

# Run phase tests
./venv310/Scripts/python.exe -m pytest tests/test_phase<N>_*.py -v --noconftest

# Run full regression (REQUIRED before moving to next phase)
./venv310/Scripts/python.exe -m pytest tests/ -v --noconftest

# Check test count
./venv310/Scripts/python.exe -m pytest tests/ --collect-only | grep "test session starts"
```

**Expected test counts:**
- After Phase 1: 115 tests ✅
- After Phase 2: ~190 tests
- After Phase 3: ~255 tests
- After Phase 4: ~345 tests
- After Phase 5: ~390 tests
- After Phase 6: ~465 tests
- After Phase 7: ~540 tests
- After Phase 8: ~660 tests
- After Phase 9: ~725 tests
- After Phase 10: ~775 tests
- After Phase 11: ~815 tests
- After Phase 12: ~880 tests

---

*Document Version: 2.1 - Full Parity Edition with Detailed Steps*
*Created: 2025-01-27*
*Last Updated: 2025-01-27*
*Status: Ready for Autonomous Execution*
