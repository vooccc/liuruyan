import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Copy, Terminal } from 'lucide-react'
import type { ContentSegment } from '../api/chat'

/**
 * CommandStreamCard —— 渲染 execute_command 的 SSE 流式输出（live terminal 卡）。
 *
 * 协议：后端 `_make_sse_stream_callback` 推 `system_notice` 事件，每条 chunk 一个
 * segment：
 *   {
 *     type: 'system_notice',
 *     kind: 'command_chunk',
 *     payload: {
 *       command_id: string,
 *       command: string,
 *       shell: 'bash' | 'powershell' | 'cmd',
 *       stream: 'stdout' | 'stderr',
 *       text: string,
 *       seq: number,
 *     },
 *   }
 *
 * 渲染：所有同 command_id 的 chunk 在 segments[] 里按 seq 排序，拼成一段 live
 * terminal（黑底等宽字 + stdout 浅色 / stderr 红色 + 自动滚到底）。runtimeBlocks
 * 在第一个 chunk 处渲染卡片，后续 chunk 返回 null 跳过——天然支持持久化重放。
 *
 * 红线：
 * - 只读 segment.payload；不要在 card 里加 setState 跨 session 共享单例
 * - 自动滚动用 ref + scrollHeight，不用全局 IntersectionObserver
 */

export interface CommandChunkPayload {
  command_id?: string
  command?: string
  command_key?: string
  shell?: string
  stream?: 'stdout' | 'stderr' | 'end' | string
  text?: string
  seq?: number
  done?: boolean
  exit_code?: number | null
  success?: boolean
}

/** 从 segment 中安全提取 command_id（不是该类型 chunk 时返 null）。 */
export function getCommandChunkId(segment: ContentSegment | null | undefined): string | null {
  if (!segment) return null
  if (segment.type !== 'system_notice') return null
  if (segment.kind !== 'command_chunk') return null
  const payload = segment.payload as CommandChunkPayload | undefined
  const cid = payload?.command_id
  return typeof cid === 'string' && cid ? cid : null
}

/**
 * 找出所有跟 anchorSegment 同 command_id 的 chunk segments，按 seq 排序。
 *
 * 调用方：runtimeBlocks 在第一次遇到该 command_id 时渲染本卡，后续 segment 跳过。
 */
export function collectChunksForCommand(
  segments: ContentSegment[],
  commandId: string,
): ContentSegment[] {
  const matched = segments.filter((s) => getCommandChunkId(s) === commandId)
  // 按 payload.seq 升序；缺 seq 的退化用数组下标稳定
  return matched.slice().sort((a, b) => {
    const sa = (a.payload as CommandChunkPayload | undefined)?.seq ?? 0
    const sb = (b.payload as CommandChunkPayload | undefined)?.seq ?? 0
    return sa - sb
  })
}

/** 判断当前 segment 是否是它所属 command_id 在 segments[] 里的"首条"——只有首条才渲染卡片。 */
export function isFirstChunkForCommand(
  segments: ContentSegment[],
  segmentIndex: number,
): boolean {
  const cid = getCommandChunkId(segments[segmentIndex])
  if (!cid) return false
  for (let i = 0; i < segmentIndex; i += 1) {
    if (getCommandChunkId(segments[i]) === cid) return false
  }
  return true
}

/**
 * 找出 segments 里跟某个 execute_command 的 tool_call 关联的 live command_chunk。
 * 通过 chunk.payload.command 跟 tool_call.arguments.command 字符串相等匹配。
 *
 * 用途：tool_call 渲染时优先用 live chunks 喂卡片；live chunks 没了再退回 tool_result 合成。
 */
export function findLiveChunksForToolCall(
  segments: ContentSegment[],
  toolCallSegment: ContentSegment,
): ContentSegment[] {
  const cmdText = pickToolCallCommand(toolCallSegment)
  if (!cmdText) return []
  const matched = segments.filter((s) => {
    if (s.type !== 'system_notice' || s.kind !== 'command_chunk') return false
    const p = s.payload as CommandChunkPayload | undefined
    const key = typeof p?.command_key === 'string' ? p.command_key : p?.command
    return typeof key === 'string' && key === cmdText
  })
  if (matched.length === 0) return []
  return matched.slice().sort((a, b) => {
    const sa = (a.payload as CommandChunkPayload | undefined)?.seq ?? 0
    const sb = (b.payload as CommandChunkPayload | undefined)?.seq ?? 0
    return sa - sb
  })
}

