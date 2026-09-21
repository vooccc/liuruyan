/**
 * Store selector helpers
 *
 * 用 useAppStore 的 selector 时，建议把派生计算放在这里集中维护，避免组件
 * 里散写选择器导致重渲染（zustand selector 默认 shallow 比较，但函数选择器
 * 每次返回新对象会触发重渲染）。
 *
 * 用法：
 *   const session = useAppStore(selectSession('web:dm:abc'));
 *   const messages = useAppStore(selectSessionMessages('web:dm:abc'));
 */

import type { Message as ChatMessage } from '../api/chat';
import type { AppStore, RoomState, SessionState, StoreMessage, AgentState } from './useAppStore';

// ─────── session selectors ─────────

export const selectSession =
  (key: string) =>
  (state: AppStore): SessionState | undefined =>
    state.sessions[key];

export const selectSessionMessages =
  (key: string) =>
  (state: AppStore): StoreMessage[] =>
    state.sessions[key]?.messages ?? [];

/**
 * Phase 1B-2 阶段 A 影子双写读端。
 * 阶段 B 后 Chat.tsx 改读这个 selector；阶段 A 期间仅供一致性查看器对照用。
 */
export const selectSessionChatMessages =
  (key: string) =>
  (state: AppStore): ChatMessage[] =>
    state.sessions[key]?.chatMessages ?? [];

export const selectSessionStatus =
  (key: string) =>
  (state: AppStore): SessionState['status'] =>
    state.sessions[key]?.status ?? 'idle';

export const selectSessionLastEventId =
  (key: string) =>
  (state: AppStore): number | undefined =>
    state.sessions[key]?.lastEventId;

export const selectAllSessionKeys = (state: AppStore): string[] =>
  Object.keys(state.sessions);

// ─────── room selectors ─────────

export const selectRoom =
  (id: string) =>
  (state: AppStore): RoomState | undefined =>
    state.rooms[id];

export const selectRoomMessages =
  (id: string) =>
  (state: AppStore): StoreMessage[] =>
    state.rooms[id]?.messages ?? [];

export const selectAllRoomIds = (state: AppStore): string[] =>
  Object.keys(state.rooms);

// ─────── agent selectors ─────────

export const selectAgent =
  (id: string) =>
  (state: AppStore): AgentState | undefined =>
    state.agents[id];

export const selectAgentOnline =
  (id: string) =>
  (state: AppStore): boolean =>
    state.agents[id]?.online ?? false;

export const selectAllAgentIds = (state: AppStore): string[] =>
  Object.keys(state.agents);

// ─────── panorama selectors ─────────

export const selectPanorama = (state: AppStore) => state.panorama;
