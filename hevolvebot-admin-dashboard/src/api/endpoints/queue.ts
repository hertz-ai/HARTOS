import apiClient from '../client';
import type {
  QueueConfig,
  QueueStats,
  ConcurrencyLimits,
  RateLimitConfig,
  RetryConfig,
  BatchingConfig,
} from '../../types/api';

const BASE = '/queue';

export const queueApi = {
  // Get queue configuration
  getConfig: async () => {
    const response = await apiClient.get<QueueConfig>(`${BASE}/config`);
    return response.data;
  },

  // Update queue configuration
  updateConfig: async (config: Partial<QueueConfig>) => {
    const response = await apiClient.put<QueueConfig>(`${BASE}/config`, config);
    return response.data;
  },

  // Get queue statistics
  getStats: async () => {
    const response = await apiClient.get<QueueStats>(`${BASE}/stats`);
    return response.data;
  },

  // Clear queue
  clear: async () => {
    const response = await apiClient.post<{ cleared: boolean; messages_removed: number }>(
      `${BASE}/clear`
    );
    return response.data;
  },

  // Pause queue processing
  pause: async () => {
    const response = await apiClient.post<{ paused: boolean }>(`${BASE}/pause`);
    return response.data;
  },

  // Resume queue processing
  resume: async () => {
    const response = await apiClient.post<{ resumed: boolean }>(`${BASE}/resume`);
    return response.data;
  },

  // Get debounce config
  getDebounce: async () => {
    const response = await apiClient.get<{ debounce_ms: number }>(`${BASE}/debounce`);
    return response.data;
  },

  // Update debounce config
  updateDebounce: async (debounceMs: number) => {
    const response = await apiClient.put<{ debounce_ms: number }>(`${BASE}/debounce`, {
      debounce_ms: debounceMs,
    });
    return response.data;
  },

  // Get dedupe config
  getDedupe: async () => {
    const response = await apiClient.get<{ dedupe_window_seconds: number }>(`${BASE}/dedupe`);
    return response.data;
  },

  // Update dedupe config
  updateDedupe: async (windowSeconds: number) => {
    const response = await apiClient.put<{ dedupe_window_seconds: number }>(`${BASE}/dedupe`, {
      dedupe_window_seconds: windowSeconds,
    });
    return response.data;
  },

  // Get concurrency config
  getConcurrency: async () => {
    const response = await apiClient.get<ConcurrencyLimits>(`${BASE}/concurrency`);
    return response.data;
  },

  // Update concurrency config
  updateConcurrency: async (config: Partial<ConcurrencyLimits>) => {
    const response = await apiClient.put<ConcurrencyLimits>(`${BASE}/concurrency`, config);
    return response.data;
  },

  // Get rate limit config
  getRateLimit: async () => {
    const response = await apiClient.get<RateLimitConfig>(`${BASE}/rate-limit`);
    return response.data;
  },

  // Update rate limit config
  updateRateLimit: async (config: Partial<RateLimitConfig>) => {
    const response = await apiClient.put<RateLimitConfig>(`${BASE}/rate-limit`, config);
    return response.data;
  },

  // Get retry config
  getRetry: async () => {
    const response = await apiClient.get<RetryConfig>(`${BASE}/retry`);
    return response.data;
  },

  // Update retry config
  updateRetry: async (config: Partial<RetryConfig>) => {
    const response = await apiClient.put<RetryConfig>(`${BASE}/retry`, config);
    return response.data;
  },

  // Get batching config
  getBatching: async () => {
    const response = await apiClient.get<BatchingConfig>(`${BASE}/batching`);
    return response.data;
  },

  // Update batching config
  updateBatching: async (config: Partial<BatchingConfig>) => {
    const response = await apiClient.put<BatchingConfig>(`${BASE}/batching`, config);
    return response.data;
  },
};

export default queueApi;
