/**
 * 主应用 Store（Phase 1A）
 *
 * 设计：handover §3 架构图
 *   - sessions  → 1v1 会话（messages, status, lastEventId, ...）
 *   - rooms     → 协作房间（同上 + participants, speakerTargets, paused）
 *   - agents    → agent 元数据（tick, lastActive, joined）
 *   - panorama  → 全局活动聚合
 *
 * 不变量：
 *   - 所有 mutation 走 actions（avoid 在组件层散写）
 *   - 用 immer 做不可变更新
 *   - 消息以 server 为唯一真值（前端永远不持久化未来本窗口不接 Chat.tsx）
 *
 * 本窗口范围：仅定义 schema + 基础 actions，给下窗口切 Chat.tsx 时填血肉。
 */

import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import type { Message as ChatMessage } from '../api/chat';
// P1.4b（架构重构 2026-05-23）：仅 type-only 引入；编译期被 erase，无运行时依赖循环。
import type { RitualMarker } from '../components/chatExternalHandle';

// ─────── 通用类型 ─────────

export type SessionStatus = 'idle' | 'connecting' | 'streaming' | 'error' | 'closed';

/**
 * 最小消息形态（下窗口切 Chat.tsx 时会扩展为带 segments 的完整结构）。
 * 当前先用通用 record 形态，避免与 Chat.tsx 类型耦合。
 */
export interface StoreMessage {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  /** 业务字段全部走 payload，避免硬编码 schema */
  payload: Record<string, unknown>;
  /** server 分配，前端只读 */
  eventId?: number;
  /** server 时间戳 */
  timestamp?: number;
}

// ─────── Token usage slice（per-session 累计 LLM 消耗）─────────

/**
 * 每次 LLM 调用累加，跨重启保留（后端 session.json meta 持久化）。
 * 由 useDispatchEventConsumer 监听 SSE `session_token_usage` 事件维护。
 *
 * 字段口径（normalize 后两协议统一）：
 * - input/output：入参 / 出参 token 累加值
 * - cacheRead/cacheCreate：仅 Anthropic 原生协议有；OpenAI 兼容协议为 0
 * - total：4 字段累加和（含 cache）
 * - calls：LLM 调用次数（按"一轮 chat"计，工具循环每轮 +1）
 */
/**
 * 上下文构成估算（本轮请求的分段 token，后端 build_messages 算好随 usage 事件下发）。
 * 全部为估算值（±5%），与 input/output 等真实计费字段区分。
 */
export interface ContextBreakdown {
  system: number;      // 主人格提示词（GENOME/IDENTITY/SOUL 等）
  extra: number;       // 动态注入（工作模式 hint / pinned / PROJECT_NOTES 等）
  messages: number;    // 消息历史（含 tool 消息）
  tools: number;       // 该角色生效工具的 description+schema
  total: number;
  model?: string;
  contextLength?: number;    // 模型上下文窗口（capabilities 表/显式配置）；未知时前端隐藏进度条
  compactThreshold?: number; // 上下文压缩触发线（窗口的 70%）
}

export interface TokenUsageState {
  input: number;
  output: number;
  cacheRead: number;
  cacheCreate: number;
  total: number;
  calls: number;
  lastCallAt?: string;
  contextBreakdown?: ContextBreakdown | null;
}

// ─────── Session slice ─────────

