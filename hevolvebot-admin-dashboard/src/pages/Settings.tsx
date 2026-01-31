import { useState } from 'react';
import { Settings as SettingsIcon, Shield, Image, MessageSquare, Database, Download, Upload, RotateCcw, Save } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

type TabType = 'security' | 'media' | 'response' | 'memory';

const tabs: { id: TabType; label: string; icon: React.ElementType }[] = [
  { id: 'security', label: 'Security', icon: Shield },
  { id: 'media', label: 'Media', icon: Image },
  { id: 'response', label: 'Response', icon: MessageSquare },
  { id: 'memory', label: 'Memory', icon: Database },
];

export default function Settings() {
  const [activeTab, setActiveTab] = useState<TabType>('security');
  const [hasChanges, setHasChanges] = useState(false);

  const handleChange = () => {
    setHasChanges(true);
  };

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Settings</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Global system configuration
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button variant="secondary" icon={<Download className="w-4 h-4" />}>
            Export
          </Button>
          <Button variant="secondary" icon={<Upload className="w-4 h-4" />}>
            Import
          </Button>
          <Button
            icon={<Save className="w-4 h-4" />}
            disabled={!hasChanges}
          >
            Save Changes
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Tabs Sidebar */}
        <div className="lg:col-span-1">
          <Card>
            <nav className="space-y-1">
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex items-center gap-3 w-full px-3 py-2.5 rounded-lg transition-colors ${
                    activeTab === tab.id
                      ? 'bg-primary-50 dark:bg-primary-900/20 text-primary-600 dark:text-primary-400'
                      : 'text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700'
                  }`}
                >
                  <tab.icon className="w-5 h-5" />
                  <span className="font-medium">{tab.label}</span>
                </button>
              ))}
            </nav>

            <div className="mt-6 pt-6 border-t border-slate-200 dark:border-slate-700">
              <Button
                variant="danger"
                className="w-full"
                icon={<RotateCcw className="w-4 h-4" />}
              >
                Reset to Defaults
              </Button>
            </div>
          </Card>
        </div>

        {/* Settings Content */}
        <div className="lg:col-span-3">
          {activeTab === 'security' && (
            <Card title="Security Settings" description="Access control and authentication">
              <div className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Allowed Users (one per line)
                  </label>
                  <textarea
                    rows={4}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white resize-none font-mono text-sm"
                    placeholder="user_id_1&#10;user_id_2"
                    onChange={handleChange}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Blocked Users (one per line)
                  </label>
                  <textarea
                    rows={4}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white resize-none font-mono text-sm"
                    placeholder="blocked_user_1&#10;blocked_user_2"
                    onChange={handleChange}
                  />
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                      Rate Limit per User (req/min)
                    </label>
                    <input
                      type="number"
                      defaultValue={60}
                      className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                      onChange={handleChange}
                    />
                  </div>
                </div>

                <div className="flex items-center justify-between py-3">
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">Require Authentication</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      Require users to authenticate before using the bot
                    </p>
                  </div>
                  <button className="relative w-12 h-6 bg-slate-200 dark:bg-slate-600 rounded-full transition-colors">
                    <span className="absolute left-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>

                <div className="flex items-center justify-between py-3">
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">Enable Encryption</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      Encrypt stored messages and session data
                    </p>
                  </div>
                  <button className="relative w-12 h-6 bg-slate-200 dark:bg-slate-600 rounded-full transition-colors">
                    <span className="absolute left-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>
              </div>
            </Card>
          )}

          {activeTab === 'media' && (
            <Card title="Media Settings" description="File handling and processing">
              <div className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Max File Size (MB)
                  </label>
                  <input
                    type="number"
                    defaultValue={25}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                    onChange={handleChange}
                  />
                </div>

                <div className="space-y-3">
                  {[
                    { key: 'vision', label: 'Vision (Image Analysis)', enabled: true },
                    { key: 'audio', label: 'Audio Transcription', enabled: true },
                    { key: 'tts', label: 'Text-to-Speech', enabled: false },
                    { key: 'image_gen', label: 'Image Generation', enabled: false },
                    { key: 'link_preview', label: 'Link Previews', enabled: true },
                  ].map((feature) => (
                    <div key={feature.key} className="flex items-center justify-between py-3">
                      <p className="font-medium text-slate-900 dark:text-white">{feature.label}</p>
                      <button
                        className={`relative w-12 h-6 ${feature.enabled ? 'bg-primary-500' : 'bg-slate-200 dark:bg-slate-600'} rounded-full transition-colors`}
                      >
                        <span
                          className={`absolute ${feature.enabled ? 'right-1' : 'left-1'} top-1 w-4 h-4 bg-white rounded-full`}
                        />
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            </Card>
          )}

          {activeTab === 'response' && (
            <Card title="Response Settings" description="Message formatting and behavior">
              <div className="space-y-6">
                <div className="flex items-center justify-between py-3">
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">Typing Indicator</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      Show typing indicator while processing
                    </p>
                  </div>
                  <button className="relative w-12 h-6 bg-primary-500 rounded-full transition-colors">
                    <span className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Typing Delay (ms per character)
                  </label>
                  <input
                    type="number"
                    defaultValue={50}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                    onChange={handleChange}
                  />
                </div>

                <div className="flex items-center justify-between py-3">
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">Enable Reactions</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      Allow the bot to react to messages
                    </p>
                  </div>
                  <button className="relative w-12 h-6 bg-primary-500 rounded-full transition-colors">
                    <span className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Max Response Length
                  </label>
                  <input
                    type="number"
                    defaultValue={4096}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                    onChange={handleChange}
                  />
                </div>

                <div className="flex items-center justify-between py-3">
                  <div>
                    <p className="font-medium text-slate-900 dark:text-white">Split Long Messages</p>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                      Split messages that exceed platform limits
                    </p>
                  </div>
                  <button className="relative w-12 h-6 bg-primary-500 rounded-full transition-colors">
                    <span className="absolute right-1 top-1 w-4 h-4 bg-white rounded-full" />
                  </button>
                </div>
              </div>
            </Card>
          )}

          {activeTab === 'memory' && (
            <Card title="Memory Settings" description="Storage and persistence">
              <div className="space-y-6">
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Storage Backend
                  </label>
                  <select
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                    defaultValue="file"
                    onChange={handleChange}
                  >
                    <option value="file">File System</option>
                    <option value="redis">Redis</option>
                    <option value="sqlite">SQLite</option>
                  </select>
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Max Entries
                  </label>
                  <input
                    type="number"
                    defaultValue={10000}
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                    onChange={handleChange}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    TTL (seconds, leave empty for no expiry)
                  </label>
                  <input
                    type="number"
                    placeholder="No expiry"
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white"
                    onChange={handleChange}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Storage Path (for file backend)
                  </label>
                  <input
                    type="text"
                    defaultValue="./agent_data"
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white font-mono"
                    onChange={handleChange}
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    Connection String (for Redis)
                  </label>
                  <input
                    type="text"
                    placeholder="redis://localhost:6379"
                    className="w-full px-4 py-2 bg-white dark:bg-slate-700 border border-slate-200 dark:border-slate-600 rounded-lg focus:ring-2 focus:ring-primary-500 dark:text-white font-mono"
                    onChange={handleChange}
                  />
                </div>
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
