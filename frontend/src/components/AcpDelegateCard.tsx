import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, ChevronDown, ChevronRight, Sparkles, Square, Wrench } from 'lucide-react'
import type { ContentSegment } from '../api/chat'
import { cancelAcpDelegate } from '../api/acpAgents'

/**
 * AcpDelegateCard —— 渲染 delegate_acp_agent 工具的实时流式输出卡片。
 *
 * 协议：后端 `_handle_delegate_acp_agent` 推 system_notice 事件，每条 chunk 一个
 * segment：
 *   {
 *     type: 'system_notice',
 *     kind: 'acp_delegate_chunk',
 *     payload: {
 *       delegate_id: string,
 *       agent: string,
 *       chunk_kind: 'start' | 'text' | 'thought' | 'tool_call' | 'tool_done' | 'end',
 *       seq: number,
 *       // chunk_kind 相关字段：
 *       text?: string,             // text / thought
 *       title?: string,            // tool_call
 *       tool_call_id?: string,     // tool_call / tool_done
 *       tool_kind?: string,
 *       status?: string,           // tool_done
 *       prompt_preview?: string,   // start
 *       cwd?: string,
 *       success?: boolean,         // end
 *       raw_update_count?: number,
 *       error?: string,
 *     },
 *   }
 *
 * 渲染：所有同 delegate_id 的 chunk 在 segments[] 里按 seq 排序，拼成实时进度
 * 卡片。runtimeBlocks 在第一个 chunk 处渲染卡片，后续 chunk 返回 null 跳过——
 * 天然支持持久化重放。
 *
 * 红线：
 * - 只读 segment.payload；不要在 card 里加 setState 跨 session 共享单例
 * - 自动滚动用 ref + scrollHeight，跟 CommandStreamCard 保持一致
 */

export interface AcpDelegateChunkPayload {
  delegate_id?: string
  agent?: string
  chunk_kind?: 'start' | 'text' | 'thought' | 'tool_call' | 'tool_done' | 'end' | string
  seq?: number
  text?: string
  title?: string
  tool_call_id?: string
  tool_kind?: string
  status?: string
  prompt_preview?: string
  cwd?: string
  success?: boolean
  raw_update_count?: number
  error?: string
  timeout_sec?: number
  /** 2026-05-16：end chunk 带 cancelled=true 表示是用户主动取消，UI 走“⋘ cancelled”徽标而非“✗ failed” */
  cancelled?: boolean
}

/** 从 segment 中安全提取 delegate_id（不是该类型 chunk 时返 null）。 */
export function getAcpDelegateChunkId(
  segment: ContentSegment | null | undefined,
): string | null {
  if (!segment) return null
  if (segment.type !== 'system_notice') return null
  if (segment.kind !== 'acp_delegate_chunk') return null
  const payload = segment.payload as AcpDelegateChunkPayload | undefined
  const did = payload?.delegate_id
  return typeof did === 'string' && did ? did : null
}

/** 找出所有跟 anchorSegment 同 delegate_id 的 chunks，按 seq 排序。 */
export function collectChunksForDelegate(
  segments: ContentSegment[],
  delegateId: string,
): ContentSegment[] {
  const matched = segments.filter((s) => getAcpDelegateChunkId(s) === delegateId)
  return matched.slice().sort((a, b) => {
    const sa = (a.payload as AcpDelegateChunkPayload | undefined)?.seq ?? 0
    const sb = (b.payload as AcpDelegateChunkPayload | undefined)?.seq ?? 0
    return sa - sb
  })
}

/**
 * 给 `delegate_acp_agent` tool_call 找匹配的 chunks（嵌进工具卡 result slot 用）。
 *
 * 链接策略：
 * 1. tool_result 已到 → 解析 JSON 取 `delegate_id`（最权威）
 * 2. 还在跑 → 从 toolCallSegment 之后第一条 acp_delegate_chunk 拿 delegate_id
 * 3. 都失败 → 返空数组（外层不渲染 customResult）
 */
export function findChunksForAcpDelegate(
  segments: ContentSegment[],
  toolCallSegment: ContentSegment,
  toolResultSegment: ContentSegment | undefined | null,
): ContentSegment[] {
  let did: string | null = null

  // 1. 从 tool_result 解析
  if (toolResultSegment) {
    const rawResult = toolResultSegment.result ?? toolResultSegment.content ?? ''
    if (typeof rawResult === 'string' && rawResult) {
      try {
        const obj = JSON.parse(rawResult)
        if (obj && typeof obj.delegate_id === 'string') did = obj.delegate_id
      } catch {/* ignore */}
    } else if (rawResult && typeof rawResult === 'object') {
      const r = rawResult as Record<string, unknown>
      if (typeof r.delegate_id === 'string') did = r.delegate_id
    }
  }

  // 2. 从 toolCall 之后第一条 chunk 取 delegate_id（live 场景）
  if (!did) {
    const toolIdx = segments.indexOf(toolCallSegment)
    if (toolIdx >= 0) {
      for (let i = toolIdx + 1; i < segments.length; i += 1) {
        const id = getAcpDelegateChunkId(segments[i])
        if (id) { did = id; break }
      }
    }
  }

  if (!did) return []
  return collectChunksForDelegate(segments, did)
}

