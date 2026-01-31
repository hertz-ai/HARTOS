import { useState } from 'react';
import { Terminal, Plus, Search, Power, PowerOff, Trash2, Edit } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

// Mock data for demonstration
const mockCommands = [
  {
    name: 'help',
    description: 'Show available commands',
    pattern: '/help',
    enabled: true,
    admin_only: false,
    cooldown_seconds: 0,
    aliases: ['?', 'commands'],
  },
  {
    name: 'status',
    description: 'Show bot status',
    pattern: '/status',
    enabled: true,
    admin_only: true,
    cooldown_seconds: 5,
    aliases: ['info'],
  },
  {
    name: 'settings',
    description: 'Configure user settings',
    pattern: '/settings',
    enabled: true,
    admin_only: false,
    cooldown_seconds: 10,
    aliases: ['config', 'prefs'],
  },
  {
    name: 'clear',
    description: 'Clear conversation context',
    pattern: '/clear',
    enabled: false,
    admin_only: false,
    cooldown_seconds: 30,
    aliases: ['reset'],
  },
];

export default function Commands() {
  const [searchQuery, setSearchQuery] = useState('');
  const [commands] = useState(mockCommands);

  const filteredCommands = commands.filter(
    (cmd) =>
      cmd.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      cmd.description.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Commands</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Manage bot commands and triggers
          </p>
        </div>
        <Button icon={<Plus className="w-4 h-4" />}>Add Command</Button>
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-400" />
        <input
          type="text"
          placeholder="Search commands..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full pl-10 pr-4 py-3 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent dark:text-white"
        />
      </div>

      {/* Commands Table */}
      <Card>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-200 dark:border-slate-700">
                <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                  Command
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                  Description
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                  Aliases
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                  Cooldown
                </th>
                <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                  Status
                </th>
                <th className="text-right py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {filteredCommands.map((cmd) => (
                <tr
                  key={cmd.name}
                  className="border-b border-slate-100 dark:border-slate-700/50 hover:bg-slate-50 dark:hover:bg-slate-700/50"
                >
                  <td className="py-4 px-4">
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                        <Terminal className="w-4 h-4 text-primary-600 dark:text-primary-400" />
                      </div>
                      <div>
                        <p className="font-medium text-slate-900 dark:text-white">
                          {cmd.pattern}
                        </p>
                        {cmd.admin_only && (
                          <span className="text-xs text-amber-600 dark:text-amber-400">
                            Admin only
                          </span>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="py-4 px-4 text-slate-600 dark:text-slate-300">
                    {cmd.description}
                  </td>
                  <td className="py-4 px-4">
                    <div className="flex flex-wrap gap-1">
                      {cmd.aliases.map((alias) => (
                        <span
                          key={alias}
                          className="px-2 py-0.5 bg-slate-100 dark:bg-slate-700 rounded text-xs text-slate-600 dark:text-slate-300"
                        >
                          /{alias}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="py-4 px-4 text-slate-600 dark:text-slate-300">
                    {cmd.cooldown_seconds > 0 ? `${cmd.cooldown_seconds}s` : '-'}
                  </td>
                  <td className="py-4 px-4">
                    <span
                      className={`px-2 py-1 rounded-full text-xs font-medium ${
                        cmd.enabled
                          ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                          : 'bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-400'
                      }`}
                    >
                      {cmd.enabled ? 'Enabled' : 'Disabled'}
                    </span>
                  </td>
                  <td className="py-4 px-4">
                    <div className="flex items-center justify-end gap-2">
                      <button className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
                        <Edit className="w-4 h-4" />
                      </button>
                      <button className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
                        {cmd.enabled ? (
                          <PowerOff className="w-4 h-4" />
                        ) : (
                          <Power className="w-4 h-4" />
                        )}
                      </button>
                      <button className="p-2 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg text-slate-500 hover:text-red-600">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Mention Gating */}
      <Card
        title="Mention Gating"
        description="Control when the bot responds based on mentions"
      >
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="font-medium text-slate-900 dark:text-white">
                Require mention in groups
              </p>
              <p className="text-sm text-slate-500 dark:text-slate-400">
                Bot only responds when mentioned in group chats
              </p>
            </div>
            <button className="relative w-12 h-6 bg-primary-500 rounded-full transition-colors">
              <span className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
            </button>
          </div>
          <div className="flex items-center justify-between">
            <div>
              <p className="font-medium text-slate-900 dark:text-white">
                Allow reply chain
              </p>
              <p className="text-sm text-slate-500 dark:text-slate-400">
                Continue conversation when user replies to bot's message
              </p>
            </div>
            <button className="relative w-12 h-6 bg-primary-500 rounded-full transition-colors">
              <span className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
            </button>
          </div>
        </div>
      </Card>
    </div>
  );
}
