import {
  Code2,
  MessageSquareText,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export type ChatInputMode = 'code' | 'ask'

export interface ChatInputModeOption {
  key: ChatInputMode
  label: string
  description: string
  accent: string
  icon: LucideIcon
}

export const CHAT_INPUT_MODE_OPTIONS: ChatInputModeOption[] = [
  {
    key: 'code',
    label: 'Code',
    description: '默认编码模式，可直接执行与修改',
    accent: 'code',
    icon: Code2,
  },
  {
    key: 'ask',
    label: 'Ask',
    description: '只回答与分析，不主动进入后台执行',
    accent: 'ask',
    icon: MessageSquareText,
  },
]

export function getChatInputModeOption(mode: ChatInputMode) {
  return CHAT_INPUT_MODE_OPTIONS.find((option) => option.key === mode) || CHAT_INPUT_MODE_OPTIONS[0]
}

export function getChatInputPlaceholder(mode: ChatInputMode) {
  switch (mode) {
    case 'ask':
      return '先提问、排查或澄清目标，这一模式会保持主对话直答...'
    case 'code':
    default:
      return '继续在主聊天工作台输入消息...'
  }
}
