"""LLM 请求 default headers 集中管理。

设计动机
========
柳如烟用 Python OpenAI / Anthropic SDK 调 LLM。针对不同协议与中转网关：
1. OpenAI 协议（transport='openai_chat'）：
   默认伪装为 Codex 官方客户端（codex_cli_rs/0.153.0），带 originator 与 x-codex-window-id；
2. Anthropic 协议（transport='anthropic_messages'）：
   默认伪装为 Claude Code CLI v2.1.263 客户端，带 X-App: cli 与 anthropic-version: 2023-06-01，
   无缝兼容要求 Claude Code 客户端验证的反代网关（如 Sub2API / api.zzzcoding.org）与官方接口；
3. 通用 fallback：
   兼容 Cherry Studio 浏览器 UA，防止未指定协议的通用路由被拦截。

Override 优先级
===============
caller 传的 extra_headers > 默认 headers。同 key 时 caller 完全覆盖，不做"合并"
(避免半成品 header 难以 debug)。
"""
from __future__ import annotations

import functools
import platform
from typing import Dict, Optional


# Chrome 主版本号（用于通用浏览器 UA 备选）
_CHROME_MAJOR = 145
_APP_BRAND = "Lry"
_APP_VERSION = "1.0"

# OpenAI 协议默认客户端特征（Codex 官方客户端特征）
DEFAULT_CODEX_USER_AGENT = "codex_cli_rs/0.153.0 (Windows 10.0.26100; x86_64) WindowsTerminal"
DEFAULT_CODEX_ORIGINATOR = "codex_cli_rs"
DEFAULT_CODEX_WINDOW_ID = "7881613d-4d94-4e5e-827d-fb8f00a4c3b4"

# Anthropic 协议默认纯净头（标准原生，不默认注入 Claude Code 伪装）
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_BETA = "prompt-caching-2024-07-31"

# Claude Code 伪装预设（供前端一键应用或特殊站点选用）
DEFAULT_CLAUDE_CLI_USER_AGENT = "claude-cli/2.1.263 (Claude Code, external, cli)"
DEFAULT_CLAUDE_CLI_APP = "cli"
DEFAULT_CLAUDE_CODE_BETA = "claude-code-20250219,prompt-caching-2024-07-31,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"

CLAUDE_CODE_PRESET_HEADERS = {
    "User-Agent": DEFAULT_CLAUDE_CLI_USER_AGENT,
    "X-App": DEFAULT_CLAUDE_CLI_APP,
    "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
    "anthropic-beta": DEFAULT_CLAUDE_CODE_BETA,
}


def get_claude_code_preset_headers() -> Dict[str, str]:
    """返回 Claude Code 客户端伪装预设请求头。"""
    return dict(CLAUDE_CODE_PRESET_HEADERS)


@functools.lru_cache(maxsize=1)
def _build_user_agent() -> str:
    """生成 Cherry-style User-Agent 字符串。"""
    system = platform.system()
    machine = platform.machine() or "x86_64"

    if system == "Windows":
        os_string = "Windows NT 10.0; Win64; x64"
    elif system == "Darwin":
        os_string = "Macintosh; Intel Mac OS X 10_15_7"
    elif system == "Linux":
        os_string = f"X11; Linux {machine}"
    else:
        os_string = f"{system}; {machine}"

    return (
        f"Mozilla/5.0 ({os_string}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36 "
        f"{_APP_BRAND}/{_APP_VERSION}"
    )


def get_openai_llm_headers() -> Dict[str, str]:
    """返回 OpenAI 协议默认客户端特征请求头（Codex 客户端）。"""
    return {
        "User-Agent": DEFAULT_CODEX_USER_AGENT,
        "originator": DEFAULT_CODEX_ORIGINATOR,
        "x-codex-window-id": DEFAULT_CODEX_WINDOW_ID,
    }


def get_anthropic_llm_headers() -> Dict[str, str]:
    """返回 Anthropic 协议默认请求头（Lry 浏览器客户端特征防 WAF 拦截 + 官方协议版本与缓存标）。"""
    return {
        "User-Agent": _build_user_agent(),
        "HTTP-Referer": "https://lry.local",
        "X-Title": "Lry",
        "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
        "anthropic-beta": DEFAULT_ANTHROPIC_BETA,
    }


def get_default_llm_headers(transport: Optional[str] = None) -> Dict[str, str]:
    """返回项目级默认 LLM headers（每次返新 dict，调用方可放心 mutate）。

    Args:
        transport: 传输协议标识。
            - "anthropic_messages" / "anthropic": 返回 Claude Code 默认请求头
            - "openai_chat" / "openai" / None: 返回 Codex 默认请求头
    """
    if transport:
        t = transport.lower()
        if "anthropic" in t:
            return get_anthropic_llm_headers()
        if "openai" in t:
            return get_openai_llm_headers()
    return get_openai_llm_headers()


def merge_default_llm_headers(
    extra_headers: Optional[Dict[str, str]] = None,
    transport: Optional[str] = None,
) -> Dict[str, str]:
    """合并：默认 headers + caller 自定义 headers。

    caller 同 key 完全覆盖默认值。
    """
    merged = get_default_llm_headers(transport=transport)
    if extra_headers:
        merged.update(extra_headers)
    return merged
