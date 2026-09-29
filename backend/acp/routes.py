"""ACP 外部 agent 设置面板路由。

只读：返回每个 EXTERNAL_AGENTS 当前可用性 + 从哪借的 key + 还缺什么。
设计：写 API key 走「模型服务」面板（providers_routes），不在这里搞表单，
避免两套 UI 编辑同一份 secrets 引起冲突。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/acp", tags=["acp"])


class ProviderOverrideBody(BaseModel):
    """POST /acp/external_agents/{agent}/provider 请求体。

    provider_id = None / 空字符串 → 清除用户偏好，回落到 EXTERNAL_AGENTS 内置默认。
    """
    provider_id: Optional[str] = None


@router.get("/external_agents")
async def list_external_agents_status() -> Dict[str, Any]:
    """返回所有外部 ACP agent 的当前可用性状态。"""
    rows: List[Dict[str, Any]] = []
    try:
        from backend.acp.client.service import EXTERNAL_AGENTS, agent_availability

        for name in EXTERNAL_AGENTS.keys():
            try:
                rows.append(agent_availability(name))
            except Exception as err:
                logger.error("Failed to check ACP agent '%s' availability: %s", name, err)
                rows.append({
                    "agent": name,
                    "description": "",
                    "command": "",
                    "env_required": [],
                    "provider_id": None,
                    "effective_provider_id": None,
                    "compatible_providers": [],
                    "available": False,
                    "missing_env": [],
                    "resolved_via": "none",
                    "error": str(err),
                })
    except Exception as exc:
        logger.error("Failed to load ACP service: %s", exc)
    return {"agents": rows}


@router.post("/external_agents/{agent}/provider")
async def set_external_agent_provider(agent: str, body: ProviderOverrideBody) -> Dict[str, Any]:
    """保存"该外部 agent 借哪个 provider 的 key"用户偏好。

    POST body: {"provider_id": "custom-llmapi-pro" | null | ""}

    - 传非空 provider_id：写入用户偏好（覆盖 EXTERNAL_AGENTS 内置默认）
    - 传 null / 空字符串：清除偏好，下次 spawn 用内置默认

    返回更新后的 agent 状态（同 GET /acp/external_agents 单项）。
    """
    from backend.acp.client.service import (
        EXTERNAL_AGENTS,
        agent_availability,
        save_user_provider_override,
        list_compatible_providers,
    )

    if agent not in EXTERNAL_AGENTS:
        raise HTTPException(status_code=404, detail=f"unknown agent: {agent}")

    pid = body.provider_id or None
    if pid:
        # 校验 provider_id 在兼容列表里——避免用户写错 id 进去无声失败
        compatible_ids = {p["id"] for p in list_compatible_providers(agent)}
        if pid not in compatible_ids:
            raise HTTPException(
                status_code=400,
                detail=f"provider '{pid}' not compatible with {agent}; compatible: {sorted(compatible_ids)}",
            )

    save_user_provider_override(agent, pid)
    return agent_availability(agent)


@router.post("/delegate/{delegate_id}/cancel")
async def cancel_delegate_route(delegate_id: str) -> Dict[str, Any]:
    """取消正在运行的 ACP delegate。

    前端 AcpDelegateCard 上的"停止"按钮 POST 到这里。
    幂等：已结束 / 不存在的 delegate_id 也返 200，cancelled=False；
    真正发出 cancel 信号返 cancelled=True。
    """
    from backend.acp.tools.delegate_acp_agent import cancel_delegate

    ok = cancel_delegate(delegate_id)
    return {"delegate_id": delegate_id, "cancelled": ok}
