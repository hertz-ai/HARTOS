import { useState, useEffect, useRef } from 'react';
import { MessageSquare, ArrowRight, Clock, User, AlertCircle, CheckCircle } from 'lucide-react';
import { useSubscription } from '../api/websocket';

interface FeedItem {
  id: string;
  type: 'message' | 'event' | 'error' | 'success';
  channel: string;
  content: string;
  sender?: string;
  timestamp: Date;
}

interface LiveFeedProps {
  maxItems?: number;
  showChannel?: boolean;
  filter?: string[];
  className?: string;
}

export default function LiveFeed({
  maxItems = 50,
  showChannel = true,
  filter,
  className = ''
}: LiveFeedProps) {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [paused, setPaused] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  // Subscribe to channel messages
  useSubscription('com.hertzai.hevolve.channel.message', (data: any) => {
    if (paused) return;
    if (filter && !filter.includes(data.channel)) return;

    const newItem: FeedItem = {
      id: `${Date.now()}-${Math.random()}`,
      type: 'message',
      channel: data.channel || 'unknown',
      content: data.text || data.message || JSON.stringify(data),
      sender: data.sender_name || data.sender_id,
      timestamp: new Date(),
    };

    setItems(prev => [newItem, ...prev].slice(0, maxItems));
  });

  // Subscribe to system events
  useSubscription('com.hertzai.hevolve.system.event', (data: any) => {
    if (paused) return;

    const newItem: FeedItem = {
      id: `${Date.now()}-${Math.random()}`,
      type: data.error ? 'error' : 'event',
      channel: data.source || 'system',
      content: data.message || data.event || JSON.stringify(data),
      timestamp: new Date(),
    };

    setItems(prev => [newItem, ...prev].slice(0, maxItems));
  });

  // Subscribe to bridge events
  useSubscription('com.hertzai.hevolve.bridge.forwarded', (data: any) => {
    if (paused) return;

    const newItem: FeedItem = {
      id: `${Date.now()}-${Math.random()}`,
      type: data.success ? 'success' : 'error',
      channel: `${data.source_channel} → ${data.target_channel}`,
      content: `Message forwarded via rule: ${data.rule_id}`,
      timestamp: new Date(),
    };

    setItems(prev => [newItem, ...prev].slice(0, maxItems));
  });

  // Auto-scroll handling
  useEffect(() => {
    if (autoScrollRef.current && feedRef.current) {
      feedRef.current.scrollTop = 0;
    }
  }, [items]);

  const handleScroll = () => {
    if (feedRef.current) {
      autoScrollRef.current = feedRef.current.scrollTop < 10;
    }
  };

  const getItemIcon = (type: string) => {
    switch (type) {
      case 'message': return <MessageSquare className="w-4 h-4" />;
      case 'error': return <AlertCircle className="w-4 h-4" />;
      case 'success': return <CheckCircle className="w-4 h-4" />;
      default: return <ArrowRight className="w-4 h-4" />;
    }
  };

  const getItemColor = (type: string) => {
    switch (type) {
      case 'message': return 'text-blue-500 bg-blue-50 dark:bg-blue-900/20';
      case 'error': return 'text-red-500 bg-red-50 dark:bg-red-900/20';
      case 'success': return 'text-green-500 bg-green-50 dark:bg-green-900/20';
      default: return 'text-slate-500 bg-slate-50 dark:bg-slate-800';
    }
  };

  const formatTime = (date: Date) => {
    return date.toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
  };

  return (
    <div className={`flex flex-col ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <div className={`w-2 h-2 rounded-full ${paused ? 'bg-yellow-500' : 'bg-green-500 animate-pulse'}`} />
          <span className="text-sm font-medium text-slate-700 dark:text-slate-300">
            Live Feed
          </span>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            ({items.length} events)
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPaused(!paused)}
            className={`text-xs px-2 py-1 rounded ${
              paused
                ? 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400'
                : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-400'
            }`}
          >
            {paused ? 'Resume' : 'Pause'}
          </button>
          <button
            onClick={() => setItems([])}
            className="text-xs px-2 py-1 rounded bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-400"
          >
            Clear
          </button>
        </div>
      </div>

      {/* Feed */}
      <div
        ref={feedRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto space-y-2 max-h-[400px] pr-2"
      >
        {items.length === 0 ? (
          <div className="text-center py-8 text-slate-400 dark:text-slate-500">
            <MessageSquare className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">Waiting for events...</p>
          </div>
        ) : (
          items.map((item) => (
            <div
              key={item.id}
              className="flex items-start gap-3 p-2 rounded-lg bg-slate-50 dark:bg-slate-800/50 animate-fadeIn"
            >
              <div className={`p-1.5 rounded ${getItemColor(item.type)}`}>
                {getItemIcon(item.type)}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  {showChannel && (
                    <span className="text-xs font-medium text-slate-500 dark:text-slate-400 capitalize">
                      {item.channel}
                    </span>
                  )}
                  {item.sender && (
                    <span className="flex items-center gap-1 text-xs text-slate-400 dark:text-slate-500">
                      <User className="w-3 h-3" />
                      {item.sender}
                    </span>
                  )}
                </div>
                <p className="text-sm text-slate-700 dark:text-slate-300 truncate">
                  {item.content}
                </p>
              </div>
              <span className="flex items-center gap-1 text-xs text-slate-400 dark:text-slate-500 whitespace-nowrap">
                <Clock className="w-3 h-3" />
                {formatTime(item.timestamp)}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
