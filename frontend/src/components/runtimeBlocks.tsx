import { useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from 'react'
import { Brain, CheckCircle2, ChevronDown, ChevronRight, Info, Languages, Loader2, Wrench } from 'lucide-react'
import type { Agent, ContentSegment } from '../api/chat'
import { translateText } from '../api/utility'
import SubAgentCard from './SubAgentCard'
import AskUserQuestionCard, {
  type AskExecutionState,
  type AskUserAnswer,
  type AskUserQuestionPayload,
} from './AskUserQuestionCard'
import {
  BrowserPreviewCard,
  tryParseBrowserPreviewPayload,
} from './BrowserPreviewCard'
import {
  GeneratedImageCard,
  tryParseGeneratedImagePayload,
} from './GeneratedImageCard'
import {
  FileDiffCard,
  tryParseFileDiffPayload,
} from './FileDiffCard'
import {
  ReadFileCard,
  tryParseReadFilePayload,
} from './ReadFileCard'
import {
  CommandStreamCard,
  buildSyntheticChunksFromToolResult,
  findLiveChunksForToolCall,
} from './CommandStreamCard'
import {
  FastContextCard,
  buildSyntheticChunksForFastContext,
  findLiveChunksForFastContext,
} from './FastContextCard'
import {
  AcpDelegateCard,
  buildSyntheticChunksFromAcpResult,
  collectChunksForDelegate,
  findChunksForAcpDelegate,
  getAcpDelegateChunkId,
  isFirstChunkForDelegate,
} from './AcpDelegateCard'
import { getToolDisplaySummary } from './toolDisplayLabel'
import { buildToolStages, type ToolStage } from './toolConcurrency'

// 后端 ToolResultBudget（backend/tools/defense/budget.py）超 50000 字符会截断 +
// 追加这段中文 note 到末尾，导致前端看到的不再是合法 JSON。识别出来后剥离掉，
// 再对前段做"修复式 parse"。匹配前缀 \n\n[system] Tool result truncated...
const BUDGET_NOTE_RE = /\n*\[system\] Tool result truncated due to budget\.[\s\S]*$/

function getRuntimeNoticeTitle(kind?: string) {
  switch (kind) {
    case 'llm_provider_error': return '模型服务商错误'
    case 'llm_quota_exhausted': return '账户余额不足'
    case 'llm_auth_failed': return 'API Key 无效'
    case 'llm_rate_limited': return '请求频率超限'
    case 'llm_timeout': return '模型请求超时'
    case 'llm_context_overflow': return '上下文超出窗口'
    case 'llm_schema_error': return '工具定义错误'
    case 'llm_tool_unsupported': return '模型不支持工具'
    case 'llm_upstream_busy': return '模型服务繁忙'
    case 'llm_exception': return '模型调用出错'
    case 'llm_upstream_retry': return '模型繁忙，自动重试中'
    case 'max_tool_rounds': return '达到工具轮次上限'
    case 'repeated_identical_rounds': return '检测到重复回合'
    case 'aggregate_budget_warning': return '运行预算接近上限'
    case 'aggregate_tool_calls_exhausted': return '工具调用预算已耗尽'
    case 'aggregate_tool_output_exhausted': return '工具输出预算已耗尽'
    case 'aggregate_wall_clock_exhausted': return '运行时间预算已耗尽'
    default: return '系统通知'
  }
}

function getRuntimeNoticeTone(kind?: string) {
  switch (kind) {
    case 'aggregate_tool_calls_exhausted':
    case 'aggregate_tool_output_exhausted':
    case 'aggregate_wall_clock_exhausted':
      return 'danger'
    case 'aggregate_budget_warning':
    case 'llm_upstream_retry':
      return 'retry'
    default:
      return 'default'
  }
}

function normalizeRuntimeNoticeText(content?: string | null) {
  return String(content || '').trim()
}

function isContextCompactionNotice(kind?: string) {
  return kind === 'context_compaction_started' || kind === 'context_compaction_completed'
}

function ContextCompactionNotice({
  completed,
  content,
}: {
  completed: boolean
  content: string
}) {
  return (
    <div
      className={`context-compaction-status ${completed ? 'is-complete' : 'is-active'}`}
      role="status"
      aria-live="polite"
    >
      <span className="context-compaction-status__icon" aria-hidden>
        {completed
          ? <CheckCircle2 size={14} strokeWidth={2} />
          : <Loader2 size={14} strokeWidth={2} />}
      </span>
      <span className="context-compaction-status__label">
        {completed ? '上下文已整理' : '正在压缩上下文'}
      </span>
      <span className="context-compaction-status__detail">{content}</span>
    </div>
  )
}

// 工具结果常见两种形态：纯文本（read_file 文本内容）/ JSON 编码 string
// （grep 返回 '[{...},...]'）。后者直接 pre 渲染会挤成一行，可读性极差。
// 这里做三层尝试：(1) 直接 parse；(2) 剥离 budget note 再 parse；(3) 修复半截 array/object
// 然后再 parse。任何步骤成功就 pretty 输出 + 把 note 追加回末尾。
// ── 结构化键值渲染（2026-07-26）：工具参数/结果不再直接转录 JSON ──────────
// 生 JSON（大括号、引号转义、null 平铺、蛇形键名）是传输格式不是信息设计：
// command_status 一次展开 14 个字段，其中 5 个是空值，command 里还带 \" 转义。
// 这里把"对象"拆成键值对：键弱化、值等宽且字符串按原文显示（不再有引号转义）；
// 空值字段（null/undefined/''/[]/{}）折进一行"空字段 ×N"按需展开。
// 数组与非 JSON 文本保持原来的 pretty 文本——列表自有结构，别硬拆。
function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}
function isEmptyValue(v: unknown): boolean {
  if (v === null || v === undefined || v === '') return true
  if (Array.isArray(v) && v.length === 0) return true
  if (isPlainObject(v) && Object.keys(v).length === 0) return true
  return false
}
function formatKVValue(v: unknown): string {
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (v === null) return 'null'
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}
function StructuredKV({ data, className }: { data: Record<string, unknown>; className: string }) {
  const [showEmpty, setShowEmpty] = useState(false)
  const entries = Object.entries(data)
  const primary = entries.filter(([, v]) => !isEmptyValue(v))
  const empty = entries.filter(([, v]) => isEmptyValue(v))
  return (
    <div className={`tool-kv ${className}`}>
      {primary.map(([k, v]) => (
        <div className="tool-kv-row" key={k}>
          <span className="tool-kv-key">{k}</span>
          <span className="tool-kv-val">{formatKVValue(v)}</span>
        </div>
      ))}
      {empty.length > 0 && (
        <>
          <button
            type="button"
            className="tool-kv-more"
            onClick={(e) => { e.stopPropagation(); setShowEmpty((s) => !s) }}
          >
            {showEmpty ? '收起空字段' : `空字段 ×${empty.length}`}
          </button>
          {showEmpty && empty.map(([k, v]) => (
            <div className="tool-kv-row is-empty" key={k}>
              <span className="tool-kv-key">{k}</span>
              <span className="tool-kv-val">{v === '' ? '""' : formatKVValue(v)}</span>
            </div>
          ))}
        </>
      )}
    </div>
  )
}

function tryPrettyJSON(text: string): string {
  if (!text) return text
  const trimmed = text.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return text

  // 剥离尾部 budget note（如果有），后面 pretty 完再贴回
  const noteMatch = trimmed.match(BUDGET_NOTE_RE)
  const note = noteMatch ? noteMatch[0] : ''
  const body = note ? trimmed.slice(0, -note.length).trim() : trimmed

  const pretty = (obj: unknown) => JSON.stringify(obj, null, 2) + (note ? `\n${note.trim()}` : '')

  // 第一层：完整 JSON
  try {
    const parsed = JSON.parse(body)
    if (parsed !== null && typeof parsed === 'object') return pretty(parsed)
  } catch { /* 继续下一层 */ }

  // 第二层：被截断的 array → 找最后一个完整 }，闭合
  if (body.startsWith('[')) {
    const lastClose = body.lastIndexOf('}')
    if (lastClose > 0) {
      try {
        const parsed = JSON.parse(body.slice(0, lastClose + 1) + ']')
        if (Array.isArray(parsed)) return pretty(parsed)
      } catch { /* fall through */ }
    }
  }

  // 第三层：被截断的 object → 找最后一个完整 "key": value，闭合 }
  if (body.startsWith('{')) {
    // 简单策略：找最后一个 `,`，截掉它和后面，补 }
    const lastComma = body.lastIndexOf(',')
    if (lastComma > 0) {
      try {
        const parsed = JSON.parse(body.slice(0, lastComma) + '}')
        if (parsed !== null && typeof parsed === 'object') return pretty(parsed)
      } catch { /* fall through */ }
    }
  }

  // 全部失败：保持原样（含 note）
  return text
}

