/**
 * Worker / Subagent 共享的活动时间线累积逻辑。
 *
 * 抽出来是因为 worker 走 useDispatchEventConsumer 的 reducer，
 * subagent 走 subagentLiveBus 的 useEffect，两条路径独立但语义一致。
 *
 * 规则（跟 reducer 里原版保持一致）：
 * - thinking 5s 时间窗内同类型事件合并：拼接 text，不新建条目
 * - tool_call / tool_result 各为独立条目
 * - 单条 text 截断 200 字防爆
 * - 总条数封顶 20 条，溢出 shift 老的
 */

import type { AgentTaskActivity } from './types'

export const MAX_ACTIVITY_ITEMS = 20
export const MAX_ACTIVITY_TEXT = 200
export const THINKING_MERGE_WINDOW_MS = 5000

export function appendActivity(
  prev: AgentTaskActivity[] | undefined,
  item: { type: AgentTaskActivity['type']; text: string; toolName?: string; at: string },
): AgentTaskActivity[] {
  const list = prev ? prev.slice() : []
  const last = list[list.length - 1]
  const text = item.text.length > MAX_ACTIVITY_TEXT
    ? item.text.slice(0, MAX_ACTIVITY_TEXT) + '…'
    : item.text

  if (
    item.type === 'thinking'
    && last
    && last.type === 'thinking'
    && (new Date(item.at).getTime() - new Date(last.at).getTime()) < THINKING_MERGE_WINDOW_MS
  ) {
    const merged = (last.text + text)
    list[list.length - 1] = {
      ...last,
      text: merged.length > MAX_ACTIVITY_TEXT ? merged.slice(-MAX_ACTIVITY_TEXT) : merged,
      at: item.at,
    }
  } else {
    list.push({
      id: `${item.at}-${Math.random().toString(36).slice(2, 8)}`,
      type: item.type,
      text,
      at: item.at,
      toolName: item.toolName,
    })
  }

  if (list.length > MAX_ACTIVITY_ITEMS) {
    return list.slice(list.length - MAX_ACTIVITY_ITEMS)
  }
  return list
}
