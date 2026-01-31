import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Layers, Pause, Play, Trash2, RefreshCw, Settings } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';
import StatCard from '../components/charts/StatCard';
import queueApi from '../api/endpoints/queue';

export default function Queue() {
  const queryClient = useQueryClient();

  const { data: config, isLoading: configLoading } = useQuery({
    queryKey: ['queue', 'config'],
    queryFn: () => queueApi.getConfig(),
  });

  const { data: stats, isLoading: statsLoading } = useQuery({
    queryKey: ['queue', 'stats'],
    queryFn: () => queueApi.getStats(),
    refetchInterval: 5000,
  });

  const pauseMutation = useMutation({
    mutationFn: () => queueApi.pause(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['queue'] }),
  });

  const resumeMutation = useMutation({
    mutationFn: () => queueApi.resume(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['queue'] }),
  });

  const clearMutation = useMutation({
    mutationFn: () => queueApi.clear(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['queue'] }),
  });

  const isLoading = configLoading || statsLoading;

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Queue</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Message processing pipeline configuration
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button
            variant="secondary"
            icon={<Pause className="w-4 h-4" />}
            onClick={() => pauseMutation.mutate()}
            loading={pauseMutation.isPending}
          >
            Pause
          </Button>
          <Button
            variant="secondary"
            icon={<Play className="w-4 h-4" />}
            onClick={() => resumeMutation.mutate()}
            loading={resumeMutation.isPending}
          >
            Resume
          </Button>
          <Button
            variant="danger"
            icon={<Trash2 className="w-4 h-4" />}
            onClick={() => {
              if (confirm('Are you sure you want to clear the queue?')) {
                clearMutation.mutate();
              }
            }}
            loading={clearMutation.isPending}
          >
            Clear
          </Button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center h-64">
          <RefreshCw className="w-8 h-8 text-primary-500 animate-spin" />
        </div>
      ) : (
        <>
          {/* Stats Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <StatCard
              title="Queue Size"
              value={stats?.queue_size ?? 0}
              icon={<Layers />}
            />
            <StatCard
              title="Pending"
              value={stats?.pending_messages ?? 0}
            />
            <StatCard
              title="Processing"
              value={stats?.processing_messages ?? 0}
            />
            <StatCard
              title="Avg Time (ms)"
              value={stats?.avg_processing_time_ms?.toFixed(1) ?? '0'}
            />
          </div>

          {/* Pipeline Visualization */}
          <Card title="Processing Pipeline" description="Message flow through the system">
            <div className="flex items-center justify-between overflow-x-auto py-4">
              {[
                { name: 'Debounce', value: `${config?.debounce_ms ?? 0}ms` },
                { name: 'Dedupe', value: `${config?.dedupe_window_seconds ?? 0}s` },
                { name: 'Rate Limit', value: `${config?.rate_limits?.requests_per_minute ?? 0}/min` },
                { name: 'Concurrency', value: `${config?.concurrency_limits?.max_global ?? 0} max` },
                { name: 'Batching', value: config?.batching?.enabled ? 'Enabled' : 'Disabled' },
                { name: 'Retry', value: `${config?.retry_config?.max_retries ?? 0} attempts` },
              ].map((stage, index, arr) => (
                <div key={stage.name} className="flex items-center">
                  <div className="flex flex-col items-center">
                    <div className="w-24 h-24 bg-primary-50 dark:bg-primary-900/20 rounded-lg flex flex-col items-center justify-center">
                      <span className="text-sm font-medium text-slate-900 dark:text-white">
                        {stage.name}
                      </span>
                      <span className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                        {stage.value}
                      </span>
                    </div>
                  </div>
                  {index < arr.length - 1 && (
                    <div className="w-8 h-0.5 bg-slate-300 dark:bg-slate-600 mx-2" />
                  )}
                </div>
              ))}
            </div>
          </Card>

          {/* Configuration Cards */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Concurrency Limits */}
            <Card
              title="Concurrency Limits"
              action={
                <Button variant="ghost" size="sm" icon={<Settings className="w-4 h-4" />}>
                  Edit
                </Button>
              }
            >
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Per User</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.concurrency_limits?.max_per_user ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Per Channel</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.concurrency_limits?.max_per_channel ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Per Chat</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.concurrency_limits?.max_per_chat ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Global</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.concurrency_limits?.max_global ?? 0}
                  </span>
                </div>
              </div>
            </Card>

            {/* Rate Limits */}
            <Card
              title="Rate Limits"
              action={
                <Button variant="ghost" size="sm" icon={<Settings className="w-4 h-4" />}>
                  Edit
                </Button>
              }
            >
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Requests/min</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.rate_limits?.requests_per_minute ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Requests/hour</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.rate_limits?.requests_per_hour ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Burst Limit</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.rate_limits?.burst_limit ?? 0}
                  </span>
                </div>
              </div>
            </Card>

            {/* Retry Configuration */}
            <Card
              title="Retry Configuration"
              action={
                <Button variant="ghost" size="sm" icon={<Settings className="w-4 h-4" />}>
                  Edit
                </Button>
              }
            >
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Max Retries</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.retry_config?.max_retries ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Base Delay</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.retry_config?.base_delay_seconds ?? 0}s
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Max Delay</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.retry_config?.max_delay_seconds ?? 0}s
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Exponential Base</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.retry_config?.exponential_base ?? 0}
                  </span>
                </div>
              </div>
            </Card>

            {/* Batching */}
            <Card
              title="Batching"
              action={
                <Button variant="ghost" size="sm" icon={<Settings className="w-4 h-4" />}>
                  Edit
                </Button>
              }
            >
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Enabled</span>
                  <span
                    className={`px-2 py-1 rounded-full text-sm font-medium ${
                      config?.batching?.enabled
                        ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                        : 'bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300'
                    }`}
                  >
                    {config?.batching?.enabled ? 'Yes' : 'No'}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Max Batch Size</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.batching?.max_batch_size ?? 0}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-slate-600 dark:text-slate-300">Max Wait</span>
                  <span className="font-medium text-slate-900 dark:text-white">
                    {config?.batching?.max_wait_ms ?? 0}ms
                  </span>
                </div>
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
