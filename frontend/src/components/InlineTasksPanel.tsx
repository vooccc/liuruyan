/**
 * InlineTasksPanel — 对话流内嵌的任务进度 timeline
 *
 * 设计目标：流畅、不卡顿、有沉浸感、克制不张扬。
 *
 * 视觉语言（融合 Claude Code TaskListV2 + Codeium Cascade 圆圈范式）：
 *   - 圆圈状态符（pending=○ stroke / in_progress=◐ + 旋转 / completed=●+✓ / failed=●+✕）
 *   - 1px dashed 弱连接线（不要浓重 timeline 感）
 *   - 30s "刚完成"高亮：background 微脉冲，渐隐
 *   - allDone 整组淡出：0.5s fade-out → 1s "全部任务已完成"回执 → 消失
 *   - 配色克制：completed 用偏冷低饱和绿 #5a9b7a（不是 --accent-green 鲜亮）
 *
 * 数据流：
 *   - 接 sid prop → useSessionTasks → store + SSE 实时刷新
 *   - 不持有自己的 tasks state，避免双源真值
 *
 * 双形态挂载：本组件由 Chat 用 createPortal 切到 timeline 末尾 / 输入框上方两个锚点；
 *   组件本身不感知形态，靠外层 Portal target 切换。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE } from '../api/base'
import { useSessionTasks } from '../hooks/useSessionTasks'
import { useTasksStore, type Task, type TaskStatus } from '../stores/tasksStore'

interface InlineTasksPanelProps {
  sid: string
}

const COMPLETED_HIGHLIGHT_MS = 30_000
const FADE_OUT_MS = 500
const RECEIPT_HOLD_MS = 1000  // receipt 持续 1s 后真正消失

type Phase = 'normal' | 'fading' | 'receipt' | 'gone'

export default function InlineTasksPanel({ sid }: InlineTasksPanelProps) {
  const { tasks } = useSessionTasks(sid)

  // 执行过的任务按首次开始时间排列；尚未执行的任务按创建时间接在后面。
  // 不使用 updated_at，避免完成或补充元数据时任务再次跳位。
  const sortedTasks = useMemo(() => [...tasks].sort((a, b) => {
    const aStarted = a.started_at ?? (a.status === 'in_progress' ? a.updated_at : null)
    const bStarted = b.started_at ?? (b.status === 'in_progress' ? b.updated_at : null)
    if (aStarted !== null && bStarted !== null) return aStarted - bStarted
    if (aStarted !== null) return -1
    if (bStarted !== null) return 1
    return a.created_at - b.created_at
  }), [tasks])

  // 30s 高亮跟踪：task.id → transitionedToCompletedAt timestamp
  const completedAtRef = useRef<Map<string, number>>(new Map())
  const [highlightTick, setHighlightTick] = useState(0)

  useEffect(() => {
    const now = Date.now()
    const seenCompleted = new Set<string>()
    for (const t of sortedTasks) {
      if (t.status === 'completed') {
        seenCompleted.add(t.id)
        if (!completedAtRef.current.has(t.id)) {
          completedAtRef.current.set(t.id, now)
        }
      }
    }
    // 清理：已不存在 / 不再 completed 的 id
    for (const id of Array.from(completedAtRef.current.keys())) {
      if (!seenCompleted.has(id)) completedAtRef.current.delete(id)
    }
    // 调度最早过期那个的 re-render，让高亮自然结束
    let earliest = Infinity
    for (const ts of completedAtRef.current.values()) {
      const expire = ts + COMPLETED_HIGHLIGHT_MS
      if (expire > now && expire < earliest) earliest = expire
    }
    if (earliest === Infinity) return
    const timer = window.setTimeout(
      () => setHighlightTick((n) => n + 1),
      earliest - now + 50  // 偏 50ms 确保过期
    )
    return () => window.clearTimeout(timer)
  }, [sortedTasks])

  // allDone 状态机：phase 由 store 里的 allDoneAt 时间戳 + 现在时间计算，
  // 不持有 useState。原因：本组件被 Chat.tsx 用 createPortal 在 timelineAnchor /
  // inputAnchor 间切换 target，container 变化时 React 会 unmount + remount，
  // 内部 useState 会丢失 → 用户看到"已淡出的列表，发新消息时跳回来一下再消失"。
  // 时间戳走 store + wall-clock，重 mount 也算得出当前应处的相位。
  // 终态判定：completed / failed / stale 都算结束（避免混入的失败任务把
  // 整组永久卡在 normal 相位，触发"任务异常后输入框上方残留不消失"）。
  // pending / in_progress 仍保持活跃，不推进 fade-out。
  const allDone = useMemo(
    () => (
      sortedTasks.length > 0
      && sortedTasks.every((t) => (
        t.status === 'completed' || t.status === 'failed' || t.status === 'stale'
      ))
    ),
    [sortedTasks]
  )
  const allDoneAt = useTasksStore((s) => s.allDoneAt[sid])
  const markAllDone = useTasksStore((s) => s.markAllDone)
  const clearAllDone = useTasksStore((s) => s.clearAllDone)

  // 维护 allDoneAt：allDone 翻转时同步标记/清除
  useEffect(() => {
    if (allDone) markAllDone(sid)
    else clearAllDone(sid)
  }, [allDone, sid, markAllDone, clearAllDone])

  // setTick 强制重渲染，让 phase memo 在 setTimeout 后重算
  const [tick, setTick] = useState(0)

  const phase: Phase = useMemo(() => {
    if (!allDone || !allDoneAt) return 'normal'
    const elapsed = Date.now() - allDoneAt
    if (elapsed < FADE_OUT_MS) return 'fading'
    if (elapsed < FADE_OUT_MS + RECEIPT_HOLD_MS) return 'receipt'
    return 'gone'
    // tick 是 re-render trigger，不参与计算但要进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allDone, allDoneAt, tick])

  // 调度 phase 推进的 re-render
  useEffect(() => {
    if (!allDoneAt || phase === 'gone') return
    const elapsed = Date.now() - allDoneAt
    let nextDelay: number
    if (phase === 'fading') nextDelay = FADE_OUT_MS - elapsed + 20
    else /* receipt */      nextDelay = FADE_OUT_MS + RECEIPT_HOLD_MS - elapsed + 20
    if (nextDelay <= 0) {
      // 落后于墙钟，立即推一帧
      const raf = window.requestAnimationFrame(() => setTick((n) => n + 1))
      return () => window.cancelAnimationFrame(raf)
    }
    const timer = window.setTimeout(() => setTick((n) => n + 1), nextDelay)
    return () => window.clearTimeout(timer)
  }, [allDoneAt, phase])

  // phase=gone 时 fire-and-forget 让后端真正清掉 completed 任务，
  // 否则下次新建 task 时 store 会拉到旧 + 新两组堆叠。
  // archivedAtRef 防重：同一批 allDoneAt 时间戳只发一次（Portal target 切换重 mount 仍幂等）。
  const archivedAtRef = useRef<number | null>(null)
  useEffect(() => {
    if (phase !== 'gone' || !allDoneAt || !sid) return
    if (archivedAtRef.current === allDoneAt) return
    archivedAtRef.current = allDoneAt
    // 清 completed + failed + stale 三类终态任务；pending / in_progress 不动
    // （避免 failed 任务留在后端下次拉回来，和新 task 合起来永久卡在 normal）。
    fetch(`${API_BASE}/tasks/ended?session_id=${encodeURIComponent(sid)}`, {
      method: 'DELETE',
    }).catch((e) => console.debug('[InlineTasksPanel] archive failed:', e))
  }, [phase, allDoneAt, sid])

  // 空 tasks → 整组隐形（保留 div 占位以稳住外层 Portal anchor 高度，但内容空）
  if (sortedTasks.length === 0) return null

  // gone：彻底隐藏（allDone + 1.5s 已过）
  if (phase === 'gone') return null

  // receipt：只显示"全部任务已完成"回执（扁平，融入对话流）
  if (phase === 'receipt') {
    return (
      <div className="inline-tasks-panel inline-tasks-panel--receipt">
        <div className="inline-tasks-receipt-badge">
          <CircleSvg status="completed" />
        </div>
        <span className="inline-tasks-receipt-text">全部任务已完成</span>
        <PanelStyle />
      </div>
    )
  }

  // 进度计算（驱动连接线 solid overlay 高度）
  const completedCount = sortedTasks.filter((t) => t.status === 'completed').length
  const total = sortedTasks.length
  const progress = total > 0 ? completedCount / total : 0

  // normal / fading：扁平 timeline（无容器边框，沉浸融入对话）
  return (
    <div
      className={[
        'inline-tasks-panel',
        phase === 'fading' ? 'inline-tasks-panel--fading' : '',
      ].join(' ').trim()}
      style={{
        // CSS 变量驱动连接线 solid 段高度
        ['--tasks-progress' as any]: progress,
      }}
      data-tick={highlightTick}
    >
      <div className="inline-tasks-list">
        {sortedTasks.map((task, idx) => {
          const completedAt = completedAtRef.current.get(task.id)
          const isJustCompleted =
            task.status === 'completed' &&
            completedAt !== undefined &&
            Date.now() - completedAt < COMPLETED_HIGHLIGHT_MS
          return (
            <TaskRow
              key={task.id}
              task={task}
              index={idx}
              isJustCompleted={isJustCompleted}
            />
          )
        })}
      </div>
      <PanelStyle />
    </div>
  )
}

