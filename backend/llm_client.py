"""
LLM 客户端 - 支持多种后端提供商
- openai: OpenAI 兼容模式（默认）
- anthropic: Anthropic 原生模式
"""
import asyncio
import contextvars
import logging
import os
import json
import re
import time
from typing import Optional, AsyncIterator, List, Dict, Any, Tuple
from dotenv import load_dotenv

# ⚠️ 必须在 .tools 等子模块 import 之前 load_dotenv()——
# clean_registry 模块顶部 _FEATURE_CRON_TOOL 等条件注册 flag 是 module-load 时
# 立刻求值，晚加载 .env 的话这些 flag 永远拿到空字符串 → flag-gated 工具
# 全部不注册（2026-05-01 实测：柳如烟数工具时 cron_* 看不见就是这个根因）
load_dotenv()

from .anthropic_client import LLMError, LLMErrorCode
from .runtime_budget import AggregateRuntimeBudget
from .runtime_schedule import build_budget_convergence_plan
from .tools import get_tool_registry
from .tools.builtin.clean_registry import format_tool_result_for_display
from .tool_loop_guard import (
    ToolLoopGuard,
    ToolLoopStop,
    build_tool_loop_notice_event,
    build_tool_loop_user_message,
)

logger = logging.getLogger(__name__)


def _extract_content_blocks_from_tool_result(result: Any) -> "Optional[List[Dict[str, Any]]]":
    """探查工具返回值里有没有 `_content_blocks` 标记位。

    read_file 读图片时返回 JSON 字符串 `{"_content_blocks": [...], "_meta": {...}}`，
    本函数把它解开返 list[block]；没标记则返 None。

    协议见 docs/architecture_spec.md §3.1.2。
    """
    if isinstance(result, dict):
        blocks = result.get("_content_blocks")
        if isinstance(blocks, list) and blocks:
            return blocks
        return None
    if not isinstance(result, str):
        return None
    s = result.lstrip()
    # 快速 short-circuit：避免对每个文本 result 都走 json.loads
    if not s.startswith("{") or "_content_blocks" not in s:
        return None
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    blocks = parsed.get("_content_blocks")
    if isinstance(blocks, list) and blocks:
        return blocks
    return None


def _display_text_for_blocks(blocks: List[Dict[str, Any]], result: Any) -> str:
    """从 _content_blocks 派生一段不含 base64 的纯文本，供 SSE 推前端 / log 用。

    优先收集 text block；如果 result 是带 _meta 的 dict / JSON，也并入摘要。
    """
    parts: List[str] = []
    for b in blocks:
        if isinstance(b, dict):
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif b.get("type") == "image":
                src = b.get("source") or {}
                mt = src.get("media_type") or "image"
                parts.append(f"[image attachment: {mt}]")
            elif b.get("type") == "document":
                # 2026-05-12 P3: PDF document block (Anthropic native PDF support)
                src = b.get("source") or {}
                mt = src.get("media_type") or "application/pdf"
                parts.append(f"[document attachment: {mt}]")
    meta_str = ""
    try:
        if isinstance(result, str) and "_meta" in result:
            parsed = json.loads(result)
            meta = parsed.get("_meta") if isinstance(parsed, dict) else None
            if isinstance(meta, dict):
                size = meta.get("size_bytes")
                path = meta.get("path")
                if path:
                    meta_str = f" (path={path}" + (f", size={size}B)" if size else ")")
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return ("\n".join(parts) + meta_str) or "(image attached)"


def _split_text_and_image_blocks(
    blocks: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """从 _content_blocks 拆出 text 字符串和 image blocks 列表。

    多模态延迟图像投递机制 设计（2026-05-12 流派切换）：image 不再嵌 tool_result.content
    （某些反代如 llhub.top 在内部转 OpenAI 协议时会把 tool_result 里的 image
    block 剥光 → 模型只看到 base64 字符串噪声 → 自信瞎编。事故复现见
    scripts/llhub_probe_image_2026-05-12.py 跑出的 case A 对比 case B）。

    解法：image 拆出来当独立 user message 喂给模型，跟 多模态延迟图像投递机制 一致——
    任何反代（真 Anthropic 透传 / 伪 Anthropic 转 OpenAI）的 image-in-user-message
    路径都合法，不会再丢图。本函数负责把 _content_blocks 拆成两半：
    - text 部分聚合成字符串供 caller stuff 进 tool_result.content（含 anchor）
    - image 部分作为独立 list 让 caller 拼到下一条 user message

    Returns:
        (text_str, image_blocks_list)
        - text_str: 非空时含原 text block 内容（多个 text 用 \\n 连）
        - image_blocks_list: 0+ 个 image / image_ref block，原顺序保留
    """
    text_parts: List[str] = []
    image_blocks: List[Dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            txt = b.get("text", "")
            if txt:
                text_parts.append(txt)
        elif btype in ("image", "image_ref", "document"):
            # 2026-05-12 P3: "document" 是 Anthropic 原生 PDF block 类型。
            # 复用 image_blocks 变量名和下游 deferred user message 路径，
            # 避免改名引发其他 6 处调用点联动修改。语义上这是
            # “attachment blocks”不是严格“image blocks”。
            image_blocks.append(b)
    return ("\n".join(text_parts) if text_parts else ""), image_blocks


def _maybe_downgrade_images_for_non_vision_model(
    text_str: str,
    image_blocks: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """非 vision 模型：把 image/document block 降级为文字占位并入 text_str，image_blocks 置空。

    事故背景（2026-05-13 DeepSeek V3/R1 400 根因）：
    `read_file` 读图后 `_split_text_and_image_blocks` 拆出 image block，下游
    `openai_message_converter` 把 image → OpenAI vision 格式 {type:image_url, ...}。
    DeepSeek chat/reasoner（以及其他纯文本 OpenAI 兼容 provider）反序列化 content
    array 时 variant 只认 `text`，直接 400 "unknown variant image_url, expected text"。

    防线策略：
    - 上游（本 helper，llm_client 层）：非 vision 模型本轮的 image blocks 立刻降级
      为文字占位。converter 只看到纯 text，永远不会 emit image_url。
    - vision 模型：原值透传。

    判断依据：`_active_call_config_var` 里 cc.model 经 `backend.providers.capabilities.
    infer_capabilities` 判 "vision" ∈ caps。

    失败模式：拿不到 cc / model / import 出错 → fail-open（返原值，让下游按老路径走，
    非 vision provider 依然会 400，但至少不会静默把 vision 模型的图片误降级）。
    """
    if not image_blocks:
        return text_str, image_blocks
    try:
        cfg = _active_call_config_var.get()
        if cfg is None:
            return text_str, image_blocks  # fail-open: 无 cc
        model_id = getattr(cfg, "model", "") or ""
        if not model_id:
            return text_str, image_blocks  # fail-open: 无 model
        from backend.providers.capabilities import infer_capabilities
        caps = infer_capabilities(model_id)
        if "vision" in caps:
            return text_str, image_blocks  # vision 模型，透传
    except Exception:
        return text_str, image_blocks  # fail-open: 判断失败不误杀

    placeholders: List[str] = []
    for b in image_blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "image":
            src = b.get("source") or {}
            media = src.get("media_type") or "image/unknown"
            placeholders.append(
                f"[image attachment: {media} (pixels unavailable; current model has no vision capability)]"
            )
        elif btype == "document":
            src = b.get("source") or {}
            media = src.get("media_type") or "application/pdf"
            placeholders.append(
                f"[document attachment: {media} (vision/document unsupported on current model)]"
            )
        elif btype == "image_ref":
            sha = (b.get("sha256") or "?")[:8]
            placeholders.append(
                f"[image attachment: sha256={sha}... (pixels unavailable; current model has no vision capability)]"
            )
    combined = "\n".join(placeholders) if placeholders else "[image attachment: pixels unavailable]"
    new_text = (text_str + "\n" + combined) if text_str else combined
    try:
        logger.warning(
            "[VISION-DOWNGRADE] model=%r 不支持 vision，本轮 %s 个 "
            "image/document block 降级为文字占位",
            model_id, len(image_blocks),
        )
    except Exception:
        pass
    return new_text, []


def _build_tool_image_user_message(
    image_blocks: List[Dict[str, Any]],
    tool_names: List[str],
) -> Dict[str, Any]:
    """组装 多模态延迟图像投递机制 的 deferred image user message。

    一条 user message 含 leading text + 1+ image block，紧跟在 tool_results
    user message 之后；模型看到这条 message 时知道是上一轮工具产出的图。

    Args:
        image_blocks: image / image_ref block 列表（一轮 N 个工具的所有图合并）
        tool_names: 关联的工具名列表（用于 leading text；空 / 重复自动归一）

    Returns:
        OpenAI std 格式的 user message dict（content 是 list[block]）。
        - Anthropic 路径：to_anthropic_messages 保留 list 透传
        - OpenAI 路径：to_openai_messages 把 image block 转 image_url
    """
    unique_names = sorted({n for n in tool_names if n})
    # 2026-05-12 P3: 混合 image / document blocks 时说 "file(s)" 更准。
    # 根据 blocks 内容动态选一个贴切措辞：全是 image 说 image(s)，
    # 全是 document 说 document(s)，混合说 file(s)。
    has_image = any(
        isinstance(b, dict) and b.get("type") in ("image", "image_ref")
        for b in image_blocks
    )
    has_document = any(
        isinstance(b, dict) and b.get("type") == "document"
        for b in image_blocks
    )
    if has_image and has_document:
        kind_word = "file(s)"
    elif has_document:
        kind_word = "document(s)"
    else:
        kind_word = "image(s)"
    if unique_names:
        leading = (
            f"Attached {kind_word} from {' / '.join(unique_names)} tool result(s) above:"
        )
    else:
        leading = f"Attached {kind_word} from tool result(s) above:"
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": leading},
            *image_blocks,
        ],
    }


# tool_result.content 里 image / document 被拆走后追加的 anchor 文字。
# 2026-05-12 P3 后拆走的不只 image，还可能是 document（PDF），文字改成通用 attachment。
# 常量名保留 _TOOL_RESULT_IMAGE_ANCHOR 不改，避免联动修改 anthropic_client.py / 测试文件。
_TOOL_RESULT_IMAGE_ANCHOR = " [attachment(s) shown in following user message]"


# ── 工具调用 context（task-local）──
# LLMClient 是全局单例，必须用 ContextVar 隔离 per-task 状态；用实例变量会被
# 并发会话的 chat() 互相覆盖，造成工具/runtime 拿到错误 session_id 的 bug。
_current_agent_id_var: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "llm_current_agent_id", default="default"
)
_current_session_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "llm_current_session_id", default=None
)

# 2026-05-12 P3 方案：当前 LLM 调用的 provider 是否原生支持 Anthropic PDF document block。
# - True：走 Anthropic 原生路径（anthropic_client.chat），PDF 可以 base64 直接喂给 Claude
#   自己解析（质量最高，复杂版式 / 表格 / 公式都能识别）
# - False（默认）：走 OpenAI 兼容路径（含伪 Anthropic 反代），file_tools 会用 pypdf
#   抽文本作为兜底（所有 provider 都能消费的纯文本形态）
#
# 设置点：anthropic_client.chat 入口 set True。llm_client._chat_with_tools 默认 False
# 不显式 set（保持 ContextVar default）。
# 读取点：file_tools._read_pdf 读 var 决定走哪条路径。
#
# 为什么是 ContextVar 而不是判断 self.provider：file_tools.py 是工具层 helper，
# 不持有 LLMClient/AnthropicNative 实例引用；用 ContextVar 解耦层级。同时
# ContextVar 按 asyncio task 隔离，并发会话不互串。
_provider_supports_pdf_native_var: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "llm_provider_supports_pdf_native", default=False
)

# ── per-call model config（per-agent model routing 阶段 3 Commit 3.0）──
# 设计抉择 Q1'（混合 Y）：
#
# - LLMClient.chat 显式接 `call_config: Optional[CallModelConfig] = None` 参数
# - 同时维护这个 ContextVar，chat 一开头自动 set；`_get_api_client` 自动读
# - 调用点（worker / main.py / social_bus 等）外层用 `with use_call_config(cfg):`
#   set 一次，让中间层（agent.chat / LeadAgent.run_streaming / subagent.engine）
#   不必逐层透传——它们最终落到 LLMClient.chat 时 ContextVar 已经 set 好
# - 全程**绝不写**任何 LLMClient 实例字段；并发会话之间 ContextVar 按 asyncio
#   task 隔离，零串扰
#
# 为什么是 ContextVar 而不是实例字段：跟 _current_session_id_var 同样的理由——
# LLMClient 是进程单例，实例字段并发改写会串扰（已踩过的坑，详见上面注释）。
_active_call_config_var: "contextvars.ContextVar[Optional[Any]]" = contextvars.ContextVar(
    "llm_active_call_config", default=None
)

# ── Disable-thinking ContextVar（per-task）──
# 2026-05-25 思考块翻译 v2：辅助任务（如思考块翻译）调用 LLM 时设 True，
# 让两条 provider 路径在构造 CoreRequest 时跳过 reasoning：
# - OpenAI 路径：extras["reasoning_effort"] = "none"（GPT-5/o-series/GLM 等支持）
# - Anthropic 路径：extras["temperature"] = 0.0 → 触发 AnthropicThinkingPlugin
#   "caller 已塞 temperature 时尊重不覆盖" 的逃生舱（plugin
#   @anthropic_thinking.py:67），跳过自动注入 thinking={enabled, ...}
# 不传则保持默认行为（reasoning model 会照常 reason），完全向后兼容。
# 主对话/工具调用/记忆压缩等其他场景**绝不**该设这个 var——
# 它纯粹是给"翻译/字符串处理"这种"不要让 model 自由发挥"的辅助任务用的。
_disable_thinking_var: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "llm_disable_thinking", default=False
)


def use_disable_thinking():
    """Context manager：本块内的 LLM 调用一律关 reasoning。

    用法（典型场景：思考块翻译 / 短字符串处理）::

        with use_disable_thinking():
            result = await client.chat_completion_async(messages)

    与 use_call_config(cfg) 可叠加（ContextVar 互不影响）。
    嵌套安全：内层 set→reset 不污染外层。
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        token = _disable_thinking_var.set(True)
        try:
            yield
        finally:
            _disable_thinking_var.reset(token)

    return _ctx()


def use_call_config(call_config):
    """上下文管理器：在作用域内把 call_config 注入 LLMClient 用的 ContextVar。

    用法（调用点外层）：
        from .llm_call_config import resolve_call_model_config
        from .llm_client import use_call_config

        cfg = resolve_call_model_config(agent_id=..., override=..., session=...)
        with use_call_config(cfg):
            async for event in agent.chat(...):
                ...

    设计：
    - call_config 可以是 None → no-op（让"未配 model 时直接走全局默认"自然降级）
    - 嵌套安全：内层 set 进入后内层生效；reset 后恢复外层
    - asyncio task 隔离：不同 task 之间 ContextVar 各自独立
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        if call_config is None:
            yield
            return
        token = _active_call_config_var.set(call_config)
        try:
            yield
        finally:
            _active_call_config_var.reset(token)

    return _ctx()