export interface SessionState {
  key: string;                 // SessionKey 字符串形式
  messages: StoreMessage[];
  /**
   * Phase 1B-2 阶段 D：messages 真值字段（ref 层已删）。
   * 阶段 B 起 Chat.tsx 切读这个字段；阶段 D 后讨论是否合并 messages/chatMessages。
   * 与 messages 的差异：messages 用 StoreMessage（schema 化但当前无消费者），
   * chatMessages 用 Message（Chat.tsx 渲染口径，含 segments / streaming 状态等）。
   */
  chatMessages?: ChatMessage[];
  /**
   * Token 累计消耗（per-session 独立、跨重启保留）。
   * SSE `session_token_usage` 事件覆盖式更新（payload 是累加后的总值，前端不做累加）。
   */
  tokenUsage?: TokenUsageState | null;
  status: SessionStatus;
  /** 最后收到的 event_id，用于断线续传 */
  lastEventId?: number;
  error?: string;
  /**
   * P1.1（架构重构 2026-05-23）：per-sid in-flight loading。
   * 之前在 Chat 组件内 useRef Map<sid,boolean> 持有，Chat unmount → 整 map 蒸发，
   * 切走切回（切到议事厅 / 多视图共存）后停止按钮失效、isLoading 状态不正确。
   * 提到 store 后跟 Chat 组件 lifecycle 解耦：Chat 仅订阅，Chat unmount 不影响。
   * 设计参考 references/provider-models 的 loadingMap。
   */
  loading?: boolean;
  /**
   * P1.2（架构重构 2026-05-23）：per-sid message meta（updatedAt / messageCount）。
   * 之前在 Chat 组件内 useRef Map<sid, meta> 持有，Chat unmount → 整 map 蒸发，
   * 导致 cacheIsFresh 判断错乱、切回时拉不到 backend 真值（"光标一闪一闪没结果" bug）。
   * 提到 store 后即使 Chat unmount，meta 仍持久，新 mount 切回时能正确判 cache 新鲜度。
   */
  messageMeta?: ChatSessionMessageMeta;
  /**
   * P1.4a（架构重构 2026-05-23）：worker / dispatch 外部流的 streamId → messageId 映射。
   * 之前在 Chat 组件内 useRef<Record<sid::streamId, number>> 持有（外部流 SSE 推 thinking /
   * content / final / error 事件时，前端按 streamId 找现有 message 累加，没有就新建）。
   * Chat unmount → 整 map 丢 → 切回时同一个 worker 的下一帧事件被误判为新流 → 重复创建
   * 占位 message 或注入到错的 message id。提到 store 后跟 Chat lifecycle 解耦。
   *
   * key = streamId（不含 sid 前缀，因为已经在 sessions[sid] 下了）；value = messageId。
   * 终态事件（onWorkerFinal / onWorkerError）后 callsite 调 removeSessionExternalStreamMessageId 清掉。
   */
  externalStreamMessageIds?: Record<string, number>;
  /**
   * P1.4b（架构重构 2026-05-23）：per-sid 仪式 marker 列表（consult_start / consult_end /
   * consult_error 等子代理/咨询场景的时间线标记）。之前在 Chat 组件内 useRef<Record<sid, RitualMarker[]>>
   * 持有，Chat unmount → 整 map 丢 → 切回时 timeline 失去这些标记。
   * 提到 store 后跟 Chat lifecycle 解耦。
   */
  timelineMarkers?: RitualMarker[];
  /**
   * P1.4c-1（架构重构 2026-05-23）：per-sid route progress 占位消息的 messageId。
   * 任务被路由出去后，前端塞一条 "路由中..." 的占位 assistant message，
   * 需要按 sid 记住那条 message 的 id 以便后续 upsert / 终态清除。
   * 之前在 Chat 组件内 useRef<Record<sid, number>>，always-mount 下能用，但
   * 一旦 HMR / strict mode double-mount / 未来取消 always-mount → 整 map 丢，
   * route progress 占位消息漏清。提到 store 后跟 Chat lifecycle 解耦。
   */
  routedProgressMessageId?: number;
}

/**
 * P1.2（架构重构 2026-05-23）：session message meta 类型上提到 store 层。
 * 之前定义在 useChatSessionRuntime.ts，导致 store 与组件互相依赖。
 * 现在 store 是真值层，组件 re-export 该类型保持兼容。
 */
export interface ChatSessionMessageMeta {
  updatedAt?: string;
  messageCount?: number;
  historyRevision?: number;
  nextMessageSeq?: number;
}

// ─────── Room slice ─────────

export type RoomStatus = 'idle' | 'active' | 'paused' | 'closed';

export interface RoomParticipant {
  type: 'user' | 'agent';
  id: string;
  platform?: string;
  displayName?: string;
}

export interface RoomState {
  id: string;
  title: string;
  participants: RoomParticipant[];
  speakerTargets?: string[];
  messages: StoreMessage[];
  status: RoomStatus;
  pausedReason?: string;
  lastEventId?: number;
}

// ─────── Agent slice ─────────

export interface AgentState {
  id: string;
  /** UTC 秒，最近一次活动时间（agent 主动 / 被动） */
  lastActiveAt?: number;
  /** 心跳间隔（s），未配置则使用默认 3600 */
  tickIntervalSec?: number;
  /** 当前是否 online（actor.running） */
  online: boolean;
  joinedSessions: string[];
  joinedRooms: string[];
}

