import { Bell, Moon, Sun, Search, RefreshCw, Cpu } from 'lucide-react';
import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import metricsApi from '../../api/endpoints/metrics';

export default function Header() {
  const [darkMode, setDarkMode] = useState(() => {
    if (typeof window !== 'undefined') {
      return document.documentElement.classList.contains('dark');
    }
    return false;
  });

  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ['status'],
    queryFn: () => metricsApi.status(),
    refetchInterval: 30000, // Refetch every 30 seconds
  });

  useEffect(() => {
    if (darkMode) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  }, [darkMode]);

  const toggleDarkMode = () => setDarkMode(!darkMode);

  return (
    <header className="h-16 bg-white dark:bg-slate-800 border-b border-slate-200 dark:border-slate-700 flex items-center justify-between px-6">
      {/* Search */}
      <div className="flex items-center gap-4">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            placeholder="Search..."
            className="w-64 pl-10 pr-4 py-2 bg-slate-100 dark:bg-slate-700 border-0 rounded-lg text-sm focus:ring-2 focus:ring-primary-500 dark:text-white placeholder:text-slate-400"
          />
        </div>
      </div>

      {/* Right side */}
      <div className="flex items-center gap-4">
        {/* Status indicator */}
        <div className="flex items-center gap-2 px-3 py-1.5 bg-slate-100 dark:bg-slate-700 rounded-lg">
          <div
            className={`w-2 h-2 rounded-full ${
              status?.status === 'running' ? 'bg-green-500' : 'bg-red-500'
            }`}
          />
          <span className="text-sm text-slate-600 dark:text-slate-300">
            {status?.status === 'running' ? 'Online' : 'Offline'}
          </span>
          <button
            onClick={() => refetchStatus()}
            className="p-1 hover:bg-slate-200 dark:hover:bg-slate-600 rounded"
          >
            <RefreshCw className="w-3 h-3 text-slate-500" />
          </button>
        </div>

        {/* LLM Backend indicator */}
        {status?.llm_backend && (
          <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium ${
            status.llm_backend.type === 'local_llamacpp'
              ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
              : status.llm_backend.type === 'cloud_fallback'
              ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
              : status.llm_backend.type === 'external'
              ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300'
              : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
          }`}>
            <Cpu className="w-3 h-3" />
            <span>{status.llm_backend.display_name}</span>
          </div>
        )}

        {/* Notifications */}
        <button className="relative p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg">
          <Bell className="w-5 h-5 text-slate-600 dark:text-slate-400" />
          <span className="absolute top-1 right-1 w-2 h-2 bg-red-500 rounded-full" />
        </button>

        {/* Dark mode toggle */}
        <button
          onClick={toggleDarkMode}
          className="p-2 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg"
        >
          {darkMode ? (
            <Sun className="w-5 h-5 text-slate-600 dark:text-slate-400" />
          ) : (
            <Moon className="w-5 h-5 text-slate-600 dark:text-slate-400" />
          )}
        </button>

        {/* User avatar */}
        <div className="w-8 h-8 bg-primary-500 rounded-full flex items-center justify-center text-white font-medium">
          A
        </div>
      </div>
    </header>
  );
}