_PROVIDER_ERR_MARKERS = (
    "no longer available",
    "transitioned to a paid",
    "error from provider",
    "上游错误",
    "provider returned error",
)


def _extract_provider_message(err_str: str) -> Optional[str]:
    """从 OpenAI SDK 包装的异常 str 里挖出 provider 给最终用户看的 message。

    OpenAI SDK 抛错形如：
        "Error code: 402 - {'error': {'message': '...真正人话...', 'code': 402, 'metadata': {...}}}"

    或纯文本：
        "Error from provider: ...真正人话... https://example.com/upgrade"

    返回提取出的 message（截断 600 字内），无法识别返 None。
    只在能明确识别"这是 provider 业务错误而非内部 SDK 报错"时才返回，避免假阳性。
    """
    if not err_str:
        return None
    text = err_str

    # 优先：尝试用 ast.literal_eval 解出 OpenAI SDK 错误体里的 dict
    # 形如 "Error code: 402 - {'error': {'message': '...', ...}}"
    import ast
    brace = text.find("{")
    if brace >= 0:
        try:
            parsed = ast.literal_eval(text[brace:])
            if isinstance(parsed, dict):
                err_obj = parsed.get("error") or parsed
                if isinstance(err_obj, dict):
                    msg = err_obj.get("message")
                    if isinstance(msg, str) and msg.strip():
                        return msg.strip()[:600]
        except (ValueError, SyntaxError, MemoryError):
            pass  # body 不是合法 dict literal，走下面正则兜底

    # 正则兜底（处理 message 内含撇号 / 半截 dict 情况——粗粒度即可，能识别就行）
    m = re.search(r"['\"]message['\"]\s*:\s*['\"]([^'\"]{6,800})['\"]", text)
    if m:
        msg = m.group(1).strip()
        if msg:
            return msg[:600]

    # 纯文本特征匹配（无 dict 包装的直白错误）
    lowered = text.lower()
    if any(marker in lowered for marker in _PROVIDER_ERR_MARKERS):
        cleaned = text.strip()
        return cleaned[:600] if len(cleaned) > 600 else cleaned
    return None


def _classify_llm_exception(err_type: str, err_str: str) -> tuple[str, str]:
    lowered = (err_str or "").lower()

    if "insufficient_user_quota" in err_str or "预扣费额度失败" in err_str:
        return "llm_quota_exhausted", "API 账户余额不足。请前往模型服务商后台充值，或切换到更便宜的模型。"
    if "invalid_api_key" in err_str or "incorrect api key" in lowered or err_type == "AuthenticationError":
        # missing Authorization: Bearer <key> ⇒ 用户配的 provider 根本没填 API Key（或填了空串）
        # 这类 provider 通常是 OpenAI 兼容的付费网关，必须配 key；区别于 ollama/lm-studio 本地无 key 场景。
        if "missing authorization" in lowered or "missing api key" in lowered:
            return "llm_auth_failed", "该 provider 需要 API Key，但当前模型对应的 provider 未配置 Key（或为空）。请到「模型管理」给它填上 Key，或切换到已配 Key 的模型。"
        return "llm_auth_failed", "API Key 无效或已过期，请在模型管理中检查（或确认该 provider 是否真的需要 Key）。"

    # OpenRouter / 各 provider 透传的业务错误（如“模型转付费”、“模型不可用”、“区域限制”）。
    # OpenAI SDK 把这类错误包成 BadRequestError / APIStatusError，err_str 形如：
    #   "Error code: 402 - {'error': {'message': '...', 'code': 402, 'metadata': {...}}}"
    # 友好文案直接用 provider 给的 message——它就是面向最终用户写的，通常有“前往 xxx 链接继续使用”等指引。
    _provider_msg = _extract_provider_message(err_str)
    if _provider_msg:
        return "llm_provider_error", _provider_msg
    if "rate_limit" in lowered or err_type == "RateLimitError":
        return "llm_rate_limited", "请求过于频繁，模型服务商限流。请稍等几秒后重试。"
    if err_type in ("APITimeoutError", "ReadTimeout", "TimeoutError"):
        return "llm_timeout", "模型请求超时。可能是网络波动或上下文过长，请重试。"
    if "context_length_exceeded" in lowered or "maximum context length" in lowered:
        return "llm_context_overflow", "对话上下文过长，超出模型窗口。可以新建会话或清理一下历史。"

    # Schema 错误：工具定义格式问题
    if "schemacheckerror" in lowered or "schema" in lowered and "required" in lowered:
        return "llm_schema_error", "工具定义格式错误。这是系统内部问题，请联系开发者修复。"

    # 工具不支持错误
    if "tool" in lowered and ("not support" in lowered or "unsupported" in lowered or "不支持" in err_str):
        return "llm_tool_unsupported", "该模型不支持工具调用功能。请切换到支持工具的模型（如 GPT-4、Claude 等）。"

    if (
        err_type == "InternalServerError"
        or "engineinternalerror" in lowered
        or "system is busy" in lowered
        or "please try again later" in lowered
        or "one_api_error" in lowered
    ):
        return "llm_upstream_busy", "模型服务暂时繁忙，请稍后重试；如果持续出现，可切换到其他模型。"
    return "llm_exception", f"调用模型时出错：{err_type}"


# 上游繁忙类错误的 message 特征串。用于兜底识别那些没有规范 retryable 标志
# 或 HTTP 状态码的错误（例如讯飞 Anthropic 协议把 10012 包在 500/200 响应里，
# classify_anthropic_error 会落到 UNKNOWN(retryable=False)）。
_UPSTREAM_BUSY_MARKERS = (
    "engineinternalerror",
    "one_api_error",
    "system is busy",
    "please try again later",
    "internal server error",
    "10012",
)

_RETRYABLE_BUSY_NOTICE_KINDS = frozenset({
    "llm_upstream_busy",
    "llm_rate_limited",
    "llm_timeout",
})


_QUOTA_EXHAUSTED_MARKERS = (
    "freeusagelimit",
    "insufficient_quota",
    "quota exhausted",
    "quota_exhausted",
    "billing",
    "payment required",
    "exceeded your current quota",
    "usage limit",
    "resource has been exhausted",
    "resource_exhausted",
    "额度已达免费上限",
    "配额已耗尽",
    "配额",
)


def _is_retryable_upstream_error(exc: BaseException) -> bool:
    """识别 upstream_busy / overloaded / rate_limit 类可重试错误。

    对讯飞两种接入都健壮：
    - Anthropic 原生协议：classify_anthropic_error 对讯飞 10012 / engineinternalerror
      通常落到 UNKNOWN(retryable=False)，不能只信 retryable flag，要对 message 兜底匹配
    - OpenAI 兼容协议：_classify_llm_exception 已能识别 one_api_error / engineinternalerror

    **配额耗尽短路**：FreeUsageLimitError / insufficient_quota / billing 类是
    "钱/额度问题"不是"短时拥塞"，retry 必然继续失败且白等 4.5s 还给上游加压。
    线上事故 2026-05-06：subagent 撞免费配额时 retry 2 次每次都失败，总耗时
    20s+，前端 20s 看不到任何事件。这类短路 → 立刻抛给上层标 FAILED。
    """
    err_str = str(exc) or ""
    lowered = err_str.lower()

    # 配额耗尽类：直接判定不可重试（必须先于其它分支判断）
    if any(marker in lowered for marker in _QUOTA_EXHAUSTED_MARKERS):
        return False

    if isinstance(exc, LLMError):
        # 2026-05-21 NETWORK_ERROR 短路：anthropic_client 内层已自己重试 2 次
        # （round_yielded_useful 锁 + exp backoff），外层不该把 NETWORK_ERROR 当
        # 业务繁忙再来 1+2=3 轮——之前的叠加最坏 9 次重试，前端 banner "1/3" 重复
        # 三次循环。OpenAI path 的 silent-stall 也走 NETWORK_ERROR；按相同语义
        # 一次失败即视为真断开，不重试。外层只负责"上游业务繁忙"（OVERLOADED /
        # RATE_LIMIT / 讯飞 10012 / one_api_error 等）。
        if exc.code == LLMErrorCode.NETWORK_ERROR:
            return False
        if exc.retryable:
            return True
        if exc.code in (LLMErrorCode.OVERLOADED, LLMErrorCode.RATE_LIMIT):
            return True
        if any(marker in lowered for marker in _UPSTREAM_BUSY_MARKERS):
            return True
        return False

    err_type = type(exc).__name__
    code, _ = _classify_llm_exception(err_type, err_str)
    if code in ("llm_upstream_busy", "llm_rate_limited", "llm_timeout"):
        return True
    return any(marker in lowered for marker in _UPSTREAM_BUSY_MARKERS)


def _parse_retry_backoffs(raw: str) -> List[float]:
    result: List[float] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = float(part)
        except ValueError:
            continue
        if val > 0:
            result.append(val)
    return result or [1.5, 3.0]


# load_dotenv() 已移到顶部 import 段（避免子模块 module-load 时拿不到 env）