/**
 * 重载/重启后 live chunks 没了，从 tool_call + tool_result JSON 合成静态 chunks 喂卡片。
 *
 * 对齐 `buildSyntheticChunksFromToolResult`（CommandStreamCard 同款持久化路径），
 * 保证 ACP 卡片刷新/重连后还能显示，不退化成默认 <pre> JSON。
 *
 * 合成的三条：
 *   [0] 'start' chunk —— agent/prompt_preview/cwd（从 tool_call.arguments 取）
 *   [1] 'text'  chunk —— tool_result.output 全文
 *   [2] 'end'   chunk —— success / raw_update_count / error
 *
 * 没 result 时返空数组（call 还没结束 / SSE 还在跑），让外层用 live chunks。
 */
export function buildSyntheticChunksFromAcpResult(
  toolCallSegment: ContentSegment,
  resultSegment: ContentSegment | undefined | null,
  fallbackDelegateId: string,
): ContentSegment[] {
  if (!resultSegment) return []

  // 解析 tool_result JSON
  const rawResult = resultSegment.result ?? resultSegment.content ?? ''
  let parsed: Record<string, unknown> = {}
  if (typeof rawResult === 'string' && rawResult) {
    try { parsed = JSON.parse(rawResult) } catch { /* 不是 JSON 就当 raw text */ }
  } else if (rawResult && typeof rawResult === 'object') {
    parsed = rawResult as Record<string, unknown>
  }

  // tool_call.arguments 兼容三种形状（同 CommandStreamCard.pickToolCallCommand 思路）
  function pickArg<T = unknown>(key: string): T | undefined {
    const a1 = (toolCallSegment.arguments as Record<string, unknown> | undefined)?.[key]
    if (a1 !== undefined) return a1 as T
    const a2 = (toolCallSegment.toolCall?.input as Record<string, unknown> | undefined)?.[key]
    if (a2 !== undefined) return a2 as T
    if (typeof toolCallSegment.arguments === 'string') {
      try {
        const obj = JSON.parse(toolCallSegment.arguments)
        if (obj && key in obj) return obj[key] as T
      } catch { /* ignore */ }
    }
    return undefined
  }

  const did = (typeof parsed.delegate_id === 'string' && parsed.delegate_id)
    || toolCallSegment.tool_use_id
    || fallbackDelegateId
  const agent = (typeof parsed.agent === 'string' && parsed.agent)
    || pickArg<string>('agent')
    || 'external-agent'
  const promptText = pickArg<string>('prompt') || ''
  const cwd = pickArg<string>('cwd') || ''
  const output = typeof parsed.output === 'string' ? parsed.output : ''
  const success = parsed.success !== false
  const rawCount = typeof parsed.raw_update_count === 'number' ? parsed.raw_update_count : 0
  const errStr = typeof parsed.error === 'string' ? parsed.error : ''
  const cancelled = parsed.cancelled === true

  const makeBase = (kind: string, seq: number, extra: Record<string, unknown>): ContentSegment => ({
    id: seq,
    type: 'system_notice',
    kind: 'acp_delegate_chunk',
    content: '',
    payload: {
      delegate_id: did,
      agent,
      chunk_kind: kind,
      seq,
      ...extra,
    },
  })

  const out: ContentSegment[] = []
  out.push(makeBase('start', 1, {
    prompt_preview: promptText.slice(0, 200),
    cwd: cwd || null,
  }))
  if (output) {
    out.push(makeBase('text', 2, { text: output }))
  }
  out.push(makeBase('end', 3, {
    success,
    raw_update_count: rawCount,
    error: errStr,
    cancelled,
  }))
  return out
}

/** 判断当前 segment 是该 delegate_id 在 segments[] 里的"首条"——只有首条才渲染卡片。 */
export function isFirstChunkForDelegate(
  segments: ContentSegment[],
  segmentIndex: number,
): boolean {
  const did = getAcpDelegateChunkId(segments[segmentIndex])
  if (!did) return false
  for (let i = 0; i < segmentIndex; i += 1) {
    if (getAcpDelegateChunkId(segments[i]) === did) return false
  }
  return true
}

interface AcpDelegateCardProps {
  delegateId: string
  chunks: ContentSegment[]
}

