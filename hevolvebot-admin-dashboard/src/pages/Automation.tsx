import { useState } from 'react';
import { Webhook, Clock, Zap, Calendar, Plus, Trash2, Edit, Play, Power } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

type TabType = 'webhooks' | 'cron' | 'triggers' | 'scheduled';

const tabs: { id: TabType; label: string; icon: React.ElementType }[] = [
  { id: 'webhooks', label: 'Webhooks', icon: Webhook },
  { id: 'cron', label: 'Cron Jobs', icon: Clock },
  { id: 'triggers', label: 'Triggers', icon: Zap },
  { id: 'scheduled', label: 'Scheduled', icon: Calendar },
];

const mockWebhooks = [
  { id: '1', name: 'Slack Notification', url: 'https://hooks.slack.com/...', events: ['message', 'error'], enabled: true },
  { id: '2', name: 'Analytics', url: 'https://api.analytics.com/...', events: ['message'], enabled: true },
];

const mockCronJobs = [
  { id: '1', name: 'Daily Report', schedule: '0 9 * * *', handler: 'send_daily_report', enabled: true, last_run: '2024-01-15 09:00' },
  { id: '2', name: 'Cleanup', schedule: '0 0 * * 0', handler: 'cleanup_old_sessions', enabled: false, last_run: '2024-01-14 00:00' },
];

const mockTriggers = [
  { id: '1', name: 'Welcome Message', trigger_type: 'join', pattern: null, enabled: true, priority: 10 },
  { id: '2', name: 'Keyword Alert', trigger_type: 'message', pattern: 'urgent|help', enabled: true, priority: 5 },
];

const mockScheduled = [
  { id: '1', channel: 'telegram', chat_id: '-100123456', message: 'Good morning!', scheduled_time: '2024-01-16 08:00', recurring: true },
  { id: '2', channel: 'discord', chat_id: '987654321', message: 'Reminder: Weekly meeting', scheduled_time: '2024-01-17 14:00', recurring: false },
];

