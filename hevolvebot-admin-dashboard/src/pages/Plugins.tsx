import { useState } from 'react';
import { Puzzle, Search, Power, PowerOff, Settings, Trash2, Download, ExternalLink } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

const mockPlugins = [
  {
    id: 'analytics',
    name: 'Analytics',
    version: '1.2.0',
    description: 'Track message metrics and user engagement',
    enabled: true,
    hooks: ['on_message', 'on_response'],
  },
  {
    id: 'moderation',
    name: 'Content Moderation',
    version: '2.0.1',
    description: 'Automatically filter inappropriate content',
    enabled: true,
    hooks: ['on_message', 'before_response'],
  },
  {
    id: 'translation',
    name: 'Auto Translation',
    version: '1.0.0',
    description: 'Translate messages between languages',
    enabled: false,
    hooks: ['on_message', 'before_response'],
  },
  {
    id: 'reminder',
    name: 'Reminders',
    version: '1.1.0',
    description: 'Set and manage user reminders',
    enabled: true,
    hooks: ['on_command'],
  },
];

const availablePlugins = [
  {
    id: 'sentiment',
    name: 'Sentiment Analysis',
    version: '1.0.0',
    description: 'Analyze message sentiment and tone',
    downloads: 1250,
  },
  {
    id: 'polls',
    name: 'Polls & Surveys',
    version: '2.1.0',
    description: 'Create interactive polls and surveys',
    downloads: 890,
  },
];

export default function Plugins() {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeTab, setActiveTab] = useState<'installed' | 'available'>('installed');

  const filteredPlugins = mockPlugins.filter(
    (p) =>
      p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      p.description.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Plugins</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Extend functionality with plugins
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-4 border-b border-slate-200 dark:border-slate-700">
        <button
          onClick={() => setActiveTab('installed')}
          className={`pb-3 font-medium text-sm border-b-2 transition-colors ${
            activeTab === 'installed'
              ? 'border-primary-500 text-primary-600 dark:text-primary-400'
              : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
          }`}
        >
          Installed ({mockPlugins.length})
        </button>
        <button
          onClick={() => setActiveTab('available')}
          className={`pb-3 font-medium text-sm border-b-2 transition-colors ${
            activeTab === 'available'
              ? 'border-primary-500 text-primary-600 dark:text-primary-400'
              : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
          }`}
        >
          Available
        </button>
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-400" />
        <input
          type="text"
          placeholder="Search plugins..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full pl-10 pr-4 py-3 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent dark:text-white"
        />
      </div>

      {activeTab === 'installed' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {filteredPlugins.map((plugin) => (
            <Card key={plugin.id}>
              <div className="flex items-start gap-4">
                <div className="w-12 h-12 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                  <Puzzle className="w-6 h-6 text-primary-600 dark:text-primary-400" />
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="font-semibold text-slate-900 dark:text-white">{plugin.name}</h3>
                    <span className="text-xs text-slate-500 dark:text-slate-400">
                      v{plugin.version}
                    </span>
                  </div>
                  <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
                    {plugin.description}
                  </p>
                  <div className="flex flex-wrap gap-1 mt-2">
                    {plugin.hooks.map((hook) => (
                      <span
                        key={hook}
                        className="px-2 py-0.5 bg-slate-100 dark:bg-slate-700 rounded text-xs text-slate-600 dark:text-slate-300"
                      >
                        {hook}
                      </span>
                    ))}
                  </div>
                </div>
                <span
                  className={`px-2 py-1 rounded-full text-xs font-medium ${
                    plugin.enabled
                      ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                      : 'bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-400'
                  }`}
                >
                  {plugin.enabled ? 'Active' : 'Disabled'}
                </span>
              </div>

              <div className="flex items-center gap-2 mt-4 pt-4 border-t border-slate-200 dark:border-slate-700">
                <Button
                  variant={plugin.enabled ? 'secondary' : 'primary'}
                  size="sm"
                  className="flex-1"
                  icon={plugin.enabled ? <PowerOff className="w-4 h-4" /> : <Power className="w-4 h-4" />}
                >
                  {plugin.enabled ? 'Disable' : 'Enable'}
                </Button>
                <button className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg">
                  <Settings className="w-4 h-4 text-slate-500" />
                </button>
                <button className="p-2 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg">
                  <Trash2 className="w-4 h-4 text-slate-500 hover:text-red-500" />
                </button>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {availablePlugins.map((plugin) => (
            <Card key={plugin.id}>
              <div className="flex items-start gap-4">
                <div className="w-12 h-12 bg-slate-100 dark:bg-slate-700 rounded-lg flex items-center justify-center">
                  <Puzzle className="w-6 h-6 text-slate-500" />
                </div>
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="font-semibold text-slate-900 dark:text-white">{plugin.name}</h3>
                    <span className="text-xs text-slate-500 dark:text-slate-400">
                      v{plugin.version}
                    </span>
                  </div>
                  <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
                    {plugin.description}
                  </p>
                  <p className="text-xs text-slate-400 dark:text-slate-500 mt-2">
                    {plugin.downloads.toLocaleString()} downloads
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-2 mt-4 pt-4 border-t border-slate-200 dark:border-slate-700">
                <Button size="sm" className="flex-1" icon={<Download className="w-4 h-4" />}>
                  Install
                </Button>
                <button className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg">
                  <ExternalLink className="w-4 h-4 text-slate-500" />
                </button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
