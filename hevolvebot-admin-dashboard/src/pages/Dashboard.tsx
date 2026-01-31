import { useQuery } from '@tanstack/react-query';
import {
  MessageSquare,
  Users,
  Layers,
  Zap,
  Activity,
  Clock,
  AlertCircle,
} from 'lucide-react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  BarChart,
  Bar,
} from 'recharts';
import Card from '../components/common/Card';
import StatCard from '../components/charts/StatCard';
import metricsApi from '../api/endpoints/metrics';

export default function Dashboard() {
  const { data: metrics } = useQuery({
    queryKey: ['metrics'],
    queryFn: () => metricsApi.get(),
    refetchInterval: 5000,
  });

  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: () => metricsApi.status(),
    refetchInterval: 10000,
  });

  const { data: latency } = useQuery({
    queryKey: ['latency'],
    queryFn: () => metricsApi.getLatencyMetrics(),
    refetchInterval: 10000,
  });

  // Mock data for charts
  const messageData = Array.from({ length: 24 }, (_, i) => ({
    hour: `${i}:00`,
    messages: Math.floor(Math.random() * 100) + 20,
  }));

  const channelData = [
    { name: 'Telegram', messages: 450 },
    { name: 'Discord', messages: 320 },
    { name: 'Slack', messages: 180 },
    { name: 'WhatsApp', messages: 120 },
    { name: 'Matrix', messages: 80 },
  ];

  const formatUptime = (seconds?: number) => {
    if (!seconds) return '0h 0m';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${minutes}m`;
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Dashboard</h1>
        <p className="text-slate-500 dark:text-slate-400 mt-1">
          System overview and real-time metrics
        </p>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <StatCard
          title="Active Channels"
          value={status?.channels_count ?? 0}
          change={5}
          changeLabel="vs last week"
          icon={<MessageSquare />}
        />
        <StatCard
          title="Active Sessions"
          value={metrics?.active_sessions ?? 0}
          change={12}
          changeLabel="vs last hour"
          icon={<Users />}
        />
        <StatCard
          title="Queue Depth"
          value={metrics?.queue_depth ?? 0}
          change={-8}
          changeLabel="vs 5 min ago"
          icon={<Layers />}
        />
        <StatCard
          title="Messages/min"
          value={metrics?.messages_per_minute?.toFixed(1) ?? '0'}
          change={3}
          changeLabel="vs avg"
          icon={<Zap />}
        />
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Message Activity Chart */}
        <Card title="Message Activity" description="Messages per hour over last 24 hours">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={messageData}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                <XAxis
                  dataKey="hour"
                  tick={{ fontSize: 12 }}
                  className="text-slate-500"
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 12 }}
                  className="text-slate-500"
                  tickLine={false}
                  axisLine={false}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'rgb(30 41 59)',
                    border: 'none',
                    borderRadius: '8px',
                    color: 'white',
                  }}
                />
                <Line
                  type="monotone"
                  dataKey="messages"
                  stroke="#0ea5e9"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>

        {/* Channel Distribution Chart */}
        <Card title="Messages by Channel" description="Distribution across platforms">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={channelData}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: 12 }}
                  className="text-slate-500"
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 12 }}
                  className="text-slate-500"
                  tickLine={false}
                  axisLine={false}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'rgb(30 41 59)',
                    border: 'none',
                    borderRadius: '8px',
                    color: 'white',
                  }}
                />
                <Bar dataKey="messages" fill="#0ea5e9" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      {/* Bottom Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* System Status */}
        <Card title="System Status">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Activity className="w-5 h-5 text-slate-400" />
                <span className="text-slate-600 dark:text-slate-300">Status</span>
              </div>
              <span className="px-2.5 py-1 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded-full text-sm font-medium">
                {status?.status ?? 'Unknown'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <Clock className="w-5 h-5 text-slate-400" />
                <span className="text-slate-600 dark:text-slate-300">Uptime</span>
              </div>
              <span className="text-slate-900 dark:text-white font-medium">
                {formatUptime(metrics?.uptime_seconds)}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <AlertCircle className="w-5 h-5 text-slate-400" />
                <span className="text-slate-600 dark:text-slate-300">Error Rate</span>
              </div>
              <span className="text-slate-900 dark:text-white font-medium">
                {((metrics?.error_rate ?? 0) * 100).toFixed(2)}%
              </span>
            </div>
          </div>
        </Card>

        {/* Latency Metrics */}
        <Card title="Latency (ms)">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P50</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {latency?.p50_ms?.toFixed(1) ?? '-'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P95</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {latency?.p95_ms?.toFixed(1) ?? '-'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P99</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {latency?.p99_ms?.toFixed(1) ?? '-'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Max</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {latency?.max_ms?.toFixed(1) ?? '-'}
              </span>
            </div>
          </div>
        </Card>

        {/* Quick Stats */}
        <Card title="Component Counts">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Commands</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {status?.commands_count ?? 0}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Plugins</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {status?.plugins_count ?? 0}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Webhooks</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {status?.webhooks_count ?? 0}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Workflows</span>
              <span className="text-slate-900 dark:text-white font-medium">
                {status?.workflows_count ?? 0}
              </span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
