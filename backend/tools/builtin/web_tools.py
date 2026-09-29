#!/usr/bin/env python3
"""
网络工具 - 搜索 + 提取（多后端级联 + 全异步）

后端优先级：
  英文 query: Tavily > SearXNG > Bing > Exa > Firecrawl > DuckDuckGo
  中文 query: Tavily > Exa > SearXNG > 百度 > Bing > DDG > Firecrawl
  （付费服务用户已花钱配置 → 优先；爬虫不稳定 → 兜底）
任何一层失败 / 返回空都会自动 fallthrough 到下一个，保证"永远出结果"。

关键能力：
- 百度搜索：直接抓取百度结果页，零配置零依赖，中文搜索质量最好
- Bing搜索：直接抓取Bing结果页，零配置零依赖，中英文都不错
- SearXNG：自部署元搜索引擎，聚合百度/Bing/搜狗等70+引擎（需部署）
- Tavily 高级参数：search_depth=advanced / include_answer / topic / time_range
- DDG 中文搜索：自动 region="cn-zh"
- 结果附带 score / published_date，LLM 能据此排序
- web_extract 多 URL 并行抓取
- LLM 内容压缩：根据内容语言自动选择中/英提示词

环境变量：
- SEARXNG_URL: SearXNG 实例地址（可选，如 http://localhost:8888）
- SEARXNG_KEY: 可选，实例 API key
- TAVILY_API_KEY: Tavily 搜索 API key
- EXA_API_KEY: Exa 搜索 API key
- FIRECRAWL_API_KEY / FIRECRAWL_API_URL: Firecrawl API
- WEB_EXTRACT_MODEL: LLM 压缩用模型
"""

import json
import logging
import os
import asyncio
import re
import time
import hashlib
from typing import List, Dict, Any, Optional, Tuple
import httpx

logger = logging.getLogger(__name__)

# LLM 内容压缩配置
DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000  # 超过 5KB 才压缩
MAX_CONTENT_SIZE = 2_000_000  # 2MB 拒绝处理
CHUNK_THRESHOLD = 500_000  # 500KB 使用分块处理
CHUNK_SIZE = 100_000  # 每块 100KB
MAX_OUTPUT_SIZE = 5000  # 最终输出最大 5KB

# 后端优先级（按顺序尝试 fallthrough，默认优先使用已启用的 AI 引擎，末尾免 Key 引擎兜底）
_BACKEND_PRIORITY = ["devin", "exa", "tavily", "searxng", "bing", "ddg", "firecrawl"]

# 中文 query 专用优先级（付费服务优先，爬虫兜底）：
#   devin/exa/tavily — 用户已配置 API key 说明愿意花钱，必须先用（爬虫不稳定）
#   searxng          — 自部署元搜索，已配置则质量很好
#   baidu            — 中文搜索本土最强，但反爬/HTML 结构变更风险
#   bing             — 中英文都不错，反爬比百度松
#   ddg              — region="cn-zh" 兜底
#   firecrawl        — 中文覆盖弱，最后位
_BACKEND_PRIORITY_CJK = ["devin", "exa", "tavily", "searxng", "baidu", "bing", "ddg", "firecrawl"]


def _get_backend_priority(query: str) -> List[str]:
    """根据用户在设置界面的配置和 query 语言返回后端优先级列表"""
    is_cjk = _has_cjk(query)
    try:
        from backend.search_config import get_effective_priority
        p = get_effective_priority(is_cjk=is_cjk)
        logger.info(f"[web_search] using dynamic priority: {p}")
        return p
    except Exception as e:
        logger.warning(f"[web_search] get_effective_priority failed: {e}")
        if is_cjk:
            return _BACKEND_PRIORITY_CJK
        return _BACKEND_PRIORITY