// ─────── Panorama slice ─────────

export interface PanoramaEntry {
  id: string;            // 通常是 event_id 字符串
  type: string;          // 业务事件类型
  source: string;        // 来源 channel（session:xxx / room:xxx / agent:xxx）
  summary: string;
  timestamp: number;
}

// ─────── Store shape ─────────

/**
 * 后端写改通知（自进化护网 D）：角色 write_file/patch/edit_file/notebook_edit
 * 命中 backend/* 时，后端 SSE 推 backend_modified 事件，前端 toast 提示用户。
 *
 * 全局队列（不挂 session）——任何 session 触发的写都集中到一个 UI 区显示。
 */
export interface BackendToast {
  id: string;
  ts: number;
  agentId?: string;
  toolName: string;
  path: string;
  filename: string;
  charsBefore: number;
  charsAfter: number;
  linesBefore: number;
  linesAfter: number;
  created: boolean;
  backupPath?: string;
}

interface AppStoreState {
  sessions: Record<string, SessionState>;
  rooms: Record<string, RoomState>;
  agents: Record<string, AgentState>;
  panorama: PanoramaEntry[];
  backendToasts: BackendToast[];
}

interface AppStoreActions {
  // session actions
  ensureSession: (key: string) => void;
  appendSessionMessage: (key: string, msg: StoreMessage) => void;
  replaceSessionMessages: (key: string, messages: StoreMessage[]) => void;
  setSessionStatus: (key: string, status: SessionStatus, error?: string) => void;
  setSessionLastEventId: (key: string, eventId: number) => void;
  resetSessionLastEventId: (key: string, eventId: number) => void;
  /**
   * Phase 1B-2 阶段 D：messages 唯一写入入口。
   * 调用方走 readSessionChatMessages / writeSessionChatMessages helper（in useChatSessionRuntime）。
   */
  setSessionChatMessages: (key: string, messages: ChatMessage[]) => void;
  hydrateSessionChatMessages: (key: string, messages: ChatMessage[], meta: ChatSessionMessageMeta) => void;
  /** Token usage：覆盖式写入（payload 是累加后的总值） */
  setSessionTokenUsage: (key: string, usage: TokenUsageState | null) => void;
  /**
   * P1.1（架构重构 2026-05-23）：写入 per-sid loading。
   * 入参 `key` 是 session id；前端 sendMessage 起设 true，[DONE] / abort / error 设 false。
   */
  setSessionLoading: (key: string, loading: boolean) => void;
  /**
   * P1.2（架构重构 2026-05-23）：写入 per-sid message meta（updatedAt / messageCount）。
   * 切会话 / refreshSessionState / syncSessionSnapshot 完成后同步真值。
   * 传 null 表示清掉（用于 clearSession 类操作）。
   */
  setSessionMessageMeta: (key: string, meta: ChatSessionMessageMeta | null) => void;
  /**
   * P1.4a（架构重构 2026-05-23）：set 单个 streamId 的 messageId 映射。
   * 外部流第一帧到达时由 callsite 分配 messageId 并写入；后续帧通过 read helper 找到同一条。
   */
  setSessionExternalStreamMessageId: (key: string, streamId: string, messageId: number) => void;
  /**
   * P1.4a：删除单个 streamId 的映射（终态事件后）。
   */
  removeSessionExternalStreamMessageId: (key: string, streamId: string) => void;
  /**
   * P1.4b（架构重构 2026-05-23）：覆盖 per-sid 仪式 marker 列表。
   * 传 null 清空（取代 delete sessionTimelineMarkersMapRef.current[sid]）。
   */
  setSessionTimelineMarkers: (key: string, markers: RitualMarker[] | null) => void;
  /**
   * P1.4c-1（架构重构 2026-05-23）：写 per-sid route progress 占位 messageId（数字）。
   * 传 null 清除（取代 delete routedProgressMessageIdMapRef.current[sid]）。
   */
  setSessionRoutedProgressMessageId: (key: string, messageId: number | null) => void;
  clearSession: (key: string) => void;