// ── 工具卡视觉时长锁（2026-05-17 第三版，"真实节奏"原则）─────────────
// 设计原则演变：
//   v1：calling 锁 350ms——本意"防止本地工具几十 ms 完成 calling 一闪而过"
//   v2：v1 + collapse 锁 600ms——同样为防止瞬间折叠
//   v3（current）：发现 v1/v2 把后端 gather 的"真实并发节奏"掩盖了：
//     • LLM 流式 emit tool_call_started 间隔 50-200ms → firstSeenAt 错峰
//     • 后端 dispatch_batch gather → 所有 result 几乎同帧到达
//     • 但 calling 锁让每个 tool 按 firstSeenAt+350ms 错峰切 done
//     • 视觉：第一个先绿、后面 N 个差不多一起绿 → "假"，不是真实并发
//
//   v3 改回"按真实结果到达"：
//   ┌─ T+0     tool_call 入场，badge=calling
//   ├─ T+X     result 到达（X 由后端实际执行决定：本地几十 ms / 网络几百 ms / 慢工具几秒）
//   ├─ T+max(X, firstSeenAt+100ms) badge 切 done（仅 100ms 防御一帧闪过）
//   └─ 整组所有成员 done 后 → collapse 锁过期 → 整组一起平滑折叠
//
// gather 的天然语义保证了"几乎同帧 done"——不需要人为同步；慢的工具就是慢，真实节奏。
//
// 历史回放路径：缓存消息无 firstSeenAt → 两个锁都不触发 → 直接 done 终态。
const TOOL_CALL_MIN_CALLING_DISPLAY_MS = 100    // 仅防一帧（16ms）闪过，约 6 帧人眼可感知
const TOOL_CALL_AUTO_COLLAPSE_DELAY_MS = 500    // 整组所有成员 done 后再多停留 ~150ms 才折叠

function formatThinkingDuration(durationMs?: number): string | null {
  if (typeof durationMs !== 'number' || !Number.isFinite(durationMs) || durationMs <= 0) return null
  if (durationMs < 1000) return '不到 1 秒'
  const seconds = Math.round(durationMs / 1000)
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const restSeconds = seconds % 60
  return restSeconds > 0 ? `${minutes} 分 ${restSeconds} 秒` : `${minutes} 分钟`
}

// 是否仍处于"显示 calling badge"窗口（用于 RuntimeToolBlock 的 status 字段）
function isToolCallInDisplayLock(
  segment: ContentSegment,
  hasResult: boolean,
  nowMs: number,
): boolean {
  if (!hasResult) return true
  const firstSeenAt = segment.firstSeenAt
  if (typeof firstSeenAt !== 'number') return false
  return nowMs < firstSeenAt + TOOL_CALL_MIN_CALLING_DISPLAY_MS
}

// 是否仍阻止自动折叠（用于 group / 单 tool 的 naturalExpanded 决策）。
// 比 calling 锁更长 → done 状态有 ~400ms 停留期再收起，避免"绿勾闪一下就走"。
// 组级用法：groupBlocksCollapse = members.some(...) 自动取最晚 firstSeenAt 语义，
//   保证整组所有成员都过 collapse 锁后才一起折叠（用户期望的"全部一起平滑消失"）。
function isToolCallBlockingAutoCollapse(
  segment: ContentSegment,
  hasResult: boolean,
  nowMs: number,
): boolean {
  if (!hasResult) return true
  const firstSeenAt = segment.firstSeenAt
  if (typeof firstSeenAt !== 'number') return false
  return nowMs < firstSeenAt + TOOL_CALL_AUTO_COLLAPSE_DELAY_MS
}

// 锁过期后强制重渲染：扫描两段锁的所有 expiry 取最早未过期者排定时器。
//
// ⚠️ 关键：useEffect 的 deps 必须是"firstSeenAt 集合"而非整个 segments。
//
// 历史 bug（2026-05-17 修）：原本 deps=[segments]，但思考字符流入会频繁修改
// segments[lastIdx].content（每次 P4 buffer flush 都改一次，间隔 50-250ms），
// 导致 segments 引用频繁变化 → useEffect 反复 cleanup + 重排 timer →
// 定时器永远在被推迟，永远到不了 firstSeenAt+500ms 触发点。
//
// 用户视觉感受：工具完成对勾后 collapse 锁本应 500ms 后过期触发折叠，但实际
// 要等到思考流安静下来（思考字符出现并 flush 后 250ms 静默）timer 才有机会
// 触发——看上去就是"工具卡在等思考出字才折叠"。
//
// 修复：deps 用 firstSeenAt 数组的 join 字符串，tool_call 入队后值不变，思考
// 流入不会触发 timer 重排，定时器按预期在 firstSeenAt+500ms 准时触发。
function useToolCallDisplayLockTick(segments: ContentSegment[]): void {
  const [, forceTick] = useReducer((x: number) => x + 1, 0)
  // 锁的"身份"= 所有 tool_call 的 firstSeenAt 列表（稳定值，入队后不变）。
  // 用 useMemo 算出字符串 key，segments 内容变化时只要 firstSeenAt 集合不变，
  // key 就相同，下面的 useEffect 不会重跑。
  const lockKey = useMemo(() => {
    const keys: string[] = []
    for (const seg of segments) {
      if (seg.type !== 'tool_call') continue
      const firstSeenAt = seg.firstSeenAt
      if (typeof firstSeenAt !== 'number') continue
      keys.push(String(firstSeenAt))
    }
    return keys.join(',')
  }, [segments])
  // 用 ref 让 scheduleNext 闭包始终读到最新 segments（思考字符 flush 时 segments
  // 引用变化但 lockKey 不变，useEffect 不重跑——闭包仍要能访问新 firstSeenAt 集合，
  // 但其实 firstSeenAt 集合稳定，旧引用读出来的值跟新引用一样，不用 ref 也行；
  // 这里直接闭包捕获 segments 即可。
  useEffect(() => {
    let cancelled = false
    // 递归排下一个 expiry：timer 触发后立刻 forceTick 并排下一个，
    // 不依赖 useEffect 重跑（lockKey 不变 → useEffect 不会重跑）。
    // 之前的 bug：useEffect 只设一次 timer 到最早 expiry，触发后状态僵住，
    //   下一个 expiry（如 collapse 锁 500ms）永远没人排，要靠"思考字符 flush
    //   触发 segments 变 → React 重渲染"才间接看到状态切换 → 用户感受是
    //   "工具折叠在等思考字符"。
    const scheduleNext = () => {
      if (cancelled) return
      const now = Date.now()
      let earliestExpiry = Infinity
      for (const seg of segments) {
        if (seg.type !== 'tool_call') continue
        const firstSeenAt = seg.firstSeenAt
        if (typeof firstSeenAt !== 'number') continue
        // 两个锁都要 tick：calling 锁过期 → badge 切 done；collapse 锁过期 → 折叠
        for (const expiry of [
          firstSeenAt + TOOL_CALL_MIN_CALLING_DISPLAY_MS,
          firstSeenAt + TOOL_CALL_AUTO_COLLAPSE_DELAY_MS,
        ]) {
          if (expiry > now && expiry < earliestExpiry) earliestExpiry = expiry
        }
      }
      if (earliestExpiry === Infinity) return
      const delay = Math.max(0, earliestExpiry - now) + 16
      setTimeout(() => {
        if (cancelled) return
        forceTick()
        scheduleNext()  // 关键：递归排下一个 expiry 的 timer
      }, delay)
    }
    scheduleNext()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- segments 通过闭包
    // 捕获，firstSeenAt 集合是稳定值（lockKey 不变即代表此集合不变），
    // 故依赖 lockKey 而非 segments 避免思考流入重排定时器。
  }, [lockKey])
}