export function AcpDelegateCard({ delegateId, chunks }: AcpDelegateCardProps) {
  const [expanded, setExpanded] = useState(true)
  const [showThoughts, setShowThoughts] = useState(false)
  const userToggledRef = useRef(false)
  const autoCollapsedRef = useRef(false)
  const bodyRef = useRef<HTMLDivElement | null>(null)

  // 第一条 chunk 通常是 'start'，含 agent / prompt_preview / cwd 等元数据
  const meta = useMemo(() => {
    let agent = ''
    let promptPreview = ''
    let cwd = ''
    for (const c of chunks) {
      const p = c.payload as AcpDelegateChunkPayload | undefined
      if (!p) continue
      if (!agent && typeof p.agent === 'string') agent = p.agent
      if (p.chunk_kind === 'start') {
        if (typeof p.prompt_preview === 'string') promptPreview = p.prompt_preview
        if (typeof p.cwd === 'string') cwd = p.cwd
        break
      }
    }
    return { agent: agent || 'external-agent', promptPreview, cwd }
  }, [chunks])

  // 找 'end' sentinel
  const doneInfo = useMemo(() => {
    for (const c of chunks) {
      const p = c.payload as AcpDelegateChunkPayload | undefined
      if (p && p.chunk_kind === 'end') {
        return {
          done: true,
          success: p.success !== false,
          rawCount: typeof p.raw_update_count === 'number' ? p.raw_update_count : 0,
          error: typeof p.error === 'string' ? p.error : '',
          cancelled: p.cancelled === true,
        }
      }
    }
    return { done: false, success: true, rawCount: 0, error: '', cancelled: false }
  }, [chunks])

  // 取消按钮状态：防重复点击 / 点了以后给反馈
  const [cancelling, setCancelling] = useState(false)
  const handleCancel = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (cancelling || doneInfo.done) return
    setCancelling(true)
    try {
      await cancelAcpDelegate(delegateId)
    } catch (err) {
      // 取消失败不阻塞用户——后端幂等 + 有超时兑底
      console.warn('[AcpDelegateCard] cancel failed:', err)
      setCancelling(false)
    }
    // 成功后不手动重置 cancelling——end chunk 到达 doneInfo.done=true 后按钮自然隐藏
  }

  // chunks=[] 时（首批 SSE 还没到 / 持久化也没合成）也得让 doneInfo 跟 chunks 状态保持一致；
  // useMemo 默认就是这样，无需额外处理

  // 完成后自动折叠（用户手动展开过就不再覆盖）
  useEffect(() => {
    if (doneInfo.done && !autoCollapsedRef.current && !userToggledRef.current) {
      autoCollapsedRef.current = true
      setExpanded(false)
    }
  }, [doneInfo.done])

  // 把 chunks 按出现顺序拍平为渲染条目
  const items = useMemo(() => {
    type Item =
      | { kind: 'text'; text: string }
      | { kind: 'thought'; text: string }
      | { kind: 'tool_call'; toolCallId: string; title: string; toolKind: string }
      | { kind: 'tool_done'; toolCallId: string; status: string }
    const out: Item[] = []
    for (const c of chunks) {
      const p = c.payload as AcpDelegateChunkPayload | undefined
      if (!p) continue
      const ck = p.chunk_kind
      if (ck === 'text') {
        const t = typeof p.text === 'string' ? p.text : ''
        if (!t) continue
        const last = out[out.length - 1]
        if (last && last.kind === 'text') {
          last.text += t
        } else {
          out.push({ kind: 'text', text: t })
        }
      } else if (ck === 'thought') {
        const t = typeof p.text === 'string' ? p.text : ''
        if (!t) continue
        const last = out[out.length - 1]
        if (last && last.kind === 'thought') {
          last.text += t
        } else {
          out.push({ kind: 'thought', text: t })
        }
      } else if (ck === 'tool_call') {
        out.push({
          kind: 'tool_call',
          toolCallId: typeof p.tool_call_id === 'string' ? p.tool_call_id : '',
          title: typeof p.title === 'string' ? p.title : '',
          toolKind: typeof p.tool_kind === 'string' ? p.tool_kind : 'other',
        })
      } else if (ck === 'tool_done') {
        out.push({
          kind: 'tool_done',
          toolCallId: typeof p.tool_call_id === 'string' ? p.tool_call_id : '',
          status: typeof p.status === 'string' ? p.status : '',
        })
      }
    }
    return out
  }, [chunks])

  // 自动滚到底
  useEffect(() => {
    if (!expanded) return
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [expanded, chunks.length, items])

  const totalTextChars = useMemo(
    () => items.reduce((acc, it) => acc + ((it.kind === 'text') ? it.text.length : 0), 0),
    [items],
  )

  const thoughtCount = items.filter((it) => it.kind === 'thought').length
  const toolCallCount = items.filter((it) => it.kind === 'tool_call').length

  return (
    <div className="chain-segment chain-acp-delegate">
      <button
        type="button"
        className={`chain-tool-header-unified chain-acp-delegate-header${
          doneInfo.done
            ? doneInfo.success
              ? ' chain-acp-delegate-header-done'
              : ' chain-acp-delegate-header-failed'
            : ' chain-acp-delegate-header-live'
        }`}
        onClick={() => {
          userToggledRef.current = true
          setExpanded((v) => !v)
        }}
        title={meta.promptPreview || delegateId}
      >
        <span className="chain-tool-leading">
          <Bot size={14} />
          <span className="chain-tool-name">
            <code className="chain-acp-delegate-agent">{meta.agent.replace(/-/g, ' ')}</code>
            <span className="chain-acp-delegate-prompt">
              {meta.promptPreview || `(delegate ${delegateId.slice(0, 12)}…)`}
            </span>
          </span>
        </span>
        <span className="chain-acp-delegate-meta">
          {doneInfo.done ? (
            <span
              className={`chain-acp-delegate-status${
                doneInfo.cancelled
                  ? ' is-cancelled'
                  : doneInfo.success ? ' is-success' : ' is-failed'
              }`}
            >
              {doneInfo.cancelled
                ? '⊘ cancelled'
                : (doneInfo.success ? '✓ done' : '✗ failed')}
            </span>
          ) : (
            <>
              <span className="chain-acp-delegate-status is-running">running…</span>
              <button
                type="button"
                className="chain-acp-delegate-cancel"
                onClick={handleCancel}
                disabled={cancelling}
                title="停止外部 agent"
              >
                <Square size={10} />
                {cancelling ? '取消中…' : '停止'}
              </button>
            </>
          )}
          <span className="chain-acp-delegate-bytes">
            {totalTextChars} chars
            {toolCallCount > 0 ? ` · ${toolCallCount} tools` : ''}
          </span>
          <span className="chain-tool-toggle">
            {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </span>
        </span>
      </button>

      {expanded && (
        <div ref={bodyRef} className="chain-acp-delegate-body">
          {items.length === 0 ? (
            <div className="chain-acp-delegate-empty">
              {doneInfo.done ? (
                doneInfo.cancelled ? (
                  <>外部 agent 已被用户取消</>
                ) : doneInfo.error ? (
                  <>外部 agent 报错：<code>{doneInfo.error}</code></>
                ) : doneInfo.rawCount > 0 ? (
                  <>外部 agent 收到 {doneInfo.rawCount} 条 update 但未提取出文本输出（SSE 可能丢了，看后端日志 <code>[ACP-CLIENT-UPDATE]</code>）</>
                ) : (
                  <>外部 agent 一字未出（成功完成但没产生输出）</>
                )
              ) : (
                <>（已派给 {meta.agent}，等首批响应…）</>
              )}
            </div>
          ) : (
            <div className="chain-acp-delegate-stream">
              {items.map((it, i) => {
                if (it.kind === 'text') {
                  return (
                    <div key={i} className="chain-acp-delegate-line is-text">
                      {it.text}
                    </div>
                  )
                }
                if (it.kind === 'thought') {
                  if (!showThoughts) return null
                  return (
                    <div key={i} className="chain-acp-delegate-line is-thought">
                      <Sparkles size={11} />
                      <span>{it.text}</span>
                    </div>
                  )
                }
                if (it.kind === 'tool_call') {
                  return (
                    <div key={i} className="chain-acp-delegate-line is-tool-call">
                      <Wrench size={11} />
                      <span>
                        <span className="chain-acp-delegate-tool-kind">[{it.toolKind}]</span>{' '}
                        {it.title || it.toolCallId}
                      </span>
                    </div>
                  )
                }
                if (it.kind === 'tool_done') {
                  const ok = it.status === 'completed'
                  return (
                    <div
                      key={i}
                      className={`chain-acp-delegate-line is-tool-done${
                        ok ? ' is-ok' : ' is-bad'
                      }`}
                    >
                      <span>
                        {ok ? '  └ ✓ ' : '  └ ✗ '}
                        {it.toolCallId} ({it.status || 'unknown'})
                      </span>
                    </div>
                  )
                }
                return null
              })}
              {!doneInfo.success && doneInfo.error && (
                <div className="chain-acp-delegate-line is-error">{doneInfo.error}</div>
              )}
            </div>
          )}
          {thoughtCount > 0 && (
            <button
              type="button"
              className="chain-acp-delegate-thoughts-toggle"
              onClick={(e) => {
                e.stopPropagation()
                setShowThoughts((v) => !v)
              }}
            >
              {showThoughts ? '隐藏' : '显示'} {thoughtCount} 条思考
            </button>
          )}
        </div>
      )}
    </div>
  )
}