class LLMClient:
    """LLM 客户端（支持多提供商）"""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None, provider: str = None, default_headers: Dict[str, str] = None):
        self.provider = provider or os.getenv("LLM_PROVIDER", "openai").lower()
        self.api_key = api_key
        self.base_url = base_url
        self.default_headers = dict(default_headers) if default_headers else None
        self.model = model or os.getenv("OPENAI_MODEL", "astron-code-latest")
        self.stream_idle_timeout = int(
            os.getenv("OPENAI_STREAM_IDLE_TIMEOUT_MS")
            or os.getenv("LLM_STREAM_IDLE_TIMEOUT_MS")
            or "180000"
        ) / 1000

        # 延迟初始化 Anthropic client（OpenAI 兼容路径走 ApiClient registry，
        # 不再需要 SDK instance 字段）
        # _anthropic_client 字段保留兼容性，但 _ensure_anthropic_client 实际逻辑
        # 已切到 _anthropic_client_pool（per-agent model routing 阶段 3.5 · Bug 3）。
        # 阶段 4 清理时一并删。
        self._anthropic_client = None
        # AnthropicClient pool（per-agent model routing 阶段 3.5 · Bug 3 修补）：
        # 老路径单坑位缓存 + 不读 ContextVar，导致并发用不同 Claude 配置时互踩。
        # dict pool 按 (api_key, base_url, model) 三元组缓存，LRU(10) 防泄漏。
        # 行为对齐 OpenAI 路径的 ProviderRegistry LRU——同 cc 跨 chat 复用
        # keep-alive，不同 cc 各自实例不串扰。
        self._anthropic_client_pool: Dict[Tuple[str, str, str], Any] = {}
        # 池上限对齐 OpenAI 路径的 ProviderRegistry._DEFAULT_CACHE_SIZE=10
        # （provider_registry.py:27），保持两条路径行为完全对称。
        self._anthropic_pool_max = 10
        self.tool_registry = get_tool_registry()

        # 工具调用 context：用 ContextVar 而非实例变量。
        # 之前用 self._current_* 是 BUG：LLMClient 是全局单例，多并发会话同时跑
        # use_tools=True 时后到的会话会覆盖前一个的 session_id，导致工具/runtime
        # 把状态挂到错误的 parent_session_id（A 的事件 → 跑到 B 的卡片）。
        # ContextVar 按 asyncio task 隔离上下文，每个 chat 调用的子任务拷贝独立 context。
        # 这两个 ContextVar 故意做成实例属性，方便测试中 reset；模块级单实例下行为等价。

    def _ensure_anthropic_client(self):
        """按当前 ContextVar cc 拿 / 建 AnthropicClient（dict pool LRU 命中 ~µs）。

        per-agent model routing 阶段 3.5 · Bug 3 修补（2026-05-11）：
        老路径单坑位缓存 + 不读 ContextVar，并发用不同 Claude 配置时互踩。
        改为 dict pool 按 (api_key, base_url, model) 三元组缓存，LRU(8) 防泄漏。

        cc 缺失时（无 use_call_config 包裹）fallback 到 self.{api_key,model,base_url}，
        保持向后兼容（worker 路径少数边角调用 + 兼容老测试）。

        并发安全：
        - 同 cc 命中同一 client → 共享 AsyncAnthropic + httpx 连接池（协程安全）
        - 不同 cc 各自的 cache_key → 各自实例，零互踩
        - 池满弹出时若该 client 仍被某 task 持有，引用计数不为 0，GC 不回收，
          task 用完正常释放。下次同 cc 来重建一次（极小窗口性能损失）

        与 OpenAI registry LRU 行为对等：跨 chat 同 cc 复用 HTTP keep-alive，
        无连接重建开销（海外 Claude 直连场景节省 0.6-1s/chat 的 TCP+TLS 握手）。
        """
        cc = _active_call_config_var.get()
        if cc is not None:
            api_key = cc.api_key or self.api_key
            model = cc.model
            base_url = cc.base_url or self.base_url
            default_headers = cc.headers_dict() or self.default_headers
        else:
            api_key = self.api_key
            model = self.model
            base_url = self.base_url
            default_headers = self.default_headers

        cache_key = (api_key or "", base_url or "", model or "")

        if cache_key in self._anthropic_client_pool:
            # LRU 触摸：移到末尾（dict 在 Python 3.7+ 保序，迭代顺序 = 插入顺序）
            client = self._anthropic_client_pool.pop(cache_key)
            self._anthropic_client_pool[cache_key] = client
            return client

        if len(self._anthropic_client_pool) >= self._anthropic_pool_max:
            # 池满，弹出最早的
            self._anthropic_client_pool.pop(next(iter(self._anthropic_client_pool)))

        from .anthropic_client import AnthropicClient
        client = AnthropicClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            default_headers=default_headers,
        )
        self._anthropic_client_pool[cache_key] = client
        return client

    def _resolve_active_model(self) -> str:
        """读当前 ContextVar 里的 cfg.model，缺失时回 self.model。

        per-agent model routing 阶段 3 Commit 3.0+ 修补（2026-05-09）：
        所有 CoreRequest 构造点都必须用这个 helper 而不是直接 self.model——
        否则 _get_api_client 已经按 cfg 选对 provider/url/key，但请求 body 里
        model 仍是 self.model（残留的全局 default 比如 'astron-code-latest'），
        会导致打到 deepseek 端点却带 model=astron-code-latest → 401 ModelError。
        这是 user 2026-05-09 23:18 报的真实事故根因。
        """
        cc = _active_call_config_var.get()
        return cc.model if cc is not None else self.model

    def _get_api_client(self):
        """OpenAI 兼容路径拿 ApiClient（registry LRU cache 命中 ~µs；fail-fast on
        ProviderNotFoundError）。

        共用给 _chat_one_round / _chat_stream / _chat_complete。base_url 已
        进 cache key（v2 P1-E-2b），切讯飞反代 / 自建网关不会命中老实例。

        per-agent model routing 阶段 3 Commit 3.0：
        优先读 `_active_call_config_var`（ContextVar，per-task 隔离），有则用 cfg
        的字段；没有则 fallback 到 self.{provider,model,api_key,base_url,default_headers}。
        这是混合 Y 方案的核心——并发安全的 model 配置注入点。
        """
        from liuruyan.llm import (
            ProviderNotFoundError,
            get_default_registry,
        )

        # 优先读 ContextVar（per-call cfg）
        cc = _active_call_config_var.get()
        if cc is not None:
            provider = cc.provider_kind
            provider_id = getattr(cc, "provider_id", "")
            model = cc.model
            api_key = cc.api_key
            base_url = cc.base_url
            default_headers = cc.headers_dict()
        else:
            provider = self.provider
            provider_id = getattr(self, "provider", "")
            model = self.model
            api_key = self.api_key
            base_url = self.base_url
            default_headers = self.default_headers

        if provider_id == "google-antigravity" or provider in ("google-antigravity", "google"):
            try:
                from backend.compute_accounts.antigravity_client import AntigravityApiClient
                return AntigravityApiClient(model)
            except ImportError:
                pass

        try:
            return get_default_registry().get_client(
                provider,
                model,
                api_key=api_key,
                base_url_override=base_url,
                # Slice 4: 注入工具名白名单 callable，给 text_leaked_tool_call
                # grammar 用（讯飞 GLM5 等文本泄漏 tool_call 救火）。
                # ⚠️ harness 永不 import backend，所以 callable 由 caller 提供
                # 延迟读取——self.tool_registry 是 LLMClient 单例字段，闭包安全。
                get_known_tool_names=lambda: set(self.tool_registry.tools.keys())
                if hasattr(self, "tool_registry") and self.tool_registry is not None
                else set(),
                default_headers=default_headers,
            )
        except ProviderNotFoundError as exc:
            raise LLMError(
                f"Unknown provider {provider!r}: {exc}",
                LLMErrorCode.RESPONSE_FORMAT,
                retryable=False,
            ) from exc

    def set_model(self, model: str, base_url: str = None, api_key: str = None, provider: str = None, default_headers: Dict[str, str] = None):
        """切换模型（运行时动态切换）

        Args:
            model: 模型 ID
            base_url: API 地址（可选）
            api_key: API Key（可选）
            provider: 提供商类型 "openai" 或 "anthropic"（可选）
        """
        self.model = model
        if provider:
            self.provider = provider.lower()
        if api_key is not None:
            self.api_key = api_key
        if base_url is not None:
            self.base_url = base_url
        self.default_headers = dict(default_headers) if default_headers else None

        # 重置 Anthropic client 实例，下次调用时重新初始化
        # （OpenAI 兼容路径走 ApiClient registry，cache key 含 base_url 自动区分）
        self._anthropic_client = None

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        use_tools: bool = True,
        agent_id: str = "default",
        session_id: str = None,
        prompt_cache_info: Dict = None,
        chain_hook=None,
        allowed_tools_override: Optional[List[str]] = None,
        call_config: Optional[Any] = None,
    ) -> AsyncIterator[Dict]:
        """发送聊天请求（外层 wrapper：处理 call_config ContextVar 注入）。

        per-agent model routing 阶段 3 Commit 3.0：
        - `call_config` 是单次调用的 model 配置快照（CallModelConfig），可选
        - 显式传入时这里 set ContextVar，整个 chat 流程用 cfg 的 provider/model/key/url
        - 不传时调用方应在外层用 ``with use_call_config(cfg):`` 设置（间接调用链场景）
        - 都不设时 fallback 到 self.{provider,model,...} 字段（向后兼容老路径）

        实际 LLM 流程逻辑在 `_chat_with_retry` 里，这里只负责 cfg 生命周期。
        """
        if call_config is None:
            async for ev in self._chat_with_retry(
                messages,
                stream=stream,
                use_tools=use_tools,
                agent_id=agent_id,
                session_id=session_id,
                prompt_cache_info=prompt_cache_info,
                chain_hook=chain_hook,
                allowed_tools_override=allowed_tools_override,
            ):
                yield ev
            return
        token = _active_call_config_var.set(call_config)
        try:
            async for ev in self._chat_with_retry(
                messages,
                stream=stream,
                use_tools=use_tools,
                agent_id=agent_id,
                session_id=session_id,
                prompt_cache_info=prompt_cache_info,
                chain_hook=chain_hook,
                allowed_tools_override=allowed_tools_override,
            ):
                yield ev
        finally:
            _active_call_config_var.reset(token)

    async def _chat_with_retry(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        use_tools: bool = True,
        agent_id: str = "default",
        session_id: str = None,
        prompt_cache_info: Dict = None,
        chain_hook=None,
        allowed_tools_override: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict]:
        """发送聊天请求，支持流式输出和工具调用（返回结构化事件）。

        外层是一个轻量 retry wrapper，针对上游繁忙（讯飞 10012 / engineinternalerror
        / one_api_error / 429 / 529 等）在尚未产出有用事件的窗口内自动重试。工具循环
        一旦启动（首个 content / tool_call / tool_result / thinking 事件 yield 之后）
        即放弃重试以避免副作用重放。

        Args:
            agent_id: 代理 ID
            session_id: 会话 ID（用于工具调用上下文）
            prompt_cache_info: Prompt caching 配置信息，包含:
                - enable_caching: 是否启用缓存
                - static: 静态部分（可缓存）
                - dynamic: 动态部分（不缓存）
            chain_hook: v2 C2-c/C2-d 反向注入钩子（每轮 LLM 完成后调）。
                签名 ``async (round_done: dict) -> bool``。返 True 时立即终止
                工具循环（chain 决定 should_end）。透传给两条 provider 路径：
                - Anthropic provider: 经 AnthropicClient.chat → _chat_with_tools
                - OpenAI provider: 经 LLMClient._chat_with_tools → 每轮 _chat_one_round
                让 mw 08 LoopDetection / mw 14 TitleGen / mw 15 MemoryQueue /
                mw 16 SubagentLimit / mw 17 Clarification 等 after_model 钩子
                在两条 provider 路径上都真生效。
            allowed_tools_override: 可选工具白名单覆盖。当不为 None 时**绕过**
                ``get_agent_manager().get_allowed_tools(agent_id)`` 的查询，
                直接以本列表为白名单。
                Subagent 用法：engine 把 template["tools"] 减去 disallowed 后
                作为 override 传入，确保子代理不会调用模板外的工具。
                None 时维持老行为（按 agent_id 查 manager 白名单）。
                空列表 ``[]`` = 一个工具都不给（合法语义）。
        """
        # 保存当前 agent_id 和 session_id 供工具调用时使用。
        # 注意：LLMClient 是全局单例（get_llm_client()）+ self._current_* 是实例变量
        # = 跨 asyncio task 共享。如果无条件赋值，use_tools=False 的内部辅助调用
        # （chat_completion_async / chat_completion / web_extract 摘要等）会把状态
        # 污染成 default/None，破坏并发跑的 use_tools=True 主对话流的工具调用 context
        # （root cause: 工具/runtime context missing: session_id=None, agent_id='default'）。
        # use_tools=False 路径不调工具，self._current_* 跟它们无关；这里只在 use_tools=True
        # 时设状态。
        if use_tools:
            # ContextVar.set 返回的 token 仅当前 task context 生效，
            # 不会污染并发的其他 chat() 调用的 context。
            _current_agent_id_var.set(agent_id)
            _current_session_id_var.set(session_id)

        max_retries = max(0, int(os.getenv("LLM_RETRY_ON_BUSY_MAX", "2") or 0))
        backoffs = _parse_retry_backoffs(os.getenv("LLM_RETRY_ON_BUSY_BACKOFFS", "1.5,3.0"))

        attempt = 0
        while True:
            yielded_useful = False
            retry_requested = False

            try:
                async for event in self._chat_once(
                    messages,
                    stream=stream,
                    use_tools=use_tools,
                    agent_id=agent_id,
                    session_id=session_id,
                    prompt_cache_info=prompt_cache_info,
                    chain_hook=chain_hook,
                    allowed_tools_override=allowed_tools_override,
                ):
                    etype = event.get("type")
                    kind = event.get("kind")

                    if (
                        etype == "system_notice"
                        and kind in _RETRYABLE_BUSY_NOTICE_KINDS
                        and not yielded_useful
                        and attempt < max_retries
                    ):
                        retry_requested = True
                        break

                    if etype in ("content", "tool_call", "tool_result", "thinking"):
                        yielded_useful = True
                    yield event

                if not retry_requested:
                    return
            except Exception as exc:
                if (
                    not yielded_useful
                    and attempt < max_retries
                    and _is_retryable_upstream_error(exc)
                ):
                    retry_requested = True
                else:
                    # C4-c-2：non-retryable 异常 raise 前调 chain_hook，让 mw 06
                    # 在 anthropic + OpenAI 任意 provider 的"未被内层归一化的异常"
                    # 路径上真生效（anthropic 路径异常直接 raise LLMError 不 yield，
                    # OpenAI 内层 fallback 已在 _chat_with_tools C4-c 调过一次但
                    # 外层兜底也调一次以覆盖罕见的内层 fallback 抛异常情况）。
                    # chain_hook 调用异常 swallow + log，不影响原异常 raise 语义。
                    if chain_hook is not None:
                        try:
                            err_type = type(exc).__name__
                            err_str = str(exc)
                            kind, friendly = _classify_llm_exception(err_type, err_str)
                            error_round_done = {
                                "type": "_round_done",
                                "finish_reason": None,
                                "has_tool_calls": False,
                                "has_invalid_tool_calls": False,
                                "empty_response": False,
                                "text_only": False,
                                "current_content": "",
                                "assistant_msg": None,
                                "all_tool_call_ids": [],
                                "tool_result_msgs": [],
                                "executed_tool_calls": [],
                                "executed_tool_results": [],
                                "spawn_dispatched": False,
                                "invalid_tool_calls": [],
                                "llm_error": {
                                    "kind": kind,
                                    "friendly": friendly,
                                    "err_type": err_type,
                                    "err_str": err_str,
                                },
                            }
                            await chain_hook(error_round_done)
                        except Exception:
                            import traceback
                            logger.error(
                                "[LLMClient] chain_hook (chat outer error path) "
                                "failed: %s",
                                traceback.format_exc(),
                            )
                    raise

            backoff = backoffs[min(attempt, len(backoffs) - 1)]
            yield {
                "type": "system_notice",
                "kind": "llm_upstream_retry",
                "content": (
                    f"模型上游繁忙，{backoff:.1f}s 后自动重试"
                    f"（第 {attempt + 1}/{max_retries} 次）"
                ),
            }
            await asyncio.sleep(backoff)
            attempt += 1

    async def _chat_once(
        self,
        messages: List[Dict[str, str]],
        *,
        stream: bool,
        use_tools: bool,
        agent_id: str,
        session_id: Optional[str],
        prompt_cache_info: Optional[Dict],
        chain_hook=None,
        allowed_tools_override: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict]:
        """单次 LLM 尝试（不含重试）。保留原 chat() 的 provider dispatch 逻辑。

        v2 C2-d-3：chain_hook 真透传到两条 provider 路径（Anthropic + OpenAI），
        让 mw 08/14/15/16/17 等 after_model 钩子在两条路径上都真生效。
        """
        # 入口 provider 分流必须读 ContextVar——self.provider 是全局 default，
        # 切 Claude 时 cc.provider_kind=="anthropic"，但 self.provider 残留 OpenAI
        # 会把 Claude 请求错误送进 OpenAI 路径 → AnthropicApiClient 收 OpenAI-only
        # 字段（stream_options / tool_choice）→ TypeError。
        # per-agent model routing 阶段 3.5 · Bug 1 修补（2026-05-11）：补阶段 3
        # 当年只覆盖 OpenAI 路径的遗漏。
        cc = _active_call_config_var.get()

        effective_provider = cc.provider_kind if cc is not None else self.provider
        effective_provider_id = getattr(cc, "provider_id", "") if cc is not None else getattr(self, "provider", "")

        # Anthropic 原生模式
        if effective_provider == "anthropic":
            async for event in self._ensure_anthropic_client().chat(
                messages, stream, use_tools,
                agent_id=agent_id,
                session_id=_current_session_id_var.get(),
                prompt_cache_info=prompt_cache_info,
                chain_hook=chain_hook,
                allowed_tools_override=allowed_tools_override,
            ):
                yield event
            return

        # OpenAI 兼容模式（不支持缓存）
        if use_tools:
            async for event in self._chat_with_tools(
                messages, stream,
                chain_hook=chain_hook,
                allowed_tools_override=allowed_tools_override,
            ):
                yield event
        else:
            if stream:
                async for event in self._chat_stream(messages):
                    yield event
            else:
                result = await self._chat_complete(messages)
                yield {"type": "content", "text": result}

    @staticmethod
    def _usage_field(usage_obj, key: str, default=None):
        """usage 字段读取：OpenAI SDK 是属性对象，Antigravity/自研客户端给的是 dict。
        两种形态都要能取，否则 dict 形态的 usage 全部归零（bug：算力账号聊天
        顶栏用量徽章永远显示 '—'）。"""
        if isinstance(usage_obj, dict):
            return usage_obj.get(key, default)
        return getattr(usage_obj, key, default)

    @staticmethod
    def _normalize_openai_usage(usage_obj) -> dict | None:
        """OpenAI 协议 usage 对象 → 跟 Anthropic 同语义的 4 字段 dict。

        关键归一化：OpenAI 规范的 `prompt_tokens` **包含** cached 部分，
        Anthropic 规范的 `input_tokens` **不含** cache。为了让两条路径下
        `total = input + output + cache_read + cache_create` 都不重复计数、
        且 `input` 字段语义一致，这里把 cached 部分剥出 input 入 cache_read。

        cached_tokens 字段在 `prompt_tokens_details.cached_tokens`（OpenAI 标准；
        讯飞兼容 / DeepSeek / 智谱等多数兼容协议遵循）。拿不到字段时回退 0，
        行为退回到"全 prompt 计入 input、cache 字段 0"的老语义。

        Returns:
            None - usage_obj 为 None 或全 0
            dict - {input_tokens, output_tokens, cache_read, cache_create}
        """
        if not usage_obj:
            return None
        try:
            _get = LLMClient._usage_field
            prompt_total = int(_get(usage_obj, "prompt_tokens", 0) or 0)
            completion = int(_get(usage_obj, "completion_tokens", 0) or 0)
            details = _get(usage_obj, "prompt_tokens_details", None)
            cached = 0
            if details is not None:
                cached = int(_get(details, "cached_tokens", 0) or 0)
                if cached < 0:
                    cached = 0
            # cached 不能超过 prompt_total（防协议返脏数据）
            cached = min(cached, prompt_total)
            return {
                "input_tokens": max(prompt_total - cached, 0),
                "output_tokens": completion,
                "cache_read": cached,
                "cache_create": 0,  # OpenAI 协议无对应字段
            }
        except Exception:
            return None

    @staticmethod
    def _extract_openai_round_token_usage(usage_obj) -> dict | None:
        """从 OpenAI usage 对象提取本轮 token 增量给 mw 10 用。

        跟 _record_openai_usage 用同一份归一化逻辑（_normalize_openai_usage）。
        失败返 None（mw 10 跳过 emit）。
        """
        return LLMClient._normalize_openai_usage(usage_obj)

    async def _record_openai_usage(self, usage_obj) -> None:
        """从 OpenAI usage 对象（chat.completions.usage 或 stream chunk.usage）累加 token。

        只在 _current_session_id 已设置时算账（即 use_tools=True 主路径）。
        失败静默吞掉，不污染 LLM 主流程。
        """
        _sid = _current_session_id_var.get()
        if not _sid:
            return
        normalized = self._normalize_openai_usage(usage_obj)
        if not normalized:
            return
        try:
            from .session import get_session_manager
            await get_session_manager().record_token_usage(
                _sid,
                input_tokens=normalized["input_tokens"],
                output_tokens=normalized["output_tokens"],
                cache_read=normalized["cache_read"],
                cache_create=normalized["cache_create"],
            )
        except Exception:
            pass

    def _normalize_tool_arguments(self, raw_args: Any, tool_name: str) -> tuple[Dict[str, Any], List[int]]:
        """
        标准化工具参数，返回 (解析后的参数, 触发的修复层列表)
        """
        repair_layers = []

        if raw_args is None or raw_args == "":
            return {}, repair_layers
        if isinstance(raw_args, dict):
            return raw_args, repair_layers
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                # 尝试多种修复策略
                import re
                fixed = raw_args

                # 策略 1: 修复未转义的反斜杠（Windows 路径）
                try:
                    fixed = fixed.replace('\\', '\\\\').replace('\\\\\\\\', '\\\\')
                    parsed = json.loads(fixed)
                    repair_layers.append(1)
                except json.JSONDecodeError:
                    # 策略 2: 修复未闭合的字符串（尝试补全结尾引号和括号）
                    try:
                        # 检查是否缺少结尾引号
                        if fixed.count('"') % 2 == 1:
                            fixed = fixed + '"}'
                        # 检查是否缺少结尾括号
                        elif not fixed.rstrip().endswith('}'):
                            fixed = fixed + '}'
                        parsed = json.loads(fixed)
                        repair_layers.append(2)
                    except json.JSONDecodeError:
                        # 策略 3: 提取部分有效的 JSON（截断到最后一个完整的键值对）
                        try:
                            # 找到最后一个完整的键值对位置
                            last_complete = fixed.rfind('",')
                            if last_complete > 0:
                                fixed = fixed[:last_complete+1] + '}'
                                parsed = json.loads(fixed)
                                repair_layers.append(3)
                            else:
                                raise exc
                        except json.JSONDecodeError:
                            # 策略 4: 修复多键错误（合并到 command 字段）
                            try:
                                matches = re.findall(r'"([^"]+)":\s*"([^"]*)"', raw_args)
                                if matches and any(k == 'command' for k, _ in matches):
                                    command_parts = [v for k, v in matches]
                                    parsed = {"command": '\n'.join(command_parts)}
                                    repair_layers.append(4)
                                else:
                                    raise exc
                            except json.JSONDecodeError:
                                # 策略 5: 裸 glob/路径当值（如 **/*.py 没加引号）
                                # 模式：":  <非引号-非数字-非合法 JSON token 起始字符> <直到 , 或 }>"
                                # 命中后给值加引号；专门匹配 glob 通配符 / 含路径分隔符的裸 token。
                                try:
                                    fixed5 = re.sub(
                                        r':\s*([*?][^,}\]]*?)(?=\s*[,}\]])',
                                        lambda m: ': "' + m.group(1).strip() + '"',
                                        raw_args,
                                    )
                                    if fixed5 != raw_args:
                                        parsed = json.loads(fixed5)
                                        repair_layers.append(5)
                                    else:
                                        raise exc
                                except (json.JSONDecodeError, Exception):
                                    preview = raw_args[:200].replace("\n", "\\n")
                                    raise LLMError(
                                        f"Tool input JSON parse failed for '{tool_name}': {exc}. Raw: {preview}",
                                        LLMErrorCode.RESPONSE_FORMAT,
                                        retryable=False,
                                    ) from exc
            if isinstance(parsed, dict):
                return parsed, repair_layers
            raise LLMError(
                f"Tool input for '{tool_name}' must be a JSON object, got {type(parsed).__name__}",
                LLMErrorCode.RESPONSE_FORMAT,
                retryable=False,
            )
        raise LLMError(
            f"Tool input for '{tool_name}' must be an object or JSON string, got {type(raw_args).__name__}",
            LLMErrorCode.RESPONSE_FORMAT,
            retryable=False,
        )

    def _build_invalid_tool_call_notice(self, invalid_calls: List[Dict[str, str]]) -> str:
        count = len(invalid_calls)
        samples = []
        for item in invalid_calls[:3]:
            samples.append(f"- {item['name']}: {item['error']}")
        sample_text = "\n".join(samples)
        suffix = "" if count <= 3 else f"\n- 另有 {count - 3} 个工具调用同样无效"
        return (
            f"检测到 {count} 个工具调用参数格式无效，已跳过执行并请求模型重试。\n"
            f"{sample_text}{suffix}"
        )

    def _build_tool_retry_message(self, invalid_calls: List[Dict[str, str]]) -> str:
        details = []
        for item in invalid_calls[:3]:
            details.append(
                f"- 工具: {item['name']}\n"
                f"  错误: {item['error']}\n"
                f"  原始参数片段: {item['raw_arguments']}"
            )
        return (
            "[系统] 你上一轮发出的工具调用参数不是有效 JSON 对象，工具未执行。\n"
            "请只重新发送修正后的工具调用，参数必须是合法 JSON object，不要补充解释，不要把参数写成半截字符串。\n\n"
            + "\n".join(details)
        )

    def _sanitize_tool_call_pairing(self, messages: List[Dict[str, Any]]) -> int:
        """In-place 自愈历史 messages 里 assistant.tool_calls 缺对应 tool message 的残缺。

        历史残缺通常来自上一次 chat 的 tool execute 抛异常或被中断没补齐，
        新一轮 chat 复用同一份 history 时被 gpt-5 严格校验拒（"No tool output found
        for function call call_xxx" 400）。把缺的补成占位 tool message，让 LLM 看到
        旧调用没结果而不是直接 400。返回插入条数（用于日志/调试）。
        """
        inserted = 0
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_calls = msg.get("tool_calls") or []
                ordered_ids: List[str] = []
                id_to_name: Dict[str, str] = {}
                for tc in tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if not tc_id:
                        continue
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    name = fn.get("name") if isinstance(fn, dict) else None
                    ordered_ids.append(tc_id)
                    id_to_name[tc_id] = name or "?"
                pending = set(ordered_ids)
                j = i + 1
                while j < len(messages) and pending:
                    nxt = messages[j]
                    if nxt.get("role") == "tool":
                        tid = nxt.get("tool_call_id")
                        if tid in pending:
                            pending.discard(tid)
                        j += 1
                    else:
                        break
                if pending:
                    pad = [
                        {
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": (
                                f"[recovered] Tool call '{id_to_name.get(tid, '?')}' "
                                f"(id={tid}) had no recorded output — likely the previous run was "
                                f"interrupted or the tool raised. Treat as failed."
                            ),
                        }
                        for tid in ordered_ids if tid in pending
                    ]
                    messages[j:j] = pad
                    inserted += len(pad)
                    j += len(pad)
                i = j
            else:
                i += 1
        return inserted

    def _pad_tool_messages_for_invalid_batch(
        self,
        messages: List[Dict[str, Any]],
        all_tool_call_ids: List[str],
        invalid_id_to_error: Dict[str, str],
    ) -> None:
        """invalid 分支补齐 messages 配对（gpt-5 严格校验：assistant.tool_calls 必须每个都有匹配 tool message）

        invalid 那些 → tool message content 写解析错误；同批次 valid 那些（部分 invalid 场景下不会执行）→ 写占位说明。
        Anthropic 路径不走此函数（其 native protocol 自带 args schema 校验，invalid_tool_calls 概念只在 OpenAI 兼容层）。
        """
        for tc_id in all_tool_call_ids:
            if not tc_id:
                continue
            err = invalid_id_to_error.get(tc_id)
            if err is not None:
                content = f"Error: invalid tool arguments — {err}"
            else:
                content = "Tool not executed: another tool call in this batch had invalid arguments; the batch will be retried."
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": content,
            })

    def _build_invalid_tool_arguments_stop(
        self,
        invalid_calls: List[Dict[str, str]],
        retry_count: int,
        retry_limit: int,
    ) -> ToolLoopStop:
        return ToolLoopStop(
            reason="invalid_tool_arguments_exhausted",
            message=(
                f"工具调用参数连续 {retry_count} 次无效，已达到重试上限（{retry_limit} 次）。"
                "不要再继续请求工具，请基于当前已有信息直接给出结论，"
                "如果仍有缺口请明确说明。"
            ),
            payload={
                "invalid_tool_call_count": len(invalid_calls),
                "invalid_tool_retry_count": retry_count,
                "invalid_tool_retry_limit": retry_limit,
            },
        )

    async def _chat_stream(self, messages: List[Dict[str, str]]) -> AsyncIterator[Dict[str, str]]:
        """流式输出（无工具）。

        v2 A 迁移：内部从直调 self._ensure_openai_client().chat.completions.create
        切到 ApiClient.send_stream。reasoning_content（GLM 等）由 parser 自动
        翻译为 ParsedEvent.reasoning_delta。
        """
        api_client = self._get_api_client()
        from liuruyan.llm import CoreRequest, RequestContext
        from .runtime_budget import get_effective_max_output_tokens, get_effective_stream_idle_timeout

        # 2026-05-25：辅助任务（思考块翻译等）通过 _disable_thinking_var 关 reasoning。
        # 主对话路径用 _chat_with_tools / _chat_one_round 不走这里，无影响。
        #
        # 2026-05-26 踩坑：早期版本传 `reasoning_effort="none"` 触发了某些 provider
        # 的 schema 校验 400（"unknown variant `none`, expected one of `high/low/
        # medium/max/xhigh`"）。`none` 不是任何标准 provider 的合法值——
        # OpenAI 官方接 `minimal/low/medium/high`，第三方各家白名单不一。
        # 退一步用 `low` 也不行：纯 chat model 的 provider 看到陌生字段会 400。
        #
        # 当前策略：OpenAI 兼容路径**不**传 reasoning_effort，靠翻译 prompt 的
        # XML 硬边界 + 否定式约束 + few-shot 兜底；Anthropic 路径继续在
        # AnthropicClient.chat 里走 temperature=0 → AnthropicThinkingPlugin 逃生舱
        # （那条路是 plugin 内部读 caller-provided temperature 决定要不要注入
        # thinking={enabled,...}，行为完全可控，不会 schema 错）。
        # 真要彻底关 reasoning 的用户：去设置→模型服务→「辅助模型」选个
        # 非 reasoning 的 fast model，比 in-band 字段控制可靠多了。
        extras: Dict[str, Any] = {}

        core_req = CoreRequest(
            messages=messages,
            system=None,
            tools=None,
            model=self._resolve_active_model(),
            max_tokens=get_effective_max_output_tokens("openai"),
            extras=extras,
        )
        core_ctx = RequestContext(cancel_event=None)

        # chunk-level idle timeout helper：详见 backend/llm_stream_utils.py
        # P3-stream-timeout-cfg：从 runtime_budget.json 读，让用户在“预算”对话框里能调。
        from .llm_stream_utils import iter_stream_with_idle_timeout
        idle_timeout = get_effective_stream_idle_timeout("openai")
        async for ev in iter_stream_with_idle_timeout(
            api_client.send_stream(core_req, core_ctx),
            idle_timeout,
            label="openai",
        ):
            if ev.type == "content_delta":
                yield {"type": "content", "text": ev.text}
            elif ev.type == "reasoning_delta":
                yield {"type": "thinking", "text": ev.text}
            # tool_call_* / usage / round_end / error 等其他事件类型本路径
            # 不消费（无工具调用 / 无 token 算账）

    async def _chat_complete(self, messages: List[Dict[str, str]]) -> str:
        """非流式输出。

        优先走 ApiClient.send_once()（真正的 stream=False，避免某些 provider
        流式卡死导致超时）；没有 send_once 的 client 退回累积 _chat_stream。
        """
        api_client = self._get_api_client()
        if hasattr(api_client, "send_completion"):
            from liuruyan.llm import CoreRequest, RequestContext
            from .runtime_budget import get_effective_max_output_tokens
            req = CoreRequest(
                messages=messages,
                system=None,
                tools=None,
                model=self._resolve_active_model(),
                max_tokens=get_effective_max_output_tokens("openai"),
                extras={},
            )
            ctx = RequestContext(cancel_event=None)
            result = await api_client.send_completion(req, ctx)
            return (result.get("content") or "").strip()

        parts: List[str] = []
        async for event in self._chat_stream(messages):
            if event.get("type") == "content":
                parts.append(event.get("text", ""))
        return "".join(parts)

    async def _finalize_after_tool_loop(
        self,
        messages: List[Dict[str, Any]],
        stop: ToolLoopStop,
    ) -> str:
        final_messages = list(messages)
        final_messages.append({
            "role": "user",
            "content": build_tool_loop_user_message(stop)
        })
        return await self._chat_complete(final_messages)

    def chat_completion(self, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        """同步非流式调用（供记忆提炼等后台任务使用）

        Args:
            messages: 消息列表 [{"role": "...", "content": "..."}]
            **kwargs: 额外参数（兼容性，当前未使用）

        Returns:
            {"content": "回复文本"} 或异常时返回 {"content": ""}
        """
        import asyncio

        call_config = kwargs.get("call_config")
        if call_config is None:
            call_config = _active_call_config_var.get()
        if call_config is None:
            try:
                from .llm_call_config import resolve_call_model_config
                call_config = resolve_call_model_config()
            except Exception:
                pass

        async def _do_call():
            result = ""
            async for event in self.chat(messages, stream=False, use_tools=False, call_config=call_config):
                if event.get("type") == "content":
                    result += event.get("text", "")
            return result

        try:
            # 在新的事件循环中执行异步调用
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                content = loop.run_until_complete(_do_call())
                return {"content": content}
            finally:
                loop.close()
        except Exception as e:
            logger.error("[LLMClient] chat_completion 异常: %s", e)
            return {"content": ""}

    async def chat_completion_async(self, messages: List[Dict[str, str]], **kwargs) -> Dict[str, Any]:
        """异步非流式调用，适合在主事件循环里做摘要/压缩。"""
        call_config = kwargs.get("call_config")
        if call_config is None:
            call_config = _active_call_config_var.get()
        if call_config is None:
            try:
                from .llm_call_config import resolve_call_model_config
                call_config = resolve_call_model_config()
            except Exception:
                pass
        try:
            result = ""
            async for event in self.chat(messages, stream=False, use_tools=False, call_config=call_config):
                if event.get("type") == "content":
                    result += event.get("text", "")
            return {"content": result}
        except Exception as e:
            logger.error("[LLMClient] chat_completion_async 异常: %s", e)
            return {"content": ""}

    def _parse_tool_call_from_text(
        self,
        text: str,
        *,
        strict_sigil_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """从文本中解析工具调用（支持文本形式的工具调用）。

        Args:
            strict_sigil_only: 仅识别显式 sigil 格式（pattern6: ``<tool_call>NAME<tool_sep>...``）。
                启用场景：本轮已经向模型暴露了 native function-calling tools 但模型走了
                正文（罕见，比如混元3 / ChatGLM3 把训练数据里的 sigil 漏出来）。这种情况下
                ```execute_command 这类 markdown 代码块应被视为人类演示而非真工具调用——
                避免模型在「总结」段落写 `\`\`\`execute_command\\ngit log\\n\`\`\`` 演示用法
                被柳如烟当真执行（事故见 2026-05-16 session edd8839a 报告）。
                根因纪律：解析器宁可不解析，不要乱解析。
        """
        tools = []

        tool_name_map = {
            'bash': 'execute_command',
            'read': 'read_file',
            'read_file': 'read_file',
            'write': 'write_file',
            'write_file': 'write_file',
            'edit': 'edit',
            # edit_file 是 2026-05-11 apply_patch→edit 改名前的老名，registry 里
            # 没有这个工具；映射到 edit 而不是它自己，否则文本形态的 edit 调用会在
            # 「未知工具」过滤那一步被静默丢掉。
            'edit_file': 'edit',
            'multi_edit': 'multi_edit',
            'list': 'list_directory',
            'list_directory': 'list_directory',
            'ls': 'list_directory',
            'dir': 'list_directory',
            # 2026-05-20 老名 search_files/grep_content 彻底废弃 — 不作映射，
            # 让模型什么路径下调老名都 tool_not_found，作为诊断信号。
            'search': 'grep',
            'grep': 'grep',
            'execute': 'execute_command',
            'execute_command': 'execute_command',
            'web_search': 'web_search',
            'fetch': 'fetch_url',
            'fetch_url': 'fetch_url',
            'mkdir': 'create_directory',
            'create_directory': 'create_directory',
            'delete': 'delete',
            'copy': 'copy',
            'move': 'move',
        }

        # strict_sigil_only：本轮已暴露 native FC tools，跳过宽松的 markdown 风格匹配，
        # 仅信任显式 sigil（pattern6）。避免模型在总结里写 ```execute_command 演示用法
        # 被当真执行。
        if strict_sigil_only:
            matches1 = []
            matches2 = []
            matches3 = []
            matches4 = []
            matches5 = []
        else:
            # 格式1: ```tool_name\nparams\n```
            pattern1 = r'```(\w+)\s*\n(.*?)```'
            matches1 = re.findall(pattern1, text, re.DOTALL | re.IGNORECASE)

            # 格式2: [Tool_name\nparams\n``` 或 [Tool_name\nparams\n]
            pattern2 = r'\[(\w+)\s*\n(.*?)(?:```|\[|$)'
            matches2 = re.findall(pattern2, text, re.DOTALL | re.IGNORECASE)

            # 格式3: [Tool_name\nparams (没有结束标记)
            pattern3 = r'\[(\w+)\s*\n([^\[\]]+)\$'
            matches3 = re.findall(pattern3, text, re.DOTALL | re.IGNORECASE)

            # 格式4: [Tool_name\nparams (更宽松)
            pattern4 = r'\[(\w+)\s*\n(.+)'
            matches4 = re.findall(pattern4, text, re.DOTALL | re.IGNORECASE)

            # 格式5: tool_name\n{json} （无代码块包裹，如 GLM 输出的格式）
            pattern5 = r'(?<!\w)(\w+)\s*\n(\s*\{)'
            matches5_raw = re.finditer(pattern5, text, re.DOTALL | re.IGNORECASE)
            matches5 = []
            for m in matches5_raw:
                tool_name = m.group(1)
                start = m.start(2)
                # 尝试从 { 开始找到配对的 }
                brace_count = 0
                end = start
                for i in range(start, len(text)):
                    if text[i] == '{': brace_count += 1
                    elif text[i] == '}': brace_count -= 1
                    if brace_count == 0:
                        end = i + 1
                        break
                if brace_count == 0:
                    json_text = text[start:end]
                    matches5.append((tool_name, json_text))

        # 格式6: <tool_call>NAME<tool_sep><arg_key>K</arg_key><arg_value>V</arg_value>...</tool_call>
        # 混元3 / ChatGLM3 sigil 格式：在 OpenAI 兼容接口下，模型偶尔会"漏出"训练数据
        # 自带的工具调用模板格式而不是用 native function calling 协议（线上 2026-05-06
        # 混元3 plan/explore subagent 的工具调用就漏到正文里）。
        # 模型内部用此格式 → API 把它当 content 返 → native 解析路径错过 → 这里救场。
        pattern6 = r'<tool_call>\s*(\w+)\s*<tool_sep>(.*?)</tool_call>'
        matches6_raw = re.findall(pattern6, text, re.DOTALL | re.IGNORECASE)
        matches6 = []
        arg_pair_pattern = re.compile(
            r'<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>',
            re.DOTALL,
        )
        for tool_name, body in matches6_raw:
            args_dict: Dict[str, Any] = {}
            for k, v in arg_pair_pattern.findall(body):
                args_dict[k.strip()] = v.strip()
            try:
                params_text = json.dumps(args_dict, ensure_ascii=False)
            except Exception:
                continue
            matches6.append((tool_name, params_text))

        all_matches = (
            list(matches1) + list(matches2) + list(matches3)
            + list(matches4) + list(matches5) + list(matches6)
        )

        # 去重（同一段文本可能被多个 pattern 匹配）
        seen = set()
        unique_matches = []
        for tool_name, params_text in all_matches:
            key = (tool_name, params_text[:50])
            if key not in seen:
                seen.add(key)
                unique_matches.append((tool_name, params_text))
        all_matches = unique_matches

        for tool_name, params_text in all_matches:
            tool_name_lower = tool_name.lower()
            actual_tool = tool_name_map.get(tool_name_lower, tool_name_lower)

            # 检查是否是已知工具
            if actual_tool not in self.tool_registry.tools:
                continue

            # 解析参数：先尝试 JSON 格式，再尝试 key: value 行格式
            args = {}
            params_stripped = params_text.strip()
            # 尝试 JSON 解析
            try:
                parsed = json.loads(params_stripped)
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, ValueError):
                # JSON 解析失败 → 尝试 "key: value\nkey: value" 行格式。
                # 红线（2026-05-15）：key 必须是简单标识符（字母数字下划线），
                # 否则会把命令字符串里的 `D:\path` / `http://x` / `time: 12:30:45`
                # 当成 key:value 拦腰切掉，污染 args 让下面 1502 行的兜底失效。
                # 此处宁可全部放弃也不要乱解析——后续兜底会把整段当 command 直传。
                _ident_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
                lines = params_stripped.split('\n')
                kv_dict: Dict[str, Any] = {}
                kv_valid = True
                for line in lines:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    if ':' not in line_s:
                        # 行内没冒号 = 不是 key:value 行 → 整体放弃当键值对解析
                        kv_valid = False
                        break
                    key, value = line_s.split(':', 1)
                    k = key.strip().strip('"').strip("'")
                    if not _ident_re.match(k):
                        # key 不像标识符 = 多半是命令里的冒号（D:\...、URL、时间）
                        kv_valid = False
                        break
                    v = value.strip().strip('"').strip("'")
                    kv_dict[k] = v
                if kv_valid and kv_dict:
                    args = kv_dict
                # else: 留 args 为空，让下面的 execute_command 兜底把整段当 command

            # 特殊处理：Bash 命令
            if actual_tool == 'execute_command' and 'command' not in args:
                args['command'] = params_text.strip()

            # 特殊处理：read_file
            # 兜底键名必须是 canonical path（不是 file_path），否则会把模型传对的
            # 参数改错、在 dispatch 的 strict 门被拒。
            if actual_tool == 'read_file' and 'path' not in args:
                args['path'] = params_text.strip()

            # 特殊处理：list_directory
            if actual_tool == 'list_directory' and 'path' not in args:
                args['path'] = params_text.strip() or '.'

            tools.append({
                'name': actual_tool,
                'arguments': args
            })

        return tools

    async def _chat_one_round(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
        round_num: int = 0,
        api_client: Optional[Any] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """v2 C2-d：OpenAI 兼容路径单轮 stream + valid 工具 dispatch（async generator）。

        v2 P1-E-1：内部从直调 SDK + 手装 parser 切到 ApiClient.send_stream；
        ApiClient 实例由 ProviderRegistry 按 (provider, model, key, base_url)
        LRU 缓存。可选 ``api_client`` 注入参数用于测试 / future LeadAgent 接管循环。

        从 _chat_with_tools 主流式分支抽出来作为公开 API，让 v2 chain
        （LeadAgent）将来能按 round 调本方法做"一轮"——chain 接管循环外的
        budget/guard/spawn/convergence/text-fallback/empty-fallback/异常 fallback
        决策；本方法只管"跑一轮 native stream + 工具 dispatch"。

        本方法**不**修改 messages（纯函数语义）；通过末尾 ``_round_done`` 事件
        返出 assistant_msg / tool_result_msgs / invalid_tool_calls / executed_*
        让调用方决定何时 append。

        invalid_tool_calls 路径**不进 dispatch_batch**，直接返出
        invalid_tool_calls + assistant_msg + all_tool_call_ids 让外层调
        ``_pad_tool_messages_for_invalid_batch`` + 决定 retry/失败终止。这跟
        anthropic _chat_one_round 的"valid 工具才进 dispatch"语义一致。

        异常**不**捕获——让外层 _chat_with_tools 接管（text-fallback / 空响应
        非流式 fallback / 异常 fallback / classify error）。例外：registry
        provider lookup 失败时直接抛 LLMError（fail-fast，不让 fallback 接住
        伪装成 SDK 错误）。

        yield 事件：
        - content / thinking / tool_call / tool_result（透传给调用方）
        - 末尾恰好一次 ``type='_round_done'`` 内部事件（前缀下划线表示内部约定）

        ``_round_done`` payload：
        - finish_reason: str | None
        - has_tool_calls: bool（valid native tool_calls 检测且 dispatch 完成）
        - has_invalid_tool_calls: bool（finish_reason=tool_calls 但 args 解析全失败）
        - empty_response: bool（既没 tool_calls 也没 content；外层走非流式 fallback）
        - text_only: bool（仅文本无工具；外层可走 text-fallback 解析）
        - current_content: str（外层 text-fallback 解析用）
        - assistant_msg: dict | None（外层 append；valid + invalid 路径都返出）
        - all_tool_call_ids: list[str]（invalid 时外层调 _pad_tool_messages_for_invalid_batch 用）
        - tool_result_msgs: list[dict]（valid 路径 dispatch 后；外层 extend 到 messages）
        - executed_tool_calls / executed_tool_results
        - spawn_dispatched: bool
        - invalid_tool_calls: list[dict]（结构化错误项；外层 retry 计数 + notice）

        参数：
        - messages: 当前对话上下文（不含 system；in-place 不修改）
        - tools: get_tool_definitions(format='openai', allowed_tools=...) 输出
        - stream: 是否 yield 流事件给调用方
        - round_num: 仅日志用
        - api_client: 可注入的 ApiClient 实例（测试 / 自定义路由用）；None 时
          走 ProviderRegistry 默认单例。
        """
        # v2 P1-E-1 / A 迁移：用 _get_api_client() helper 共用三个调用点
        # （_chat_one_round / _chat_stream / _chat_complete）；fail-fast on
        # ProviderNotFoundError，包成 LLMError 让外层 _chat_with_tools 异常
        # fallback 路径不会误认为 SDK 错误。
        if api_client is None:
            api_client = self._get_api_client()

        from liuruyan.llm import CoreRequest, RequestContext
        from .runtime_budget import get_effective_max_output_tokens, get_effective_stream_idle_timeout

        # CoreRequest 等价性映射（vs 原 SDK 入参）：
        # - messages: 直传（OpenAI 路径 system 已嵌在 messages 内）
        # - tools: 直传（OpenAI std 格式，ApiClient 内部 to_openai_messages 净化）
        # - max_tokens: 从 runtime_budget.json 读（P3-max-tokens-cfg），默认 32768
        # - extras["stream_options"]: 让末 chunk 带 usage（include_usage=True，
        #   OpenAI 兼容协议要求显式开启）—— ApiClient 已落代码不会自动加这个
        # - extras["tool_choice"]="auto": 跟原 SDK 入参对齐，避免 GLM/Astron 等
        #   vendor 默认行为差异
        core_req = CoreRequest(
            messages=messages,
            system=None,
            tools=tools,
            model=self._resolve_active_model(),
            max_tokens=get_effective_max_output_tokens("openai"),
            extras={
                "stream_options": {"include_usage": True},
                "tool_choice": "auto",
            },
        )
        core_ctx = RequestContext(
            agent_id=_current_agent_id_var.get(),
            session_id=_current_session_id_var.get(),
            cancel_event=None,
        )

        current_content = ""
        current_reasoning = ""
        current_tool_calls: Dict[int, Dict] = {}  # index -> {id, name, arguments}

        finish_reason = None
        final_usage = None  # 末 chunk usage（include_usage 后才有）

        # chunk-level idle timeout helper：详见 backend/llm_stream_utils.py
        # 必须用 wait_for 包 __anext__，不能在 async for 循环体内打时间差判断
        # —— 后者在上游静默挂死时无法触发，是 2026-05-13 聊天框光标卡死事故根因。
        # P3-stream-timeout-cfg：读 runtime_budget.json
        from .llm_stream_utils import iter_stream_with_idle_timeout
        idle_timeout = get_effective_stream_idle_timeout("openai")
        async for ev in iter_stream_with_idle_timeout(
            api_client.send_stream(core_req, core_ctx),
            idle_timeout,
            label="openai_tools",
        ):
            ev_type = ev.type
            if ev_type == "content_delta":
                current_content += ev.text
                if stream:
                    yield {"type": "content", "text": ev.text}
            elif ev_type == "reasoning_delta":
                current_reasoning += ev.text
                if stream:
                    yield {"type": "thinking", "text": ev.text}
            elif ev_type == "tool_call_delta":
                # 兜底路径：plugin 没装时（理论上不应该走这里，registry 默认装
                # tool_call_accumulator）。按 vendor index 累积 —— 跟 Slice 1
                # 行为完全等价，保留兼容性
                idx = ev.index
                if idx not in current_tool_calls:
                    current_tool_calls[idx] = {
                        "id": ev.id or "",
                        "name": "",
                        "arguments": ""
                    }
                if ev.id:
                    current_tool_calls[idx]["id"] = ev.id
                if ev.name:
                    current_tool_calls[idx]["name"] += ev.name
                if ev.arguments:
                    current_tool_calls[idx]["arguments"] += ev.arguments
            elif ev_type == "tool_call_started":
                # P1 体感优化（2026-05-16）：accumulator plugin 在 name 一就位
                # 时透传本事件；立即 yield 给前端，让 UI 在 args 还在累积的
                # 1-3 秒里就渲染 calling 卡——而不是等 tool_call_complete 流末
                # 才一次性 flash 出 N 张卡。前端按 tool_use_id 配对、收到完整
                # tool_call 时覆盖更新即可（chatStreamMessage.ts）。
                if stream:
                    yield {
                        "type": "tool_call_started",
                        "name": ev.name,
                        "tool_use_id": ev.id,
                        "id": ev.id,
                    }
            elif ev_type == "tool_call_complete":
                # Slice 3+ 主路径：plugin 累积完成，按 toolCallId 索引（修按
                # array index 累积可能在同名 tool_call 跨 chunk 时不稳定的 bug）
                current_tool_calls[ev.id] = {
                    "id": ev.id,
                    "name": ev.name,
                    "arguments": ev.arguments_raw,
                    "thought_signature": getattr(ev, "thought_signature", None),
                }
            elif ev_type == "usage":
                final_usage = ev.payload
            elif ev_type == "round_end":
                finish_reason = ev.finish_reason
            elif ev_type == "error":
                # 单 chunk 解析失败：parser 转 ParseError 事件继续；这里仅日志
                logger.warning(f"[stream_parser] {ev.message}")

        # 流结束 → token usage 算账（include_usage 末 chunk）
        await self._record_openai_usage(final_usage)

        # mw 10 TokenUsage chain 镜像：本轮 usage 增量挂 _round_done payload，
        # LeadAgent chain_hook 写 state["last_round_token_usage"] 让 mw 10 emit
        # token_usage_recorded 事件做 trace journal 标记（v1 仍真算账）
        round_token_usage = self._extract_openai_round_token_usage(final_usage)

        # ── 分类决策 ──────────────────────────────────────────────────
        if (finish_reason in ("tool_calls", "function_call") or current_tool_calls) and current_tool_calls:
            # native 工具调用：组装 assistant_msg + 校验 args
            assistant_msg = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"]
                        },
                        **({"thought_signature": tc["thought_signature"]} if tc.get("thought_signature") else {})
                    }
                    for tc in sorted(current_tool_calls.values(), key=lambda x: x["id"])
                ]
            }
            if current_content:
                assistant_msg["content"] = current_content
            # DeepSeek thinking 模式协议要求：上轮 assistant 必须带 reasoning_content
            # 回传，否则 400 "The reasoning_content in the thinking mode must be
            # passed back to the API"。current_reasoning 由上面 reasoning_delta
            # 事件累积；工具循环 append 这条 assistant_msg 进 messages，下一轮发出
            # 去时 to_openai_messages 会透传该字段（参 openai_message_converter
            # TestReasoningContentPassthrough 契约）。
            if current_reasoning:
                assistant_msg["reasoning_content"] = current_reasoning

            parsed_tool_calls = []
            invalid_tool_calls = []
            for tc in current_tool_calls.values():
                function_name = tc["name"]
                raw_args = tc["arguments"]
                try:
                    function_args, repair_layers = self._normalize_tool_arguments(raw_args, function_name)
                    if repair_layers:
                        tc["_json_repair_layers"] = repair_layers
                except LLMError as exc:
                    invalid_tool_calls.append({
                        "id": tc["id"],
                        "name": function_name,
                        "error": str(exc),
                        "raw_arguments": (raw_args or "")[:200].replace("\n", "\\n"),
                    })
                    continue
                tc_item = {
                    "id": tc["id"],
                    "name": function_name,
                    "arguments": function_args,
                    "_json_repair_layers": repair_layers,
                }
                if tc.get("thought_signature"):
                    tc_item["thought_signature"] = tc["thought_signature"]
                parsed_tool_calls.append(tc_item)

            all_tool_call_ids = [
                tc["id"]
                for tc in sorted(current_tool_calls.values(), key=lambda x: x["id"])
                if tc.get("id")
            ]

            if invalid_tool_calls:
                # invalid 路径不进 dispatch；让外层调 _pad_tool_messages_for_invalid_batch
                # + 决定 retry / 失败终止
                yield {
                    "type": "_round_done",
                    "finish_reason": finish_reason,
                    "has_tool_calls": False,
                    "has_invalid_tool_calls": True,
                    "empty_response": False,
                    "text_only": False,
                    "current_content": current_content,
                    "assistant_msg": assistant_msg,
                    "all_tool_call_ids": all_tool_call_ids,
                    "tool_result_msgs": [],
                    "executed_tool_calls": [],
                    "executed_tool_results": [],
                    "spawn_dispatched": False,
                    "invalid_tool_calls": invalid_tool_calls,
                    "token_usage": round_token_usage,
                }
                return

            if stream:
                for tc in parsed_tool_calls:
                    yield {
                        "type": "tool_call",
                        "name": tc["name"],
                        "input": tc["arguments"],
                        "tool_use_id": tc.get("id"),
                    }

            # valid → dispatch_batch 并发预取（read 类并发 gather，写类/独占串行）
            results_by_id = await self.tool_registry.dispatch_batch(
                parsed_tool_calls,
                agent_id=_current_agent_id_var.get(),
                session_id=_current_session_id_var.get(),
                llm_client=self,
            )
            executed_tool_calls = []
            executed_tool_results = []
            tool_result_msgs: List[Dict[str, Any]] = []
            # 多模态延迟图像分块投递机制
            # 独立 user message 追加到 tool_result_msgs（紧跟 tool_results user
            # message 之后），让模型在合法位置看到真图（不被反代 tool_result 路径
            # 吃图）。事故根因 + 切派理由见 _split_text_and_image_blocks docstring。
            deferred_image_blocks: List[Dict[str, Any]] = []
            deferred_image_tool_names: List[str] = []
            deferred_image_tool_use_ids: List[str] = []
            for tc in parsed_tool_calls:
                function_name = tc["name"]
                function_args = tc["arguments"]
                executed_tool_calls.append({
                    "name": function_name,
                    "arguments": function_args
                })

                # tool 执行 + 结果处理整段包 try/except：任何异常都要补对应 tool message，
                # 否则 messages 配对漏，下一轮 / 外层 fallback 会被 gpt-5 严格校验拒（400）
                try:
                    res_entry = results_by_id.get(tc["id"]) or {}
                    if "exception" in res_entry:
                        raise res_entry["exception"]
                    result = res_entry.get("result", "")

                    # vision: read_file 读图片返回 _content_blocks 标记位（list[block]）。
                    # 多模态延迟图像分块投递机制
                    # 只放 text + anchor。display_result 用脱敏文本（不含 base64）。
                    content_blocks = _extract_content_blocks_from_tool_result(result)
                    if content_blocks is not None:
                        display_result = _display_text_for_blocks(content_blocks, result)
                        text_str, this_round_images = _split_text_and_image_blocks(content_blocks)
                        # 非 vision 模型：image/document block 立刻降级为文字占位，
                        # 避免下游 OpenAI converter emit image_url 让非 vision provider 400
                        text_str, this_round_images = _maybe_downgrade_images_for_non_vision_model(
                            text_str, this_round_images
                        )
                        if this_round_images:
                            tool_msg_content = (text_str or "(image attached)") + _TOOL_RESULT_IMAGE_ANCHOR
                            deferred_image_blocks.extend(this_round_images)
                            deferred_image_tool_names.append(function_name)
                            if tc.get("id"):
                                deferred_image_tool_use_ids.append(tc["id"])
                        else:
                            # image 已被降级（非 vision 模型）或标记位存在但拆完没图（罕见）
                            tool_msg_content = text_str or result
                    else:
                        display_result = format_tool_result_for_display(function_name, result)
                        tool_msg_content = result

                    if stream:
                        event = {
                            "type": "tool_result",
                            "name": function_name,
                            "result": display_result,
                            "tool_use_id": tc.get("id"),
                        }
                        yield event

                    tool_result_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_msg_content,
                    })
                    executed_tool_results.append(str(result))
                except Exception as tool_exc:
                    err_text = f"Tool execution failed: {type(tool_exc).__name__}: {tool_exc}"
                    tool_result_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": err_text,
                    })
                    executed_tool_results.append(err_text)
                    if stream:
                        yield {
                            "type": "tool_result",
                            "name": function_name,
                            "result": format_tool_result_for_display(function_name, err_text),
                            "tool_use_id": tc.get("id"),
                        }

            # 多模态延迟图像分块投递机制
            # 紧跟在 tool_results 之后追加到 tool_result_msgs。外层 _chat_with_tools
            # 拿到 _round_done.tool_result_msgs 后会逐条 messages.append → 这条
            # user message 自然落到 messages[] 里 image-in-user-message 合法位置。
            if deferred_image_blocks:
                deferred_user_msg = _build_tool_image_user_message(
                    deferred_image_blocks,
                    deferred_image_tool_names,
                )
                tool_result_msgs.append(deferred_user_msg)
                # 同时发 SSE event 让 runtime_segments / transcript 把这条 message
                # 落盘（持久化路径见 session.append_assistant_structured 处理
                # tool_image_attachment segment）。
                if stream:
                    yield {
                        "type": "tool_image_attachment",
                        "tool_use_ids": list(deferred_image_tool_use_ids),
                        "tool_names": list(deferred_image_tool_names),
                        "content_blocks": deferred_user_msg["content"],
                    }

            yield {
                "type": "_round_done",
                "finish_reason": finish_reason,
                "has_tool_calls": True,
                "has_invalid_tool_calls": False,
                "empty_response": False,
                "text_only": False,
                "current_content": current_content,
                "assistant_msg": assistant_msg,
                "all_tool_call_ids": [],
                "tool_result_msgs": tool_result_msgs,
                "executed_tool_calls": executed_tool_calls,
                "executed_tool_results": executed_tool_results,
                "spawn_dispatched": False,
                "invalid_tool_calls": [],
                "token_usage": round_token_usage,
            }
            return

        # 没 native 工具调用 → 让外层决定 text-fallback / 空响应 fallback
        is_empty = not current_content
        yield {
            "type": "_round_done",
            "finish_reason": finish_reason,
            "has_tool_calls": False,
            "has_invalid_tool_calls": False,
            "empty_response": is_empty,
            "text_only": not is_empty,
            "current_content": current_content,
            "assistant_msg": None,
            "all_tool_call_ids": [],
            "tool_result_msgs": [],
            "executed_tool_calls": [],
            "executed_tool_results": [],
            "spawn_dispatched": False,
            "invalid_tool_calls": [],
            "token_usage": round_token_usage,
        }

    async def _dispatch_completion_fallback(self, messages, message, *, guard, budget, chain_hook, stream, ctl):
        """send_completion 兜底分支的共享 dispatch/retry/stop 逻辑（空响应 & 异常两条兜底共用）。

        message: send_completion 返回的 {content, tool_calls, finish_reason, usage}。
        ctl: 可变控制字典 {"action": None, "retry_count": int, "retry_limit": int}；
        本方法据情设 ctl["action"]="return"（终止整个工具循环）或 "continue"（续轮），并原地更新 retry_count。
        """
        content = message["content"] or ""
        if message.get("tool_calls"):
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": message["tool_calls"],
            })
            parsed_tool_calls = []
            invalid_tool_calls = []
            for tc in message["tool_calls"]:
                fn_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"]
                try:
                    fn_args, repair_layers = self._normalize_tool_arguments(raw_args, fn_name)
                except LLMError as exc:
                    invalid_tool_calls.append({
                        "id": tc["id"],
                        "name": fn_name,
                        "error": str(exc),
                        "raw_arguments": (raw_args or "")[:200].replace("\n", "\\n"),
                    })
                    continue
                parsed_tool_calls.append({
                    "id": tc["id"],
                    "name": fn_name,
                    "arguments": fn_args,
                    "_json_repair_layers": repair_layers,
                })
            if invalid_tool_calls:
                self._pad_tool_messages_for_invalid_batch(
                    messages,
                    all_tool_call_ids=[tc["id"] for tc in message["tool_calls"] if tc.get("id")],
                    invalid_id_to_error={ic["id"]: ic["error"] for ic in invalid_tool_calls if ic.get("id")},
                )
                notice = self._build_invalid_tool_call_notice(invalid_tool_calls)
                if stream:
                    yield {"type": "system_notice", "kind": "invalid_tool_arguments", "content": notice}
                ctl["retry_count"] += 1
                if ctl["retry_count"] > ctl["retry_limit"]:
                    stop = self._build_invalid_tool_arguments_stop(
                        invalid_tool_calls,
                        retry_count=ctl["retry_count"],
                        retry_limit=ctl["retry_limit"],
                    )
                    final_response = await self._finalize_after_tool_loop(messages, stop)
                    yield build_tool_loop_notice_event(stop)
                    yield {"type": "content", "text": final_response}
                    ctl["action"] = "return"
                    return
                messages.append({"role": "user", "content": self._build_tool_retry_message(invalid_tool_calls)})
                ctl["action"] = "continue"
                return
            if stream:
                for tc in parsed_tool_calls:
                    yield {"type": "tool_call", "name": tc["name"], "input": tc["arguments"], "tool_use_id": tc.get("id")}
            results_by_id = await self.tool_registry.dispatch_batch(
                parsed_tool_calls,
                agent_id=_current_agent_id_var.get(),
                session_id=_current_session_id_var.get(),
                llm_client=self,
            )
            executed_tool_calls = []
            executed_tool_results = []
            deferred_image_blocks = []
            deferred_image_tool_names = []
            deferred_image_tool_use_ids = []
            for tc in parsed_tool_calls:
                fn_name = tc["name"]
                fn_args = tc["arguments"]
                executed_tool_calls.append({"name": fn_name, "arguments": fn_args})
                try:
                    res_entry = results_by_id.get(tc["id"]) or {}
                    if "exception" in res_entry:
                        raise res_entry["exception"]
                    result = res_entry.get("result", "")
                    content_blocks = _extract_content_blocks_from_tool_result(result)
                    if content_blocks is not None:
                        display_result = _display_text_for_blocks(content_blocks, result)
                        text_str, this_round_images = _split_text_and_image_blocks(content_blocks)
                        text_str, this_round_images = _maybe_downgrade_images_for_non_vision_model(text_str, this_round_images)
                        if this_round_images:
                            tool_msg_content = (text_str or "(image attached)") + _TOOL_RESULT_IMAGE_ANCHOR
                            deferred_image_blocks.extend(this_round_images)
                            deferred_image_tool_names.append(fn_name)
                            if tc.get("id"):
                                deferred_image_tool_use_ids.append(tc["id"])
                        else:
                            tool_msg_content = text_str or str(result)
                    else:
                        display_result = format_tool_result_for_display(fn_name, str(result))
                        tool_msg_content = str(result)
                    if stream:
                        yield {"type": "tool_result", "name": fn_name, "result": display_result, "tool_use_id": tc.get("id")}
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_msg_content})
                    executed_tool_results.append(str(result))
                except Exception as tool_exc:
                    err_text = f"Tool execution failed: {type(tool_exc).__name__}: {tool_exc}"
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": err_text})
                    executed_tool_results.append(err_text)
                    if stream:
                        yield {"type": "tool_result", "name": fn_name, "result": format_tool_result_for_display(fn_name, err_text), "tool_use_id": tc.get("id")}
            if deferred_image_blocks:
                deferred_user_msg = _build_tool_image_user_message(deferred_image_blocks, deferred_image_tool_names)
                messages.append(deferred_user_msg)
                if stream:
                    yield {"type": "tool_image_attachment", "tool_use_ids": list(deferred_image_tool_use_ids), "tool_names": list(deferred_image_tool_names), "content_blocks": deferred_user_msg["content"]}
            ctl["retry_count"] = 0
            if chain_hook is not None:
                should_stop_by_hook = False
                try:
                    should_stop_by_hook = await chain_hook({
                        "type": "_round_done",
                        "finish_reason": "tool_calls",
                        "has_tool_calls": True,
                        "has_invalid_tool_calls": False,
                        "executed_tool_calls": executed_tool_calls,
                        "executed_tool_results": executed_tool_results,
                    })
                except Exception:
                    import traceback
                    logger.error("[llm_client] chain_hook failed: %s", traceback.format_exc())
                if should_stop_by_hook:
                    stop = guard.max_rounds_stop()
                    final_response = await self._finalize_after_tool_loop(messages, stop)
                    yield build_tool_loop_notice_event(stop)
                    yield {"type": "content", "text": final_response}
                    ctl["action"] = "return"
                    return
            stop = guard.observe_round(executed_tool_calls, executed_tool_results)
            if stop:
                final_response = await self._finalize_after_tool_loop(messages, stop)
                yield build_tool_loop_notice_event(stop)
                yield {"type": "content", "text": final_response}
                ctl["action"] = "return"
                return
            stop = budget.observe_tool_batch(executed_tool_calls, executed_tool_results)
            if stop:
                final_response = await self._finalize_after_tool_loop(messages, stop)
                yield build_tool_loop_notice_event(stop)
                yield {"type": "content", "text": final_response}
                ctl["action"] = "return"
                return
            convergence = build_budget_convergence_plan(budget)
            if convergence:
                if stream:
                    yield {"type": "system_notice", "kind": convergence.notice_kind, "content": convergence.notice_message, "payload": convergence.notice_payload}
                messages.append({"role": "user", "content": convergence.prompt_message})
            ctl["action"] = "continue"
            return
        else:
            if stream:
                for i in range(0, len(content), 10):
                    yield {"type": "content", "text": content[i:i+10]}
            elif content:
                yield {"type": "content", "text": content}
            ctl["action"] = "return"
            return

    async def _chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        stream: bool = True,
        *,
        chain_hook=None,
        allowed_tools_override: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict]:
        """带工具调用的聊天（流式，返回结构化事件）

        事件类型：
        - {"type": "thinking", "text": "..."}        - 模型思考内容（reasoning）
        - {"type": "content", "text": "..."}          - 模型正文回复
        - {"type": "tool_call", "name": "xxx", "args": {...}}  - 即将调用工具
        - {"type": "tool_result", "name": "xxx", "result": "..."}  - 工具执行结果

        v2 C2-d-3：可选 ``chain_hook`` —— 每轮 _chat_one_round 完成后调一次，
        签名 ``async (round_done: dict) -> bool``。返 True 时立即终止本次工具
        循环（chain 决定 should_end，比 v1 自己的 stop 决策优先级高）。这让
        v2 chain 的 mw 08 LoopDetection / mw 14 TitleGen / mw 15 MemoryQueue /
        mw 16 SubagentLimit / mw 17 Clarification 等 after_model 钩子在 OpenAI
        路径上真生效——通过反向注入而非 chain 接管循环；老管家仍在跑循环但
        已经听 chain 指挥。

        chain_hook 覆盖 native 主路径，以及 text-fallback / send_completion
        fallback 中实际执行了工具的一轮；这样 subagent maxTurns 不会被兼容路径绕过。
        """
        # 入口自愈：历史 messages 里 assistant.tool_calls 缺对应 tool message 的（前一次 run 被中断
        # 或 execute 抛异常时残缺）补占位 tool message，避免 gpt-5 严格校验直接 400。
        recovered = self._sanitize_tool_call_pairing(messages)
        if recovered:
            logger.warning("[llm_client] sanitized %s unpaired tool_call(s) from history", recovered)

        # P1#4：按当前 agent_id 查白名单，未配置时返回 None → 不过滤（向后兼容全工具）
        # 失败静默降级到全工具：白名单读不到不能阻断对话
        # ── allowed_tools_override 优先 ──
        # caller（subagent engine）传了精确白名单时直接用，不再查 agent_manager。
        # 设计：subagent 继承父 agent_id 但工具集应受模板约束（非父 agent 的全集），
        # 这里给 caller 一个旁路。None=老行为；[]=合法的"零工具"语义。
        if allowed_tools_override is not None:
            _allowed = list(allowed_tools_override)
        else:
            _allowed = None
            try:
                from backend.agent_config import get_agent_manager
                _allowed = get_agent_manager().get_allowed_tools(_current_agent_id_var.get())
            except Exception as e:
                logger.warning(f"agent tool whitelist lookup failed, fallthrough to all tools: {e}")

        # OpenAI 兼容模式使用 OpenAI 格式的工具定义
        tools = self.tool_registry.get_tool_definitions(format="openai", allowed_tools=_allowed)
        # v2 B 迁移：不再需要 SDK client（_chat_one_round / _chat_stream /
        # _chat_complete / fallback 全走 ApiClient）。删 client = self._ensure_openai_client()
        guard = ToolLoopGuard.from_env("openai")
        budget = AggregateRuntimeBudget.from_env("openai")
        invalid_tool_retry_limit = max(
            0,
            int(
                os.getenv("OPENAI_MAX_INVALID_TOOL_ARGUMENT_RETRIES")
                or os.getenv("LLM_MAX_INVALID_TOOL_ARGUMENT_RETRIES")
                or "3"
            ),
        )
        invalid_tool_retry_count = 0

        # 最多进行 5 轮工具调用
        for round_num in range(guard.max_rounds):
            stop = budget.observe_before_round()
            if stop:
                final_response = await self._finalize_after_tool_loop(messages, stop)
                yield build_tool_loop_notice_event(stop)
                yield {"type": "content", "text": final_response}
                return
            try:
                # v2 C2-d-2：调 _chat_one_round 跑单轮（_chat_one_round 内部含
                # stream 累积 / token usage / native 工具 dispatch / invalid 检测）。
                # _chat_one_round 不修改 messages — 通过末尾 _round_done 事件
                # 回传 assistant_msg / tool_result_msgs / invalid_tool_calls 等。
                round_done: Optional[Dict[str, Any]] = None
                async for event in self._chat_one_round(
                    messages,
                    tools=tools,
                    stream=stream,
                    round_num=round_num,
                ):
                    if event.get("type") == "_round_done":
                        round_done = event
                        continue
                    yield event

                if round_done is None:
                    # _chat_one_round 应该总是 yield _round_done — 防御性兜底
                    return

                # === C2-d-3 反向注入：chain hook（after_model 钩子真生效）===
                # mw 08 LoopDetection / mw 14 TitleGen / mw 15 MemoryQueue /
                # mw 16 SubagentLimit / mw 17 Clarification 等 after_model 钩子
                # 都通过 chain_hook 在每轮跑；返 True 时 chain 决定 should_end，
                # v1 立即终止循环（优先级高于 v1 自己的 budget / guard 决策）。
                # 钩子位置：每轮 _chat_one_round 完成后（已 yield 工具事件 + dispatch 完成），
                # 跟 anthropic_client 的钩子点保持对称。
                if chain_hook is not None:
                    should_stop_by_hook = False
                    try:
                        should_stop_by_hook = await chain_hook(round_done)
                    except Exception:
                        # chain hook 异常不影响 v1 主路径；记日志（hook 内部
                        # 也应该 swallow，这里再保险一层）
                        import traceback
                        logger.error(
                            "[llm_client] chain_hook failed: %s",
                            traceback.format_exc(),
                        )
                    if should_stop_by_hook:
                        if round_done.get("has_tool_calls"):
                            messages.append(round_done["assistant_msg"])
                            for tool_msg in round_done["tool_result_msgs"]:
                                messages.append(tool_msg)
                            stop = guard.max_rounds_stop()
                            final_response = await self._finalize_after_tool_loop(messages, stop)
                            yield build_tool_loop_notice_event(stop)
                            yield {"type": "content", "text": final_response}
                        return
                # =============================================================

                # ── ask_user_question 阻塞等待（交互式问答等待语义）──
                # 本轮若发出了 ask_user_question：交互会话在此挂起等用户作答，
                # 答案就地改写进 tool_result（模型同轮拿到答案继续）；超时/取消/
                # 非交互场景 → append 本轮 messages 后立即结束本轮，绝不把
                # awaiting_user_choice 喂回模型（否则模型不守"立即结束"约定
                # 就会出现"提问完自己继续思考"的事故，2026-09-29 用户报告）。
                if round_done.get("has_tool_calls"):
                    from backend.tools.builtin.ask_user_question_tool import (
                        pause_for_ask_user_answers,
                    )
                    ask_updates, ask_action = await pause_for_ask_user_answers(round_done)
                    for _ask_upd in ask_updates:
                        if stream:
                            yield {
                                "type": "tool_result_update",
                                "name": "ask_user_question",
                                "tool_use_id": _ask_upd["tool_use_id"],
                                "result": _ask_upd["result"],
                            }
                    if ask_action == "append_then_stop":
                        messages.append(round_done["assistant_msg"])
                        for tool_msg in round_done["tool_result_msgs"]:
                            messages.append(tool_msg)
                        return

                if round_done["has_invalid_tool_calls"]:
                    # invalid 路径：_chat_one_round 已校验 args 解析失败但**不进 dispatch**；
                    # 外层 append assistant_msg + 调 pad helper 补齐 messages 配对 +
                    # 决定 retry / 失败终止。
                    messages.append(round_done["assistant_msg"])
                    invalid_tool_calls = round_done["invalid_tool_calls"]
                    if invalid_tool_calls:
                        # 先补齐 messages 配对，避免 gpt-5 严格校验 400（assistant.tool_calls 必须每个都有匹配 tool message）
                        self._pad_tool_messages_for_invalid_batch(
                            messages,
                            all_tool_call_ids=round_done["all_tool_call_ids"],
                            invalid_id_to_error={ic["id"]: ic["error"] for ic in invalid_tool_calls if ic.get("id")},
                        )
                        notice = self._build_invalid_tool_call_notice(invalid_tool_calls)
                        if stream:
                            yield {"type": "system_notice", "kind": "invalid_tool_arguments", "content": notice}
                        invalid_tool_retry_count += 1
                        if invalid_tool_retry_count > invalid_tool_retry_limit:
                            stop = self._build_invalid_tool_arguments_stop(
                                invalid_tool_calls,
                                retry_count=invalid_tool_retry_count,
                                retry_limit=invalid_tool_retry_limit,
                            )
                            final_response = await self._finalize_after_tool_loop(messages, stop)
                            yield build_tool_loop_notice_event(stop)
                            yield {"type": "content", "text": final_response}
                            return
                        messages.append({"role": "user", "content": self._build_tool_retry_message(invalid_tool_calls)})
                        continue

                if round_done["has_tool_calls"]:
                    # valid 工具调用：_chat_one_round 已 dispatch + yield 完事件；
                    # 外层 append assistant_msg + tool_result_msgs，跑 5 个 stop 决策。
                    messages.append(round_done["assistant_msg"])
                    for tool_msg in round_done["tool_result_msgs"]:
                        messages.append(tool_msg)
                    invalid_tool_retry_count = 0

                    executed_tool_calls = round_done["executed_tool_calls"]
                    executed_tool_results = round_done["executed_tool_results"]
                    stop = guard.observe_round(executed_tool_calls, executed_tool_results)
                    if stop:
                        final_response = await self._finalize_after_tool_loop(messages, stop)
                        yield build_tool_loop_notice_event(stop)
                        yield {"type": "content", "text": final_response}
                        return
                    stop = budget.observe_tool_batch(executed_tool_calls, executed_tool_results)
                    if stop:
                        final_response = await self._finalize_after_tool_loop(messages, stop)
                        yield build_tool_loop_notice_event(stop)
                        yield {"type": "content", "text": final_response}
                        return

                    convergence = build_budget_convergence_plan(budget)
                    if convergence:
                        if stream:
                            yield {
                                "type": "system_notice",
                                "kind": convergence.notice_kind,
                                "content": convergence.notice_message,
                                "payload": convergence.notice_payload,
                            }
                        messages.append({"role": "user", "content": convergence.prompt_message})
                    continue  # 继续下一轮对话

                # 没 native 工具调用 → text-fallback / 空响应 fallback
                # current_content 从 round_done 取出（_chat_one_round 内部累积）
                current_content = round_done["current_content"]

                # 没有工具调用，检查正文是否包含文本形式的工具调用
                if current_content:
                    # strict_sigil_only：本轮已暴露 native FC tools 时仅识别 <tool_call>...<tool_sep>
                    # 显式 sigil 格式，避免模型在「总结」段落写 ```execute_command 演示
                    # 用法被柳如烟当真执行（事故根因：2026-05-16 session edd8839a）。
                    # 弱模型（混元3/ChatGLM3）的 sigil 救场仍生效；普通 markdown 代码块不再误触发。
                    # 可用 env LIURUYAN_TEXT_FALLBACK_LOOSE=1 临时回到老行为。
                    _force_loose = os.getenv("LIURUYAN_TEXT_FALLBACK_LOOSE", "").strip() in ("1", "true", "True")
                    try:
                        parsed_tools = self._parse_tool_call_from_text(
                            current_content,
                            strict_sigil_only=bool(tools) and not _force_loose,
                        )
                    except TypeError:
                        parsed_tools = self._parse_tool_call_from_text(current_content)
                    if parsed_tools:
                        # 找到了文本形式的工具调用
                        # 先执行所有工具调用，收集结果
                        # P0 并发改造：text-fallback 路径同样并发预取（GLM 等不走 native tool_calls 协议的 model）
                        # 注意：parsed_tools 元素没有 "id" 字段（文本解析出来的）；dispatch_batch 用 tc.get("id") or "" 处理，
                        # 同名 tool_call 多个时 results_by_id 用空字符串 key 会冲突。这里临时给每个 tc 注入一个稳定 id。
                        for _idx, _tc in enumerate(parsed_tools):
                            if not _tc.get("id"):
                                _tc["id"] = f"text-fallback-{_idx}"
                        if stream:
                            for tc in parsed_tools:
                                yield {"type": "tool_call", "name": tc["name"], "input": tc["arguments"], "tool_use_id": tc.get("id")}
                        results_by_id = await self.tool_registry.dispatch_batch(
                            parsed_tools,
                            agent_id=_current_agent_id_var.get(),
                            session_id=_current_session_id_var.get(),
                            llm_client=self,
                        )
                        raw_tool_results = []
                        structured_tool_results = []
                        for tc in parsed_tools:
                            fn_name = tc["name"]
                            fn_args = tc["arguments"]
                            res_entry = results_by_id.get(tc["id"]) or {}
                            if "exception" in res_entry:
                                tool_exc = res_entry["exception"]
                                result = f"Error: {type(tool_exc).__name__}: {tool_exc}"
                            else:
                                result = res_entry.get("result", "")
                            # vision: text-fallback 路径只能传文字（模型不原生支持 tools，
                            # 自然也基本不支持 vision），把 _content_blocks 降级成纯文本提示。
                            content_blocks = _extract_content_blocks_from_tool_result(result)
                            if content_blocks is not None:
                                display_result = _display_text_for_blocks(content_blocks, result)
                                effective_result: Any = (
                                    display_result + " [note: image not visible in text-fallback mode]"
                                )
                            else:
                                display_result = format_tool_result_for_display(fn_name, result)
                                effective_result = result
                            if stream:
                                yield {"type": "tool_result", "name": fn_name, "result": display_result, "tool_use_id": tc.get("id")}
                            structured_tool_results.append({
                                "name": fn_name,
                                "arguments": fn_args,
                                "result": effective_result,
                            })
                            raw_tool_results.append(str(effective_result))
                        stop = guard.observe_round(parsed_tools, raw_tool_results)
                        if stop:
                            final_response = await self._finalize_after_tool_loop(messages, stop)
                            yield build_tool_loop_notice_event(stop)
                            yield {"type": "content", "text": final_response}
                            return
                        stop = budget.observe_tool_batch(parsed_tools, raw_tool_results)
                        if stop:
                            final_response = await self._finalize_after_tool_loop(messages, stop)
                            yield build_tool_loop_notice_event(stop)
                            yield {"type": "content", "text": final_response}
                            return

                        # 用纯文本 assistant + user 消息传递工具结果（避免 tool_calls 格式兼容性问题）
                        # 很多模型（如 GLM）对 tool_calls 消息格式支持不完善
                        messages.append({"role": "assistant", "content": current_content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "[系统] 已执行上述工具调用，结果如下。结果已用 JSON 序列化，避免因为换行或引号导致后续解析异常。\n\n"
                                "```json\n"
                                + json.dumps(structured_tool_results, ensure_ascii=False)
                                + "\n```\n\n请根据工具结果继续回复，不要再重复工具调用。"
                            )
                        })
                        if chain_hook is not None:
                            should_stop_by_hook = False
                            try:
                                should_stop_by_hook = await chain_hook({
                                    "type": "_round_done",
                                    "finish_reason": "text_tool_call",
                                    "has_tool_calls": True,
                                    "has_invalid_tool_calls": False,
                                    "executed_tool_calls": parsed_tools,
                                    "executed_tool_results": raw_tool_results,
                                })
                            except Exception:
                                import traceback
                                logger.error(
                                    "[llm_client] chain_hook failed: %s",
                                    traceback.format_exc(),
                                )
                            if should_stop_by_hook:
                                stop = guard.max_rounds_stop()
                                final_response = await self._finalize_after_tool_loop(messages, stop)
                                yield build_tool_loop_notice_event(stop)
                                yield {"type": "content", "text": final_response}
                                return
                        convergence = build_budget_convergence_plan(budget)
                        if convergence:
                            if stream:
                                yield {
                                    "type": "system_notice",
                                    "kind": convergence.notice_kind,
                                    "content": convergence.notice_message,
                                    "payload": convergence.notice_payload,
                                }
                            messages.append({"role": "user", "content": convergence.prompt_message})
                        continue  # 继续下一轮
                    if not stream:
                        yield {"type": "content", "text": current_content}
                    return
                else:
                    # v2 B 迁移：空响应非流式回退用 ApiClient.send_completion
                    # （原直调 client.chat.completions.create(stream=False)）
                    api_client = self._get_api_client()
                    from liuruyan.llm import CoreRequest, RequestContext
                    fb_req = CoreRequest(
                        messages=messages,
                        system=None,
                        tools=tools,
                        model=self._resolve_active_model(),
                        extras={"tool_choice": "auto"},
                    )
                    fb_ctx = RequestContext(
                        agent_id=_current_agent_id_var.get(),
                        session_id=_current_session_id_var.get(),
                    )
                    fb_message = await api_client.send_completion(fb_req, fb_ctx)
                    if not isinstance(fb_message, dict):
                        raise LLMError(
                            f"send_completion returned invalid result: {type(fb_message).__name__}",
                            LLMErrorCode.RESPONSE_FORMAT,
                            retryable=False,
                        )
                    # fb_message 是 dict: {content, tool_calls, finish_reason, usage}
                    await self._record_openai_usage(fb_message.get("usage"))

                    ctl = {
                        "action": None,
                        "retry_count": invalid_tool_retry_count,
                        "retry_limit": invalid_tool_retry_limit,
                    }
                    async for _ev in self._dispatch_completion_fallback(
                        messages, fb_message,
                        guard=guard, budget=budget, chain_hook=chain_hook,
                        stream=stream, ctl=ctl,
                    ):
                        yield _ev
                    invalid_tool_retry_count = ctl["retry_count"]
                    if ctl["action"] == "return":
                        return
                    continue

            except Exception as e:
                import traceback
                traceback.print_exc()

                # ── 短路：rate-limit / 配额耗尽类异常不走非流式 fallback ──
                # 流式失败 → 非流式 fallback 是为了应对"格式不兼容"的代理网关。
                # 但 429 / FreeUsageLimitError / quota exhausted 是配额问题，
                # 重试必然继续失败，每次浪费 5-30s + 给上游再加压。
                # 直接抛进外层 except 让用户立刻看到错误，比假装"在重试"好得多。
                # （subagent 场景尤其明显：3 个并行专家各等 60s 才报错，前端
                #  60s 内看不到任何事件——线上事故 2026-05-06。）
                _err_str_lower = str(e).lower()
                _err_type_name = type(e).__name__
                _is_rate_limit = (
                    "ratelimit" in _err_type_name.lower()
                    or "429" in _err_str_lower
                    or "rate limit" in _err_str_lower
                    or "freeusagelimit" in _err_str_lower
                    or "quota" in _err_str_lower
                    or "insufficient_quota" in _err_str_lower
                )
                if _is_rate_limit:
                    logger.warning(
                        "[LLMClient] rate-limit 类异常跳过 send_completion fallback "
                        "(类型=%s)——避免浪费时间继续撞配额墙",
                        _err_type_name,
                    )
                    raise

                # 如果 messages 中有 tool role 消息，说明之前走的是文本解析路径
                # 这种情况下用纯文本格式回退（不带 tools 参数），避免 tool_calls 格式兼容性问题
                has_tool_msgs = any(m.get("role") == "tool" for m in messages)
                try:
                    # v2 B 迁移：异常 fallback 用 ApiClient.send_completion
                    # （原直调 client.chat.completions.create(stream=False)）
                    api_client = self._get_api_client()
                    from liuruyan.llm import CoreRequest, RequestContext
                    fb_req = CoreRequest(
                        messages=messages,
                        system=None,
                        tools=None if has_tool_msgs else (tools if tools else None),
                        model=self._resolve_active_model(),
                        # tool_choice=None（不传 → SDK 默认）
                    )
                    fb_ctx = RequestContext(
                        agent_id=_current_agent_id_var.get(),
                        session_id=_current_session_id_var.get(),
                    )
                    message = await api_client.send_completion(fb_req, fb_ctx)
                    if not isinstance(message, dict):
                        raise LLMError(
                            f"send_completion returned invalid result: {type(message).__name__}",
                            LLMErrorCode.RESPONSE_FORMAT,
                            retryable=False,
                        )
                    # message 是 dict: {content, tool_calls, finish_reason, usage}
                    await self._record_openai_usage(message.get("usage"))
                    ctl = {
                        "action": None,
                        "retry_count": invalid_tool_retry_count,
                        "retry_limit": invalid_tool_retry_limit,
                    }
                    async for _ev in self._dispatch_completion_fallback(
                        messages, message,
                        guard=guard, budget=budget, chain_hook=chain_hook,
                        stream=stream, ctl=ctl,
                    ):
                        yield _ev
                    invalid_tool_retry_count = ctl["retry_count"]
                    if ctl["action"] == "return":
                        return
                    continue

                except Exception as exc:
                    import traceback
                    tb_text = traceback.format_exc()
                    logger.error("[LLMClient] 工具循环异常: %s: %s\n%s", type(exc).__name__, exc, tb_text)
                    err_str = str(exc)
                    err_type = type(exc).__name__

                    # 调试：打印原始错误信息
                    logger.debug("[DEBUG] err_type=%s, err_str=%s", err_type, err_str)
                    logger.debug("[DEBUG] err_str.lower()=%s", err_str.lower())
                    logger.debug("[DEBUG] 'one_api_error' in err_str.lower() = %s", 'one_api_error' in err_str.lower())

                    # 友好化常见错误
                    kind, friendly = _classify_llm_exception(err_type, err_str)

                    yield {
                        "type": "system_notice",
                        "kind": kind,
                        "content": friendly,
                        "payload": {
                            "error_type": err_type,
                            "error_message": err_str,
                        },
                    }
                    # 同时把原始 err_type/err_str 拼进 content 暴露给 UI（派发卡展开 / worker 工作台），
                    # 友好文案只够定性，排查兼容性问题（如 OpenAI 兼容代理网关 BadRequestError）
                    # 必须看 raw message：unknown model / unsupported tools / context overflow 等都在这里
                    yield {
                        "type": "content",
                        "text": (
                            f"⚠️ {friendly}\n\n"
                            f"**原始错误**：`{err_type}`\n\n"
                            f"```\n{err_str}\n```"
                        ),
                    }
                    # === C4-c：v1 异常路径调 chain_hook（mw 06 LLMErrorHandling 真生效）===
                    # 让 chain 看到本轮 LLM 抛出的可归一化错误，state["last_llm_error"]
                    # 由 LeadAgent chain_hook 写入；mw 06 after_model 看到后 emit
                    # llm_error_observed 事件到 EventBus + TraceJournal 落盘。
                    # 注意：v1 异常归一化（_classify_llm_exception + yield system_notice）
                    # 仍真执行——mw 06 当前是 metric-only，不重复决策。真接管错误归一化
                    # （决定 swallow / retry / abort）留 C4-b 升级（要扩展 chain_hook 协议）。
                    if chain_hook is not None:
                        try:
                            error_round_done = {
                                "type": "_round_done",
                                "finish_reason": None,
                                "has_tool_calls": False,
                                "has_invalid_tool_calls": False,
                                "empty_response": False,
                                "text_only": False,
                                "current_content": "",
                                "assistant_msg": None,
                                "all_tool_call_ids": [],
                                "tool_result_msgs": [],
                                "executed_tool_calls": [],
                                "executed_tool_results": [],
                                "spawn_dispatched": False,
                                "invalid_tool_calls": [],
                                "llm_error": {
                                    "kind": kind,
                                    "friendly": friendly,
                                    "err_type": err_type,
                                    "err_str": err_str,
                                },
                            }
                            await chain_hook(error_round_done)
                        except Exception:
                            import traceback
                            logger.error(
                                "[llm_client] chain_hook (error path) failed: %s",
                                traceback.format_exc(),
                            )
                    # =============================================================
                    return

        # 超过最大轮数
        stop = guard.max_rounds_stop()
        final_response = await self._finalize_after_tool_loop(messages, stop)
        yield build_tool_loop_notice_event(stop)
        yield {"type": "content", "text": final_response}