// 尝试把 tool_result 解析成 AskUserQuestion 载荷；不是则返回 null。
// 两种形态：awaiting_user_choice（提问待答）/ answered_user_choice（阻塞等待
// 的终态回写，刷新页面后从持久化段恢复"已回答"卡片）。
// 2026-09-29 P1：议事厅内联 ask 卡（CollabMessageItem）也要识别 ask 载荷，
// 从私有改导出——识别逻辑仍只有这一份，两端共享。
export function tryParseAskPayload(seg: ContentSegment | undefined): AskUserQuestionPayload | null {
  if (!seg) return null
  const raw = seg.result ?? seg.content ?? ''
  const text = typeof raw === 'string' ? raw : (() => {
    try { return JSON.stringify(raw) } catch { return '' }
  })()
  if (!text || (!text.includes('awaiting_user_choice') && !text.includes('answered_user_choice'))) return null
  try {
    const parsed = JSON.parse(text)
    if (
      parsed &&
      parsed.status === 'awaiting_user_choice' &&
      typeof parsed.ask_id === 'string' &&
      Array.isArray(parsed.questions)
    ) {
      return { ...parsed, resolvedAnswers: null } as AskUserQuestionPayload
    }
    if (
      parsed &&
      parsed.status === 'answered_user_choice' &&
      typeof parsed.ask_id === 'string' &&
      Array.isArray(parsed.questions) &&
      Array.isArray(parsed.answers)
    ) {
      return { ...parsed, resolvedAnswers: parsed.answers } as AskUserQuestionPayload
    }
  } catch {
    // 不是合法 JSON 或结构不符，走老路径
  }
  return null
}

export interface AskStateEntry {
  state: AskExecutionState
  error?: string | null
}
export type AskStatesByAskId = Record<string, AskStateEntry>

type RuntimeVariant = 'chat' | 'collab' | 'worker'

function hasFollowingRuntimeDetail(segments: ContentSegment[], segmentIndex: number) {
  return segments.slice(segmentIndex + 1).some((nextSegment) => (
    ['thinking', 'tool_call', 'tool_result', 'text'].includes(nextSegment.type)
  ))
}

export function findMatchingToolResult(
  segments: ContentSegment[],
  toolCallSegment: ContentSegment,
  toolCallIndex: number,
) {
  return segments.find((segment, segmentIndex) => (
    segmentIndex > toolCallIndex &&
    segment.type === 'tool_result' &&
    (
      !segment.tool_use_id ||
      !toolCallSegment.tool_use_id ||
      segment.tool_use_id === toolCallSegment.tool_use_id
    )
  ))
}

// 2026-04-29 已弃用：原本想"只渲染最后一个 thinking 段并把前面的合并进来"，
// 但 isLastThinkingSegment 的逻辑写错（把"下一个非 thinking"当成"最后一个"），
// 导致每个 thinking 段都被认为是最后一个、各自带着累积的合并内容渲染，引发重复显示和共用 key 全展开 bug。
// 现在每个 thinking 段独立渲染（runtimeBlocks 主体里直接用 segment.content + segIndex key），
// 这两个 helper 不再被任何地方使用，仅保留作为"不要再这样写"的活体警示。
export function isLastThinkingSegment(segments: ContentSegment[], thinkingIndex: number) {
  return (
    segments[thinkingIndex]?.type === 'thinking' &&
    segments[thinkingIndex + 1]?.type !== 'thinking'
  )
}

export function mergeThinkingSegments(segments: ContentSegment[], thinkingIndex: number) {
  let mergedContent = segments[thinkingIndex]?.content || ''

  for (let index = thinkingIndex - 1; index >= 0; index -= 1) {
    if (segments[index]?.type !== 'thinking') break
    mergedContent = `${segments[index]?.content || ''}${mergedContent}`
  }

  return mergedContent
}

export function RuntimeThinkingBlock({
  blockKey,
  content,
  variant,
  expanded = true,
  streaming = false,
  onToggle,
  htmlContent,
  sessionId,
  agentId,
  renderMarkdown,
  durationMs,
}: {
  blockKey: string | number
  content: string
  variant: RuntimeVariant
  expanded?: boolean
  streaming?: boolean
  onToggle?: () => void
  htmlContent?: string
  /** chat variant 下，用于「翻译思考块」按钮——把 sessionId/agentId 传给 /utility/translate，
   *  让翻译复用当前会话的 model，不引第二个 model 选择器。 */
  sessionId?: string | null
  agentId?: string | null
  /** chat variant 下，可选：把翻译后的文本也走 markdown 渲染（跟原文一致风格）。
   *  没传时 fallback 纯文本展示。 */
  renderMarkdown?: (text: string) => string
  durationMs?: number
}) {
  // 2026-05-25 翻译开关：LLM 偶尔用英文思考时，中文用户点「译」按钮把思考块翻译成中文。
  // - 仅 chat variant + 非 streaming + 有内容时显示按钮（worker/collab 不需要）
  // - 翻译结果缓存在组件本地，再次点击切到「原文」不重新调 LLM
  // - 翻译走 /utility/translate，沿用当前 session/agent model（详见 utility_routes.py）
  const [translation, setTranslation] = useState<string | null>(null)
  const [translating, setTranslating] = useState(false)
  const [showTranslation, setShowTranslation] = useState(false)
  const [translateError, setTranslateError] = useState<string | null>(null)
  const [thinkingBodyMinHeight, setThinkingBodyMinHeight] = useState<number | null>(null)

  if (!content.trim()) return null

  if (variant === 'worker') {
    return (
      <pre key={blockKey} className="worker-trace-pre">{content}</pre>
    )
  }

  if (variant === 'collab') {
    // 2026-09-30 P1 打磨：collab 思考块支持独立折叠（头部可点 + 渐进收起），
    // 不再只能整条链路一起收。onToggle 未接线时退化为纯展示。
    return (
      <div key={blockKey} className="collab-chain-segment thinking">
        <button
          type="button"
          className="collab-chain-segment-head is-toggle"
          onClick={onToggle}
          aria-expanded={expanded}
        >
          <span className="collab-chain-segment-badge">
            <Brain size={13} />
            <span>思考</span>
          </span>
          {onToggle && (
            <ChevronDown
              size={13}
              className={`collab-chain-chevron${expanded ? '' : ' is-folded'}`}
            />
          )}
        </button>
        <div
          className={`collab-chain-thinking-body${expanded ? '' : ' is-collapsed'}`}
          aria-hidden={!expanded}
        >
          {content}
        </div>
      </div>
    )
  }

  // 流式思考体验（2026-05-16，对齐 Cascade）：思考过程中的 body 加 max-height +
  // 内部滚动 + 自动滚到底，让"思考区域"保持固定高度而不是随内容疯涨把页面顶得乱跳。
  // 思考结束后（streaming=false）恢复正常高度，用户点开就是完整内容。
  const thinkingBodyRef = useRef<HTMLDivElement | null>(null)
  // 2026-05-21 修：用户在思考块内滚轮上滑时不能跟原代码"无脑钉底"——
  // 否则每帧 content 增量都把 scrollTop 拉回底部，滚轮根本滑不动。
  // 跟正文外层（Chat.tsx 的 userScrolledUpRef）同款守卫：
  //   - 距底 > 24px 视为用户上滑 → 暂停 autopin
  //   - 用户滑回底部附近 → 自动恢复跟随
  //   - streaming 结束 → 重置（下次重新开始流式时再判定）
  const userScrolledUpInThinkingRef = useRef(false)
  useEffect(() => {
    if (!streaming) {
      userScrolledUpInThinkingRef.current = false
      return
    }
    if (!expanded) return
    const el = thinkingBodyRef.current
    if (!el) return
    if (userScrolledUpInThinkingRef.current) return
    el.scrollTop = el.scrollHeight
  }, [streaming, expanded, content, htmlContent])

  const handleThinkingScroll = () => {
    const el = thinkingBodyRef.current
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.clientHeight - el.scrollTop
    // 滑回底部附近（< 24px）→ 恢复跟随；明显上滑 → 暂停跟随
    userScrolledUpInThinkingRef.current = distanceFromBottom > 24
  }

  const bodyClassName = `chain-segment-body${expanded ? '' : ' collapsed'}${streaming && expanded ? ' chain-thinking-streaming' : ''}`
  const bodyStyle = thinkingBodyMinHeight && expanded ? { minHeight: `${thinkingBodyMinHeight}px` } : undefined
  const durationLabel = !streaming ? formatThinkingDuration(durationMs) : null

  // 翻译按钮：仅 chat variant 非流式且展开时可见
  const canShowTranslateBtn = variant === 'chat' && !streaming && expanded
  const isShowingTranslation = showTranslation && !!translation
  const lockThinkingBodyHeight = () => {
    const height = thinkingBodyRef.current?.offsetHeight
    if (!height) return
    setThinkingBodyMinHeight((prev) => Math.max(prev ?? 0, height))
  }
  const handleTranslateClick = async (e: React.MouseEvent) => {
    // 防止点按钮顺带触发外层 header 的 onToggle（不然每次点都把整块折叠/展开）
    e.stopPropagation()
    if (translating) return
    lockThinkingBodyHeight()
    // 已经翻译过 → 仅切换显示
    if (translation) {
      setShowTranslation((v) => !v)
      return
    }
    setTranslating(true)
    setTranslateError(null)
    try {
      const result = await translateText(content, {
        sessionId: sessionId ?? null,
        agentId: agentId ?? null,
      })
      setTranslation(result.text)
      setShowTranslation(true)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '翻译失败'
      setTranslateError(msg)
    } finally {
      setTranslating(false)
    }
  }

  // 决定 body 渲染的内容：翻译态 / 原文（markdown html 或纯文本）
  const renderedBody = (() => {
    if (isShowingTranslation && translation) {
      const html = renderMarkdown ? renderMarkdown(translation) : null
      if (html) {
        return (
          <div
            ref={thinkingBodyRef}
            className={bodyClassName}
            style={bodyStyle}
            onScroll={handleThinkingScroll}
            onWheel={handleThinkingScroll}
            dangerouslySetInnerHTML={{ __html: html }}
          />
        )
      }
      // 没 renderMarkdown → 纯文本（保留换行）。pre-wrap 由 .chain-segment-body 默认样式提供。
      return (
        <div
          ref={thinkingBodyRef}
          className={bodyClassName}
          onScroll={handleThinkingScroll}
          onWheel={handleThinkingScroll}
        >
          {translation}
        </div>
      )
    }
    // 原文路径（保持原来逻辑）
    if (htmlContent) {
      return (
        <div
          ref={thinkingBodyRef}
          className={bodyClassName}
          onScroll={handleThinkingScroll}
          onWheel={handleThinkingScroll}
          dangerouslySetInnerHTML={{ __html: htmlContent }}
        />
      )
    }
    return (
      <div
        ref={thinkingBodyRef}
        className={bodyClassName}
        onScroll={handleThinkingScroll}
        onWheel={handleThinkingScroll}
      >
        {content}
      </div>
    )
  })()

  return (
    <div key={blockKey} className={`chain-segment chain-thinking${expanded ? ' is-expanded' : ''}`}>
      <div className="chain-segment-header" onClick={onToggle}>
        <span className="chain-arrow chain-thinking-arrow">
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>
        <span className="chain-label">
          思考过程
          {durationLabel && (
            <span className="chain-thinking-duration">{durationLabel}</span>
          )}
        </span>
        {canShowTranslateBtn && (
          <button
            type="button"
            className={`chain-thinking-translate-btn${isShowingTranslation ? ' is-active' : ''}`}
            onClick={handleTranslateClick}
            disabled={translating}
            title={
              translating
                ? '翻译中…'
                : translation
                  ? (isShowingTranslation ? '查看原文' : '查看译文')
                  : '翻译为中文'
            }
            aria-label={
              translating
                ? '翻译中'
                : translation
                  ? (isShowingTranslation ? '查看原文' : '查看译文')
                  : '翻译思考块'
            }
          >
            {translating ? (
              <Loader2 size={12} className="chain-thinking-translate-spin" />
            ) : (
              <Languages size={12} />
            )}
            <span>
              {translating
                ? '翻译中'
                : translation
                  ? (isShowingTranslation ? '原文' : '译文')
                  : '翻译'}
            </span>
          </button>
        )}
      </div>
      {translateError && expanded && (
        <div className="chain-thinking-translate-error" role="alert">
          翻译失败：{translateError}
        </div>
      )}
      {renderedBody}
    </div>
  )
}

