"""工具检视台 + 白名单浅编辑 路由。

设计契约（见对话设计文档）：
- 单一事实源：`backend.tools.builtin.clean_registry.registry`。
- UI 只是 registry 的镜像视图；**禁止**通过 UI 修改 description / input_schema。
- Tier 2 浅编辑只改 agent meta.json 的工具配置这一个关注点：
  `tool_preset`（档位预设 id，定义见 backend.tool_presets）或 `tools` 白名单列表，二者互斥。
- Tier 3（编辑 description / 自定义工具）显式不在此模块实现：风险见对话记录。
  若未来要做：必须 dev-mode flag + 鉴权门，且 description 改动须有版本管理 + fallback。
"""
from __future__ import annotations

import json
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
from backend.tool_presets import PRESETS, expand_preset
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


def _approx_tokens(text: str) -> int:
    """粗估 prompt token 数（加权估算，系数按 tiktoken 实测校准）。

    实现已抽到 backend.token_estimation 共享模块（context breakdown 也要用），
    这里保留薄别名维持模块内调用点不变。
    """
    from backend.token_estimation import approx_tokens

    return approx_tokens(text)


def _tool_token_estimate(desc: str, schema: Dict[str, Any]) -> int:
    """单工具 token 估算：description + input_schema 一起算。"""
    from backend.token_estimation import tool_token_estimate

    return tool_token_estimate(desc, schema)


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
    # 每工具 token 估算（含 schema），agent / 档位估算都从它累加，保证口径一致
    per_tool_tokens: Dict[str, int] = {}
    for name, tool in registry.tools.items():
        desc = tool.get("description") or ""
        schema = tool.get("input_schema") or {}
        source = _classify_source(name)
        approx = _tool_token_estimate(desc, schema)
        per_tool_tokens[name] = approx

        tools_payload.append({
            "name": name,
            "description": desc,
            "input_schema": schema,
            "source": source,
            "concurrency_class": tool.get("concurrency_class") or "exclusive",
            "description_chars": len(desc),
            "approx_tokens": approx,
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

    # 档位预设：后端算好工具数 / token 估算，前端只渲染不重复实现估算
    presets_payload: List[Dict[str, Any]] = []
    for pid, preset in PRESETS.items():
        ptools = [n for n in preset["tools"] if n in registry.tools]
        presets_payload.append({
            "id": pid,
            "name": preset["name"],
            "description": preset["description"],
            "tools": ptools,
            "tool_count": len(ptools),
            "approx_tokens": sum(per_tool_tokens.get(n, 0) for n in ptools),
        })

    # 每 agent 的预算估算
    agents_payload: List[Dict[str, Any]] = []
    for aid, meta in agent_meta_map.items():
        allow = agent_allow_map.get(aid)
        preset_id = mgr.get_tool_preset(aid)
        if preset_id and preset_id in PRESETS:
            tools_for_agent = [n for n in expand_preset(preset_id) if n in registry.tools]
            scope = "preset"
        elif allow is None:
            tools_for_agent = list(registry.tools.keys())
            scope = "all"
        else:
            tools_for_agent = [n for n in allow if n in registry.tools]
            scope = "whitelist"
        agents_payload.append({
            "id": aid,
            "name": meta.get("name") or aid,
            "avatar": meta.get("avatar"),
            "avatar_url": meta.get("avatar_url"),
            "allowed_tools": allow,
            "tool_preset": preset_id if preset_id in PRESETS else None,
            "scope": scope,
            "tool_count": len(tools_for_agent),
            "description_chars": sum(
                len((registry.tools.get(n) or {}).get("description") or "")
                for n in tools_for_agent
            ),
            "approx_tokens": sum(per_tool_tokens.get(n, 0) for n in tools_for_agent),
        })
    agents_payload.sort(key=lambda a: a.get("name") or "")

    return {
        "tools": tools_payload,
        "agents": agents_payload,
        "presets": presets_payload,
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
    """更新 agent 工具配置（档位预设 或 meta.json["tools"] 白名单，二选一互斥）。

    Body（三选一）:
        {"preset": "minimal"}         → 写 tool_preset 档位，清除 tools 列表
        {"tools": ["read_file", ...]} → 写自定义白名单，清除 tool_preset
        {"tools": null}               → 清除全部配置 = 全开

    校验：
    - preset 必须在 backend.tool_presets.PRESETS 里，否则 400。
    - 未注册的工具名直接 400 拒绝写入，避免用户拼错把白名单清空导致 agent 没工具可用。
    - MCP 工具命名（mcp__server__tool）也允许，只要当前已注册到 registry。
    """
    mgr = get_agent_manager()
    config = mgr.get_agent_config(agent_id)
    if config is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")

    if "tools" not in body and "preset" not in body:
        raise HTTPException(status_code=400, detail="body must contain 'tools' or 'preset'")

    meta = config.load_meta()

    # ---- 档位预设路径 ----
    if "preset" in body:
        preset_id = body["preset"]
        if preset_id is not None and preset_id not in PRESETS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown preset '{preset_id}' (available: {sorted(PRESETS)})",
            )
        if preset_id is None:
            meta.pop("tool_preset", None)
        else:
            meta["tool_preset"] = preset_id
            meta.pop("tools", None)  # 档位与自定义白名单互斥
        config.save_meta(meta)
        return {
            "status": "success",
            "agent_id": agent_id,
            "tool_preset": preset_id,
            "allowed_tools": None,
        }

    # ---- 自定义白名单路径 ----
    new_tools = body["tools"]
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
        meta.pop("tool_preset", None)
    else:
        meta["tools"] = new_tools
        meta.pop("tool_preset", None)  # 档位与自定义白名单互斥
    config.save_meta(meta)

    return {
        "status": "success",
        "agent_id": agent_id,
        "allowed_tools": new_tools,
    }