  // room actions
  ensureRoom: (room: Omit<RoomState, 'messages' | 'status'> & Partial<Pick<RoomState, 'messages' | 'status'>>) => void;
  appendRoomMessage: (id: string, msg: StoreMessage) => void;
  setRoomStatus: (id: string, status: RoomStatus, pausedReason?: string) => void;
  setRoomLastEventId: (id: string, eventId: number) => void;
  clearRoom: (id: string) => void;

  // agent actions
  upsertAgent: (state: Partial<AgentState> & Pick<AgentState, 'id'>) => void;
  setAgentOnline: (id: string, online: boolean) => void;
  recordAgentActivity: (id: string, at?: number) => void;

  // panorama actions
  pushPanoramaEntry: (entry: PanoramaEntry, maxKeep?: number) => void;
  clearPanorama: () => void;

  // backend toast actions
  pushBackendToast: (toast: BackendToast, maxKeep?: number) => void;
  dismissBackendToast: (id: string) => void;
  clearBackendToasts: () => void;

  // dev / tests
  resetAll: () => void;
}

export type AppStore = AppStoreState & AppStoreActions;

// ─────── 初始 state ─────────

const initialState: AppStoreState = {
  sessions: {},
  rooms: {},
  agents: {},
  panorama: [],
  backendToasts: [],
};

// ─────── Store ─────────

