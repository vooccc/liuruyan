import type { ContentSegment, Message as MessageType, ToolCallItem } from '../api/chat'
import { formatChatStreamErrorMessage } from './chatUiText'
import type { ChatStreamLikeEvent } from './chatExternalHandle'


export function extractTextFromSegments(segments?: ContentSegment[]) {
  return (segments || [])
    .filter((segment) => segment.type === 'text')
    .map((segment) => segment.content || '')
    .join('')
}

export function ensureMessageSegments(message: MessageType) {
  if (message.segments) return [...message.segments]

  const segments: ContentSegment[] = []

  if (message.thinkingContent) {
    segments.push({ id: segments.length, type: 'thinking', content: message.thinkingContent })
  }

  if (message.toolCalls) {
    for (const toolCall of message.toolCalls) {
      segments.push({ id: segments.length, type: 'tool_call', toolCall })
    }
  }

  if (message.content || segments.length === 0) {
    segments.push({ id: segments.length, type: 'text', content: message.content || '' })
  }

  return segments
}

function normalizeContentChunk(content: string) {
  let textChunk = content || ''

  if (textChunk.length > 1 && textChunk.trimStart().startsWith('{')) {
    try {
      const parsed = JSON.parse(textChunk)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        if (parsed.segments && Array.isArray(parsed.segments)) {
          const texts = parsed.segments
            .filter((segment: any) => segment && segment.type === 'text' && segment.content)
            .map((segment: any) => segment.content)
          if (texts.length > 0) textChunk = texts.join('')
          else if (typeof parsed.content === 'string') textChunk = parsed.content
        } else if (typeof parsed.content === 'string') {
          textChunk = parsed.content
        }
      }
    } catch {
      // keep raw chunk
    }
  }

  return textChunk
}

function stripToolCallEchoFromTextSegments(segments: ContentSegment[], toolName: string) {
  const escapedToolName = toolName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

  for (let index = segments.length - 1; index >= 0; index -= 1) {
    if (segments[index].type !== 'text' || !segments[index].content) continue

    const fullText = segments[index].content!
    const namePattern = new RegExp(`(?:^|\\n)${escapedToolName}\\s*\\n\\s*\\{`, 'g')
    let cleaned = fullText
    let match: RegExpExecArray | null

    while ((match = namePattern.exec(fullText)) !== null) {
      const braceStart = match.index + match[0].length - 1
      let depth = 0
      let end = -1

      for (let cursor = braceStart; cursor < fullText.length; cursor += 1) {
        if (fullText[cursor] === '{') depth += 1
        else if (fullText[cursor] === '}') depth -= 1
        if (depth === 0) {
          end = cursor
          break
        }
      }

      if (end >= 0) {
        let blockStart = match.index
        if (match.index > 0 && fullText[match.index - 1] === '\n') blockStart = match.index - 1
        cleaned = cleaned.substring(0, blockStart) + cleaned.substring(end + 1)
      }
    }

    segments[index] = { ...segments[index], content: cleaned }
    break
  }
}

export function finalizeLastThinkingSegment(message: MessageType, completedAt: number = Date.now()): MessageType {
  const segments = message.segments
  if (!segments || segments.length === 0) return message
  const lastSegment = segments[segments.length - 1]
  if (lastSegment?.type !== 'thinking') return message
  if (typeof lastSegment.firstSeenAt !== 'number') return message
  if (typeof lastSegment.completedAt === 'number') return message
  const nextSegments = [...segments]
  nextSegments[nextSegments.length - 1] = { ...lastSegment, completedAt }
  return {
    ...message,
    segments: nextSegments,
    content: extractTextFromSegments(nextSegments),
  }
}

