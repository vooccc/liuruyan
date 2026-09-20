/**
 * 打字机文本组件 - 逐字显示文本
 *
 * 职责：
 * 1. 逐字符显示文本
 * 2. 可配置速度
 * 3. 完成回调
 */

import { useState, useEffect } from 'react'

interface TypewriterTextProps {
  text: string
  speed?: number // 字符/秒
  onComplete?: () => void
  className?: string
  style?: React.CSSProperties
}

export function TypewriterText({
  text,
  speed = 60,
  onComplete,
  className,
  style,
}: TypewriterTextProps) {
  const [displayedText, setDisplayedText] = useState('')
  const [isComplete, setIsComplete] = useState(false)

  useEffect(() => {
    if (isComplete) return

    let currentIndex = 0
    const intervalMs = 1000 / speed

    const timer = setInterval(() => {
      if (currentIndex < text.length) {
        setDisplayedText(text.slice(0, currentIndex + 1))
        currentIndex++
      } else {
        setIsComplete(true)
        clearInterval(timer)
        onComplete?.()
      }
    }, intervalMs)

    return () => clearInterval(timer)
  }, [text, speed, isComplete, onComplete])

  return (
    <span className={`typewriter-text ${className || ''}`} style={style}>
      {displayedText}
      {!isComplete && (
        <span
          style={{
            display: 'inline-block',
            width: '2px',
            height: '1em',
            background: 'currentColor',
            marginLeft: '2px',
            animation: 'blink 1s infinite',
            verticalAlign: 'baseline',
          }}
        />
      )}
    </span>
  )
}
