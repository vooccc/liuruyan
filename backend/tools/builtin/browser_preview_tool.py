"""browser_preview：让 agent 在 chat 中嵌入一个 URL 的 iframe 预览。

设计取舍：
- 不做服务端 headless 浏览器（playwright/puppeteer）：单人桌面应用，绝大多数场景是
  "agent 起了本地 dev server，让用户看一眼"，不需要无头模式
- iframe 由前端渲染，工具只负责返结构化 JSON 含 url
- 走"工具结果含 sentinel"模式（参考 AskUserQuestionCard 同款）：
    tool_result.result = JSON {"_runtime_block": "browser_preview", url, name, ...}
  前端 runtimeBlocks 识别 sentinel → 渲染 BrowserPreviewCard 取代默认 tool result UI

URL 安全：
- 仅 http/https；file:// / ws:// / data: 一律拒绝
- localhost dev server 标 is_localhost=true，给前端友好提示"关掉 server 后预览会白屏"
- 不限制内网 IP（用户自己机器，预览自己的服务是合法用例）

前端配套：
- frontend/src/components/BrowserPreviewCard.tsx
- frontend/src/components/runtimeBlocks.tsx 加 tryParseBrowserPreviewPayload 分支
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 允许的 scheme：http(s) 才能在 iframe 里加载
_ALLOWED_SCHEMES = {"http", "https"}

# 内置 dev server 端口提示用（不是限制）：让 is_localhost=true 时前端能加额外说明
_DEV_PORTS = {3000, 3001, 4173, 5173, 5174, 8000, 8080, 8081, 8001, 9000, 4200, 8888}


def _validate_url(url: str) -> tuple[bool, str]:
    """返回 (ok, reason)。"""
    if not url or not isinstance(url, str):
        return False, "url required (non-empty string)"
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"invalid URL: {exc}"
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False, f"only http/https allowed, got: {parsed.scheme!r}"
    if not parsed.netloc:
        return False, "URL missing host"
    return True, ""


async def browser_preview_tool(
    url: str,
    name: Optional[str] = None,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    url = (url or "").strip() if url else ""
    ok, reason = _validate_url(url)
    if not ok:
        return json.dumps({"error": f"URL rejected: {reason}"}, ensure_ascii=False)

    parsed = urlparse(url)
    is_localhost = parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
    is_dev_port = bool(parsed.port and parsed.port in _DEV_PORTS)

    preview_id = f"prev_{uuid.uuid4().hex[:8]}"
    # 友好显示名：用户传了用用户的；没传 → host[:port]/path 截 60 char
    if name and isinstance(name, str) and name.strip():
        final_name = name.strip()[:60]
    else:
        port_part = f":{parsed.port}" if parsed.port else ""
        path_part = parsed.path or ""
        final_name = f"{parsed.hostname}{port_part}{path_part}"[:60]

    # 构造 instruction 给 LLM 看（让它知道发生了什么）
    if is_localhost:
        instruction = (
            f"已在聊天界面嵌入 '{final_name}' 的实时预览。"
            "用户可直接在卡片里看页面、点'新窗口打开'、或刷新。"
            "注意：这是本机 dev server，关掉 server 后预览会变白屏。"
        )
    else:
        instruction = (
            f"已在聊天界面嵌入 '{final_name}' 的实时预览。"
            "注意：部分网站设了 X-Frame-Options/CSP 拒绝被 iframe 嵌入，"
            "若用户看到空白，建议改用 web_extract 抓内容或让用户新窗口打开。"
        )

    # 返回带 sentinel 的 JSON：前端 runtimeBlocks 识别 _runtime_block 字段渲染特殊卡片
    return json.dumps({
        "_runtime_block": "browser_preview",
        "preview_id": preview_id,
        "url": url,
        "name": final_name,
        "is_localhost": is_localhost,
        "is_dev_port": is_dev_port,
        "ts": time.time(),
        "agent_id": agent_id,
        "session_id": session_id,
        "instruction": instruction,
    }, ensure_ascii=False)


# ============================================================================
# handler
# ============================================================================

_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


async def _handle_browser_preview(**kwargs):
    args = _drop(kwargs)
    return await browser_preview_tool(
        url=args.get("url", ""),
        name=args.get("name"),
        agent_id=kwargs.get("agent_id"),
        session_id=kwargs.get("session_id"),
    )


__all__ = ["browser_preview_tool", "_handle_browser_preview"]
