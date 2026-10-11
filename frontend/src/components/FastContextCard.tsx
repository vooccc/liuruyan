import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronRight,
  Copy,
  Check,
  Tag,
  Clock,
} from 'lucide-react'
import type { ContentSegment } from '../api/chat'

/**
 * FastContextCard - 高性能语义代码检索与推演流式卡片。
 *
 * 核心交互与生命周期：
 * 1. 运行中（多轮动态执行）：
 *    - 卡片默认展开。
 *    - 显示当前轮次的 Thought 自然段落。
 *    - 下方列出当前轮次的所有步骤（初始为时钟图标 🕒 Grepping ... in .）。
 *    - 步骤执行完毕后图标转为实心绿色对勾 ✔ + 耗时（如 0.5s）。
 *    - 每轮步骤全部变绿勾后平滑向上收起（Roll-up），并展开下一轮的推演与步骤。
 * 2. 检索完成（终态）：
 *    - 整体停留 800ms 后自动平滑收缩为一个单行精简条目（Header 栏展示命中数量、轮次与耗时）。
 *    - 用户点击此折叠条后手动展开：
 *      - 顶部优先展示「命中代码位置列表」（文件路径、Lxx-xx 行号标签、原因、一键复制路径）。
 *      - 下方提供可回溯查看的各轮推演与搜索步骤流明细、关键词标签。
 */

export interface FastContextStep {
  action: string
  command: string
  duration?: number
  round?: number
  status?: 'running' | 'done' | string
}

export interface FastContextRound {
  round: number
  thought?: string
  thought_duration?: number
  steps: FastContextStep[]
  isCompleted?: boolean
}

export interface FastContextResult {
  path: string
  full_path?: string
  ranges?: number[][]
  reason?: string
  hits?: number
  matched_symbol?: string
}

export interface FastContextPayload {
  search_id?: string
  query?: string
  status?: string
  round?: number
  thought?: string
  thought_duration?: number
  action?: string
  command?: string
  duration?: number
  done?: boolean
  results?: FastContextResult[]
  keywords?: string[]
  steps?: FastContextStep[]
  rounds?: FastContextRound[]
  total_duration?: number
}

/** 清理 protobuf 二进制残余字符（\\x12\\x04 等豆腐块）与不可见控制字符，标准化列表方块字符为圆点 */
export function cleanThoughtText(text?: string): string {
  if (!text) return ''
  return text
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\uFFFD]/g, '')
    .replace(/[\u25a0-\u25ff\u2b12-\u2b1b]/g, '')
    .trim()
}

/** 提取 tool_call.arguments.query */
export function pickFastContextQuery(seg: ContentSegment): string {
  const a1 = (seg.arguments as { query?: unknown } | undefined)?.query
  if (typeof a1 === 'string' && a1) return a1
  const a2 = (seg.toolCall?.input as { query?: unknown } | undefined)?.query
  if (typeof a2 === 'string' && a2) return a2
  if (typeof seg.arguments === 'string') {
    try {
      const obj = JSON.parse(seg.arguments)
      if (obj && typeof obj.query === 'string') return obj.query
      if (obj && typeof obj.pattern === 'string') return obj.pattern
    } catch { /* ignore */ }
  }
  const a3 = (seg.arguments as { pattern?: unknown } | undefined)?.pattern
  if (typeof a3 === 'string' && a3) return a3
  return ''
}

/** 从 tool_result 或 tool_call 中提取 search_id */
export function pickFastContextSearchIdFromResult(resultSegment?: ContentSegment | null): string | null {
  if (!resultSegment) return null
  const raw = resultSegment.result ?? resultSegment.content ?? ''
  let obj: any = null
  if (typeof raw === 'string') {
    try {
      obj = JSON.parse(raw)
    } catch {
      const m = raw.match(/"search_id"\s*:\s*["']([^"']+)["']/)
      if (m) return m[1]
    }
  } else if (raw && typeof raw === 'object') {
    obj = raw
  }
  if (obj && typeof obj === 'object') {
    if (typeof obj.search_id === 'string' && obj.search_id) return obj.search_id
    if (typeof obj._meta?.search_id === 'string' && obj._meta.search_id) return obj._meta.search_id
  }
  return null
}

