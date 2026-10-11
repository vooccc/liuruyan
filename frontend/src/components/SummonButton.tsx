import { useState, useEffect, useRef } from 'react'
import { Sparkles, Send, Inbox, Reply, MessageCircle, ArrowRight } from 'lucide-react'
import { consultAgentStream, getCrossMessages, sendCorridorMessage, CrossMessage, markCrossMessageRead, markAllCrossMessagesRead, type ConsultStreamEvent } from '../api/chat'
import AgentAvatar from './AgentAvatar'
import { getAvatarSrc } from './avatarUtils'
import { startPolling } from '../lib/pollingLoop'

interface Agent {
  id: string
  name: string
  avatar?: string
  avatar_url?: string
}

interface SummonButtonProps {
  currentAgentId: string
  currentSessionId: string
  agents: Agent[]
  onConsultEvent?: (payload: {
    streamId: string
    event: ConsultStreamEvent
  }) => void
  /** 跳转到议事厅（供“要多人深谈”入口使用） */
  onGoRoundtable?: () => void
}

const EMOTION_COLORS: Record<string, string> = {
  '好奇': '#8b5cf6', '警惕': '#ef4444', '关心': '#3b82f6',
  '吃醋': '#ec4899', '挑衅': '#f59e0b', '合作': '#10b981',
  '平淡': '#6b7280', '惊喜': '#f59e0b', '不满': '#ef4444',
  '信任': '#10b981', '敌意': '#dc2626',
}
const MSG_TYPE_LABELS: Record<string, string> = {
  inform: '告知', request: '请求', react: '反应',
  gossip: '闲话', summon: '召唤', task: '任务', report: '汇报',
}

/** 获取中文名：先按 ID 匹配 agents，fallback 到首字母大写 */
function getAgentName(agentId: string | undefined, agents: Agent[]): string {
  if (!agentId) return '?'
  const found = agents.find(a => a.id === agentId)
  if (found) return found.name
  // fallback: 首字母大写
  return agentId.charAt(0).toUpperCase() + agentId.slice(1)
}