export function RuntimeNoticeBlock({
  blockKey,
  kind,
  content,
  variant,
  hasFollowingDetail = false,
}: {
  blockKey: string | number
  kind?: string
  content: string
  variant: RuntimeVariant
  hasFollowingDetail?: boolean
}) {
  if (!content.trim()) return null

  const normalizedContent = normalizeRuntimeNoticeText(content)
  const noticeTone = getRuntimeNoticeTone(kind)
  const compactNotice = false
  const showCompactText = !hasFollowingDetail

  if (variant === 'worker') {
    return (
      <div key={blockKey} className="worker-trace-stack">
        {kind && (
          <div className="worker-trace-meta-line">
            kind: <code className="worker-inline-code">{kind}</code>
          </div>
        )}
        {(!compactNotice || showCompactText) && (
          <pre className="worker-trace-pre">{normalizedContent}</pre>
        )}
      </div>
    )
  }

  if (variant === 'collab') {
    return (
      <div
        key={blockKey}
        className={`collab-chain-segment notice notice-${noticeTone} ${compactNotice ? 'compact' : ''}`}
      >
        <div className="collab-chain-segment-head">
          <span className="collab-chain-segment-badge">
            <Info size={14} />
            <span>{getRuntimeNoticeTitle(kind)}</span>
          </span>
        </div>
        {(!compactNotice || showCompactText) && (
          <div className="collab-chain-text">{normalizedContent}</div>
        )}
      </div>
    )
  }

  return (
    <div key={blockKey} className={`chain-segment chain-notice chain-notice-${noticeTone}`}>
      <div className="chain-segment-header">
        <Info size={14} className="chain-icon" />
        <span className="chain-label">{getRuntimeNoticeTitle(kind)}</span>
      </div>
      <div className="chain-tool-body">
        <div className="chain-notice-text">{normalizedContent}</div>
      </div>
    </div>
  )
}

