"""v2 · E-0a · session_mapper — IM 消息 → 柳如烟 (user_id, agent_id, session_id) 映射

设计：docs/architecture_v2/01_master_plan.md Phase E "per-user 隔离 key 算法"

核心需求（从 Phase E 验收 + 现有 v1 行为反推）：

1. **per-user 隔离**：同一群里 A 和 B 两个用户同时发问 → session 不混
2. **per-agent 隔离**（v3 二维化）：同 IM 聊天但不同 agent → 不同 session；
   切回 agent 又能命中之前的 sid 继续历史
3. **稳定**：同一 (channel, chat_id, platform_user_id, agent_id, thread_ts)
   永远映射到同一柳如烟 session_id（重启不变）
4. **thread/topic 感知**：钉钉话题 / 飞书 thread_ts → 不同 session
5. **跨 channel 隔离**：dingtalk 用户 X 跟 feishu 用户 X 是不同的柳如烟 user_id
   （即使平台原生 ID 相同也不混）

算法：

```
session_key = f"{channel}:{chat_id}:{platform_user_id}:agent:{agent_id}"
            + (f":thread:{thread_ts}" if thread_ts else "")
            + (f":topic:{topic_id}" if topic_id else "")
session_id = sha256(session_key).hexdigest()[:24]    # 24 char short hash

liuruyan_user_id = f"{channel}:{platform_user_id}"   # 命名空间隔离
```

为何把 agent_id 算进 hash（v3 设计决策）：

session 在磁盘是按 ``agents/<agent_id>/sessions/<sid>/`` 二维存的；如果 sid
只按 IM 聊天算（不含 agent_id），那同 chat 切换 channel.default_agent 时：
- 算出同一个 sid → SessionManager.get_session 在磁盘 probe 找到旧 agent 名下的 → 返回
- dispatch 用新 agent 实例化 Agent，但 conversation_history 是旧 agent 的累计聊天
- LLM 看到 history 里前面都是旧 agent 在说话 → 推断"我是旧 agent"→ 以旧语气回

二维化后 session identity 跟磁盘存放对齐：每个 (chat, agent) 各自独立 sid，
切回任一组合都能命中那个组合自己的历史。

承接两种 IM 路由模式：
- **1 App = 1 Agent**（当前 ChannelConfig.default_agent）：caller 传 channel
  config 里的 default_agent 即可。每条 channel 配置对应一个 (chat, agent) 桶
- **1 App = N Agent**（未来按 @mention / 关键词 / LLM 决策路由）：caller 自己
  算每条 inbound 的 target agent，传给 map_session。同 chat 多 agent 分别有
  自己的 session，互不污染

不依赖 IM 平台 SDK——纯算法 + 数据类。Phase E channel adapter 会用本模块。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


_SESSION_HASH_LEN = 24


@dataclass(frozen=True, slots=True)
class SessionKey:
    """IM 消息映射到柳如烟内部三元组的输出。

    frozen=True：防止误改导致同一 IM 消息映射出多个 session。
    """

    user_id: str        # 柳如烟内部 user_id（带 channel 命名空间）
    agent_id: str       # 该 channel 配置的默认 agent
    session_id: str     # 24 char 稳定 hash
    raw_key: str        # 完整 raw key（debug 用）


def map_session(
    *,
    channel: str,
    chat_id: str,
    platform_user_id: str,
    agent_id: str,
    thread_ts: str | None = None,
    topic_id: str | None = None,
) -> SessionKey:
    """IM 消息四元组 → 柳如烟 (user_id, agent_id, session_id)。

    参数：
    - channel: IM 平台名（"dingtalk" / "feishu" / "wecom" / "wechat" / "web" / ...）
    - chat_id: 平台原生会话 ID（群 ID / 私聊 ID / 频道 ID）
    - platform_user_id: 平台原生用户 ID（不是柳如烟 user_id）
    - agent_id: 该 channel 配置的"该消息应路由到哪个 agent"
    - thread_ts: 平台原生 thread/楼中楼 时间戳（可选；钉钉话题 / 飞书 thread_ts）
    - topic_id: 用户在平台明示的话题 ID（可选；如群里 #话题 标记）

    返回：SessionKey（frozen dataclass）

    异常：channel/chat_id/platform_user_id/agent_id 任一为空 → ValueError
    （会话路由必须有完整身份；缺字段是上游 channel adapter 的 bug，应该 fail-loud）
    """
    if not channel or not chat_id or not platform_user_id or not agent_id:
        raise ValueError(
            f"map_session: missing required field(s) "
            f"channel={channel!r} chat_id={chat_id!r} "
            f"platform_user_id={platform_user_id!r} agent_id={agent_id!r}"
        )

    # session identity 二维：(IM 聊天, agent)。把 agent_id 加进 hash 让磁盘存
    # 放（agents/<agent_id>/sessions/<sid>/）跟 sid 算法对齐——同 chat 切 agent
    # 自动新 sid，避免历史串台。详见模块 docstring "为何把 agent_id 算进 hash"。
    parts = [channel, chat_id, platform_user_id, f"agent:{agent_id}"]
    if thread_ts:
        parts.append(f"thread:{thread_ts}")
    if topic_id:
        parts.append(f"topic:{topic_id}")
    raw_key = ":".join(parts)

    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    session_id = digest[:_SESSION_HASH_LEN]

    return SessionKey(
        user_id=f"{channel}:{platform_user_id}",
        agent_id=agent_id,
        session_id=session_id,
        raw_key=raw_key,
    )


__all__ = ["SessionKey", "map_session"]
