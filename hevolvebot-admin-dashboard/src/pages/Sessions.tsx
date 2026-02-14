import { useState } from 'react';
import { Users, Search, Trash2, Eye, Link2, Unlink, RefreshCw } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

const mockSessions = [
  {
    session_id: 'sess_abc123',
    user_id: 'user_1',
    channel: 'telegram',
    chat_id: '-100123456',
    created_at: '2024-01-15 10:30:00',
    last_activity: '2024-01-15 14:45:00',
    message_count: 42,
  },
  {
    session_id: 'sess_def456',
    user_id: 'user_2',
    channel: 'discord',
    chat_id: '987654321',
    created_at: '2024-01-15 11:00:00',
    last_activity: '2024-01-15 14:30:00',
    message_count: 28,
  },
  {
    session_id: 'sess_ghi789',
    user_id: 'user_3',
    channel: 'slack',
    chat_id: 'C12345678',
    created_at: '2024-01-15 09:15:00',
    last_activity: '2024-01-15 13:00:00',
    message_count: 15,
  },
];

const mockPairings = [
  {
    id: 'pair_1',
    source: { channel: 'telegram', chat_id: '-100123456' },
    target: { channel: 'discord', chat_id: '987654321' },
    bidirectional: true,
  },
];

export default function Sessions() {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeTab, setActiveTab] = useState<'sessions' | 'pairings'>('sessions');
  const [selectedSession, setSelectedSession] = useState<string | null>(null);

  const filteredSessions = mockSessions.filter(
    (s) =>
      s.session_id.toLowerCase().includes(searchQuery.toLowerCase()) ||
      s.user_id.toLowerCase().includes(searchQuery.toLowerCase()) ||
      s.channel.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Sessions</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Manage active sessions and channel pairing
          </p>
        </div>
        <div className="flex gap-3">
          <Button variant="secondary" icon={<RefreshCw className="w-4 h-4" />}>
            Refresh
          </Button>
          {activeTab === 'pairings' && (
            <Button icon={<Link2 className="w-4 h-4" />}>Create Pairing</Button>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-4 border-b border-slate-200 dark:border-slate-700">
        <button
          onClick={() => setActiveTab('sessions')}
          className={`pb-3 font-medium text-sm border-b-2 transition-colors ${
            activeTab === 'sessions'
              ? 'border-primary-500 text-primary-600 dark:text-primary-400'
              : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
          }`}
        >
          Active Sessions ({mockSessions.length})
        </button>
        <button
          onClick={() => setActiveTab('pairings')}
          className={`pb-3 font-medium text-sm border-b-2 transition-colors ${
            activeTab === 'pairings'
              ? 'border-primary-500 text-primary-600 dark:text-primary-400'
              : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
          }`}
        >
          Channel Pairings ({mockPairings.length})
        </button>
      </div>

      {activeTab === 'sessions' && (
        <>
          {/* Search */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-400" />
            <input
              type="text"
              placeholder="Search sessions..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-3 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent dark:text-white"
            />
          </div>

          {/* Sessions Table */}
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                      Session
                    </th>
                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                      Channel
                    </th>
                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                      User
                    </th>
                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                      Messages
                    </th>
                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                      Last Activity
                    </th>
                    <th className="text-right py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {filteredSessions.map((session) => (
                    <tr
                      key={session.session_id}
                      className="border-b border-slate-100 dark:border-slate-700/50 hover:bg-slate-50 dark:hover:bg-slate-700/50"
                    >
                      <td className="py-4 px-4">
                        <div className="flex items-center gap-3">
                          <div className="w-8 h-8 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                            <Users className="w-4 h-4 text-primary-600 dark:text-primary-400" />
                          </div>
                          <span className="font-mono text-sm text-slate-900 dark:text-white">
                            {session.session_id}
                          </span>
                        </div>
                      </td>
                      <td className="py-4 px-4">
                        <span className="px-2 py-1 bg-slate-100 dark:bg-slate-700 rounded text-sm capitalize">
                          {session.channel}
                        </span>
                      </td>
                      <td className="py-4 px-4 text-slate-600 dark:text-slate-300">
                        {session.user_id}
                      </td>
                      <td className="py-4 px-4 text-slate-600 dark:text-slate-300">
                        {session.message_count}
                      </td>
                      <td className="py-4 px-4 text-slate-600 dark:text-slate-300">
                        {session.last_activity}
                      </td>
                      <td className="py-4 px-4">
                        <div className="flex items-center justify-end gap-2">
                          <button
                            onClick={() => setSelectedSession(session.session_id)}
                            className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
                          >
                            <Eye className="w-4 h-4" />
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
        </>
      )}

      {activeTab === 'pairings' && (
        <div className="space-y-4">
          {mockPairings.map((pairing) => (
            <Card key={pairing.id}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-6">
                  <div className="text-center">
                    <p className="text-sm text-slate-500 dark:text-slate-400 mb-1">Source</p>
                    <div className="px-4 py-2 bg-slate-100 dark:bg-slate-700 rounded-lg">
                      <p className="font-medium text-slate-900 dark:text-white capitalize">
                        {pairing.source.channel}
                      </p>
                      <p className="text-xs text-slate-500 dark:text-slate-400 font-mono">
                        {pairing.source.chat_id}
                      </p>
                    </div>
                  </div>

                  <div className="flex items-center gap-2">
                    {pairing.bidirectional ? (
                      <>
                        <div className="w-8 h-0.5 bg-primary-500" />
                        <Link2 className="w-5 h-5 text-primary-500" />
                        <div className="w-8 h-0.5 bg-primary-500" />
                      </>
                    ) : (
                      <>
                        <div className="w-16 h-0.5 bg-primary-500" />
                        <div className="w-0 h-0 border-l-8 border-l-primary-500 border-y-4 border-y-transparent" />
                      </>
                    )}
                  </div>

                  <div className="text-center">
                    <p className="text-sm text-slate-500 dark:text-slate-400 mb-1">Target</p>
                    <div className="px-4 py-2 bg-slate-100 dark:bg-slate-700 rounded-lg">
                      <p className="font-medium text-slate-900 dark:text-white capitalize">
                        {pairing.target.channel}
                      </p>
                      <p className="text-xs text-slate-500 dark:text-slate-400 font-mono">
                        {pairing.target.chat_id}
                      </p>
                    </div>
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <span
                    className={`px-2 py-1 rounded-full text-xs font-medium ${
                      pairing.bidirectional
                        ? 'bg-primary-100 dark:bg-primary-900/30 text-primary-700 dark:text-primary-400'
                        : 'bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300'
                    }`}
                  >
                    {pairing.bidirectional ? 'Bidirectional' : 'One-way'}
                  </span>
                  <button className="p-2 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg text-slate-500 hover:text-red-600">
                    <Unlink className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* Session Detail Modal would go here */}
      {selectedSession && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <Card className="w-full max-w-lg m-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-slate-900 dark:text-white">
                Session Details
              </h3>
              <button
                onClick={() => setSelectedSession(null)}
                className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg"
              >
                &times;
              </button>
            </div>
            <p className="text-slate-600 dark:text-slate-300">
              Session ID: {selectedSession}
            </p>
            <div className="mt-4 pt-4 border-t border-slate-200 dark:border-slate-700">
              <Button variant="danger" className="w-full">
                Terminate Session
              </Button>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
