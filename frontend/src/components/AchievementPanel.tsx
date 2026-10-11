import { useState, useEffect, useRef } from 'react'
import { X, Lock } from 'lucide-react'
import { getAchievements, getAchievementStats, type Achievement } from '../api/chat'

interface AchievementPanelProps {
  onClose: () => void
}

export function AchievementPanel({ onClose }: AchievementPanelProps) {
  const [achievements, setAchievements] = useState<Achievement[]>([])
  const [stats, setStats] = useState<{ unlocked: number; total: number } | null>(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<string>('all')
  const [showUnlockedOnly, setShowUnlockedOnly] = useState(false)
  const [newlyRevealed, setNewlyRevealed] = useState<Set<string>>(new Set())
  const panelRef = useRef<HTMLDivElement>(null)

  const loadData = async () => {
    setLoading(true)
    try {
      const [achData, statsData] = await Promise.all([
        getAchievements().catch(() => ({ achievements: [] })),
        getAchievementStats().catch(() => ({ unlocked: 0, total: 0 })),
      ])
      setAchievements(achData.achievements || [])
      setStats(statsData)
    } catch { /* */ }
    setLoading(false)
  }

  useEffect(() => { loadData() }, [])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  // 解锁动画：标记新解锁的
  useEffect(() => {
    const unlockedIds = achievements
      .filter(a => a.unlocked && a.unlocked_at)
      .filter(a => {
        try {
          const t = new Date(a.unlocked_at!).getTime()
          return Date.now() - t < 60000  // 1分钟内解锁的
        } catch { return false }
      })
      .map(a => a.id)
    if (unlockedIds.length) {
      setNewlyRevealed(new Set(unlockedIds))
      setTimeout(() => setNewlyRevealed(new Set()), 3000)
    }
  }, [achievements])

  const filtered = achievements.filter(a => {
    if (filter !== 'all' && a.category !== filter) return false
    if (showUnlockedOnly && !a.unlocked) return false
    return true
  })

  const unlockedCount = stats?.unlocked ?? achievements.filter(a => a.unlocked).length
  const totalCount = stats?.total ?? achievements.length
  const progressPct = totalCount > 0 ? (unlockedCount / totalCount) * 100 : 0

  return (
    <div className="ach-overlay" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="ach-panel" ref={panelRef}>
        <style>{`
          .ach-overlay {
            position: fixed; inset: 0; z-index: 10000;
            background: rgba(0,0,0,0.5); backdrop-filter: blur(4px);
            display: flex; align-items: center; justify-content: center;
            animation: achFadeIn 0.2s ease;
          }
          @keyframes achFadeIn { from { opacity: 0; } to { opacity: 1; } }

          .ach-panel {
            width: 520px; max-height: 80vh;
            border-radius: 20px; overflow: hidden;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            box-shadow: 0 24px 80px rgba(0,0,0,0.4);
            animation: achSlideIn 0.3s cubic-bezier(.16,1,.3,1);
          }
          @keyframes achSlideIn {
            from { opacity: 0; transform: translateY(20px) scale(0.97); }
            to { opacity: 1; transform: translateY(0) scale(1); }
          }

          .ach-header {
            padding: 20px 24px 16px;
            background: linear-gradient(135deg, rgba(251,191,36,0.06), rgba(167,139,250,0.06));
            border-bottom: 1px solid var(--border-light);
          }
          .ach-header-top {
            display: flex; align-items: flex-start; justify-content: space-between;
            margin-bottom: 14px;
          }
          .ach-title-row {
            display: flex; align-items: center; gap: 10px;
          }
          .ach-title-icon {
            width: 36px; height: 36px; border-radius: 10px;
            background: linear-gradient(135deg, rgba(251,191,36,0.15), rgba(167,139,250,0.12));
            display: flex; align-items: center; justify-content: center;
            font-size: 18px;
          }
          .ach-title {
            font-size: 16px; font-weight: 700; color: var(--text-color);
          }
          .ach-subtitle {
            font-size: 11px; color: var(--text-secondary); margin-top: 2px;
          }
          .ach-close-btn {
            width: 28px; height: 28px; border-radius: 50%;
            border: none; background: rgba(128,128,128,0.07);
            color: var(--text-secondary); cursor: pointer;
            display: flex; align-items: center; justify-content: center;
            transition: all 0.15s;
          }
          .ach-close-btn:hover { background: rgba(239,68,68,0.1); color: #ef4444; }

          /* 进度条 */
          .ach-progress-bar {
            height: 6px; border-radius: 3px;
            background: var(--border-light); overflow: hidden;
          }
          .ach-progress-fill {
            height: 100%; border-radius: 3px;
            background: linear-gradient(90deg, #fbbf24, #a78bfa);
            transition: width 0.6s cubic-bezier(.16,1,.3,1);
          }
          .ach-progress-text {
            display: flex; justify-content: space-between;
            font-size: 11px; color: var(--text-secondary);
            margin-top: 6px;
          }

          /* 筛选 */
          .ach-filters {
            display: flex; gap: 6px; padding: 12px 24px;
            border-bottom: 1px solid var(--border-light);
          }
          .ach-filter-btn {
            padding: 4px 10px; border-radius: 12px;
            border: 1px solid var(--border-light);
            background: transparent; color: var(--text-secondary);
            font-size: 11px; cursor: pointer; transition: all 0.15s;
            font-family: inherit;
          }
          .ach-filter-btn:hover { border-color: rgba(167,139,250,0.3); color: var(--text-color); }
          .ach-filter-btn.active {
            background: rgba(167,139,250,0.1);
            border-color: rgba(167,139,250,0.3);
            color: var(--accent-purple);
          }

          /* 成就列表 */
          .ach-list {
            padding: 12px 16px; overflow-y: auto; max-height: 50vh;
          }
          .ach-list::-webkit-scrollbar { width: 4px; }
          .ach-list::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.2); border-radius: 2px; }

          .ach-item {
            display: flex; align-items: center; gap: 12px;
            padding: 10px 12px; border-radius: 12px;
            margin-bottom: 6px; transition: all 0.2s;
            border: 1px solid transparent;
          }
          .ach-item:hover {
            background: rgba(167,139,250,0.03);
            border-color: var(--border-light);
          }
          .ach-item.locked { opacity: 0.45; }
          .ach-item.newly-unlocked {
            animation: achUnlock 0.8s cubic-bezier(.16,1,.3,1);
            border-color: rgba(251,191,36,0.3);
            background: rgba(251,191,36,0.04);
          }
          @keyframes achUnlock {
            0% { transform: scale(1); }
            30% { transform: scale(1.03); border-color: rgba(251,191,36,0.6); }
            100% { transform: scale(1); }
          }

          .ach-icon-box {
            width: 40px; height: 40px; border-radius: 10px;
            display: flex; align-items: center; justify-content: center;
            font-size: 20px; flex-shrink: 0;
            position: relative;
          }
          .ach-icon-box.legendary {
            background: linear-gradient(135deg, rgba(251,191,36,0.12), rgba(251,191,36,0.04));
            box-shadow: 0 0 12px rgba(251,191,36,0.15);
          }
          .ach-icon-box.epic {
            background: linear-gradient(135deg, rgba(192,132,252,0.12), rgba(192,132,252,0.04));
            box-shadow: 0 0 8px rgba(192,132,252,0.1);
          }
          .ach-icon-box.rare {
            background: linear-gradient(135deg, rgba(96,165,250,0.10), rgba(96,165,250,0.03));
          }
          .ach-icon-box.common {
            background: rgba(148,163,184,0.06);
          }

          .ach-info { flex: 1; min-width: 0; }
          .ach-name {
            font-size: 12px; font-weight: 600; color: var(--text-color);
            margin-bottom: 2px; line-height: 1.3;
          }
          .ach-desc-text {
            font-size: 11px; color: var(--text-secondary);
            line-height: 1.4;
          }

          .ach-rarity-badge {
            font-size: 9px; font-weight: 600;
            padding: 2px 7px; border-radius: 8px;
            flex-shrink: 0; white-space: nowrap;
            letter-spacing: 0.02em;
          }

          .ach-empty {
            text-align: center; padding: 40px 20px;
            color: var(--text-secondary); font-size: 13px;
          }
        `}</style>

        {/* 头部 */}
        <div className="ach-header">
          <div className="ach-header-top">
            <div className="ach-title-row">
              <div className="ach-title-icon">🏆</div>
              <div>
                <div className="ach-title">成就之殿</div>
                <div className="ach-subtitle">你与角色们共同书写的里程碑</div>
              </div>
            </div>
            <button className="ach-close-btn" onClick={onClose}><X size={14} /></button>
          </div>

          {/* 总进度 */}
          <div className="ach-progress-bar">
            <div className="ach-progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
          <div className="ach-progress-text">
            <span>{unlockedCount} / {totalCount} 已解锁</span>
            <span>{Math.round(progressPct)}%</span>
          </div>
        </div>

        {/* 筛选 */}
        <div className="ach-filters">
          {[
            { key: 'all', label: '全部' },
            { key: 'relation', label: '💕 关系' },
            { key: 'work', label: '⚡ 工作' },
            { key: 'narrative', label: '📖 叙事' },
            { key: 'hidden', label: '❓ 隐藏' },
          ].map(f => (
            <button
              key={f.key}
              className={`ach-filter-btn ${filter === f.key ? 'active' : ''}`}
              onClick={() => setFilter(f.key)}
            >{f.label}</button>
          ))}
          <button
            className={`ach-filter-btn ${showUnlockedOnly ? 'active' : ''}`}
            onClick={() => setShowUnlockedOnly(!showUnlockedOnly)}
            style={{ marginLeft: 'auto' }}
          >已解锁</button>
        </div>

        {/* 成就列表 */}
        <div className="ach-list">
          {loading ? (
            <div className="ach-empty">加载中...</div>
          ) : filtered.length === 0 ? (
            <div className="ach-empty">暂无匹配的成就</div>
          ) : (
            filtered.map(ach => {
              const isNew = newlyRevealed.has(ach.id)
              const rarityClass = ach.rarity || 'common'
              return (
                <div
                  key={ach.id}
                  className={`ach-item ${!ach.unlocked ? 'locked' : ''} ${isNew ? 'newly-unlocked' : ''}`}
                >
                  <div className={`ach-icon-box ${rarityClass}`}>
                    {ach.unlocked ? ach.icon : <Lock size={16} style={{ color: 'var(--text-muted)' }} />}
                    {ach.rarity_glow && ach.unlocked && (
                      <div style={{
                        position: 'absolute', inset: -3, borderRadius: 13,
                        border: `1px solid ${ach.rarity_color}33`,
                        animation: 'achGlow 2s ease-in-out infinite alternate',
                      }} />
                    )}
                  </div>
                  <div className="ach-info">
                    <div className="ach-name">{ach.title}</div>
                    <div className="ach-desc-text">{ach.desc}</div>
                  </div>
                  <span
                    className="ach-rarity-badge"
                    style={{
                      color: ach.rarity_color,
                      background: `${ach.rarity_color}12`,
                      border: `1px solid ${ach.rarity_color}22`,
                    }}
                  >{ach.rarity_label}</span>
                </div>
              )
            })
          )}
        </div>

        <style>{`
          @keyframes achGlow {
            from { box-shadow: 0 0 4px ${'rgba(251,191,36,0.1)'}; }
            to   { box-shadow: 0 0 12px ${'rgba(251,191,36,0.25)'}; }
          }
        `}</style>
      </div>
    </div>
  )
}
