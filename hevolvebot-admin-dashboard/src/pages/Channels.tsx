import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  MessageSquare,
  Plus,
  Settings,
  Power,
  PowerOff,
  RefreshCw,
  Trash2,
  TestTube,
  MoreVertical,
} from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';
import channelsApi from '../api/endpoints/channels';
import type { ChannelConfig, ChannelType } from '../types/api';

const channelIcons: Record<string, string> = {
  telegram: 'T',
  discord: 'D',
  slack: 'S',
  whatsapp: 'W',
  matrix: 'M',
  teams: 'MS',
  line: 'L',
};

const channelColors: Record<string, string> = {
  telegram: 'bg-blue-500',
  discord: 'bg-indigo-500',
  slack: 'bg-purple-500',
  whatsapp: 'bg-green-500',
  matrix: 'bg-teal-500',
  teams: 'bg-violet-500',
  line: 'bg-emerald-500',
};

export default function Channels() {
  const queryClient = useQueryClient();
  const [selectedChannel, setSelectedChannel] = useState<string | null>(null);

  const { data: channelsResponse, isLoading } = useQuery({
    queryKey: ['channels'],
    queryFn: () => channelsApi.list(),
  });

  const enableMutation = useMutation({
    mutationFn: (channelType: string) => channelsApi.enable(channelType),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['channels'] }),
  });

  const disableMutation = useMutation({
    mutationFn: (channelType: string) => channelsApi.disable(channelType),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['channels'] }),
  });

  const testMutation = useMutation({
    mutationFn: (channelType: string) => channelsApi.test(channelType),
  });

  const reconnectMutation = useMutation({
    mutationFn: (channelType: string) => channelsApi.reconnect(channelType),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['channels'] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (channelType: string) => channelsApi.delete(channelType),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['channels'] }),
  });

  const channels = channelsResponse?.items ?? [];

  const handleToggle = (channel: ChannelConfig) => {
    if (channel.enabled) {
      disableMutation.mutate(channel.channel_type);
    } else {
      enableMutation.mutate(channel.channel_type);
    }
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Channels</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Manage messaging platform integrations
          </p>
        </div>
        <Button icon={<Plus className="w-4 h-4" />}>Add Channel</Button>
      </div>

      {/* Channel Cards */}
      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <RefreshCw className="w-8 h-8 text-primary-500 animate-spin" />
        </div>
      ) : channels.length === 0 ? (
        <Card>
          <div className="text-center py-12">
            <MessageSquare className="w-12 h-12 text-slate-300 dark:text-slate-600 mx-auto mb-4" />
            <h3 className="text-lg font-medium text-slate-900 dark:text-white mb-2">
              No channels configured
            </h3>
            <p className="text-slate-500 dark:text-slate-400 mb-4">
              Add a channel to start receiving messages
            </p>
            <Button icon={<Plus className="w-4 h-4" />}>Add Channel</Button>
          </div>
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {channels.map((channel) => (
            <Card key={channel.channel_type} className="relative">
              {/* Actions Menu */}
              <div className="absolute top-4 right-4">
                <button
                  onClick={() =>
                    setSelectedChannel(
                      selectedChannel === channel.channel_type ? null : channel.channel_type
                    )
                  }
                  className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg"
                >
                  <MoreVertical className="w-4 h-4 text-slate-500" />
                </button>
                {selectedChannel === channel.channel_type && (
                  <div className="absolute right-0 top-10 bg-white dark:bg-slate-700 rounded-lg shadow-lg border border-slate-200 dark:border-slate-600 py-1 min-w-[160px] z-10">
                    <button
                      onClick={() => {
                        testMutation.mutate(channel.channel_type);
                        setSelectedChannel(null);
                      }}
                      className="flex items-center gap-2 w-full px-4 py-2 text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-600"
                    >
                      <TestTube className="w-4 h-4" />
                      Test Connection
                    </button>
                    <button
                      onClick={() => {
                        reconnectMutation.mutate(channel.channel_type);
                        setSelectedChannel(null);
                      }}
                      className="flex items-center gap-2 w-full px-4 py-2 text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-600"
                    >
                      <RefreshCw className="w-4 h-4" />
                      Reconnect
                    </button>
                    <button className="flex items-center gap-2 w-full px-4 py-2 text-sm text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-600">
                      <Settings className="w-4 h-4" />
                      Configure
                    </button>
                    <hr className="my-1 border-slate-200 dark:border-slate-600" />
                    <button
                      onClick={() => {
                        if (confirm('Are you sure you want to delete this channel?')) {
                          deleteMutation.mutate(channel.channel_type);
                        }
                        setSelectedChannel(null);
                      }}
                      className="flex items-center gap-2 w-full px-4 py-2 text-sm text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
                    >
                      <Trash2 className="w-4 h-4" />
                      Delete
                    </button>
                  </div>
                )}
              </div>

              {/* Channel Info */}
              <div className="flex items-start gap-4">
                <div
                  className={`w-12 h-12 ${channelColors[channel.channel_type] || 'bg-slate-500'} rounded-lg flex items-center justify-center text-white font-bold text-lg`}
                >
                  {channelIcons[channel.channel_type] || channel.channel_type[0].toUpperCase()}
                </div>
                <div className="flex-1">
                  <h3 className="text-lg font-semibold text-slate-900 dark:text-white">
                    {channel.name}
                  </h3>
                  <p className="text-sm text-slate-500 dark:text-slate-400 capitalize">
                    {channel.channel_type}
                  </p>
                </div>
              </div>

              {/* Status */}
              <div className="mt-4 flex items-center gap-2">
                <div
                  className={`w-2 h-2 rounded-full ${channel.enabled ? 'bg-green-500' : 'bg-slate-300 dark:bg-slate-600'}`}
                />
                <span className="text-sm text-slate-600 dark:text-slate-300">
                  {channel.enabled ? 'Enabled' : 'Disabled'}
                </span>
              </div>

              {/* Toggle Button */}
              <div className="mt-4 pt-4 border-t border-slate-200 dark:border-slate-700">
                <Button
                  variant={channel.enabled ? 'secondary' : 'primary'}
                  className="w-full"
                  onClick={() => handleToggle(channel)}
                  loading={enableMutation.isPending || disableMutation.isPending}
                  icon={channel.enabled ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
                >
                  {channel.enabled ? 'Disable' : 'Enable'}
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
