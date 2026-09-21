import type { ContentSegment } from '../api/chat'

export function hasDetailedRuntimeChain(segments: ContentSegment[]) {
  return segments.some((segment) => segment.type !== 'text')
}

export function getRuntimeChainSummary(segments: ContentSegment[]) {
  const thinkingCount = segments.filter((segment) => segment.type === 'thinking').length
  const toolCount = segments.filter((segment) => segment.type === 'tool_call').length
  const noticeCount = segments.filter((segment) => segment.type === 'system_notice').length
  const parts: string[] = []

  if (thinkingCount > 0) parts.push(`${thinkingCount} 段思考`)
  if (toolCount > 0) parts.push(`${toolCount} 次工具`)
  if (noticeCount > 0) parts.push(`${noticeCount} 条通知`)

  return parts.join(' · ')
}
