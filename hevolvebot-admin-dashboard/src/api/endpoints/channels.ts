import apiClient from '../client';
import type {
  ChannelConfig,
  ChannelStatusInfo,
  RateLimitConfig,
  SecurityConfig,
  PaginatedResponse,
  ChannelMetrics,
} from '../../types/api';

const BASE = '/channels';

export const channelsApi = {
  // List all channels
  list: async (page = 1, pageSize = 20) => {
    const response = await apiClient.get<PaginatedResponse<ChannelConfig>>(
      `${BASE}?page=${page}&page_size=${pageSize}`
    );
    return response.data;
  },

  // Get channel by type
  get: async (channelType: string) => {
    const response = await apiClient.get<ChannelConfig>(`${BASE}/${channelType}`);
    return response.data;
  },

  // Create new channel
  create: async (config: Partial<ChannelConfig>) => {
    const response = await apiClient.post<ChannelConfig>(BASE, config);
    return response.data;
  },

  // Update channel
  update: async (channelType: string, config: Partial<ChannelConfig>) => {
    const response = await apiClient.put<ChannelConfig>(`${BASE}/${channelType}`, config);
    return response.data;
  },

  // Delete channel
  delete: async (channelType: string) => {
    const response = await apiClient.delete<{ deleted: string }>(`${BASE}/${channelType}`);
    return response.data;
  },

  // Get channel status
  getStatus: async (channelType: string) => {
    const response = await apiClient.get<ChannelStatusInfo>(`${BASE}/${channelType}/status`);
    return response.data;
  },

  // Enable channel
  enable: async (channelType: string) => {
    const response = await apiClient.post<{ channel: string; enabled: boolean }>(
      `${BASE}/${channelType}/enable`
    );
    return response.data;
  },

  // Disable channel
  disable: async (channelType: string) => {
    const response = await apiClient.post<{ channel: string; enabled: boolean }>(
      `${BASE}/${channelType}/disable`
    );
    return response.data;
  },

  // Test channel connection
  test: async (channelType: string) => {
    const response = await apiClient.post<{ channel: string; test_result: string; latency_ms: number }>(
      `${BASE}/${channelType}/test`
    );
    return response.data;
  },

  // Reconnect channel
  reconnect: async (channelType: string) => {
    const response = await apiClient.post<{ channel: string; reconnected: boolean }>(
      `${BASE}/${channelType}/reconnect`
    );
    return response.data;
  },

  // Get channel metrics
  getMetrics: async (channelType: string) => {
    const response = await apiClient.get<ChannelMetrics>(`${BASE}/${channelType}/metrics`);
    return response.data;
  },

  // Get rate limit config
  getRateLimit: async (channelType: string) => {
    const response = await apiClient.get<RateLimitConfig>(`${BASE}/${channelType}/rate-limit`);
    return response.data;
  },

  // Update rate limit config
  updateRateLimit: async (channelType: string, config: RateLimitConfig) => {
    const response = await apiClient.put<RateLimitConfig>(
      `${BASE}/${channelType}/rate-limit`,
      config
    );
    return response.data;
  },

  // Get security config
  getSecurity: async (channelType: string) => {
    const response = await apiClient.get<SecurityConfig>(`${BASE}/${channelType}/security`);
    return response.data;
  },

  // Update security config
  updateSecurity: async (channelType: string, config: SecurityConfig) => {
    const response = await apiClient.put<SecurityConfig>(
      `${BASE}/${channelType}/security`,
      config
    );
    return response.data;
  },
};

export default channelsApi;
