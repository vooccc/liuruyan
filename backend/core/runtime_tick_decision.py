"""真 LLM tick handler（Phase 1B-3）

替换 default_on_tick 占位为可注入的"真 LLM 决策"handler。

设计原则：
1. **不动 default_on_tick**（1B-1 不变量 6）：本 handler 是注入式选项，
   用户在启动时 `bootstrap_agent_actors(on_tick=real_llm_tick_handler)` 显式启用。
2. **默认仍 OFF**（1B-1 不变量 5）：lifespan 仍调 default_on_tick；本 handler
   不接 lifespan，由用户后续按需 wiring。
3. **最小可启用**：当前仅实现 NO_OP / SPEAK 两个 action；DELEGATE / REMIND
   等待后台任务 runtime 接口稳定后再加。
4. **LLMClient 用 use_tools=False 路径**（C-2/D commit 9a1a8db 单例污染治根）：
   tick 不调工具，只让 LLM 决定 action；不污染主对话流的 _current_agent_id /
   _current_session_id state。
5. **80%+ NO_OP 默认偏向**：prompt 显式引导"绝大多数 NO_OP"，避免烦人。

未来扩展点（不在本 commit）：
- DELEGATE：LLM 决定派 worker 做事（待后台任务 EventBus 接通后接）
- REMIND：LLM 主动 schedule 下次 tick（需要新 schedule_one_shot_tick API）
- target_session：LLM 决定 SPEAK 到具体 session（需要 multi-session 上下文聚合）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional

from backend.core.agent_actor import AgentActor
from backend.core.event_bus import (
    PANORAMA_CHANNEL,
    EventBus,
    get_default_bus,
)
from backend.core.inbox_message import BusEvent

logger = logging.getLogger(__name__)


# ─────── 常量 ─────────

TICK_LLM_TIMEOUT_SEC = 30.0
SPEAK_MESSAGE_MAX_LEN = 80

ALLOWED_ACTIONS = {"NO_OP", "SPEAK"}


# ─────── Prompt ─────────

TICK_DECISION_PROMPT = """你是 {agent_id}（一个 AI 助手），刚醒来，需要决定要不要做点什么。

当前时间：{now_human}
最近 1 小时活动量：{activity_count} 次（消息进出 + 你产出的回复）

请决定一个动作。**绝大多数情况应该是 NO_OP**——除非你明确感受到此刻有意义的事要做（比如刚有过活跃对话还想补充几句、或者长时间没说话想刷个存在感）。

返回严格 JSON（仅 JSON，不要任何额外文字、不要 markdown 围栏）：

NO_OP 示例：
{{"action": "NO_OP", "reason": "无事可说"}}

SPEAK 示例（在 panorama 公告板自言自语一句，dev 面板能看到）：
{{"action": "SPEAK", "message": "想喝茶了。", "reason": "活跃后的一句闲话"}}

