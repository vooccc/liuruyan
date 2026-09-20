import { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Sparkles, X, ChevronRight, MessageCircle, Zap, HeartHandshake, Users, Flame, type LucideIcon } from 'lucide-react'
import { Agent, createScene, SceneInfo } from '../api/chat'
import AgentAvatar from './AgentAvatar'
import { getAvatarSrc } from './avatarUtils'

/**
 * CollabLauncher —— 协作发起器
 *
 * 单一职责：弹 modal 收集 [场景类型 + 参与者]，调 createScene API 后通知父节点。
 * 列表展示交给议事厅侧栏（RoundtableList）。
 */

interface CollabLauncherProps {
  agents: Agent[]
  /** 触发按钮的视觉风格 */
  triggerVariant?: 'icon' | 'primary'
  /** primary 模式下的按钮文案 */
  triggerLabel?: string
  /** 创建成功后回调，父节点可据此跳转到新建会话 */
  onCreated?: (scene: SceneInfo, sessionId?: string) => void
}

/** 场景类型配置 */
const SCENE_CONFIG: Record<string, { color: string; Icon: LucideIcon; label: string; desc: string }> = {
  free:        { color: 'var(--text-secondary)', Icon: MessageCircle, label: '自由', desc: '无预设剧本，纯多人协作讨论' },
  encounter:   { color: 'var(--accent-purple)', Icon: Sparkles,       label: '邂逅', desc: '偶然相遇，关系微变' },
  conflict:    { color: 'var(--primary-color)', Icon: Zap,            label: '对峙', desc: '立场碰撞，张力拉满' },
  cooperation: { color: 'var(--accent-green)',  Icon: HeartHandshake, label: '协力', desc: '各展所长，协作突破' },
  gathering:   { color: 'var(--accent-blue)',   Icon: Users,          label: '群聚', desc: '众人聚集，化学反应' },
  crisis:      { color: 'var(--accent-red)',    Icon: Flame,          label: '危机', desc: '考验关系，关键抉择' },
}

