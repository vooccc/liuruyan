/**
 * WebSocket 多路复用客户端
 *
 * 设计要点：
 * - 单连接（per 浏览器 tab）→ 多 channel 显式订阅
 * - 断线指数退避重连：1s → 2s → 4s → ... → 30s 上限
 * - 自动续传 Last-Event-ID（重连后用每个 channel 最后收到的 event_id 重新 subscribe）
 * - 协议详见 backend/core/adapters/ws.py 文件头
 *
 * 用法：
 *   const client = createWsClient(`ws://${location.host}/ws`);
 *   client.connect();
 *   const unsub = client.subscribe('session:demo', (event) => {
 *     console.log(event);
 *   });
 *   // ...
 *   unsub();  // 取消订阅（连接保留）
 *   client.close();  // 关闭连接（所有订阅自动撤销）
 */

import type {
  BusEvent,
  WsClientMessage,
  WsServerMessage,
} from './busEventTypes';

// ─────── 类型 ─────────

export type EventHandler = (event: BusEvent) => void;
export type StatusHandler = (status: WsStatus) => void;

export type WsStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'closing'
  | 'closed'
  | 'reconnecting';

interface ChannelSub {
  channel: string;
  handlers: Set<EventHandler>;
  lastEventId: number | undefined;
  /** 服务端是否已确认 subscribed */
  acked: boolean;
}

export interface WsClient {
  connect(): void;
  close(): void;
  subscribe(channel: string, handler: EventHandler): () => void;
  ping(): void;
  status(): WsStatus;
  onStatus(handler: StatusHandler): () => void;
  /** 调试用：当前活跃 channel 列表 */
  channels(): string[];
}

// ─────── 常量 ─────────

const RECONNECT_DELAY_INITIAL_MS = 1000;
const RECONNECT_DELAY_MAX_MS = 30000;
const PING_INTERVAL_MS = 30000;

// ─────── 实现 ─────────