/** 容错读 tool_call.arguments.command（兼容前端 streaming 形状 + backend 持久化形状）。 */
function pickToolCallCommand(seg: ContentSegment): string {
  // 形状 A：{ tool_name, arguments: {...} }（chatStreamMessage push 的）
  const a1 = (seg.arguments as { command?: unknown } | undefined)?.command
  if (typeof a1 === 'string' && a1) return a1
  // 形状 B：{ toolCall: { name, input } }
  const a2 = (seg.toolCall?.input as { command?: unknown } | undefined)?.command
  if (typeof a2 === 'string' && a2) return a2
  // 形状 C：arguments 是 JSON 字符串
  if (typeof seg.arguments === 'string') {
    try {
      const obj = JSON.parse(seg.arguments)
      if (obj && typeof obj.command === 'string') return obj.command
    } catch {/* ignore */}
  }
  return ''
}

/**
 * 重启/重载后 live chunks 没了，从 tool_call + tool_result 合成静态 chunks 喂卡片。
 *
 * 返回两条 segment：
 *   [0] 一条 stdout 静态 chunk，text = tool_result 的输出文本
 *   [1] 一条 stream='end' sentinel，让卡片走折叠态 + 显示 ✓ exit
 *
 * 没 result（命令还没跑完 / 没有输出）时返空数组，由上层决定显示 placeholder。
 */
export function buildSyntheticChunksFromToolResult(
  toolCallSegment: ContentSegment,
  resultSegment: ContentSegment | undefined | null,
  fallbackCommandId: string,
): ContentSegment[] {
  if (!resultSegment) return []
  const cmdText = pickToolCallCommand(toolCallSegment)
  const cid = (toolCallSegment.tool_use_id || fallbackCommandId)
  // result 可能是字符串 / 对象 / undefined
  const rawResult = resultSegment.result ?? resultSegment.content ?? ''
  let outputText = ''
  let success = true
  if (typeof rawResult === 'string') {
    outputText = rawResult
    // 兼容 backend 失败返 "Error: ..." 字符串协议（execute_command 实际就是这么返的）
    success = !outputText.startsWith('Error:')
  } else if (rawResult && typeof rawResult === 'object') {
    try { outputText = JSON.stringify(rawResult, null, 2) } catch { outputText = String(rawResult) }
  }

  const outChunk: ContentSegment = {
    id: 0,
    type: 'system_notice',
    kind: 'command_chunk',
    content: '',
    payload: {
      command_id: cid,
      command: cmdText,
      shell: '',
      stream: 'stdout',
      text: outputText,
      seq: 1,
    },
  }
  const endChunk: ContentSegment = {
    id: 1,
    type: 'system_notice',
    kind: 'command_chunk',
    content: '',
    payload: {
      command_id: cid,
      command: cmdText,
      shell: '',
      stream: 'end',
      text: '',
      seq: 2,
      done: true,
      exit_code: success ? 0 : null,
      success,
    },
  }
  return outputText ? [outChunk, endChunk] : [endChunk]
}

interface CommandStreamCardProps {
  commandId: string
  chunks: ContentSegment[]
}

