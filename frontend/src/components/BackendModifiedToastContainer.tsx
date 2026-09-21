/**
 * Backend 写改 toast 容器（自进化护网 D）
 *
 * 监听 store.backendToasts，右下角浮动卡片栈：
 *   - 自动 5 秒消失
 *   - 鼠标 hover 暂停消失
 *   - 点击复制备份路径到剪贴板（让用户能手动 restore）
 *
 * 全局组件，挂在 Chat.tsx 顶层（与所有 session 共享一个 toast 区）。
 */

import { useEffect, useRef, useState } from 'react'
import { FileText, X } from 'lucide-react'
import { useAppStore, type BackendToast } from '../stores/useAppStore'

const TOAST_TTL_MS = 6000

interface ToastCardProps {
  toast: BackendToast
  onDismiss: (id: string) => void
}

function ToastCard({ toast, onDismiss }: ToastCardProps) {
  const [hovered, setHovered] = useState(false)
  const remainingRef = useRef<number>(TOAST_TTL_MS)
  const startedAtRef = useRef<number>(Date.now())
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (hovered) {
      // 暂停：记录已用时间
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
        const elapsed = Date.now() - startedAtRef.current
        remainingRef.current = Math.max(0, remainingRef.current - elapsed)
      }
      return
    }
    // 启动剩余倒计时
    startedAtRef.current = Date.now()
    timerRef.current = setTimeout(() => {
      onDismiss(toast.id)
    }, remainingRef.current)
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
  }, [hovered, toast.id, onDismiss])

  const lineDiff = toast.linesAfter - toast.linesBefore
  const sign = lineDiff > 0 ? '+' : ''

  const handleClick = async () => {
    if (toast.backupPath) {
      try {
        await navigator.clipboard.writeText(toast.backupPath)
      } catch {
        // ignore — 退化为静默不报错
      }
    }
  }

  return (
    <div
      className="backend-toast-card"
      style={{
        background: 'var(--bg-secondary, #1e1e1e)',
        border: '1px solid var(--border-color, #2a2a2a)',
        borderLeft: '3px solid #e0a040',
        borderRadius: 6,
        padding: '8px 10px 8px 12px',
        marginTop: 8,
        minWidth: 280,
        maxWidth: 360,
        boxShadow: '0 4px 12px var(--shadow-sm, rgba(0,0,0,0.3))',
        cursor: toast.backupPath ? 'pointer' : 'default',
        fontSize: 12,
        color: 'var(--text-color)',
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={handleClick}
      title={toast.backupPath ? `点击复制备份路径：${toast.backupPath}` : toast.path}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
        <FileText size={13} style={{ color: '#e0a040' }} />
        <span style={{ fontWeight: 600, flex: 1 }}>
          {toast.created ? '新建' : '修改'}: {toast.filename}
        </span>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onDismiss(toast.id) }}
          style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', padding: 0, opacity: 0.6 }}
          title="关闭"
        >
          <X size={12} />
        </button>
      </div>
      <div style={{ opacity: 0.75, fontSize: 11, marginBottom: 2 }}>
        {toast.toolName}
        {toast.agentId ? ` · ${toast.agentId}` : ''}
        {' · '}
        {toast.charsBefore} → {toast.charsAfter} 字
        {' · '}
        {toast.linesBefore} → {toast.linesAfter} 行 ({sign}{lineDiff})
      </div>
      <div style={{ opacity: 0.5, fontSize: 10, wordBreak: 'break-all' }}>{toast.path}</div>
      {toast.backupPath && (
        <div style={{ opacity: 0.5, fontSize: 10, marginTop: 2 }}>
          备份: {toast.backupPath.split(/[\\/]/).slice(-2).join('/')} （点击复制完整路径）
        </div>
      )}
    </div>
  )
}

export default function BackendModifiedToastContainer() {
  const toasts = useAppStore((s) => s.backendToasts)
  const dismiss = useAppStore((s) => s.dismissBackendToast)

  if (toasts.length === 0) return null

  return (
    <div
      style={{
        position: 'fixed',
        right: 16,
        top: 64,
        zIndex: 9999,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-end',
        pointerEvents: 'none',
      }}
    >
      {toasts.map((t) => (
        <div key={t.id} style={{ pointerEvents: 'auto' }}>
          <ToastCard toast={t} onDismiss={dismiss} />
        </div>
      ))}
    </div>
  )
}