export function SummonButton({ currentAgentId, currentSessionId, agents, onConsultEvent, onGoRoundtable }: SummonButtonProps) {
  const [showPanel, setShowPanel] = useState(false)
  const [showInbox, setShowInbox] = useState(false)
  const [summonReason, setSummonReason] = useState('')
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const [summoning, setSummoning] = useState(false)
  const [inbox, setInbox] = useState<CrossMessage[]>([])
  const [corridorReply, setCorridorReply] = useState('')
  const [corridorReplyTo, setCorridorReplyTo] = useState<CrossMessage | null>(null)
  const [corridorSending, setCorridorSending] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  const otherAgents = agents.filter(a => a.id !== currentAgentId)
  const unreadCount = inbox.filter(msg => !msg.read).length
  const canSummon = Boolean(currentAgentId && currentSessionId)

  const loadInbox = async () => {
    if (!currentAgentId) return
    try { setInbox((await getCrossMessages(currentAgentId, true)).messages || []) } catch {}
  }
  useEffect(() => startPolling(loadInbox, 15000), [currentAgentId])

  useEffect(() => {
    if (!showPanel && !showInbox) return
    const handleMouseDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) {
        setShowPanel(false)
        setShowInbox(false)
      }
    }
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setShowPanel(false)
        setShowInbox(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handleMouseDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [showPanel, showInbox])

  const handleConsult = async () => {
    if (!selectedAgent) {
      console.warn('[SummonButton] 未选择角色')
      return
    }
    if (!currentSessionId) {
      console.warn('[SummonButton] currentSessionId 为空，无法征求意见')
      return
    }
    const targetAgent = selectedAgent
    const reason = summonReason
    setSummoning(true)
    setShowPanel(false)
    const streamId = `consult-${Date.now()}-${targetAgent}`
    try {
      for await (const event of consultAgentStream(currentAgentId, currentSessionId, targetAgent, reason)) {
        onConsultEvent?.({ streamId, event })

        if (event.type === 'consult_end') {
          setSummonReason('')
          setSelectedAgent(null)
        }
      }
    } catch (e: any) {
      console.error('[SummonButton] 征求意见失败:', e)
      onConsultEvent?.({
        streamId,
        event: {
          type: 'error',
          session_id: currentSessionId,
          agent_id: targetAgent,
          error: e?.message || '征求意见失败',
        },
      })
    } finally {
      setSummoning(false)
    }
  }

  const handleCorridorReply = async () => {
    if (!corridorReplyTo || !corridorReply.trim()) return
    setCorridorSending(true)
    try {
      await sendCorridorMessage({
        from_agent: currentAgentId,
        to_agent: corridorReplyTo.from_agent,
        content: corridorReply,
        msg_type: 'react',
        emotion_tone: '友好',
      })
      await markCrossMessageRead(corridorReplyTo.id)
      setCorridorReply('')
      setCorridorReplyTo(null)
      loadInbox()
    } catch (e) {
      console.error('[SummonButton] 走廊回复失败:', e)
    } finally { setCorridorSending(false) }
  }

  return (
    <div style={{ position: 'relative' }} ref={ref}>
      <style>{`
        .sum-shell {
          display: inline-flex; align-items: center; gap: 4px;
        }

        /* ========== 按钮 ========== */
        .sum-btn {
          display: inline-flex; align-items: center; gap: 4px;
          padding: 4px 9px; border-radius: 16px; border: none; cursor: pointer;
          font-size: 11px; font-weight: 600; white-space: nowrap;
          background: transparent; color: var(--text-secondary);
          transition: all 0.2s ease; font-family: inherit; line-height: 1;
        }
        .sum-btn:hover { background: rgba(139,92,246,0.08); color: #8b5cf6; }
        .sum-btn.active { background: linear-gradient(135deg, #8b5cf6, #a78bfa); color: #fff; box-shadow: 0 2px 8px rgba(139,92,246,0.35); }

        .sum-inbox-dot {
          display: inline-flex; align-items: center; justify-content: center;
          width: 18px; height: 18px; border-radius: 50%; border: none; cursor: pointer;
          background: transparent; color: var(--text-secondary); padding: 0;
          transition: all 0.2s ease; position: relative;
        }
        .sum-inbox-dot:hover { background: rgba(59,130,246,0.08); color: #3b82f6; }
        .sum-badge {
          position: absolute; top: -3px; right: -4px;
          min-width: 15px; height: 15px; border-radius: 8px;
          background: linear-gradient(135deg, #f43f5e, #ec4899); color: #fff;
          font-size: 9px; font-weight: 700; letter-spacing: -0.02em;
          display: flex; align-items: center; justify-content: center; padding: 0 3px;
          box-shadow: 0 1px 4px rgba(236,72,153,0.35);
        }

        /* ========== 面板 ========== */
        .sum-panel {
          position: absolute; top: calc(100% + 6px); left: 50%;
          transform: translateX(-50%); border-radius: 16px;
          background: var(--bg-card);
          border: 1px solid rgba(128,128,128,0.14);
          box-shadow: 0 8px 32px rgba(0,0,0,0.15), 0 0 0 1px rgba(255,255,255,0.05) inset;
          z-index: 9999; animation: sumIn 0.22s cubic-bezier(.16,1,.3,1);
        }
        @keyframes sumIn {
          from { opacity: 0; transform: translateX(-50%) translateY(-4px) scale(0.97); }
          to   { opacity: 1; transform: translateX(-50%) translateY(6px) scale(1); }
        }

        .sum-header { padding: 14px 18px 11px; border-bottom: 1px solid var(--border-light); }
        .sum-title { font-size: 14px; font-weight: 700; color: var(--text-color); margin-bottom: 2px; }
        .sum-subtitle { font-size: 11px; color: var(--text-secondary); opacity: 0.65; }

        /* 角色列表 */
        .sum-agent-list { padding: 5px 8px; max-height: 180px; overflow-y: auto; }
        .sum-agent-item {
          display: flex; align-items: center; gap: 8px;
          width: 100%; padding: 7px 10px; border-radius: 10px;
          border: 1px solid transparent; cursor: pointer;
          text-align: left; font-size: 12px; transition: all 0.12s ease; font-family: inherit;
          background: transparent; color: var(--text-color);
        }
        .sum-agent-item:not(.sel):hover { background: rgba(139,92,246,0.06); border-color: rgba(139,92,246,0.1); }
        .sum-agent-item.sel { background: rgba(139,92,246,0.1); border-color: rgba(139,92,246,0.25); color: var(--accent-purple); font-weight: 600; }
        .sum-agent-avatar {
          display: inline-flex; align-items: center; justify-content: center;
          width: 24px; height: 24px; flex: 0 0 24px; border-radius: 50%; overflow: hidden;
          font-size: 12px; line-height: 1; font-weight: 600;
        }
        .sum-agent-avatar-wrap { display: inline-flex; width: 100%; height: 100%; }
        .sum-agent-avatar-img { display: block; width: 100%; height: 100%; border-radius: 50%; object-fit: cover; }
        .sum-agent-avatar-fallback { display: inline-flex; align-items: center; justify-content: center; width: 100%; height: 100%; }
        .sum-sender-avatar { width: 20px; height: 20px; flex-basis: 20px; font-size: 10px; }

        /* 操作区 */
        .sum-actions { padding: 12px 16px; border-top: 1px solid var(--border-light); }
        .sum-input {
          width: 100%; padding: 8px 11px; border-radius: 10px;
          border: 1px solid var(--border-light); outline: none;
          font-size: 12px; box-sizing: border-box; margin-bottom: 8px;
          background: var(--input-bg); color: var(--text-color);
          font-family: inherit; transition: border-color 0.15s;
        }
        .sum-input:focus { border-color: rgba(139,92,246,0.35); }
        .sum-panel-tip {
          margin-top: 10px; padding: 8px 10px; border-radius: 10px;
          background: rgba(59,130,246,0.05); border: 1px solid rgba(59,130,246,0.08);
          font-size: 10.5px; line-height: 1.45; color: var(--text-secondary);
        }
        .sum-panel-tip b { color: var(--text-color); font-weight: 600; }
        .sum-disabled-note {
          margin: 0 16px 14px; padding: 10px 12px; border-radius: 12px;
          background: rgba(148,163,184,0.08); border: 1px solid rgba(148,163,184,0.12);
          font-size: 11px; line-height: 1.45; color: var(--text-secondary);
        }
        .sum-disabled-note b { color: var(--text-color); }
        .sum-send {
          width: 100%; padding: 8px; border-radius: 10px; border: none;
          background: linear-gradient(135deg, #8b5cf6, #a78bfa); color: #fff;
          cursor: pointer; font-size: 12px; font-weight: 600;
          display: flex; align-items: center; justify-content: center; gap: 5px;
          transition: all 0.15s; font-family: inherit;
          box-shadow: 0 2px 8px rgba(139,92,246,0.25);
        }
        .sum-send:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(139,92,246,0.35); }
        .sum-send:disabled { opacity: 0.45; cursor: not-allowed; }
        /* 去议事厅跳转链接 */
        .sum-goto-roundtable {
          margin-top: 8px; padding: 8px 10px; border-radius: 10px;
          background: linear-gradient(135deg, rgba(96,165,250,0.08), rgba(168,139,250,0.06));
          border: 1px dashed rgba(96,165,250,0.25);
          display: flex; align-items: center; gap: 6px;
          font-size: 11px; color: var(--text-secondary); cursor: pointer;
          transition: all 0.15s; font-family: inherit; width: 100%; text-align: left;
        }
        .sum-goto-roundtable:hover {
          background: linear-gradient(135deg, rgba(96,165,250,0.14), rgba(168,139,250,0.10));
          color: var(--text-color); border-color: rgba(96,165,250,0.4);
        }
        .sum-goto-roundtable b { color: var(--text-color); font-weight: 600; }

        /* 收件箱 */
        .sum-inbox-panel { width: 310px; max-height: 370px; display: flex; flex-direction: column; }
        .sum-inbox-head {
          display: flex; align-items: center; justify-content: space-between;
          padding: 14px 18px 11px; border-bottom: 1px solid var(--border-light);
        }
        .sum-read-all-btn {
          font-size: 11px; color: #a78bfa; cursor: pointer; border: none;
          background: none; padding: 0; font-family: inherit;
          transition: color 0.15s;
        }
        .sum-read-all-btn:hover { color: #c4b5fd; }
        .sum-msg-body { flex: 1; overflow-y: auto; }
        .sum-msg-row {
          padding: 9px 14px; border-bottom: 1px solid rgba(128,128,128,0.07);
          cursor: pointer; transition: all 0.12s;
        }
        .sum-msg-row:hover { background: rgba(139,92,246,0.03); }
        .sum-meta { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
        .sum-sender { font-size: 12px; font-weight: 500; color: var(--text-color); }
        .sum-emo-tag {
          font-size: 10px; padding: 1px 6px; border-radius: 8px;
          border: 1px solid rgba(255,255,255,0.15); font-weight: 500;
        }
        .sum-type-label { font-size: 10px; color: var(--text-secondary); margin-left: auto; }
        .sum-text {
          font-size: 12px; color: var(--text-secondary); line-height: 1.48; margin: 0;
          display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
        }
        .sum-empty { text-align: center; padding: 30px 0; color: var(--text-secondary); font-size: 12px; opacity: 0.55; }

        /* 滚动条 */
        .sum-agent-list::-webkit-scrollbar, .sum-msg-body::-webkit-scrollbar { width: 3px; }
        .sum-agent-list::-webkit-scrollbar-track, .sum-msg-body::-webkit-scrollbar-track { background: transparent; }
        .sum-agent-list::-webkit-scrollbar-thumb, .sum-msg-body::-webkit-scrollbar-thumb { background: rgba(128,128,128,0.15); border-radius: 2px; }
      `}</style>

      <div className="sum-shell">
        <button className={`sum-btn ${showPanel ? 'active' : ''}`} onClick={() => { setShowPanel(!showPanel); setShowInbox(false) }} title="召唤角色">
          <Sparkles size={12} fill={showPanel ? '#fff' : 'none'} strokeWidth={showPanel ? 0 : 1.8} />
          <span>召唤</span>
        </button>

        <button className="sum-inbox-dot"
          style={unreadCount === 0 ? { opacity: 0.4 } : {}}
          onClick={() => { setShowInbox(!showInbox); setShowPanel(false) }}
          title={`收件箱${unreadCount > 0 ? ` ${unreadCount}条未读` : ''}`}>
          <Inbox size={11} />
          {unreadCount > 0 && <span className="sum-badge">{unreadCount > 9 ? '9+' : unreadCount}</span>}
        </button>
      </div>

      {/* ====== 召唤面板 ====== */}
      {showPanel && (
        <div className="sum-panel" style={{ width: '296px' }}>
          <div className="sum-header">
            <div className="sum-title">✨ 召唤伙伴说一句</div>
            <div className="sum-subtitle">征求意见，他会回流一条建议到当前主聊</div>
          </div>

          <div className="sum-agent-list">
            {otherAgents.map(agent => (
              <button key={agent.id} className={`sum-agent-item ${selectedAgent === agent.id ? 'sel' : ''}`}
                onClick={() => setSelectedAgent(selectedAgent === agent.id ? null : agent.id)}>
                <span className="sum-agent-avatar">
                  <AgentAvatar
                    name={agent.name}
                    src={getAvatarSrc(agent)}
                    wrapperClassName="sum-agent-avatar-wrap"
                    imageClassName="sum-agent-avatar-img"
                    plainClassName="sum-agent-avatar-fallback"
                  />
                </span>
                <span>{agent.name}</span>
              </button>
            ))}
            {otherAgents.length === 0 && <div className="sum-empty" style={{ padding: '16px 0' }}>没有其他角色</div>}
          </div>

          {!canSummon && (
            <div className="sum-disabled-note">
              <b>先进入一个主聊天会话</b>，再从这里发起召唤。
            </div>
          )}

          {selectedAgent && canSummon && (
            <div className="sum-actions">
              <input type="text" value={summonReason} onChange={e => setSummonReason(e.target.value)}
                placeholder="这次想让 TA 帮什么（可选）" onKeyDown={e => e.key === 'Enter' && handleConsult()} className="sum-input"/>
              <button className="sum-send" onClick={handleConsult} disabled={summoning}>
                <MessageCircle size={13} />{summoning ? '处理中...' : '征求意见'}
              </button>
              <div className="sum-panel-tip">
                <b>征求意见</b>会把对方的一次性建议直接插回当前主聊天。
              </div>
              {onGoRoundtable && (
                <button className="sum-goto-roundtable"
                  onClick={() => { setShowPanel(false); onGoRoundtable() }}>
                  <span style={{ flex: 1 }}>
                    要多人深谈？去<b>议事厅</b>发起协作
                  </span>
                  <ArrowRight size={12} />
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* ====== 收件箱面板（走廊模式） ====== */}
      {showInbox && (
        <div className="sum-panel sum-inbox-panel">
          <div className="sum-inbox-head">
            <div>
              <span className="sum-title">📬 走廊收件箱</span>
              <div className="sum-subtitle">这里只显示传话往来，不承接召唤反馈</div>
            </div>
            {unreadCount > 0 && <button className="sum-read-all-btn" onClick={async () => { try { await markAllCrossMessagesRead(currentAgentId); loadInbox() } catch {} }}>全部已读</button>}
          </div>

          <div className="sum-msg-body">
            {inbox.length === 0 ? (
              <div className="sum-empty">暂无消息</div>
            ) : inbox.map(msg => {
              const sender = agents.find(a => a.id === msg.from_agent)
              const ec = EMOTION_COLORS[msg.emotion_tone] || '#6b7280'
              const isReplying = corridorReplyTo?.id === msg.id
              return (
                <div key={msg.id} className="sum-msg-row" style={isReplying ? { background: 'rgba(139,92,246,0.06)', borderColor: 'rgba(139,92,246,0.15)' } : {}}>
                  <div className="sum-meta">
                    <span className="sum-agent-avatar sum-sender-avatar">
                      <AgentAvatar
                        name={sender?.name}
                        src={getAvatarSrc(sender)}
                        avatar={sender?.avatar}
                        fallbackAvatar="💬"
                        wrapperClassName="sum-agent-avatar-wrap"
                        imageClassName="sum-agent-avatar-img"
                        plainClassName="sum-agent-avatar-fallback"
                      />
                    </span>
                    <span className="sum-sender">{getAgentName(msg.from_agent, agents)}</span>
                    <span className="sum-emo-tag" style={{ background: `${ec}15`, color: ec }}>{msg.emotion_tone}</span>
                    <span className="sum-type-label">{MSG_TYPE_LABELS[msg.msg_type] || msg.msg_type}</span>
                    {isReplying ? null : (
                      <button className="sum-read-all-btn" style={{ marginLeft: 'auto' }}
                        onClick={e => { e.stopPropagation(); setCorridorReplyTo(msg); markCrossMessageRead(msg.id); loadInbox() }}
                        title="回复">
                        <Reply size={11} />
                      </button>
                    )}
                  </div>
                  <p className="sum-text">{msg.content}</p>
                  {isReplying && (
                    <div style={{ display: 'flex', gap: '6px', marginTop: '6px' }}>
                      <input type="text" value={corridorReply} onChange={e => setCorridorReply(e.target.value)}
                        placeholder={`回复${getAgentName(msg.from_agent, agents)}...`}
                        onKeyDown={e => e.key === 'Enter' && handleCorridorReply()}
                        className="sum-input" style={{ flex: 1, marginBottom: 0 }} autoFocus />
                      <button className="sum-send" onClick={handleCorridorReply} disabled={corridorSending || !corridorReply.trim()}
                        style={{ width: 'auto', padding: '6px 12px', marginBottom: 0 }}>
                        <Send size={11} />
                      </button>
                      <button className="sum-read-all-btn" onClick={() => { setCorridorReplyTo(null); setCorridorReply('') }}
                        style={{ whiteSpace: 'nowrap' }}>取消</button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
