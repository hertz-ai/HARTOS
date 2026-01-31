/**
 * WebSocket client for real-time updates via WAMP Crossbar
 * Connects to the existing Crossbar infrastructure for live data
 */

type MessageHandler = (data: any) => void;
type ConnectionHandler = () => void;

interface Subscription {
  topic: string;
  handler: MessageHandler;
}

class WebSocketClient {
  private ws: WebSocket | null = null;
  private url: string;
  private realm: string;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;
  private reconnectDelay = 1000;
  private subscriptions: Map<string, Set<MessageHandler>> = new Map();
  private messageQueue: any[] = [];
  private isConnected = false;
  private sessionId: string | null = null;
  private msgIdCounter = 0;

  // Event handlers
  private onConnectHandlers: Set<ConnectionHandler> = new Set();
  private onDisconnectHandlers: Set<ConnectionHandler> = new Set();
  private onErrorHandlers: Set<(error: Error) => void> = new Set();

  constructor(url?: string, realm?: string) {
    this.url = url || import.meta.env.VITE_WS_URL || 'ws://localhost:8088/ws';
    this.realm = realm || import.meta.env.VITE_WAMP_REALM || 'realm1';
  }

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(this.url);

        this.ws.onopen = () => {
          console.log('WebSocket connected');
          this.reconnectAttempts = 0;

          // Send WAMP hello
          this.sendWampHello();
        };

        this.ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            this.handleMessage(data);

            if (data.msg === 'connected') {
              this.isConnected = true;
              this.sessionId = data.session;
              this.onConnectHandlers.forEach(h => h());
              this.flushMessageQueue();
              resolve();
            }
          } catch (e) {
            console.error('Failed to parse WebSocket message:', e);
          }
        };

        this.ws.onclose = () => {
          console.log('WebSocket disconnected');
          this.isConnected = false;
          this.onDisconnectHandlers.forEach(h => h());
          this.attemptReconnect();
        };

        this.ws.onerror = (error) => {
          console.error('WebSocket error:', error);
          this.onErrorHandlers.forEach(h => h(new Error('WebSocket error')));
          reject(error);
        };

      } catch (error) {
        reject(error);
      }
    });
  }

  private sendWampHello() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        msg: 'connect',
        version: '1',
        support: ['1']
      }));
    }
  }

  private handleMessage(data: any) {
    // Handle ping/pong
    if (data.msg === 'ping') {
      this.ws?.send(JSON.stringify({ msg: 'pong' }));
      return;
    }

    // Handle subscription events
    if (data.msg === 'changed' && data.collection) {
      const topic = data.collection;
      const handlers = this.subscriptions.get(topic);
      if (handlers) {
        const payload = data.fields?.args?.[0] || data.fields || data;
        handlers.forEach(handler => handler(payload));
      }
    }

    // Handle pub/sub events
    if (data.msg === 'event') {
      const topic = data.topic || data.subscription;
      const handlers = this.subscriptions.get(topic);
      if (handlers) {
        handlers.forEach(handler => handler(data.args?.[0] || data));
      }
    }
  }

  private attemptReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error('Max reconnection attempts reached');
      return;
    }

    this.reconnectAttempts++;
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);

    console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);

    setTimeout(() => {
      this.connect().catch(console.error);
    }, delay);
  }

  private flushMessageQueue() {
    while (this.messageQueue.length > 0) {
      const msg = this.messageQueue.shift();
      this.send(msg);
    }
  }

  private nextMsgId(): string {
    return String(++this.msgIdCounter);
  }

  subscribe(topic: string, handler: MessageHandler): () => void {
    if (!this.subscriptions.has(topic)) {
      this.subscriptions.set(topic, new Set());

      // Send WAMP subscribe
      if (this.isConnected) {
        this.send({
          msg: 'sub',
          id: this.nextMsgId(),
          name: topic,
          params: [topic, false]
        });
      }
    }

    this.subscriptions.get(topic)!.add(handler);

    // Return unsubscribe function
    return () => {
      const handlers = this.subscriptions.get(topic);
      if (handlers) {
        handlers.delete(handler);
        if (handlers.size === 0) {
          this.subscriptions.delete(topic);
          // Send WAMP unsubscribe
          if (this.isConnected) {
            this.send({
              msg: 'unsub',
              id: this.nextMsgId(),
              name: topic
            });
          }
        }
      }
    };
  }

  publish(topic: string, data: any) {
    this.send({
      msg: 'method',
      method: 'publish',
      id: this.nextMsgId(),
      params: [topic, data]
    });
  }

  call(method: string, ...args: any[]): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = this.nextMsgId();

      const handler = (data: any) => {
        if (data.id === id) {
          if (data.error) {
            reject(new Error(data.error));
          } else {
            resolve(data.result);
          }
        }
      };

      // Temporary subscription for RPC response
      const cleanup = this.subscribe(`rpc.response.${id}`, handler);

      this.send({
        msg: 'method',
        method: method,
        id: id,
        params: args
      });

      // Timeout after 30s
      setTimeout(() => {
        cleanup();
        reject(new Error('RPC timeout'));
      }, 30000);
    });
  }

  private send(data: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    } else {
      this.messageQueue.push(data);
    }
  }

  onConnect(handler: ConnectionHandler): () => void {
    this.onConnectHandlers.add(handler);
    return () => this.onConnectHandlers.delete(handler);
  }

  onDisconnect(handler: ConnectionHandler): () => void {
    this.onDisconnectHandlers.add(handler);
    return () => this.onDisconnectHandlers.delete(handler);
  }

  onError(handler: (error: Error) => void): () => void {
    this.onErrorHandlers.add(handler);
    return () => this.onErrorHandlers.delete(handler);
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.isConnected = false;
    this.subscriptions.clear();
    this.messageQueue = [];
  }

  get connected(): boolean {
    return this.isConnected;
  }
}

// Singleton instance
export const wsClient = new WebSocketClient();

// React hook for WebSocket
import { useEffect, useState, useCallback } from 'react';

export function useWebSocket() {
  const [connected, setConnected] = useState(wsClient.connected);

  useEffect(() => {
    const unsubConnect = wsClient.onConnect(() => setConnected(true));
    const unsubDisconnect = wsClient.onDisconnect(() => setConnected(false));

    if (!wsClient.connected) {
      wsClient.connect().catch(console.error);
    }

    return () => {
      unsubConnect();
      unsubDisconnect();
    };
  }, []);

  return { connected, wsClient };
}

export function useSubscription<T = any>(topic: string, handler: (data: T) => void) {
  useEffect(() => {
    const unsubscribe = wsClient.subscribe(topic, handler);
    return unsubscribe;
  }, [topic, handler]);
}

export function useLiveData<T = any>(topic: string, initialData: T): T {
  const [data, setData] = useState<T>(initialData);

  const handler = useCallback((newData: T) => {
    setData(newData);
  }, []);

  useSubscription(topic, handler);

  return data;
}

export default wsClient;