/** 找出跟 tool_call 关联的 live fast_context_chunk segments */
export function findLiveChunksForFastContext(
  segments: ContentSegment[],
  toolCallSegment: ContentSegment,
  resultSegment?: ContentSegment | null,
): ContentSegment[] {
  const queryText = pickFastContextQuery(toolCallSegment).trim()
  const resultSearchId = pickFastContextSearchIdFromResult(resultSegment)

  // 1. 在 segments 列表中定位当前 tool_call 的下标位置
  let callIdx = segments.indexOf(toolCallSegment)
  if (callIdx === -1) {
    callIdx = segments.findIndex((s) => {
      if (s === toolCallSegment) return true
      if (s.id != null && toolCallSegment.id != null && s.id === toolCallSegment.id) return true
      if (s.tool_use_id && toolCallSegment.tool_use_id && s.tool_use_id === toolCallSegment.tool_use_id) return true
      return false
    })
  }

  // 2. 如果找到 callIdx，严格在本次 tool_call 的生命周期物理区间内收集
  if (callIdx !== -1) {
    // 计算本次 tool_call 作用域的右边界：遇到下一个 tool_call 即刻截止
    let endIdx = segments.length
    for (let i = callIdx + 1; i < segments.length; i++) {
      const s = segments[i]
      const isNextToolCall = s.type === 'tool_call' || Boolean(s.toolCall) || (Boolean(s.tool_name) && s.type !== 'system_notice')
      if (isNextToolCall) {
        endIdx = i
        break
      }
    }

    const candidateChunks: ContentSegment[] = []
    let boundSearchId: string | null = resultSearchId

    for (let i = callIdx + 1; i < endIdx; i++) {
      const s = segments[i]
      if (s.type !== 'system_notice' || s.kind !== 'fast_context_chunk') continue
      const p = s.payload as FastContextPayload | undefined
      if (!p) continue

      // 若尚未绑定 search_id，以区间内出现的首个 search_id 锁定
      if (!boundSearchId && p.search_id) {
        boundSearchId = p.search_id
      }

      // 存在 search_id 时必须严格一致，杜绝跨实例污染
      if (boundSearchId && p.search_id && p.search_id !== boundSearchId) {
        continue
      }

      // 双方均有 query 时必须严格匹配
      if (queryText && p.query && p.query.trim() !== queryText) {
        continue
      }

      candidateChunks.push(s)
    }

    if (candidateChunks.length > 0) {
      return candidateChunks
    }
  }

  // 3. Fallback：如果无法从 segments 获取下标，根据 search_id 或 query 精确过滤
  if (resultSearchId) {
    return segments.filter((s) => {
      if (s.type !== 'system_notice' || s.kind !== 'fast_context_chunk') return false
      const p = s.payload as FastContextPayload | undefined
      return Boolean(p && p.search_id === resultSearchId)
    })
  }

  if (queryText) {
    return segments.filter((s) => {
      if (s.type !== 'system_notice' || s.kind !== 'fast_context_chunk') return false
      const p = s.payload as FastContextPayload | undefined
      return Boolean(p && p.query && p.query.trim() === queryText)
    })
  }

  // 严禁无脑 return true，避免任何无归属 chunk 窜入历史卡片
  return []
}

/** 从 tool_result 中提取历史重放数据 */
export function buildSyntheticChunksForFastContext(
  toolCallSegment: ContentSegment,
  resultSegment: ContentSegment | undefined | null,
): ContentSegment[] {
  if (!resultSegment) return []
  const query = pickFastContextQuery(toolCallSegment)

  let raw: any = resultSegment.result ?? resultSegment.content ?? ''
  let obj: any = null

  if (typeof raw === 'string') {
    try {
      obj = JSON.parse(raw)
    } catch {
      // 容错：若 JSON 被意外截断，尝试提取 JSON 中的 results 数组片段
      const resultsMatch = raw.match(/"results"\s*:\s*(\[[^\]]*\])/)
      if (resultsMatch) {
        try {
          const parsedResults = JSON.parse(resultsMatch[1])
          obj = { results: parsedResults }
        } catch { /* ignore */ }
      }
    }
  } else if (raw && typeof raw === 'object') {
    obj = raw
  }

  // 合并 obj 与 obj._meta（无论 results 在顶级还是在 _meta 内均能完整提取）
  const meta: any = {
    ...(obj && typeof obj === 'object' ? obj : {}),
    ...((obj && typeof obj === 'object' && obj._meta && typeof obj._meta === 'object') ? obj._meta : {}),
  }

  // 提取优先级：顶级非空数组 > _meta 非空数组 > 兜底属性
  const candidateResults: FastContextResult[] = (Array.isArray(obj?.results) && obj.results.length > 0)
    ? obj.results
    : (Array.isArray(obj?._meta?.results) && obj._meta.results.length > 0)
    ? obj._meta.results
    : (Array.isArray(meta.results) && meta.results.length > 0)
    ? meta.results
    : []

  const candidateSteps = (Array.isArray(obj?.steps) && obj.steps.length > 0)
    ? obj.steps
    : (Array.isArray(obj?._meta?.steps) && obj._meta.steps.length > 0)
    ? obj._meta.steps
    : (Array.isArray(meta.steps) && meta.steps.length > 0)
    ? meta.steps
    : []

  let candidateRounds = (Array.isArray(obj?.rounds) && obj.rounds.length > 0)
    ? obj.rounds
    : (Array.isArray(obj?._meta?.rounds) && obj._meta.rounds.length > 0)
    ? obj._meta.rounds
    : (Array.isArray(meta.rounds) && meta.rounds.length > 0)
    ? meta.rounds
    : []

  const candidateKeywords = (Array.isArray(obj?.keywords) && obj.keywords.length > 0)
    ? obj.keywords
    : (Array.isArray(obj?._meta?.keywords) && obj._meta.keywords.length > 0)
    ? obj._meta.keywords
    : (Array.isArray(meta.keywords) && meta.keywords.length > 0)
    ? meta.keywords
    : []

  // 纯文本 summary 解析兜底 (如 "1. `path/to/file.ts` (L10-50): ...")
  let finalResults = candidateResults
  if (finalResults.length === 0) {
    const textToScan = typeof raw === 'string'
      ? raw
      : (typeof obj?.summary === 'string' ? obj.summary : '')
    if (textToScan) {
      const textResults: FastContextResult[] = []
      const lineRegex = /(?:^|\n)\s*(?:\d+\.|\*|-)\s*[`']?([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+)[`']?(?:\s*\(?(?:L)?(\d+)(?:-(\d+))?\)?)*(?:\s*[-:]\s*([^\n]+))?/g
      for (const m of textToScan.matchAll(lineRegex)) {
        const fpath = m[1].replace(/^[\\/]/, '')
        if (anyEndsWith(fpath, ['.ts', '.tsx', '.js', '.jsx', '.py', '.json', '.css', '.html', '.md', '.vue', '.rs', '.go'])) {
          const r1 = m[2] ? parseInt(m[2], 10) : 1
          const r2 = m[3] ? parseInt(m[3], 10) : (r1 + 60)
          const reason = m[4]?.trim() || 'Matched location'
          textResults.push({ path: fpath, ranges: [[r1, r2]], reason })
        }
      }
      if (textResults.length > 0) {
        finalResults = textResults
      }
    }
  }

  // 若只有 steps 没有 rounds，自动补齐单轮结构用于多轮流程展示
  if (candidateRounds.length === 0 && candidateSteps.length > 0) {
    candidateRounds = [{
      round: 1,
      thought: cleanThoughtText(meta.thought),
      thought_duration: meta.thought_duration,
      steps: [...candidateSteps],
      isCompleted: true,
    }]
  } else if (candidateRounds.length > 0) {
    candidateRounds = candidateRounds
      .map((r: any, idx: number) => ({
        ...r,
        round: r.round || idx + 1,
        thought: cleanThoughtText(r.thought),
        steps: Array.isArray(r.steps) ? [...r.steps] : [],
        isCompleted: true,
      }))
      .filter((r: any) => r.steps.length > 0 || Boolean(r.thought))
  }

  const syntheticChunk: ContentSegment = {
    id: 0,
    type: 'system_notice',
    kind: 'fast_context_chunk',
    content: '',
    payload: {
      query: meta.query || query,
      done: true,
      status: 'completed',
      thought: cleanThoughtText(meta.thought),
      thought_duration: meta.thought_duration,
      steps: [...candidateSteps],
      rounds: candidateRounds,
      results: finalResults,
      keywords: candidateKeywords,
      total_duration: meta.total_duration,
    },
  }

  return [syntheticChunk]
}

