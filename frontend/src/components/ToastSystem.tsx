import { useState, useEffect, useCallback, useRef, createContext, useContext, type ReactNode } from 'react'
import { X } from 'lucide-react'
import type { GameNotification } from '../api/chat'

// ========================================================================
// Toast 通知系统 — 全局弹幕式提示
// ========================================================================

interface ToastItem {
  id: string
  type: 'achievement' | 'affinity' | 'jealousy' | 'bond' | 'label' | 'info' | 'cron'
  title: string
  desc: string
  icon: string
  urgency: 'low' | 'medium' | 'high'
  color: string
  duration?: number  // ms，默认由 urgency 决定
  // type='cron' 专属：结构化展示角色名 / cron 表达式 / 状态
  cronDetails?: {
    agent_name: string
    agent_avatar_url?: string  // 真头像图片（来自 avatar_url + API_BASE 拼接）
    agent_avatar_emoji?: string  // 兜底 emoji
    cron: string                // 原表达式（如 "0 9 * * *"）
    cron_natural?: string       // 自然语言版（如 "每天 09:00"）；显示用
    mode_label: string  // "一次性"/"周期"
    once_completed: boolean
    next_text: string
  }
}

// 全局通知上下文
interface ToastContextValue {
  addToast: (toast: Omit<ToastItem, 'id'>) => void
  removeToast: (id: string) => void
}

const ToastContext = createContext<ToastContextValue>({
  addToast: () => {},
  removeToast: () => {},
})

export function useToast() {
  return useContext(ToastContext)
}

// 通知类型 → 颜色/图标映射
const TOAST_STYLES: Record<string, { color: string; icon: string }> = {
  achievement: { color: '#fbbf24', icon: '🏆' },
  affinity:    { color: '#ec4899', icon: '💗' },
  jealousy:    { color: '#f97316', icon: '😤' },
  bond:        { color: '#a78bfa', icon: '🔗' },
  label:       { color: '#60a5fa', icon: '🔄' },
  info:        { color: '#94a3b8', icon: '💬' },
  cron:        { color: '#60a5fa', icon: '⏰' },
}

// 紧急度 → 持续时间
const URGENCY_DURATION: Record<string, number> = {
  low: 3000,
  medium: 4500,
  high: 6000,
}

function normalizeUrgency(urgency?: string): ToastItem['urgency'] {
  if (urgency === 'low' || urgency === 'medium' || urgency === 'high') return urgency
  return 'medium'
}

