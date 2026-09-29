export type AgentTaskStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface AgentTaskActivity {
  id: string
  type: 'thinking' | 'tool_call' | 'tool_result'
  text: string
  at: string
  toolName?: string
}

export interface AgentTaskStat {
  taskId: string
  title?: string
  status: AgentTaskStatus
  startedAt: string
  updatedAt: string
  completedAt?: string
  thinkingChars: number
  toolsCalled: number
  toolsCompleted: number
  recentActivity?: AgentTaskActivity[]
  terminalKind?: string
  resultText?: string
  errorMessage?: string
}
