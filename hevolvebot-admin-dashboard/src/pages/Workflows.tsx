import { useState } from 'react';
import { GitBranch, Plus, Play, Save, Trash2, Settings } from 'lucide-react';
import Card from '../components/common/Card';
import Button from '../components/common/Button';

const mockWorkflows = [
  {
    id: '1',
    name: 'Welcome Flow',
    description: 'Greet new users and collect preferences',
    enabled: true,
    nodes: 5,
    updated_at: '2024-01-15 14:30',
  },
  {
    id: '2',
    name: 'Support Ticket',
    description: 'Handle support requests and escalate if needed',
    enabled: true,
    nodes: 8,
    updated_at: '2024-01-14 10:15',
  },
  {
    id: '3',
    name: 'Feedback Collection',
    description: 'Collect and process user feedback',
    enabled: false,
    nodes: 4,
    updated_at: '2024-01-10 09:00',
  },
];

export default function Workflows() {
  const [selectedWorkflow, setSelectedWorkflow] = useState<string | null>(null);

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Workflows</h1>
          <p className="text-slate-500 dark:text-slate-400 mt-1">
            Visual workflow automation builder
          </p>
        </div>
        <Button icon={<Plus className="w-4 h-4" />}>Create Workflow</Button>
      </div>

      {selectedWorkflow ? (
        // Workflow Editor
        <div className="space-y-6">
          <div className="flex items-center justify-between">
            <Button variant="ghost" onClick={() => setSelectedWorkflow(null)}>
              &larr; Back to list
            </Button>
            <div className="flex items-center gap-2">
              <Button variant="secondary" icon={<Play className="w-4 h-4" />}>
                Test
              </Button>
              <Button icon={<Save className="w-4 h-4" />}>Save</Button>
            </div>
          </div>

          <Card>
            <div className="h-[500px] bg-slate-100 dark:bg-slate-700/50 rounded-lg flex items-center justify-center">
              <div className="text-center">
                <GitBranch className="w-12 h-12 text-slate-300 dark:text-slate-600 mx-auto mb-4" />
                <p className="text-slate-500 dark:text-slate-400">
                  React Flow workflow editor would be rendered here
                </p>
                <p className="text-sm text-slate-400 dark:text-slate-500 mt-2">
                  Drag and drop nodes from the palette to build your workflow
                </p>
              </div>
            </div>
          </Card>

          <div className="grid grid-cols-4 gap-4">
            <Card title="Trigger Nodes">
              <div className="space-y-2">
                {['Message Received', 'User Joined', 'Reaction Added', 'Scheduled'].map((node) => (
                  <div
                    key={node}
                    className="p-3 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg cursor-pointer hover:bg-green-100 dark:hover:bg-green-900/30"
                  >
                    <p className="text-sm font-medium text-green-700 dark:text-green-400">{node}</p>
                  </div>
                ))}
              </div>
            </Card>
            <Card title="Condition Nodes">
              <div className="space-y-2">
                {['If/Else', 'Switch', 'Filter', 'Contains'].map((node) => (
                  <div
                    key={node}
                    className="p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg cursor-pointer hover:bg-amber-100 dark:hover:bg-amber-900/30"
                  >
                    <p className="text-sm font-medium text-amber-700 dark:text-amber-400">{node}</p>
                  </div>
                ))}
              </div>
            </Card>
            <Card title="Action Nodes">
              <div className="space-y-2">
                {['Send Message', 'Add Reaction', 'Call API', 'Set Variable'].map((node) => (
                  <div
                    key={node}
                    className="p-3 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg cursor-pointer hover:bg-blue-100 dark:hover:bg-blue-900/30"
                  >
                    <p className="text-sm font-medium text-blue-700 dark:text-blue-400">{node}</p>
                  </div>
                ))}
              </div>
            </Card>
            <Card title="Transform Nodes">
              <div className="space-y-2">
                {['Format Text', 'Parse JSON', 'Extract', 'Delay'].map((node) => (
                  <div
                    key={node}
                    className="p-3 bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800 rounded-lg cursor-pointer hover:bg-purple-100 dark:hover:bg-purple-900/30"
                  >
                    <p className="text-sm font-medium text-purple-700 dark:text-purple-400">{node}</p>
                  </div>
                ))}
              </div>
            </Card>
          </div>
        </div>
      ) : (
        // Workflow List
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {mockWorkflows.map((workflow) => (
            <Card key={workflow.id}>
              <div className="flex items-start justify-between mb-4">
                <div className="w-12 h-12 bg-primary-100 dark:bg-primary-900/30 rounded-lg flex items-center justify-center">
                  <GitBranch className="w-6 h-6 text-primary-600 dark:text-primary-400" />
                </div>
                <span
                  className={`px-2 py-1 rounded-full text-xs font-medium ${
                    workflow.enabled
                      ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                      : 'bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-400'
                  }`}
                >
                  {workflow.enabled ? 'Active' : 'Disabled'}
                </span>
              </div>

              <h3 className="text-lg font-semibold text-slate-900 dark:text-white">
                {workflow.name}
              </h3>
              <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
                {workflow.description}
              </p>

              <div className="flex items-center gap-4 mt-4 text-sm text-slate-500 dark:text-slate-400">
                <span>{workflow.nodes} nodes</span>
                <span>Updated {workflow.updated_at}</span>
              </div>

              <div className="flex items-center gap-2 mt-4 pt-4 border-t border-slate-200 dark:border-slate-700">
                <Button
                  variant="secondary"
                  size="sm"
                  className="flex-1"
                  onClick={() => setSelectedWorkflow(workflow.id)}
                >
                  Edit
                </Button>
                <button className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg">
                  <Play className="w-4 h-4 text-slate-500" />
                </button>
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
      )}
    </div>
  );
}