export function applyStreamEventToMessage(message: MessageType, event: ChatStreamLikeEvent): MessageType {
  const updated: MessageType = { ...message }
  const segments = ensureMessageSegments(updated)
  const agentId = event.agent_id

  // 过滤掉带 runtime task refs 的内部旁路事件，避免混入主消息文本。
  if (event.refs?.worker_id) {
    return updated
  }

  if (event.type === 'content') {
    const textChunk = normalizeContentChunk(event.content || '')
    // 空 chunk noop：backend 在 thinking→text 转折等场景会推空 content event，
    // 若 lastSegment 非 text 会落到 else 分支 push 出空 text 段。空段不会被
    // backend 持久化，留在 cache 里会让 buildMessageSignature 漂移，触发
    // in-flight assistant 累积（"发 N 条回 N 条"）。
    if (!textChunk) return updated
    const lastSegment = segments[segments.length - 1]
    const now = Date.now()

    if (lastSegment && lastSegment.type === 'thinking' && typeof lastSegment.firstSeenAt === 'number' && typeof lastSegment.completedAt !== 'number') {
      segments[segments.length - 1] = { ...lastSegment, completedAt: now }
    }

    if (lastSegment && lastSegment.type === 'text' && !lastSegment.content?.endsWith('\n```')) {
      segments[segments.length - 1] = {
        ...lastSegment,
        content: (lastSegment.content || '') + textChunk,
        agent_id: lastSegment.agent_id || agentId,
      }
    } else {
      segments.push({ id: segments.length, type: 'text', content: textChunk, agent_id: agentId })
    }
  } else if (event.type === 'thinking') {
    const thinkingChunk = event.content || ''
    // 空 chunk noop：理由同 content 分支，避免空 thinking 段污染 cache。
    if (!thinkingChunk) return updated
    const lastSegment = segments[segments.length - 1]
    const now = Date.now()

    if (lastSegment && lastSegment.type === 'thinking') {
      segments[segments.length - 1] = {
        ...lastSegment,
        content: (lastSegment.content || '') + thinkingChunk,
        agent_id: lastSegment.agent_id || agentId,
        firstSeenAt: lastSegment.firstSeenAt ?? now,
      }
    } else {
      segments.push({ id: segments.length, type: 'thinking', content: thinkingChunk, agent_id: agentId, firstSeenAt: now })
    }

    updated.thinkingContent = (updated.thinkingContent || '') + thinkingChunk
  } else if (event.type === 'tool_call_started') {
    // P1 体感优化（2026-05-16）：解析层 tool name 一就位就发本事件，args 还在累积。
    // 立刻 push 一张 calling 占位卡（input 为空对象），让 UI 在模型还在流 args 的
    // 1-3 秒里就有视觉反馈——不必等 args 流完才"flash"出现。后续完整 `tool_call`
    // 事件到达时按 tool_use_id 配对、就地覆盖 input/name（见下方 tool_call 分支）。
    const toolName = event.name || 'execute_command'
    stripToolCallEchoFromTextSegments(segments, toolName)
    const lastSegment = segments[segments.length - 1]
    if (lastSegment && lastSegment.type === 'thinking' && typeof lastSegment.firstSeenAt === 'number' && typeof lastSegment.completedAt !== 'number') {
      segments[segments.length - 1] = { ...lastSegment, completedAt: Date.now() }
    }
    const toolUseId = event.id || event.tool_use_id || `tcu_${segments.length}`

    // 防御：如果同 tool_use_id 的 segment 已存在（极罕见——重复事件），noop。
    const existed = segments.some((s) => s.type === 'tool_call' && s.tool_use_id === toolUseId)
    if (existed) return updated

    const toolCallItem: ToolCallItem = { name: event.name || '', input: {}, status: 'calling' }
    segments.push({
      id: segments.length,
      type: 'tool_call',
      agent_id: agentId,
      tool_name: event.name || '',
      arguments: {},
      input: {},
      tool_use_id: toolUseId,
      toolCall: toolCallItem,
      firstSeenAt: Date.now(),
    })
    const calls = [...(updated.toolCalls || [])]
    calls.push(toolCallItem)
    updated.toolCalls = calls
  } else if (event.type === 'tool_call') {
    const toolName = event.name || 'execute_command'
    stripToolCallEchoFromTextSegments(segments, toolName)
    const lastSegment = segments[segments.length - 1]
    if (lastSegment && lastSegment.type === 'thinking' && typeof lastSegment.firstSeenAt === 'number' && typeof lastSegment.completedAt !== 'number') {
      segments[segments.length - 1] = { ...lastSegment, completedAt: Date.now() }
    }

    const toolUseId = event.id || event.tool_use_id || `tcu_${segments.length}`

    // P1：如果之前已收到 tool_call_started 创建过 segment（同 tool_use_id），
    // 就地覆盖 input/name（不重置 firstSeenAt，让 calling 锁继续按 started 时机走）。
    const existedIndex = segments.findIndex(
      (s) => s.type === 'tool_call' && s.tool_use_id === toolUseId,
    )
    if (existedIndex >= 0) {
      const existed = segments[existedIndex]
      const updatedToolCall: ToolCallItem = {
        ...(existed.toolCall || { name: '', status: 'calling' }),
        name: event.name || existed.toolCall?.name || '',
        input: event.input,
      }
      segments[existedIndex] = {
        ...existed,
        tool_name: event.name || existed.tool_name || '',
        arguments: event.input || {},
        input: event.input || {},
        toolCall: updatedToolCall,
      }
      // toolCalls 数组同步：找到引用的旧 item 替换（按 name+引用都不可靠时跳过）
      const calls = [...(updated.toolCalls || [])]
      const callIdx = calls.findIndex((c) => c === existed.toolCall)
      if (callIdx >= 0) calls[callIdx] = updatedToolCall
      updated.toolCalls = calls
      return updated
    }

    // 兼容老后端 / 未发 started 的路径：直接 push 一张新卡（原行为）。
    const toolCallItem: ToolCallItem = { name: event.name || '', input: event.input, status: 'calling' }

    segments.push({
      id: segments.length,
      type: 'tool_call',
      agent_id: agentId,
      tool_name: event.name || '',
      arguments: event.input || {},
      input: event.input || {},
      tool_use_id: toolUseId,
      toolCall: toolCallItem,
      // 仅 live SSE 路径注入：runtimeBlocks 的 calling 最小展示时长锁靠它驱动。
      // 缓存/历史路径走 ensureMessageSegments 不经过本 reducer，不会设此字段 →
      // 历史消息回放时锁不触发，直接显示 done 终态。
      firstSeenAt: Date.now(),
    })

    const calls = [...(updated.toolCalls || [])]
    calls.push(toolCallItem)
    updated.toolCalls = calls
  } else if (event.type === 'tool_result') {
    segments.push({
      id: segments.length,
      type: 'tool_result',
      agent_id: agentId,
      // name 必须塞：backend 持久化的 tool_result 段带 name，cache 不带会让
      // buildMessageSignature 漂移，触发 in-flight 累积。
      name: event.name || '',
      tool_use_id: event.tool_use_id || '',
      content: typeof event.result === 'string' ? event.result : JSON.stringify(event.result),
      result: event.result,
    })

    // tool_call ↔ tool_result 配对（2026-05-03 修：原版倒序 + OR 太松，
    // 同一工具并发调用 N 次时会错配 → 真主人永远没 result → 永久"调用中"）。
    //
    // 策略：优先 tool_use_id 严格匹配；缺 id 时退化到 FIFO + name 匹配
    // （LLM 一般按调用顺序返回 result，FIFO 比 LIFO 更接近真相）。
    let matchedSegIndex = -1
    if (event.tool_use_id) {
      for (let i = 0; i < segments.length - 1; i += 1) {
        const s = segments[i]
        if (
          s.type === 'tool_call'
          && s.toolCall?.status === 'calling'
          && s.tool_use_id === event.tool_use_id
        ) {
          matchedSegIndex = i
          break
        }
      }
    }
    if (matchedSegIndex < 0) {
      for (let i = 0; i < segments.length - 1; i += 1) {
        const s = segments[i]
        if (s.type === 'tool_call' && s.toolCall?.status === 'calling') {
          if (!event.name || s.toolCall.name === event.name) {
            matchedSegIndex = i
            break
          }
        }
      }
    }
    if (matchedSegIndex >= 0) {
      segments[matchedSegIndex] = {
        ...segments[matchedSegIndex],
        toolCall: {
          ...segments[matchedSegIndex].toolCall!,
          result: event.result,
          status: 'done',
        },
      }
    }

    // updated.toolCalls 的并行配对（同样改 FIFO，保持一致）
    const calls = [...(updated.toolCalls || [])]
    for (let i = 0; i < calls.length; i += 1) {
      if (calls[i].status === 'calling' && (!event.name || calls[i].name === event.name)) {
        calls[i] = { ...calls[i], result: event.result, status: 'done' as const }
        break
      }
    }
    updated.toolCalls = calls
  } else if (event.type === 'system_notice') {
    segments.push({
      id: segments.length,
      type: 'system_notice',
      agent_id: agentId,
      kind: event.kind,
      content: event.content || '',
      payload: event.payload,
    })
  } else if (event.type === 'error') {
    segments.push({
      id: segments.length,
      type: 'text',
      content: formatChatStreamErrorMessage(event.error),
      agent_id: agentId,
    })
  }

  updated.segments = segments
  updated.content = extractTextFromSegments(segments)
  return updated
}
