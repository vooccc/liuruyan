"""Anthropic Messages API ``anthropic-beta`` header 拼装。

设计动机
========
Anthropic 把 beta 功能锁在 ``anthropic-beta: <flag-YYYY-MM-DD>`` header 里。
不同 flag 控制不同行为（交错思考、细粒度工具流、128K 输出等），
而且每个 flag 有发布日期版本号——客户端通过日期"锁定"行为版本，避免后端
升级时静默改变语义。

完整语义参考：
- 交错思考：https://docs.claude.com/en/docs/build-with-claude/extended-thinking#interleaved-thinking
- 细粒度工具流：https://docs.claude.com/en/docs/build-with-claude/tool-use/fine-grained-tool-streaming
- 128K 输出：https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking

为什么是 Anthropic 专属
========================
OpenAI 用模型名后缀（``-preview`` / ``-turbo``）做 feature gate；Gemini 用
URL path 版本（``/v1beta/`` vs ``/v1/``）；只有 Anthropic 把功能 flag 放在
请求 header 里，所以这个 helper 只在 Anthropic 路径上调用。

设计参考：Cherry Studio 的 ``addAnthropicHeaders``
==================================================
``_research/cherry-studio-main/src/renderer/src/aiCore/prepareParams/header.ts``

Cherry 不无脑全开 beta，而是按"模型 + 模式 + provider"动态拼，避开几个雷：

1. **Vertex AI / AWS Bedrock 不支持 ``interleaved-thinking``**
   这两个云厂商透传 Anthropic 时屏蔽了 beta header，开了直接 400。
   柳如烟当前不接 Vertex/Bedrock，但 helper 仍保留 transport 跳过位
   以备未来扩展。

2. **``interleaved-thinking`` 仅在 Claude 4.5+ 推理模型 + function-call
   模式下有意义**
   Claude 3.x / 4.0 即使收到 header 也是 no-op；XML tool 模式下交错思考
   不参与 tool_use 推理，加了浪费协议位。

3. **``output-128k-2025-02-19`` 仅 Sonnet 3.7 需要**
   3.7 默认 max_tokens 上限 8192，加这个 flag 才能拉到 128K。
   Sonnet 4 / 4.5 的 128K 是默认能力，加了 header 是 no-op 但不报错。
   保守起见仅给 3.7 加。

4. **``fine-grained-tool-streaming`` 配套 ``interleaved-thinking`` 加**
   这俩在 Cherry 的 Claude Code OAuth 路径里捆绑出现，单独开 fine-grained
   不带 interleaved 也能 work，但配套加体验更顺（tool_use 参数边思边流）。
"""
from __future__ import annotations

import re
from typing import List, Optional


# ── Beta flag 字面量 ──────────────────────────────────────────────────────
# 这些日期是 Anthropic 公布的 beta feature 首发日期，**不是** 我们填什么都行——
# 升级 flag（比如 ``interleaved-thinking-2025-09-XX`` 出来）前必须确认 Anthropic
# 后端仍接受老日期，否则要同步柳如烟+回归测试。
INTERLEAVED_THINKING = "interleaved-thinking-2025-05-14"
FINE_GRAINED_TOOL_STREAMING = "fine-grained-tool-streaming-2025-05-14"
OUTPUT_128K = "output-128k-2025-02-19"


def _norm(model: Optional[str]) -> str:
    """模型名规范化：lower + 替换 . 为 -，统一对比。

    Anthropic 模型在不同入口的 ID 风格不一：
    - 官方：``claude-3-7-sonnet-20250219``、``claude-sonnet-4-5-20251022``
    - OpenRouter：``anthropic/claude-3.7-sonnet`` / ``anthropic/claude-sonnet-4.5``
    - 第三方反代：``claude-opus-4-7-thinking`` / ``claude-4.5-sonnet``

    统一成 lower + 全 ``-`` 分隔，匹配模式按子串。
    """
    if not model:
        return ""
    return model.lower().replace(".", "-")


