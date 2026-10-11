export interface CollabSceneConfig {
  color: string
  emoji: string
  label: string
  arc: string
}

export interface CollabPhaseStep {
  key: string
  label: string
  icon: string
}

export const COLLAB_SCENE_CONFIGS: Record<string, CollabSceneConfig> = {
  free: { color: '#9ca3af', emoji: '💬', label: '自由', arc: '无预设剧本，纯多人协作讨论' },
  encounter: { color: '#a78bfa', emoji: '✨', label: '邂逅', arc: '偶然相遇 -> 关系微变 -> 各自离开' },
  conflict: { color: '#f87171', emoji: '⚡', label: '对峙', arc: '立场碰撞 -> 张力升高 -> 决裂或和解' },
  cooperation: { color: '#4ade80', emoji: '🤝', label: '协力', arc: '明确分工 -> 遇到障碍 -> 协作突破' },
  gathering: { color: '#60a5fa', emoji: '🎀', label: '群聚', arc: '众人齐聚 -> 化学反应 -> 意外收获' },
  crisis: { color: '#fb923c', emoji: '🚨', label: '危机', arc: '危机降临 -> 考验关系 -> 转机或崩裂' },
}

export const COLLAB_PHASE_STEPS: CollabPhaseStep[] = [
  { key: 'opening', label: '开场', icon: '🌟' },
  { key: 'rising', label: '升温', icon: '🔥' },
  { key: 'climax', label: '高潮', icon: '⚡' },
  { key: 'resolution', label: '收束', icon: '🪄' },
]

export const COLLAB_UI_TEXT = {
  userName: '我',
  userAvatar: '我',
  agentName: '角色',
  agentAvatar: '✨',
  systemName: '系统',
  systemAvatar: 'ⓘ',
  loadingHistory: '正在加载协作历史...',
  emptySceneTitle: '场景协作已就绪',
  emptyTip: '在下方输入框发送消息开始协作',
  sendLabel: '发送',
  stopLabel: '停止',
  dismissTitle: '解散并删除当前协作会话',
  dismissLabel: '解散协作',
  closeTitle: '关闭',
  closeLabel: '关闭',
  noReplyMessage: 'ⓘ 未收到角色回复，可能后端未启动或当前协作已经结束。',
  requestFailedPrefix: 'ⓘ 请求失败：',
  unknownError: '未知错误',
  sceneModeHint: '场景阶段会影响角色回复节奏与协作氛围',
  /** scene_update（is_active=false）：时间线里的散会系统行 */
  sceneEndedMessage: 'ⓘ 本场议事已散会',
  /** header 副标题的散会后缀 */
  sceneEndedLabel: '已散会',
  /** 散会后输入框 placeholder */
  sceneEndedPlaceholder: '本场已散会，无法继续发言',
} as const

export function formatCollabEmptyDescription(agentNames: string[]) {
  return `输入消息后，${agentNames.join('、')} 会依次回应。`
}

/** scene_update 阶段推进时的时间线系统行文案 */
export function formatCollabPhaseChangeNotice(phaseKey: string) {
  const step = COLLAB_PHASE_STEPS.find((item) => item.key === phaseKey)
  return step ? `ⓘ 阶段推进 · ${step.icon} ${step.label}` : `ⓘ 阶段推进 · ${phaseKey}`
}

export function formatCollabInputPlaceholder(phaseLabel: string) {
  return `对 ${phaseLabel} 阶段说点什么...`
}

export function formatCollabRequestErrorMessage(error?: string) {
  return `${COLLAB_UI_TEXT.requestFailedPrefix}${error || COLLAB_UI_TEXT.unknownError}`
}
