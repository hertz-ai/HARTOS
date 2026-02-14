import { useState } from 'react';
import {
  ArrowRightLeft,
  Plus,
  Trash2,
  Play,
  Pause,
  Settings,
  Filter,
  Zap,
  Link2,
  RefreshCw
} from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

interface BridgeRule {
  id: string;
  name: string;
  source_channel: string;
  source_chat_id?: string;
  target_channel: string;
  target_chat_id?: string;
  route_type: 'forward' | 'mirror' | 'broadcast' | 'filter';
  enabled: boolean;
  trigger_count: number;
  last_triggered?: string;
}

const mockRules: BridgeRule[] = [
  {
    id: '1',
    name: 'Telegram to Discord',
    source_channel: 'telegram',
    target_channel: 'discord',
    target_chat_id: '123456789',
    route_type: 'forward',
    enabled: true,
    trigger_count: 156,
    last_triggered: '2 min ago',
  },
  {
    id: '2',
    name: 'Slack Mirror',
    source_channel: 'slack',
    target_channel: 'teams',
    route_type: 'mirror',
    enabled: true,
    trigger_count: 89,
    last_triggered: '5 min ago',
  },
  {
    id: '3',
    name: 'Support Broadcast',
    source_channel: 'whatsapp',
    target_channel: '*',
    route_type: 'broadcast',
    enabled: false,
    trigger_count: 12,
  },
];

const channels = [
  'telegram', 'discord', 'slack', 'whatsapp', 'teams',
  'matrix', 'signal', 'line', 'viber', 'email'
];