export function RuntimeToolBlock({
  blockKey,
  segment,
  resultSegment,
  variant,
  expanded = true,
  onToggle,
  customResult,
}: {
  blockKey: string | number
  segment: ContentSegment
  resultSegment?: ContentSegment
  variant: RuntimeVariant
  expanded?: boolean
  onToggle?: () => void
  /**
   * 自定义"结果"区域 JSX。提供后会替换默认的 <pre> 渲染（用于 execute_command 场景：
   * 把 CommandStreamCard 嵌进 chain-tool 双栏的 result slot，外层依然是工具卡，内层是终端流）。
   */
  customResult?: ReactNode
}) {
  const toolName = segment.tool_name || segment.toolCall?.name || segment.name || '工具调用'
  const hasResult = Boolean(resultSegment)
  // Header 摘要：read_file → "backend/x.py:1-50"，让用户不展开 args 也知道在做什么。识别不出参数时返回 undefined。
  const rawArgs = (segment.input ?? segment.arguments ?? segment.toolCall?.input) as
    | Record<string, any>
    | undefined
  const toolSummary = getToolDisplaySummary(toolName, rawArgs)
  // calling 最小展示时长锁：缓存路径无 firstSeenAt → 立即 done；live 路径
  // result 到达但 elapsed<350ms 时仍显示 calling，确保 calling 视觉态可见。
  const status = isToolCallInDisplayLock(segment, hasResult, Date.now()) ? 'calling' : 'done'
  const hasArgs =
    segment.input !== undefined ||
    segment.arguments !== undefined ||
    segment.toolCall?.input !== undefined
  const toolArgs = segment.input ?? segment.arguments ?? segment.toolCall?.input ?? {}
  const argsText = JSON.stringify(toolArgs, null, 2)
  const resultContent = resultSegment?.result ?? resultSegment?.content ?? ''
  // 工具结果若是 JSON 编码后的 string（grep / list_dir 等常见），
  // 尝试 parse + re-stringify pretty 后展示，避免"一行挤一坨"。非 JSON 文本原样保留。
  const resultText = typeof resultContent === 'string'
    ? tryPrettyJSON(resultContent)
    : JSON.stringify(resultContent, null, 2)
  // 结构化路径（仅 chat 变体使用）：对象走键值对渲染；字符串先试 parse。
  // 注意：普通计算而非 hook——本组件在 worker/collab 分支有提前 return，
  // 在这之后加 hook 会破坏 hooks 顺序。
  const argsObj = isPlainObject(toolArgs) && Object.keys(toolArgs).length > 0
    ? (toolArgs as Record<string, unknown>)
    : null
  let resultObj: Record<string, unknown> | null = null
  if (isPlainObject(resultContent)) {
    resultObj = resultContent as Record<string, unknown>
  } else if (typeof resultContent === 'string' && resultContent.trim().startsWith('{')) {
    try {
      const parsed = JSON.parse(resultContent.trim())
      if (isPlainObject(parsed)) resultObj = parsed
    } catch { /* 非 JSON，走文本 */ }
  }

  if (variant === 'worker') {
    return (
      <div key={blockKey} className="worker-trace-stack">
        {hasArgs && (
          <pre className="worker-trace-pre">{argsText}</pre>
        )}
        {hasResult && (
          <pre className="worker-trace-pre">{resultText}</pre>
        )}
      </div>
    )
  }

  if (variant === 'collab') {
    // 2026-09-30 P1 打磨：collab 工具卡支持折叠（头部可点 + 渐进收起），
    // expanded/onToggle 由上层 expandWithUserToggle 提供；未接线时全展开纯展示。
    return (
      <div key={blockKey} className="collab-chain-segment tool">
        <button
          type="button"
          className="collab-chain-segment-head is-toggle"
          onClick={onToggle}
          aria-expanded={expanded}
        >
          <span className="collab-chain-segment-badge">
            <Wrench size={13} />
            <span className="collab-chain-segment-name">{toolName}</span>
            {/* 2026-09-29 P1 打磨：头部带人类可读摘要（如 read_file → IDENTITY.md），
                与 chat 变体头部信息量对齐——原先 collab 卡只有工具名 */}
            {toolSummary && (
              <span className="collab-chain-segment-summary" title={toolSummary}>
                {toolSummary}
              </span>
            )}
          </span>
          <span className="collab-chain-head-right">
            <span className={`collab-chain-status ${status}`}>
              {status === 'done' ? '已完成' : '调用中'}
            </span>
            {onToggle && (
              <ChevronDown
                size={13}
                className={`collab-chain-chevron${expanded ? '' : ' is-folded'}`}
              />
            )}
          </span>
        </button>
        <div
          className={`collab-chain-tool-body${expanded ? '' : ' is-collapsed'}`}
          aria-hidden={!expanded}
        >
          {/* 2026-09-29 P1 打磨：对象参数走键值行（path 显示真实路径，不再是一坨
              带转义反斜杠的原始 JSON），与 chat 变体的 StructuredKV 同款 */}
          {hasArgs && (argsObj ? (
            <StructuredKV data={argsObj} className="collab-chain-args" />
          ) : (
            <pre className="collab-chain-pre">{argsText}</pre>
          ))}
          {/* 2026-05-22：collab 分支补 customResult slot，跟 chat 路径对齐。
           *   execute_command / delegate_acp_agent 上层会传 CommandStreamCard / AcpDelegateCard
           *   作为 customResult，让 stdout/stderr / chunk 实时流出来；不接的话工具卡只会
           *   在 tool_result 到来后一次性显示文本结果，跟"等结果完了一下子出来"现象吻合。 */}
          {customResult !== undefined ? (
            <div className="collab-chain-result">
              <div className="collab-chain-result-label">工具结果</div>
              {customResult}
            </div>
          ) : (
            hasResult && (
              <div className="collab-chain-result">
                <div className="collab-chain-result-label">工具结果</div>
                <pre className="collab-chain-pre">{resultText}</pre>
              </div>
            )
          )}
        </div>
      </div>
    )
  }

  // 展开自带入视野：用户点 expand 时把卡身体的底部滚进视口。
  // 比 messagesEndRef.scrollIntoView 稳：直接定位"刚展开的卡"DOM 自己。
  // initial mount 时不滚（避免历史回放滚乱），只在 expanded 从 false → true 时触发。
  const rootRef = useRef<HTMLDivElement | null>(null)
  const prevExpandedRef = useRef<boolean>(Boolean(expanded))
  useEffect(() => {
    const wasExpanded = prevExpandedRef.current
    prevExpandedRef.current = Boolean(expanded)
    if (!expanded || wasExpanded) return
    const el = rootRef.current
    if (!el) return
    requestAnimationFrame(() => {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    })
  }, [expanded])

  return (
    <div key={blockKey} ref={rootRef} className="chain-segment chain-tool">
      <button
        type="button"
        className="chain-tool-header-unified"
        onClick={onToggle}
      >
        <span className="chain-tool-leading">
          <Wrench size={14} />
          <span className="chain-tool-name">{toolName}</span>
          {toolSummary && (
            <span className="chain-tool-summary" title={toolSummary}>
              {toolSummary}
            </span>
          )}
        </span>
        <span className={`chain-tool-status-badge ${status}`}>
          {status === 'done' ? '已完成' : '调用中'}
        </span>
        {onToggle && (
          <span className="chain-tool-toggle">{expanded ? '▾' : '▸'}</span>
        )}
      </button>

      {/* 2026-05-16：从条件渲染改成始终 mount + className 切换，让折叠走 CSS
          max-height transition（对齐 .chain-thinking .chain-segment-body 的渐进折叠）。
          原 `{expanded && ...}` 是 unmount/remount，瞬间消失没有过渡动画。 */}
      <div className={`chain-tool-body${expanded ? '' : ' collapsed'}`} aria-hidden={!expanded}>
        {hasArgs && (argsObj ? (
          <StructuredKV data={argsObj} className="chain-tool-args" />
        ) : (
          <pre className="chain-tool-args">{argsText}</pre>
        ))}
        {customResult !== undefined ? (
          <div className="chain-tool-custom-result">{customResult}</div>
        ) : (
          hasResult && (resultObj ? (
            <StructuredKV data={resultObj} className="chain-tool-result" />
          ) : (
            <pre className="chain-tool-result">{resultText}</pre>
          ))
        )}
      </div>
    </div>
  )
}