function anyEndsWith(str: string, exts: string[]): boolean {
  return exts.some((e) => str.endsWith(e))
}

/** 解析匹配原因中的关键词与命中频次 (例如: Directly matched symbol 'attachment' (12 hits)) */
export function parseResultReason(reason?: string): {
  keyword?: string
  hits?: number
  cleanDetail?: string
} {
  if (!reason) return {}
  const wordMatch = reason.match(/['"]([^'"]+)['"]/)
  const keyword = wordMatch ? wordMatch[1] : undefined

  // 匹配 hits 数字：优先从 (X hits) 或 X hits 提取，其次从 X search occurrences 提取
  let hits: number | undefined
  const hitsMatch = reason.match(/\((\d+)\s*hits?\)/i) || reason.match(/(\d+)\s*hits?/i)
  if (hitsMatch) {
    hits = parseInt(hitsMatch[1], 10)
  } else {
    const occMatch = reason.match(/(\d+)\s*search\s*occurrences?/i)
    if (occMatch) {
      hits = parseInt(occMatch[1], 10)
    }
  }

  // 过滤清洗掉内部机械兜底文本 (如 Matched 2 search occurrences 等)，保留真实业务描述
  let cleanDetail = reason
  cleanDetail = cleanDetail.replace(/Directly matched symbol\s*['"][^'"]*['"]\s*(?:\(\d+\s*hits?\))?\s*(?:-\s*)?/gi, '')
  cleanDetail = cleanDetail.replace(/Matched\s*\d+\s*search\s*occurrences?\s*(?:in codebase)?/gi, '')
  cleanDetail = cleanDetail.replace(/\(\d+\s*hits?\)/gi, '')
  cleanDetail = cleanDetail.replace(/^[-\s:·]+/, '').replace(/[-\s:·]+$/, '').trim()

  return { keyword, hits, cleanDetail: cleanDetail || undefined }
}

/** Devin 风格文件扩展名小徽标 (如 py, js, ts, mdc 等) */
export function renderFileBadge(path: string) {
  const extMatch = path.match(/\.([a-zA-Z0-9]+)$/)
  const ext = (extMatch ? extMatch[1] : '').toLowerCase()

  let label = ext.toUpperCase() || 'FILE'
  let colorClass = 'fc-badge-default'

  if (ext === 'py') {
    label = 'py'
    colorClass = 'fc-badge-python'
  } else if (ext === 'js' || ext === 'mjs' || ext === 'cjs') {
    label = 'js'
    colorClass = 'fc-badge-js'
  } else if (ext === 'ts' || ext === 'mts' || ext === 'cts') {
    label = 'ts'
    colorClass = 'fc-badge-ts'
  } else if (ext === 'tsx' || ext === 'jsx') {
    label = ext
    colorClass = 'fc-badge-react'
  } else if (ext === 'md' || ext === 'mdc' || ext === 'markdown') {
    label = 'md'
    colorClass = 'fc-badge-md'
  } else if (ext === 'json') {
    label = '{}'
    colorClass = 'fc-badge-json'
  } else if (ext === 'css' || ext === 'scss' || ext === 'less') {
    label = 'css'
    colorClass = 'fc-badge-css'
  } else if (ext === 'html' || ext === 'htm') {
    label = 'html'
    colorClass = 'fc-badge-html'
  } else if (ext === 'rs') {
    label = 'rs'
    colorClass = 'fc-badge-rust'
  } else if (ext === 'go') {
    label = 'go'
    colorClass = 'fc-badge-go'
  }

  return <span className={`fc-file-badge ${colorClass}`}>{label}</span>
}

