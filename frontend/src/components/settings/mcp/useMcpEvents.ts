/**
 * 订阅 MCP 子系统的状态广播。
 *
 * 后端通过 sse_manager 把 server status / tools changed / oauth needed 事件
 * 推到固定 pseudo session id（`__mcp_control__`），前端在这里挂 EventSource。
 *
 * 设计要点：
 * - 复用既有 `/sessions/{id}/events` 端点（断线续传 + ring buffer）
 * - 启动时 `?last_event_id=0` 拿全量 backlog，让 UI 一进 tab 就能复原状态
 * - SSE 失败 / 后端关停时静默 fallback，调用方可继续走 polling
 *
 * 与 `subagentLiveBus` 的 EventSource 模式对齐（见 frontend/src/api/subagentLiveBus.ts）
 */

import { useEffect, useRef } from 'react'
import { API_BASE } from '../../../api/base'
import { getControlSessionId } from '../../../api/mcp'

export type MCPEvent =
  | { type: 'mcp.server.status'; server_id: string; state: string; error: string | null; tool_count: number }
  | { type: 'mcp.tools.changed'; server_id: string; tool_count: number }
  | { type: 'mcp.oauth.needed'; server_id: string; authorize_url: string }
  | { type: string; [k: string]: unknown }

export interface UseMcpEventsOptions {
  onEvent: (event: MCPEvent) => void
  enabled?: boolean
}

export function useMcpEvents({ onEvent, enabled = true }: UseMcpEventsOptions) {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    if (!enabled) return
    let es: EventSource | null = null
    let cancelled = false

    ;(async () => {
      try {
        const sid = await getControlSessionId()
        if (cancelled) return
        es = new EventSource(
          `${API_BASE}/sessions/${encodeURIComponent(sid)}/events?last_event_id=0`,
        )
        es.onmessage = (msg) => {
          try {
            const data = JSON.parse(msg.data)
            if (data && typeof data.type === 'string' && data.type.startsWith('mcp.')) {
              onEventRef.current(data as MCPEvent)
            }
          } catch (e) {
            console.debug('[MCP-SSE] parse error', e)
          }
        }
        es.onerror = () => {
          // 浏览器会自动重连；这里只打 debug
          console.debug('[MCP-SSE] connection error (auto-reconnect)')
        }
      } catch (e) {
        console.debug('[MCP-SSE] subscribe failed, falling back to polling', e)
      }
    })()

    return () => {
      cancelled = true
      if (es) es.close()
    }
  }, [enabled])
}
