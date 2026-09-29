/**
 * 通用"侧栏可拖拽调宽"hook + 配套手柄组件。
 *
 * - localStorage 持久化宽度
 * - min/max 夹紧
 * - 拖拽期间给 body 加 `lr-resizing` 类锁全局光标 + 禁选中
 * - 双击手柄重置回默认值
 *
 * 用法：
 *   const { width, handleProps } = useResizableWidth({
 *     storageKey: 'lr:my-sidebar', defaultWidth: 260, min: 200, max: 480,
 *   })
 *   <aside style={{ width }}>
 *     ...
 *     <ResizeHandle {...handleProps} />
 *   </aside>
 */
import { useCallback, useRef, useState } from 'react'

export interface UseResizableWidthOpts {
  storageKey: string
  defaultWidth: number
  min: number
  max: number
  /** 手柄挂在容器哪一侧；右侧（默认）：拖右 → 变宽；左侧：拖左 → 变宽 */
  side?: 'right' | 'left'
}

export interface ResizeHandleProps {
  active: boolean
  onPointerDown: (e: React.PointerEvent) => void
  onDoubleClick: () => void
  side: 'right' | 'left'
}

export function useResizableWidth(opts: UseResizableWidthOpts) {
  const { storageKey, defaultWidth, min, max, side = 'right' } = opts

  const [width, setWidth] = useState<number>(() => {
    try {
      const v = Number(localStorage.getItem(storageKey))
      if (Number.isFinite(v) && v >= min && v <= max) return v
    } catch { /* ignore */ }
    return defaultWidth
  })
  const [resizing, setResizing] = useState(false)
  const widthRef = useRef(width)
  widthRef.current = width

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = widthRef.current
    setResizing(true)
    document.body.classList.add('lr-resizing')

    const dir = side === 'right' ? 1 : -1
    const onMove = (ev: PointerEvent) => {
      const next = Math.max(min, Math.min(max, startW + (ev.clientX - startX) * dir))
      setWidth(next)
    }
    const onUp = () => {
      setResizing(false)
      document.body.classList.remove('lr-resizing')
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
      try { localStorage.setItem(storageKey, String(Math.round(widthRef.current))) } catch { /* ignore */ }
    }
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp)
  }, [min, max, side, storageKey])

  const onDoubleClick = useCallback(() => {
    setWidth(defaultWidth)
    try { localStorage.removeItem(storageKey) } catch { /* ignore */ }
  }, [defaultWidth, storageKey])

  const handleProps: ResizeHandleProps = {
    active: resizing,
    onPointerDown,
    onDoubleClick,
    side,
  }

  return { width, setWidth, resizing, handleProps }
}

/** 4px 拖拽条；样式见 index.css 的 .lr-resize-handle */
export function ResizeHandle({ active, onPointerDown, onDoubleClick, side }: ResizeHandleProps) {
  return (
    <div
      className={`lr-resize-handle lr-resize-handle--${side}${active ? ' active' : ''}`}
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整宽度"
      onPointerDown={onPointerDown}
      onDoubleClick={onDoubleClick}
      title="拖动调整宽度（双击重置）"
    />
  )
}
