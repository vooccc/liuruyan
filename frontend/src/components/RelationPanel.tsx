import { useState, useEffect, useRef } from 'react'
import { Heart, Eye } from 'lucide-react'
import { getRelationSummary, getUserRelation, getAttention } from '../api/chat'
import AgentAvatar from './AgentAvatar'
import { getAvatarSrc, getAvatarFallback } from './avatarUtils'

interface Agent {
  id: string
  name: string
  avatar?: string
  avatar_url?: string
}

interface RelationPanelProps {
  currentAgentId: string
  agents: Agent[]
}

export function RelationPanel({ currentAgentId, agents }: RelationPanelProps) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<any>(null)
  const [userRel, setUserRel] = useState<any>(null)
  const [attentionData, setAttentionData] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  async function loadData() {
    if (!currentAgentId) return
    setLoading(true)
    try {
      const [summary, ur, att] = await Promise.all([
        getRelationSummary(currentAgentId).catch(() => null),
        getUserRelation(currentAgentId).catch(() => null),
        getAttention(currentAgentId).catch(() => null),
      ])
      setData(summary)
      setUserRel(ur?.user_relation || null)
      setAttentionData(att || null)
    } catch { /* */ }
    setLoading(false)
  }

  useEffect(() => { if (open) loadData() }, [open, currentAgentId])
  useEffect(() => {
    if (!open) return
    const handleMouseDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', handleMouseDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handleMouseDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  const currentName = agents.find(a => a.id === currentAgentId)?.name || ''
  const lineScores = userRel?.line_scores || {}
  const currentLine = userRel?.current_line || ''
  const currentStage = userRel?.current_stage || ''
  const relations = data?.relationships || []
  const attentionScore = attentionData?.attention_score ?? 0.5

  // 三线进度映射
  const lineConfig = [
    { key: '恋人线', keyEn: 'lover', emoji: '💕', color: '#ec4899', stages: ['好奇', '心动', '试探', '确认', '依恋', '共生'] },
    { key: '羁绊线', keyEn: 'bond', emoji: '🤝', color: '#a78bfa', stages: ['欣赏', '默契', '信任', '懂你', '无需言语'] },
    { key: '守护线', keyEn: 'guardian', emoji: '🛡️', color: '#3fb950', stages: ['陌生', '认可', '守护', '誓约', '以命相护'] },
  ]

  return (
    <div style={{ position: 'relative' }} ref={ref}>
      <style>{`
        .rp-btn {
          display: inline-flex; align-items: center; gap: 4px;
          padding: 4px 8px; border-radius: 16px;
          border: none; cursor: pointer; font-size: 11px;
          font-weight: 500; white-space: nowrap;
          background: transparent; color: var(--text-secondary);
          transition: all 0.2s ease; font-family: inherit; line-height: 1;
        }
        .rp-btn:hover { background: rgba(232,121,249,0.1); color: #e879f9; }
        .rp-btn.active { background: linear-gradient(135deg, #ec4899, #e879f9); color: #fff; box-shadow: 0 2px 8px rgba(236,72,153,0.35); }

        .rp-panel {
          position: absolute; top: calc(100% + 6px); left: 50%; transform: translateX(-50%);
          width: 320px; max-height: 480px; overflow-y: auto;
          border-radius: 16px; background: var(--bg-card);
          border: 1px solid var(--border-color);
          box-shadow: var(--shadow-md); z-index: 9999;
          animation: rpIn 0.22s cubic-bezier(.16,1,.3,1);
        }
        @keyframes rpIn {
          from { opacity: 0; transform: translateX(-50%) translateY(-4px) scale(0.97); }
          to   { opacity: 1; transform: translateX(-50%) translateY(6px) scale(1); }
        }

        .rp-head { padding: 16px 18px 12px; border-bottom: 1px solid var(--border-light); }
        .rp-head-row { display: flex; align-items: flex-start; justify-content: space-between; }
        .rp-title { font-size: 15px; font-weight: 700; color: var(--text-color); margin-bottom: 2px; line-height: 1.3; }
        .rp-subtitle { font-size: 11px; color: var(--text-secondary); opacity: 0.65; }
        .rp-close-btn {
          width: 24px; height: 24px; border-radius: 50%; border: none;
          background: rgba(128,128,128,0.07); color: var(--text-secondary);
          cursor: pointer; display: flex; align-items: center; justify-content: center;
          font-size: 13px; transition: all 0.15s; padding: 0; line-height: 1;
        }
        .rp-close-btn:hover { background: rgba(239,68,68,0.1); color: #ef4444; }

        .rp-body { padding: 12px 16px 14px; display: flex; flex-direction: column; gap: 12px; }

        /* 关注值仪表盘 */
        .rp-gauge {
          display: flex; align-items: center; gap: 14px;
          padding: 11px 14px; border-radius: 12px;
          background: rgba(96,165,250,0.04);
          border: 1px solid rgba(96,165,250,0.08);
        }
        .rp-gauge-ring {
          width: 46px; height: 46px; position: relative; flex-shrink: 0;
        }
        .rp-gauge-ring svg { transform: rotate(-90deg); }
        .rp-gauge-ring-label {
          position: absolute; inset: 0;
          display: flex; align-items: center; justify-content: center;
          font-size: 11px; font-weight: 700; color: var(--text-color);
        }
        .rp-gauge-info { flex: 1; }
        .rp-gauge-title { font-size: 11px; font-weight: 600; color: var(--text-color); margin-bottom: 2px; display: flex; align-items: center; gap: 4px; }
        .rp-gauge-desc { font-size: 10px; color: var(--text-secondary); }

        /* 三线卡片 */
        .rp-line-card {
          border-radius: 12px; padding: 13px 14px;
          background: rgba(254,205,211,0.04);
          border: 1px solid var(--border-light);
        }
        .rp-line-item {
          display: flex; align-items: center; gap: 8px;
          margin-bottom: 8px;
        }
        .rp-line-item:last-child { margin-bottom: 0; }
        .rp-line-emoji { font-size: 13px; flex-shrink: 0; }
        .rp-line-name { font-size: 11px; font-weight: 500; color: var(--text-color); width: 40px; flex-shrink: 0; }
        .rp-line-bar-bg {
          flex: 1; height: 6px; border-radius: 3px;
          background: var(--border-light); overflow: hidden;
          position: relative;
        }
        .rp-line-bar-fg {
          height: 100%; border-radius: 3px;
          transition: width 0.5s cubic-bezier(.16,1,.3,1);
          position: relative;
        }
        .rp-line-bar-fg::after {
          content: ''; position: absolute; right: 0; top: 0; bottom: 0; width: 12px;
          background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2));
          border-radius: 0 3px 3px 0;
        }
        .rp-line-score { font-size: 10px; font-weight: 600; color: var(--text-secondary); width: 32px; text-align: right; flex-shrink: 0; }
        .rp-line-stage-bar {
          margin-top: 10px; padding-top: 9px;
          border-top: 1px solid var(--border-light);
          display: flex; align-items: center; justify-content: center;
          gap: 6px; font-size: 11px; color: var(--text-secondary);
        }
        .rp-line-stage-bar b { color: var(--text-color); font-weight: 600; }

        /* 角色列表 */
        .rp-section-header {
          display: flex; align-items: center; gap: 5px;
          margin-bottom: 6px; font-size: 12px; font-weight: 600; color: var(--text-color);
        }
        .rp-list { display: flex; flex-direction: column; gap: 5px; }
        .rp-item {
          display: flex; align-items: center; gap: 9px;
          padding: 8px 11px; border-radius: 10px;
          border: 1px solid var(--border-light);
          transition: all 0.15s ease;
        }
        .rp-item:hover { background: rgba(139,92,246,0.04); border-color: rgba(139,92,246,0.15); transform: translateX(2px); }
        .rp-avatar-circle {
          width: 28px; height: 28px; border-radius: 50%;
          background: linear-gradient(145deg, rgba(139,92,246,0.12), rgba(236,72,153,0.1));
          display: flex; align-items: center; justify-content: center;
          font-size: 13px; flex-shrink: 0;
          box-shadow: 0 1px 3px rgba(139,92,246,0.1);
          overflow: hidden;
        }
        .rp-avatar-img-wrap { width: 100%; height: 100%; display: block; line-height: 0; }
        .rp-avatar-img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; display: block; }
        .rp-item-info { flex: 1; min-width: 0; }
        .rp-name-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 3px; }
        .rp-name-text {
          font-size: 12px; font-weight: 500; color: var(--text-color);
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 130px;
        }
        .rp-score { font-size: 11px; font-weight: 700; color: var(--text-color); flex-shrink: 0; font-variant-numeric: tabular-nums; }
        .rp-bar-bg { height: 4px; border-radius: 2px; background: rgba(148,163,184,0.2); overflow: hidden; }
        .rp-bar-fg { height: 100%; border-radius: 2px; transition: width 0.35s cubic-bezier(.16,1,.3,1); }

        .rp-loading { text-align: center; padding: 30px 20px; color: var(--text-secondary); font-size: 13px; }
        .rp-empty-state { text-align: center; padding: 26px 0; color: var(--text-secondary); font-size: 12px; opacity: 0.6; }

        .rp-panel::-webkit-scrollbar { width: 4px; }
        .rp-panel::-webkit-scrollbar-track { background: transparent; }
        .rp-panel::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.2); border-radius: 2px; }
      `}</style>

      {/* 心形按钮 */}
      <button className={`rp-btn ${open ? 'active' : ''}`} onClick={() => setOpen(!open)} title="关系网络">
        <Heart size={12} fill={open ? '#fff' : 'none'} strokeWidth={open ? 0 : 1.8} />
        <span>关系</span>
      </button>

      {/* 面板 */}
      {open && (
        <div className="rp-panel">
          <div className="rp-head">
            <div className="rp-head-row">
              <div>
                <div className="rp-title">{currentName} 的关系网络</div>
                <div className="rp-subtitle">好感度 · 羁绊值 · 三线关系</div>
              </div>
              <button className="rp-close-btn" onClick={() => setOpen(false)}>✕</button>
            </div>
          </div>

          {loading ? (
            <div className="rp-loading">加载中...</div>
          ) : (
            <div className="rp-body">
              {/* 关注值仪表盘 */}
              <div className="rp-gauge">
                <div className="rp-gauge-ring">
                  <svg width="46" height="46" viewBox="0 0 46 46">
                    <circle cx="23" cy="23" r="19" fill="none" stroke="var(--border-light)" strokeWidth="4" />
                    <circle
                      cx="23" cy="23" r="19" fill="none"
                      stroke="#60a5fa"
                      strokeWidth="4"
                      strokeLinecap="round"
                      strokeDasharray={`${2 * Math.PI * 19}`}
                      strokeDashoffset={`${2 * Math.PI * 19 * (1 - Math.min(attentionScore, 1))}`}
                      style={{ transition: 'stroke-dashoffset 0.6s cubic-bezier(.16,1,.3,1)' }}
                    />
                  </svg>
                  <div className="rp-gauge-ring-label">{Math.round(attentionScore * 100)}</div>
                </div>
                <div className="rp-gauge-info">
                  <div className="rp-gauge-title">
                    <Eye size={12} style={{ color: '#60a5fa' }} />
                    关注值
                  </div>
                  <div className="rp-gauge-desc">
                    {attentionScore >= 0.8 ? '极度在意你的一举一动' :
                     attentionScore >= 0.6 ? '对你的关注度很高' :
                     attentionScore >= 0.4 ? '对你保持关注' :
                     attentionScore >= 0.2 ? '偶尔想起你' : '你还不太被注意'}
                  </div>
                </div>
              </div>

              {/* 三线并行卡片 */}
              <div className="rp-line-card">
                {lineConfig.map(line => {
                  const score = lineScores[line.keyEn] ?? 0
                  const pct = Math.min(Math.max(score * 100, 0), 100)
                  const isActive = currentLine === line.key
                  return (
                    <div key={line.key} className="rp-line-item" style={{ opacity: isActive ? 1 : 0.6 }}>
                      <span className="rp-line-emoji">{line.emoji}</span>
                      <span className="rp-line-name" style={{ color: isActive ? line.color : 'var(--text-secondary)' }}>
                        {line.key.replace('线', '')}
                      </span>
                      <div className="rp-line-bar-bg">
                        <div
                          className="rp-line-bar-fg"
                          style={{
                            width: `${pct}%`,
                            background: `linear-gradient(90deg, ${line.color}55, ${line.color})`,
                            boxShadow: isActive ? `0 0 6px ${line.color}44` : 'none',
                          }}
                        />
                      </div>
                      <span className="rp-line-score" style={{ color: isActive ? line.color : 'var(--text-secondary)' }}>
                        {score.toFixed(1)}
                      </span>
                    </div>
                  )
                })}
                {currentLine && currentStage && (
                  <div className="rp-line-stage-bar">
                    当前: <b>{currentLine}</b> · 阶段 <b>{currentStage}</b>
                  </div>
                )}
              </div>

              {/* 角色间关系 */}
              {relations.length > 0 && (
                <>
                  <div className="rp-section-header">
                    <span style={{ fontSize: '14px' }}>👥</span> 角色关系
                  </div>
                  <div className="rp-list">
                    {relations.map((rel: any, i: number) => {
                      const aid = rel.agent_id || ''
                      const target = agents.find(a => a.id === aid)
                      const displayName = target?.name || (aid.charAt(0).toUpperCase() + aid.slice(1))
                      const affinity = rel.affinity ?? rel.bond_depth ?? 0
                      const label = rel.current_label || ''

                      let barColor = '#94a3b8'
                      if (affinity >= 0.7) barColor = '#ef4444'
                      else if (affinity >= 0.4) barColor = '#a78bfa'
                      else if (affinity >= 0.2) barColor = '#38bdf8'

                      const avatarSrc = getAvatarSrc(target)
                      const avatarFallback = getAvatarFallback(target, displayName.charAt(0))
                      return (
                        <div key={`${rel.agent_id || rel.id || i}`} className="rp-item">
                          <div className="rp-avatar-circle">
                            <AgentAvatar
                              name={displayName}
                              src={avatarSrc}
                              avatar={avatarFallback}
                              fallbackAvatar={displayName.charAt(0)}
                              wrapperClassName="rp-avatar-img-wrap"
                              imageClassName="rp-avatar-img"
                            />
                          </div>
                          <div className="rp-item-info">
                            <div className="rp-name-row">
                              <span className="rp-name-text">{displayName}</span>
                              {label && (
                                <span style={{
                                  fontSize: 10, color: '#a78bfa', fontWeight: 500,
                                  background: 'rgba(167,139,250,0.08)', padding: '1px 6px', borderRadius: '8px',
                                  marginRight: '4px',
                                }}>{label}</span>
                              )}
                              <span className="rp-score">{affinity.toFixed(3)}</span>
                            </div>
                            <div className="rp-bar-bg">
                              <div className="rp-bar-fg" style={{
                                width: `${Math.min(Math.max(affinity * 100, 0), 100)}%`,
                                background: `linear-gradient(90deg, ${barColor}66, ${barColor})`,
                              }}/>
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </>
              )}

              {/* 空状态 */}
              {Object.keys(lineScores).length === 0 && relations.length === 0 && (
                <div className="rp-empty-state">暂无关系数据</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