// ---- 单条 Toast ----
function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: (id: string) => void }) {
  const [exiting, setExiting] = useState(false)
  const [progress, setProgress] = useState(100)
  const timerRef = useRef<number>(0)
  const startRef = useRef<number>(0)

  const duration = toast.duration || URGENCY_DURATION[toast.urgency] || 4000

  useEffect(() => {
    startRef.current = Date.now()
    timerRef.current = window.setTimeout(() => {
      setExiting(true)
      setTimeout(() => onDismiss(toast.id), 300)
    }, duration)

    // 进度条动画
    const interval = window.setInterval(() => {
      const elapsed = Date.now() - startRef.current
      setProgress(Math.max(0, 100 - (elapsed / duration) * 100))
    }, 50)

    return () => {
      clearTimeout(timerRef.current)
      clearInterval(interval)
    }
  }, [duration, toast.id, onDismiss])

  const handleDismiss = () => {
    clearTimeout(timerRef.current)
    setExiting(true)
    setTimeout(() => onDismiss(toast.id), 300)
  }

  const style = TOAST_STYLES[toast.type] || TOAST_STYLES.info

  const isCron = toast.type === 'cron' && toast.cronDetails

  // ============ Cron 专属高级版布局 ============
  if (isCron && toast.cronDetails) {
    const cd = toast.cronDetails
    return (
      <div
        className="toast-card-cron-v2"
        style={{
          '--toast-color': toast.color || style.color,
          animation: exiting
            ? 'toastOut 0.3s cubic-bezier(.16,1,.3,1) forwards'
            : 'toastIn 0.4s cubic-bezier(.16,1,.3,1)',
        } as React.CSSProperties}
      >
        <button className="toast-close-v2" onClick={handleDismiss} aria-label="关闭">
          <X size={13} />
        </button>

        <div className="toast-cron-v2-head">
          <div className="toast-cron-v2-avatar-wrap">
            {cd.agent_avatar_url ? (
              <img
                src={cd.agent_avatar_url}
                alt=""
                className="toast-cron-v2-avatar"
                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
              />
            ) : (
              <div className="toast-cron-v2-avatar toast-cron-v2-avatar-emoji">
                {cd.agent_avatar_emoji || '⏰'}
              </div>
            )}
            <div className="toast-cron-v2-bell" aria-hidden>⏰</div>
          </div>
          <div className="toast-cron-v2-text">
            <div className="toast-cron-v2-title">{toast.title}</div>
            <div className="toast-cron-v2-by">来自 {cd.agent_name}</div>
          </div>
        </div>

        <div className="toast-cron-v2-meta">
          <span className="toast-cron-v2-time">{cd.cron_natural || cd.cron}</span>
          {cd.once_completed ? (
            <span className="toast-cron-v2-pill completed">✓ 一次性已完成</span>
          ) : (
            <span className="toast-cron-v2-pill recurring">{cd.mode_label}</span>
          )}
        </div>

        {cd.next_text && (
          <div className="toast-cron-v2-next">{cd.next_text}</div>
        )}

        <div className="toast-progress-v2">
          <div
            className="toast-progress-v2-bar"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>
    )
  }

  // ============ 其它 type 保持原版布局 ============
  return (
    <div
      className="toast-card"
      style={{
        '--toast-color': toast.color || style.color,
        animation: exiting
          ? 'toastOut 0.3s cubic-bezier(.16,1,.3,1) forwards'
          : 'toastIn 0.35s cubic-bezier(.16,1,.3,1)',
      } as React.CSSProperties}
    >
      <div className="toast-icon-glow" style={{ background: `${toast.color || style.color}15` }}>
        <span style={{ fontSize: 18 }}>{toast.icon || style.icon}</span>
      </div>
      <div className="toast-body">
        <div className="toast-title">{toast.title}</div>
        <div className="toast-desc">{toast.desc}</div>
      </div>
      <button className="toast-close" onClick={handleDismiss}>
        <X size={12} />
      </button>
      <div className="toast-progress">
        <div
          className="toast-progress-bar"
          style={{
            width: `${progress}%`,
            background: `linear-gradient(90deg, ${toast.color || style.color}88, ${toast.color || style.color})`,
          }}
        />
      </div>
    </div>
  )
}