export const useAppStore = create<AppStore>()(
  immer((set) => ({
    ...initialState,

    // ───── session ─────
    ensureSession: (key) =>
      set((state) => {
        if (!state.sessions[key]) {
          state.sessions[key] = {
            key,
            messages: [],
            status: 'idle',
          };
        }
      }),

    appendSessionMessage: (key, msg) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [msg],
            status: 'streaming',
            lastEventId: msg.eventId,
          };
          return;
        }
        s.messages.push(msg);
        if (msg.eventId !== undefined) s.lastEventId = msg.eventId;
      }),

    replaceSessionMessages: (key, messages) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [...messages],
            status: 'idle',
          };
          return;
        }
        s.messages = [...messages];
      }),

    setSessionStatus: (key, status, error) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            status,
            error,
          };
          return;
        }
        s.status = status;
        s.error = error;
      }),

    setSessionLastEventId: (key, eventId) =>
      set((state) => {
        const s = state.sessions[key];
        if (s) s.lastEventId = Math.max(s.lastEventId ?? 0, eventId);
      }),

    resetSessionLastEventId: (key, eventId) =>
      set((state) => {
        const s = state.sessions[key];
        if (s) s.lastEventId = Math.max(0, eventId);
      }),

    setSessionChatMessages: (key, messages) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            chatMessages: [...messages],
            status: 'idle',
          };
          return;
        }
        s.chatMessages = [...messages];
      }),


    hydrateSessionChatMessages: (key, messages, meta) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            chatMessages: [...messages],
            messageMeta: { ...meta },
            status: 'idle',
          };
          return;
        }
        const currentRevision = s.messageMeta?.historyRevision;
        const nextRevision = meta.historyRevision;
        if (typeof currentRevision === 'number') {
          if (typeof nextRevision !== 'number' || nextRevision < currentRevision) return;
          if (
            nextRevision === currentRevision
            && typeof s.messageMeta?.nextMessageSeq === 'number'
            && typeof meta.nextMessageSeq === 'number'
            && meta.nextMessageSeq < s.messageMeta.nextMessageSeq
          ) return;
        }
        s.chatMessages = [...messages];
        s.messageMeta = { ...meta };
      }),

    setSessionLoading: (key, loading) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          // 还没 ensureSession 就被设 loading（提交流程的早期路径）→ 顺手建一个
          // 骨架，避免 callsite 必须先 ensureSession 才能写。下游字段保持空。
          state.sessions[key] = {
            key,
            messages: [],
            status: 'idle',
            loading,
          };
          return;
        }
        s.loading = loading;
      }),

    setSessionMessageMeta: (key, meta) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            status: 'idle',
            messageMeta: meta ? { ...meta } : undefined,
          };
          return;
        }
        s.messageMeta = meta ? { ...meta } : undefined;
      }),

    setSessionExternalStreamMessageId: (key, streamId, messageId) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            status: 'idle',
            externalStreamMessageIds: { [streamId]: messageId },
          };
          return;
        }
        if (!s.externalStreamMessageIds) {
          s.externalStreamMessageIds = {};
        }
        s.externalStreamMessageIds[streamId] = messageId;
      }),

    removeSessionExternalStreamMessageId: (key, streamId) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s || !s.externalStreamMessageIds) return;
        delete s.externalStreamMessageIds[streamId];
      }),

    setSessionTimelineMarkers: (key, markers) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            status: 'idle',
            timelineMarkers: markers ? [...markers] : undefined,
          };
          return;
        }
        s.timelineMarkers = markers ? [...markers] : undefined;
      }),

    setSessionRoutedProgressMessageId: (key, messageId) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            status: 'idle',
            routedProgressMessageId: messageId === null ? undefined : messageId,
          };
          return;
        }
        s.routedProgressMessageId = messageId === null ? undefined : messageId;
      }),

    setSessionTokenUsage: (key, usage) =>
      set((state) => {
        const s = state.sessions[key];
        if (!s) {
          state.sessions[key] = {
            key,
            messages: [],
            tokenUsage: usage ? { ...usage } : null,
            status: 'idle',
          };
          return;
        }
        s.tokenUsage = usage ? { ...usage } : null;
      }),

    clearSession: (key) =>
      set((state) => {
        delete state.sessions[key];
      }),

    // ───── room ─────
    ensureRoom: (room) =>
      set((state) => {
        if (state.rooms[room.id]) return;
        state.rooms[room.id] = {
          messages: [],
          status: 'idle',
          ...room,
        };
      }),

    appendRoomMessage: (id, msg) =>
      set((state) => {
        const r = state.rooms[id];
        if (!r) return;
        r.messages.push(msg);
        if (msg.eventId !== undefined) r.lastEventId = msg.eventId;
      }),

    setRoomStatus: (id, status, pausedReason) =>
      set((state) => {
        const r = state.rooms[id];
        if (!r) return;
        r.status = status;
        r.pausedReason = pausedReason;
      }),

    setRoomLastEventId: (id, eventId) =>
      set((state) => {
        const r = state.rooms[id];
        if (r) r.lastEventId = eventId;
      }),

    clearRoom: (id) =>
      set((state) => {
        delete state.rooms[id];
      }),

    // ───── agent ─────
    upsertAgent: (partial) =>
      set((state) => {
        const existing = state.agents[partial.id];
        if (!existing) {
          state.agents[partial.id] = {
            id: partial.id,
            online: partial.online ?? false,
            joinedSessions: partial.joinedSessions ?? [],
            joinedRooms: partial.joinedRooms ?? [],
            lastActiveAt: partial.lastActiveAt,
            tickIntervalSec: partial.tickIntervalSec,
          };
          return;
        }
        Object.assign(existing, partial);
      }),

    setAgentOnline: (id, online) =>
      set((state) => {
        const a = state.agents[id];
        if (a) a.online = online;
      }),

    recordAgentActivity: (id, at) =>
      set((state) => {
        const a = state.agents[id];
        if (a) a.lastActiveAt = at ?? Date.now() / 1000;
      }),

    // ───── panorama ─────
    pushPanoramaEntry: (entry, maxKeep = 200) =>
      set((state) => {
        state.panorama.push(entry);
        if (state.panorama.length > maxKeep) {
          state.panorama.splice(0, state.panorama.length - maxKeep);
        }
      }),

    clearPanorama: () =>
      set((state) => {
        state.panorama = [];
      }),

    // ───── backend toasts ─────
    pushBackendToast: (toast, maxKeep = 30) =>
      set((state) => {
        state.backendToasts.push(toast);
        if (state.backendToasts.length > maxKeep) {
          state.backendToasts.splice(0, state.backendToasts.length - maxKeep);
        }
      }),

    dismissBackendToast: (id) =>
      set((state) => {
        state.backendToasts = state.backendToasts.filter((t) => t.id !== id);
      }),

    clearBackendToasts: () =>
      set((state) => {
        state.backendToasts = [];
      }),

    // ───── reset ─────
    resetAll: () =>
      set((state) => {
        state.sessions = {};
        state.rooms = {};
        state.agents = {};
        state.panorama = [];
        state.backendToasts = [];
      }),
  })),
);

// dev-only：把 store 暴露到 window，方便 DevTools console 直接查影子双写一致性
// 用法：useAppStore.getState().sessions[sid].chatMessages
if (import.meta.env.DEV) {
  (window as unknown as { useAppStore?: typeof useAppStore }).useAppStore = useAppStore;
}
