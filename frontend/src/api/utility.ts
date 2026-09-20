/**
 * 通用 LLM 工具 API（utility）—— 对应后端 backend/routes/utility_routes.py。
 *
 * 当前能力：
 * - translateText(text, opts)：把任意文本翻译到目标语言。
 *   主用例：思考块「翻译」按钮（LLM 英文思考 → 中文）。
 *   走当前 session/agent 的 model，不引第二个 model 选择器。
 */

import { API_BASE } from './base'

export type TranslateOptions = {
  /** 目标语言：zh / zh-CN / zh-TW / en / ja / ko / fr / de / es / ru / 或任意语言名 */
  targetLang?: string
  /** 当前会话 id（优先，模型按 session.model 解析） */
  sessionId?: string | null
  /** 当前角色 id（session 没传 / 没 model 时按 agent.active_model 兜底） */
  agentId?: string | null
}

export interface TranslateResult {
  text: string
  target_lang: string
  chars_in: number
  chars_out: number
}

export async function translateText(
  text: string,
  opts: TranslateOptions = {},
): Promise<TranslateResult> {
  const body = {
    text,
    target_lang: opts.targetLang || 'zh',
    session_id: opts.sessionId || undefined,
    agent_id: opts.agentId || undefined,
  }
  const res = await fetch(`${API_BASE}/utility/translate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = `翻译失败（HTTP ${res.status}）`
    try {
      const data = await res.json()
      if (data?.detail) detail = String(data.detail)
    } catch {
      /* swallow */
    }
    throw new Error(detail)
  }
  return res.json()
}
