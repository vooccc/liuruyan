import { useState, useEffect, useRef } from 'react'
import { AlertTriangle, Trash2, Info } from 'lucide-react'

/** 对话框类型 */
export type ConfirmType = 'danger' | 'warning' | 'info'

interface ConfirmDialogProps {
  open: boolean
  title: string
  message: string
  type?: ConfirmType
  confirmText?: string
  cancelText?: string
  onConfirm: () => void
  onCancel: () => void
}

/** 自定义确认对话框 */
export function ConfirmDialog({
  open, title, message, type = 'danger',
  confirmText = '确认', cancelText = '取消',
  onConfirm, onCancel
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', handler)
    dialogRef.current?.focus()
    return () => document.removeEventListener('keydown', handler)
  }, [open, onCancel])

  if (!open) return null

  const iconMap = {
    danger: <Trash2 size={20} className="confirm-icon danger" />,
    warning: <AlertTriangle size={20} className="confirm-icon warning" />,
    info: <Info size={20} className="confirm-icon info" />,
  }

  return (
    <div className="confirm-overlay" onClick={onCancel}>
      <div className="confirm-dialog" ref={dialogRef} tabIndex={-1} onClick={e => e.stopPropagation()}>
        <div className="confirm-header">
          {iconMap[type]}
          <span className="confirm-title">{title}</span>
        </div>
        <p className="confirm-message">{message}</p>
        <div className="confirm-actions">
          <button className="confirm-btn cancel" onClick={onCancel}>{cancelText}</button>
          <button className={`confirm-btn ${type}`} onClick={onConfirm}>{confirmText}</button>
        </div>
      </div>
    </div>
  )
}

/** 自定义 Alert 对话框 */
interface AlertDialogProps {
  open: boolean
  message: string
  onClose: () => void
}

export function AlertDialog({ open, message, onClose }: AlertDialogProps) {
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' || e.key === 'Enter') onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="confirm-overlay" onClick={onClose}>
      <div className="confirm-dialog" onClick={e => e.stopPropagation()}>
        <div className="confirm-header">
          <Info size={20} className="confirm-icon info" />
          <span className="confirm-title">提示</span>
        </div>
        <p className="confirm-message">{message}</p>
        <div className="confirm-actions">
          <button className="confirm-btn info" onClick={onClose}>知道了</button>
        </div>
      </div>
    </div>
  )
}

/** Hook: 用 Promise 封装确认对话框 */
export function useConfirmDialog() {
  const [state, setState] = useState<{
    open: boolean
    title: string
    message: string
    type: ConfirmType
    confirmText: string
    resolver: ((value: boolean) => void) | null
  }>({
    open: false, title: '', message: '', type: 'danger', confirmText: '确认', resolver: null
  })

  const confirm = (title: string, message: string, type: ConfirmType = 'danger', confirmText = '确认') => {
    return new Promise<boolean>(resolve => {
      setState({ open: true, title, message, type, confirmText, resolver: resolve })
    })
  }

  const handleConfirm = () => {
    state.resolver?.(true)
    setState(prev => ({ ...prev, open: false, resolver: null }))
  }

  const handleCancel = () => {
    state.resolver?.(false)
    setState(prev => ({ ...prev, open: false, resolver: null }))
  }

  const dialog = (
    <ConfirmDialog
      open={state.open}
      title={state.title}
      message={state.message}
      type={state.type}
      confirmText={state.confirmText}
      onConfirm={handleConfirm}
      onCancel={handleCancel}
    />
  )

  return { confirm, dialog }
}

/** Hook: 用 Promise 封装 Alert 对话框 */
export function useAlertDialog() {
  const [state, setState] = useState<{
    open: boolean
    message: string
    resolver: ((value: void) => void) | null
  }>({ open: false, message: '', resolver: null })

  const alert = (message: string) => {
    return new Promise<void>(resolve => {
      setState({ open: true, message, resolver: resolve })
    })
  }

  const handleClose = () => {
    state.resolver?.()
    setState(prev => ({ ...prev, open: false, resolver: null }))
  }

  const dialog = (
    <AlertDialog open={state.open} message={state.message} onClose={handleClose} />
  )

  return { alert, dialog }
}
