"""API host URL 规范化工具——对齐 Cherry Studio 的 packages/shared/utils/api/utils.ts。

设计动机：
provider base_url 处理是 LLM 客户端的反复踩坑点：
- 用户填 `https://api.openai.com`（少 /v1）→ OpenAI SDK 拼 /chat/completions 落到 /chat/completions 404
- 用户填 `https://api.getrouter.top/v1`（多 /v1）→ Anthropic Python SDK 自动追加 /v1/messages 变 /v1/v1/messages 404
- 用户用第三方反代如 cloudflare gateway `/v1/xxxx/google-ai-studio/v1beta` 中间含 v1 末尾含 v1beta，简单 endsWith 判断会误伤

Cherry 的方案是**统一原语 + 一个 escape hatch**：
1. 末尾 `#` 是 "按字面 URL，不要再处理我"
2. URL.pathname 解析判断版本段（任意位置）
3. 仅末尾的 `/v\d+(alpha|beta)?` 才剥
4. `format_api_host` 综合上述，按 provider 需要选择是否自动补版本

本模块是该工具集的 Python 移植，用法对齐：
- Anthropic Python SDK 路径：用 `without_trailing_api_version`（SDK 自加 /v1/messages，
  base_url 必须不含 /v1）
- OpenAI 兼容路径：用 `format_api_host(host, support_api_version=True)`（SDK 不自加 /v1，
  base_url 必须含 /v1，缺则补）
- Gemini 路径：用 `format_api_host(host, True, api_version='v1beta')`
- Copilot / Perplexity / Azure：用 `format_api_host(host, support_api_version=False)`
  （这些 provider 不接受版本后缀）

参考：_research/cherry-studio-main/packages/shared/utils/api/utils.ts
回归测试：backend/tests_core/test_api_host_normalization.py
"""
from __future__ import annotations

import re
from typing import Optional

# 末尾 API 版本（带可选尾斜杠），仅识别末尾位置：/v1, /v2beta, /v3alpha 等
_TRAILING_VERSION_REGEX = re.compile(r"/v\d+(?:alpha|beta)?/?$", re.IGNORECASE)

# 路径中任意位置的 API 版本段（用于 has_api_version）
_VERSION_REGEX = re.compile(r"/v\d+(?:alpha|beta)?(?:/|$)", re.IGNORECASE)


def without_trailing_slash(url: str) -> str:
    """削掉**单个**末尾 `/`（非空 URL）。

    >>> without_trailing_slash("https://example.com/")
    'https://example.com'
    >>> without_trailing_slash("https://example.com")
    'https://example.com'
    """
    if not url:
        return url
    return url[:-1] if url.endswith("/") else url


def is_with_trailing_sharp(url: str) -> bool:
    """末尾恰好是 `#`（不含尾随空白）。

    >>> is_with_trailing_sharp("https://example.com#")
    True
    >>> is_with_trailing_sharp("https://example.com# ")
    False
    >>> is_with_trailing_sharp("https://example.com#path")
    False
    """
    if not url:
        return False
    return url.endswith("#")


def without_trailing_sharp(url: str) -> str:
    """削掉**最后一个** `#`（仅当末尾真是 `#` 时）。

    >>> without_trailing_sharp("https://example.com#")
    'https://example.com'
    >>> without_trailing_sharp("https://example.com##")
    'https://example.com#'
    >>> without_trailing_sharp("https://example.com#path")
    'https://example.com#path'
    """
    if not url:
        return url
    return url[:-1] if url.endswith("#") else url


def has_api_version(host: Optional[str]) -> bool:
    """URL pathname 是否含 API 版本段（任意位置）。

    用于 `format_api_host` 判断是否要自动追加版本。误伤防御：
    - `/api` `/vendor` `/a1v` `/av1` 都不算版本

    >>> has_api_version("https://api.example.com/v1")
    True
    >>> has_api_version("https://api.example.com/v2beta/chat")
    True
    >>> has_api_version("https://api.example.com/vendor")
    False
    >>> has_api_version("")
    False
    """
    if not host:
        return False
    # 优先用 URL 解析（精准 pathname 匹配，不被 query/fragment 干扰）
    try:
        from urllib.parse import urlparse

        parsed = urlparse(host)
        if parsed.scheme and parsed.netloc:
            return bool(_VERSION_REGEX.search(parsed.path or ""))
    except Exception:
        pass
    # fallback：当作 path 字符串直接 regex
    return bool(_VERSION_REGEX.search(host))


