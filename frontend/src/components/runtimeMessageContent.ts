import type { ContentSegment } from '../api/chat'

function extractTextSegments(segments: ContentSegment[]) {
  return segments
    .filter((segment) => segment.type === 'text')
    .map((segment) => segment.content || '')
    .join('')
}

function stripLegacyThinkingPrefix(content: string) {
  if (!content.includes('思考过程')) return content
  return content.replace(/^##\s*思考过程\s*\n[\s\S]*?\n---\s*\n*/g, '')
}

function parseNestedPayload(content: string): unknown {
  let parsed: unknown = JSON.parse(content)
  let depth = 0

  while (
    depth < 3 &&
    parsed &&
    typeof parsed === 'object' &&
    !Array.isArray(parsed) &&
    typeof (parsed as { content?: unknown }).content === 'string' &&
    ((parsed as { content: string }).content || '').trim().startsWith('{')
  ) {
    try {
      parsed = JSON.parse((parsed as { content: string }).content)
      depth += 1
    } catch {
      break
    }
  }

  return parsed
}

export function parseStoredRuntimeMessageContent(content: string) {
  if (!content) {
    return {
      text: '' as string,
      segments: undefined as ContentSegment[] | undefined,
      agentId: undefined as string | undefined,
    }
  }

  try {
    const parsed = parseNestedPayload(content)

    if (typeof parsed === 'string') {
      return { text: stripLegacyThinkingPrefix(parsed), segments: undefined, agentId: undefined }
    }

    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const parsedSegments = Array.isArray((parsed as { segments?: unknown[] }).segments)
        ? (parsed as { segments: ContentSegment[] }).segments
        : undefined

      if (parsedSegments && parsedSegments.length > 0) {
        const extractedText = extractTextSegments(parsedSegments)
        const fallbackText = typeof (parsed as { content?: unknown }).content === 'string'
          ? (parsed as { content: string }).content
          : ''
        const agentId = parsedSegments.find((segment) => segment.agent_id)?.agent_id

        return {
          text: stripLegacyThinkingPrefix(extractedText.trim() ? extractedText : fallbackText),
          segments: parsedSegments,
          agentId,
        }
      }

      if (typeof (parsed as { content?: unknown }).content === 'string') {
        return {
          text: stripLegacyThinkingPrefix((parsed as { content: string }).content),
          segments: undefined,
          agentId: undefined,
        }
      }
    }
  } catch {
    // 防御 ReDoS：仅短文本（<= 4000 字符）且包含 "content" 时做安全尝试，长文本绝不跑回溯正则
    if (content.length <= 4000 && content.includes('"content"')) {
      const looseContentMatch = content.match(/"content"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/)
      if (looseContentMatch?.[1]) {
        try {
          return {
            text: stripLegacyThinkingPrefix(JSON.parse(`"${looseContentMatch[1]}"`)),
            segments: undefined,
            agentId: undefined,
          }
        } catch {
          return {
            text: stripLegacyThinkingPrefix(looseContentMatch[1]),
            segments: undefined,
            agentId: undefined,
          }
        }
      }
    }
  }

  return { text: stripLegacyThinkingPrefix(content), segments: undefined, agentId: undefined }
}
