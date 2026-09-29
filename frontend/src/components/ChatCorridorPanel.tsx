import type { Agent } from '../api/chat'

interface ChatCorridorPanelProps {
  currentAgentId?: string | null
  currentAgentName?: string | null
  agents: Agent[]
  corridorTarget: string | null
  corridorContent: string
  corridorSending: boolean
  onTargetChange: (target: string | null) => void
  onContentChange: (content: string) => void
  onCancel: () => void
  onSend: () => void
}

export default function ChatCorridorPanel({
  currentAgentId,
  currentAgentName,
  agents,
  corridorTarget,
  corridorContent,
  corridorSending,
  onTargetChange,
  onContentChange,
  onCancel,
  onSend,
}: ChatCorridorPanelProps) {
  return (
    <div className="corridor-panel">
      <div className="corridor-panel-title">
        轻协作侧轨 · 走廊传话
      </div>
      <div className="corridor-panel-help">
        由 {currentAgentName || currentAgentId} 异步转述，当前主聊天不会被切走。
      </div>
      <select
        className="corridor-panel-select"
        value={corridorTarget || ''}
        onChange={(event) => onTargetChange(event.target.value || null)}
      >
        <option value="">选择要传话的角色...</option>
        {agents
          .filter((agent) => agent.id !== currentAgentId)
          .map((agent) => (
            <option key={agent.id} value={agent.id}>{agent.name || agent.id}</option>
          ))}
      </select>
      <textarea
        className="corridor-panel-textarea"
        value={corridorContent}
        onChange={(event) => onContentChange(event.target.value)}
        placeholder="要传话的内容..."
        rows={3}
      />
      <div className="corridor-panel-actions">
        <button
          type="button"
          className="corridor-panel-btn secondary"
          onClick={onCancel}
        >
          取消
        </button>
        <button
          type="button"
          className={`corridor-panel-btn primary ${corridorTarget && corridorContent.trim() ? 'is-ready' : ''}`}
          disabled={!corridorTarget || !corridorContent.trim() || corridorSending}
          onClick={onSend}
        >
          {corridorSending ? '传话中...' : '传话'}
        </button>
      </div>
    </div>
  )
}
