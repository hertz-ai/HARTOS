// API Response Types

export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: string;
  timestamp: string;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
  has_prev: boolean;
}

// Channel Types
export type ChannelType =
  | 'telegram'
  | 'discord'
  | 'slack'
  | 'whatsapp'
  | 'matrix'
  | 'teams'
  | 'line'
  | 'signal'
  | 'imessage'
  | 'google_chat'
  | 'web';

export type ChannelStatus = 'connected' | 'disconnected' | 'connecting' | 'error' | 'paused';

export interface ChannelConfig {
  channel_type: ChannelType;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
  rate_limit?: RateLimitConfig;
  security?: SecurityConfig;
}

export interface ChannelStatusInfo {
  channel_type: ChannelType;
  name: string;
  status: ChannelStatus;
  connected_at?: string;
  message_count: number;
  error?: string;
  metrics: Record<string, unknown>;
}

// Queue Types
export interface QueueConfig {
  max_size: number;
  debounce_ms: number;
  dedupe_window_seconds: number;
  concurrency_limits: ConcurrencyLimits;
  rate_limits: RateLimitConfig;
  retry_config: RetryConfig;
  batching: BatchingConfig;
}

export interface ConcurrencyLimits {
  max_per_user: number;
  max_per_channel: number;
  max_per_chat: number;
  max_global: number;
}

export interface RateLimitConfig {
  requests_per_minute: number;
  requests_per_hour?: number;
  burst_limit: number;
}

export interface RetryConfig {
  max_retries: number;
  base_delay_seconds: number;
  max_delay_seconds: number;
  exponential_base: number;
}

export interface BatchingConfig {
  enabled: boolean;
  max_batch_size: number;
  max_wait_ms: number;
}

export interface QueueStats {
  queue_size: number;
  pending_messages: number;
  processing_messages: number;
  completed_messages: number;
  failed_messages: number;
  avg_processing_time_ms: number;
  messages_per_minute: number;
  by_channel: Record<string, number>;
  by_priority: Record<string, number>;
}

// Command Types
export interface CommandConfig {
  name: string;
  description: string;
  pattern: string;
  handler: string;
  enabled: boolean;
  admin_only: boolean;
  cooldown_seconds: number;
  usage_limit?: number;
  aliases: string[];
  arguments: CommandArgument[];
}

export interface CommandArgument {
  name: string;
  type: 'string' | 'number' | 'boolean';
  required: boolean;
  description?: string;
  default?: unknown;
}

export interface MentionGatingConfig {
  enabled: boolean;
  require_mention_in_groups: boolean;
  allow_reply_chain: boolean;
  keywords: string[];
  exempt_users: string[];
  exempt_channels: string[];
}

// Automation Types
export interface WebhookConfig {
  id?: string;
  name: string;
  url: string;
  secret?: string;
  events: string[];
  enabled: boolean;
  retry_count: number;
  timeout_seconds: number;
  headers: Record<string, string>;
}

export interface CronJob {
  id?: string;
  name: string;
  schedule: string;
  handler: string;
  enabled: boolean;
  last_run?: string;
  next_run?: string;
  run_count: number;
  payload: Record<string, unknown>;
}

export interface TriggerConfig {
  id?: string;
  name: string;
  trigger_type: string;
  pattern?: string;
  conditions: Record<string, unknown>;
  actions: TriggerAction[];
  enabled: boolean;
  priority: number;
}

export interface TriggerAction {
  type: string;
  config: Record<string, unknown>;
}

export interface Workflow {
  id?: string;
  name: string;
  description: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface WorkflowNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: Record<string, unknown>;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
}

export interface ScheduledMessage {
  id?: string;
  channel: string;
  chat_id: string;
  message: string;
  scheduled_time: string;
  recurring: boolean;
  recurrence_pattern?: string;
  enabled: boolean;
}

// Identity Types
export interface IdentityConfig {
  agent_id: string;
  display_name: string;
  avatar_url?: string;
  bio: string;
  personality: Record<string, unknown>;
  response_style: Record<string, unknown>;
  per_channel_identity: Record<string, Record<string, unknown>>;
}

