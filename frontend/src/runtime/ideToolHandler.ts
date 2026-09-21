/**
 * IDE 反向调请求处理器（C-2）。
 *
 * 协议：backend tool handler 调 ide_tool_bridge.request → 通过 sse_manager 推一个
 *      `{type: 'ide_tool_request', call_id, tool, args}` 事件给前端。前端 SSE
 *      consumer (api/chat.ts::sendMessage) 收到这个 type 短路调 handleIdeToolRequest，
 *      由本模块代调 runtime.ide.<tool>(args) 然后 POST 回填到
 *      `${API_BASE}/v2/ide_tool_result`。backend 阻塞中的 future 被唤醒，handler 拿到结果。
 *
 * 这个处理器**不应该把事件渲染到聊天消息流里**——纯协议事件，对用户透明。
 *
 * 失败兜底：runtime.ide 不可用 / 调用抛错 → POST 回填 ok=false + error 字符串，
 *           backend 把错误传给 LLM，让模型自己降级（如 fallback 用 read_file）。
 */

import { getRuntime } from './index'
import { API_BASE } from '../api/base'

export interface IdeToolRequestPayload {
  call_id: string
  tool:
    | 'open_file'
    | 'insert_snippet'
    | 'get_diagnostics'
    | 'apply_edit'
    | 'open_terminal'
    | 'run_command_in_terminal'
    | string
  args: Record<string, unknown>
}

/** 从 SSE event 里抽 ide_tool_request payload；字段都是 backend 推的 transport 顶层字段。 */
export function extractIdeToolRequest(event: unknown): IdeToolRequestPayload | null {
  if (!event || typeof event !== 'object') return null
  const e = event as Record<string, unknown>
  if (e.type !== 'ide_tool_request') return null
  const callId = typeof e.call_id === 'string' ? e.call_id : null
  const tool = typeof e.tool === 'string' ? e.tool : null
  const args = e.args && typeof e.args === 'object' && !Array.isArray(e.args)
    ? (e.args as Record<string, unknown>)
    : {}
  if (!callId || !tool) return null
  return { call_id: callId, tool, args }
}

async function postResult(payload: {
  call_id: string
  ok: boolean
  result?: unknown
  error?: string
}): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/v2/ide_tool_result`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) {
      // 404 = backend future 已 done（超时 / 重复回填），不是 bug，静默吞
      if (res.status !== 404) {
        console.warn('[ideToolHandler] POST ide_tool_result non-ok:', res.status, await res.text().catch(() => ''))
      }
    }
  } catch (err) {
    console.warn('[ideToolHandler] POST ide_tool_result network error:', err)
  }
}

/**
 * 收到 backend 推的 ide_tool_request 事件后调它。
 * 内部异步执行 runtime.ide.<tool>(args) 然后 POST 回填，永远不抛错（捕获后回填 error）。
 */
export async function handleIdeToolRequest(event: unknown): Promise<void> {
  const payload = extractIdeToolRequest(event)
  if (!payload) {
    console.warn('[ideToolHandler] invalid ide_tool_request event:', event)
    return
  }
  const { call_id, tool, args } = payload

  let ok = false
  let result: unknown = null
  let error: string | undefined

  try {
    const runtime = await getRuntime()
    if (runtime.kind !== 'vscode' || !runtime.ide) {
      throw new Error('runtime.ide unavailable (not in VSCode webview runtime)')
    }
    const ide = runtime.ide
    if (tool === 'open_file') {
      result = await ide.openFile(args as Parameters<typeof ide.openFile>[0])
    } else if (tool === 'insert_snippet') {
      result = await ide.insertSnippet(args as Parameters<typeof ide.insertSnippet>[0])
    } else if (tool === 'get_diagnostics') {
      result = await ide.getDiagnostics(args as Parameters<typeof ide.getDiagnostics>[0])
    } else if (tool === 'apply_edit') {
      result = await ide.applyEdit(args as Parameters<typeof ide.applyEdit>[0])
    } else if (tool === 'open_terminal') {
      result = await ide.openTerminal(args as Parameters<typeof ide.openTerminal>[0])
    } else if (tool === 'run_command_in_terminal') {
      result = await ide.runCommandInTerminal(args as Parameters<typeof ide.runCommandInTerminal>[0])
    } else {
      throw new Error(`unknown ide tool: ${tool}`)
    }
    ok = true
  } catch (err) {
    error = err instanceof Error ? err.message : String(err)
  }

  await postResult({ call_id, ok, result, error })
}
