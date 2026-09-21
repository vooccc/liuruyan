import type { Message as MessageType, Session } from '../api/chat'
import { parseStoredRuntimeMessageContent } from './runtimeMessageContent'

function buildMessageSignature(message: Pick<MessageType, 'role' | 'content' | 'segments'>) {
  // 对 tool_call/tool_result：只用 type + tool_name。不用 tool_use_id 因为
  // frontend 流式 cache 的 tool_use_id 来自 stream event，可能 fallback 成 `tcu_${idx}`，
  // 跟 backend 持久化（常为空或别的 id）天然不一致——用它做签名等于自找漂移。
  // 同 message 内多个同名 tool_call 仍能区分（segments 顺序保留拼到 segmentKinds 里）。
  // 对其他 segment：仍用 type:kind:content（短内容稳定）。
  const segmentKinds = (message.segments || [])
    .map((segment) => {
      if (segment.type === 'tool_call' || segment.type === 'tool_result') {
        return `${segment.type}:${segment.tool_name || segment.name || ''}`
      }
      return `${segment.type}:${segment.kind || ''}:${segment.content || ''}`
    })
    .join('|')
  return `${message.role}::${message.content || ''}::${segmentKinds}`
}

function extractTextFromSegments(segments?: MessageType['segments']) {
  return (segments || [])
    .filter((segment) => segment.type === 'text')
    .map((segment) => segment.content || '')
    .join('')
}

function mergeRuntimeOnlySegments(
  normalizedSegments?: MessageType['segments'],
  reusedSegments?: MessageType['segments'],
) {
  const reusedThinkingSegments = (reusedSegments || []).filter((segment) => segment.type === 'thinking')
  let thinkingMatchIndex = 0
  const mergedSegments = normalizedSegments?.map((segment) => {
    if (segment.type !== 'thinking') return segment
    const reusedThinking = reusedThinkingSegments[thinkingMatchIndex++]
    if (!reusedThinking) return segment
    return {
      ...segment,
      firstSeenAt: segment.firstSeenAt ?? reusedThinking.firstSeenAt,
      completedAt: segment.completedAt ?? reusedThinking.completedAt,
    }
  }) ?? normalizedSegments

  // 纯运行期 segment：subagent_started —— backend 不持久化，需要从前一帧 reused 续回来。
  // （注：command_chunk 历史上也在这里，但 2026-05-15 起 execute_command 卡片改由
  //   tool_result 持久化驱动 —— tool_call 渲染时合成静态 chunks，不再依赖 SSE chunks
  //   在 normalize 后续命。所以这里不再处理 command_chunk。）
  const runtimeOnly = (reusedSegments || []).filter((segment) => (
    segment.type === 'system_notice' && segment.kind === 'subagent_started'
  ))
  if (runtimeOnly.length === 0) return mergedSegments

  const existingIds = new Set(
    (mergedSegments || [])
      .filter((segment) => segment.type === 'system_notice' && segment.kind === 'subagent_started')
      .map((segment) => String(segment.payload?.subagent_id || '')),
  )
  const missing = runtimeOnly.filter((segment) => {
    const subagentId = String(segment.payload?.subagent_id || '')
    return subagentId && !existingIds.has(subagentId)
  })
  if (missing.length === 0) return mergedSegments
  return [...missing, ...(mergedSegments || [])]
}

function buildStableMessageId(message: Pick<MessageType, 'role' | 'content' | 'timestamp'>, index: number) {
  const seed = `${message.role}|${message.timestamp || ''}|${message.content || ''}|${index}`
  let hash = 0
  for (let i = 0; i < seed.length; i += 1) {
    hash = ((hash << 5) - hash + seed.charCodeAt(i)) | 0
  }
  return Math.abs(hash || index + 1) + 1
}

