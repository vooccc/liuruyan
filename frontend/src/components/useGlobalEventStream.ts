/**
 * useGlobalEventStream — 跨 session 全局事件 SSE 订阅
 *
 * 后端 /events/global 端点（main.py）走 v2 EventBus fan-out，白名单仅
 * 推 cron_fired 等"用户跨 session 关心"的事件类型。本 hook 把它们映射
 * 成 ToastSystem 的 toast。
 *
 * 设计稿：docs/cron_toast_design.md
 *
 * 注意事项：
 * - 多 tab 同开会同时收到事件；按 event_id dedup（容量 100 自动 GC 一半）
 * - EventSource 浏览器原生自动重连，不用手动处理 onerror
 * - hello / heartbeat 系统消息直接吃掉不弹 toast
 * - 后端推帧带 `event: xxx` 字段时浏览器只走 addEventListener，**不走
 *   onmessage**——必须按事件名分别订阅（详见 MEMORY
 *   feedback_eventsource_addeventlistener）
 */

import { useEffect, useRef } from 'react'
import { API_BASE } from '../api/base'
import type { Agent } from '../api/chat'
import { useToast } from './ToastSystem'
import { notifyTasksChanged } from '../stores/tasksStore'

interface GlobalEvent {
  event_id: string | number
  type: string
  trace_id?: string
  session_id?: string
  agent_id?: string
  payload?: Record<string, any>
  ts?: number
}

const SEEN_CAP = 100

/** cron 表达式 → 中文自然语言（覆盖常用模式，不识别的 fallback 原表达式）。 */
function cronToNatural(expr: string): string {
  if (!expr) return ''
  const parts = expr.trim().split(/\s+/)
  if (parts.length !== 5) return expr
  const [m, h, dom, mon, dow] = parts
  const isNum = (s: string) => /^\d+$/.test(s)
  const fmt = (hh: string, mm: string) => `${hh.padStart(2, '0')}:${mm.padStart(2, '0')}`

  // 每分钟
  if (m === '*' && h === '*' && dom === '*' && mon === '*' && dow === '*') return '每分钟'
  // 每 N 分钟
  const stepMatch = m.match(/^\*\/(\d+)$/)
  if (stepMatch && h === '*' && dom === '*' && mon === '*' && dow === '*') return `每 ${stepMatch[1]} 分钟`
  // 每小时 N 分
  if (isNum(m) && h === '*' && dom === '*' && mon === '*' && dow === '*') return `每小时 ${m} 分`

  // 工作日某时刻
  if (isNum(m) && isNum(h) && dom === '*' && mon === '*' && dow === '1-5') return `工作日 ${fmt(h, m)}`
  // 每周 N 某时刻
  if (isNum(m) && isNum(h) && dom === '*' && mon === '*' && isNum(dow)) {
    const days = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
    return `每${days[parseInt(dow) % 7]} ${fmt(h, m)}`
  }
  // 每月 D 号某时刻
  if (isNum(m) && isNum(h) && isNum(dom) && mon === '*' && dow === '*') return `每月 ${dom} 号 ${fmt(h, m)}`
  // 每天某时刻
  if (isNum(m) && isNum(h) && dom === '*' && mon === '*' && dow === '*') return `每天 ${fmt(h, m)}`
  // 一次性具体日期：N N D M *
  if (isNum(m) && isNum(h) && isNum(dom) && isNum(mon) && dow === '*') return `${mon}/${dom} ${fmt(h, m)}`

  return expr
}