export function CollabLauncher({
  agents,
  triggerVariant = 'icon',
  triggerLabel = '发起协作',
  onCreated,
}: CollabLauncherProps) {
  const [showPanel, setShowPanel] = useState(false)
  const [selectedType, setSelectedType] = useState<string>('free')
  const [selectedAgents, setSelectedAgents] = useState<string[]>([])
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  const btnRef = useRef<HTMLButtonElement>(null)

  // ESC 关闭；modal 打开时锁体滚动
  useEffect(() => {
    if (!showPanel) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeModal()
    }
    window.addEventListener('keydown', handler)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', handler)
      document.body.style.overflow = prevOverflow
    }
  }, [showPanel])

  const closeModal = () => {
    setShowPanel(false)
    setError('')
  }

  const openModal = () => {
    // 每次打开重置表单
    setSelectedType('free')
    setSelectedAgents([])
    setError('')
    setShowPanel(true)
  }

  const handleCreate = async () => {
    if (selectedAgents.length < 2) {
      setError('至少选 2 个参与者')
      return
    }
    setCreating(true)
    setError('')
    try {
      const result = await createScene({
        scene_type: selectedType,
        participants: selectedAgents,
        force: true,
      })
      closeModal()
      onCreated?.(result.scene, result.session_id)
    } catch (e: any) {
      setError(e.message || '创建失败')
    } finally {
      setCreating(false)
    }
  }

  /* ====== Modal CSS ====== */
  const portalCSS = `
@keyframes clOverlayIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes clModalIn {
  from { opacity: 0; transform: translateY(8px) scale(0.97); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
.cl-overlay {
  position: fixed; inset: 0; z-index: 9999;
  display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, 0.28);
  animation: clOverlayIn 0.16s ease-out;
}
.cl-panel { width: min(480px, calc(100vw - 32px)); max-height: calc(100vh - 64px);
  border-radius:16px; overflow:hidden; display:flex; flex-direction:column;
  background:var(--bg-card); border:1px solid var(--border-color);
  box-shadow:0 24px 80px rgba(0,0,0,0.5), 0 0 1px rgba(255,255,255,0.04) inset;
  animation: clModalIn 0.18s ease-out;
}
.cl-header { padding:14px 18px;
  background:linear-gradient(135deg, rgba(188,140,255,0.06), rgba(96,165,250,0.04));
  border-bottom:1px solid var(--border-light);
}
.cl-type-grid{ display:grid; grid-template-columns:repeat(6,1fr); gap:6px; margin-bottom:14px; }
.cl-type-card{ display:flex; flex-direction:column; align-items:center; justify-content:center; gap:6px;
  padding:12px 4px; border-radius:10px; transition:background 160ms ease, border-color 160ms ease; cursor:pointer;
  border:1px solid transparent; background:var(--bg-tertiary); font-family:inherit;
}
.cl-type-card:hover{ background:var(--bg-secondary); }
.cl-agent-item{ display:flex; align-items:center; gap:10px; padding:7px 10px;
  border-radius:10px; cursor:pointer; transition:background 160ms ease, border-color 160ms ease, color 160ms ease;
  border:1px solid transparent;
  width:100%; text-align:left; background:transparent; font-family:inherit;
  font-size:12.5px; color:var(--text-secondary);
}
.cl-agent-item:hover{ background:var(--bg-secondary); }
.cl-agent-item.selected{
  background:color-mix(in srgb,var(--accent-purple) 10%,transparent);
  border-color:color-mix(in srgb,var(--accent-purple) 22%,transparent);
  color:var(--text-color);
}
.cl-agent-avatar{ flex:none; width:26px; height:26px; border-radius:50%; overflow:hidden;
  display:inline-flex; align-items:center; justify-content:center;
  background:var(--bg-tertiary); border:1px solid var(--border-light);
  font-size:12px; color:var(--text-secondary); font-weight:500;
}
.cl-agent-avatar-img{ display:inline-flex; width:100%; height:100%; }
.cl-agent-avatar-img-el{ width:100%; height:100%; object-fit:cover; display:block; }
.cl-agent-avatar-text{ font-size:12px; }
`

  /* ====== Trigger CSS（独立挂载，不依赖 modal 打开）====== */
  const triggerCSS = `
.cl-trigger-primary{
  box-sizing: border-box;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  height: 33px; padding: 0 12px;
  line-height: 1;
  font-family: inherit; font-size: 12.5px; font-weight: 500;
  letter-spacing: 0.06em;
  color: var(--text-color);
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 9px;
  cursor: pointer;
  transition: background 180ms ease, border-color 180ms ease, color 180ms ease;
}
.cl-trigger-primary svg{
  display: block;
  color: var(--accent-purple);
  opacity: 0.7;
  transition: opacity 180ms ease;
}
.cl-trigger-primary:hover{
  background: var(--bg-secondary);
  border-color: color-mix(in srgb, var(--accent-purple) 38%, var(--border-color));
}
.cl-trigger-primary:hover svg{ opacity: 1; }
.cl-trigger-primary:active{
  background: var(--bg-tertiary);
}
.cl-trigger-primary:focus-visible{
  outline: 1px solid color-mix(in srgb, var(--accent-purple) 50%, transparent);
  outline-offset: 2px;
}
`

  /* 渲染 Modal 内容 */
  const renderPortalContent = () => (
    <div
      id="collab-launcher-portal"
      className="cl-overlay"
      onMouseDown={(e) => { if (e.target === e.currentTarget) closeModal() }}
      role="dialog"
      aria-modal="true"
      aria-label="发起协作"
    >
      <style>{portalCSS}</style>

      <div className="cl-panel">
        {/* 头部 */}
        <div className="cl-header">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{
                width: 32, height: 32, borderRadius: 10,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: 'linear-gradient(135deg, rgba(188,140,255,0.15), rgba(188,140,255,0.05))',
              }}>
                <Sparkles size={15} style={{ color: 'var(--accent-purple)' }} />
              </div>
              <div>
                <h3 style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--text-color)', lineHeight: 1.2 }}>
                  发起一场协作
                </h3>
                <p style={{ fontSize: 10.5, color: 'var(--text-muted)', marginTop: 1.5 }}>
                  选类型 + 选人 · 自由模式无剧本
                </p>
              </div>
            </div>
            <button onClick={closeModal} style={{
              padding: '6px 8px', borderRadius: 8, border: 'none', background: '#f0f0f0',
              color: '#6b7280', cursor: 'pointer',
              transition: 'background 0.12s',
            }}
              onMouseEnter={e => { e.currentTarget.style.background = '#e5e7eb'; e.currentTarget.style.color = '#374151' }}
              onMouseLeave={e => { e.currentTarget.style.background = '#f0f0f0'; e.currentTarget.style.color = '#6b7280' }}
            >
              <X size={14} />
            </button>
          </div>
        </div>

        {/* 创建表单 */}
        <div style={{ padding: 16, overflowY: 'auto' }}>
          <div className="cl-type-grid">
            {Object.entries(SCENE_CONFIG).map(([type, cfg]) => {
              const active = selectedType === type
              const Icon = cfg.Icon
              return (
                <button key={type} className="cl-type-card"
                  onClick={() => setSelectedType(type)}
                  style={active ? {
                    background: `color-mix(in srgb, ${cfg.color} 10%, transparent)`,
                    borderColor: `color-mix(in srgb, ${cfg.color} 35%, transparent)`,
                  } : {}}
                >
                  <Icon size={16} strokeWidth={1.6}
                    style={{ color: active ? cfg.color : 'var(--text-muted)', transition: 'color 160ms ease' }} />
                  <span style={{ fontSize: 10.5, fontWeight: 500, letterSpacing: '0.04em',
                    color: active ? cfg.color : 'var(--text-muted)'
                  }}>{cfg.label}</span>
                </button>
              )
            })}
          </div>

          <div style={{
            marginBottom: 14, padding: '8px 12px', borderRadius: 8, textAlign: 'center',
            background: `color-mix(in srgb, ${SCENE_CONFIG[selectedType].color} 6%, transparent)`,
          }}>
            <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
              {SCENE_CONFIG[selectedType].desc}
            </span>
          </div>

          <h4 style={{ fontSize: 10.5, fontWeight: 500, color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6
          }}>参与者（至少 2 个，不限上限）</h4>
          <div style={{ maxHeight: 200, overflowY: 'auto', marginBottom: 12, scrollbarWidth: 'thin' }}>
            {agents.map(agent => {
              const isSelected = selectedAgents.includes(agent.id)
              return (
                <button key={agent.id}
                  className={`cl-agent-item${isSelected ? ' selected' : ''}`}
                  onClick={() => setSelectedAgents(prev =>
                    isSelected ? prev.filter(a => a !== agent.id) : [...prev, agent.id]
                  )}
                >
                  <span className="cl-agent-avatar">
                    <AgentAvatar
                      name={agent.name}
                      src={getAvatarSrc(agent)}
                      fallbackAvatar={agent.name?.[0] || 'A'}
                      wrapperClassName="cl-agent-avatar-img"
                      imageClassName="cl-agent-avatar-img-el"
                      plainClassName="cl-agent-avatar-text"
                    />
                  </span>
                  <span style={{ flex: 1 }}>{agent.name}</span>
                  {isSelected && <ChevronRight size={12} />}
                </button>
              )
            })}
          </div>

          {error && (
            <div style={{
              marginBottom: 10, padding: '8px 12px', borderRadius: 8,
              background: 'color-mix(in srgb, var(--accent-red) 8%, transparent)',
              border: '1px solid color-mix(in srgb, var(--accent-red) 15%, transparent)',
            }}>
              <p style={{ fontSize: 11, color: 'var(--accent-red)' }}>{error}</p>
            </div>
          )}

          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={closeModal} style={{
              flex: 1, padding: '10px 0', borderRadius: 10,
              fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)',
              background: 'var(--bg-tertiary)', border: '1px solid var(--border-light)',
              cursor: 'pointer', fontFamily: 'inherit',
            }}>取消</button>
            <button onClick={handleCreate} disabled={creating} style={{
              flex: 1, padding: '10px 0', borderRadius: 10,
              fontSize: 12, fontWeight: 600, color: '#fff', border: 'none',
              cursor: creating ? 'not-allowed' : 'pointer', fontFamily: 'inherit',
              background: creating ? 'var(--text-muted)' : 'linear-gradient(135deg, #a78bfa, #8b5cf6)',
              opacity: creating ? 0.5 : 1,
            }}>
              {creating ? '创建中...' : '🎬 开始协作'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )

  /* ===== 触发按钮 ===== */
  const renderTrigger = () => {
    if (triggerVariant === 'primary') {
      return (
        <button ref={btnRef} onClick={openModal}
          className="cl-trigger-primary" title={triggerLabel}>
          <Sparkles size={14} strokeWidth={2.2} />
          <span>{triggerLabel}</span>
        </button>
      )
    }
    return (
      <button ref={btnRef} onClick={openModal}
        className="icon-btn" title={triggerLabel}>
        <Sparkles size={14} />
      </button>
    )
  }

  return (
    <>
      <style>{triggerCSS}</style>
      {renderTrigger()}
      {showPanel && createPortal(renderPortalContent(), document.body)}
    </>
  )
}