## Claude 4.5+ 推理模型识别正则
##
## 匹配两类命名风格：
## - `claude-{family}-4-{minor}` （官方 + 多数反代）→ family ∈ {sonnet, opus, haiku}
## - `claude-4-{minor}-{family}` （部分 OpenRouter 风格）
##
## minor 必须 >= 5 才算 4.5+ 系列：
## - 4-0 / 4-{date} 是 Claude 4.0（claude-sonnet-4-20250514），不开 beta
## - 4-1, 4-2, 4-3, 4-4 实际未发布；保守起见也不开
## - 4-5 / 4-6 / 4-7 / 4-8 / 4-9 / 4-10 / ... 都开
## - 5-x 和未来更高 major 走另外的判定（_CLAUDE_5_PLUS_PATTERN）
##
## 设计目标：Anthropic 出 4.6/4.7/4.8 不用改代码，自动覆盖。
##
## 关键约束：``\d{1,2}(?!\d)`` 限制 minor 是 1-2 位且后面不能再跟数字。
## 防止 ``claude-sonnet-4-20250514``（4.0 dated）被误识别成 minor=20250514。
## 8 位日期不会被 ``\d{1,2}(?!\d)`` 切出来，因为 "2" 后接 "0" 是数字。
_CLAUDE_4_X_PATTERN = re.compile(
    r"claude-(?:sonnet|opus|haiku)-4-(\d{1,2})(?!\d)"
    r"|claude-4-(\d{1,2})(?!\d)-(?:sonnet|opus|haiku)"
)

## Claude 5.x 及更高 major 直接全开（推理模型默认能力，5.x 都内置 interleaved thinking）。
## Anthropic 真出 Claude 5 时如果约定不一样再回头改。
##
## 三种 alt 格式都覆盖：
## - ``claude-sonnet-5-0`` / ``claude-sonnet-5-0-20270101``
## - ``claude-5-0-sonnet`` / ``claude-6-0-opus``
## - ``claude-5-sonnet`` （无 minor 版本）
_CLAUDE_5_PLUS_PATTERN = re.compile(
    r"claude-(?:sonnet|opus|haiku)-(\d+)-"
    r"|claude-(\d+)-(?:\d+-)?(?:sonnet|opus|haiku)"
)


def is_claude_4_5_reasoning_model(model: Optional[str]) -> bool:
    """识别 Claude 4.5+ 系列推理模型（Sonnet 4.5 / Opus 4.5/4.6/4.7+ / 5.x 等）。

    判定逻辑：
    1. ``claude-{family}-4-N`` 或 ``claude-4-N-{family}`` 中 N >= 5 → 命中
    2. ``claude-{family}-M-...`` 或 ``claude-M-...-{family}`` 中 M >= 5 → 命中
    3. 否则不命中（Claude 4.0 / 3.x / 非 Claude 模型）

    覆盖示例：
    - claude-sonnet-4-5 → 命中（4.5）
    - claude-sonnet-4-5-20251022 → 命中（4.5 dated）
    - claude-opus-4-7 → 命中（部分反代代号 / 4.7）
    - claude-sonnet-4-6 → 命中（4.6，未来发布）
    - claude-opus-4-9 → 命中（4.9）
    - claude-opus-4-10 → 命中（4.10）
    - claude-sonnet-5-0 → 命中（5.x）
    - anthropic/claude-sonnet-4.5 → 命中（_norm 把 . 转 -）
    - claude-sonnet-4-20250514 → **不命中**（4.0 dated）
    - claude-3-7-sonnet → **不命中**（3.x）
    - gpt-5 → **不命中**（非 Claude）
    """
    n = _norm(model)
    if not n:
        return False

    # 1. 4.x 系列：minor >= 5
    m = _CLAUDE_4_X_PATTERN.search(n)
    if m:
        minor = int(m.group(1) or m.group(2))
        if minor >= 5:
            return True

    # 2. 5.x 及以上 major：全开
    m = _CLAUDE_5_PLUS_PATTERN.search(n)
    if m:
        major = int(m.group(1) or m.group(2))
        if major >= 5:
            return True

    return False


def is_claude_3_7_sonnet(model: Optional[str]) -> bool:
    """识别 Claude 3.7 Sonnet（需 ``output-128k`` flag 解锁 128K 输出）。"""
    n = _norm(model)
    return "claude-3-7-sonnet" in n if n else False


def is_anthropic_native_provider(transport: Optional[str]) -> bool:
    """判定是否走 Anthropic 直连协议（而非 Vertex/Bedrock 透传）。

    柳如烟当前用 ``transport`` 字段标识 ``anthropic_messages`` / ``openai_chat``。
    Vertex / Bedrock 接入未来会有独立 transport（``anthropic_vertex`` /
    ``anthropic_bedrock``），届时这两个返 False 跳过 beta header。

    None / 未知 transport 默认返 True（保守开 beta，错了 Anthropic 也只是
    报 400 容易调）。
    """
    if not transport:
        return True  # 默认认为是 Anthropic 直连
    t = transport.lower()
    # 已知不支持 anthropic-beta 的 transport
    if t in ("anthropic_vertex", "anthropic_bedrock"):
        return False
    return True


