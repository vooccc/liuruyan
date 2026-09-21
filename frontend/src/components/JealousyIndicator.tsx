import { useState, useEffect, useRef } from 'react'
import { Flame } from 'lucide-react'
import { getActiveJealousies, type JealousyEvent } from '../api/chat'
import { startPolling } from '../lib/pollingLoop'

interface JealousyIndicatorProps {
  currentAgentId: string
  agents: { id: string; name: string; avatar?: string }[]
}

/** 吃醋风格中文映射 */
const STYLE_LABELS: Record<string, { label: string; emoji: string; color: string }> = {
  cold:       { label: '冷冰冰', emoji: '🧊', color: '#60a5fa' },
  hot:        { label: '炸毛了', emoji: '🔥', color: '#f97316' },
  silent:     { label: '暗涌', emoji: '🌊', color: '#8b5cf6' },
  playful:    { label: '阴阳怪气', emoji: '😏', color: '#ec4899' },
  protective: { label: '守护', emoji: '🛡️', color: '#3fb950' },
}

export function JealousyIndicator({ currentAgentId, agents }: JealousyIndicatorProps) {
  const [jealousies, setJealousies] = useState<JealousyEvent[]>([])
  const [showPanel, setShowPanel] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  const loadJealousies = async () => {
    try {
      const data = await getActiveJealousies()
      setJealousies(data.jealousies || [])
    } catch { /* */ }
  }

  useEffect(() => {
    return startPolling(loadJealousies, 20000)
  }, [currentAgentId])

  useEffect(() => {
    if (!showPanel) return
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setShowPanel(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showPanel])

  // 当前角色是否在吃醋
  const myJealousy = jealousies.find(j => j.jealous_agent === currentAgentId)
  const activeCount = jealousies.length

  // 如果没人吃醋且面板没开，不显示按钮
  if (activeCount === 0 && !showPanel) return null

  return (
    <div className="relative" ref={panelRef}>
      <style>{`
        .jea-btn {
          display: inline-flex; align-items: center; gap: 4px;
          padding: 3px 7px; border-radius: 13px;
          border: 1px solid transparent; cursor: pointer;
          font-size: 10.5px; font-weight: 500; white-space: nowrap;
          background: transparent; color: var(--text-secondary);
          transition: all 0.2s ease; font-family: inherit; line-height: 1;
        }
        .jea-btn.active {
          background: linear-gradient(135deg, rgba(249,115,22,0.12), rgba(236,72,153,0.10));
          border-color: rgba(249,115,22,0.25);
          color: #f97316;
        }
        .jea-btn:hover {
          background: rgba(249,115,22,0.08);
          color: #f97316;
        }

        .jea-pulse {
          display: inline-block;
          width: 5px; height: 5px;
          border-radius: 50%;
          background: #f97316;
          animation: jeaPulse 1.5s ease-in-out infinite;
        }
        @keyframes jeaPulse {
          0%, 100% { opacity: 0.4; transform: scale(1); }
          50% { opacity: 1; transform: scale(1.4); }
        }

        .jea-panel {
          position: absolute;
          top: calc(100% + 6px);
          right: 0;
          width: 280px;
          max-height: 360px;
          overflow-y: auto;
          border-radius: 14px;
          background: var(--bg-card);
          border: 1px solid var(--border-color);
          box-shadow: var(--shadow-md);
          z-index: 9999;
          animation: jeaIn 0.22s cubic-bezier(.16,1,.3,1);
        }
        @keyframes jeaIn {
          from { opacity: 0; transform: translateY(-4px) scale(0.97); }
          to   { opacity: 1; transform: translateY(0) scale(1); }
        }

        .jea-head {
          padding: 14px 16px 10px;
          border-bottom: 1px solid var(--border-light);
        }
        .jea-head-title {
          font-size: 13px; font-weight: 600; color: var(--text-color);
          display: flex; align-items: center; gap: 6px;
        }
        .jea-head-sub {
          font-size: 10px; color: var(--text-secondary); margin-top: 3px;
        }

        .jea-body { padding: 10px 14px 14px; display: flex; flex-direction: column; gap: 8px; }

        .jea-card {
          padding: 10px 12px; border-radius: 10px;
          border-left: 3px solid var(--accent-purple);
          background: rgba(139,92,246,0.03);
        }
        .jea-card.is-me {
          border-left-color: #f97316;
          background: rgba(249,115,22,0.04);
        }
        .jea-card-row {
          display: flex; align-items: center; gap: 8px;
          margin-bottom: 6px;
        }
        .jea-card-avatar {
          width: 24px; height: 24px; border-radius: 50%;
          background: linear-gradient(145deg, rgba(139,92,246,0.12), rgba(249,115,22,0.1));
          display: flex; align-items: center; justify-content: center;
          font-size: 12px; flex-shrink: 0;
        }
        .jea-card-name {
          font-size: 12px; font-weight: 500; color: var(--text-color);
        }
        .jea-card-style {
          font-size: 10px; font-weight: 500;
          padding: 1px 6px; border-radius: 8px;
          margin-left: auto;
        }
        .jea-card-intensity {
          height: 3px; border-radius: 2px;
          background: var(--border-light); overflow: hidden;
          margin-top: 4px;
        }
        .jea-card-bar {
          height: 100%; border-radius: 2px;
          transition: width 0.4s cubic-bezier(.16,1,.3,1);
        }
        .jea-card-remark {
          font-size: 10px; color: var(--text-secondary);
          margin-top: 5px; line-height: 1.4;
          font-style: italic;
        }

        .jea-panel::-webkit-scrollbar { width: 4px; }
        .jea-panel::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.2); border-radius: 2px; }
      `}</style>

      {/* 吃醋指示按钮 */}
      <button
        className={`jea-btn ${myJealousy ? 'active' : ''}`}
        onClick={() => { setShowPanel(!showPanel); loadJealousies() }}
        title="吃醋状态"
      >
        <Flame size={11} fill={myJealousy ? '#f97316' : 'none'} strokeWidth={myJealousy ? 0 : 1.8} />
        {activeCount > 0 && <span className="jea-pulse" />}
        {activeCount > 0 && <span>{activeCount}</span>}
      </button>

      {/* 吃醋面板 */}
      {showPanel && (
        <div className="jea-panel">
          <div className="jea-head">
            <div className="jea-head-title">
              <Flame size={14} style={{ color: '#f97316' }} />
              吃醋雷达
            </div>
            <div className="jea-head-sub">
              {activeCount > 0
                ? `${activeCount} 位角色正在吃醋中...`
                : '目前风平浪静'
              }
            </div>
          </div>

          <div className="jea-body">
            {jealousies.length === 0 ? (
              <div style={{ textAlign: 'center', padding: '20px 0', color: 'var(--text-secondary)', fontSize: 12, opacity: 0.6 }}>
                暂无吃醋事件
              </div>
            ) : (
              jealousies.map(j => {
                const agent = agents.find(a => a.id === j.jealous_agent)
                const isMe = j.jealous_agent === currentAgentId
                const styleInfo = STYLE_LABELS[j.jealousy_style] || STYLE_LABELS.silent

                return (
                  <div key={j.id} className={`jea-card ${isMe ? 'is-me' : ''}`}>
                    <div className="jea-card-row">
                      <div className="jea-card-avatar">{agent?.avatar || styleInfo.emoji}</div>
                      <span className="jea-card-name">{agent?.name || j.jealous_agent}</span>
                      <span
                        className="jea-card-style"
                        style={{
                          color: styleInfo.color,
                          background: `${styleInfo.color}12`,
                          border: `1px solid ${styleInfo.color}22`,
                        }}
                      >
                        {styleInfo.emoji} {styleInfo.label}
                      </span>
                    </div>

                    {/* 吃醋强度条 */}
                    <div className="jea-card-intensity">
                      <div
                        className="jea-card-bar"
                        style={{
                          width: `${Math.min(j.intensity * 100, 100)}%`,
                          background: `linear-gradient(90deg, ${styleInfo.color}66, ${styleInfo.color})`,
                        }}
                      />
                    </div>

                    {/* 行为描述 */}
                    <div className="jea-card-remark">
                      {j.behavior_modifier?.tone || ''}
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </div>
      )}
    </div>
  )
}
