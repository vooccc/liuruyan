/**
 * SubAgentCard —— 2026-04-27 视觉系统统一改写
 *
 * 设计：
 * - 内部用 AgentTaskRow（kind='subagent'）渲染
 * - 订阅 subagentLiveBus 接收 subagent_thinking / tool_call / tool_result / completed / failed / timeout
 * - 把流式事件累计成 AgentTaskStat 形态喂给行组件（thinkingChars / toolsCalled / toolsCompleted / status）
 *
 * 数据流：
 * - 数据源是 subagentLiveBus（不进 store）
 * - 折叠/展开状态本地 useState
 *
 * 报告展开（resultText）：subagent_completed 事件目前不带完整 result_text，
 * 仅显示状态行。如后续后端补上 result/error，此处可填进 task.resultText/errorMessage。
 */

import { useEffect, useMemo, useState } from 'react'
import type { ContentSegment } from '../api/chat'
import { subagentLiveBus } from '../api/subagentLiveBus'
import { AgentTaskRow } from './agentTask/AgentTaskRow'
import { appendActivity } from './agentTask/activityHelpers'
import type { AgentTaskStat } from './agentTask/types'

interface SubAgentCardProps {
  segment: ContentSegment
}

export default function SubAgentCard({ segment }: SubAgentCardProps) {
  const payload = (segment.payload || {}) as Record<string, any>
  const subagentId: string | undefined = payload.subagent_id
  const initialIcon: string = payload.icon || ''
  const initialName: string = payload.name || '专家代理'

  const startedAt = useMemo(() => new Date().toISOString(), [])

  const [task, setTask] = useState<AgentTaskStat>({
    taskId: subagentId || `subagent-${Math.random().toString(36).slice(2, 9)}`,
    title: initialName,
    status: 'running',
    startedAt,
    updatedAt: startedAt,
    thinkingChars: 0,
    toolsCalled: 0,
    toolsCompleted: 0,
  })

  const [expanded, setExpanded] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())

  // 计时器仅在 running 时跑
  useEffect(() => {
    if (task.status !== 'running' && task.status !== 'queued') return
    const id = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [task.status])

  // 订阅 subagentLiveBus
  useEffect(() => {
    if (!subagentId) return
    const unsubscribe = subagentLiveBus.subscribe(subagentId, (ev) => {
      const kind = ev.kind
      const data = ev.payload || {}
      const nowIso = new Date().toISOString()
      setTask((prev) => {
        if (kind === 'subagent_content') {
          // content 是模型正文流（非 thinking 模型的主要进度信号源），每 200 字
          // 一个事件。**只更新数字进度 thinkingChars，不 push activity** ——
          // 否则流式文本碎片会把 recentActivity（cap 20 条）刷满，工具调用被
          // 顶出去，用户展开详情看到一堆文本碎片而不是工具调用列表。
          // （字段名 thinkingChars 语义实际是"模型输出字符总数"，跟 worker 路径
          //  useDispatchEventConsumer:597 的设计对齐。）
          return {
            ...prev,
            thinkingChars: data.total_length ?? prev.thinkingChars + 50,
            updatedAt: nowIso,
          }
        }
        if (kind === 'subagent_thinking') {
          // reasoning 流：有结构性语义（"模型在想什么"），值得上 activity 时间线。
          // payload.content 是后端发的最后 100 字文本，total_length 是累计总长度。
          const text = typeof data.content === 'string' ? data.content : ''
          return {
            ...prev,
            thinkingChars: data.total_length ?? prev.thinkingChars + (text.length || 50),
            recentActivity: text
              ? appendActivity(prev.recentActivity, { type: 'thinking', text, at: nowIso })
              : prev.recentActivity,
            updatedAt: nowIso,
          }
        }
        if (kind === 'subagent_tool_call') {
          // engine.py:307-311 payload.name + payload.input
          const toolName = String(data.name || 'tool')
          let preview = ''
          if (typeof data.input === 'string') preview = data.input
          else if (data.input && typeof data.input === 'object') {
            try { preview = JSON.stringify(data.input) } catch { preview = '' }
          }
          return {
            ...prev,
            toolsCalled: data.tools_called ?? prev.toolsCalled + 1,
            recentActivity: appendActivity(prev.recentActivity, {
              type: 'tool_call', text: preview, toolName, at: nowIso,
            }),
            updatedAt: nowIso,
          }
        }
        if (kind === 'subagent_tool_result') {
          // engine.py:316-320 payload.name + payload.output（后端已截断 200）
          const toolName = String(data.name || 'tool')
          const text = typeof data.output === 'string' ? data.output : ''
          return {
            ...prev,
            toolsCompleted: prev.toolsCompleted + 1,
            recentActivity: appendActivity(prev.recentActivity, {
              type: 'tool_result', text, toolName, at: nowIso,
            }),
            updatedAt: nowIso,
          }
        }
        if (kind === 'subagent_completed') {
          // engine.py 发的字段是 `output`（不是 result_text/result）。result_text/result
          // 兜底兼容老版本以防回滚。
          return {
            ...prev,
            status: 'completed',
            resultText: data.output || data.result_text || data.result,
            completedAt: nowIso,
            updatedAt: nowIso,
          }
        }
        if (kind === 'subagent_failed') {
          const resultText = data.output || data.result_text || data.result
          return {
            ...prev,
            status: 'failed',
            errorMessage: data.error || '未拿到错误详情。去后端控制台查 [SUBAGENT-] 日志。',
            resultText,
            completedAt: nowIso,
            updatedAt: nowIso,
          }
        }
        if (kind === 'subagent_timeout') {
          const resultText = data.output || data.result_text || data.result
          return {
            ...prev,
            status: 'failed',
            errorMessage: data.error || `执行超时（已调用 ${prev.toolsCalled} 个工具，${prev.toolsCompleted} 个完成）`,
            resultText,
            completedAt: nowIso,
            updatedAt: nowIso,
          }
        }
        return prev
      })
    })
    return unsubscribe
  }, [subagentId])

  // emoji 头像（如后端给了 icon）：通过 iconOverride 注入到行组件头部
  const iconOverride = initialIcon ? <span style={{ fontSize: 13 }}>{initialIcon}</span> : undefined

  return (
    <div className="dispatch-card-stack dispatch-card-stack-inline">
      <AgentTaskRow
        task={task}
        kind="subagent"
        expanded={expanded}
        onToggle={() => setExpanded((v) => !v)}
        nowMs={nowMs}
        iconOverride={iconOverride}
      />
    </div>
  )
}
