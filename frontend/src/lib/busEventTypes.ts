/**
 * EventBus 协议类型（与后端 backend/core/inbox_message.py 同步）
 *
 * 命名规则：channel 前缀决定语义
 *   session:{session_key}  → 1v1 会话
 *   room:{room_id}         → 协作房间
 *   agent:{agent_id}:inbox → agent 邮箱（不暴露前端）
 *   panorama               → 全局活动
 */

export interface BusEvent {
  type: string;
  event_id: number;
  timestamp: number;
  /** 业务字段（动态扩展） */
  [key: string]: unknown;
}

/** 客户端 → 服务端 */
export type WsClientMessage =
  | {
      action: 'subscribe';
      channel: string;
      last_event_id?: number;
    }
  | {
      action: 'unsubscribe';
      channel: string;
    }
  | {
      action: 'ping';
    }
  | {
      action: 'pong';
    };

/** 服务端 → 客户端 */
export type WsServerMessage =
  | {
      action: 'subscribed';
      channel: string;
      note?: string;
    }
  | {
      action: 'unsubscribed';
      channel: string;
    }
  | {
      action: 'event';
      channel: string;
      event: BusEvent;
    }
  | {
      action: 'ping';
      ts: number;
    }
  | {
      action: 'pong';
      ts: number;
    }
  | {
      action: 'error';
      message: string;
    };

/** Channel 前缀 helper */
export const channelSession = (sessionKey: string): string => `session:${sessionKey}`;
export const channelRoom = (roomId: string): string => `room:${roomId}`;
export const channelAgentInbox = (agentId: string): string => `agent:${agentId}:inbox`;
export const PANORAMA_CHANNEL = 'panorama';
