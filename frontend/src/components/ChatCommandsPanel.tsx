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
  /** 当前输入框内容，用于指令实时过滤 */
  filter?: string
}

export default function ChatCommandsPanel({
  commands,
  onClose,
  onSelect,
  lryWorkflows,
  onSelectLryWorkflow,
  filter,
}: ChatCommandsPanelProps) {
  const filterKey = (filter?.startsWith('/') ? filter.slice(1) : filter ?? '').trim().toLowerCase()
  const filteredCommands = filterKey
    ? commands.filter(
        (c) =>
          c.command.toLowerCase().includes(filterKey) ||
          c.description.toLowerCase().includes(filterKey)
      )
    : commands

  const filteredWorkflows = filterKey && lryWorkflows
    ? lryWorkflows.filter(
        (wf) =>
          wf.name.toLowerCase().includes(filterKey) ||
          (wf.description?.toLowerCase() ?? '').includes(filterKey)
      )
    : (lryWorkflows ?? [])

  const hasWorkflows = filteredWorkflows.length > 0
  const hasMatches = filteredCommands.length > 0 || hasWorkflows

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
        {!hasMatches && (
          <div style={{ padding: '8px 12px', fontSize: 12, color: 'var(--text-tertiary, #999)' }}>
            未找到匹配的快捷指令
          </div>
        )}
        {filteredCommands.map((command) => (
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
            {filteredWorkflows.map((wf) => (
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