export default function Automation() {
  const [activeTab, setActiveTab] = useState<TabType>('webhooks');

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Automation</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Configure webhooks, scheduled tasks, and triggers
          </p>
        </div>
        <Button icon={<Plus className="w-4 h-4" />}>
          Add {tabs.find((t) => t.id === activeTab)?.label.slice(0, -1)}
        </Button>
      </div>

      {/* Tabs */}
      <div className="flex gap-2 border-b border-slate-200 dark:border-slate-700">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-3 font-medium text-sm border-b-2 transition-colors ${
              activeTab === tab.id
                ? 'border-primary-500 text-primary-600 dark:text-primary-400'
                : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      {activeTab === 'webhooks' && (
        <Card>
          <div className="space-y-4">
            {mockWebhooks.map((webhook) => (
              <div
                key={webhook.id}
                className="flex items-center justify-between p-4 bg-slate-50 dark:bg-slate-700/50 rounded-lg"
              >
                <div className="flex items-center gap-4">
                  <div className="w-10 h-10 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                    <Webhook className="w-5 h-5 text-primary-600 dark:text-primary-400" />
                  </div>
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">{webhook.name}</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400 truncate max-w-md">
                      {webhook.url}
                    </p>
                    <div className="flex gap-1 mt-1">
                      {webhook.events.map((event) => (
                        <span
                          key={event}
                          className="px-2 py-0.5 bg-slate-200 dark:bg-slate-600 rounded text-xs"
                        >
                          {event}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    className={`px-2 py-1 rounded-full text-xs font-medium ${
                      webhook.enabled
                        ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                        : 'bg-slate-100 dark:bg-slate-600 text-slate-600 dark:text-slate-300'
                    }`}
                  >
                    {webhook.enabled ? 'Active' : 'Disabled'}
                  </span>
                  <button className="p-2 hover:bg-slate-200 dark:hover:bg-slate-600 rounded-lg">
                    <Edit className="w-4 h-4 text-slate-500" />
                  </button>
                  <button className="p-2 hover:bg-red-100 dark:hover:bg-red-900/20 rounded-lg">
                    <Trash2 className="w-4 h-4 text-slate-500 hover:text-red-500" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {activeTab === 'cron' && (
        <Card>
          <div className="space-y-4">
            {mockCronJobs.map((job) => (
              <div
                key={job.id}
                className="flex items-center justify-between p-4 bg-slate-50 dark:bg-slate-700/50 rounded-lg"
              >
                <div className="flex items-center gap-4">
                  <div className="w-10 h-10 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                    <Clock className="w-5 h-5 text-primary-600 dark:text-primary-400" />
                  </div>
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">{job.name}</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400 font-mono">
                      {job.schedule}
                    </p>
                    <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
                      Last run: {job.last_run}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    className={`px-2 py-1 rounded-full text-xs font-medium ${
                      job.enabled
                        ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                        : 'bg-slate-100 dark:bg-slate-600 text-slate-600 dark:text-slate-300'
                    }`}
                  >
                    {job.enabled ? 'Active' : 'Disabled'}
                  </span>
                  <button className="p-2 hover:bg-slate-200 dark:hover:bg-slate-600 rounded-lg">
                    <Play className="w-4 h-4 text-slate-500" />
                  </button>
                  <button className="p-2 hover:bg-slate-200 dark:hover:bg-slate-600 rounded-lg">
                    <Edit className="w-4 h-4 text-slate-500" />
                  </button>
                  <button className="p-2 hover:bg-red-100 dark:hover:bg-red-900/20 rounded-lg">
                    <Trash2 className="w-4 h-4 text-slate-500 hover:text-red-500" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {activeTab === 'triggers' && (
        <Card>
          <div className="space-y-4">
            {mockTriggers.map((trigger) => (
              <div
                key={trigger.id}
                className="flex items-center justify-between p-4 bg-slate-50 dark:bg-slate-700/50 rounded-lg"
              >
                <div className="flex items-center gap-4">
                  <div className="w-10 h-10 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                    <Zap className="w-5 h-5 text-primary-600 dark:text-primary-400" />
                  </div>
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">{trigger.name}</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      Type: {trigger.trigger_type}
                      {trigger.pattern && (
                        <span className="ml-2 font-mono">Pattern: {trigger.pattern}</span>
                      )}
                    </p>
                    <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
                      Priority: {trigger.priority}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    className={`px-2 py-1 rounded-full text-xs font-medium ${
                      trigger.enabled
                        ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                        : 'bg-slate-100 dark:bg-slate-600 text-slate-600 dark:text-slate-300'
                    }`}
                  >
                    {trigger.enabled ? 'Active' : 'Disabled'}
                  </span>
                  <button className="p-2 hover:bg-slate-200 dark:hover:bg-slate-600 rounded-lg">
                    <Power className="w-4 h-4 text-slate-500" />
                  </button>
                  <button className="p-2 hover:bg-slate-200 dark:hover:bg-slate-600 rounded-lg">
                    <Edit className="w-4 h-4 text-slate-500" />
                  </button>
                  <button className="p-2 hover:bg-red-100 dark:hover:bg-red-900/20 rounded-lg">
                    <Trash2 className="w-4 h-4 text-slate-500 hover:text-red-500" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {activeTab === 'scheduled' && (
        <Card>
          <div className="space-y-4">
            {mockScheduled.map((msg) => (
              <div
                key={msg.id}
                className="flex items-center justify-between p-4 bg-slate-50 dark:bg-slate-700/50 rounded-lg"
              >
                <div className="flex items-center gap-4">
                  <div className="w-10 h-10 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                    <Calendar className="w-5 h-5 text-primary-600 dark:text-primary-400" />
                  </div>
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white truncate max-w-md">
                      {msg.message}
                    </p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      {msg.channel} / {msg.chat_id}
                    </p>
                    <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
                      {msg.scheduled_time} {msg.recurring && '(recurring)'}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button className="p-2 hover:bg-slate-200 dark:hover:bg-slate-600 rounded-lg">
                    <Edit className="w-4 h-4 text-slate-500" />
                  </button>
                  <button className="p-2 hover:bg-red-100 dark:hover:bg-red-900/20 rounded-lg">
                    <Trash2 className="w-4 h-4 text-slate-500 hover:text-red-500" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
