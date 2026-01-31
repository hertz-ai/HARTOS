import { useQuery } from '@tanstack/react-query';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  AreaChart,
  Area,
} from 'recharts';
import { Activity, TrendingUp, AlertTriangle, Clock, RefreshCw } from 'lucide-react';
import Card from '../components/common/Card';
import StatCard from '../components/charts/StatCard';
import Button from '../components/common/Button';
import metricsApi from '../api/endpoints/metrics';

export default function Metrics() {
  const { data: metrics, refetch: refetchMetrics } = useQuery({
    queryKey: ['metrics'],
    queryFn: () => metricsApi.get(),
    refetchInterval: 5000,
  });

  const { data: latency } = useQuery({
    queryKey: ['latency'],
    queryFn: () => metricsApi.getLatencyMetrics(),
    refetchInterval: 10000,
  });

  const { data: errors } = useQuery({
    queryKey: ['errors'],
    queryFn: () => metricsApi.getErrorMetrics(),
    refetchInterval: 10000,
  });

  const { data: queueMetrics } = useQuery({
    queryKey: ['queue-metrics'],
    queryFn: () => metricsApi.getQueueMetrics(),
    refetchInterval: 5000,
  });

  // Mock historical data
  const throughputData = Array.from({ length: 60 }, (_, i) => ({
    time: `${i}s`,
    value: Math.floor(Math.random() * 50) + 10,
  }));

  const latencyData = Array.from({ length: 60 }, (_, i) => ({
    time: `${i}s`,
    p50: Math.floor(Math.random() * 30) + 20,
    p95: Math.floor(Math.random() * 50) + 50,
    p99: Math.floor(Math.random() * 80) + 80,
  }));

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Metrics</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Real-time system performance monitoring
          </p>
        </div>
        <Button variant="secondary" icon={<RefreshCw className="w-4 h-4" />} onClick={() => refetchMetrics()}>
          Refresh
        </Button>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <StatCard
          title="Total Messages"
          value={(metrics?.total_messages_processed ?? 0).toLocaleString()}
          icon={<Activity />}
        />
        <StatCard
          title="Messages/min"
          value={metrics?.messages_per_minute?.toFixed(1) ?? '0'}
          icon={<TrendingUp />}
        />
        <StatCard
          title="Error Rate"
          value={`${((metrics?.error_rate ?? 0) * 100).toFixed(2)}%`}
          icon={<AlertTriangle />}
        />
        <StatCard
          title="Avg Latency"
          value={`${metrics?.latency_p50_ms?.toFixed(0) ?? 0}ms`}
          icon={<Clock />}
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Throughput Chart */}
        <Card title="Throughput" description="Messages processed per second">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={throughputData}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                <XAxis
                  dataKey="time"
                  tick={{ fontSize: 12 }}
                  tickLine={false}
                  interval={9}
                />
                <YAxis tick={{ fontSize: 12 }} tickLine={false} axisLine={false} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'rgb(30 41 59)',
                    border: 'none',
                    borderRadius: '8px',
                    color: 'white',
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="value"
                  stroke="#0ea5e9"
                  fill="#0ea5e9"
                  fillOpacity={0.2}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>

        {/* Latency Chart */}
        <Card title="Latency Distribution" description="Response time percentiles">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={latencyData}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                <XAxis
                  dataKey="time"
                  tick={{ fontSize: 12 }}
                  tickLine={false}
                  interval={9}
                />
                <YAxis tick={{ fontSize: 12 }} tickLine={false} axisLine={false} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'rgb(30 41 59)',
                    border: 'none',
                    borderRadius: '8px',
                    color: 'white',
                  }}
                />
                <Line type="monotone" dataKey="p50" stroke="#22c55e" strokeWidth={2} dot={false} name="P50" />
                <Line type="monotone" dataKey="p95" stroke="#f59e0b" strokeWidth={2} dot={false} name="P95" />
                <Line type="monotone" dataKey="p99" stroke="#ef4444" strokeWidth={2} dot={false} name="P99" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      {/* Detailed Metrics */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Queue Metrics */}
        <Card title="Queue Performance">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Throughput/sec</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {queueMetrics?.throughput_per_second?.toFixed(1) ?? '-'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Avg Wait Time</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {queueMetrics?.avg_wait_time_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Avg Process Time</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {queueMetrics?.avg_processing_time_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Queue Depth</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {queueMetrics?.queue_depth ?? '-'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Rejected</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {queueMetrics?.rejected_count ?? '-'}
              </span>
            </div>
          </div>
        </Card>

        {/* Latency Percentiles */}
        <Card title="Latency Percentiles">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P50</span>
              <span className="font-medium text-green-600 dark:text-green-400">
                {latency?.p50_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P75</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {latency?.p75_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P90</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {latency?.p90_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P95</span>
              <span className="font-medium text-amber-600 dark:text-amber-400">
                {latency?.p95_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">P99</span>
              <span className="font-medium text-red-600 dark:text-red-400">
                {latency?.p99_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Max</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {latency?.max_ms?.toFixed(0) ?? '-'}ms
              </span>
            </div>
          </div>
        </Card>

        {/* Error Metrics */}
        <Card title="Error Summary">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Total Errors</span>
              <span className="font-medium text-slate-900 dark:text-white">
                {errors?.total_errors ?? '-'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600 dark:text-slate-300">Error Rate</span>
              <span className={`font-medium ${(errors?.error_rate ?? 0) > 0.01 ? 'text-red-600' : 'text-green-600'}`}>
                {((errors?.error_rate ?? 0) * 100).toFixed(3)}%
              </span>
            </div>
            <div className="pt-2 border-t border-slate-200 dark:border-slate-700">
              <p className="text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                Recent Errors
              </p>
              {errors?.recent_errors && errors.recent_errors.length > 0 ? (
                <div className="space-y-2">
                  {errors.recent_errors.slice(0, 3).map((err, i) => (
                    <div key={i} className="text-xs p-2 bg-red-50 dark:bg-red-900/20 rounded">
                      <p className="text-red-700 dark:text-red-400">{err.message}</p>
                      <p className="text-red-500 dark:text-red-500 mt-1">{err.timestamp}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-slate-500 dark:text-slate-400">No recent errors</p>
              )}
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