def compute_anthropic_beta_flags(
    model: Optional[str],
    *,
    transport: Optional[str] = None,
    function_call_mode: bool = True,
) -> List[str]:
    """根据模型 + 模式拼装 beta flag 列表。

    Args:
        model: 模型 ID。``None`` 或空串返回空列表。
        transport: 协议透传标识。Vertex / Bedrock 直接返空（不支持 beta）。
        function_call_mode: 是否走 function-call 模式（vs XML tool 模式）。
            ``interleaved-thinking`` 只在 function-call 下有效；柳如烟所有 Claude
            路径默认走 function-call，所以默认 ``True``。
    Returns:
        beta flag string 列表。空列表表示不需要加 ``anthropic-beta`` header。

    决策表（与 Cherry 对齐）：

    | 条件 | 加的 flag |
    |---|---|
    | Claude 4.5+ 推理模型 + function-call | ``interleaved-thinking-2025-05-14`` + ``fine-grained-tool-streaming-2025-05-14`` |
    | Claude 3.7 Sonnet（任意模式） | ``output-128k-2025-02-19`` |
    | Vertex / Bedrock | （跳过全部） |
    """
    if not is_anthropic_native_provider(transport):
        return []

    flags: List[str] = []

    if function_call_mode and is_claude_4_5_reasoning_model(model):
        flags.append(INTERLEAVED_THINKING)
        flags.append(FINE_GRAINED_TOOL_STREAMING)

    if is_claude_3_7_sonnet(model):
        flags.append(OUTPUT_128K)

    return flags


def compute_anthropic_beta_header(
    model: Optional[str],
    *,
    transport: Optional[str] = None,
    function_call_mode: bool = True,
) -> Optional[str]:
    """同 ``compute_anthropic_beta_flags`` 但直接返回逗号拼接的 header value。

    Returns:
        ``"interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"``
        这种格式的字符串。空列表时返回 ``None``，调用方应跳过设 header。
    """
    flags = compute_anthropic_beta_flags(
        model,
        transport=transport,
        function_call_mode=function_call_mode,
    )
    return ",".join(flags) if flags else None


def merge_anthropic_beta_into_headers(
    base_headers: dict,
    model: Optional[str],
    *,
    transport: Optional[str] = None,
    function_call_mode: bool = True,
) -> dict:
    """把按模型嗅探出的 beta flags 合并进 ``base_headers``。

    覆盖语义：
    - caller 已在 ``base_headers`` 里显式设了 ``anthropic-beta``：
      - 若包含 baseline 的 'claude-code-20250219'，则将按模型嗅探的新 flags 并入（去重拼接）；
      - 若不包含，说明是 caller 显式自定义覆盖（例如调试 context-1m-2025-08-07），尊重不追加；
    - 否则按模型嗅探，若有 flag 则写入；无 flag 不写

    这是核心契约：*用户可以通过 provider.extra_headers 显式传 anthropic-beta
    覆盖默认行为*（例如调试 interleaved-thinking）。

    Args:
        base_headers: 已经 merge 过 default_llm_headers 的 dict（mutate in-place）。

    Returns:
        同 ``base_headers``（同一对象，方便链式调用）。
    """
    if not is_anthropic_native_provider(transport):
        return base_headers

    existing = base_headers.get("anthropic-beta")
    is_claude_code = (
        (existing and "claude-code-20250219" in existing)
        or base_headers.get("X-App") == "cli"
        or "claude-cli" in base_headers.get("User-Agent", "").lower()
    )

    if is_claude_code:
        value = compute_anthropic_beta_header(
            model,
            transport=transport,
            function_call_mode=function_call_mode,
        )
        if not value:
            return base_headers

        if not existing:
            base_headers["anthropic-beta"] = value
        else:
            existing_flags = [f.strip() for f in existing.split(",") if f.strip()]
            new_flags = [f.strip() for f in value.split(",") if f.strip()]
            for f in new_flags:
                if f not in existing_flags:
                    existing_flags.append(f)
            base_headers["anthropic-beta"] = ",".join(existing_flags)
    else:
        # 纯净原生模式：仅在 3.7 Sonnet 时考虑 output-128k；绝不追加 interleaved-thinking / fine-grained-tool-streaming
        # 规避 AWS Bedrock / justwoker 等中转站因 SSE 工具调用分包导致的参数丢包解析为空字典 {} 问题。
        if is_claude_3_7_sonnet(model):
            if not existing:
                base_headers["anthropic-beta"] = OUTPUT_128K
            elif OUTPUT_128K not in existing:
                base_headers["anthropic-beta"] = f"{existing},{OUTPUT_128K}"

    return base_headers

