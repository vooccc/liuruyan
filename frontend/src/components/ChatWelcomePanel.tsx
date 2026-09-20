import { Sparkles } from 'lucide-react'
import type { Agent } from '../api/chat'
import AgentAvatar from './AgentAvatar'

interface ChatWelcomePanelProps {
  currentAgent: Agent | null
  avatarSrc: string | null
  quickCommands: Array<{ command: string; description: string }>
  onQuickCommand: (command: string) => void
}

export default function ChatWelcomePanel({
  currentAgent,
  avatarSrc,
  quickCommands,
  onQuickCommand,
}: ChatWelcomePanelProps) {
  return (
    <div className="welcome-message">
      <div className="welcome-avatar">
        <AgentAvatar
          name={currentAgent?.name}
          src={avatarSrc}
          imageClassName="welcome-avatar-img"
          plainClassName="welcome-avatar-fallback"
        />
      </div>
      <h2>老板，我在这里</h2>
      <p className="welcome-subtitle">{currentAgent?.description || '有什么可以帮您的？'}</p>
      <div className="quick-actions">
        {quickCommands.map((command) => (
          <button
            key={command.command}
            className="quick-action-btn"
            onClick={() => onQuickCommand(command.command)}
          >
            <Sparkles size={14} />
            <span>{command.command.replace('/', '')}</span>
            <span className="quick-desc">{command.description}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