// ---- Toast 容器 ----
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const idCounter = useRef(0)

  const addToast = useCallback((toast: Omit<ToastItem, 'id'>) => {
    const id = `toast-${++idCounter.current}`
    setToasts(prev => [...prev.slice(-4), { ...toast, id }])  // 最多5条
  }, [])

  const removeToast = useCallback((id: string) => {
    setToasts(prev => prev.filter(t => t.id !== id))
  }, [])

  return (
    <ToastContext.Provider value={{ addToast, removeToast }}>
      {children}
      {/* Toast 栈 */}
      <div className="toast-container">
        {toasts.map(toast => (
          <ToastCard key={toast.id} toast={toast} onDismiss={removeToast} />
        ))}
      </div>
      {/* 全局样式 */}
      <style>{`
        .toast-container {
          position: fixed;
          top: 56px;
          right: 16px;
          z-index: 99999;
          display: flex;
          flex-direction: column;
          gap: 10px;
          pointer-events: none;
          max-width: 420px;
          width: 420px;
        }
        .toast-card {
          pointer-events: auto;
          display: flex;
          align-items: flex-start;
          gap: 10px;
          padding: 12px 14px;
          border-radius: 12px;
          background: var(--bg-card);
          border: 1px solid color-mix(in srgb, var(--toast-color, #a78bfa) 25%, var(--border-color));
          box-shadow: 0 4px 20px rgba(0,0,0,0.25), 0 0 0 1px color-mix(in srgb, var(--toast-color, #a78bfa) 10%, transparent);
          position: relative;
          overflow: hidden;
          backdrop-filter: blur(8px);
        }
        @keyframes toastIn {
          from { opacity: 0; transform: translateX(40px) scale(0.95); }
          to   { opacity: 1; transform: translateX(0) scale(1); }
        }
        @keyframes toastOut {
          from { opacity: 1; transform: translateX(0) scale(1); }
          to   { opacity: 0; transform: translateX(40px) scale(0.95); }
        }
        .toast-icon-glow {
          width: 34px; height: 34px; border-radius: 10px;
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0;
        }
        .toast-body { flex: 1; min-width: 0; }
        .toast-title {
          font-size: 12px; font-weight: 600; color: var(--text-color);
          margin-bottom: 2px; line-height: 1.3;
        }
        .toast-desc {
          font-size: 11px; color: var(--text-secondary);
          line-height: 1.4; display: -webkit-box;
          -webkit-line-clamp: 2; -webkit-box-orient: vertical;
          overflow: hidden;
        }
        .toast-close {
          position: absolute; top: 6px; right: 6px;
          width: 18px; height: 18px; border-radius: 50%;
          border: none; background: transparent;
          color: var(--text-muted); cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          padding: 0; transition: all 0.15s;
        }
        .toast-close:hover { background: rgba(128,128,128,0.15); color: var(--text-color); }
        .toast-progress {
          position: absolute; bottom: 0; left: 0; right: 0;
          height: 2px; background: transparent;
        }
        .toast-progress-bar {
          height: 100%; border-radius: 0 0 12px 12px;
          transition: width 0.05s linear;
        }

        /* ====== Cron 专属 toast v2（高级版） ====== */
        .toast-card-cron-v2 {
          pointer-events: auto;
          position: relative;
          padding: 16px 18px 14px 18px;
          border-radius: 14px;
          background:
            linear-gradient(
              135deg,
              color-mix(in srgb, var(--toast-color) 14%, var(--bg-card)) 0%,
              color-mix(in srgb, #a78bfa 5%, var(--bg-card)) 100%
            );
          border: 1px solid color-mix(in srgb, var(--toast-color) 30%, transparent);
          box-shadow:
            0 12px 32px -8px color-mix(in srgb, var(--toast-color) 25%, transparent),
            0 2px 8px rgba(0,0,0,0.18),
            inset 0 1px 0 0 color-mix(in srgb, white 8%, transparent);
          backdrop-filter: blur(20px) saturate(180%);
          -webkit-backdrop-filter: blur(20px) saturate(180%);
          overflow: hidden;
        }
        /* 顶部装饰光带 */
        .toast-card-cron-v2::before {
          content: '';
          position: absolute;
          inset: 0 0 auto 0;
          height: 1px;
          background: linear-gradient(
            90deg,
            transparent 0%,
            color-mix(in srgb, var(--toast-color) 60%, transparent) 50%,
            transparent 100%
          );
        }
        .toast-close-v2 {
          position: absolute;
          top: 10px; right: 10px;
          width: 22px; height: 22px;
          border: none; background: transparent;
          color: var(--text-muted);
          cursor: pointer; border-radius: 6px;
          display: flex; align-items: center; justify-content: center;
          transition: all 140ms;
        }
        .toast-close-v2:hover {
          background: color-mix(in srgb, var(--text-muted) 14%, transparent);
          color: var(--text-color);
        }

        .toast-cron-v2-head {
          display: flex;
          align-items: center;
          gap: 12px;
          margin-bottom: 10px;
          padding-right: 22px;  /* 让 X 不挡到 */
        }
        .toast-cron-v2-avatar-wrap {
          position: relative;
          flex-shrink: 0;
        }
        .toast-cron-v2-avatar {
          width: 38px; height: 38px;
          border-radius: 50%;
          object-fit: cover;
          border: 2px solid color-mix(in srgb, var(--toast-color) 35%, var(--bg-card));
          box-shadow: 0 2px 8px color-mix(in srgb, var(--toast-color) 20%, transparent);
        }
        .toast-cron-v2-avatar-emoji {
          display: flex; align-items: center; justify-content: center;
          font-size: 20px;
          background: color-mix(in srgb, var(--toast-color) 15%, var(--bg-card));
        }
        /* 头像右下挂个小铃铛 = 这是定时任务来的 */
        .toast-cron-v2-bell {
          position: absolute;
          right: -3px; bottom: -3px;
          width: 18px; height: 18px;
          border-radius: 50%;
          background: var(--toast-color);
          color: white;
          font-size: 10px;
          display: flex; align-items: center; justify-content: center;
          border: 2px solid var(--bg-card);
          box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }

        .toast-cron-v2-text {
          flex: 1;
          min-width: 0;
        }
        .toast-cron-v2-title {
          font-size: 14.5px;
          font-weight: 600;
          color: var(--text-color);
          letter-spacing: 0.2px;
          line-height: 1.35;
          margin-bottom: 3px;
          overflow: hidden;
          text-overflow: ellipsis;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
        }
        .toast-cron-v2-by {
          font-size: 12px;
          color: var(--text-secondary);
          line-height: 1.4;
        }

        .toast-cron-v2-meta {
          display: flex;
          align-items: center;
          gap: 8px;
          flex-wrap: wrap;
          margin-bottom: 6px;
        }
        .toast-cron-v2-time {
          font-size: 13px;
          font-weight: 600;
          color: var(--toast-color);
          letter-spacing: 0.3px;
        }
        .toast-cron-v2-pill {
          display: inline-flex;
          align-items: center;
          font-size: 11px;
          font-weight: 500;
          padding: 2px 9px;
          border-radius: 999px;
          line-height: 1.6;
        }
        .toast-cron-v2-pill.recurring {
          color: var(--toast-color);
          background: color-mix(in srgb, var(--toast-color) 14%, transparent);
        }
        .toast-cron-v2-pill.completed {
          color: var(--accent-green);
          background: color-mix(in srgb, var(--accent-green) 16%, transparent);
        }

        .toast-cron-v2-next {
          font-size: 11.5px;
          color: var(--text-muted);
          margin-top: 2px;
          letter-spacing: 0.2px;
        }

        .toast-progress-v2 {
          position: absolute;
          bottom: 0; left: 0; right: 0;
          height: 2px;
          background: color-mix(in srgb, var(--toast-color) 8%, transparent);
        }
        .toast-progress-v2-bar {
          height: 100%;
          background: linear-gradient(
            90deg,
            color-mix(in srgb, var(--toast-color) 60%, transparent) 0%,
            var(--toast-color) 100%
          );
          border-radius: 0 2px 2px 0;
          transition: width 0.05s linear;
        }
      `}</style>
    </ToastContext.Provider>
  )
}

