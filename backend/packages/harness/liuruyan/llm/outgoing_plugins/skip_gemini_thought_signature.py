"""v2 · H · P2-D · SkipGeminiThoughtSignaturePlugin（待启用）

Gemini API（OpenAI 兼容路径）的 tool_call 响应里有时携带 vendor 私有
``thought_signature`` 字段，导致下游消费链报"未知字段"。本 plugin 在
工具调用 args 里清掉这个字段。

⚠️ 状态：**待启用**。当前柳如烟未接入 Gemini provider，本 plugin 写出来
保留代码层为未来准备，但 `register_builtin_plugins()` **不**注册到 builder。
等真接入 Gemini（providers_data 加 GEMINI ProviderDef）时再开启注册。

参考：cherry-studio `skipGeminiThoughtSignaturePlugin`。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

from ..api_client import CoreRequest, RequestContext


class SkipGeminiThoughtSignaturePlugin:
    """剥离 Gemini tool_call args 中的 thought_signature 字段（OpenAI 兼容路径）。

    路由：仅 Gemini provider（待接入）+ tools 非空时启用。
    """

    name = "skip_gemini_thought_signature"

    def transform_request(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> CoreRequest:
        if not req.tools:
            return req

        new_tools = self._strip_thought_signature(req.tools)
        if new_tools is req.tools:
            return req
        return dataclasses.replace(req, tools=new_tools)

    @staticmethod
    def _strip_thought_signature(
        tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """从每个 tool 定义里清掉 function.parameters.properties.*.thought_signature。

        Gemini 在 tool 定义返回时会塞这个字段；下游 OpenAI 兼容协议消费方
        识别不了。剥它一层。
        """
        modified = False
        new_tools: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                new_tools.append(tool)
                continue
            new_tool = dict(tool)
            fn = new_tool.get("function")
            if isinstance(fn, dict):
                params = fn.get("parameters")
                if isinstance(params, dict):
                    props = params.get("properties")
                    if isinstance(props, dict):
                        for prop_name, prop_def in props.items():
                            if isinstance(prop_def, dict) and "thought_signature" in prop_def:
                                new_prop = {k: v for k, v in prop_def.items()
                                            if k != "thought_signature"}
                                # 重建嵌套 dict 链 immutability
                                new_props = {**props, prop_name: new_prop}
                                new_params = {**params, "properties": new_props}
                                new_fn = {**fn, "parameters": new_params}
                                new_tool["function"] = new_fn
                                modified = True
                                break  # 单 tool 单次重建
            new_tools.append(new_tool)

        return new_tools if modified else tools


__all__ = ["SkipGeminiThoughtSignaturePlugin"]