export interface Avatar {
  id?: string;
  name: string;
  url: string;
  channel?: string;
  is_default: boolean;
}

export interface SenderMapping {
  platform_user_id: string;
  internal_user_id: string;
  channel: string;
  display_name?: string;
  metadata: Record<string, unknown>;
}

// Plugin Types
export interface PluginConfig {
  id: string;
  name: string;
  version: string;
  description: string;
  enabled: boolean;
  config: Record<string, unknown>;
  hooks: string[];
  dependencies: string[];
}

// Session Types
export interface SessionConfig {
  session_id: string;
  user_id: string;
  channel: string;
  chat_id: string;
  created_at: string;
  last_activity: string;
  context: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface PairingRequest {
  source_channel: string;
  source_chat_id: string;
  target_channel: string;
  target_chat_id: string;
  bidirectional: boolean;
}

// Metrics Types
export interface Metrics {
  timestamp: string;
  uptime_seconds: number;
  total_messages_processed: number;
  messages_per_minute: number;
  active_sessions: number;
  active_channels: number;
  queue_depth: number;
  memory_usage_mb: number;
  cpu_usage_percent: number;
  error_rate: number;
  latency_p50_ms: number;
  latency_p99_ms: number;
  by_channel: Record<string, ChannelMetrics>;
}

export interface ChannelMetrics {
  messages_sent: number;
  messages_received: number;
  avg_latency_ms: number;
  error_rate: number;
}

export interface LatencyMetrics {
  p50_ms: number;
  p75_ms: number;
  p90_ms: number;
  p95_ms: number;
  p99_ms: number;
  max_ms: number;
}

// Config Types
export interface SecurityConfig {
  allowed_users: string[];
  blocked_users: string[];
  allowed_channels: string[];
  blocked_channels: string[];
  rate_limit_per_user: number;
  require_authentication: boolean;
  encryption_enabled: boolean;
}

export interface MediaConfig {
  max_file_size_mb: number;
  allowed_types: string[];
  vision_enabled: boolean;
  audio_transcription_enabled: boolean;
  tts_enabled: boolean;
  image_generation_enabled: boolean;
  link_preview_enabled: boolean;
}

export interface ResponseConfig {
  typing_indicator_enabled: boolean;
  typing_delay_ms: number;
  reactions_enabled: boolean;
  templates_enabled: boolean;
  streaming_enabled: boolean;
  max_response_length: number;
  split_long_messages: boolean;
}

export interface MemoryConfig {
  backend: 'file' | 'redis' | 'sqlite';
  max_entries: number;
  ttl_seconds?: number;
  path?: string;
  connection_string?: string;
}

export interface GlobalConfig {
  queue: QueueConfig;
  security: SecurityConfig;
  media: MediaConfig;
  response: ResponseConfig;
  memory: MemoryConfig;
}

// LLM Backend Info
export interface LLMBackend {
  type: 'local_llamacpp' | 'cloud_fallback' | 'external' | 'local_transformers' | 'unknown';
  display_name: string;
  model: string;
  mode: string;
  url?: string;
  tps?: number;
  reason?: string;
  cloud_fallback_configured?: boolean;
}

// Status Types
export interface SystemStatus {
  status: 'running' | 'stopped' | 'error';
  uptime_seconds: number;
  channels_count: number;
  commands_count: number;
  plugins_count: number;
  sessions_count: number;
  webhooks_count: number;
  cron_jobs_count: number;
  triggers_count: number;
  workflows_count: number;
  // LLM backend fields (from enriched /status endpoint)
  node_tier?: string;
  llm_backend?: LLMBackend;
  crawl4ai_healthy?: boolean;
  crawl4ai_url?: string;
}

export interface HealthCheck {
  status: 'healthy' | 'unhealthy';
  uptime: number;
}

export interface Version {
  api_version: string;
  hevolvebot_version: string;
  python_version: string;
}