def _match_domain(url: str, domains: List[str]) -> bool:
    """url 的 host 是否匹配 domains 里任一（后缀匹配，允许子域）。"""
    if not url or not domains:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for d in domains:
        d = (d or "").lower().lstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _apply_domain_filter(
    results: List[Dict[str, Any]],
    include: Optional[List[str]],
    exclude: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """客户端 domain 过滤，给不支持原生参数的后端（Firecrawl/DDG）兜底用。"""
    if include:
        results = [r for r in results if _match_domain(r.get("url", ""), include)]
    if exclude:
        results = [r for r in results if not _match_domain(r.get("url", ""), exclude)]
    # 重新编号 position
    for i, r in enumerate(results):
        r["position"] = i + 1
    return results


# ============================================================================
# 同 query 去重缓存（防止 LLM 拿到垃圾结果后反复同 query 重试烧 token）
# ============================================================================

# 结构：{session_id: {cache_key: (timestamp, result_json_str)}}
_SEARCH_CACHE: Dict[str, Dict[str, Tuple[float, str]]] = {}
_SEARCH_CACHE_TTL = 60.0  # 秒：60s 内同 query 视为重复
_SEARCH_CACHE_MAX_PER_SESSION = 32  # 每 session 最多缓存条目（简单 LRU）
_DEFAULT_CACHE_SESSION = "__default__"  # 拿不到 session_id 时的 fallback


# 中文常见无信息后缀/前缀，归一化时剥离让"灵山天气"和"灵山 天气预报"打到同一个 cache。
# 排序按长度降序——长后缀（"实时天气"）必须先匹配，否则会被 "天气" 短匹配先吃掉一截。
_CJK_NOISE_SUFFIXES = tuple(sorted([
    "天气", "天气预报", "实时天气", "今日天气",
    "怎么样", "在哪里", "在哪", "是什么", "介绍",
    "查询", "搜索", "百度", "百科", "最新",
], key=len, reverse=True))
_CJK_NOISE_PREFIXES = tuple(sorted([
    "查一下", "查询", "搜索", "请问", "帮我查", "帮我找",
], key=len, reverse=True))


def _normalize_query(query: str) -> str:
    """
    归一化 query 用于 cache key。目标：让 LLM 微调措辞绕过 cache 失效。
    - lowercase + 去所有空白（包括中间的）
    - 中文 query：剥常见无信息前后缀（"...天气" / "查一下..." 等）
    - 标点统一去掉

    例：
      "河南灵山天气" -> "河南灵山"
      "河南 灵山 天气预报" -> "河南灵山"
      "查一下灵山" -> "灵山"
      "灵山" -> "灵山"
    所有这些都会打到同一个 cache bucket。
    """
    q = (query or "").strip().lower()
    # 去所有空白
    q = re.sub(r"\s+", "", q)
    # 去常见标点
    q = re.sub(r"[,，。.!！?？;；:：、'\"`()（）\[\]【】<>《》—\-_/\\]+", "", q)
    if not q:
        return ""
    if _has_cjk(q):
        # 剥前缀
        for p in _CJK_NOISE_PREFIXES:
            if q.startswith(p) and len(q) > len(p):
                q = q[len(p):]
                break
        # 剥后缀（循环一次：可能"灵山天气预报"先剥"天气预报"再没东西可剥）
        for s in _CJK_NOISE_SUFFIXES:
            if q.endswith(s) and len(q) > len(s):
                q = q[: -len(s)]
                break
    return q


def _cache_key(
    query: str,
    limit: int,
    topic: str,
    time_range: Optional[str],
    include: Optional[List[str]],
    exclude: Optional[List[str]],
) -> str:
    parts = [
        _normalize_query(query),
        # limit 不进 key——同一个 query 不同 limit 应共享缓存（节省后端调用）
        topic or "",
        time_range or "",
        ",".join(sorted(include or [])),
        ",".join(sorted(exclude or [])),
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


# ============================================================================
# Session 级 web_search 调用计数器（防止 LLM 在同一会话里疯狂搜索）
# ============================================================================

# {session_id: [timestamp, timestamp, ...]} —— 仅保留 5 分钟内的调用
_SEARCH_CALL_LOG: Dict[str, List[float]] = {}
_CALL_LOG_WINDOW = 300.0  # 5 分钟滑窗
_CALL_SOFT_LIMIT = 5  # 触达后附 abuse_warning 软提醒
_CALL_HARD_LIMIT = 8  # 触达后直接拒绝


def _record_call(session_id: str) -> int:
    """记录一次 web_search 调用，返回滑窗内的总次数。"""
    now = time.monotonic()
    log = _SEARCH_CALL_LOG.setdefault(session_id, [])
    # 清理过期
    cutoff = now - _CALL_LOG_WINDOW
    log[:] = [t for t in log if t > cutoff]
    log.append(now)
    return len(log)


def _cache_get(session_id: str, key: str) -> Optional[Tuple[float, str]]:
    bucket = _SEARCH_CACHE.get(session_id)
    if not bucket:
        return None
    entry = bucket.get(key)
    if not entry:
        return None
    ts, payload = entry
    if time.monotonic() - ts > _SEARCH_CACHE_TTL:
        bucket.pop(key, None)
        return None
    return ts, payload


def _cache_set(session_id: str, key: str, payload: str) -> None:
    bucket = _SEARCH_CACHE.setdefault(session_id, {})
    if len(bucket) >= _SEARCH_CACHE_MAX_PER_SESSION:
        # 简单 LRU：踢掉最老的一条
        oldest_key = min(bucket.items(), key=lambda kv: kv[1][0])[0]
        bucket.pop(oldest_key, None)
    bucket[key] = (time.monotonic(), payload)


# ============================================================================
# 结果质量启发式（给 LLM 看的"结果可能是垃圾"信号）
# ============================================================================

def _assess_quality(query: str, results: List[Dict[str, Any]], backend: str) -> Optional[str]:
    """
    粗判结果质量。description 已不返回（学 Claude Code），改靠以下信号：
    - 标题为空 → 可疑
    - 标题像 URL（全 ASCII + 含 . 或 /）→ 后端没拿到人类可读标题
    - query 含中文但标题完全无中文 → 多半不相关
    - 结果域名在中文垃圾站黑名单中 → 质量差
    任一规则命中算 suspicious；过 40% 即返回硬语气警告。
    """
    if not results:
        return None

    # 中文低质量域名黑名单（SEO 采集站 / 内容农场）
    _CJK_SPAM_DOMAINS = {
        "csdn.net",        # 大量采集/转载
        "360doc.com",      # 内容农场
        "51cto.com",       # 大量转载
        "chinaz.com",      # SEO 工具站
        "baike.baidu.com", # 百科内容对搜索无增量价值
        "wenku.baidu.com", # 需登录，内容不可提取
        "zhidao.baidu.com",# 问答质量参差
    }

    query_has_cjk = _has_cjk(query)
    suspicious = 0
    for r in results:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()

        # 空标题
        if not title:
            suspicious += 1
            continue
        # 标题像 URL
        if title.isascii() and ("." in title or "/" in title):
            suspicious += 1
            continue
        # 中文 query 但标题全英文
        if query_has_cjk:
            title_has_cjk = _has_cjk(title)
            if not title_has_cjk:
                suspicious += 1
                continue
        # 中文垃圾站域名
        if query_has_cjk and url:
            try:
                from urllib.parse import urlparse
                host = (urlparse(url).netloc or "").lower()
                for spam_domain in _CJK_SPAM_DOMAINS:
                    if host == spam_domain or host.endswith("." + spam_domain):
                        # 标记为低质量但不计入 suspicious（只是降权提示）
                        r["spam_domain_warning"] = f"结果来自已知低质量站点 {spam_domain}"
                        break
            except Exception:
                pass

    if suspicious / len(results) < 0.4:
        return None

    if query_has_cjk:
        return (
            f"[web_search quality_warning] {suspicious}/{len(results)} 条结果质量低 "
            f"（标题为空/像URL/与中文query不匹配）。后端 '{backend}' 可能无法很好地回答此查询。"
            "不要用同样的 query 重试——要么(1)换关键词重搜，"
            "要么(2)用其他工具，要么(3)告诉用户无法通过搜索找到该信息。"
        )
    return (
        f"[web_search quality_warning] {suspicious}/{len(results)} results look low-quality "
        f"(empty/URL-like titles or off-language). Backend '{backend}' likely cannot answer this query well. "
        "DO NOT retry the same query — either (1) reformulate with different keywords, "
        "(2) use a different tool, or (3) tell the user the information cannot be found via web search."
    )


# ============================================================================
# 后端选择逻辑
# ============================================================================

def _has_env(name: str) -> bool:
    """检查环境变量是否存在"""
    val = os.getenv(name)
    return bool(val and val.strip())


def _backend_available(backend: str) -> bool:
    if backend == "devin":
        return _has_env("DEVIN_API_KEY")
    if backend == "tavily":
        return _has_env("TAVILY_API_KEY")
    if backend == "exa":
        return _has_env("EXA_API_KEY")
    if backend == "firecrawl":
        return _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")
    if backend == "searxng":
        return _searxng_available()
    if backend == "baidu":
        return _baidu_available()
    if backend == "bing":
        return _bing_available()
    if backend == "ddg":
        # ddgs 或旧的 duckduckgo_search 任一可用即可
        try:
            import ddgs  # noqa: F401
            return True
        except ImportError:
            pass
        try:
            from duckduckgo_search import DDGS  # noqa: F401
            return True
        except ImportError:
            return False
    return False


def _available_backends() -> List[str]:
    return [b for b in _BACKEND_PRIORITY if _backend_available(b)]


# ============================================================================
# Devin 搜索后端（基于官方云端服务）
# ============================================================================

_DEVIN_SERVER_HOSTS = ["server.codeium.com", "server.self-serve.windsurf.com"]
_DEVIN_WEB_SEARCH_PATH = "/exa.api_server_pb.ApiServerService/GetWebSearchResults"


async def _devin_search(
    query: str,
    limit: int = 5,
    domain: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    **_ignore,
) -> dict:
    """使用 Devin 官方云端服务接口进行高精技术搜索"""
    api_key = (os.getenv("DEVIN_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("DEVIN_API_KEY environment variable not set")

    from backend.search_config import detect_local_devin
    devin_info = detect_local_devin()
    ide_version = devin_info.get("ideVersion", "1.126.0")
    ext_version = devin_info.get("extensionVersion", "3.10.27")

    dom = domain or ""
    if not dom and include_domains:
        dom = include_domains[0]

    body: Dict[str, Any] = {
        "metadata": {
            "apiKey": api_key,
            "ideName": "devin",
            "ideVersion": ide_version,
            "extensionName": "devin",
            "extensionVersion": ext_version,
            "locale": "en",
        },
        "query": query.strip(),
        "limit": max(1, min(limit, 10)),
    }
    if dom:
        body["domain"] = dom

    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Accept": "application/json",
        "User-Agent": f"devin/{ide_version}",
    }

    last_err = None
    for host in _DEVIN_SERVER_HOSTS:
        url = f"https://{host}{_DEVIN_WEB_SEARCH_PATH}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code >= 400:
                    raise ValueError(f"Devin host {host} returned HTTP {resp.status_code}: {resp.text[:200]}")
                payload = resp.json()
                raw_results = payload.get("results") if isinstance(payload, dict) else []
                if not isinstance(raw_results, list):
                    raw_results = []

                results = []
                for i, r in enumerate(raw_results[:limit]):
                    if not isinstance(r, dict):
                        continue
                    title = r.get("title") or r.get("name") or r.get("webTitle") or ""
                    link = r.get("url") or r.get("sourceUrl") or r.get("webUrl") or r.get("link") or ""
                    snippet = r.get("snippet") or r.get("summary") or r.get("text") or r.get("content") or ""
                    if not title or not link:
                        continue
                    results.append({
                        "title": title,
                        "url": link,
                        "snippet": snippet,
                        "position": i + 1,
                    })

                logger.info(f"[web_search] devin '{query}' returned {len(results)} hits via {host}")
                return {
                    "success": True,
                    "backend": "devin",
                    "data": {"web": results},
                }
        except Exception as e:
            last_err = e
            logger.warning(f"[web_search] Devin search via {host} failed: {e}")

    raise last_err or ValueError("Devin web search: all hosts failed")


async def _devin_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """Devin 提取：复用通用的 httpx 抓取提取逻辑"""
    return await _ddg_extract(urls)


# ============================================================================
# Tavily 后端
# ============================================================================

async def _tavily_request(endpoint: str, payload: dict) -> dict:
    """调用 Tavily API（async）"""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise ValueError("TAVILY_API_KEY environment variable not set")

    url = f"https://api.tavily.com/{endpoint}"
    payload = {**payload, "api_key": api_key}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def _tavily_search(
    query: str,
    limit: int = 5,
    topic: str = "general",
    time_range: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    **_ignore,
) -> dict:
    """使用 Tavily 搜索（advanced depth + include_answer + topic/time_range + 域名过滤）"""
    logger.info(f"[web_search] tavily '{query}' limit={limit} topic={topic} time_range={time_range}")

    payload = {
        "query": query,
        "max_results": min(max(limit, 1), 20),
        "search_depth": "advanced",
        "include_answer": True,
        "include_raw_content": False,
        "include_images": False,
        "topic": topic,
    }
    if time_range:
        payload["time_range"] = time_range
    if include_domains:
        payload["include_domains"] = list(include_domains)
    if exclude_domains:
        payload["exclude_domains"] = list(exclude_domains)

    raw = await _tavily_request("search", payload)

    # 转换为统一格式（保留 score / published_date 供 LLM 排序）
    results = []
    for i, item in enumerate(raw.get("results", [])[:limit]):
        # 仅返 title + url + 元信息（学 Claude Code）：snippet 易被 SEO 垃圾污染 LLM context，
        # 想看正文让 LLM 显式调 web_extract。
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "score": item.get("score"),
            "published_date": item.get("published_date"),
            "position": i + 1
        })

    return {
        "success": True,
        "backend": "tavily",
        "answer": raw.get("answer"),
        "data": {
            "web": results
        }
    }


async def _tavily_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """使用 Tavily 批量提取网页内容（一次请求多个 URL，按 url 匹配返回）"""
    urls = list(urls)[:5]
    try:
        raw = await _tavily_request("extract", {
            "urls": urls,
            "extract_depth": "basic",
        })
    except Exception as e:
        logger.exception("Tavily extract request failed")
        return [{"url": u, "error": str(e), "success": False} for u in urls]

    ok = {r.get("url"): r for r in raw.get("results", []) if r.get("url")}
    failed = {r.get("url"): r for r in raw.get("failed_results", []) if r.get("url")}

    results: List[Dict[str, Any]] = []
    for url in urls:
        if url in ok:
            results.append({
                "url": url,
                "content": ok[url].get("raw_content", "") or "",
                "success": True,
            })
        elif url in failed:
            results.append({
                "url": url,
                "error": failed[url].get("error", "extraction failed"),
                "success": False,
            })
        else:
            results.append({
                "url": url,
                "error": "URL not returned by Tavily",
                "success": False,
            })
    return results


# ============================================================================
# Exa 后端
# ============================================================================

def _get_exa_client():
    """获取 Exa 客户端"""
    try:
        from exa_py import Exa
    except ImportError:
        raise ImportError("exa_py not installed. Run: pip install exa_py")

    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        raise ValueError("EXA_API_KEY environment variable not set")

    return Exa(api_key=api_key)


async def _exa_search(
    query: str,
    limit: int = 10,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    **_ignore,
) -> dict:
    """使用 Exa 搜索（sync lib 通过 to_thread 不阻塞事件循环）"""
    logger.info(f"[web_search] exa '{query}' limit={limit}")

    client = _get_exa_client()
    search_kwargs = {"num_results": limit, "use_autoprompt": True}
    if include_domains:
        search_kwargs["include_domains"] = list(include_domains)
    if exclude_domains:
        search_kwargs["exclude_domains"] = list(exclude_domains)
    response = await asyncio.to_thread(client.search, query, **search_kwargs)

    results = []
    for i, item in enumerate(response.results):
        results.append({
            "title": getattr(item, "title", "") or "",
            "url": getattr(item, "url", "") or "",
            "score": getattr(item, "score", None),
            "published_date": getattr(item, "published_date", None),
            "position": i + 1,
        })

    return {
        "success": True,
        "backend": "exa",
        "data": {"web": results},
    }


async def _exa_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """使用 Exa 提取网页内容（多 URL 并行）"""
    client = _get_exa_client()

    async def _one(url: str) -> Dict[str, Any]:
        try:
            resp = await asyncio.to_thread(client.get_contents, [url])
            if resp.results:
                return {
                    "url": url,
                    "content": getattr(resp.results[0], "text", "") or "",
                    "success": True,
                }
            return {"url": url, "error": "No content found", "success": False}
        except Exception as e:
            logger.warning(f"Exa extract {url} failed: {str(e)[:120]}")
            return {"url": url, "error": str(e), "success": False}

    return list(await asyncio.gather(*(_one(u) for u in list(urls)[:5])))


# ============================================================================
# Firecrawl 后端
# ============================================================================

_firecrawl_client = None


def _get_firecrawl_client():
    """获取 Firecrawl 客户端"""
    global _firecrawl_client

    if _firecrawl_client is not None:
        return _firecrawl_client

    try:
        from firecrawl import Firecrawl
    except ImportError:
        raise ImportError("firecrawl-py not installed. Run: pip install firecrawl-py")

    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    api_url = os.getenv("FIRECRAWL_API_URL", "").strip()

    if not api_key and not api_url:
        raise ValueError("FIRECRAWL_API_KEY or FIRECRAWL_API_URL not set")

    kwargs = {}
    if api_key:
        kwargs["api_key"] = api_key
    if api_url:
        kwargs["api_url"] = api_url

    _firecrawl_client = Firecrawl(**kwargs)
    return _firecrawl_client


def _fc_field(obj, key, default=""):
    """Firecrawl 返回值兼容：新版是 dict，老版是带属性的对象。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def _firecrawl_search(query: str, limit: int = 5, **_ignore) -> dict:
    """使用 Firecrawl 搜索（sync lib → to_thread，dict/attr 兼容解析）"""
    logger.info(f"[web_search] firecrawl '{query}' limit={limit}")

    client = _get_firecrawl_client()
    response = await asyncio.to_thread(client.search, query=query, limit=limit)

    data = _fc_field(response, "data", None)
    if data is None and hasattr(response, "data"):
        data = response.data

    results = []
    for i, item in enumerate((data or [])[:limit]):
        results.append({
            "title": _fc_field(item, "title", "") or "",
            "url": _fc_field(item, "url", "") or "",
            "position": i + 1,
        })

    return {
        "success": True,
        "backend": "firecrawl",
        "data": {"web": results},
    }


async def _firecrawl_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """使用 Firecrawl 提取网页内容（多 URL 并行）"""
    client = _get_firecrawl_client()

    async def _one(url: str) -> Dict[str, Any]:
        try:
            resp = await asyncio.to_thread(
                client.scrape_url, url, params={"formats": ["markdown"]}
            )
            content = _fc_field(resp, "markdown", "") or _fc_field(resp, "content", "") or ""
            if not content:
                return {"url": url, "error": "empty content", "success": False}
            return {"url": url, "content": content, "success": True}
        except Exception as e:
            logger.warning(f"Firecrawl extract {url} failed: {str(e)[:120]}")
            return {"url": url, "error": str(e), "success": False}

    return list(await asyncio.gather(*(_one(u) for u in list(urls)[:5])))


# ============================================================================
# SearXNG 后端（自部署元搜索引擎，中文搜索质量最好）
# ============================================================================

# 环境变量：
#   SEARXNG_URL  — SearXNG 实例地址（如 http://localhost:8888 或 https://search.example.com）
#   SEARXNG_KEY  — 可选，如果实例设置了 API key（settings.yml → server.secret_key）


def _searxng_available() -> bool:
    """检查 SearXNG 是否配置"""
    return bool(os.getenv("SEARXNG_URL", "").strip())


async def _searxng_search(
    query: str,
    limit: int = 5,
    time_range: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    **_ignore,
) -> dict:
    """
    使用 SearXNG 搜索（自部署元搜索引擎，聚合 Google/Bing/百度/搜狗等70+引擎）。

    优势：
    - 中文搜索：可聚合百度/Bing/搜狗，质量碾压 DDG
    - 免费：自部署零成本，无需 API key
    - 隐私：搜索请求不经过第三方
    - 灵活：language 参数精准控制搜索语言
    """
    base_url = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
    api_key = os.getenv("SEARXNG_KEY", "").strip()

    logger.info(f"[web_search] searxng '{query}' limit={limit}")

    # 构建请求参数
    params: Dict[str, Any] = {
        "q": query,
        "format": "json",
        "pageno": 1,
    }

    # 语言感知：CJK query 用中文搜索
    if _has_cjk(query):
        params["language"] = "zh-CN"
        params["categories"] = "general"

    # 时间范围
    if time_range:
        params["time_range"] = time_range

    # 域名过滤（SearXNG 不原生支持 include/exclude，用查询语法模拟）
    if include_domains:
        # 加 site: 限定
        site_clauses = " OR ".join(f"site:{d}" for d in include_domains)
        params["q"] = f"({site_clauses}) {query}"
    elif exclude_domains:
        # 加 -site: 排除
        exclude_clauses = " ".join(f"-site:{d}" for d in exclude_domains)
        params["q"] = f"{query} {exclude_clauses}"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(
            f"{base_url}/search",
            params=params,
            headers=headers,
        )
        if r.status_code != 200:
            raise ValueError(f"SearXNG returned HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()

    # 解析结果
    results = []
    for i, item in enumerate((data.get("results", []) or [])[:limit]):
        # SearXNG 返回的字段：title, url, content(摘要), engine, score, publishedDate
        results.append({
            "title": item.get("title", "") or "",
            "url": item.get("url", "") or "",
            "snippet": item.get("content", "") or "",  # SearXNG 有摘要，保留
            "engine": item.get("engine", ""),
            "score": item.get("score"),
            "published_date": item.get("publishedDate"),
            "position": i + 1,
        })

    # 客户端域名过滤（include_domains 时 query 语法不一定生效，兜底）
    if include_domains or exclude_domains:
        results = _apply_domain_filter(results, include_domains, exclude_domains)

    return {
        "success": True,
        "backend": "searxng",
        "data": {"web": results},
    }


async def _searxng_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """SearXNG 提取：复用 DDG 的 httpx 抓取逻辑"""
    return await _ddg_extract(urls)


# ============================================================================
# 百度搜索后端（零依赖，直接扒百度结果页，中文搜索首选）
# ============================================================================

# 无需环境变量，始终可用。但仅对中文 query 生效（英文 query 走其他后端更好）。

# 百度结果页 UA（模拟 Chrome，否则会被拦）
_BAIDU_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 百度广告/推广域名黑名单（搜索结果中的推广链接过滤掉）
_BAIDU_AD_PATTERNS = {
    "baidu.com/baidu.php",  # 百度竞价排名
    "e.baidu.com",           # 百度推广
    "baichuan.baidu.com",    # 百度商业推广
}


def _baidu_available() -> bool:
    """百度后端始终可用（只需 httpx）"""
    return True


def _is_baidu_ad(url: str) -> bool:
    """判断百度结果是否为广告/推广链接"""
    if not url:
        return True
    for pattern in _BAIDU_AD_PATTERNS:
        if pattern in url:
            return True
    return False


async def _baidu_search(
    query: str,
    limit: int = 5,
    exclude_domains: Optional[List[str]] = None,
    **_ignore,
) -> dict:
    """
    使用百度搜索（直接抓取百度结果页，零依赖零服务）。

    优势：
    - 中文搜索质量最好（百度就是中国最大的搜索引擎）
    - 零配置：无需 API key，无需部署额外服务
    - 轻量：只有 httpx 请求，无额外依赖
    - URL 是百度跳转链接，web_extract 调用时会自动解析到真实页面

    限制：
    - 仅对中文 query 有效，英文 query 结果不如 DDG/Tavily
    - 百度可能偶尔反爬，遇到 302/验证码时自动降级到下一个后端
    - 不支持 include_domains（百度搜索语法不支持 site: 与其他参数组合）
    """
    logger.info(f"[web_search] baidu '{query}' limit={limit}")

    params = {
        "wd": query,
        "rn": str(min(limit + 5, 20)),  # 多取几个，过滤广告后可能不够
        "ie": "utf-8",
    }

    # 全套真实浏览器头（含 Sec-Fetch / Sec-Ch-Ua / Referer）显著降低被反爬概率
    _headers = {
        "User-Agent": _BAIDU_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.baidu.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Chromium";v="125", "Not.A/Brand";v="24", "Google Chrome";v="125"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        # 先访问首页拿 Set-Cookie（BAIDUID 等），httpx client 自动存；带 cookie 再搜
        # → 绕过百度对无 cookie 请求的反爬（否则常只回零星结果 / 验证页）
        try:
            await client.get("https://www.baidu.com/", headers=_headers)
        except Exception:
            pass
        r = await client.get("https://www.baidu.com/s", params=params, headers=_headers)
        if r.status_code != 200:
            raise ValueError(f"Baidu returned HTTP {r.status_code}")
        html = r.text

    # 限定在 .result.c-container 容器内抓 <h3>，避免假命中（右侧推荐 / 底部热搜）。
    # 百度搜索结果项的 class 历史上有：result c-container, result-op c-container 等。
    # mu 属性也能识别"通用结果"类节点。
    container_pattern = re.compile(
        r'<div[^>]*class="[^"]*\bresult\b[^"]*\bc-container\b[^"]*"[^>]*>(.*?)</div>\s*</div>',
        re.S,
    )
    containers = container_pattern.findall(html)
    # 兜底：如果上面的限定抓不到（百度改版），退回宽松 <h3> 抓取
    if not containers:
        containers = [html]

    h3_pattern = re.compile(r'<h3[^>]*>(.*?)</h3>', re.S)

    raw_hits: List[Dict[str, str]] = []
    for container_html in containers:
        for h3_html in h3_pattern.findall(container_html):
            title = re.sub(r"<[^>]+>", "", h3_html).strip()
            if not title:
                continue
            link_match = re.search(r'href=["\']([^"\']+)["\']', h3_html)
            if not link_match:
                continue
            url = link_match.group(1)
            if _is_baidu_ad(url) or not url.startswith("http"):
                continue
            if exclude_domains and _match_domain(url, exclude_domains):
                continue
            raw_hits.append({"title": title, "url": url})
            if len(raw_hits) >= limit:
                break
        if len(raw_hits) >= limit:
            break

    # 解 baidu.com/link?url=xxx 跳转：并行 HEAD 拿 Location。
    # 百度跳转链接形如 https://www.baidu.com/link?url=...&wd=...
    # Location 头里是真实 URL。失败保留原 URL（不影响 web_extract，只影响 sources 显示）。
    async def _resolve_baidu_link(item: Dict[str, str]) -> Dict[str, str]:
        url = item["url"]
        if "baidu.com/link" not in url:
            return item
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as c:
                head = await c.head(url, headers={"User-Agent": _BAIDU_UA})
                real = head.headers.get("Location") or head.headers.get("location")
                if real and real.startswith("http"):
                    item["url"] = real
        except Exception as e:
            logger.debug(f"baidu link resolve {url[:60]} failed: {str(e)[:60]}")
        return item

    resolved = await asyncio.gather(*(_resolve_baidu_link(h) for h in raw_hits))

    results = []
    for i, item in enumerate(resolved):
        # 解跳后再过 exclude（真实 host 可能命中黑名单）
        if exclude_domains and _match_domain(item["url"], exclude_domains):
            continue
        results.append({
            "title": item["title"],
            "url": item["url"],
            "position": len(results) + 1,
        })

    return {
        "success": True,
        "backend": "baidu",
        "data": {"web": results},
    }


async def _baidu_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """百度提取：复用 DDG 的 httpx 抓取逻辑"""
    return await _ddg_extract(urls)


# ============================================================================
# Bing 搜索后端（零依赖，直接扒 Bing 结果页，中英文都好用）
# ============================================================================

# Bing 结果页 UA
_BING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Bing 广告/跟踪域名黑名单
_BING_AD_PATTERNS = {
    "bing.com/a?",           # Bing 广告跳转
    "bing.com/aclick?",      # Bing 点击跟踪
    "bing.com/click?",       # Bing 广告点击
    "microsoft.com/","bing.com/rewards/",  # Bing 奖励推广
    "go.microsoft.com/",     # 微软跳转
}


def _bing_available() -> bool:
    """Bing 后端始终可用（只需 httpx）"""
    return True


def _is_bing_ad(url: str) -> bool:
    """判断 Bing 结果是否为广告/跟踪链接"""
    if not url:
        return True
    for pattern in _BING_AD_PATTERNS:
        if pattern in url:
            return True
    return False


async def _bing_search(
    query: str,
    limit: int = 5,
    exclude_domains: Optional[List[str]] = None,
    **_ignore,
) -> dict:
    """
    使用 Bing 搜索（直接抓取 Bing 结果页，零依赖零服务）。

    优势：
    - 中英文搜索质量都不错（Bing 是全球第二大搜索引擎）
    - 零配置：无需 API key，无需部署额外服务
    - 反爬比百度松，稳定性好
    - 支持 include_domains / exclude_domains

    限制：
    - 中文搜索质量不如百度，但比 DDG 好
    - 英文搜索不如 Tavily/Exa，但作为免费备用足够
    """
    logger.info(f"[web_search] bing '{query}' limit={limit}")

    # 中文 query 用 cn.bing.com，英文用 www.bing.com
    is_cjk = _has_cjk(query)
    base_url = "https://cn.bing.com/search" if is_cjk else "https://www.bing.com/search"

    params = {
        "q": query,
        "count": str(min(limit + 5, 20)),  # 多取几个，过滤广告后可能不够
    }
    if is_cjk:
        params["setlang"] = "zh-CN"

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        r = await client.get(
            base_url,
            params=params,
            headers={
                "User-Agent": _BING_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8" if is_cjk else "en-US,en;q=0.9",
            },
        )
        if r.status_code != 200:
            raise ValueError(f"Bing returned HTTP {r.status_code}")
        html = r.text

    # 解析搜索结果：Bing 结果在 <li class="b_algo"> 中
    # 提取 <h2><a href="...">title</a></h2>
    results = []
    algo_pattern = re.compile(r'<li\s+class="b_algo"[^>]*>(.*?)</li>', re.S)

    for algo_match in algo_pattern.finditer(html):
        algo_html = algo_match.group(1)

        # 提取标题和链接
        h2_match = re.search(r'<h2[^>]*>\s*<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>\s*</h2>', algo_html, re.S)
        if not h2_match:
            continue

        url = h2_match.group(1)
        title = re.sub(r'<[^>]+>', '', h2_match.group(2)).strip()

        if not title or not url:
            continue

        # 过滤广告/跟踪链接
        if _is_bing_ad(url):
            continue

        # 只保留 http/https
        if not url.startswith("http"):
            continue

        # 过滤 exclude_domains
        if exclude_domains and _match_domain(url, exclude_domains):
            continue

        results.append({
            "title": title,
            "url": url,
            "position": len(results) + 1,
        })

        if len(results) >= limit:
            break

    # 客户端域名过滤
    if exclude_domains:
        results = _apply_domain_filter(results, None, exclude_domains)

    return {
        "success": True,
        "backend": "bing",
        "data": {"web": results},
    }


async def _bing_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """Bing 提取：复用 DDG 的 httpx 抓取逻辑"""
    return await _ddg_extract(urls)


# ============================================================================
# DuckDuckGo 后端（免费备用）
# ============================================================================

def _get_ddgs_cls():
    """拿 ddgs.DDGS 或旧的 duckduckgo_search.DDGS"""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # 旧版兜底
        return DDGS


def _has_cjk(text: str) -> bool:
    """检测文本是否含 CJK 字符（中日韩）"""
    return any("\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff" or "\uac00" <= c <= "\ud7af" for c in (text or ""))


async def _ddg_search(query: str, limit: int = 5, **_ignore) -> dict:
    """使用 DuckDuckGo 搜索（ddgs 库，真·搜索结果不是 Instant Answer）

    中文 query 自动加 region="cn-zh"，提升中文结果排序和索引命中率。
    """
    logger.info(f"[web_search] ddg '{query}' limit={limit}")

    DDGS = _get_ddgs_cls()

    # 语言感知：中文 query 用中国区搜索，其他走默认全球索引
    is_cjk = _has_cjk(query)
    region = "cn-zh" if is_cjk else None

    def _do():
        with DDGS() as d:
            kwargs = {"max_results": limit}
            if region:
                kwargs["region"] = region
            return list(d.text(query, **kwargs))

    rows = await asyncio.to_thread(_do)

    results = []
    for i, r in enumerate((rows or [])[:limit]):
        results.append({
            "title": r.get("title", "") or "",
            "url": r.get("href", "") or r.get("url", "") or "",
            "position": i + 1,
        })

    return {
        "success": True,
        "backend": "ddg",
        "data": {"web": results},
    }


# 保留旧函数名作为别名，避免外部误引用
async def _duckduckgo_search(query: str, limit: int = 5) -> dict:
    """[deprecated 名称] 实际走 _ddg_search"""
    return await _ddg_search(query, limit)


async def _ddg_extract(urls: List[str]) -> List[Dict[str, Any]]:
    """无 API 兜底提取：httpx 抓 + 粗暴去标签。多 URL 并行。"""
    async def _one(url: str) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    return {"url": url, "error": f"HTTP {r.status_code}", "success": False}
                html = r.text
                # 先干掉 script/style 再去标签，避免把 JS 文本留下来
                html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
                html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
                text = re.sub(r"<[^>]+>", "", html)
                text = re.sub(r"\s+", " ", text).strip()
                return {"url": url, "content": text[:10000], "success": True}
        except Exception as e:
            logger.warning(f"DDG extract {url} failed: {str(e)[:120]}")
            return {"url": url, "error": str(e), "success": False}

    return list(await asyncio.gather(*(_one(u) for u in list(urls)[:5])))


# 保留旧函数名别名（避免外部 import 炸）
async def _duckduckgo_extract(urls: List[str]) -> List[Dict[str, Any]]:
    return await _ddg_extract(urls)


# ============================================================================
# 统一工具接口
# ============================================================================

_SEARCH_IMPLS = {
    "devin": _devin_search,
    "tavily": _tavily_search,
    "searxng": _searxng_search,
    "baidu": _baidu_search,
    "bing": _bing_search,
    "exa": _exa_search,
    "firecrawl": _firecrawl_search,
    "ddg": _ddg_search,
}

_EXTRACT_IMPLS = {
    "devin": _devin_extract,
    "tavily": _tavily_extract,
    "searxng": _searxng_extract,
    "baidu": _baidu_extract,
    "bing": _bing_extract,
    "exa": _exa_extract,
    "firecrawl": _firecrawl_extract,
    "ddg": _ddg_extract,
}


async def web_search_tool(
    query: str,
    limit: int = 5,
    topic: str = "general",
    time_range: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    网络搜索：7 后端级联（Tavily/Exa/SearXNG/百度/Bing/DDG/Firecrawl），
    根据 query 语言（CJK vs 英文）动态调整优先级。单后端失败或空结果自动降级。

    内置三道护栏（防止 LLM 拿到垃圾结果反复重试烧 token）：
    - 60 秒内同 session 同 query（含归一化模糊匹配）直接返回上次结果 + cache_note
    - 同 session web_search 调用次数超限（5 次软警告 / 8 次硬拒绝）
    - 结果质量低时附加 quality_warning（标题像 URL / 中文 query 全英文标题等）

    Args:
        query: 搜索查询
        limit: 每后端最多返回数（1-20，默认 5）
        topic: Tavily 专用。"general" | "news"
        time_range: Tavily 专用。"day" | "week" | "month" | "year"
        include_domains / exclude_domains: 域名白/黑名单
        session_id: 由 handler 从 tool context 注入；用于缓存隔离
    """
    # 护栏 0：domain 白/黑名单互斥（学 Claude Code / Cline）。
    # 同时传两个会让 LLM 困惑——明确报错强迫它二选一。
    if include_domains and exclude_domains:
        return json.dumps({
            "success": False,
            "error": (
                "Cannot specify both include_domains and exclude_domains in the same call. "
                "Use one or the other."
            ),
        }, ensure_ascii=False)

    cache_sid = session_id or _DEFAULT_CACHE_SESSION

    # 护栏 1：session 级调用次数硬上限（防 LLM 在一个会话里搜 12+ 次）
    call_count = _record_call(cache_sid)
    if call_count > _CALL_HARD_LIMIT:
        is_cjk = _has_cjk(query)
        msg = (
            f"已在本会话中调用 web_search {call_count} 次（5 分钟窗口），超过硬上限 {_CALL_HARD_LIMIT}。"
            "请停止搜索，根据已有结果直接回答用户，或告诉用户该信息无法通过搜索获取。"
        ) if is_cjk else (
            f"web_search has been called {call_count} times in this session (5-min window), "
            f"exceeding hard limit {_CALL_HARD_LIMIT}. Stop searching now. Answer the user with what you "
            "already have, or tell them the information cannot be found via web search."
        )
        return json.dumps({
            "success": False,
            "error": "web_search_call_limit_exceeded",
            "abuse_warning": msg,
            "call_count": call_count,
        }, ensure_ascii=False, indent=2)

    ckey = _cache_key(query, limit, topic, time_range, include_domains, exclude_domains)

    # 护栏 A：同 session 同 query 60s 内命中缓存 → 强硬提示 LLM 别再重试
    cached = _cache_get(cache_sid, ckey)
    if cached is not None:
        ts, payload = cached
        age = round(time.monotonic() - ts, 1)
        try:
            prev = json.loads(payload)
        except Exception:
            prev = {"raw": payload}
        prev["cache_hit"] = True
        prev["cache_age_seconds"] = age
        prev["cache_note"] = (
            f"You searched this EXACT query {age}s ago and got the same results below. "
            "DO NOT retry this query. Either (1) reformulate with different keywords, "
            "(2) try a different tool, or (3) tell the user the information cannot be found via web search."
        ) if not _has_cjk(query) else (
            f"你在 {age} 秒前搜索过完全相同的 query 并得到了以下结果。"
            "不要用同样的 query 重试。要么(1)换关键词重搜，"
            "要么(2)用其他工具，要么(3)告诉用户无法通过搜索找到该信息。"
        )
        logger.info(f"[web_search] cache hit sid={cache_sid[:8]} age={age}s query='{query[:40]}'")
        return json.dumps(prev, ensure_ascii=False, indent=2)

    # 语言感知的后端优先级
    backends = [b for b in _get_backend_priority(query) if _backend_available(b)]
    if not backends:
        return json.dumps({
            "success": False,
            "error": (
                "No search backend available. Install `ddgs` for free fallback "
                "(pip install ddgs), or set TAVILY_API_KEY / EXA_API_KEY / FIRECRAWL_API_KEY."
            ),
        }, ensure_ascii=False)

    errors: List[str] = []
    for b in backends:
        try:
            impl = _SEARCH_IMPLS[b]
            result = await impl(
                query=query,
                limit=limit,
                topic=topic,
                time_range=time_range,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )
            # Tavily/Exa 原生支持 domain 过滤；Firecrawl/DDG 在这里客户端兜底
            if b in ("firecrawl", "ddg") and (include_domains or exclude_domains):
                result["data"]["web"] = _apply_domain_filter(
                    result["data"]["web"], include_domains, exclude_domains
                )
            if result.get("success") and result.get("data", {}).get("web"):
                # 护栏 B：质量评估
                warn = _assess_quality(query, result["data"]["web"], b)
                if warn:
                    result["quality_warning"] = warn
                    logger.info(f"[web_search] low quality flagged: {warn[:80]}")
                # 学 Claude Code：强制 LLM 在最终回复中列 Sources。
                # web_search 现在不再返回 description，LLM 用结果时必须把 url 列出来引用。
                if _has_cjk(query):
                    result["sources_reminder"] = (
                        "在回复用户时，你必须在末尾包含「来源：」部分，以 Markdown 链接形式列出相关 URL："
                        "[标题](URL)。要阅读页面内容，请对该 URL 调用 web_extract。"
                    )
                else:
                    result["sources_reminder"] = (
                        "When you reply to the user using these results, you MUST include a "
                        "'Sources:' section at the end listing relevant URLs as markdown links: "
                        "[Title](URL). To read a page's content, call web_extract on the URL."
                    )
                # 护栏 C：调用次数软警告（达到 soft_limit 但未达 hard_limit）
                if call_count >= _CALL_SOFT_LIMIT:
                    if _has_cjk(query):
                        result["abuse_warning"] = (
                            f"你已在本会话中调用 web_search {call_count} 次（上限 {_CALL_HARD_LIMIT}）。"
                            "考虑停止搜索：根据已有结果回答，或告诉用户信息找不到。"
                        )
                    else:
                        result["abuse_warning"] = (
                            f"You have called web_search {call_count} times in this session "
                            f"(hard limit {_CALL_HARD_LIMIT}). Consider stopping: answer with what you "
                            "have, or tell the user the information cannot be found."
                        )
                payload = json.dumps(result, ensure_ascii=False, indent=2)
                _cache_set(cache_sid, ckey, payload)
                return payload
            errors.append(f"{b}: empty result")
            logger.info(f"[web_search] {b} returned empty, trying next")
        except Exception as e:
            logger.warning(f"[web_search] {b} failed: {str(e)[:150]}")
            errors.append(f"{b}: {str(e)[:120]}")
            continue

    # 全部失败：也缓存，避免 LLM 反复撞同一堵墙
    failure_payload = json.dumps({
        "success": False,
        "error": "All backends failed or returned empty",
        "tried": errors,
    }, ensure_ascii=False, indent=2)
    _cache_set(cache_sid, ckey, failure_payload)
    return failure_payload


# ============================================================================
# LLM 内容压缩功能
# ============================================================================

async def _compress_with_llm(
    content: str,
    context_str: str = "",
    model: str = None,
    is_chunk: bool = False,
    chunk_info: str = ""
) -> Optional[str]:
    """
    使用 LLM 压缩网页内容

    Args:
        content: 要压缩的内容
        context_str: 上下文信息（标题、URL）
        model: 使用的模型（默认 deepseek-chat）
        is_chunk: 是否是分块处理
        chunk_info: 分块信息

    Returns:
        压缩后的内容，失败返回 None
    """
    try:
        # 延迟导入避免循环依赖
        import sys
        from pathlib import Path

        # 确保可以导入 backend 模块
        # 当前文件: backend/tools/builtin/web_tools.py
        # 需要添加项目根目录（backend 的父目录）到 sys.path
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent.parent  # builtin -> tools -> backend -> 项目根
        sys.path.insert(0, str(project_root))

        # 现在可以导入了
        from backend.llm_client import get_llm_client

        llm_client = get_llm_client()

        # 智能选择模型：
        # 1. 优先使用传入的 model 参数
        # 2. 其次使用环境变量 WEB_EXTRACT_MODEL
        # 3. 最后复用主对话的 LLM（无需额外配置）
        effective_model = model or os.getenv("WEB_EXTRACT_MODEL")

        if not effective_model:
            logger.info("💡 Using main LLM for compression (no WEB_EXTRACT_MODEL configured). Tip: Set WEB_EXTRACT_MODEL=deepseek-chat to save 88.9% cost.")

        # 构建提示词（根据内容语言自动选择中/英）
        content_has_cjk = _has_cjk(content[:2000]) if content else False

        if is_chunk:
            if content_has_cjk:
                system_prompt = """你是一位专业内容分析师，正在处理大型文档的一个片段。你的任务是从这个片段中提取和总结关键信息。

重要指南：
1. 不要写引言或结论——这是文档的一部分
2. 专注于提取此片段中的所有关键事实、数据、洞察
3. 保留重要引述、代码片段和具体细节的原文
4. 使用项目符号和结构化格式，便于后续合成
5. 注意引用其他部分的文字（如"如前所述"、"见下文"），无需展开

你的输出会与其他片段的摘要合并，因此专注于全面提取而非叙述连贯性。"""

                user_prompt = f"""从大型文档的以下片段中提取关键信息：

{context_str}{chunk_info}

片段内容：
{content}

以结构化格式提取此片段中的所有重要信息。专注于事实、数据、洞察和关键细节。不要添加引言或结论。"""
            else:
                system_prompt = """You are an expert content analyst processing a SECTION of a larger document. Your job is to extract and summarize the key information from THIS SECTION ONLY.

Important guidelines for chunk processing:
1. Do NOT write introductions or conclusions - this is a partial document
2. Focus on extracting ALL key facts, figures, data points, and insights from this section
3. Preserve important quotes, code snippets, and specific details verbatim
4. Use bullet points and structured formatting for easy synthesis later
5. Note any references to other sections (e.g., "as mentioned earlier", "see below") without trying to resolve them

Your output will be combined with summaries of other sections, so focus on thorough extraction rather than narrative flow."""

                user_prompt = f"""Extract key information from this SECTION of a larger document:

{context_str}{chunk_info}

SECTION CONTENT:
{content}

Extract all important information from this section in a structured format. Focus on facts, data, insights, and key details. Do not add introductions or conclusions."""
        else:
            # 完整文档提示词
            if content_has_cjk:
                system_prompt = """你是一位专业内容分析师。你的任务是处理网页内容，创建全面而简洁的摘要，在大幅减少篇幅的同时保留所有重要信息。

创建结构良好的 Markdown 摘要，包括：
1. 关键摘录（引述、代码片段、重要事实）保持原格式
2. 所有其他重要信息的全面总结
3. 规范的 Markdown 格式：标题、列表、强调

你的目标是保留所有重要信息的同时减少篇幅。绝不要丢失关键事实、数据、洞察或可操作的信息。确保摘要清晰易读、结构良好。"""

                user_prompt = f"""请处理以下网页内容，创建全面的 Markdown 摘要：

{context_str}待处理内容：
{content}

创建一份 Markdown 摘要，以结构良好、清晰易读的格式捕获所有关键信息。保留重要的引述和代码片段的原格式。专注于可操作的信息、具体细节和独到见解。"""
            else:
                system_prompt = """You are an expert content analyst. Your job is to process web content and create a comprehensive yet concise summary that preserves all important information while dramatically reducing bulk.

Create a well-structured markdown summary that includes:
1. Key excerpts (quotes, code snippets, important facts) in their original format
2. Comprehensive summary of all other important information
3. Proper markdown formatting with headers, bullets, and emphasis

Your goal is to preserve ALL important information while reducing length. Never lose key facts, figures, insights, or actionable information. Make it scannable and well-organized."""

                user_prompt = f"""Please process this web content and create a comprehensive markdown summary:

{context_str}CONTENT TO PROCESS:
{content}

Create a markdown summary that captures all key information in a well-organized, scannable format. Include important quotes and code snippets in their original formatting. Focus on actionable information, specific details, and unique insights."""

        # 调用 LLM（注意：不传 model 参数，使用客户端默认模型）
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        # 使用流式响应收集结果
        result_content = ""
        async for chunk in llm_client.chat(
            messages=messages,
            stream=True,
            use_tools=False,
            agent_id="web_extract"
        ):
            if chunk.get("type") == "content":
                result_content += chunk.get("content", "")

        return result_content if result_content else None

    except Exception as e:
        logger.warning(f"LLM compression failed: {str(e)[:100]}")
        return None


async def _process_large_content_chunked(
    content: str,
    context_str: str,
    model: str = None
) -> Optional[str]:
    """
    分块处理超大内容

    Args:
        content: 超大内容
        context_str: 上下文信息
        model: 使用的模型

    Returns:
        合成后的摘要
    """
    # 分块
    chunks = []
    for i in range(0, len(content), CHUNK_SIZE):
        chunk = content[i:i + CHUNK_SIZE]
        chunks.append(chunk)

    logger.info(f"Split into {len(chunks)} chunks of ~{CHUNK_SIZE} chars each")

    # 并行处理每个块
    async def summarize_chunk(chunk_idx: int, chunk_content: str) -> tuple[int, Optional[str]]:
        try:
            chunk_info = f"\n[Processing chunk {chunk_idx + 1} of {len(chunks)}]"
            summary = await _compress_with_llm(
                chunk_content,
                context_str,
                model,
                is_chunk=True,
                chunk_info=chunk_info
            )
            if summary:
                logger.info(f"Chunk {chunk_idx + 1}/{len(chunks)} summarized: {len(chunk_content)} -> {len(summary)} chars")
            return chunk_idx, summary
        except Exception as e:
            logger.warning(f"Chunk {chunk_idx + 1}/{len(chunks)} failed: {str(e)[:50]}")
            return chunk_idx, None

    # 并行执行
    tasks = [summarize_chunk(i, chunk) for i, chunk in enumerate(chunks)]
    results = await asyncio.gather(*tasks)

    # 收集成功的摘要
    summaries = []
    for chunk_idx, summary in sorted(results, key=lambda x: x[0]):
        if summary:
            summaries.append(f"## Section {chunk_idx + 1}\n{summary}")

    if not summaries:
        return "[Failed to process large content: all chunk summarizations failed]"

    logger.info(f"Got {len(summaries)}/{len(chunks)} chunk summaries")

    # 如果只有一个块成功，直接返回
    if len(summaries) == 1:
        result = summaries[0]
        if len(result) > MAX_OUTPUT_SIZE:
            result = result[:MAX_OUTPUT_SIZE] + "\n\n[... truncated ...]"
        return result

    # 合成最终摘要
    logger.info(f"Synthesizing {len(summaries)} summaries...")

    combined_summaries = "\n\n---\n\n".join(summaries)

    # 检测合成内容的语言
    synth_has_cjk = _has_cjk(combined_summaries[:2000])

    if synth_has_cjk:
        synthesis_context = f"""{context_str}你已获得大型文档不同片段的摘要。
将这些摘要合成为一份连贯、全面的摘要，要求：
1. 去除片段间的冗余内容
2. 按逻辑组织信息
3. 保留所有关键事实和细节
4. 使用清晰的 Markdown 格式

片段摘要：
"""
    else:
        synthesis_context = f"""{context_str}
You have been given summaries of different sections of a large document.
Synthesize these into ONE cohesive, comprehensive summary that:
1. Removes redundancy between sections
2. Organizes information logically
3. Preserves all key facts and details
4. Uses clear markdown formatting

SECTION SUMMARIES:
"""

    final_summary = await _compress_with_llm(
        combined_summaries,
        synthesis_context,
        model,
        is_chunk=False
    )

    if final_summary and len(final_summary) > MAX_OUTPUT_SIZE:
        final_summary = final_summary[:MAX_OUTPUT_SIZE] + "\n\n[... summary truncated for context management ...]"

    return final_summary or combined_summaries[:MAX_OUTPUT_SIZE]


async def process_content_with_llm(
    content: str,
    url: str = "",
    title: str = "",
    model: str = None,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION
) -> Optional[str]:
    """
    使用 LLM 处理网页内容，创建智能摘要

    这个函数使用便宜的 LLM（如 DeepSeek）来智能提取关键信息并创建 Markdown 摘要，
    显著减少 token 使用量的同时保留所有重要信息。

    对于超大内容（>500KB），使用分块处理和合成。
    对于极大内容（>2MB），拒绝处理。

    Args:
        content: 要处理的原始内容
        url: 来源 URL（用于上下文）
        title: 页面标题（用于上下文）
        model: 使用的模型（默认 deepseek-chat）
        min_length: 触发处理的最小内容长度（默认 5000）

    Returns:
        处理后的 Markdown 内容，如果内容太短或处理失败则返回 None
    """
    try:
        content_len = len(content)

        # 拒绝超大内容
        if content_len > MAX_CONTENT_SIZE:
            size_mb = content_len / 1_000_000
            logger.warning(f"Content too large ({size_mb:.1f}MB > 2MB limit). Refusing to process.")
            return f"[Content too large to process: {size_mb:.1f}MB. Try using web_crawl with specific extraction instructions, or search for a more focused source.]"

        # 跳过太短的内容
        if content_len < min_length:
            logger.debug(f"Content too short ({content_len} < {min_length} chars), skipping LLM processing")
            return None

        # 创建上下文信息
        context_info = []
        if title:
            context_info.append(f"Title: {title}")
        if url:
            context_info.append(f"Source: {url}")
        context_str = "\n".join(context_info) + "\n\n" if context_info else ""

        # 检查是否需要分块处理
        if content_len > CHUNK_THRESHOLD:
            logger.info(f"Content large ({content_len} chars). Using chunked processing...")
            return await _process_large_content_chunked(content, context_str, model)

        # 标准单次处理
        logger.info(f"Processing content with LLM ({content_len} characters)")

        processed_content = await _compress_with_llm(content, context_str, model)

        if processed_content:
            # 强制输出上限
            if len(processed_content) > MAX_OUTPUT_SIZE:
                processed_content = processed_content[:MAX_OUTPUT_SIZE] + "\n\n[... summary truncated for context management ...]"

            # 记录压缩指标
            processed_length = len(processed_content)
            compression_ratio = processed_length / content_len if content_len > 0 else 1.0
            logger.info(f"Content processed: {content_len} -> {processed_length} chars ({compression_ratio * 100:.1f}%)")

        return processed_content

    except Exception as e:
        logger.warning(f"web_extract LLM summarization failed: {str(e)[:120]}")
        # 降级到截断的原始内容
        truncated = content[:MAX_OUTPUT_SIZE]
        if len(content) > MAX_OUTPUT_SIZE:
            truncated += f"\n\n[Content truncated — showing first {MAX_OUTPUT_SIZE:,} of {len(content):,} chars. LLM summarization failed.]"
        return truncated


async def web_extract_tool(
    urls: List[str],
    compress: bool = True,
    offset: int = 0,
    limit: int = 0,
) -> str:
    """
    网页内容提取工具（零配置，支持 LLM 压缩 + 字符级分页）

    Args:
        urls: 要提取的 URL 列表（最多 5 个）
        compress: 是否使用 LLM 压缩内容（默认 True，节省 88.9% 成本）
        offset: 每个 URL content 的字符起点（默认 0）
        limit: 每个 URL content 的最大字符数（0 = 不切片，向后兼容）

    Returns:
        JSON 格式的提取结果。limit>0 时每个 result 附带
        content_length / has_more / next_offset，支持续翻。
    """
    # P1#5 分页：clamp 防御
    if not isinstance(offset, int) or offset < 0:
        offset = 0
    if not isinstance(limit, int) or limit < 0:
        limit = 0
    if not urls:
        return json.dumps({"success": False, "error": "urls is empty"}, ensure_ascii=False)

    backends = _available_backends()
    if not backends:
        return json.dumps({
            "success": False,
            "error": (
                "No extract backend available. Install `ddgs` (pip install ddgs) "
                "or set TAVILY_API_KEY / EXA_API_KEY / FIRECRAWL_API_KEY."
            ),
        }, ensure_ascii=False)

    # 链路 fallthrough：只要有任一 URL 成功就算这个后端 OK
    results: Optional[List[Dict[str, Any]]] = None
    used_backend: Optional[str] = None
    errors: List[str] = []
    for b in backends:
        try:
            candidate = await _EXTRACT_IMPLS[b](urls)
            if any(r.get("success") for r in candidate):
                results = candidate
                used_backend = b
                break
            errors.append(f"{b}: all urls failed")
            logger.info(f"[web_extract] {b} got 0 successes, trying next")
        except Exception as e:
            logger.warning(f"[web_extract] {b} failed: {str(e)[:150]}")
            errors.append(f"{b}: {str(e)[:120]}")
            continue

    if results is None:
        return json.dumps({
            "success": False,
            "error": "All extract backends failed",
            "tried": errors,
        }, ensure_ascii=False, indent=2)

    # 如果启用压缩，对每个 success 结果并行 LLM 压缩
    if compress:
        async def _maybe_compress(result: Dict[str, Any]) -> None:
            if not (result.get("success") and isinstance(result.get("content"), str)):
                return
            original = result["content"]
            try:
                compressed = await process_content_with_llm(
                    content=original,
                    url=result.get("url", ""),
                    title=result.get("title", ""),
                )
            except Exception as e:
                logger.warning(f"compress failed for {result.get('url')}: {str(e)[:100]}")
                return
            if compressed:
                orig_len = len(original)
                comp_len = len(compressed)
                result["content"] = compressed
                result["compressed"] = True
                result["compression_ratio"] = f"{comp_len / max(orig_len, 1) * 100:.1f}%"
                logger.info(f"Compressed {result.get('url')}: {orig_len} -> {comp_len} chars")

        await asyncio.gather(*(_maybe_compress(r) for r in results))

    # P1#5 字符级分页：切片在 compress 之后做
    if limit > 0:
        for r in results:
            c = r.get("content")
            if not isinstance(c, str):
                continue
            total = len(c)
            r["content_length"] = total
            r["offset"] = offset
            r["limit"] = limit
            r["content"] = c[offset:offset + limit]
            if total > offset + limit:
                r["has_more"] = True
                r["next_offset"] = offset + limit
            else:
                r["has_more"] = False

    return json.dumps({
        "success": True,
        "backend": used_backend,
        "results": results,
    }, ensure_ascii=False, indent=2)


# ============================================================================
# 工具注册处理函数
# ============================================================================

# clean_registry 用 handler(**all_args) 展开调用，业务参数与 context（agent_id/session_id/
# llm_client）会一起塞进来。handler 必须用 **kwargs 接后显式挑字段，旧的 (args, **kw) 签名
# 拿不到 args dict 会报 missing required argument。


async def _handle_web_search(**kwargs):
    query = kwargs.get("query", "")
    limit = kwargs.get("limit", 5)
    topic = kwargs.get("topic", "general") or "general"
    time_range = kwargs.get("time_range") or None
    include_domains = kwargs.get("include_domains") or None
    exclude_domains = kwargs.get("exclude_domains") or None
    # session_id 由 clean_registry 通过 context 塞进来，用于同-query 缓存隔离
    session_id = kwargs.get("session_id") or None
    return await web_search_tool(
        query,
        limit,
        topic=topic,
        time_range=time_range,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        session_id=session_id,
    )


async def _handle_web_extract(**kwargs):
    urls = kwargs.get("urls", [])
    compress = kwargs.get("compress", True)
    offset = kwargs.get("offset", 0)
    limit = kwargs.get("limit", 0)
    return await web_extract_tool(urls, compress, offset=offset, limit=limit)