约束：
- action 只能是 NO_OP 或 SPEAK
- SPEAK 时 message 必须 ≤ {speak_max} 字
- 不要因为孤独就 SPEAK；不要重复说类似的话
- 不允许 DELEGATE / REMIND / 其他 action（暂未支持）
"""


# ─────── 数据结构 ─────────

@dataclass
class TickDecision:
    action: str  # "NO_OP" | "SPEAK"
    reason: str = ""
    message: Optional[str] = None  # 仅 SPEAK 用


# ─────── LLM 调用契约（可注入便于测试）─────────

# (messages, agent_id) -> AsyncIterator[event-dict]
LLMCaller = Callable[[List[dict], str], Any]


async def _default_llm_caller(messages: List[dict], agent_id: str):
    """默认实现：用 backend.llm_client 全局单例。

    use_tools=False 路径不污染单例的 _current_* state（commit 9a1a8db 治根）。
    返回 async iterator，内部 yield 各类 event dict（content / thinking / etc.）。
    """
    from backend.llm_client import get_llm_client
    client = get_llm_client()
    return client.chat(
        messages=messages,
        stream=False,
        use_tools=False,
        agent_id=agent_id,
        session_id=None,
    )


# ─────── 解析 ─────────

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _strip_json_fence(text: str) -> str:
    """LLM 偶发用 ```json ... ``` 围栏，剥掉。"""
    text = text.strip()
    m = _JSON_FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def parse_tick_decision(raw: str) -> Optional[TickDecision]:
    """解析 LLM 返回的 JSON 决策。失败返 None（视为 NO_OP）。"""
    if not raw or not raw.strip():
        return None

    cleaned = _strip_json_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    action = str(data.get("action", "")).upper().strip()
    if action not in ALLOWED_ACTIONS:
        return None

    reason = str(data.get("reason", "") or "")
    message_raw = data.get("message")
    message = str(message_raw) if message_raw is not None else None

    if action == "SPEAK":
        if not message or not message.strip():
            return None  # SPEAK 必须有 message
        # 截断防超长
        if len(message) > SPEAK_MESSAGE_MAX_LEN:
            message = message[:SPEAK_MESSAGE_MAX_LEN]

    return TickDecision(action=action, reason=reason, message=message)


# ─────── 执行 ─────────

async def execute_tick_decision(
    actor: AgentActor,
    decision: TickDecision,
    *,
    bus: Optional[EventBus] = None,
) -> None:
    """执行决策。NO_OP 仅记日志；SPEAK 推 panorama channel。"""
    if decision.action == "NO_OP":
        logger.info(
            "tick[%s] NO_OP: %s",
            actor.agent_id,
            decision.reason or "(no reason)",
        )
        return

    if decision.action == "SPEAK":
        target_bus = bus or get_default_bus()
        await target_bus.publish(
            PANORAMA_CHANNEL,
            BusEvent(
                type="agent_tick_speak",
                payload={
                    "agent_id": actor.agent_id,
                    "message": decision.message or "",
                    "reason": decision.reason,
                    "at": time.time(),
                },
            ),
        )
        logger.info(
            "tick[%s] SPEAK -> panorama: %s",
            actor.agent_id,
            decision.message,
        )
        return

    # 不应到达（parse 已守白名单），防御性日志
    logger.warning("tick[%s] unknown action: %s", actor.agent_id, decision.action)


# ─────── 调 LLM 收集结果 ─────────

async def _collect_content(stream) -> str:
    """从 chat() 的 async iterator 收集 content 事件拼成完整文本。

    chat() 是 async generator，事件形如 {"type": "content", "text": "..."}；
    我们只关心 content；thinking / system_notice 等忽略。
    """
    chunks: List[str] = []
    async for event in stream:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "content":
            text = event.get("text") or event.get("content") or ""
            if text:
                chunks.append(str(text))
    return "".join(chunks)


async def call_tick_llm(
    actor: AgentActor,
    *,
    llm_caller: Optional[LLMCaller] = None,
) -> Optional[TickDecision]:
    """调 LLM 决定 tick action。失败/超时/解析失败均返 None。"""
    caller = llm_caller or _default_llm_caller

    prompt = TICK_DECISION_PROMPT.format(
        agent_id=actor.agent_id,
        now_human=time.strftime("%Y-%m-%d %H:%M"),
        activity_count=actor.activity.count(),
        speak_max=SPEAK_MESSAGE_MAX_LEN,
    )

    try:
        stream = caller([{"role": "user", "content": prompt}], actor.agent_id)
    except Exception:  # noqa: BLE001
        logger.exception("real_llm_tick[%s]: caller setup failed", actor.agent_id)
        return None

    try:
        raw = await asyncio.wait_for(_collect_content(stream), timeout=TICK_LLM_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("real_llm_tick[%s]: LLM timeout (>%.0fs), NO_OP", actor.agent_id, TICK_LLM_TIMEOUT_SEC)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("real_llm_tick[%s]: stream collect failed", actor.agent_id)
        return None

    decision = parse_tick_decision(raw)
    if decision is None:
        logger.info("real_llm_tick[%s]: parse failed, raw=%r", actor.agent_id, raw[:200])
    return decision


# ─────── 主入口（注入到 bootstrap_agent_actors）─────────

async def real_llm_tick_handler(actor: AgentActor) -> None:
    """1B-3 真 LLM tick handler。

    用法：
        from backend.core.runtime_tick_decision import real_llm_tick_handler
        await bootstrap_agent_actors(on_tick=real_llm_tick_handler)

    流程：
        1. 调 LLM 决定 action（NO_OP / SPEAK）
        2. 执行决策（SPEAK 推 panorama channel）
        3. 失败 / 超时 / 解析失败均视为 NO_OP，记日志

    安全：
        - 任何异常都不会冒泡到 actor._handle_tick（actor 已 try/except）
        - LLM 单例 use_tools=False 不污染 _current_* state
        - tick_guard 配额 + 间隔限制由 actor._handle_tick 上层守
    """
    decision = await call_tick_llm(actor)
    if decision is None:
        return  # 视为 NO_OP

    try:
        await execute_tick_decision(actor, decision)
    except Exception:  # noqa: BLE001
        logger.exception("real_llm_tick[%s]: execute failed", actor.agent_id)