def get_trailing_api_version(url: str) -> Optional[str]:
    """提取末尾的 API 版本号（不含斜杠）；无则 None。

    末尾含 `#`、版本不在末尾、URL 后还有内容时返回 None。

    >>> get_trailing_api_version("https://api.example.com/v1")
    'v1'
    >>> get_trailing_api_version("https://api.example.com/v2beta/")
    'v2beta'
    >>> get_trailing_api_version("https://api.example.com/v1/chat")
    >>> get_trailing_api_version("https://api.example.com/v1#")
    >>> get_trailing_api_version("")
    """
    if not url:
        return None
    match = _TRAILING_VERSION_REGEX.search(url)
    if match is None:
        return None
    return match.group(0).lstrip("/").rstrip("/")


def without_trailing_api_version(url: str) -> str:
    """削掉末尾的 API 版本段（`/v1` / `/v2beta` / `/v3alpha` 等，含可选尾斜杠）。

    Anthropic Python SDK 路径专用：SDK 自动追加 /v1/messages，所以 base_url
    必须不含末尾 /v1，否则双 v1。

    中间路径里的版本不动（`/v1/chat`、`/service/v1/foo` 都不削）。

    >>> without_trailing_api_version("https://api.example.com/v1")
    'https://api.example.com'
    >>> without_trailing_api_version("https://api.example.com/v2beta/")
    'https://api.example.com'
    >>> without_trailing_api_version("https://api.example.com/v1/chat")
    'https://api.example.com/v1/chat'
    >>> without_trailing_api_version("https://api.example.com/service/v1")
    'https://api.example.com/service'
    """
    if not url:
        return url
    return _TRAILING_VERSION_REGEX.sub("", url)


def format_api_host(
    host: Optional[str],
    support_api_version: bool = True,
    api_version: str = "v1",
) -> str:
    """规范化 API host URL。综合 trim + 削尾斜杠 + # escape + 按需补版本。

    核心规则（对齐 Cherry formatApiHost）：
    1. trim 空白 + 削尾斜杠
    2. 空 → ''
    3. 末尾 `#` → without_trailing_sharp，**不**追加版本（escape hatch）
    4. support_api_version=False → 同上不追加（copilot/perplexity/azure）
    5. 已有版本段（任意位置 `/v\d+...`）→ without_trailing_sharp 不追加
    6. 否则 → 追加 /<api_version>

    Args:
        host: 用户填的原始 URL，可能含尾随空白 / 尾斜杠 / # 后缀
        support_api_version: provider 是否需要 API 版本后缀
        api_version: 要追加的版本号（默认 v1，Gemini 用 v1beta）

    Returns:
        规范化后的 host。空输入返回 ''。

    >>> format_api_host("https://api.example.com")
    'https://api.example.com/v1'
    >>> format_api_host("https://api.example.com/v2beta")
    'https://api.example.com/v2beta'
    >>> format_api_host("https://api.example.com#")
    'https://api.example.com'
    >>> format_api_host("https://api.example.com", False)
    'https://api.example.com'
    >>> format_api_host("  https://api.openai.com  ")
    'https://api.openai.com/v1'
    >>> format_api_host("https://api.example.com", True, "v2")
    'https://api.example.com/v2'
    >>> format_api_host("")
    ''
    """
    if host is None:
        return ""
    normalized = without_trailing_slash(host.strip())
    if not normalized:
        return ""

    should_append = not (
        normalized.endswith("#")
        or not support_api_version
        or has_api_version(normalized)
    )

    if should_append:
        return f"{normalized}/{api_version}"
    return without_trailing_sharp(normalized)


def normalize_anthropic_base_url(base_url: Optional[str]) -> Optional[str]:
    """Anthropic Python SDK 专用：base_url 末尾 /v1 自动剥（含 # escape）。

    Anthropic Python SDK 在 base_url 后自动追加 /v1/messages，不剥末尾 /v1
    会变 /v1/v1/messages 404。Cherry 那边因为 JS SDK 不自动加 /v1 才能保留。

    `#` escape hatch：用户末尾加 `#` 表示"按字面 URL，不要碰"，此时只削 # 不动版本。
    适合用户的反代真在 /v1 子路径下提供 Anthropic API 这种罕见场景。

    None / 空串原样返回。

    >>> normalize_anthropic_base_url("https://api.getrouter.top/v1")
    'https://api.getrouter.top'
    >>> normalize_anthropic_base_url("https://api.getrouter.top/v1#")
    'https://api.getrouter.top/v1'
    >>> normalize_anthropic_base_url("https://api.getrouter.top")
    'https://api.getrouter.top'
    >>> normalize_anthropic_base_url("https://gateway.example/anthropic")
    'https://gateway.example/anthropic'
    >>> normalize_anthropic_base_url(None)
    >>> normalize_anthropic_base_url("")
    ''
    """
    if not base_url:
        return base_url
    stripped_ws = base_url.strip()
    if is_with_trailing_sharp(stripped_ws):
        # 用户明确按字面 URL，仅削 # 不动 /v1
        return without_trailing_sharp(stripped_ws)
    return without_trailing_api_version(without_trailing_slash(stripped_ws))