export function CommandStreamCard({ chunks }: CommandStreamCardProps) {
  const [expanded, setExpanded] = useState(true)
  const userToggledRef = useRef(false)
  const autoCollapsedRef = useRef(false)
  const collapseTimerRef = useRef<number | null>(null)
  const hoverRef = useRef(false)
  const bodyRef = useRef<HTMLDivElement | null>(null)

  // 第一条 chunk 的 metadata（command 文本 / shell 类型）—— 后续 chunk 不再带元信息
  const meta = useMemo(() => {
    const first = chunks[0]?.payload as CommandChunkPayload | undefined
    return {
      command: typeof first?.command === 'string' ? first.command : '',
      shell: typeof first?.shell === 'string' ? first.shell : '',
    }
  }, [chunks])

  // 找 done sentinel（stream='end' 或 done=true）—— 后端在命令结束后会推一条
  const doneInfo = useMemo(() => {
    for (const chunk of chunks) {
      const p = chunk.payload as CommandChunkPayload | undefined
      if (p && (p.done === true || p.stream === 'end')) {
        return {
          done: true,
          exitCode: typeof p.exit_code === 'number' ? p.exit_code : null,
          success: p.success !== false, // 兼容老数据：缺字段视作 success
        }
      }
    }
    return { done: false, exitCode: null as number | null, success: true }
  }, [chunks])

  // （自动折叠 effect 依赖 totalChars，挪到其定义之后 —— 见下方）

  // 渲染条目：把 chunk 按出现顺序拍平为 [{kind, text}]，相邻同 stream 的合并以减少 DOM 节点
  const lines = useMemo(() => {
    const out: { stream: 'stdout' | 'stderr'; text: string }[] = []
    for (const chunk of chunks) {
      const p = chunk.payload as CommandChunkPayload | undefined
      const text = typeof p?.text === 'string' ? p.text : ''
      if (!text) continue
      const stream: 'stdout' | 'stderr' = p?.stream === 'stderr' ? 'stderr' : 'stdout'
      const last = out[out.length - 1]
      if (last && last.stream === stream) {
        last.text += text
      } else {
        out.push({ stream, text })
      }
    }
    return out
  }, [chunks])

  // 自动滚到底（只在 expanded 时；用户手动滚走后下一帧停一次的策略复杂，先简化）
  useEffect(() => {
    if (!expanded) return
    const el = bodyRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  }, [expanded, chunks.length, lines])

  const totalChars = useMemo(
    () => lines.reduce((acc, line) => acc + line.text.length, 0),
    [lines],
  )

  // 命令完成时的自动折叠（2026-07-26 修订）。
  // 旧行为：done 一到立即折叠——快命令（Get-ChildItem 这种一秒内跑完的）
  // 输出用户根本来不及看见，而输出本身往往就是这条命令的"答案"。
  // 新行为三路分治：
  //   失败        → 永不自动折叠（错误必须被看见）
  //   成功无输出  → 立即折叠（没东西可看，事后收纳意图保留）
  //   成功有输出  → 延迟 2.5s 再折，给眼睛留出扫尾部输出的时间；
  //                 期间鼠标移入卡片（阅读意图）或用户手动操作则取消。
  useEffect(() => {
    if (!doneInfo.done || userToggledRef.current || autoCollapsedRef.current) return
    if (!doneInfo.success) {
      autoCollapsedRef.current = true // 标记已处理，保持展开
      return
    }
    if (totalChars === 0) {
      autoCollapsedRef.current = true
      setExpanded(false)
      return
    }
    if (collapseTimerRef.current !== null) return // 已排程
    collapseTimerRef.current = window.setTimeout(() => {
      collapseTimerRef.current = null
      autoCollapsedRef.current = true
      if (!userToggledRef.current && !hoverRef.current) setExpanded(false)
    }, 2500)
    return () => {
      if (collapseTimerRef.current !== null) {
        window.clearTimeout(collapseTimerRef.current)
        collapseTimerRef.current = null
      }
    }
  }, [doneInfo.done, doneInfo.success, totalChars])

  const handleCopy = async () => {
    try {
      const full = lines.map((l) => l.text).join('')
      await navigator.clipboard.writeText(full)
    } catch {
      // 复制失败静默；不要弹错打扰用户
    }
  }

  // 折叠时仅显示一行 header，省屏；展开时显示终端 body。
  // onMouseEnter：鼠标移入视为阅读意图，取消 pending 的自动折叠。
  return (
    <div
      className="chain-segment chain-command-stream"
      onMouseEnter={() => {
        hoverRef.current = true
        if (collapseTimerRef.current !== null) {
          window.clearTimeout(collapseTimerRef.current)
          collapseTimerRef.current = null
          autoCollapsedRef.current = true // 本次不再重排折叠
        }
      }}
      onMouseLeave={() => { hoverRef.current = false }}
    >
      <button
        type="button"
        className={`chain-tool-header-unified chain-command-stream-header${
          doneInfo.done ? ' chain-command-stream-header-done' : ' chain-command-stream-header-live'
        }`}
        onClick={() => {
          userToggledRef.current = true
          setExpanded((v) => !v)
        }}
        title={meta.command || '执行命令'}
      >
        <span className="chain-tool-leading">
          <Terminal size={14} />
          <span className="chain-tool-name">
            {meta.shell ? `${meta.shell} ` : ''}
            {/* 缺 command 文本时不再裸露 toolu_bdrk_xxx 内部 ID——那是调试信息不是界面文案 */}
            <code className="chain-command-stream-cmd">{meta.command || '终端输出'}</code>
          </span>
        </span>
        <span className="chain-command-stream-meta">
          {doneInfo.done ? (
            <span
              className={`chain-command-stream-status${
                doneInfo.success ? ' is-success' : ' is-failed'
              }`}
            >
              {doneInfo.success ? '✓' : '✗'}
              {doneInfo.exitCode !== null ? ` exit ${doneInfo.exitCode}` : ''}
            </span>
          ) : (
            <span className="chain-command-stream-status is-running">running…</span>
          )}
          <span className="chain-command-stream-bytes">{totalChars} chars</span>
          <span
            role="button"
            tabIndex={0}
            className="chain-command-stream-copy"
            onClick={(e) => { e.stopPropagation(); void handleCopy() }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                e.stopPropagation()
                void handleCopy()
              }
            }}
            title="复制全部输出"
            aria-label="复制全部输出"
          >
            <Copy size={12} />
          </span>
          <span className="chain-tool-toggle">
            {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </span>
        </span>
      </button>

      {expanded && (
        <div ref={bodyRef} className="chain-command-stream-body">
          {lines.length === 0 ? (
            <div className="chain-command-stream-empty">
              {doneInfo.done ? '无输出' : '等待输出…'}
            </div>
          ) : (
            <pre className="chain-command-stream-pre">
              {lines.map((line, i) => (
                <span
                  key={i}
                  className={
                    line.stream === 'stderr'
                      ? 'chain-command-stream-stderr'
                      : 'chain-command-stream-stdout'
                  }
                >
                  {line.text}
                </span>
              ))}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