export function buildSessionMessages(session: Session, previousMessages: MessageType[] = []): MessageType[] {
  const hasValidMessageSeq = (message: Pick<MessageType, 'message_seq'>) => (
    Number.isInteger(message.message_seq) && Number(message.message_seq) > 0
  )
  const serverMessages = session.messages || []
  const orderedServerMessages = serverMessages.length > 0 && serverMessages.every(hasValidMessageSeq)
    ? [...serverMessages].sort((left, right) => Number(left.message_seq) - Number(right.message_seq))
    : serverMessages

  // 三级匹配防线：
  // 1) client_id 端到端精确匹配（治根，新消息走这条）
  // 2) buildMessageSignature signature 匹配（兼容老 session 没 client_id 的）
  // 3) tail 阶段 content 近似兜底（segments 字段漂移时的最后一层）
  //
  // 跟踪每条 previousMessage 是否已被 backend 数据匹配复用（按引用）。
  // 未消费的可能是 in-flight 或 ephemeral，按规则接尾或丢弃。
  const consumed = new Set<MessageType>()

  const messageSeqBuckets = previousMessages.reduce((acc, message) => {
    if (message.ephemeral || !hasValidMessageSeq(message)) return acc
    const messageSeq = Number(message.message_seq)
    if (!acc[messageSeq]) acc[messageSeq] = []
    acc[messageSeq].push(message)
    return acc
  }, {} as Record<number, MessageType[]>)

  // 第 1 层：client_id 桶。key = `${role}::${client_id}`。仅含 client_id 非空的 cache。
  const clientIdBuckets = previousMessages.reduce((acc, message) => {
    if (message.ephemeral || hasValidMessageSeq(message)) return acc
    if (!message.client_id) return acc
    const key = `${message.role}::${message.client_id}`
    if (!acc[key]) acc[key] = []
    acc[key].push(message)
    return acc
  }, {} as Record<string, MessageType[]>)

  // 第 2 层：signature 桶。所有 non-ephemeral cache 都进，第 1 层未命中时 fallback。
  const signatureBuckets = previousMessages.reduce((acc, message) => {
    if (message.ephemeral || hasValidMessageSeq(message)) return acc
    const key = buildMessageSignature(message)
    if (!acc[key]) acc[key] = []
    acc[key].push(message)
    return acc
  }, {} as Record<string, MessageType[]>)

  // cache → 对应的 persisted 输出实例。后面 in-place merge 时按 cache 顺序遍历，
  // 命中 consumed 就吐 persisted 版本，未命中就吐 cache 自己（保留原位）。
  const cacheToOutput = new Map<MessageType, MessageType>()

  const persistedMessages = orderedServerMessages.map((message, index) => {
    const apiSegments = message.role === 'assistant' && Array.isArray(message.segments) && message.segments.length > 0
      ? message.segments
      : undefined
    const parsedAssistantContent = message.role === 'assistant' && !apiSegments
      ? parseStoredRuntimeMessageContent(message.content || '')
      : null
    const segments = apiSegments || parsedAssistantContent?.segments
    const content = message.role === 'assistant'
      ? (
          extractTextFromSegments(segments)
          || parsedAssistantContent?.text
          || message.content
        )
      : message.content
    const normalizedMessage: MessageType = {
      id: index + 1,
      role: message.role,
      content,
      timestamp: message.timestamp,
      message_seq: message.message_seq,
      history_revision: message.history_revision,
      client_id: message.client_id,
      agent_id: message.agent_id,
      segments: message.role === 'user'
        ? [{ id: 0, type: 'text' as const, content: message.content }]
        : segments,
    }

    let reused: MessageType | undefined
    if (hasValidMessageSeq(message)) {
      reused = messageSeqBuckets[Number(message.message_seq)]?.shift()
    }
    // 第 1 层：client_id 精确匹配
    if (!reused && message.client_id) {
      const cidKey = `${message.role}::${message.client_id}`
      reused = clientIdBuckets[cidKey]?.shift()
    }
    // 第 2 层：signature 兼容老消息
    if (!reused) {
      const signature = buildMessageSignature(normalizedMessage)
      const candidate = signatureBuckets[signature]?.shift()
      if (candidate && !consumed.has(candidate)) {
        reused = candidate
      }
    }
    if (reused) consumed.add(reused)

    const output: MessageType = {
      ...normalizedMessage,
      id: reused?.id || buildStableMessageId(normalizedMessage, index),
      segments: mergeRuntimeOnlySegments(normalizedMessage.segments, reused?.segments),
    }
    if (reused) cacheToOutput.set(reused, output)
    return output
  })

  // 已持久化 message 的内容索引（按 role 分桶 trim content），用于"签名漂移兜底"。
  // 只在有未消费 non-ephemeral cache 时才构造，避免常态成本。
  const hasUnconsumedNonEphemeral = previousMessages.some(
    (message) => !message.ephemeral && !consumed.has(message),
  )
  const persistedContentByRole = hasUnconsumedNonEphemeral
    ? persistedMessages.reduce((acc, message) => {
        const trimmed = (message.content || '').trim()
        if (!trimmed) return acc
        if (!acc[message.role]) acc[message.role] = new Set()
        acc[message.role].add(trimmed)
        return acc
      }, {} as Record<string, Set<string>>)
    : null

  // 参考 cline MessageStateHandler（_research/cline-main/src/core/task/message-state.ts）
  // 设计：messages 数组是唯一时间线，partial-failed / 错误中断 / in-flight 的 cache
  // 消息一律按它在 cache 中的原位置保留，不甩末尾。这避免了"上一轮没落盘的错误消息"
  // reconcile 后跑到最新成功消息后面的视觉乱序——等价于 cline abortStream 把
  // partial=false 标在原 lastMessage 上、ts/位置不动的语义。
  //
  // 遍历策略（按 cache 原顺序走）：
  // - ephemeral 临时态（plan preview / panorama）→ 收集到末尾追加
  // - consumed cache → 吐 persisted 输出版本（已建立 cacheToOutput 映射）
  // - 未配对 cache：
  //     * 内容为空 → 丢弃（cline resumeTaskFromHistory 删空 api_req_started 占位
  //       同款语义，纯 placeholder 没必要保留）
  //     * 内容已在 persisted（签名漂移） → 丢弃，避免重复显示
  //     * 否则按 cache 原位保留（错误中断 / in-flight 部分文本）
  // 走完 cache 后未用过的 persisted（罕见：collab 注入 / backend 主动追加）按
  // backend 顺序追到末尾。ephemeral 永远位于队列最尾。
  const result: MessageType[] = []
  const usedPersisted = new Set<MessageType>()
  const ephemeralTail: MessageType[] = []

  // 休眠唤醒 / backend 重连后的首次发送竞态：session 历史 snapshot 尚未返回时，
  // cache 可能只含刚发出的新轮次。snapshot 随后返回后，这些新消息能靠 client_id
  // 匹配到 persisted 尾部，但 persisted 历史前缀在 cache 中完全不存在。若仍先遍历
  // cache 再追加 unused persisted，就会把“今天”放在“昨天”上面。
  // 这里只补 earliest matched persisted 之前的 server 前缀；不按 timestamp 全局排序，
  // 也不改变未配对 partial / failed cache 在原时间线中的保序契约。
  const matchedPersistedIndexes = previousMessages
    .filter((message) => !message.ephemeral && consumed.has(message))
    .map((message) => {
      const output = cacheToOutput.get(message)
      return output ? persistedMessages.indexOf(output) : -1
    })
    .filter((index) => index >= 0)
  if (matchedPersistedIndexes.length > 0) {
    const earliestMatchedIndex = Math.min(...matchedPersistedIndexes)
    for (const persistedPrefix of persistedMessages.slice(0, earliestMatchedIndex)) {
      result.push(persistedPrefix)
      usedPersisted.add(persistedPrefix)
    }
  }

  for (const cacheMessage of previousMessages) {
    if (cacheMessage.ephemeral) {
      ephemeralTail.push(cacheMessage)
      continue
    }
    if (consumed.has(cacheMessage)) {
      const persistedOutput = cacheToOutput.get(cacheMessage)
      if (persistedOutput && !usedPersisted.has(persistedOutput)) {
        result.push(persistedOutput)
        usedPersisted.add(persistedOutput)
      }
      continue
    }
    if (hasValidMessageSeq(cacheMessage)) continue
    const trimmed = (cacheMessage.content || '').trim()
    if (!trimmed && !cacheMessage.isStreaming) continue
    if (persistedContentByRole?.[cacheMessage.role]?.has(trimmed)) {
      // 签名漂移 dedup：cache 没匹配上但 backend 已有同 role 同内容的 message
      // （segments 字段细差导致 client_id/signature 匹配漏掉）。把对应的 persisted
      // 在 cache 这个位置吐出来——既消重又保留视觉顺序，等价于"第 3 层 content 匹配"。
      // 不能 continue，否则 persisted 那条会落到尾部 unused fallback 里去，跨过 cache
      // 后续位置导致顺序错乱（旧实现走 [...persisted, ...tail] 是 persisted 整体在前，
      // 漂移这条天然在它该在的位置；现在按 cache 顺序走，必须显式把它领过来）。
      const matchingPersisted = persistedMessages.find(
        (p) => (
          !usedPersisted.has(p)
          && p.role === cacheMessage.role
          && (p.content || '').trim() === trimmed
        ),
      )
      if (matchingPersisted) {
        const mergedPersisted = {
          ...matchingPersisted,
          segments: mergeRuntimeOnlySegments(matchingPersisted.segments, cacheMessage.segments),
        }
        result.push(mergedPersisted)
        usedPersisted.add(matchingPersisted)
      }
      continue
    }
    result.push(cacheMessage)
  }

  for (const persistedOutput of persistedMessages) {
    if (!usedPersisted.has(persistedOutput)) {
      result.push(persistedOutput)
    }
  }

  result.push(...ephemeralTail)
  return result
}
