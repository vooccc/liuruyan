"""ACP Client 服务：spawn 外部 ACP agent 子进程，跑一轮 prompt，收输出。

Phase 5 MVP：每次调用都 spawn 新进程。Phase 5.5 加 connection 复用（按 agent+cwd key）。

外部 agent 配置（hardcoded MVP，Phase 5.5 改 JSON 配置文件）：
- "claude-code": `npx -y @zed-industries/claude-code-acp`
- "gemini-cli":  `gemini --acp`  （未实测）
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

try:
    import acp
    from acp.schema import TextContentBlock
    ACP_AVAILABLE = True
except ImportError:
    acp = None
    TextContentBlock = None
    ACP_AVAILABLE = False

from .client_impl import LiuruyanACPClient

logger = logging.getLogger(__name__)


# 外部 agent 注册表（Phase 5.5 改成 liuruyan_acp_config.json 可配置）
#
# provider_id：标识本 agent 应该从哪个柳如烟 provider 借用 api_key。
# 用户在「模型服务」页面配过对应 provider 后自动可用，不需要再单独填一遍 API key。
# 优先级：os.environ > 同 env_required 名的环境变量已设 → 用之；否则查 provider api_key。
EXTERNAL_AGENTS: Dict[str, Dict[str, Any]] = {
    "claude-code": {
        "command": "npx",
        "args": ["-y", "@zed-industries/claude-code-acp"],
        "env_required": ["ANTHROPIC_API_KEY"],  # 启动前检查
        "provider_id": "anthropic",  # 从 Anthropic provider 借 api_key
        # 中转 key 兼容：把 provider.base_url 注入到 ANTHROPIC_BASE_URL 让 SDK 走中转
        # 官方端点（https://api.anthropic.com）时这个变量等同于无副作用
        "provider_base_url_env": "ANTHROPIC_BASE_URL",
        "description": "Anthropic Claude Code 官方 ACP adapter（Zed 维护）",
    },
    "gemini-cli": {
        "command": "gemini",
        "args": ["--acp"],
        "env_required": ["GEMINI_API_KEY"],
        "provider_id": "gemini",  # 从 Gemini provider 借 api_key
        "provider_base_url_env": "GEMINI_BASE_URL",  # 同理支持 Gemini 中转
        "description": "Google Gemini CLI ACP 模式（未实测）",
    },
}


def list_external_agents() -> List[str]:
    return list(EXTERNAL_AGENTS.keys())


def _resolve_command_path(command: str) -> Optional[str]:
    """在 PATH 里找命令的完整路径，处理 Windows .cmd / .bat 自动补全。

    背景：Windows 上 npx 实际是 npx.cmd（npm 装的全局 CLI 都这样）。
    asyncio.create_subprocess_exec 走 CreateProcess 不走 PATHEXT 解析，
    传 "npx" 会 [WinError 2]。shutil.which 会正确按 PATHEXT 找到 npx.cmd。

    返回 None 表示 PATH 里压根没有这个命令。
    """
    return shutil.which(command)


# 各 provider_id 对应的"协议名"——fallback 扫描时用 transport 字段匹配自定义中转 provider
_PROVIDER_TRANSPORT_HINT = {
    "anthropic": "anthropic_messages",
    # gemini-cli 暂时没找 fallback transport hint，因为 Gemini 没自定义中转的常规模式
}


# ────────────────────────────────────────────────────────────────────
# 用户偏好：每个 ACP agent 显式选哪个 provider 借 key
# ────────────────────────────────────────────────────────────────────
def _load_user_provider_overrides() -> Dict[str, str]:
    """读 acp_external_agents.json 里用户为每个 agent 选的 provider_id。"""
    try:
        from backend.paths import acp_external_agents_config_path
        import json
        p = acp_external_agents_config_path()
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        raw = data.get("providers") or {}
        return {k: str(v) for k, v in raw.items() if isinstance(v, str) and v}
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to load acp_external_agents.json: %s", exc)
        return {}


def save_user_provider_override(agent: str, provider_id: Optional[str]) -> None:
    """写入用户偏好。provider_id=None 表示清除偏好（回退到默认）。"""
    from backend.paths import acp_external_agents_config_path
    import json
    p = acp_external_agents_config_path()
    data: Dict[str, Any] = {"providers": {}}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or data
        except Exception:  # noqa: BLE001
            pass
    providers: Dict[str, str] = data.get("providers") or {}
    if provider_id:
        providers[agent] = provider_id
    else:
        providers.pop(agent, None)
    data["providers"] = providers
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("acp provider override saved: agent=%s provider=%s", agent, provider_id)


def _resolve_effective_provider_id(agent: str, agent_cfg: Dict[str, Any]) -> Optional[str]:
    """先看用户覆盖（acp_external_agents.json），没有就用 EXTERNAL_AGENTS 里写死的 provider_id。"""
    overrides = _load_user_provider_overrides()
    if agent in overrides:
        return overrides[agent]
    return agent_cfg.get("provider_id")


def list_compatible_providers(agent: str) -> List[Dict[str, Any]]:
    """返回给前端下拉用：和该 agent 协议兼容的所有 provider。

    匹配规则：
    - 内置 provider_id 完全匹配（如 "anthropic"）一定算
    - transport == _PROVIDER_TRANSPORT_HINT[default_provider_id] 的所有 provider 也算
      （兼容用户自建的中转 provider）
    """
    if agent not in EXTERNAL_AGENTS:
        return []
    cfg = EXTERNAL_AGENTS[agent]
    default_pid = cfg.get("provider_id") or ""
    transport_hint = _PROVIDER_TRANSPORT_HINT.get(default_pid)

    try:
        from backend.providers.store import load_providers_config
        full = load_providers_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_compatible_providers: load_providers_config failed: %s", exc)
        return []

    rows: List[Dict[str, Any]] = []
    for pid, pc in (full.get("providers") or {}).items():
        is_default = (pid == default_pid)
        is_transport_match = (transport_hint and getattr(pc, "transport", "") == transport_hint)
        if not (is_default or is_transport_match):
            continue
        if not getattr(pc, "enabled", True):
            continue
        rows.append({
            "id": pid,
            "name": getattr(pc, "name", pid) or pid,
            "transport": getattr(pc, "transport", "") or "",
            "base_url": getattr(pc, "base_url", "") or "",
            "has_key": bool(getattr(pc, "api_key", "")),
            "is_default": is_default,
        })
    # 默认 provider 排第一，其它按 id 字典序
    rows.sort(key=lambda r: (not r["is_default"], r["id"]))
    return rows


def _get_provider_credentials(provider_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """从柳如烟 providers store 拿 (api_key, base_url)。失败/缺失返 (None, None)，不抛错。

    Fallback 链（解决内置 anthropic provider freeze_url 不能改 base_url 的问题）：
    1. 先按 provider_id 直接查（内置 anthropic / gemini）
    2. 直接查没 key 时，按 transport 扫描自定义中转 provider
       （eg. 用户建的"Anthropic 中转"自定义 provider，transport=anthropic_messages）
    3. 还没有 → 返回 (None, None)

    设计：providers store 故障不能阻塞 ACP；最差降级到"请填 env var"提示。
    base_url 用来支持中转 key（用户填非官方端点时让 SDK 走中转）。
    """
    if not provider_id:
        return None, None
    try:
        from backend.providers.store import get_provider, load_providers_config

        # 第 1 层：按 id 直查
        pc = get_provider(provider_id)
        if pc and pc.api_key:
            return (pc.api_key, pc.base_url or None)

        # 第 2 层：按 transport fallback——扫自定义 provider
        # 适用场景：用户的中转 key 没法塞进内置 anthropic（freeze_url=true），
        # 改建自定义 provider transport=anthropic_messages，ACP 自动捡这个用。
        transport_hint = _PROVIDER_TRANSPORT_HINT.get(provider_id)
        if transport_hint:
            cfg = load_providers_config()
            for pid, candidate in cfg.get("providers", {}).items():
                if pid == provider_id:
                    continue  # 已经在第 1 层查过
                if getattr(candidate, "transport", "") != transport_hint:
                    continue
                if not getattr(candidate, "enabled", True):
                    continue
                if candidate.api_key:
                    logger.info(
                        "ACP provider '%s' fallback to custom '%s' (transport=%s)",
                        provider_id, pid, transport_hint,
                    )
                    return (candidate.api_key, candidate.base_url or None)
    except Exception as exc:
        logger.warning("failed to lookup provider '%s' for ACP env: %s", provider_id, exc)
    return None, None


def _get_provider_api_key(provider_id: Optional[str]) -> Optional[str]:
    """向后兼容 wrapper：单独取 api_key。"""
    api_key, _ = _get_provider_credentials(provider_id)
    return api_key


def resolve_spawn_env(agent_cfg: Dict[str, Any], agent_name: Optional[str] = None) -> Tuple[Dict[str, str], List[str]]:
    """为 spawn 子进程准备环境变量。

    步骤：
    1. 起点 = os.environ.copy()
    2. 对每个 env_required：若 os.environ 已有，跳过（让 ops 能 override）
    3. 否则尝试从 provider_id（先看用户偏好，再默认）借 api_key 注入到第一个未设的 env_required 名

    返回 (env_dict, missing_after_resolve)；missing 非空时调用方应 abort。
    """
    env = os.environ.copy()
    required = list(agent_cfg.get("env_required", []) or [])
    # agent_name 传入时优先看用户偏好；不传时退化到 EXTERNAL_AGENTS 默认（向后兼容老 caller）
    provider_id = _resolve_effective_provider_id(agent_name, agent_cfg) if agent_name else agent_cfg.get("provider_id")
    base_url_env = agent_cfg.get("provider_base_url_env")  # 中转 key 兼容用

    api_key, base_url = _get_provider_credentials(provider_id)

    # 注入 base_url：仅当用户没在 os.environ 里手动设过 + provider 配了非空 base_url
    # （用户在 os.environ 里手动设的优先级最高，让 ops 能 override）
    if base_url_env and base_url and not env.get(base_url_env):
        env[base_url_env] = base_url

    # 注入 api_key
    missing = [v for v in required if not env.get(v)]
    if not missing:
        return env, []

    if api_key:
        # 把 provider key 注入到所有缺的 env_required 项（通常只有 1 个）
        for v in missing:
            env[v] = api_key
        return env, []

    return env, missing


def agent_availability(agent: str) -> Dict[str, Any]:
    """返回某 agent 当前是否可用 + 缺什么。给 GET /acp/external_agents 路由用。

    Returns:
        {
            "agent": str,
            "description": str,
            "command": str,
            "env_required": [str, ...],
            "provider_id": str | None,
            "available": bool,
            "missing_env": [str, ...],   # 解析完 provider 后还缺什么
            "resolved_via": "env" | "provider" | "none",
        }
    """
    if agent not in EXTERNAL_AGENTS:
        return {"agent": agent, "available": False, "error": "unknown agent"}
    cfg = EXTERNAL_AGENTS[agent]
    required = list(cfg.get("env_required", []) or [])
    # provider_id：内置默认；effective_provider_id：算上用户覆盖后真正会用的那个
    default_provider_id = cfg.get("provider_id")
    effective_provider_id = _resolve_effective_provider_id(agent, cfg)

    env_has_all = required and all(os.environ.get(v) for v in required)
    provider_key = _get_provider_api_key(effective_provider_id) if not env_has_all else None
    resolved_via = "env" if env_has_all else ("provider" if provider_key else "none")
    _, missing = resolve_spawn_env(cfg, agent_name=agent)
    if not ACP_AVAILABLE:
        missing.append("agent-client-protocol SDK 未安装或未打包")

    return {
        "agent": agent,
        "description": cfg.get("description", ""),
        "command": cfg["command"],
        "env_required": required,
        "provider_id": default_provider_id,
        "effective_provider_id": effective_provider_id,
        "compatible_providers": list_compatible_providers(agent),
        "available": len(missing) == 0,
        "missing_env": missing,
        "resolved_via": resolved_via,
    }


def _summarize_collected(updates: List[Any]) -> str:
    """把外部 agent 推流的 session/update 列表汇总成一段 markdown 文本给 LLM。"""
    parts: List[str] = []
    text_buf: List[str] = []  # agent_message_chunk 累积

    def _flush_text():
        if text_buf:
            joined = "".join(text_buf).strip()
            if joined:
                parts.append(joined)
            text_buf.clear()

    for u in updates:
        kind = getattr(u, "session_update", None) or ""
        if kind == "agent_message_chunk":
            content = getattr(u, "content", None)
            text = getattr(content, "text", "") if content is not None else ""
            text_buf.append(text)
        elif kind == "agent_thought_chunk":
            # MVP 不展示思考链
            continue
        elif kind == "tool_call":
            _flush_text()
            title = getattr(u, "title", "?")
            kind_field = getattr(u, "kind", "other")
            parts.append(f"[tool_call {kind_field}] {title}")
        elif kind == "tool_call_update":
            _flush_text()
            status = getattr(u, "status", None)
            tc_id = getattr(u, "tool_call_id", "?")
            parts.append(f"[tool_done {status}] {tc_id}")
        # 其它 kind 暂忽略
    _flush_text()

    if not parts:
        return "(外部 agent 一字未出)"
    return "\n\n".join(parts)


async def run_external_agent_turn(
    *,
    agent: str,
    prompt: str,
    cwd: Optional[str] = None,
    timeout_sec: float = 600.0,
    on_chunk: Optional[Callable[[str, dict], Awaitable[None]]] = None,
) -> dict:
    """跑一次外部 agent 的完整对话回合，返回收集的输出。

    Args:
        agent: EXTERNAL_AGENTS 里的 key
        prompt: 给外部 agent 的用户消息
        cwd: 外部 agent 的工作目录（None = 当前进程 cwd）
        timeout_sec: 整轮超时（spawn + initialize + new_session + prompt 合计）

    Returns:
        {"success": True/False, "agent": ..., "output": str, "raw_update_count": int, "error": str?}
    """
    if agent not in EXTERNAL_AGENTS:
        return {
            "success": False,
            "agent": agent,
            "error": f"Unknown agent '{agent}'. Available: {list_external_agents()}",
        }

    if not ACP_AVAILABLE:
        return {
            "success": False,
            "agent": agent,
            "error": "ACP 协议库 (agent-client-protocol) 未安装或未打包，无法启动外部 agent。",
        }

    cfg = EXTERNAL_AGENTS[agent]

    # 环境变量解析：os.environ 优先，缺的从 provider_id 借（agent_name 让 _resolve_effective_provider_id 看用户偏好）
    spawn_env, missing_env = resolve_spawn_env(cfg, agent_name=agent)
    if missing_env:
        provider_id = _resolve_effective_provider_id(agent, cfg)
        hint = (
            f"未配置 {provider_id} provider 的 API key"
            if provider_id
            else "请在系统环境变量里设置"
        )
        return {
            "success": False,
            "agent": agent,
            "error": (
                f"Missing required env vars: {missing_env}. "
                f"Hint: {hint}（可在「设置 → 模型服务」里配置 {provider_id} provider 并填 API key，"
                f"或直接设置系统环境变量 {missing_env[0]}）。"
            ),
        }

    # Windows 上把 "npx" 解析成 "C:\...\npx.cmd"——CreateProcess 不走 PATHEXT
    resolved_command = _resolve_command_path(cfg["command"])
    if not resolved_command:
        return {
            "success": False,
            "agent": agent,
            "error": (
                f"Command '{cfg['command']}' not found in PATH. "
                f"For claude-code: 装一下 Node.js（含 npx）：https://nodejs.org/，"
                f"然后重启柳如烟让新 PATH 生效。"
            ),
        }

    client_impl = LiuruyanACPClient(allow_write=True, on_chunk=on_chunk)
    workdir = cwd or os.getcwd()
    logger.info(
        "spawning external ACP agent '%s' (command=%s, resolved=%s, cwd=%s)",
        agent,
        cfg["command"],
        resolved_command,
        workdir,
    )

    try:
        async with acp.spawn_agent_process(
            client_impl,
            resolved_command,
            *cfg["args"],
            env=spawn_env,
            cwd=workdir,
            # 默认 StreamReader 缓冲是 64KB，外部 agent（如 claude-code-acp）
            # 单条 JSON-RPC 行可能 >>1MB（整文件读回结果、大 tool_call.input 等）。
            # 不调大就会 LimitOverrunError → Connection closed。16MB 给足。
            transport_kwargs={"limit": 16 * 1024 * 1024},
        ) as (conn, process):
            return await asyncio.wait_for(
                _run_turn(conn, process, agent, prompt, workdir, client_impl),
                timeout=timeout_sec,
            )
    except asyncio.TimeoutError:
        return {
            "success": False,
            "agent": agent,
            "error": f"Timeout after {timeout_sec}s",
            "raw_update_count": len(client_impl.collected_updates),
            "output": _summarize_collected(client_impl.collected_updates),
        }
    except FileNotFoundError as exc:
        return {
            "success": False,
            "agent": agent,
            "error": (
                f"Failed to spawn '{cfg['command']}': {exc}. "
                f"Make sure it's installed and on PATH (for claude-code: `npm i -g @zed-industries/claude-code-acp` "
                f"or use npx)."
            ),
        }
    except Exception as exc:
        logger.exception("run_external_agent_turn failed for %s", agent)
        return {
            "success": False,
            "agent": agent,
            "error": f"{type(exc).__name__}: {exc}",
            "raw_update_count": len(client_impl.collected_updates),
            "output": _summarize_collected(client_impl.collected_updates),
        }


async def _run_turn(
    conn,
    process,  # noqa: ARG001
    agent: str,
    prompt: str,
    workdir: str,
    client_impl: LiuruyanACPClient,
) -> dict:
    """完整 ACP 握手 + prompt 流程。"""
    # 1. initialize
    init_resp = await conn.initialize(
        protocol_version=1,
        client_capabilities={
            "fs": {"read_text_file": True, "write_text_file": True},
            "terminal": False,
        },
        client_info={"name": "liuruyan", "title": "柳如烟", "version": "0.1"},
    )
    agent_info = getattr(init_resp, "agent_info", None)
    agent_name = getattr(agent_info, "name", "?") if agent_info else "?"
    logger.info("external agent self-reported: name=%s", agent_name)

    # 2. new_session
    new_resp = await conn.new_session(cwd=workdir, mcp_servers=[])
    sid = new_resp.session_id

    # 3. prompt
    await conn.prompt(
        prompt=[TextContentBlock(type="text", text=prompt)],
        session_id=sid,
    )

    # session_update 都已经在过程中累积到 client_impl.collected_updates
    output = _summarize_collected(client_impl.collected_updates)
    return {
        "success": True,
        "agent": agent,
        "external_agent_name": agent_name,
        "session_id": sid,
        "raw_update_count": len(client_impl.collected_updates),
        "permission_decisions": client_impl.permission_decisions,
        "output": output,
    }
