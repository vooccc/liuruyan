import { API_HOST_PORT } from '../api/base'

export interface ChatSlashCommand {
  command: string
  description: string
  prompt: string
}

export const CHAT_SLASH_COMMANDS: ChatSlashCommand[] = [
  { command: '/翻译', description: '翻译文本', prompt: '请帮我翻译以下内容：' },
  { command: '/\u603b\u7ed3', description: '\u603b\u7ed3\u5185\u5bb9', prompt: '\u8bf7\u5e2e\u6211\u603b\u7ed3\u4ee5\u4e0b\u5185\u5bb9\uff1a' },
  { command: '/\u5199\u4ee3\u7801', description: '\u7f16\u5199\u4ee3\u7801', prompt: '\u8bf7\u5e2e\u6211\u7f16\u5199\u4ee5\u4e0b\u4ee3\u7801\uff1a' },
  { command: '/\u89e3\u91ca', description: '\u89e3\u91ca\u6982\u5ff5', prompt: '\u8bf7\u89e3\u91ca\u4e00\u4e0b\u8fd9\u4e2a\u6982\u5ff5\uff1a' },
  { command: '/\u6da6\u8272', description: '\u6da6\u8272\u6587\u672c', prompt: '\u8bf7\u5e2e\u6211\u6da6\u8272\u4ee5\u4e0b\u6587\u672c\uff0c\u4f7f\u5176\u66f4\u52a0\u4f18\u7f8e\u6d41\u7545\uff1a' },
  { command: '/\u5206\u6790', description: '\u6df1\u5ea6\u5206\u6790', prompt: '\u8bf7\u5bf9\u4ee5\u4e0b\u5185\u5bb9\u8fdb\u884c\u6df1\u5ea6\u5206\u6790\uff1a' },
  { command: '/goal', description: '目标驱动模式', prompt: '/goal ' },
]

export const CHAT_UI_TEXT = {
  attachmentTitle: '\u6dfb\u52a0\u9644\u4ef6',
  mentionTitle: '\u63d0\u53ca\u4e0a\u4e0b\u6587 (@)',
  backendCheckMessage: `\u8bf7\u786e\u8ba4\u540e\u7aef\u670d\u52a1\u5df2\u542f\u52a8\uff0c\u5e76\u68c0\u67e5 ${API_HOST_PORT} \u662f\u5426\u53ef\u8bbf\u95ee\u3002`,
  builtinModelsHeader: '\u5185\u7f6e\u6a21\u578b',
  clearAction: '\u6e05\u7a7a',
  clearDialogMessage: '\u6e05\u7a7a\u540e\u5f53\u524d\u4f1a\u8bdd\u7684\u6240\u6709\u6d88\u606f\u90fd\u4f1a\u88ab\u5220\u9664\uff0c\u786e\u5b9a\u7ee7\u7eed\u5417\uff1f',
  clearDialogTitle: '\u6e05\u7a7a\u5bf9\u8bdd',
  clearLabel: '\u6e05\u7a7a',
  clearTitle: '\u6e05\u7a7a\u5bf9\u8bdd',
  configureCustomModel: '\u914d\u7f6e\u81ea\u5b9a\u4e49\u6a21\u578b',
  corridorTitle: '\u8d70\u5eca\u4f20\u8bdd',
  customModelsHeader: '\u81ea\u5b9a\u4e49\u6a21\u578b',
  defaultAssistantName: '\u52a9\u624b',
  defaultBuiltinModelIcon: '\u2726',
  defaultCustomModelIcon: '\u25cc',
  defaultModelName: 'GLM-5v-Turbo',
  inputPlaceholder: '\u7ee7\u7eed\u5728\u4e3b\u804a\u5929\u5de5\u4f5c\u53f0\u8f93\u5165\u6d88\u606f...',
  modeLabel: 'Craft',
  modeSwitchTitle: '\u5207\u6362\u8f93\u5165\u6a21\u5f0f',
  offlineBanner: `\u540e\u7aef\u670d\u52a1\u79bb\u7ebf\uff08${API_HOST_PORT}\uff09\uff0c\u8bf7\u68c0\u67e5\u540e\u7aef\u662f\u5426\u5df2\u542f\u52a8\u5e76\u4fdd\u6301\u8fd0\u884c\u3002`,
  providerBadgeAnthropic: 'Anthropic',
  requestFailed: '\u8bf7\u6c42\u5931\u8d25',
  requestPrefix: '\u9519\u8bef\uff1a',
  sendTitle: '\u53d1\u9001\u6d88\u606f',
  skillsLabel: 'Skills',
  skillsTitle: '\u6280\u80fd\u7ba1\u7406',
  stopTitle: '\u505c\u6b62\u751f\u6210',
} as const

export function formatChatSessionTitle(agentId: string, agentName: string) {
  return agentId === 'default'
    ? '\u65b0\u5bf9\u8bdd'
    : `\u4e0e${agentName}\u7684\u5bf9\u8bdd`
}

export function formatChatStreamErrorMessage(error?: string) {
  return `\n\n${CHAT_UI_TEXT.requestPrefix}${error || CHAT_UI_TEXT.requestFailed}`
}

export function formatChatRequestErrorMessage(error: string) {
  return `${CHAT_UI_TEXT.requestPrefix}${error}\n\n${CHAT_UI_TEXT.backendCheckMessage}`
}