export default function Bridge() {
  const [rules, setRules] = useState<BridgeRule[]>(mockRules);
  const [showAddModal, setShowAddModal] = useState(false);
  const [editingRule, setEditingRule] = useState<BridgeRule | null>(null);

  const toggleRule = (id: string) => {
    setRules(rules.map(r =>
      r.id === id ? { ...r, enabled: !r.enabled } : r
    ));
  };

  const deleteRule = (id: string) => {
    setRules(rules.filter(r => r.id !== id));
  };

  const getRouteTypeIcon = (type: string) => {
    switch (type) {
      case 'forward': return <ArrowRightLeft className="w-4 h-4" />;
      case 'mirror': return <RefreshCw className="w-4 h-4" />;
      case 'broadcast': return <Zap className="w-4 h-4" />;
      case 'filter': return <Filter className="w-4 h-4" />;
      default: return <Link2 className="w-4 h-4" />;
    }
  };

  const getRouteTypeColor = (type: string) => {
    switch (type) {
      case 'forward': return 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400';
      case 'mirror': return 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400';
      case 'broadcast': return 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400';
      case 'filter': return 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400';
      default: return 'bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-300';
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Channel Bridge</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Route messages between channels via WAMP Crossbar
          </p>
        </div>
        <Button icon={<Plus className="w-4 h-4" />} onClick={() => setShowAddModal(true)}>
          Add Rule
        </Button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <Card>
          <div className="text-center">
            <p className="text-3xl font-bold text-slate-900 dark:text-white">{rules.length}</p>
            <p className="text-sm text-slate-500 dark:text-slate-400">Total Rules</p>
          </div>
        </Card>
        <Card>
          <div className="text-center">
            <p className="text-3xl font-bold text-green-600">{rules.filter(r => r.enabled).length}</p>
            <p className="text-sm text-slate-500 dark:text-slate-400">Active</p>
          </div>
        </Card>
        <Card>
          <div className="text-center">
            <p className="text-3xl font-bold text-blue-600">
              {rules.reduce((sum, r) => sum + r.trigger_count, 0)}
            </p>
            <p className="text-sm text-slate-500 dark:text-slate-400">Total Forwards</p>
          </div>
        </Card>
        <Card>
          <div className="text-center">
            <p className="text-3xl font-bold text-purple-600">{channels.length}</p>
            <p className="text-sm text-slate-500 dark:text-slate-400">Connected Channels</p>
          </div>
        </Card>
      </div>

      {/* WAMP Connection Status */}
      <Card title="Crossbar Connection" description="WAMP pub/sub status">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 bg-green-500 rounded-full animate-pulse" />
              <span className="text-sm font-medium text-slate-700 dark:text-slate-300">Connected</span>
            </div>
            <span className="text-sm text-slate-500 dark:text-slate-400">
              ws://crossbar:8088/ws • realm1
            </span>
          </div>
          <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
            <span>Topics:</span>
            <code className="bg-slate-100 dark:bg-slate-700 px-2 py-1 rounded text-xs">
              com.hertzai.hevolve.channel.*
            </code>
            <code className="bg-slate-100 dark:bg-slate-700 px-2 py-1 rounded text-xs">
              com.hertzai.hevolve.bridge.*
            </code>
          </div>
        </div>
      </Card>

      {/* Rules List */}
      <Card title="Bridge Rules" description="Message routing rules between channels">
        <div className="space-y-4">
          {rules.map((rule) => (
            <div
              key={rule.id}
              className={`flex items-center justify-between p-4 rounded-lg border ${
                rule.enabled
                  ? 'bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700'
                  : 'bg-slate-50 dark:bg-slate-800/50 border-slate-200 dark:border-slate-700 opacity-60'
              }`}
            >
              <div className="flex items-center gap-4">
                {/* Route Type Badge */}
                <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${getRouteTypeColor(rule.route_type)}`}>
                  {getRouteTypeIcon(rule.route_type)}
                  <span className="capitalize">{rule.route_type}</span>
                </div>

                {/* Rule Info */}
                <div>
                  <h4 className="font-medium text-slate-900 dark:text-white">{rule.name}</h4>
                  <div className="flex items-center gap-2 mt-1 text-sm text-slate-500 dark:text-slate-400">
                    <span className="capitalize">{rule.source_channel}</span>
                    <ArrowRightLeft className="w-3 h-3" />
                    <span className="capitalize">
                      {rule.target_channel === '*' ? 'All Channels' : rule.target_channel}
                    </span>
                    {rule.target_chat_id && (
                      <span className="text-xs bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 rounded">
                        #{rule.target_chat_id}
                      </span>
                    )}
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-6">
                {/* Stats */}
                <div className="text-right">
                  <p className="text-sm font-medium text-slate-700 dark:text-slate-300">
                    {rule.trigger_count} forwards
                  </p>
                  {rule.last_triggered && (
                    <p className="text-xs text-slate-500 dark:text-slate-400">
                      Last: {rule.last_triggered}
                    </p>
                  )}
                </div>

                {/* Actions */}
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => toggleRule(rule.id)}
                    className={`p-2 rounded-lg transition-colors ${
                      rule.enabled
                        ? 'bg-green-100 text-green-600 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400'
                        : 'bg-slate-100 text-slate-400 hover:bg-slate-200 dark:bg-slate-700 dark:text-slate-500'
                    }`}
                  >
                    {rule.enabled ? <Play className="w-4 h-4" /> : <Pause className="w-4 h-4" />}
                  </button>
                  <button
                    onClick={() => setEditingRule(rule)}
                    className="p-2 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-700 dark:text-slate-400"
                  >
                    <Settings className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => deleteRule(rule.id)}
                    className="p-2 rounded-lg bg-red-100 text-red-600 hover:bg-red-200 dark:bg-red-900/30 dark:text-red-400"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </div>
          ))}

          {rules.length === 0 && (
            <div className="text-center py-12 text-slate-500 dark:text-slate-400">
              <ArrowRightLeft className="w-12 h-12 mx-auto mb-4 opacity-50" />
              <p>No bridge rules configured</p>
              <p className="text-sm mt-1">Add a rule to start routing messages between channels</p>
            </div>
          )}
        </div>
      </Card>

      {/* Add Rule Modal would go here */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-slate-800 rounded-xl p-6 w-full max-w-md mx-4">
            <h3 className="text-lg font-semibold text-slate-900 dark:text-white mb-4">
              Add Bridge Rule
            </h3>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Rule Name
                </label>
                <input
                  type="text"
                  placeholder="e.g., Telegram to Discord"
                  className="w-full px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-slate-900 dark:text-white"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                    Source Channel
                  </label>
                  <select className="w-full px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-slate-900 dark:text-white">
                    {channels.map(ch => (
                      <option key={ch} value={ch} className="capitalize">{ch}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                    Target Channel
                  </label>
                  <select className="w-full px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-slate-900 dark:text-white">
                    {channels.map(ch => (
                      <option key={ch} value={ch} className="capitalize">{ch}</option>
                    ))}
                    <option value="*">All Channels (Broadcast)</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Route Type
                </label>
                <select className="w-full px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-slate-900 dark:text-white">
                  <option value="forward">Forward - One-way routing</option>
                  <option value="mirror">Mirror - Two-way sync</option>
                  <option value="broadcast">Broadcast - To all channels</option>
                  <option value="filter">Filter - With conditions</option>
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Target Chat ID (optional)
                </label>
                <input
                  type="text"
                  placeholder="Channel/group ID"
                  className="w-full px-3 py-2 border border-slate-200 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-700 text-slate-900 dark:text-white"
                />
              </div>
            </div>

            <div className="flex justify-end gap-3 mt-6">
              <Button variant="secondary" onClick={() => setShowAddModal(false)}>
                Cancel
              </Button>
              <Button onClick={() => setShowAddModal(false)}>
                Create Rule
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
