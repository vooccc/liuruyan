"""Anthropic tools 块 prompt cache plugin。

Why：
    Anthropic prompt cache 的前缀顺序是 `tools → system → messages`。我们已经
    在 `AnthropicCachePlugin` 给 system 静态段打了 cache_control，但 tools 块
    （在 system 之前）却没打——这意味着 Anthropic 实际从 tools 开头到缓存断点
    之前这段**每次都要重新读**，哪怕 system 命中了缓存也只能省 system 那段。

    给 tools[-1] 打上 `cache_control: ephemeral` 后，Anthropic 会把"tools 所有
    字节 + 第一个 system block 字节"合并缓存到同一个 ephemeral 断点，再命中时
    整段一起省。

    柳如烟每 agent 暴露 20-70+ 工具，schema 累计几千到上万 token，这里收益通常
    比 system static 段更大。

Design：
    - 严格 anthropic-only（跟 AnthropicCachePlugin 共享 predicate）
    - 只在 **AnthropicCachePlugin 确实打了 system cache** 的情况下才给 tools
      打 cache_control（通过检查 extras["system"] 是否为 list[block] 判断）。
      理由：如果 system 那边 fail-safe 掉了缓存，tools 单独打没意义（SDK 也
      不会单独缓存 tools——Anthropic 语义是"从头到 cache_control 断点整段
      缓存"，system 不参与缓存时前缀断裂，tools 的 cache_control 变"单独
      缓存 tools 这一大块"——虽然也有收益但容易跟 caller 语义冲突）。
    - tools[-1] 是 OpenAI std 顶层 dict（`{type: function, function: {...}}`），
      plugin 在顶层加 `cache_control` 字段；converter 层透传到 Anthropic SDK
      识别的位置。
    - register 顺序晚于 AnthropicCachePlugin，才能看到 cache 已激活的标记。

Non-goals：
    - 不对 tools 做分桶 / 增量缓存（Anthropic 最多 4 个 cache breakpoint，
      全留给 tools 不值；tools+system 两档最优）
    - 不处理 OpenAI 路径（OpenAI Chat Completions 没有对应缓存语义）
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

from ..api_client import CoreRequest, RequestContext


class AnthropicToolsCachePlugin:
    """给 req.tools[-1] 加 cache_control，利用 Anthropic tools 段缓存。"""

    name = "anthropic_tools_cache"

    def transform_request(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> CoreRequest:
        # 没有工具就没啥可缓存的
        if not req.tools:
            return req

        # 依赖 AnthropicCachePlugin 已经给 system 打好 cache（list[block] 形态）；
        # 否则说明 caller 没开 prompt cache 或走了 fail-safe，这里也不打 tools 缓存
        extras_system = req.extras.get("system") if req.extras else None
        if not isinstance(extras_system, list) or not extras_system:
            return req

        # 深拷一份 tools，避免共享引用污染上游
        new_tools = copy.deepcopy(list(req.tools))
        # 只在最后一个工具顶层加 cache_control；converter 会透传到 Anthropic
        # 风格 tool dict 的 cache_control 字段
        new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}

        return dataclasses.replace(req, tools=new_tools)


__all__ = ["AnthropicToolsCachePlugin"]