// ─────── 单行 ─────────

function TaskRow({
  task,
  index,
  isJustCompleted,
}: {
  task: Task
  index: number
  isJustCompleted: boolean
}) {
  // description 只在 in_progress 时展开（windsurf "active task 放大" 范式）：
  // 历史/未来任务的 desc 几乎都是噪音，唯一有信息密度的是"现在为什么做这一步"。
  const showDesc = task.status === 'in_progress' && !!task.description
  return (
    <div
      className={[
        'inline-tasks-row',
        `inline-tasks-row--${task.status}`,
        isJustCompleted ? 'inline-tasks-row--just-completed' : '',
      ].join(' ').trim()}
      // --i 驱动 stagger：每行 delay 40ms * index（整组入场仪式感）
      style={{ ['--i' as any]: index }}
    >
      {/* in_progress 行的左侧 2px accent bar（光带），纯装饰；非 in_progress 不渲染保持留白 */}
      {task.status === 'in_progress' && (
        <span className="inline-tasks-row__accent" aria-hidden="true" />
      )}
      <div className={`inline-tasks-row__circle inline-tasks-circle inline-tasks-circle--${task.status}`}>
        <CircleSvg status={task.status} />
      </div>
      <div className="inline-tasks-row__text">
        <div className="inline-tasks-row__title">{task.title}</div>
        {/* 现代塌缩范式：grid-template-rows 0fr ↔ 1fr，避免 max-height 触发 layout thrash */}
        <div
          className={[
            'inline-tasks-row__desc-wrap',
            showDesc ? 'inline-tasks-row__desc-wrap--open' : '',
          ].join(' ').trim()}
        >
          <div className="inline-tasks-row__desc-inner">
            {task.description && (
              <div className="inline-tasks-row__desc">{task.description}</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ─────── 圆圈 SVG（16px） ─────────

function CircleSvg({ status }: { status: TaskStatus }) {
  // viewBox 24×24, stroke 1.5px, radius 9 → 视觉直径 ~16px（含 stroke）
  switch (status) {
    case 'pending':
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      )
    case 'in_progress':
      // dasharray 让圆只画一段，配合 CSS rotate 形成"半圆旋转"视觉
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" className="inline-tasks-circle__spin">
          <circle
            cx="12"
            cy="12"
            r="9"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeDasharray="40 60"
          />
        </svg>
      )
    case 'completed':
      // 圆底色 scale-in + 勾 stroke-dashoffset draw（见 CSS @keyframes inlineTasksCheckDraw / CircleFill）
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" className="inline-tasks-circle__completed-svg">
          <circle cx="12" cy="12" r="9" fill="currentColor" className="inline-tasks-circle__fill" />
          <path
            d="M8 12.5l2.5 2.5L16 9.5"
            stroke="#fff"
            strokeWidth="1.9"
            strokeLinecap="round"
            strokeLinejoin="round"
            fill="none"
            className="inline-tasks-circle__check"
            pathLength={20}
          />
        </svg>
      )
    case 'failed':
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="9" fill="currentColor" />
          <path
            d="M9 9l6 6M15 9l-6 6"
            stroke="#fff"
            strokeWidth="1.8"
            strokeLinecap="round"
            fill="none"
          />
        </svg>
      )
    case 'stale':
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <circle
            cx="12"
            cy="12"
            r="9"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeDasharray="2 2"
          />
        </svg>
      )
    default:
      return null
  }
}

