import apiClient from '../client';
import type {
  Metrics,
  ChannelMetrics,
  LatencyMetrics,
  SystemStatus,
  HealthCheck,
  Version,
} from '../../types/api';

export const metricsApi = {
  // Get current metrics
  get: async () => {
    const response = await apiClient.get<Metrics>('/metrics');
    return response.data;
  },

  // Get metrics history
  getHistory: async (limit = 100) => {
    const response = await apiClient.get<Metrics[]>(`/metrics/history?limit=${limit}`);
    return response.data;
  },

  // Get all channel metrics
  getChannelMetrics: async () => {
    const response = await apiClient.get<Record<string, ChannelMetrics>>('/metrics/channels');
    return response.data;
  },

  // Get command metrics
  getCommandMetrics: async () => {
    const response = await apiClient.get<Record<string, {
      invocations: number;
      successful: number;
      failed: number;
      avg_response_time_ms: number;
    }>>('/metrics/commands');
    return response.data;
  },

  // Get queue metrics
  getQueueMetrics: async () => {
    const response = await apiClient.get<{
      throughput_per_second: number;
      avg_wait_time_ms: number;
      avg_processing_time_ms: number;
      queue_depth: number;
      rejected_count: number;
    }>('/metrics/queue');
    return response.data;
  },

  // Get error metrics
  getErrorMetrics: async () => {
    const response = await apiClient.get<{
      total_errors: number;
      error_rate: number;
      recent_errors: Array<{
        timestamp: string;
        message: string;
        type: string;
      }>;
    }>('/metrics/errors');
    return response.data;
  },

  // Get latency metrics
  getLatencyMetrics: async () => {
    const response = await apiClient.get<LatencyMetrics>('/metrics/latency');
    return response.data;
  },

  // Health check
  health: async () => {
    const response = await apiClient.get<HealthCheck>('/health');
    return response.data;
  },

  // Get system status
  status: async () => {
    const response = await apiClient.get<SystemStatus>('/status');
    return response.data;
  },

  // Get version info
  version: async () => {
    const response = await apiClient.get<Version>('/version');
    return response.data;
  },
};

export default metricsApi;
