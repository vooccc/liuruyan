/**
 * 增强文本块 - 支持打字机效果的文本渲染
 *
 * 职责：
 * 1. 检测最终结果回流（worker_result）
 * 2. 应用打字机效果
 * 3. 完成后触发仪式感动画
 */

import { useState, useEffect } from 'react'
import { TypewriterText } from './TypewriterText'
import { animationSystem } from '../lib/animationSystem'

interface EnhancedTextBlockProps {
  content: string
  isWorkerResult?: boolean
  isFinalMerge?: boolean
  enableTypewriter?: boolean
  className?: string
  renderMarkdown?: (text: string) => string
  onComplete?: () => void
}

export function EnhancedTextBlock({
  content,
  isWorkerResult = false,
  isFinalMerge = false,
  enableTypewriter = true,
  className = '',
  renderMarkdown,
  onComplete,
}: EnhancedTextBlockProps) {
  const [showTypewriter, setShowTypewriter] = useState(false)
  const [isComplete, setIsComplete] = useState(false)

  useEffect(() => {
    // 只对最终结果回流启用打字机效果
    if ((isWorkerResult || isFinalMerge) && enableTypewriter && content.trim()) {
      setShowTypewriter(true)
    }
  }, [isWorkerResult, isFinalMerge, enableTypewriter, content])

  const handleTypewriterComplete = async () => {
    setIsComplete(true)
    onComplete?.()

    // 完成仪式感动画
    await animationSystem.animate({
      elementId: 'typewriter-complete-indicator',
      config: {
        type: 'pulse',
        duration: 600,
        iterations: 2,
      },
    })
  }

  if (!content.trim()) {
    return null
  }

  // 打字机模式
  if (showTypewriter && !isComplete) {
    return (
      <div className={`enhanced-text-block ${className}`}>
        {renderMarkdown ? (
          <div
            className="message-content typewriter-content"
            dangerouslySetInnerHTML={{
              __html: renderMarkdown(content),
            }}
          />
        ) : (
          <div className="message-content">
            <TypewriterText
              text={content}
              speed={Math.max(12, Math.min(45, Math.round(1800 / Math.max(content.length, 1))))}
              onComplete={handleTypewriterComplete}
            />
          </div>
        )}
        {!isComplete && (
          <span
            id="typewriter-complete-indicator"
            className="enhanced-text-progress-dot"
            aria-hidden="true"
          />
        )}
      </div>
    )
  }

  // 标准模式
  return (
    <div className={`enhanced-text-block ${isComplete ? 'is-complete' : ''} ${className}`}>
      {renderMarkdown ? (
        <div
          className="message-content"
          dangerouslySetInnerHTML={{
            __html: renderMarkdown(content),
          }}
        />
      ) : (
        <div className="message-content">{content}</div>
      )}
    </div>
  )
}
