"""v2 · H · P2-D · OvmsNoThinkPlugin（待启用）

OpenVINO Model Server (OVMS) 自部署 OpenAI 兼容端点：MCP 工具调用前需要
关闭 thinking 模式（OVMS 默认行为差异）。本 plugin 检测 tools 非空时
自动给末条 user 追 `/no_think`。

⚠️ 状态：**待启用**。当前柳如烟未接入 OVMS provider，本 plugin 写出来
保留代码层为未来准备，但 `register_builtin_plugins()` **不**注册到 builder。
等真接入 OVMS 时再开启注册。

参考：通用供应商模型列表 `noThinkPlugin`（OVMS 路径）。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

from ..api_client import CoreRequest, RequestContext


_NO_THINK_TRAILER = "/no_think"


class OvmsNoThinkPlugin:
    """OVMS provider MCP 工具调用前关闭 thinking。

    路由：仅 OVMS base_url（待接入）+ req.tools 非空时启用。
    """

    name = "ovms_no_think"

    def transform_request(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> CoreRequest:
        # 没工具调用就没必要关 thinking（thinking 帮助 reasoning）
        if not req.tools:
            return req

        new_messages = self._append_no_think_trailer(req.messages)
        if new_messages is req.messages:
            return req
        return dataclasses.replace(req, messages=new_messages)

    @staticmethod
    def _append_no_think_trailer(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """末条 user.content 是 str 时追 /no_think（已有则不重复）。"""
        if not messages:
            return messages

        last = messages[-1]
        if last.get("role") != "user":
            return messages

        content = last.get("content")
        if not isinstance(content, str):
            return messages

        if content.rstrip().endswith(_NO_THINK_TRAILER):
            return messages

        new_content = (
            f"{content}\n\n{_NO_THINK_TRAILER}" if content else _NO_THINK_TRAILER
        )
        new_last = {**last, "content": new_content}
        return messages[:-1] + [new_last]


__all__ = ["OvmsNoThinkPlugin"]