// ─────── 样式 ─────────

function PanelStyle() {
  return (
    <style>{`
      /* ════════════════════════════════════════════════════════════════
         Tokens (scoped)
         ──────────────────────────────────────────────
         颜色走 color-mix + var，保持 dark/light 主题自适应；
         semantic accent（蓝=进行 / 绿=完成 / 红=失败 / 紫=归档）集中在此修改。
         ═══════════════════════════════════════════════════════════════ */
      /* 扁平 timeline：无容器 chrome，沉浸融入对话流。
         只保留语义 accent 变量 + 入场动画；所有"卡片感"（border/bg/shadow/header/
         顶部进度条）都已删除——连接线 solid overlay 已经是最自然的进度隐喻。 */
      .inline-tasks-panel {
        /* 水墨极简调色盘：降饱和、偏冷、有烟气。
           参考「柳如烟」项目调性——不用鲜亮互补色，用同色系 muted 变奏。
           语义保留（蓝=进 / 绿=完 / 红=失 / 紫=归），但都压到 40% 饱和度以下。 */
        --tk-accent-run:   #8ba3b8;  /* steel slate blue */
        --tk-accent-done:  #7a9b83;  /* 烟青绿 */
        --tk-accent-fail:  #b58581;  /* 烟红 */
        --tk-accent-stale: #a49db6;  /* 烟紫灰 */

        position: relative;
        /* 左右各让 44px = avatar 34 + gap 10，与 .message-row.assistant 气泡边界对齐
           （右边的 44 对应 .message-row.assistant .message-body padding-right 同值）。 */
        margin: 12px 44px;
        opacity: 1;
        transform: translateY(0);
        transition: opacity 200ms ease, transform 200ms ease;
        animation: inlineTasksFadeIn 280ms cubic-bezier(.2,.8,.2,1) both;
      }

      /* Fading 阶段：opacity + 高度塌缩 */
      .inline-tasks-panel--fading {
        overflow: hidden;
        animation: inlineTasksFadeOut ${FADE_OUT_MS}ms cubic-bezier(.4,0,.2,1) forwards;
      }

      /* ────── Receipt 态：扁平收束，无 pill 外壳 ───── */
      .inline-tasks-panel--receipt {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 4px 0;
        margin: 12px 44px;
        overflow: hidden;
        animation: inlineTasksReceiptIn 320ms cubic-bezier(.2,.8,.2,1) both;
      }
      .inline-tasks-receipt-badge {
        position: relative;
        width: 16px;
        height: 16px;
        display: flex;
        align-items: center;
        justify-content: center;
        color: var(--tk-accent-done);
      }
      /* 徽章一次性光晕脉冲（用 ::after 独立打，避免影响 layout） */
      .inline-tasks-receipt-badge::after {
        content: '';
        position: absolute;
        inset: -4px;
        border-radius: 50%;
        pointer-events: none;
        animation: inlineTasksReceiptGlow 900ms ease-out 120ms both;
      }
      .inline-tasks-receipt-text {
        font-size: 13.5px;
        font-weight: 400;
        color: var(--text-secondary);
        letter-spacing: 0.3px;
      }

      /* ────── List + 连接线 ───── */
      .inline-tasks-list {
        position: relative;
        display: flex;
        flex-direction: column;
      }
      /* 底层 dashed 连接线：全长。
         去色化——中性灰淡线。线不抢色，圆圈才是色的载体。
         top/bottom: 16 让线端点被首/末圆圈的背景 mask 干净遮住（圆心 y≈10，
         线从 y=16 进入圆底部被 mask → 视觉上"线从圆里出来"而不是贴着圆外侧）。 */
      .inline-tasks-list::before {
        content: '';
        position: absolute;
        left: 8px;
        top: 16px;
        bottom: 16px;
        border-left: 1px dashed color-mix(in srgb, var(--text-muted) 26%, transparent);
        pointer-events: none;
      }
      /* 覆盖层 solid 连接线：长度 = progress%。
         同样走中性灰（比 dashed 更实），只靠"实/虚"区分进度不靠颜色——
         避免与 completed 圆（烟青绿）糊在一起。 */
      .inline-tasks-list::after {
        content: '';
        position: absolute;
        left: 8px;
        top: 16px;
        height: calc((100% - 32px) * var(--tasks-progress, 0));
        border-left: 1px solid color-mix(in srgb, var(--text-muted) 52%, transparent);
        pointer-events: none;
        transition: height 680ms cubic-bezier(.2,.8,.2,1);
      }

      /* ────── Row ───── */
      /* 扁平：无行背景、无 accent bar；in_progress 的存在感交给圆圈 halo + 标题字重。
         padding-left=0 保证圆心 x ≡ 8，与连接线 left:7 对齐。 */
      .inline-tasks-row {
        position: relative;
        display: flex;
        align-items: flex-start;
        gap: 12px;
        padding: 4px 0;
        /* stagger 入场：按 index 40ms 错开 */
        animation: inlineTasksRowIn 360ms cubic-bezier(.2,.8,.2,1) both;
        animation-delay: calc(var(--i, 0) * 40ms);
      }

      /* 兼容：旧 DOM 里 JSX 仍会渲染 accent span（in_progress 行）但我们不想要视觉，
         直接隐藏。保留结构避免组件分支复杂化。 */
      .inline-tasks-row__accent { display: none; }

      .inline-tasks-row__circle {
        position: relative;
        z-index: 1;
        flex-shrink: 0;
        width: 16px;
        height: 16px;
        margin-top: 2px;
        display: flex;
        align-items: center;
        justify-content: center;
        /* 用 panel 背景覆盖连接线与圆的交叉点；卡片内外都 OK */
        background: var(--bg-color);
        border-radius: 50%;
        transition: transform 280ms cubic-bezier(.2,.8,.2,1);
      }
      .inline-tasks-circle--pending    { color: var(--text-muted); }
      .inline-tasks-circle--in_progress{ color: var(--tk-accent-run); }
      .inline-tasks-circle--completed  { color: var(--tk-accent-done); }
      .inline-tasks-circle--failed     { color: var(--tk-accent-fail); }
      .inline-tasks-circle--stale      { color: var(--text-muted); opacity: 0.6; }

      .inline-tasks-circle__spin {
        animation: inlineTasksRotate 1.25s linear infinite;
        transform-origin: center;
      }
      /* in_progress 圆圈 halo：极淡的 accent 光晕，强化存在感 */
      .inline-tasks-circle--in_progress::before {
        content: '';
        position: absolute;
        inset: -3px;
        border-radius: 50%;
        background: radial-gradient(
          circle,
          color-mix(in srgb, var(--tk-accent-run) 22%, transparent) 0%,
          transparent 70%
        );
        animation: inlineTasksHaloBreathe 2.2s ease-in-out infinite;
        pointer-events: none;
      }

      /* completed 圆圈 draw-in：圆底 scale + 勾 stroke dashoffset */
      .inline-tasks-circle__completed-svg {
        animation: inlineTasksCompletedPop 420ms cubic-bezier(.2,1.4,.3,1) both;
      }
      .inline-tasks-circle__fill {
        transform-origin: center;
        animation: inlineTasksCircleFill 320ms cubic-bezier(.2,.9,.2,1) both;
      }
      .inline-tasks-circle__check {
        stroke-dasharray: 20;
        stroke-dashoffset: 20;
        animation: inlineTasksCheckDraw 280ms cubic-bezier(.4,0,.2,1) 140ms both;
      }

      .inline-tasks-row__text {
        flex: 1;
        min-width: 0;
        font-family: inherit;
      }
      .inline-tasks-row__title {
        font-size: 14.5px;
        line-height: 1.5;
        color: var(--text-color);
        letter-spacing: 0.15px;
        transition: color 240ms ease, font-weight 240ms ease;
      }

      /* Desc 现代塌缩：grid-template-rows 0fr → 1fr，不 triggers layout thrash */
      .inline-tasks-row__desc-wrap {
        display: grid;
        grid-template-rows: 0fr;
        transition: grid-template-rows 360ms cubic-bezier(.2,.8,.2,1);
      }
      .inline-tasks-row__desc-wrap--open {
        grid-template-rows: 1fr;
      }
      .inline-tasks-row__desc-inner {
        min-height: 0;
        overflow: hidden;
      }
      .inline-tasks-row__desc {
        margin-top: 3px;
        font-size: 13px;
        line-height: 1.55;
        color: var(--text-secondary);
        font-weight: 400;
        letter-spacing: 0.1px;
      }

      .inline-tasks-row--in_progress .inline-tasks-row__title {
        color: var(--text-color);
        font-weight: 550;
      }
      .inline-tasks-row--completed .inline-tasks-row__title,
      .inline-tasks-row--completed .inline-tasks-row__desc {
        text-decoration: line-through;
        text-decoration-thickness: 0.5px;
        text-decoration-color: color-mix(in srgb, var(--text-muted) 50%, transparent);
        color: var(--text-secondary);
        opacity: 0.7;
      }
      .inline-tasks-row--failed .inline-tasks-row__title { color: var(--tk-accent-fail); }
      .inline-tasks-row--stale .inline-tasks-row__title,
      .inline-tasks-row--stale .inline-tasks-row__desc {
        opacity: 0.5;
      }

      /* just-completed 行高亮：已移除。
         原设计是 30s 背景绿 tint 衰减，但 row 是 full-width flex bar，
         背景色会形成"绿色长方形贴着圆圈左边"的容器感——与扁平沉浸气质冲突。
         完成反馈现由圆圈 pop-in 420ms + 勾 stroke draw-in 280ms 承担，已足够。
         保留 isJustCompleted prop + completedAtRef 不动（API 稳定，组件仍可选择用）。 */
      .inline-tasks-row--just-completed { /* no-op */ }

      /* ═══════════ Keyframes ═══════════ */
      @keyframes inlineTasksRotate {
        from { transform: rotate(0deg); }
        to   { transform: rotate(360deg); }
      }
      @keyframes inlineTasksFadeIn {
        from { opacity: 0; transform: translateY(4px); }
        to   { opacity: 1; transform: translateY(0); }
      }
      @keyframes inlineTasksRowIn {
        from { opacity: 0; transform: translateY(4px); }
        to   { opacity: 1; transform: translateY(0); }
      }
      @keyframes inlineTasksHaloBreathe {
        0%, 100% { opacity: 0.6; transform: scale(0.95); }
        50%      { opacity: 1;   transform: scale(1.05); }
      }
      @keyframes inlineTasksCompletedPop {
        0%   { transform: scale(0.6); }
        60%  { transform: scale(1.08); }
        100% { transform: scale(1); }
      }
      @keyframes inlineTasksCircleFill {
        from { transform: scale(0); }
        to   { transform: scale(1); }
      }
      @keyframes inlineTasksCheckDraw {
        from { stroke-dashoffset: 20; }
        to   { stroke-dashoffset: 0; }
      }
      /* fade out 同时塌缩高度 + padding + margin，让外层布局连续上移；
         max-height 用 800px 作"足够大"封顶，前 ~85% 时间 panel 实际高度由 content 主导，
         后 ~15% 才真正 truncate——但 padding/margin 全程平滑过渡，是用户感知到的"塌缩"。 */
      @keyframes inlineTasksFadeOut {
        0% {
          opacity: 1;
          transform: translateY(0);
          max-height: 800px;
          margin-top: 14px;
          margin-bottom: 14px;
          padding-top: 14px;
          padding-bottom: 12px;
        }
        40% { opacity: 0.25; }
        100% {
          opacity: 0;
          transform: translateY(-2px);
          max-height: 0;
          margin-top: 0;
          margin-bottom: 0;
          padding-top: 0;
          padding-bottom: 0;
        }
      }
      /* receipt 从 0 高度 grow 出来，跟 fade 终态衔接，整体高度连续推进无突跳 */
      @keyframes inlineTasksReceiptIn {
        0% {
          opacity: 0;
          transform: translateY(-2px);
          max-height: 0;
          margin-top: 0;
          margin-bottom: 0;
          padding-top: 0;
          padding-bottom: 0;
        }
        100% {
          opacity: 1;
          transform: translateY(0);
          max-height: 60px;
          margin-top: 14px;
          margin-bottom: 14px;
          padding-top: 10px;
          padding-bottom: 10px;
        }
      }
      /* receipt 徽章一次性光晕：从中心向外 puff + 淡出 */
      @keyframes inlineTasksReceiptGlow {
        0%   {
          background: radial-gradient(circle,
            color-mix(in srgb, var(--tk-accent-done) 45%, transparent) 0%,
            transparent 60%);
          transform: scale(0.7);
          opacity: 1;
        }
        60%  { transform: scale(1.6); opacity: 0.35; }
        100% { transform: scale(1.8); opacity: 0; background: transparent; }
      }

      /* reduced-motion：把所有动画降频到 opacity 过渡 */
      @media (prefers-reduced-motion: reduce) {
        .inline-tasks-panel,
        .inline-tasks-row,
        .inline-tasks-circle__completed-svg,
        .inline-tasks-circle__fill,
        .inline-tasks-circle__check,
        .inline-tasks-circle__spin,
        .inline-tasks-circle--in_progress::before,
        .inline-tasks-row__accent,
        .inline-tasks-progress__fill::after {
          animation: none !important;
          transition-duration: 120ms !important;
        }
      }
    `}</style>
  )
}
