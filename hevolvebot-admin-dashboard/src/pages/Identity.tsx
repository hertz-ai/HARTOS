import { useState } from 'react';
import { User, Upload, Save, Plus, Trash2, Link2 } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

export default function Identity() {
  const [identity, setIdentity] = useState({
    display_name: 'HevolveBot Assistant',
    bio: 'A helpful AI assistant for your messaging needs.',
    avatar_url: '',
  });

  const [avatars] = useState([
    { id: '1', name: 'Default', url: '', is_default: true },
    { id: '2', name: 'Telegram', url: '', channel: 'telegram', is_default: false },
    { id: '3', name: 'Discord', url: '', channel: 'discord', is_default: false },
  ]);

  const [senderMappings] = useState([
    { id: '1', platform_user_id: 'user123', internal_user_id: 'u_abc', channel: 'telegram', display_name: 'John' },
    { id: '2', platform_user_id: 'user456', internal_user_id: 'u_def', channel: 'discord', display_name: 'Jane' },
  ]);

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Identity</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Configure agent identity and appearance
          </p>
        </div>
        <Button icon={<Save className="w-4 h-4" />}>Save Changes</Button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Identity */}
        <div className="lg:col-span-2 space-y-6">
          <Card title="Agent Identity">
            <div className="flex gap-6">
              {/* Avatar Preview */}
              <div className="flex flex-col items-center">
                <div className="w-24 h-24 bg-primary-100 dark:bg-primary-900/30 rounded-full flex items-center justify-center">
                  {identity.avatar_url ? (
                    <img
                      src={identity.avatar_url}
                      alt="Avatar"
                      className="w-full h-full rounded-full object-cover"
                    />
                  ) : (
                    <User className="w-12 h-12 text-primary-500" />
                  )}
                </div>
                <Button variant="ghost" size="sm" className="mt-3" icon={<Upload className="w-4 h-4" />}>
                  Upload
                </Button>
              </div>

              {/* Identity Fields */}
              <div className="flex-1 space-y-4">
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                    Display Name
                  </label>
                  <input
                    type="text"
                    value={identity.display_name}
                    onChange={(e) => setIdentity({ ...identity, display_name: e.target.value })}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                    Bio
                  </label>
                  <textarea
                    value={identity.bio}
                    onChange={(e) => setIdentity({ ...identity, bio: e.target.value })}
                    rows={3}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white resize-none"
                  />
                </div>
              </div>
            </div>
          </Card>

          {/* Personality */}
          <Card title="Personality" description="Configure response style and behavior">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Tone
                </label>
                <select className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg dark:text-white">
                  <option>Professional</option>
                  <option>Friendly</option>
                  <option>Casual</option>
                  <option>Formal</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Verbosity
                </label>
                <select className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg dark:text-white">
                  <option>Concise</option>
                  <option>Moderate</option>
                  <option>Detailed</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Emoji Usage
                </label>
                <select className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg dark:text-white">
                  <option>None</option>
                  <option>Minimal</option>
                  <option>Moderate</option>
                  <option>Frequent</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Greeting Style
                </label>
                <select className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg dark:text-white">
                  <option>Hello</option>
                  <option>Hi there</option>
                  <option>Hey</option>
                  <option>Greetings</option>
                </select>
              </div>
            </div>
          </Card>

          {/* Sender Mappings */}
          <Card
            title="Sender Mappings"
            description="Map platform users to internal identities"
            action={<Button variant="ghost" size="sm" icon={<Plus className="w-4 h-4" />}>Add</Button>}
          >
            <div className="space-y-3">
              {senderMappings.map((mapping) => (
                <div
                  key={mapping.id}
                  className="flex items-center justify-between p-3 bg-slate-50 dark:bg-slate-700/50 rounded-lg"
                >
                  <div className="flex items-center gap-4">
                    <Link2 className="w-5 h-5 text-slate-400" />
                    <div>
                      <p className="font-medium text-slate-900 dark:text-white">
                        {mapping.display_name}
                      </p>
                      <p className="text-sm text-slate-500 dark:text-slate-400">
                        {mapping.channel}: {mapping.platform_user_id} &rarr; {mapping.internal_user_id}
                      </p>
                    </div>
                  </div>
                  <button className="p-2 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg">
                    <Trash2 className="w-4 h-4 text-slate-400 hover:text-red-500" />
                  </button>
                </div>
              ))}
            </div>
          </Card>
        </div>

        {/* Avatars */}
        <div>
          <Card
            title="Avatars"
            description="Per-channel avatar configuration"
            action={<Button variant="ghost" size="sm" icon={<Plus className="w-4 h-4" />}>Add</Button>}
          >
            <div className="space-y-4">
              {avatars.map((avatar) => (
                <div
                  key={avatar.id}
                  className="flex items-center gap-4 p-3 bg-slate-50 dark:bg-slate-700/50 rounded-lg"
                >
                  <div className="w-12 h-12 bg-primary-100 dark:bg-primary-900/30 rounded-full flex items-center justify-center">
                    {avatar.url ? (
                      <img
                        src={avatar.url}
                        alt={avatar.name}
                        className="w-full h-full rounded-full object-cover"
                      />
                    ) : (
                      <User className="w-6 h-6 text-primary-500" />
                    )}
                  </div>
                  <div className="flex-1">
                    <p className="font-medium text-slate-900 dark:text-white">{avatar.name}</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      {avatar.channel ? `For ${avatar.channel}` : 'Default avatar'}
                    </p>
                  </div>
                  {avatar.is_default && (
                    <span className="px-2 py-0.5 bg-primary-100 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400 rounded text-xs">
                      Default
                    </span>
                  )}
                  <button className="p-2 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg">
                    <Trash2 className="w-4 h-4 text-slate-400 hover:text-red-500" />
                  </button>
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
