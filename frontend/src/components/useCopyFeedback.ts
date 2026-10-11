import { useCallback, useState } from 'react'

interface UseCopyFeedbackOptions {
  resetMs?: number
}

export function useCopyFeedback<T extends string | number>({
  resetMs = 1500,
}: UseCopyFeedbackOptions = {}) {
  const [copiedId, setCopiedId] = useState<T | null>(null)

  const copyContent = useCallback(async (id: T, content: string) => {
    if (!content.trim()) return

    try {
      await navigator.clipboard.writeText(content)
      setCopiedId(id)
      window.setTimeout(() => {
        setCopiedId((current) => (current === id ? null : current))
      }, resetMs)
    } catch {}
  }, [resetMs])

  return {
    copiedId,
    copyContent,
  }
}
