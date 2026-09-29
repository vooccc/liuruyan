/**
 * [DEV ONLY] EventBus 端到端 demo 页
 *
 * 用法：访问 http://localhost:1420/#dev-eventbus
 *
 * 验证目标：
 *   1. 前端 wsClient → 后端 /ws → InMemoryEventBus → 回前端订阅 — 通路是否畅通
 *   2. 状态机（idle/connecting/open/...）是否正确驱动
 *   3. Last-Event-ID 续传是否生效（断网/重连后）
 *
 * 下窗口完成 Chat.tsx 切换后，本文件会被删除。
 */

import { useEffect, useState, useRef } from 'react';

import { API_BASE } from '../../api/base';
import { useAppStore } from '../../stores/useAppStore';
import { selectSessionMessages, selectSessionStatus } from '../../stores/selectors';
import {
  createWsClient,
  type WsClient,
  type WsStatus,
} from '../../lib/wsClient';
import { channelSession } from '../../lib/busEventTypes';
import type { BusEvent } from '../../lib/busEventTypes';

const DEMO_SESSION_KEY = 'demo';
const DEMO_CHANNEL = channelSession(DEMO_SESSION_KEY);

const STATUS_COLOR: Record<WsStatus, string> = {
  idle: '#888',
  connecting: '#fbbf24',
  open: '#22c55e',
  closing: '#fbbf24',
  closed: '#94a3b8',
  reconnecting: '#f59e0b',
};

interface ReceivedEvent {
  id: number;
  type: string;
  text: string;
  ts: number;
}

