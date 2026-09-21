"""工具检视台 + 白名单浅编辑 路由。

设计契约（见对话设计文档）：
- 单一事实源：`backend.tools.builtin.clean_registry.registry`。
- UI 只是 registry 的镜像视图；**禁止**通过 UI 修改 description / input_schema。
- Tier 2 浅编辑只改 agent meta.json["tools"] 白名单这一个字段，不碰 prompt 资产。
- Tier 3（编辑 description / 自定义工具）显式不在此模块实现：风险见对话记录。
  若未来要做：必须 dev-mode flag + 鉴权门，且 description 改动须有版本管理 + fallback。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from backend.agent_config import get_agent_manager
from backend.dev_mode import (
    get_dev_mode_state,
    is_dev_mode_enabled,
    require_dev_mode,
    set_runtime_toggle,
)
from backend.tool_overrides import (
    apply_overrides_to_registry,
    get_baseline,
    is_overridden,
    set_override,
)
from backend.tools.builtin.clean_registry import registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tools-inventory"])


def _classify_source(name: str) -> str:
    """按命名前缀粗分来源。MCP/IDE 工具命名见 mcp/tool_bridge.py 与 ide_* 注册点。"""
    if name.startswith("mcp__"):
        return "mcp"
    if name.startswith("ide_"):
        return "ide"
    return "builtin"


def _approx_tokens(char_count: int) -> int:
    """粗估 prompt token 数。

    中英混合 description 平均约 3.5 chars/token（纯英文 4，纯中文 1.5-2）。
    只用作 UI 量级提示，不参与任何业务决策。
    """
    if char_count <= 0:
        return 0
    return max(1, int(char_count / 3.5))


@router.get("/tools/inventory")
async def get_tools_inventory():
    """返回 registry 当前状态镜像 + 每 agent 白名单聚合视图。"""
    mgr = get_agent_manager()
    raw_agents = mgr.list_agents()

    # 提前抓每 agent 的 allowed_tools（None = 未配置 = 全开）
    agent_allow_map: Dict[str, Optional[List[str]]] = {}
    agent_meta_map: Dict[str, Dict[str, Any]] = {}
    for a in raw_agents:
        if not isinstance(a, dict):
            continue
        aid = a.get("id")
        if not aid:
            continue
        agent_allow_map[aid] = mgr.get_allowed_tools(aid)
        agent_meta_map[aid] = a

    tools_payload: List[Dict[str, Any]] = []
    for name, tool in registry.tools.items():
        desc = tool.get("description") or ""
        schema = tool.get("input_schema") or {}
        source = _classify_source(name)

        # 反查：哪些 agent 显式白名单包含此工具
        # 未配置白名单（None）的 agent 不出现在 chip 里——避免视觉污染，
        # 用户在 agents 区块独立看到那些是"全开"。
        allowed_in: List[str] = []
        for aid, allow in agent_allow_map.items():
            if allow is None:
                continue
            if name in allow:
                allowed_in.append(aid)

        tools_payload.append({
            "name": name,
            "description": desc,
            "input_schema": schema,
            "source": source,
            "concurrency_class": tool.get("concurrency_class") or "exclusive",
            "description_chars": len(desc),
            "approx_tokens": _approx_tokens(len(desc)),
            "allowed_in_agents": allowed_in,
            # dev-mode override 状态：UI 据此判断是否显示「编辑 / 重置」按钮。
            # 仅 builtin 工具允许编辑；MCP/IDE 描述来自外部源不在此暴露。
            "is_overridden": is_overridden(name) if source == "builtin" else False,
            "baseline_description": get_baseline(name) if source == "builtin" else None,
            "can_edit_description": source == "builtin",
        })

    source_order = {"builtin": 0, "ide": 1, "mcp": 2}
    tools_payload.sort(key=lambda t: (source_order.get(t["source"], 9), t["name"]))

    totals: Dict[str, int] = {"builtin": 0, "ide": 0, "mcp": 0, "total": len(tools_payload)}
    for t in tools_payload:
        totals[t["source"]] = totals.get(t["source"], 0) + 1

    # 每 agent 的预算估算
    agents_payload: List[Dict[str, Any]] = []
    for aid, meta in agent_meta_map.items():
        allow = agent_allow_map.get(aid)
        if allow is None:
            tools_for_agent = list(registry.tools.keys())
            scope = "all"
        else:
            tools_for_agent = [n for n in allow if n in registry.tools]
            scope = "whitelist"
        desc_chars = sum(
            len((registry.tools.get(n) or {}).get("description") or "")
            for n in tools_for_agent
        )
        agents_payload.append({
            "id": aid,
            "name": meta.get("name") or aid,
            "avatar": meta.get("avatar"),
            "avatar_url": meta.get("avatar_url"),
            "allowed_tools": allow,
            "scope": scope,
            "tool_count": len(tools_for_agent),
            "description_chars": desc_chars,
            "approx_tokens": _approx_tokens(desc_chars),
        })
    agents_payload.sort(key=lambda a: a.get("name") or "")

    return {
        "tools": tools_payload,
        "agents": agents_payload,
        "totals": totals,
        "dev_mode": is_dev_mode_enabled(),
        "dev_mode_state": get_dev_mode_state(),
    }


@router.get("/dev/status")
async def get_dev_status():
    """返回 dev mode 详细状态（via_env / via_runtime_toggle / can_toggle_off）。

    前端用此判断：
    - enabled=True 且 via_env=True → 显示「env 模式」标签，不给 toggle off 按钮
    - enabled=True 且 via_runtime_toggle=True → 显示「已开启」+「退出 dev」按钮
    - enabled=False → 不显示 dev UI；用户可在 hero stat 手势激活
    """
    return get_dev_mode_state()


@router.post("/dev/toggle")
async def toggle_dev_mode(body: Dict[str, Any] = Body(...)):
    """切换运行时 dev toggle。

    **本端点无 dev mode 守门**——这是进入 dev 模式的入口本身。
    保护机制是前端的「点击 5 次」UX 手势：避免误触；非 dev 用户不会去翻 API。
    单机本地应用语境下足够。

    Body: {"enabled": true|false}

    若 env LIURUYAN_DEV_MODE=1 已开，关闭 toggle 不会真关闭（env 优先），
    返回值 `can_toggle_off=false` 提示前端。
    """
    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="missing 'enabled' field")
    enabled = bool(body["enabled"])
    state = set_runtime_toggle(enabled)
    return {"status": "success", **state}


@router.put(
    "/tools/inventory/{tool_name}/description",
    dependencies=[Depends(require_dev_mode)],
)
async def update_tool_description(tool_name: str, body: Dict[str, Any] = Body(...)):
    """覆盖 / 重置内置工具的 description（仅 dev mode）。

    Body:
        {"description": "..."}  → 写入覆盖，立即影响后续 LLM 调用拿到的工具描述
        {"description": null}    → 清除覆盖，恢复源码原始 description

    注意：只支持 builtin 工具。MCP/IDE 工具的 description 是外部来源，改了
    会被下次刷新覆盖，没意义，所以直接 400 拒绝。
    """
    if tool_name not in registry.tools:
        raise HTTPException(status_code=404, detail=f"tool '{tool_name}' not found")
    if _classify_source(tool_name) != "builtin":
        raise HTTPException(
            status_code=400,
            detail="only builtin tools support description override",
        )
    if "description" not in body:
        raise HTTPException(status_code=400, detail="missing 'description' field")
    desc = body["description"]
    if desc is not None and not isinstance(desc, str):
        raise HTTPException(status_code=400, detail="'description' must be string or null")
    try:
        result = set_override(tool_name, desc)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"tool '{tool_name}' not found")
    return {"status": "success", **result}


# ------------- 启动时应用持久化 override -------------
# 模块在 main.py 装配 router 时被 import，此时 builtin 工具已全部完成 register。
# MCP 工具是 lifespan 后动态注册的，但我们也不覆盖 MCP 工具，所以时机不冲突。
try:
    _applied = apply_overrides_to_registry()
    if _applied:
        logger.info("[TOOL-OVERRIDES] applied %s description override(s) at startup", _applied)
except Exception as exc:  # noqa: BLE001
    # 启动期不能因为 override 文件损坏整段挂掉
    logger.warning("[TOOL-OVERRIDES] failed to apply overrides at startup: %s", exc)


@router.patch("/agents/{agent_id}/allowed_tools")
async def update_agent_allowed_tools(agent_id: str, body: Dict[str, Any] = Body(...)):
    """更新 agent meta.json["tools"] 白名单。

    Body:
        {"tools": ["read_file", ...]}  → 写白名单
        {"tools": null}                → 清除白名单 = 全开

    校验：
    - 未注册的工具名直接 400 拒绝写入，避免用户拼错把白名单清空导致 agent 没工具可用。
    - MCP 工具命名（mcp__server__tool）也允许，只要当前已注册到 registry。
    """
    mgr = get_agent_manager()
    config = mgr.get_agent_config(agent_id)
    if config is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")

    if "tools" not in body:
        raise HTTPException(status_code=400, detail="missing 'tools' field")

    new_tools = body["tools"]
    meta = config.load_meta()
    old_tools_raw = meta.get("tools") if isinstance(meta.get("tools"), list) else []
    old_set = {str(t) for t in old_tools_raw}

    if new_tools is not None:
        if not isinstance(new_tools, list):
            raise HTTPException(status_code=400, detail="'tools' must be list or null")
        # 只校验"本次新增"的工具是否在 registry 里。
        # 老白名单里残留的未注册工具（比如旧版本曾有的 execute_powershell）允许 grandfather，
        # 否则用户每次想保存都被一个无关的老条目挡住，体验崩。
        # 保留 = 不卡；新加的 = 必须能被识别（防拼错）
        added = [t for t in new_tools if str(t) not in old_set]
        unknown_new = [t for t in added if t not in registry.tools]
        if unknown_new:
            raise HTTPException(
                status_code=400,
                detail=f"unknown tools (not registered in registry): {unknown_new}",
            )
        # 去重保序
        seen = set()
        deduped = []
        for t in new_tools:
            t = str(t)
            if t in seen:
                continue
            seen.add(t)
            deduped.append(t)
        new_tools = deduped

    if new_tools is None:
        meta.pop("tools", None)
    else:
        meta["tools"] = new_tools
    config.save_meta(meta)

    return {
        "status": "success",
        "agent_id": agent_id,
        "allowed_tools": new_tools,
    }