export function createWsClient(url: string): WsClient {
  let ws: WebSocket | null = null;
  let status: WsStatus = 'idle';
  let reconnectDelay = RECONNECT_DELAY_INITIAL_MS;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let manuallyClosed = false;

  const subs = new Map<string, ChannelSub>();
  const statusHandlers = new Set<StatusHandler>();

  function setStatus(next: WsStatus) {
    if (status === next) return;
    status = next;
    statusHandlers.forEach((h) => {
      try {
        h(status);
      } catch (err) {
        console.error('[wsClient] status handler error', err);
      }
    });
  }

  function send(msg: WsClientMessage) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify(msg));
    } catch (err) {
      console.error('[wsClient] send failed', err);
    }
  }

  function syncSubscriptions() {
    // 连接打开后，把所有当前 sub 同步到服务端（含 last_event_id 续传）
    subs.forEach((sub) => {
      sub.acked = false;
      const msg: WsClientMessage = { action: 'subscribe', channel: sub.channel };
      if (sub.lastEventId !== undefined) {
        msg.last_event_id = sub.lastEventId;
      }
      send(msg);
    });
  }

  function clearTimers() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (pingTimer !== null) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  function scheduleReconnect() {
    if (manuallyClosed) return;
    setStatus('reconnecting');
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    const delay = reconnectDelay;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_DELAY_MAX_MS);
      doConnect();
    }, delay);
  }

  function handleMessage(raw: string) {
    let msg: WsServerMessage;
    try {
      msg = JSON.parse(raw) as WsServerMessage;
    } catch {
      console.warn('[wsClient] non-json message', raw);
      return;
    }

    switch (msg.action) {
      case 'subscribed': {
        const sub = subs.get(msg.channel);
        if (sub) sub.acked = true;
        break;
      }
      case 'unsubscribed': {
        // 服务端确认；客户端已经从 subs 移除（在 unsubscribe 时）
        break;
      }
      case 'event': {
        const sub = subs.get(msg.channel);
        if (!sub) return;
        sub.lastEventId = msg.event.event_id;
        sub.handlers.forEach((h) => {
          try {
            h(msg.event);
          } catch (err) {
            console.error('[wsClient] handler error', err);
          }
        });
        break;
      }
      case 'ping': {
        send({ action: 'pong' });
        break;
      }
      case 'pong': {
        // 服务端响应客户端 ping，无需处理
        break;
      }
      case 'error': {
        console.warn('[wsClient] server error:', msg.message);
        break;
      }
      default:
        // 协议升级时未知 action
        break;
    }
  }

  function doConnect() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
      return;
    }
    setStatus('connecting');
    try {
      ws = new WebSocket(url);
    } catch (err) {
      console.error('[wsClient] WebSocket constructor failed', err);
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      setStatus('open');
      reconnectDelay = RECONNECT_DELAY_INITIAL_MS;
      syncSubscriptions();
      // 启动 client-side ping
      if (pingTimer !== null) clearInterval(pingTimer);
      pingTimer = setInterval(() => send({ action: 'ping' }), PING_INTERVAL_MS);
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        handleMessage(ev.data);
      }
    };

    ws.onerror = () => {
      // 不在此处直接重连，等 onclose 触发统一处理
    };

    ws.onclose = () => {
      if (pingTimer !== null) {
        clearInterval(pingTimer);
        pingTimer = null;
      }
      ws = null;
      if (manuallyClosed) {
        setStatus('closed');
        return;
      }
      scheduleReconnect();
    };
  }

  return {
    connect() {
      manuallyClosed = false;
      doConnect();
    },

    close() {
      manuallyClosed = true;
      clearTimers();
      if (ws) {
        try {
          ws.close();
        } catch {
          // ignore
        }
      }
      ws = null;
      setStatus('closed');
    },

    subscribe(channel, handler) {
      let sub = subs.get(channel);
      const isFirst = !sub;
      if (!sub) {
        sub = { channel, handlers: new Set(), lastEventId: undefined, acked: false };
        subs.set(channel, sub);
      }
      sub.handlers.add(handler);

      if (isFirst && status === 'open') {
        send({ action: 'subscribe', channel });
      }

      return () => {
        const s = subs.get(channel);
        if (!s) return;
        s.handlers.delete(handler);
        if (s.handlers.size === 0) {
          subs.delete(channel);
          if (status === 'open') {
            send({ action: 'unsubscribe', channel });
          }
        }
      };
    },

    ping() {
      send({ action: 'ping' });
    },

    status() {
      return status;
    },

    onStatus(handler) {
      statusHandlers.add(handler);
      // 立刻推一次当前状态
      try {
        handler(status);
      } catch {
        // ignore
      }
      return () => {
        statusHandlers.delete(handler);
      };
    },

    channels() {
      return Array.from(subs.keys());
    },
  };
}

// ─────── 默认单例（基于 API_BASE 推导 WS URL）─────────

import { API_BASE } from '../api/base';

let _defaultClient: WsClient | null = null;

function deriveWsUrl(httpBase: string): string {
  // http://host:port → ws://host:port/ws
  // https → wss
  // API_BASE 在 base.ts 已经保证是合法 URL（env / location.origin / fallback 三种全合法），
  // 所以 new URL() 不会抛——这里 try/catch 是防御性兜底，真要落到 catch 说明 API_BASE 被
  // 外部代码污染过，应该 fail loud 让开发者立刻发现，而不是兜底到 hardcoded 127.0.0.1（
  // 这在 M8a 远程访问场景下也连不上——手机的 127.0.0.1 是手机自己）。
  try {
    const u = new URL(httpBase);
    const proto = u.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${u.host}/ws`;
  } catch (err) {
    throw new Error(
      `[wsClient] API_BASE is not a valid URL: ${JSON.stringify(httpBase)} — check api/base.ts. Original error: ${(err as Error).message}`
    );
  }
}

export function getDefaultWsClient(): WsClient {
  if (!_defaultClient) {
    _defaultClient = createWsClient(deriveWsUrl(API_BASE));
  }
  return _defaultClient;
}

export function resetDefaultWsClientForTesting(): void {
  if (_defaultClient) _defaultClient.close();
  _defaultClient = null;
}