export function useGlobalEventStream(agents: Agent[] = []) {
  const { addToast } = useToast()
  const seenIds = useRef<Set<string>>(new Set())
  const agentsRef = useRef(agents)
  agentsRef.current = agents  // 保持最新引用，handler 闭包内拿得到当前值

  useEffect(() => {
    const es = new EventSource(`${API_BASE}/events/global`)

    // ⚠️ SSE 规范：后端推 `event: cron_fired` 字段时只触发 addEventListener，
    // 不走 onmessage。必须按事件名分别订阅。
    const handle = (e: MessageEvent) => {
      let event: GlobalEvent
      try {
        event = JSON.parse(e.data)
      } catch {
        return
      }
      // hello / heartbeat / 缺 id 的事件不弹 toast
      if (!event || !event.event_id || event.type === 'hello') return
      const idStr = String(event.event_id)
      if (seenIds.current.has(idStr)) return
      seenIds.current.add(idStr)
      // 容量上限：过半就把前一半扔掉，保留较新的
      if (seenIds.current.size > SEEN_CAP) {
        const arr = Array.from(seenIds.current)
        seenIds.current = new Set(arr.slice(Math.floor(arr.length / 2)))
      }

      // tasks_changed：仅通知 InlineTasksPanel refetch，不弹 toast。
      // payload 不含完整列表，前端订阅方拿信号去 refetch /tasks?session_id=
      // session_id 为空（clear all 等）则 broadcast 给所有订阅的 sid
      if (event.type === 'tasks_changed') {
        notifyTasksChanged(event.session_id ?? '')
        return
      }

      if (event.type === 'cron_completed' || event.type === 'cron_failed') {
        const p = event.payload ?? {}
        const desc = p.description ?? '定时任务'
        const ok = event.type === 'cron_completed'
        addToast({
          type: 'info',
          title: ok ? `定时任务已完成：${desc}` : `定时任务执行失败：${desc}`,
          desc: ok ? '结果已写回对应聊天会话。' : String(p.error || '请打开对应会话查看失败详情。'),
          icon: ok ? '✓' : '!',
          urgency: ok ? 'low' : 'high',
          color: ok ? '#22c55e' : '#ef4444',
          duration: ok ? 6000 : 10000,
        })
        return
      }

      if (event.type === 'cron_fired') {
        const p = event.payload ?? {}
        const desc = p.description ?? '定时任务'
        const cron = p.cron ?? ''
        const isOnce = p.mode === 'once'
        const nextFireAt: string | null = p.next_fire_at ?? null
        const agentId = event.agent_id ?? ''

        // 把 agent_id 翻译成角色名 + 头像（拿不到时退回 id 字符串显示）
        // 头像优先级跟 Sidebar.getAvatarSrc 一致：
        //   avatar_url（拼 API_BASE） > avatar 是 data:/http 直接用 > emoji fallback
        const agentEntry = agentsRef.current.find(a => a.id === agentId)
        const agentName = agentEntry?.name || agentId || '当前角色'
        let avatarUrl: string | undefined
        let avatarEmoji: string | undefined
        if (agentEntry) {
          if (agentEntry.avatar_url) {
            avatarUrl = `${API_BASE}${agentEntry.avatar_url}`
          } else if (agentEntry.avatar && (agentEntry.avatar.startsWith('data:') || agentEntry.avatar.startsWith('http'))) {
            avatarUrl = agentEntry.avatar
          } else if (agentEntry.avatar) {
            avatarEmoji = agentEntry.avatar
          }
        }

        // 下次时间格式化：今天 / 明天 / 月-日 hh:mm
        const formatNext = (iso: string | null): string => {
          if (!iso) return '—'
          const d = new Date(iso)
          if (isNaN(d.getTime())) return iso
          const now = new Date()
          const hh = d.getHours().toString().padStart(2, '0')
          const mm = d.getMinutes().toString().padStart(2, '0')
          if (d.toDateString() === now.toDateString()) return `今天 ${hh}:${mm}`
          const tmr = new Date(now); tmr.setDate(tmr.getDate() + 1)
          if (d.toDateString() === tmr.toDateString()) return `明天 ${hh}:${mm}`
          return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`
        }
        const nextText = isOnce
          ? '已完成 · 可手动清理'
          : nextFireAt ? `下次 ${formatNext(nextFireAt)}` : ''

        addToast({
          type: 'cron',
          title: desc,
          desc: '',  // cron type 走 cronDetails，desc 不显示
          icon: '⏰',
          urgency: 'medium',
          color: '#60a5fa',
          duration: 10000,  // 用户偏好：cron toast 停 10s 充分看清
          cronDetails: {
            agent_name: agentName,
            agent_avatar_url: avatarUrl,
            agent_avatar_emoji: avatarEmoji,
            cron,
            cron_natural: cronToNatural(cron),
            mode_label: isOnce ? '一次性' : '周期任务',
            once_completed: isOnce,
            next_text: nextText,
          },
        })
      }
    }

    es.addEventListener('cron_fired', handle)
    es.addEventListener('cron_completed', handle)
    es.addEventListener('cron_failed', handle)
    es.addEventListener('tasks_changed', handle)
    es.onmessage = handle  // 兜底（默认 type=message 的事件）

    return () => {
      es.close()
    }
  }, [addToast])
}
