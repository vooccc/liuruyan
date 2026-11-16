/**
 * voice 孤岛模块出口。对外只暴露这一个组件——
 * 接线方（ChatInputBottomToolbar）换掉 import + JSX 标签共 2 处即可接入/撤除。
 */
export { default as VoiceInputSlot } from './VoiceInputSlot'
export { default as VoiceStreamButton } from './VoiceStreamButton'
export { default as VoiceStreamSettingsPane } from './VoiceStreamSettingsPane'
export { useVoiceStream } from './useVoiceStream'
export { VoiceStreamClient } from './client'
export * from './types'