export default function EventBusDemo() {
  const [status, setStatus] = useState<WsStatus>('idle');
  const [received, setReceived] = useState<ReceivedEvent[]>([]);
  const [publishText, setPublishText] = useState('');
  const [publishCount, setPublishCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busStats, setBusStats] = useState<unknown>(null);
  const clientRef = useRef<WsClient | null>(null);

  // 同时把消息写进 store，验证 store schema
  const ensureSession = useAppStore((s) => s.ensureSession);
  const appendSessionMessage = useAppStore((s) => s.appendSessionMessage);
  const storeMessages = useAppStore(selectSessionMessages(DEMO_SESSION_KEY));
  const sessionStatus = useAppStore(selectSessionStatus(DEMO_SESSION_KEY));

  useEffect(() => {
    ensureSession(DEMO_SESSION_KEY);
  }, [ensureSession]);

  // 拉 bus stats（每 2s）
  useEffect(() => {
    let cancelled = false;
    const fetchStats = async () => {
      try {
        const res = await fetch(`${API_BASE}/_dev/bus_stats`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setBusStats(data);
      } catch {
        /* ignore */
      }
    };
    fetchStats();
    const t = setInterval(fetchStats, 2000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const handleConnect = () => {
    setError(null);
    if (!clientRef.current) {
      const wsBase =
        API_BASE.replace(/^http/, 'ws') + '/ws';
      const client = createWsClient(wsBase);
      clientRef.current = client;

      client.onStatus((s) => setStatus(s));

      client.subscribe(DEMO_CHANNEL, (event: BusEvent) => {
        const text =
          (event['text'] as string | undefined) ?? JSON.stringify(event);
        setReceived((prev) => [
          ...prev,
          {
            id: event.event_id,
            type: event.type,
            text,
            ts: event.timestamp,
          },
        ]);
        appendSessionMessage(DEMO_SESSION_KEY, {
          id: `e${event.event_id}`,
          role: 'system',
          payload: { ...event },
          eventId: event.event_id,
          timestamp: event.timestamp,
        });
      });
    }
    clientRef.current.connect();
  };

  const handleDisconnect = () => {
    clientRef.current?.close();
    clientRef.current = null;
    setStatus('closed');
  };

  const handlePublish = async () => {
    setError(null);
    const text = publishText.trim() || `hello-${publishCount + 1}`;
    try {
      const res = await fetch(`${API_BASE}/_dev/publish`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          channel: DEMO_CHANNEL,
          type: 'demo_message',
          payload: { text },
        }),
      });
      if (!res.ok) {
        setError(`publish failed: ${res.status}`);
        return;
      }
      setPublishCount((n) => n + 1);
      setPublishText('');
    } catch (err) {
      setError(`publish error: ${(err as Error).message}`);
    }
  };

  const handleClear = () => {
    setReceived([]);
    setPublishCount(0);
    useAppStore.getState().clearSession(DEMO_SESSION_KEY);
    ensureSession(DEMO_SESSION_KEY);
  };

  return (
    <div
      style={{
        fontFamily: 'system-ui, sans-serif',
        padding: '24px',
        maxWidth: '900px',
        margin: '0 auto',
        color: '#e6edf3',
        background: '#0d1117',
        minHeight: '100vh',
      }}
    >
      <h1 style={{ marginTop: 0 }}>🔧 EventBus Demo (DEV)</h1>
      <p style={{ color: '#8b949e' }}>
        通路：前端 wsClient → 后端 <code>/ws</code> → InMemoryEventBus → 订阅回流。
        URL: <code>{API_BASE}</code>
      </p>

      {/* 状态卡 */}
      <div
        style={{
          display: 'flex',
          gap: '12px',
          flexWrap: 'wrap',
          marginBottom: '16px',
        }}
      >
        <StatCard label="WS 状态">
          <span
            style={{
              color: STATUS_COLOR[status],
              fontWeight: 600,
            }}
          >
            ● {status}
          </span>
        </StatCard>
        <StatCard label="收到事件">{received.length}</StatCard>
        <StatCard label="已发布">{publishCount}</StatCard>
        <StatCard label="Store 消息">{storeMessages.length}</StatCard>
        <StatCard label="Session 状态">{sessionStatus}</StatCard>
      </div>

      {/* 控制按钮 */}
      <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
        <button
          onClick={handleConnect}
          disabled={status === 'open' || status === 'connecting'}
          style={btnPrimary}
        >
          Connect
        </button>
        <button
          onClick={handleDisconnect}
          disabled={status !== 'open' && status !== 'reconnecting'}
          style={btnSecondary}
        >
          Disconnect
        </button>
        <button onClick={handleClear} style={btnSecondary}>
          Clear
        </button>
      </div>

      {/* 发布输入 */}
      <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
        <input
          value={publishText}
          onChange={(e) => setPublishText(e.target.value)}
          placeholder={`输入要发的文本（默认: hello-${publishCount + 1}）`}
          style={{
            flex: 1,
            padding: '8px 12px',
            background: '#161b22',
            color: '#e6edf3',
            border: '1px solid #30363d',
            borderRadius: '6px',
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handlePublish();
          }}
        />
        <button onClick={handlePublish} style={btnPrimary}>
          Publish
        </button>
      </div>

      {error && (
        <div
          style={{
            padding: '8px 12px',
            background: '#3d1414',
            color: '#f85149',
            borderRadius: '6px',
            marginBottom: '16px',
          }}
        >
          {error}
        </div>
      )}

      {/* 事件流 */}
      <h3 style={{ marginBottom: '8px' }}>📥 收到的事件</h3>
      <div
        style={{
          background: '#161b22',
          border: '1px solid #30363d',
          borderRadius: '6px',
          maxHeight: '300px',
          overflowY: 'auto',
        }}
      >
        {received.length === 0 ? (
          <div
            style={{
              padding: '24px',
              textAlign: 'center',
              color: '#6e7681',
            }}
          >
            没有事件 — 先 Connect 再 Publish
          </div>
        ) : (
          received.map((ev) => (
            <div
              key={ev.id}
              style={{
                padding: '8px 12px',
                borderBottom: '1px solid #21262d',
                fontFamily: 'monospace',
                fontSize: '13px',
              }}
            >
              <span style={{ color: '#7c3aed' }}>#{ev.id}</span>{' '}
              <span style={{ color: '#06b6d4' }}>{ev.type}</span>{' '}
              <span>{ev.text}</span>{' '}
              <span style={{ color: '#6e7681', fontSize: '11px' }}>
                {new Date(ev.ts * 1000).toLocaleTimeString()}
              </span>
            </div>
          ))
        )}
      </div>

      {/* Bus stats */}
      <h3 style={{ marginTop: '20px', marginBottom: '8px' }}>📊 后端 Bus 状态</h3>
      <pre
        style={{
          background: '#161b22',
          border: '1px solid #30363d',
          borderRadius: '6px',
          padding: '12px',
          fontSize: '12px',
          color: '#8b949e',
          overflowX: 'auto',
        }}
      >
        {busStats ? JSON.stringify(busStats, null, 2) : '未连通'}
      </pre>

      <div style={{ marginTop: '24px', color: '#6e7681', fontSize: '12px' }}>
        <strong>验证项：</strong>
        <ul style={{ marginLeft: '16px' }}>
          <li>Connect → 状态变绿（open）+ Bus stats 出现 <code>session:demo</code> channel</li>
          <li>Publish → 收到事件（event_id 单调递增）+ Store 消息条数同步增加</li>
          <li>DevTools Network → Offline 5s → Online → 自动重连 + Last-Event-ID 续传，无事件丢失</li>
        </ul>
      </div>
    </div>
  );
}

function StatCard({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{
        flex: '1 0 140px',
        background: '#161b22',
        border: '1px solid #30363d',
        borderRadius: '6px',
        padding: '12px',
      }}
    >
      <div style={{ fontSize: '11px', color: '#6e7681', marginBottom: '4px' }}>
        {label}
      </div>
      <div style={{ fontSize: '18px', fontWeight: 600 }}>{children}</div>
    </div>
  );
}

const btnPrimary: React.CSSProperties = {
  padding: '8px 16px',
  background: '#238636',
  color: '#fff',
  border: 'none',
  borderRadius: '6px',
  cursor: 'pointer',
  fontWeight: 500,
};

const btnSecondary: React.CSSProperties = {
  padding: '8px 16px',
  background: '#21262d',
  color: '#e6edf3',
  border: '1px solid #30363d',
  borderRadius: '6px',
  cursor: 'pointer',
};