export function RuntimeChainSegments({
  segments,
  variant,
  messageId,
  sessionId,
  sourceAgent,
  streaming = false,
  expandedThinking,
  onToggleThinking,
  expandedToolCalls,
  onToggleToolCall,
  renderMarkdown,
  askStatesByAskId,
  onSubmitAskAnswer,
}: {
  segments: ContentSegment[]
  variant: Extract<RuntimeVariant, 'chat' | 'collab'>
  messageId: string | number
  sessionId?: string | null
  sourceAgent?: Agent | null
  streaming?: boolean
  expandedThinking?: Set<string>
  onToggleThinking?: (thinkingKey: string) => void
  expandedToolCalls?: Set<string>
  onToggleToolCall?: (toolKey: string) => void
  renderMarkdown?: (text: string) => string
  askStatesByAskId?: AskStatesByAskId
  onSubmitAskAnswer?: (askId: string, toolUseId: string | undefined, answers: AskUserAnswer[]) => void
}) {
  // calling 最小展示时长锁的"到点重渲染"驱动：锁过期后 forceTick，
  // 让 RuntimeToolBlock 的 status 从 calling 切到 done。详见文件头部 helper。
  useToolCallDisplayLockTick(segments)

  // 2026-04-29：检测连续 tool_call 段，标记为"本轮调用组"。
  // 推理依据：LLM 在一次响应里 yield 多个 tool_calls 时，segments 里它们会连续出现，
  // 后端再统一执行并把 results 一并塞回；而跨轮调用的形态是 tool_call → tool_result → tool_call → tool_result，
  // tool_call 不连续。所以"段层 adjacency"= "同一轮 LLM 响应里的工具批"。
  //
  // 2026-05-17：增加 `stages`——按后端 dispatch_batch 同款算法把组内成员切成
  // "并行段（连续 read）/ 串行段（write/exclusive）"，UI 渲染时段间插分隔条标签，
  // 让用户看出"哪批工具同时跑、哪个工具卡着等前面"。算法对齐
  // `backend/tools/builtin/clean_registry.py:_DEFAULT_CONCURRENCY_CLASSES` + `dispatch_batch`。
  const toolGroupMap = useMemo(() => {
    const isRenderableToolCall = (seg: ContentSegment | undefined) =>
      seg?.type === 'tool_call'

    interface GroupInfo {
      isStart: boolean
      members: number[]
      // stages 里的 indices 是 members 数组的下标（0-based 组内序号）；
      // 渲染时 members[stage.indices[k]] 才是真正的 segment 下标。
      stages: ToolStage[]
    }
    const map = new Map<number, GroupInfo>()
    let i = 0
    while (i < segments.length) {
      if (isRenderableToolCall(segments[i])) {
        const members: number[] = []
        let j = i
        while (j < segments.length && isRenderableToolCall(segments[j])) {
          members.push(j)
          j += 1
        }
        const memberToolNames = members.map((idx) => segments[idx]?.tool_name)
        const stages = buildToolStages(memberToolNames)
        members.forEach((idx, k) => {
          map.set(idx, { isStart: k === 0, members, stages })
        })
        i = j
      } else {
        i += 1
      }
    }
    return map
  }, [segments])

  return (
    <>
      {segments.map((segment, segmentIndex) => {
        if (segment.type === 'thinking') {
          // 2026-04-29：每个 thinking 段独立渲染、独立 toggle。
          // 之前的合并 + isLastThinkingSegment 逻辑会让多个思考段渲染成多个块、内容互相重复，
          // 加上共用 key 导致"点一个全展开"的 bug。
          if (!segment.content?.trim()) return null

          // 自动折叠规则：如果这段思考后面已经有别的段（说明思考阶段结束、agent 进入下一步），
          // 默认折叠 + 不再显示"思考中..."；用户点击可重新打开看历史。
          // 当前最新一段 thinking 才保持展开 + 显示流式光标。
          const hasContentAfter = segmentIndex < segments.length - 1
          const naturalExpanded = !hasContentAfter
          const isThinkingStreaming = Boolean(streaming) && !hasContentAfter

          if (variant === 'chat') {
            const thinkingKey = `${messageId}-think-${segmentIndex}`
            const userToggled = expandedThinking?.has(thinkingKey)
            const isExpanded = userToggled ? !naturalExpanded : naturalExpanded

            return (
              <RuntimeThinkingBlock
                key={thinkingKey}
                blockKey={`think-${segmentIndex}`}
                content={segment.content}
                variant="chat"
                expanded={isExpanded}
                streaming={isThinkingStreaming}
                onToggle={onToggleThinking ? () => onToggleThinking(thinkingKey) : undefined}
                htmlContent={renderMarkdown ? renderMarkdown(segment.content) : undefined}
                sessionId={sessionId}
                agentId={sourceAgent?.id ?? null}
                renderMarkdown={renderMarkdown}
                durationMs={typeof segment.firstSeenAt === 'number' ? ((segment.completedAt ?? Date.now()) - segment.firstSeenAt) : undefined}
              />
            )
          }

          // 2026-09-30 P1 打磨：collab 思考块接入独立折叠（键位规则与 chat 一致）
          const collabThinkingKey = `${messageId}-think-${segmentIndex}`
          const collabUserToggled = expandedThinking?.has(collabThinkingKey)
          return (
            <RuntimeThinkingBlock
              key={`${messageId}-thinking-${segmentIndex}`}
              blockKey={`${messageId}-thinking-${segmentIndex}`}
              content={segment.content}
              variant="collab"
              expanded={collabUserToggled ? !naturalExpanded : naturalExpanded}
              streaming={isThinkingStreaming}
              onToggle={onToggleThinking ? () => onToggleThinking(collabThinkingKey) : undefined}
            />
          )
        }

        if (segment.type === 'tool_call') {
          const resultSegment = findMatchingToolResult(segments, segment, segmentIndex)

          // ── 通用单条工具卡工厂（chat / collab 共用）──
          // 2026-09-29 规则08 Step 2 落地：原议事厅独立工厂与本工厂的特化识别有
          // ~70 行重复（BrowserPreview / GeneratedImage / FileDiff / ReadFile /
          // Acp / Cmd / FastContext 七张卡两份代码），合并后加新卡只写这一处，
          // 两端自动生效。差异只保留两类：
          //   1) AskUserQuestion：业务闭环依赖调用方接线（答案回流 + 卡片状态），
          //      chat 始终接线；collab 传了 onSubmitAskAnswer 才开放（议事厅 ask
          //      闭环 P1 已接）。未接线时降级普通工具卡。
          //   2) 展开节奏：chat 有"自然展开 + 用户 toggle 反转 + 折叠锁"；collab
          //      无 toggle，固定"跑中展开、完成折叠"——expandWithUserToggle 统一。
          // 并发组外壳仍是 chat-only（议事厅不需要单 agent 内的并发视觉外壳）。
          //   见 .lry/rules/04-tool-card-variant-guard.md
          const renderUniversalToolBlock = (
            memberSeg: ContentSegment,
            memberIdx: number,
            memberResult: ContentSegment | undefined,
            isInGroup: boolean = false,
          ): ReactNode => {
            const isChat = variant === 'chat'
            const memberKey = `${messageId}-tc-${memberIdx}`

            // 展开节奏：chat 有折叠锁等自然规则；collab 自然规则 = 跑中展开、完成
            // 折叠。用户 toggle（expandedToolCalls）两种 variant 都生效——
            // 2026-09-30 起 collab 卡也支持独立折叠，不再钉死展开状态。
            const expandWithUserToggle = (naturalExpanded: boolean) => {
              const userToggled = expandedToolCalls?.has(memberKey)
              return {
                expanded: userToggled ? !naturalExpanded : naturalExpanded,
                onToggle: onToggleToolCall ? () => onToggleToolCall(memberKey) : undefined,
              }
            }

            // AskUserQuestion —— tool_result 里带 awaiting_user_choice（或
            // 阻塞等待终态 answered_user_choice，刷新后恢复"已回答"卡片）
            const askPayload = tryParseAskPayload(memberResult)
            if (askPayload && (isChat || onSubmitAskAnswer)) {
              const askState = askStatesByAskId?.[askPayload.ask_id]
              const askExecutionState = askPayload.resolvedAnswers?.length
                ? 'submitted' as const
                : askState?.state
              return (
                <AskUserQuestionCard
                  key={`${messageId}-ask-${memberIdx}`}
                  payload={askPayload}
                  toolUseId={memberSeg.tool_use_id}
                  executionState={askExecutionState}
                  executionError={askState?.error ?? null}
                  onSubmit={onSubmitAskAnswer}
                />
              )
            }

            // BrowserPreview —— tool_result 里带 _runtime_block: 'browser_preview'
            const browserPreviewRaw = memberResult?.result ?? memberResult?.content
            const browserPreviewPayload = tryParseBrowserPreviewPayload(
              typeof browserPreviewRaw === 'string'
                ? browserPreviewRaw
                : browserPreviewRaw != null
                  ? (() => { try { return JSON.stringify(browserPreviewRaw) } catch { return null } })()
                  : null,
            )
            if (browserPreviewPayload) {
              return (
                <BrowserPreviewCard
                  key={`${messageId}-browser-${memberIdx}-${browserPreviewPayload.preview_id}`}
                  payload={browserPreviewPayload}
                />
              )
            }

            // GeneratedImage —— tool_result 里带 _runtime_block: 'generated_image'
            const genImageRawM = memberResult?.result ?? memberResult?.content
            const genImagePayloadM = tryParseGeneratedImagePayload(
              typeof genImageRawM === 'string'
                ? genImageRawM
                : genImageRawM != null
                  ? (() => { try { return JSON.stringify(genImageRawM) } catch { return null } })()
                  : null,
            )
            if (genImagePayloadM) {
              return (
                <GeneratedImageCard
                  key={`${messageId}-genimg-${memberIdx}`}
                  payload={genImagePayloadM}
                />
              )
            }

            // FileDiff —— tool_result 里带 _runtime_block: 'file_diff'
            // 对应 edit / multi_edit / write_to_file 三个工具
            const fileDiffRawM = memberResult?.result ?? memberResult?.content
            const fileDiffPayloadM = tryParseFileDiffPayload(
              typeof fileDiffRawM === 'string'
                ? fileDiffRawM
                : fileDiffRawM != null
                  ? (() => { try { return JSON.stringify(fileDiffRawM) } catch { return null } })()
                  : null,
            )
            if (fileDiffPayloadM) {
              return (
                <FileDiffCard
                  key={`${messageId}-filediff-${memberIdx}-${fileDiffPayloadM.path}`}
                  payload={fileDiffPayloadM}
                />
              )
            }

            // ReadFile —— read_file 结构化 JSON → 行号 + 代码预览
            const readFileRawM = memberResult?.result ?? memberResult?.content
            const readFilePayloadM = tryParseReadFilePayload(
              typeof readFileRawM === 'string'
                ? readFileRawM
                : readFileRawM != null
                  ? (() => { try { return JSON.stringify(readFileRawM) } catch { return null } })()
                  : null,
              memberSeg.tool_name,
            )
            if (readFilePayloadM) {
              const { expanded: readFileExpanded, onToggle: readFileOnToggle } = expandWithUserToggle(!memberResult)
              return (
                <RuntimeToolBlock
                  key={memberKey}
                  blockKey={memberIdx}
                  segment={memberSeg}
                  resultSegment={memberResult}
                  variant={variant}
                  expanded={readFileExpanded}
                  onToggle={readFileOnToggle}
                  customResult={(<ReadFileCard payload={readFilePayloadM} />)}
                />
              )
            }

            // delegate_acp_agent —— 外层 chain-tool（显示 prompt/agent 参数），
            // result slot 嵌 AcpDelegateCard（跑完自动折叠，跟命令卡一致）。
            // 持久化策略：先用 live chunks；live 没了（SSE 重连/刷新页面）就从
            // tool_result JSON 合成 start/text/end 三条假 chunks 喂卡片。
            if (memberSeg.tool_name === 'delegate_acp_agent') {
              const syntheticDid = `${messageId}-acp-${memberIdx}`
              const liveChunks = findChunksForAcpDelegate(segments, memberSeg, memberResult)
              const chunks = liveChunks.length > 0
                ? liveChunks
                : buildSyntheticChunksFromAcpResult(memberSeg, memberResult, syntheticDid)
              const did = (chunks.length > 0
                ? (chunks[0].payload as { delegate_id?: string } | undefined)?.delegate_id
                : undefined)
                || memberSeg.tool_use_id
                || syntheticDid
              const { expanded: acpExpanded, onToggle: acpOnToggle } = expandWithUserToggle(!memberResult)
              return (
                <RuntimeToolBlock
                  key={memberKey}
                  blockKey={memberIdx}
                  segment={memberSeg}
                  resultSegment={memberResult}
                  variant={variant}
                  expanded={acpExpanded}
                  onToggle={acpOnToggle}
                  customResult={(
                    // 即使 chunks=[]（跑中 SSE 还没到）也渲染卡片，让占位"等首批响应…"可见，
                    // 不要退化成 undefined→外层 <pre> JSON / 空 body
                    <AcpDelegateCard delegateId={did} chunks={chunks} />
                  )}
                />
              )
            }

            // execute_command —— 外层 chain-tool 保留命令参数，result slot 嵌 CommandStreamCard
            if (memberSeg.tool_name === 'execute_command') {
              const liveChunks = findLiveChunksForToolCall(segments, memberSeg)
              const syntheticId = `${messageId}-cmd-${memberIdx}`
              const chunks = liveChunks.length > 0
                ? liveChunks
                : buildSyntheticChunksFromToolResult(memberSeg, memberResult, syntheticId)
              const cid = (liveChunks[0]?.payload as { command_id?: string } | undefined)?.command_id
                || memberSeg.tool_use_id
                || syntheticId
              const { expanded: cmdExpanded, onToggle: cmdOnToggle } = expandWithUserToggle(!memberResult)
              return (
                <RuntimeToolBlock
                  key={memberKey}
                  blockKey={memberIdx}
                  segment={memberSeg}
                  resultSegment={memberResult}
                  variant={variant}
                  expanded={cmdExpanded}
                  onToggle={cmdOnToggle}
                  customResult={(
                    <CommandStreamCard
                      commandId={cid}
                      chunks={chunks}
                    />
                  )}
                />
              )
            }

            // fast_context_search —— Devin 风格 FastContextCard
            const memberToolName = memberSeg.tool_name || memberSeg.toolCall?.name || memberSeg.name
            if (memberToolName === 'fast_context_search') {
              const liveChunks = findLiveChunksForFastContext(segments, memberSeg, memberResult)
              const chunks = liveChunks.length > 0
                ? liveChunks
                : buildSyntheticChunksForFastContext(memberSeg, memberResult)
              return (
                <FastContextCard
                  key={memberKey}
                  chunks={chunks}
                  toolCallSegment={memberSeg}
                  resultSegment={memberResult}
                  onToggle={onToggleToolCall ? () => onToggleToolCall(memberKey) : undefined}
                />
              )
            }

            // 默认：普通 chain-tool
            // collab 自然节奏：跑中展开看实时输出，完成折叠成一行（可点头部展开）
            if (!isChat) {
              const { expanded: collabExpanded, onToggle: collabOnToggle } = expandWithUserToggle(!memberResult)
              return (
                <RuntimeToolBlock
                  key={memberKey}
                  blockKey={memberIdx}
                  segment={memberSeg}
                  resultSegment={memberResult}
                  variant={variant}
                  expanded={collabExpanded}
                  onToggle={collabOnToggle}
                />
              )
            }
            // chat 自然展开规则（2026-05-16，对齐 thinking 块）：
            //   组外单 tool：
            //     - calling 中（含展示锁定期）：强制展开看 args + 菊花
            //     - done + 后面还有内容：自然折叠（这步过去了，让位给后续）
            //     - done + 是最后一段：自然展开（最近一步默认敞开看 result）
            //   组内（isInGroup=true）：始终折叠成单行条
            //     —— 组外壳已经表达"这批 N 个工具"，组内每个成员只显示状态条
            //     就够了，args/result 原始 JSON 默认不展开避免刷屏。用户点单
            //     个成员的 toggle 才展开看细节，跟 Cascade UX 一致。
            const memberHasResult = Boolean(memberResult)
            const memberHasContentAfter = memberIdx < segments.length - 1
            let memberNaturalExpanded: boolean
            if (isInGroup) {
              memberNaturalExpanded = false
            } else {
              // 用 collapse 锁（500ms）：result 到达后再停留 ~400ms 才自动折叠，
              // 避免跟 done 同帧消失让用户看不到完成态。注 calling 锁仅 100ms
              // （v3 真实节奏原则），所以这里 500-100=400ms 是真正的"done 停留期"。
              const memberBlocksCollapse = isToolCallBlockingAutoCollapse(
                memberSeg, memberHasResult, Date.now(),
              )
              // 流式中：collapse 锁过期 → 直接折叠（不再等"后面有别的段"）。
              //   因 P4 smooth streaming 把后续思考/正文缓冲在 buffer 里（凑齐
              //   句号 / 60字 / 250ms 静默才 flush），等"看到下一段"会让工具卡
              //   完成对勾后停留半秒到一秒才收，用户感受是"等好一会儿才收"。
              // 流式结束：保留原逻辑——工具是最后一段时展开看 result。
              memberNaturalExpanded = memberBlocksCollapse
                || (!memberHasContentAfter && !streaming)
            }
            const memberUserToggled = expandedToolCalls?.has(memberKey)
            const memberExpanded = memberUserToggled
              ? !memberNaturalExpanded
              : memberNaturalExpanded
            return (
              <RuntimeToolBlock
                key={memberKey}
                blockKey={memberIdx}
                segment={memberSeg}
                resultSegment={memberResult}
                variant={variant}
                expanded={memberExpanded}
                onToggle={onToggleToolCall ? () => onToggleToolCall(memberKey) : undefined}
              />
            )
          }

          if (variant === 'chat') {
            // 并发组：≥2 个连续 tool_call 包成一个外壳，组首索引负责整个组的渲染，组员返回 null
            const groupInfo = toolGroupMap.get(segmentIndex)
            if (groupInfo && groupInfo.members.length >= 2) {
              if (!groupInfo.isStart) return null
              const lastMemberIdx = groupInfo.members[groupInfo.members.length - 1]
              const groupKey = `${messageId}-tg-${segmentIndex}`
              // 与 thinking 同款自动折叠：组的最后一个工具后面还有别的段说明这批做完了，默认折叠。
              // 2026-05-16：组内任一工具仍 calling 时强制展开（跟单 tool 路径一致），
              // 让本轮并发执行期间 args + 菊花一直可见。
              const groupHasContentAfter = lastMemberIdx < segments.length - 1
              const nowMsForGroup = Date.now()
              // 用 collapse 锁（500ms）：some() 取最晚 firstSeenAt 语义——所有成员
              // 都过 collapse 锁后才一起折叠（"全部一起平滑消失"）。badge 切 done
              // 走 calling 锁（仅 100ms），按真实 result 到达节奏切换，不强行同步。
              const groupBlocksCollapse = groupInfo.members.some((idx) => {
                const m = segments[idx]
                if (!m || m.type !== 'tool_call') return false
                const r = findMatchingToolResult(segments, m, idx)
                return isToolCallBlockingAutoCollapse(m, Boolean(r), nowMsForGroup)
              })
              // 与单工具同款逻辑：流式中 collapse 锁过期就直接折叠，避免被 P4 smooth
              // streaming 思考 buffer 节流卡着等"后面出现新段"。流式结束时保留原
              // 逻辑——组是最后一段时展开看 result。
              const groupNaturalExpanded = groupBlocksCollapse
                || (!groupHasContentAfter && !streaming)
              const groupUserToggled = expandedToolCalls?.has(groupKey)
              const groupExpanded = groupUserToggled ? !groupNaturalExpanded : groupNaturalExpanded

              const handleGroupToggle = onToggleToolCall ? () => onToggleToolCall(groupKey) : undefined

              return (
                <div
                  key={groupKey}
                  className={`chain-tool-group${groupExpanded ? '' : ' collapsed'}`}
                >
                  <button
                    type="button"
                    className="chain-tool-group-header"
                    onClick={handleGroupToggle}
                    aria-expanded={groupExpanded}
                  >
                    <Wrench size={14} />
                    <span className="chain-tool-group-label">本轮调用</span>
                    <span className="chain-tool-group-count">{groupInfo.members.length} 个工具</span>
                    <span className="chain-tool-group-chevron" aria-hidden>
                      {groupExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </span>
                  </button>
                  {/* 2026-05-16：从条件渲染改成始终 mount + className 切换。
                      原 `{groupExpanded ? ... : null}` 是 unmount/remount，瞬间消失没有
                      过渡动画。折叠走 CSS max-height transition（对齐思考块的渐进折叠），
                      自然消除"完成后等一会儿突然收"的生硬感。 */}
                  <div
                    className={`chain-tool-group-body${groupExpanded ? '' : ' collapsed'}`}
                    aria-hidden={!groupExpanded}
                  >
                    {/* 2026-05-17：按 stages 切段渲染——并行段（≥2 个 read 一起 gather）+
                        串行段（write/exclusive 单独 await）之间插一行小标签，
                        让用户看出"哪批同时跑、哪个卡着等"。单段（整组只有一段）时
                        不插标签，避免视觉冗余。 */}
                    {(() => {
                      const stages = groupInfo.stages
                      // 2026-07-26：标签只在"确有并发"时插入——至少一个并行段有
                      // ≥2 个成员。否则（如 独占→单个read→独占）所有工具实际就是
                      // 依次执行，插一排标签零信息量，还会出现"并行执行 · 1 个"
                      // 这种自相矛盾的文案（跟谁并行？）。
                      const hasRealConcurrency = stages.some(
                        (s) => s.type === 'parallel' && s.indices.length >= 2,
                      )
                      const showStageLabels = stages.length >= 2 && hasRealConcurrency
                      return stages.flatMap((stage, stageIdx) => {
                        const stageNodes = stage.indices.map((memberIdxInGroup) => {
                          const memberIdx = groupInfo.members[memberIdxInGroup]
                          const memberSeg = segments[memberIdx]
                          const memberResult = findMatchingToolResult(segments, memberSeg, memberIdx)
                          // isInGroup=true：组内成员默认折叠成单行条，避免 args/result
                          // 原始 JSON 同时全部摊开刷屏。
                          return renderUniversalToolBlock(memberSeg, memberIdx, memberResult, true)
                        })
                        if (!showStageLabels) return stageNodes
                        const labelKey = `${groupKey}-stage-${stageIdx}`
                        // 落单在并行通道里的读工具：标"读取"而不是"并行执行 · 1 个"
                        const stageLabel = stage.type === 'parallel'
                          ? (stage.indices.length >= 2
                              ? `并行执行 · ${stage.indices.length} 个`
                              : '读取')
                          : stage.klass === 'write'
                            ? '串行写入'
                            : '独占执行'
                        return [
                          <div
                            key={labelKey}
                            className={`chain-tool-stage-label chain-tool-stage-${stage.type}`}
                          >
                            <span className="chain-tool-stage-dot" aria-hidden />
                            <span className="chain-tool-stage-text">{stageLabel}</span>
                          </div>,
                          ...stageNodes,
                        ]
                      })
                    })()}
                  </div>
                </div>
              )
            }

            // 非组：单条 tool_call 直接走工厂
            return renderUniversalToolBlock(segment, segmentIndex, resultSegment)
          }

          // collab：单条直接走通用工厂（并发组外壳是 chat-only）
          return renderUniversalToolBlock(segment, segmentIndex, resultSegment)
        }

        if (segment.type === 'text') {
          if (variant !== 'chat' || !segment.content?.trim()) return null
          return (
            <div
              key={segmentIndex}
              className="message-content"
              dangerouslySetInnerHTML={{ __html: renderMarkdown ? renderMarkdown(segment.content) : segment.content }}
            />
          )
        }

        if (segment.type === 'system_notice') {
          if (variant === 'chat' && isContextCompactionNotice(segment.kind)) {
            const completed = segment.kind === 'context_compaction_completed'
            const hasCompletedAfter = segments.slice(segmentIndex + 1).some(
              (candidate) => candidate.type === 'system_notice'
                && candidate.kind === 'context_compaction_completed',
            )
            const hasActivityAfter = segments.slice(segmentIndex + 1).some(
              (candidate) => !(
                candidate.type === 'system_notice'
                && isContextCompactionNotice(candidate.kind)
              ),
            )

            // 开始态被完成态原位替换；模型一旦开始输出，完成态自动退场。
            if ((!completed && hasCompletedAfter) || (completed && (!streaming || hasActivityAfter))) {
              return null
            }
            return (
              <ContextCompactionNotice
                key={`${messageId}-compaction-${segmentIndex}`}
                completed={completed}
                content={segment.content || ''}
              />
            )
          }

          // 禁用 SchedulerPanoramaCard
          if (variant === 'chat' && segment.kind === 'scheduler_panorama') {
            return null
            // return (
            //   <SchedulerPanoramaCard
            //     key={`${messageId}-scheduler-${segmentIndex}`}
            //     segment={segment}
            //   />
            // )
          }

          // 渲染子代理卡片
          if (variant === 'chat' && segment.kind === 'subagent_started') {
            return (
              <SubAgentCard
                key={`${messageId}-subagent-${segment.payload?.subagent_id}`}
                segment={segment}
              />
            )
          }

          // command_chunk / fast_context_chunk segment 不再独立渲染
          if (segment.kind === 'command_chunk' || segment.kind === 'fast_context_chunk') {
            return null
          }

          // delegate_acp_agent 的 chunks 已经在 member tool 分支里通过 customResult 嵌进工具卡了，
          // 这里 standalone 渲染仅作为 fallback——只在 segments[] 里找不到对应 tool_call 时才渲染独立卡片
          // （兼容历史消息或 tool_call 跨消息的边缘场景）。
          if (variant === 'chat' && segment.kind === 'acp_delegate_chunk') {
            const did = getAcpDelegateChunkId(segment)
            const hasMatchingToolCall = !!did && segments.some((s) => {
              if (s.type !== 'tool_call' || s.tool_name !== 'delegate_acp_agent') return false
              return true
            })
            if (did && !hasMatchingToolCall && isFirstChunkForDelegate(segments, segmentIndex)) {
              const chunks = collectChunksForDelegate(segments, did)
              return (
                <AcpDelegateCard
                  key={`${messageId}-acp-${segmentIndex}`}
                  delegateId={did}
                  chunks={chunks}
                />
              )
            }
            return null
          }

          return (
            <RuntimeNoticeBlock
              key={`${messageId}-notice-${segmentIndex}`}
              blockKey={variant === 'chat' ? segmentIndex : `${messageId}-notice-${segmentIndex}`}
              kind={segment.kind}
              content={segment.content || ''}
              variant={variant}
              hasFollowingDetail={hasFollowingRuntimeDetail(segments, segmentIndex)}
            />
          )
        }

        return null
      })}
    </>
  )
}