# 全局客户端实例
_llm_client: Optional[LLMClient] = None


def _sync_llm_client_to_global_default(client: "LLMClient") -> None:
    """单例首次创建后同步 self.* 字段到 providers config 的 current_model/provider。

    为什么必须做：
    - LLMClient.__init__ 默认 model='astron-code-latest'（line 286 历史遗留 default）
    - 启动后**没人调** set_model 的话，self.model 就一直是这个 stale 值
    - 老路径 _configure_runtime_model 改 self.* 字段，每次 chat 都同步一次——所以
      之前没暴露这个问题
    - 阶段 3 把业务调用点切到 ContextVar 后，ContextVar 缺失时 fallback 到 self.*
      就回退到 stale default → user 2026-05-09 23:18 报的 401 ModelError 事故
    - 修补：单例首次创建立刻按全局 current_* 同步一次，作为正确的 fallback baseline。

    本函数 fail-safe：providers config 读不到 / 损坏时静默忽略，让 self.* 保留
    __init__ 的 default（不会比 stale default 更糟）。
    """
    try:
        from .providers.store import find_model, load_providers_config_cached
        config = load_providers_config_cached()
        current_model = config.get("current_model")
        current_provider = config.get("current_provider")
        if not current_model:
            return

        try:
            from backend.compute_accounts.models_provider import is_pro_provider
            if is_pro_provider(current_provider):
                client.set_model(
                    current_model,
                    base_url="https://generativelanguage.googleapis.com",
                    api_key="PRO_ACCOUNT_MANAGED",
                    provider="openai",
                )
                logger.info(
                    "[LLMClient] synced to global compute account: model=%r provider=%r",
                    current_model, current_provider,
                )
                return
        except Exception:
            pass

        pc = find_model(config, current_model, current_provider)
        if pc is None:
            return
        provider_key = pc.transport.replace("_chat", "").replace("_messages", "")
        client.set_model(
            current_model,
            base_url=pc.base_url,
            api_key=pc.api_key,
            provider=provider_key,
            default_headers=pc.extra_headers,
        )
        logger.info(
            "[LLMClient] synced to global default: model=%r provider=%r kind=%r",
            current_model, pc.id, provider_key,
        )
    except Exception as exc:
        # 不要阻断启动；fallback 到 __init__ default
        logger.warning("[LLMClient] _sync_llm_client_to_global_default failed (ignored): %s", exc)


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
        _sync_llm_client_to_global_default(_llm_client)
    return _llm_client