/** Devin 1:1 精致双筒望远镜图标 */
function BinocularsIcon({ size = 16, className = '' }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={Math.round(size * 0.88)}
      viewBox="0 0 20 17"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={{ display: 'inline-block', verticalAlign: 'middle' }}
    >
      {/* 左物镜 */}
      <circle cx="5.5" cy="11.5" r="4" />
      {/* 右物镜 */}
      <circle cx="14.5" cy="11.5" r="4" />
      {/* 左镜筒 */}
      <path d="M3.2 8L4.2 2H6.8L7.8 8" />
      {/* 右镜筒 */}
      <path d="M12.2 8L13.2 2H15.8L16.8 8" />
      {/* 中轴横梁 */}
      <path d="M7.8 5.2H12.2" />
      <rect x="9.2" y="3.8" width="1.6" height="2.8" rx="0.5" fill="currentColor" />
    </svg>
  )
}

interface FastContextCardProps {
  searchId?: string
  query?: string
  chunks: ContentSegment[]
  toolCallSegment?: ContentSegment
  resultSegment?: ContentSegment | null
  expanded?: boolean
  onToggle?: () => void
}

export function FastContextCard({
  chunks,
  toolCallSegment,
  resultSegment,
  onToggle,
}: FastContextCardProps) {
  // 解析多轮与汇总数据
  const state = useMemo(() => {
    const defaultQuery = toolCallSegment ? pickFastContextQuery(toolCallSegment) : ''
    let activeQuery = defaultQuery
    let finalThought: string | undefined
    let isDone = Boolean(resultSegment)
    let results: FastContextResult[] = []
    let keywords: string[] = []
    let totalDuration: number | undefined

    const roundsMap = new Map<number, FastContextRound>()

    // 辅助函数：确保对应轮次结构存在
    const ensureRound = (rNum: number): FastContextRound => {
      let r = roundsMap.get(rNum)
      if (!r) {
        r = { round: rNum, steps: [], isCompleted: false }
        roundsMap.set(rNum, r)
      }
      return r
    }

    const boundCardSearchId = pickFastContextSearchIdFromResult(resultSegment)

    for (const chunk of chunks) {
      const p = chunk.payload as FastContextPayload | undefined
      if (!p) continue

      // 若卡片已锁定 search_id，忽略其它实例的杂散数据
      if (boundCardSearchId && p.search_id && p.search_id !== boundCardSearchId) {
        continue
      }
      if (activeQuery && p.query && p.query.trim() !== activeQuery.trim()) {
        continue
      }

      if (p.query) activeQuery = p.query
      if (p.done) isDone = true
      if (p.total_duration) totalDuration = p.total_duration

      // 如果 payload 直接携带了完整 rounds 历史（如重放）
      if (Array.isArray(p.rounds) && p.rounds.length > 0) {
        for (const rnd of p.rounds) {
          const cleanT = cleanThoughtText(rnd.thought)
          const rSteps = Array.isArray(rnd.steps) ? [...rnd.steps] : []
          if (rSteps.length > 0 || cleanT) {
            const rObj = ensureRound(rnd.round)
            rObj.thought = cleanT
            rObj.thought_duration = rnd.thought_duration
            rObj.steps = rSteps
            rObj.isCompleted = true
          }
        }
      }

      const rNum = p.round || 1
      const curRound = ensureRound(rNum)

      if (p.thought) {
        const cleaned = cleanThoughtText(p.thought)
        if (cleaned) {
          curRound.thought = cleaned
          finalThought = cleaned
        }
      }
      if (p.thought_duration) curRound.thought_duration = p.thought_duration
      if (p.status === 'round_done') {
        curRound.isCompleted = true
        curRound.steps = curRound.steps.map((s) => ({ ...s, status: 'done' }))
        for (const [rIdx, r] of roundsMap.entries()) {
          if (rIdx < rNum) {
            r.isCompleted = true
          }
        }
      }

      // 当出现下一轮数据 (rNum > 1) 时，先前所有历史轮次必然已经执行完毕
      if (rNum > 1) {
        for (const [rIdx, r] of roundsMap.entries()) {
          if (rIdx < rNum) {
            r.isCompleted = true
          }
        }
      }

      // 增量步骤事件处理
      if (p.action && p.command) {
        const cmdText = p.command
        const existingIdx = curRound.steps.findIndex((s) => s.command === cmdText)
        const stepEntry: FastContextStep = {
          action: p.action,
          command: cmdText,
          duration: p.duration,
          round: rNum,
          status: (p.status as 'running' | 'done') || 'done',
        }

        const nextSteps = [...curRound.steps]
        if (existingIdx >= 0) {
          nextSteps[existingIdx] = stepEntry
        } else {
          nextSteps.push(stepEntry)
        }
        curRound.steps = nextSteps
      }

      // 整体 results
      if (Array.isArray(p.results) && p.results.length > 0) {
        results = p.results
      }
      if (Array.isArray(p.keywords) && p.keywords.length > 0) {
        keywords = p.keywords
      }
    }

    // 从 resultSegment 反解兜底（以 tool_result 权威结果为准）
    if (resultSegment) {
      isDone = true
      let raw: any = resultSegment.result ?? resultSegment.content ?? ''
      let obj: any = null
      if (typeof raw === 'string') {
        try { obj = JSON.parse(raw) } catch { /* ignore */ }
      } else if (raw && typeof raw === 'object') {
        obj = raw
      }

      if (obj && typeof obj === 'object') {
        const meta = { ...obj, ...(obj._meta || {}) }
        const objResults = (Array.isArray(obj.results) && obj.results.length > 0)
          ? obj.results
          : (Array.isArray(obj._meta?.results) && obj._meta.results.length > 0)
          ? obj._meta.results
          : (Array.isArray(meta.results) && meta.results.length > 0)
          ? meta.results
          : []
        const hasExplicitResults = Array.isArray(obj.results) || Array.isArray(obj._meta?.results) || Array.isArray(meta.results)
        if (hasExplicitResults) {
          results = objResults
        } else if (objResults.length > 0 && results.length === 0) {
          results = objResults
        }

        const objKeywords = (Array.isArray(obj.keywords) && obj.keywords.length > 0)
          ? obj.keywords
          : (Array.isArray(obj._meta?.keywords) && obj._meta.keywords.length > 0)
          ? obj._meta.keywords
          : (Array.isArray(meta.keywords) && meta.keywords.length > 0)
          ? meta.keywords
          : []
        const hasExplicitKeywords = Array.isArray(obj.keywords) || Array.isArray(obj._meta?.keywords) || Array.isArray(meta.keywords)
        if (hasExplicitKeywords) {
          keywords = objKeywords
        } else if (objKeywords.length > 0 && keywords.length === 0) {
          keywords = objKeywords
        }

        if (meta.total_duration && !totalDuration) totalDuration = meta.total_duration
        if (meta.thought && !finalThought) finalThought = cleanThoughtText(meta.thought)

        const objRounds = (Array.isArray(obj.rounds) && obj.rounds.length > 0)
          ? obj.rounds
          : (Array.isArray(obj._meta?.rounds) && obj._meta.rounds.length > 0)
          ? obj._meta.rounds
          : (Array.isArray(meta.rounds) && meta.rounds.length > 0)
          ? meta.rounds
          : []
        const objSteps = (Array.isArray(obj.steps) && obj.steps.length > 0)
          ? obj.steps
          : (Array.isArray(obj._meta?.steps) && obj._meta.steps.length > 0)
          ? obj._meta.steps
          : (Array.isArray(meta.steps) && meta.steps.length > 0)
          ? meta.steps
          : []

        if (objRounds.length > 0 && roundsMap.size === 0) {
          for (const rnd of objRounds) {
            const cleanT = cleanThoughtText(rnd.thought)
            const rSteps = Array.isArray(rnd.steps) ? [...rnd.steps] : []
            if (rSteps.length > 0 || cleanT) {
              const rObj = ensureRound(rnd.round)
              rObj.thought = cleanT
              rObj.thought_duration = rnd.thought_duration
              rObj.steps = rSteps
              rObj.isCompleted = true
            }
          }
        } else if (objSteps.length > 0 && roundsMap.size === 0) {
          const rObj = ensureRound(1)
          rObj.thought = cleanThoughtText(meta.thought)
          rObj.steps = Array.isArray(objSteps) ? [...objSteps] : []
          rObj.isCompleted = true
        }
      }
    }

    const rawRounds = Array.from(roundsMap.values())
      .map((r) => ({
        ...r,
        steps: Array.isArray(r.steps) ? [...r.steps] : [],
        thought: cleanThoughtText(r.thought),
      }))

    if (isDone) {
      for (const r of rawRounds) {
        r.isCompleted = true
        r.steps = r.steps.map((s) => ({ ...s, status: 'done' }))
      }
    }

    const maxRoundInMap = rawRounds.length > 0
      ? Math.max(...rawRounds.map((r) => r.round))
      : 1

    const sortedRounds = rawRounds
      .filter((r) => {
        // 过滤幽灵轮：必须有至少 1 个步骤，或者处于运行中的当前轮且包含有效思考文字
        if (r.steps.length > 0) return true
        if (!isDone && r.round === maxRoundInMap && r.thought) {
          const meaningful = r.thought.replace(/[^a-zA-Z0-9\u4e00-\u9fa5]/g, '')
          return meaningful.length >= 2
        }
        return false
      })
      .sort((a, b) => a.round - b.round)

    const activeRoundNum = sortedRounds.length > 0
      ? sortedRounds[sortedRounds.length - 1].round
      : maxRoundInMap

    return {
      query: activeQuery,
      thought: finalThought,
      isDone,
      results,
      keywords,
      totalDuration,
      rounds: sortedRounds,
      activeRoundNum,
    }
  }, [chunks, toolCallSegment, resultSegment])

  // 识别是否为首次挂载即已完成的历史回放（如刷新或查看历史消息）
  const isInitialReplay = useRef<boolean>(state.isDone)

  // 卡片展开/收起状态机
  const [isCardExpanded, setIsCardExpanded] = useState<boolean>(!state.isDone)
  const [userManuallyToggled, setUserManuallyToggled] = useState<boolean>(false)
  const [copiedPath, setCopiedPath] = useState<string | null>(null)
  const [expandedPreviousRounds, setExpandedPreviousRounds] = useState<Record<number, boolean>>({})

  // 轮次流转串行状态机
  // expandedRound: 当前被展开显示的轮次编号，null 表示当前所有轮均收起（或在轮次切换间隙）
  const [expandedRound, setExpandedRound] = useState<number | null>(
    isInitialReplay.current ? null : 1
  )
  // 是否允许展示结果代码列表（实时搜索时等最后一轮收起后再亮相）
  const [showResultsSection, setShowResultsSection] = useState<boolean>(
    isInitialReplay.current
  )

  const prevRoundRef = useRef<number>(state.activeRoundNum)

  // 1. 跨轮次串行流转：当 activeRoundNum 推进（如 1 -> 2 或 2 -> 3）
  useEffect(() => {
    if (isInitialReplay.current) return

    if (state.activeRoundNum > prevRoundRef.current && !state.isDone) {
      const nextRound = state.activeRoundNum
      prevRoundRef.current = nextRound

      // 阶段 1：先收起上一轮（向上卷起 280ms）
      setExpandedRound(null)

      // 阶段 2：待上一轮收起卷入其标题栏后，再顺畅展开下一轮
      const expandTimer = setTimeout(() => {
        setExpandedRound(nextRound)
      }, 300)

      return () => clearTimeout(expandTimer)
    }
  }, [state.activeRoundNum, state.isDone])

  // 2. 搜索完成终态流转（最后一轮正常收拢，随后整卡平滑折叠）
  const finishHandledRef = useRef<boolean>(false)

  useEffect(() => {
    if (isInitialReplay.current) return
    if (!state.isDone || finishHandledRef.current) return

    finishHandledRef.current = true
    const finalRound = state.activeRoundNum

    // 确保最后一轮展开并确认步骤绿勾
    setExpandedRound(finalRound)

    // 350ms 后：最后一轮正常向上收折归档（280ms 动画）
    const stowFinalTimer = setTimeout(() => {
      setExpandedRound(null)
    }, 350)

    // 680ms 后：最后一轮完全卷入标题栏，随后整卡顺畅折叠为单行精简状态条
    const autoCollapseTimer = setTimeout(() => {
      setShowResultsSection(true)
      if (!userManuallyToggled) {
        setIsCardExpanded(false)
      }
    }, 680)

    return () => {
      clearTimeout(stowFinalTimer)
      clearTimeout(autoCollapseTimer)
    }
  }, [state.isDone, state.activeRoundNum, userManuallyToggled])

  const cardRef = useRef<HTMLDivElement | null>(null)
  const userScrolledAwayRef = useRef<boolean>(false)

  // 3. 实时跟踪视口底部（解决“第二轮第三轮往下展开时屏幕看不到了，没有一直追踪底部”）：
  // 利用 ResizeObserver 监听卡片高度变动（无论是轮次平滑展开动画、新步骤行实时插入、还是结果列表淡入），
  // 在用户没有主动上滑翻看历史的前提下，将聊天视口实时平滑锚定到底部。
  useEffect(() => {
    if (isInitialReplay.current) return
    const el = cardRef.current
    if (!el) return

    const container = el.closest<HTMLElement>('.messages-container, .collab-messages-container') || el.parentElement
    if (!container) return

    const handleWheel = (e: WheelEvent) => {
      // 用户滚轮向上滑：判定为主动查看历史消息，暂时停止自动贴底
      if (e.deltaY < -5) {
        userScrolledAwayRef.current = true
      }
    }

    const handleScroll = () => {
      const dist = container.scrollHeight - container.clientHeight - container.scrollTop
      // 用户滚回底部附近（<= 60px），重新恢复自动跟底
      if (dist <= 60) {
        userScrolledAwayRef.current = false
      }
    }

    container.addEventListener('wheel', handleWheel, { passive: true })
    container.addEventListener('scroll', handleScroll, { passive: true })

    const ro = new ResizeObserver(() => {
      if (userScrolledAwayRef.current) return
      const dist = container.scrollHeight - container.clientHeight - container.scrollTop
      // 只要处于底部跟随范围内（< 400px），卡片每次展开增长都紧密贴底
      if (dist < 400) {
        container.scrollTop = container.scrollHeight
      }
    })

    ro.observe(el)

    return () => {
      ro.disconnect()
      container.removeEventListener('wheel', handleWheel)
      container.removeEventListener('scroll', handleScroll)
    }
  }, [])

  // 每当轮次切换激活（如 1 -> 2 或 2 -> 3）或步骤更新时，立即确保锚定一次
  useEffect(() => {
    if (isInitialReplay.current || expandedRound === null) return
    const el = cardRef.current
    if (!el) return
    const container = el.closest<HTMLElement>('.messages-container, .collab-messages-container') || el.parentElement
    if (!container || userScrolledAwayRef.current) return
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight
    })
  }, [expandedRound, state.rounds])

  const handleHeaderClick = () => {
    setUserManuallyToggled(true)
    setIsCardExpanded((prev) => !prev)
    if (onToggle) {
      onToggle()
    }
  }

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text)
    setCopiedPath(text)
    setTimeout(() => setCopiedPath(null), 1800)
  }

  const toggleRoundCollapse = (rNum: number) => {
    setExpandedPreviousRounds((prev) => ({
      ...prev,
      [rNum]: !prev[rNum],
    }))
  }

  return (
    <div ref={cardRef} className="fast-context-card">
      {/* ── 1. Header 栏（Devin 1:1 极简一体化排版）── */}
      <div
        onClick={handleHeaderClick}
        className="fc-header"
        title="点击折叠/展开 Fast Context 检索详情"
      >
        <div className="fc-header-left">
          <span className="fc-icon-wrap">
            <BinocularsIcon size={16} />
          </span>
          <span className="fc-brand-title">Fast Context</span>
          <span className="fc-query-badge" title={state.query}>
            {state.query || 'Searching codebase...'}
          </span>
          {state.isDone && (
            <span className="fc-meta-text">
              {state.results.length > 0
                ? `${state.totalDuration ? `in ${state.totalDuration}s` : ''}`
                : `(未命中${state.totalDuration ? ` · ${state.totalDuration}s` : ''})`}
            </span>
          )}
        </div>

        <div className="fc-header-right">
          {!state.isDone && (
            <span className="fc-status-badge">
              <span className="fc-step-clock" style={{ display: 'inline-flex' }}>
                <Clock size={12} />
              </span>
              <span>Searching</span>
            </span>
          )}

          <span
            className="fc-chevron"
            style={{
              transform: isCardExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
              transition: 'transform 0.2s ease',
            }}
          >
            <ChevronRight size={14} strokeWidth={1.75} />
          </span>
        </div>
      </div>

      {/* ── 2. 平滑折叠抽屉（CSS Grid 硬件加速平滑收缩展开）── */}
      <div className={`fc-collapse-wrapper ${isCardExpanded ? 'is-expanded' : ''}`}>
        <div className="fc-collapse-inner">
          <div className="fc-body">
            {/* 
              搜索完成态 (isDone)：
              1. 命中时：Devin 风格单行代码定位列表（图标 + 文件名 + 行号 + 右侧高亮词及命中频次）
              2. 未命中时：展示友好的轻量提示卡片
              仅在最后一轮收起完毕后亮相 (showResultsSection)，保证最后一轮执行过程充分展示
            */}
            {showResultsSection && state.results.length > 0 && (
              <div className="fc-results-section">
                <div className="fc-results-list">
                  {state.results.map((res, idx) => {
                    const rangeStr = res.ranges && res.ranges.length > 0
                      ? res.ranges.map(([s, e]) => `L${s}-${e}`).join(', ')
                      : ''
                    const copyTarget = res.full_path || res.path
                    const parsed = parseResultReason(res.reason)

                    // 确定性 hits：优先读取显式 hits，其次解析 reason，最后根据代码区间智能推算保底
                    let finalHits = res.hits !== undefined ? res.hits : parsed.hits
                    if (finalHits === undefined || finalHits <= 0) {
                      if (res.ranges && res.ranges.length > 0) {
                        const span = res.ranges.reduce((acc, [s, e]) => acc + Math.max(1, e - s), 0)
                        finalHits = Math.max(1, Math.min(16, Math.round(span / 8)))
                      } else {
                        finalHits = 1
                      }
                    }

                    // 提取关键词：优先 matched_symbol，其次 reason 中的 'keyword'，最后 query 中的核心词
                    const rawWord = parsed.keyword || res.matched_symbol
                    const fallbackQueryWord = state.query
                      ? (state.query.match(/[a-zA-Z_][a-zA-Z0-9_\-.]{2,}/)?.[0] || state.query.trim().split(/\s+/)[0])
                      : 'symbol'
                    const symbolToken = rawWord || fallbackQueryWord

                    // 构造统一定义的悬浮提示：绝对消除前后数字冲突与重复拼接
                    let hoverClue = `Directly matched symbol '${symbolToken}' (${finalHits} ${finalHits === 1 ? 'hit' : 'hits'})`
                    if (parsed.cleanDetail) {
                      hoverClue += `\n详细分析: ${parsed.cleanDetail}`
                    }

                    return (
                      <div
                        key={idx}
                        className="fc-result-item"
                        onClick={() => copyToClipboard(copyTarget)}
                        title={`路径: ${copyTarget}${rangeStr ? ` (${rangeStr})` : ''}\n匹配线索: ${hoverClue}`}
                      >
                        {/* 左侧：文件类型徽标 + 文件名 + 浅灰行号 */}
                        <div className="fc-result-left">
                          {renderFileBadge(res.path)}
                          <span className="fc-result-name" title={res.path}>
                            {res.path}
                          </span>
                          {rangeStr && (
                            <span className="fc-result-range">{rangeStr}</span>
                          )}
                        </div>

                        {/* 右侧（方案2：Devin 原生极简对齐，稳定展示频次，悬浮展示完整高亮） */}
                        <div className="fc-result-right">
                          <span
                            className="fc-hits-text"
                            title={res.reason ? `${res.reason}\n(${finalHits} search hits)` : `${finalHits} search hits`}
                          >
                            {finalHits} {finalHits === 1 ? 'hit' : 'hits'}
                          </span>

                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation()
                              copyToClipboard(copyTarget)
                            }}
                            className="fc-copy-btn"
                            title="复制文件完整路径"
                          >
                            {copiedPath === copyTarget ? (
                              <Check size={12} style={{ color: '#10b981' }} />
                            ) : (
                              <Copy size={12} />
                            )}
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {/* 未命中时的提示卡片 */}
            {showResultsSection && state.results.length === 0 && (
              <div className="fc-empty-state">
                <div className="fc-empty-header">
                  <span>未定位到直接匹配的代码文件</span>
                </div>
                <div className="fc-empty-desc">
                  已完成多轮检索，可参考下方各轮推演及执行细节，或尝试输入更精准的符号、函数名或缩短查询词。
                </div>
              </div>
            )}

            {/* ── 3. 多轮推演与步骤流 (Rounds Stream，Devin 步进时间轴与风琴收缩) ── */}
            <div className="fc-rounds-stream">
              {state.rounds.map((rnd) => {
                const isRoundDone = rnd.isCompleted
                  || rnd.round < state.activeRoundNum
                  || state.isDone
                const isCurrentActive = !state.isDone && rnd.round === state.activeRoundNum && !isRoundDone
                const isRoundExpanded = expandedPreviousRounds[rnd.round] !== undefined
                  ? expandedPreviousRounds[rnd.round]
                  : rnd.round === expandedRound

                const roundStepDur = rnd.steps.reduce((acc, s) => acc + (s.duration || 0), 0)
                const totalRoundDur = (rnd.thought_duration || 0) + roundStepDur
                const durText = totalRoundDur > 0
                  ? ` · ${totalRoundDur < 1 ? totalRoundDur.toFixed(2) : totalRoundDur.toFixed(1)}s`
                  : (rnd.thought_duration ? ` · ${rnd.thought_duration}s` : '')

                return (
                  <div
                    key={rnd.round}
                    className={`fc-round-container ${isCurrentActive ? 'is-active-round' : ''} ${isRoundDone ? 'is-completed-round' : ''}`}
                  >
                    {/* 步进标题栏 (Round N + 完成状态 / 呼吸中标签) */}
                    <div
                      onClick={() => toggleRoundCollapse(rnd.round)}
                      className={`fc-round-rollup ${isRoundExpanded ? 'is-expanded' : ''} ${isRoundDone ? 'is-completed' : ''}`}
                      title="点击展开/收起该轮推演与步骤"
                    >
                      <span
                        className="fc-round-chevron"
                        style={{
                          display: 'inline-flex',
                          transform: isRoundExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                          transition: 'transform 0.22s cubic-bezier(0.16, 1, 0.3, 1)',
                        }}
                      >
                        <ChevronRight size={13} />
                      </span>
                      <span className="fc-round-title">Round {rnd.round}</span>

                      {isRoundDone ? (
                        <span className="fc-round-completed-badge">
                          ✓ {rnd.steps.length > 0 ? `${rnd.steps.length} 步已完成` : '已完成'}{durText}
                        </span>
                      ) : isCurrentActive && !state.isDone ? (
                        <span className="fc-round-active-badge">
                          <span className="fc-round-pulse-dot" />
                          <span>正在检索</span>
                        </span>
                      ) : (
                        <span className="fc-round-steps-count">
                          {rnd.steps.length} 步
                        </span>
                      )}
                    </div>

                    {/* 展开内容：CSS Grid 硬件加速平滑风琴收缩展开 (向上折叠与向下展开) */}
                    <div className={`fc-round-body-wrapper ${isRoundExpanded ? 'is-expanded' : ''}`}>
                      <div className="fc-round-body-inner">
                        {cleanThoughtText(rnd.thought) && (
                          <div className="fc-thought-text">
                            {cleanThoughtText(rnd.thought)}
                          </div>
                        )}

                        {rnd.steps.length > 0 && (
                          <div className="fc-steps-list">
                            {rnd.steps.map((step, sIdx) => {
                              const isStepRunning = step.status === 'running'
                              return (
                                <div
                                  key={sIdx}
                                  className={`fc-step-row ${isStepRunning ? 'is-running' : ''}`}
                                  style={{ animationDelay: `${Math.min(sIdx * 45, 200)}ms` }}
                                >
                                  {isStepRunning ? (
                                    <span className="fc-step-clock">
                                      <Clock size={12.5} />
                                    </span>
                                  ) : (
                                    <span className="fc-step-check">✓</span>
                                  )}

                                  <span className="fc-step-action">{step.action}</span>
                                  <span className="fc-step-command" title={step.command}>
                                    {step.command}
                                  </span>

                                  {step.duration !== undefined && !isStepRunning && (
                                    <span className="fc-step-duration">{step.duration}s</span>
                                  )}
                                </div>
                              )
                            })}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>


            {/* 运行中微动指示 */}
            {!state.isDone && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  color: 'var(--text-muted)',
                  fontSize: '11.5px',
                  fontStyle: 'italic',
                  paddingLeft: '2px',
                }}
              >
                <span className="fc-step-clock" style={{ display: 'inline-flex' }}>
                  <Clock size={12} />
                </span>
                <span>Investigating codebase...</span>
              </div>
            )}

            {/* 建议关键词标签 */}
            {state.keywords.length > 0 && (
              <div className="fc-keywords-wrap">
                <Tag size={12} style={{ color: 'var(--text-muted)' }} />
                <span className="fc-kw-label">Keywords:</span>
                {state.keywords.map((kw, i) => (
                  <span key={i} className="fc-kw-pill">
                    {kw}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