// ---- 便捷函数：从 GameNotification 创建 Toast ----
export function gameNotificationToToast(notif: GameNotification): Omit<ToastItem, 'id'> | null {
  switch (notif.type) {
    case 'achievement_unlocked': {
      const ach = notif.achievement || {}
      return {
        type: 'achievement',
        title: `🏆 成就解锁：${ach.title || '未知'}`,
        desc: ach.desc || '',
        icon: ach.icon || '🏆',
        urgency: ach.rarity === 'legendary' ? 'high' : ach.rarity === 'epic' ? 'medium' : 'low',
        color: ach.rarity_color || '#fbbf24',
        duration: ach.rarity === 'legendary' ? 8000 : undefined,
      }
    }
    case 'affinity_breakthrough': {
      return {
        type: 'affinity',
        title: `💗 ${notif.agent_name || '角色'} 好感度突破 ${Math.round((notif.threshold || 0) * 100)}%`,
        desc: `好感度从 ${Math.round((notif.old || 0) * 100)}% → ${Math.round((notif.new || 0) * 100)}%`,
        icon: '💗',
        urgency: normalizeUrgency(notif.urgency),
        color: '#ec4899',
      }
    }
    case 'affinity_drop': {
      return {
        type: 'affinity',
        title: `💔 ${notif.agent_name || '角色'} 好感度下降`,
        desc: `好感度从 ${Math.round((notif.old || 0) * 100)}% → ${Math.round((notif.new || 0) * 100)}%`,
        icon: '💔',
        urgency: 'high',
        color: '#f85149',
      }
    }
    case 'jealousy_event': {
      return {
        type: 'jealousy',
        title: `😤 ${notif.agent_name || '角色'} 吃醋了`,
        desc: notif.trigger || '',
        icon: '😤',
        urgency: 'medium',
        color: '#f97316',
      }
    }
    case 'bond_milestone': {
      return {
        type: 'bond',
        title: `🔗 羁绊里程碑：${notif.title || ''}`,
        desc: `${notif.agent_a_name || ''} ↔ ${notif.agent_b_name || ''} — ${notif.desc || ''}`,
        icon: '🔗',
        urgency: normalizeUrgency(notif.urgency),
        color: '#a78bfa',
      }
    }
    case 'label_evolution': {
      return {
        type: 'label',
        title: `🔄 关系变化：${notif.old_label} → ${notif.new_label}`,
        desc: `${notif.agent_a_name || ''} ↔ ${notif.agent_b_name || ''}`,
        icon: '🔄',
        urgency: 'medium',
        color: '#60a5fa',
      }
    }
    case 'user_relation_evolution': {
      return {
        type: 'label',
        title: `✨ ${notif.agent_name || '角色'} 对你的关系改变了`,
        desc: `${notif.old_line || ''}·${notif.old_stage || ''} → ${notif.new_line || ''}·${notif.new_stage || ''}`,
        icon: '✨',
        urgency: normalizeUrgency(notif.urgency),
        color: '#ec4899',
      }
    }
    default:
      return null
  }
}
