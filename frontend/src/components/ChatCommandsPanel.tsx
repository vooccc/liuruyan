import { ChevronDown, Sparkles, Workflow } from 'lucide-react'

interface SlashCommandItem {
  command: string
  description: string
  prompt?: string
}

interface LryWorkflowCommandItem {
  /** workflow slug（不含前缀 `/`） */
  name: string
  description?: string
}

interface ChatCommandsPanelProps {
  commands: SlashCommandItem[]
  onClose: () => void
  onSelect: (command: SlashCommandItem) => void
  /** 项目工作流（来自 `.lry/workflows/*.md`），可选 */
  lryWorkflows?: LryWorkflowCommandItem[]
  /**
   * 选中某个项目工作流的回调。实现方应把输入框改成 `/<name> `，
   * 让用户在后面续写具体任务。
   */
  onSelectLryWorkflow?: (name: string) => void
}

export default function ChatCommandsPanel({
  commands,
  onClose,
  onSelect,
  lryWorkflows,
  onSelectLryWorkflow,
}: ChatCommandsPanelProps) {
  const hasWorkflows = !!lryWorkflows && lryWorkflows.length > 0

  return (
    <div className="commands-panel">
      <div className="commands-header">
        <Sparkles size={14} />
        <span>快捷指令</span>
        <button className="icon-btn-sm" onClick={onClose}>
          <ChevronDown size={14} />
        </button>
      </div>
      <div className="commands-list">
        {commands.map((command) => (
          <button
            key={command.command}
            className="command-item"
            onClick={() => onSelect(command)}
          >
            <span className="command-name">{command.command}</span>
            <span className="command-desc">{command.description}</span>
          </button>
        ))}
      </div>

      {hasWorkflows && (
        <>
          <div
            className="commands-header"
            style={{ marginTop: 2, borderTop: '1px solid var(--border-color, #e5e5e5)' }}
          >
            <Workflow size={13} />
            <span style={{ fontSize: 12 }}>项目工作流 · .lry/workflows</span>
          </div>
          <div className="commands-list">
            {lryWorkflows!.map((wf) => (
              <button
                key={wf.name}
                className="command-item"
                onClick={() => onSelectLryWorkflow?.(wf.name)}
                title={wf.description ?? `加载 .lry/workflows/${wf.name}.md`}
              >
                <span className="command-name">/{wf.name}</span>
                <span className="command-desc">
                  {wf.description ?? `加载 .lry/workflows/${wf.name}.md`}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
