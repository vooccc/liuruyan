"""v2 · H · P2-B-1 · AnthropicCachePlugin

把 system prompt 转 list[block] 形式 + 给 static 部分打 cache_control 标记，
启用 Anthropic prompt cache（ephemeral 5 分钟 + extended 1 小时未支持）。

源算法：搬自 backend AnthropicClient._prepare_system_with_cache。

输入契约：
- caller 在 CoreRequest.extras["prompt_cache_info"] 塞配置 dict：
    {
        "enable_caching": bool,    # 是否启用 cache
        "static": str,             # 静态内容（可缓存）
        "context_checkpoint": str, # 稳定会话检查点（可选、可缓存）
        "dynamic": str,            # 动态内容（不缓存）
    }
- caller 必须同时设 req.system（plain str），plugin 据此切片 + 兜底保留 extras

行为：
- enable_caching=False / 缺 prompt_cache_info / static 为空：透传（plugin no-op）
- enable_caching=True + static 非空：
  * 在 req.system 字符串里定位 static 前缀，切出剩余文本（dynamic + 任何
    caller 之后追加的 system_extra / memory / recall / mw 注入等）
  * 无检查点时保持旧版两段行为
  * 有检查点时构造 static(cache) -> checkpoint(cache) -> dynamic/extras(no cache)
  * 写 extras["system"] = list[block]（走 P1-E-2a 修复后的直通通道）
  * req.system 设 None（避免 _build_sdk_params 走 str 拼接路径覆盖 list）
  * extras["prompt_cache_info"] 移除（已消费，避免下游 plugin 误用）
- enable_caching=True 但 static 不在 req.system 开头（边界 / caller 拼装异常）：
  * fail-safe：不启用缓存，但保留 req.system 原样透传（不丢内容）

为什么不无脑用 prompt_cache_info.dynamic 作第二段（旧实现的 bug）：
  caller（backend.agent.build_messages）在 base_system 之外还会拼 system_extra
  （工作模式纪律 / spawn hint / cron hint），mw 03 UserMemoryInject 也会再
  注入 {role:system} 块经 _split_system_and_chat_messages 合并到 req.system。
  旧实现用 prompt_cache_info.dynamic 替代第二段 → 这些"夹在 dynamic 之后"
  的内容全部被丢。改成切片 req.system 后，无论 caller 拼了多少，剩余文本
  都会作为第二个 block 保留。

为什么不打末两条 user 的 cache_control（通用供应商模型列表 有但本算法没）：
- 保持跟 backend 历史行为 bit-equivalent
- "末两条 user 打 cache" 作为后续 enhance plugin（独立切片，看真机收益再做）
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict

from ..api_client import CoreRequest, RequestContext


_PROMPT_SECTION_SEPARATOR = "\n\n---\n\n"


class AnthropicCachePlugin:
    """Anthropic prompt cache（system 块）outgoing plugin。"""

    name = "anthropic_cache"

    def transform_request(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> CoreRequest:
        prompt_cache_info: Dict[str, Any] | None = req.extras.get("prompt_cache_info")

        # 不启用 cache / 缺配置 → 透传（plugin no-op）
        if not prompt_cache_info or not prompt_cache_info.get("enable_caching"):
            return req

        static = (prompt_cache_info.get("static") or "").strip()
        checkpoint = (
            prompt_cache_info.get("context_checkpoint") or ""
        ).strip()

        # static 空时 cache 没意义（cache_control 必须挂在有内容的 block 上）
        if not static:
            return req

        system_text = (req.system or "").strip()

        # static 必须出现在 system_text 开头才能稳定切片；否则 fail-safe 不缓存。
        # 不缓存胜过丢内容（用户的工作模式纪律 / 记忆 / recall 必须送达模型）。
        if not system_text.startswith(static):
            new_extras = {**req.extras}
            new_extras.pop("prompt_cache_info", None)
            return dataclasses.replace(req, extras=new_extras)

        # 切出 static 之后的剩余文本。存在检查点时必须验证它紧跟 static，
        # 否则宁可禁用本次缓存，也不能错误分块或丢失 system 内容。
        rest = system_text[len(static):]
        if checkpoint:
            expected_checkpoint_prefix = _PROMPT_SECTION_SEPARATOR + checkpoint
            if not rest.startswith(expected_checkpoint_prefix):
                new_extras = {**req.extras}
                new_extras.pop("prompt_cache_info", None)
                return dataclasses.replace(req, extras=new_extras)
            rest = rest[len(expected_checkpoint_prefix):]
            if rest.startswith(_PROMPT_SECTION_SEPARATOR):
                rest = rest[len(_PROMPT_SECTION_SEPARATOR):]
            else:
                rest = rest.lstrip()
        else:
            rest = rest.lstrip()
            # build_system_prompt 用 "\n\n---\n\n" 拼 static / dynamic 两段；
            # 吃掉前导分隔符让第二个 block 看起来干净（保持旧行为）。
            if rest.startswith("---"):
                rest = rest.lstrip("-").lstrip()

        system_blocks: list[Dict[str, Any]] = [
            {
                "type": "text",
                "text": static,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if checkpoint:
            system_blocks.append({
                "type": "text",
                "text": checkpoint,
                "cache_control": {"type": "ephemeral"},
            })
        if rest:
            system_blocks.append({
                "type": "text",
                "text": rest,
            })

        new_extras = {**req.extras, "system": system_blocks}
        new_extras.pop("prompt_cache_info", None)

        return dataclasses.replace(req, system=None, extras=new_extras)


__all__ = ["AnthropicCachePlugin"]
