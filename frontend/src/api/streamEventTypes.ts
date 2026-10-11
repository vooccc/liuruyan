/**
 * 流式 / 协作事件类型常量 + 运行时事件类型守卫（纯类型 + 纯函数层）
 *
 * 从 `chat.ts` 抽出的「流式事件类型枚举 + 类型守卫」，无网络 / 无 store 副作用，
 * 便于单测与复用（P6 chat.ts 拆分）。本模块自包含、零外部依赖；
 * `chat.ts` 顶部 value-import 守卫供内部使用、并 facade 再导出全部公开符号，
 * 所有现有 `from '../api/chat'` 引用不变。
 */

// tool_call_started：P1 体感优化（2026-05-16）。解析层在 tool name 一就位时
// 立即发，args 还在累积；前端按 tool_use_id 配对，先渲染 calling 卡占位，
// 收到完整 tool_call（args 就位）时覆盖更新。详见 chatStreamMessage.ts 分支。
// tool_result_update：ask_user_question 阻塞等待的终态回写（2026-09-29）。
// 用户在卡片提交答案后，后端把该 tool_call 的 tool_result 就地从 awaiting
// 载荷替换成 answered 终态；前端按 tool_use_id 配对刷新对应 segment。
export const STREAM_EVENT_TYPES = ['content', 'thinking', 'tool_call_started', 'tool_call', 'tool_result', 'tool_result_update', 'system_notice', 'error'] as const
export type StreamEventType = typeof STREAM_EVENT_TYPES[number]

export const CONSULT_STREAM_EVENT_TYPES = [
  'consult_start',
  'content',
  'thinking',
  'tool_call',
  'tool_result',
  'system_notice',
  'consult_end',
  'error',
] as const
export type ConsultStreamEventType = typeof CONSULT_STREAM_EVENT_TYPES[number]

// 议事厅内层 runtime 事件注册表。
//
// 议事厅每个角色的回合复用单聊同一条 transport（backend/runtime_protocol.py
// `build_runtime_transport_event`，仅把 content 改名 agent_content），因此本表
// 必须从 STREAM_EVENT_TYPES 派生而不是手抄：新 transport 事件自动对两端生效，
// 杜绝两份白名单各自漂移。此前手抄版漏收 tool_result_update / error /
// tool_call_started，collab 侧入口守卫直接静默丢弃——ask_user 终态回写
//（2026-09-29）就丢在这里。见 .lry/rules/02-sse-event-three-channels.md。
//
// 注意：session_info / agent_start / agent_end / agent_error / scene_update 是
// collab 外层信封事件，不属 runtime 注册表；由 CollabChat 显式分支处理，
// 不经 runtime reducer。
export const COLLAB_RUNTIME_EVENT_TYPES = [
  'agent_content',
  ...STREAM_EVENT_TYPES.filter((t): t is Exclude<StreamEventType, 'content'> => t !== 'content'),
] as const
export type CollabRuntimeEventType = typeof COLLAB_RUNTIME_EVENT_TYPES[number]

export function isRuntimeStreamEventType(value: unknown): value is StreamEventType {
  return typeof value === 'string' && STREAM_EVENT_TYPES.includes(value as StreamEventType)
}

export function isCollabRuntimeEventType(value: unknown): value is CollabRuntimeEventType {
  return typeof value === 'string' && COLLAB_RUNTIME_EVENT_TYPES.includes(value as CollabRuntimeEventType)
}
