"""v2 · H · P1-B-2 · AnthropicApiClient

实现 ApiClient Protocol（`liuruyan.llm.api_client`）的 Anthropic 适配器。

职责：
1. CoreRequest（vendor-agnostic, OpenAI std messages）→ Anthropic SDK params
2. 调 sdk.messages.stream(...) 拿 vendor 流
3. 通过 get_parser("anthropic", ...) 拿 parser，转换成 ParsedEvent 流
4. 流末必产 RoundEnd（parser 保证）

不在本层做的事（保留给 backend / 未来 P1-E / P2）：
- 工具循环（`_chat_with_tools`）—— backend client 职责
- context_too_long / overloaded 等 retry —— 错误归一化层职责
- auth retry / token refresh —— 配置层职责
- prompt cache (cache_control) 计算 —— P2-B OutgoingPlugin 职责
- thinking_kwargs 自动按 model 路由 —— P2-B OutgoingPlugin 职责
  （P1-B-2 阶段：caller 在 req.extras 里显式塞 thinking）

设计参考：
- _docs/handover_p1_p2_apiclient_plan.md § 三 P1-B
- _research/cherry-studio-main/src/renderer/src/aiCore/clients/AnthropicAPIClient.ts
- backend/anthropic_client.py:584-609（现有 stream → parser 桥接逻辑）
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Mapping, Optional

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    AsyncAnthropic = None  # type: ignore[assignment,misc]
    HAS_ANTHROPIC = False

from ..api_client import CoreRequest, RequestContext
from ..events import ParsedEvent
from ..registry import get_parser
from .anthropic_message_converter import (
    convert_tools_to_anthropic,
    to_anthropic_messages,
)


# base_url 规范化集中在 backend.utils.api_host.normalize_anthropic_base_url
# （对齐 Cherry packages/shared/utils/api/utils.ts）。本地不再保留 ad-hoc 实现。


class AnthropicApiClient:
    """ApiClient 实现：Anthropic 原生 API。

    构造期接收认证 + 端点信息，每次 send_stream 是无状态调用。
    多轮工具循环 / retry / cache 等横切关注点不在本类——在 backend client
    或 mw chain 层。

    Attributes:
        name: "anthropic" 固定。
        client: AsyncAnthropic SDK 客户端。
        base_url: 自定义 endpoint（讯飞 Anthropic 协议 / 第三方代理用）。

    实现纪律（满足 ApiClient Protocol 结构性子类型）：
    1. send_stream 是 async generator（流末通过 parser 发 RoundEnd）
    2. 不读 ContextVar / 全局状态（一切走构造参数 + ctx）
    3. 错误穿透抛（caller 按 LLMError 归一化）
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: Optional[float] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ):
        """构造客户端。

        Args:
            api_key: Anthropic API key（如果是 base_url 自定义端点 +
                auth_token 模式，可以是 ""，但要求 SDK 接受）。
            model: 默认 model id（如 "claude-opus-4-7"）。仍可被 CoreRequest.model
                覆盖——构造期的 model 只是默认值，实际 send_stream 用 req.model。
            base_url: 自定义 endpoint。讯飞 Anthropic 协议 / 第三方反代用。
            auth_token: Bearer token 模式（替代 api_key）。两个不同时给 SDK。
            timeout: SDK 超时（秒）。
            default_headers: 每次 HTTP 请求自动追加的 header dict。典型用途：
                - getrouter / 第三方 Anthropic 反代要 Cherry 风格
                  ``User-Agent`` + ``HTTP-Referer`` + ``X-Title`` 才放行
                - 自建 Anthropic 网关要 ``X-Tenant-Id`` 多租户路由
                空 dict / None 不注入。
        Raises:
            ImportError: 没装 anthropic SDK。
        """
        if not HAS_ANTHROPIC:
            raise ImportError(
                "anthropic SDK 未安装。请 pip install anthropic。"
            )

        self.default_model = model
        # 对齐 Cherry：尾部版本号 /v1 /v2beta /v3alpha 等自动剥（SDK 自加 /v1/messages）；
        # # 后缀是 escape hatch（保留路径，仅剥 #）。完整规则见 backend.utils.api_host。
        from backend.utils.api_host import normalize_anthropic_base_url
        normalized_base_url = normalize_anthropic_base_url(base_url)
        self.base_url = normalized_base_url

        # 注入项目级默认 headers（Cherry-style UA + HTTP-Referer + X-Title）
        # 防聚合 router 反爬。caller 传的 default_headers 会 override 同 key 项。
        from backend.utils.llm_headers import merge_default_llm_headers
        self.default_headers = merge_default_llm_headers(default_headers, transport="anthropic_messages")
        # Cherry-style anthropic-beta：按 default_model 嗅探 interleaved-thinking /
        # output-128k 等 flag 注入。caller 显式设了 anthropic-beta 时不覆盖。
        # 注：v2 client 是 per-model 缓存（registry.get_client 用 model 作 key），
        # 同 client 实例 model 不变，构造期注入即可，无需 _build_client 重算。
        from backend.utils.anthropic_beta import merge_anthropic_beta_into_headers
        merge_anthropic_beta_into_headers(
            self.default_headers,
            self.default_model,
        )

        client_kwargs: Dict[str, Any] = {"default_headers": self.default_headers}
        if auth_token:
            client_kwargs["auth_token"] = auth_token
        else:
            client_kwargs["api_key"] = api_key
        if normalized_base_url:
            client_kwargs["base_url"] = normalized_base_url
        if timeout is not None:
            client_kwargs["timeout"] = timeout

        self.client = AsyncAnthropic(**client_kwargs)

    def _build_sdk_params(self, req: CoreRequest) -> Dict[str, Any]:
        """CoreRequest → Anthropic Messages SDK params。

        转换规则：
        1. messages: OpenAI std → Anthropic content blocks（用 to_anthropic_messages）
        2. system: 三档优先级 ——
           a. req.extras["system"] 显式提供时整个覆盖（caller 直接控制；list[block]
              形式携带 cache_control 结构化数据时，这是唯一安全通道，f-string
              拼接会把 list str 化破坏 cache_control）
           b. req.system + messages 内提取的 system 拼接（str 形式）
           c. 从 messages 内提取的 system
        3. tools: OpenAI std → Anthropic 风格（用 convert_tools_to_anthropic）
        4. model / max_tokens: 直接透传
        5. extras: 任何 SDK 接受的字段（temperature / thinking / stop_sequences /
           metadata 等）都允许透传，但**不允许覆盖核心字段**（model / messages /
           tools / max_tokens）；"system" 已在 2.a 单独处理跳过，extras 透传时跳过。

        Returns:
            可直接 ** 展开到 client.messages.stream() 的 dict。
        """
        system_extracted, anthropic_messages = to_anthropic_messages(req.messages)

        # System 字段三档优先级
        extras_system = req.extras.get("system") if req.extras else None
        if extras_system is not None:
            system_param = extras_system
        elif req.system:
            system_param = (
                req.system if not system_extracted
                else f"{req.system}\n{system_extracted}"
            )
        else:
            system_param = system_extracted

        sdk_params: Dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": anthropic_messages,
        }
        if system_param:
            sdk_params["system"] = system_param
        if req.tools:
            sdk_params["tools"] = convert_tools_to_anthropic(req.tools)

        _CORE_FIELDS = {"model", "max_tokens", "messages", "tools"}
        _OPENAI_ONLY = {"stream_options", "tool_choice"}
        for key, value in req.extras.items():
            if key in _CORE_FIELDS or key == "system" or key in _OPENAI_ONLY:
                continue
            sdk_params[key] = value

        return sdk_params

    def _is_claude_code_disguise_enabled(self) -> bool:
        """检查当前客户端是否启用了 Claude Code 伪装。
        判定依据：
        - headers 包含 X-App: cli
        - headers 包含 claude-code-20250219
        - headers 的 User-Agent 包含 claude-cli
        """
        headers = self.default_headers or {}
        if headers.get("X-App") == "cli":
            return True
        beta = headers.get("anthropic-beta", "")
        if "claude-code" in beta:
            return True
        ua = headers.get("User-Agent", "").lower()
        if "claude-cli" in ua:
            return True
        return False

    def _apply_claude_code_disguise(self, sdk_params: Dict[str, Any]) -> Dict[str, Any]:
        """注入 Claude Code CLI 计费归因头与合规 metadata.user_id。

        规则：
        1. 仅当显式启用 Claude Code 客户端伪装（如预设请求头包含 X-App: cli / claude-code-20250219）时注入；
        2. 默认纯净标准原生请求下：不向 system 插入 billing 块，也不注入伪造 metadata.user_id，
           保持原生零污染，兼容 AWS Bedrock / 自建反代。
        """
        if not self._is_claude_code_disguise_enabled():
            return sdk_params

        import uuid

        params = dict(sdk_params)
        CLAUDE_CODE_BILLING_TEXT = "x-anthropic-billing-header: cc_version=2.1.263; cc_entrypoint=cli; cch=d8726;"
        CLAUDE_CODE_BILLING_BLOCK = {"type": "text", "text": CLAUDE_CODE_BILLING_TEXT}

        system_param = params.get("system")
        if isinstance(system_param, list):
            has_billing = any(
                isinstance(b, dict) and CLAUDE_CODE_BILLING_TEXT in b.get("text", "")
                for b in system_param
            )
            if not has_billing:
                params["system"] = [CLAUDE_CODE_BILLING_BLOCK] + list(system_param)
        elif isinstance(system_param, str):
            if system_param.strip():
                params["system"] = [CLAUDE_CODE_BILLING_BLOCK, {"type": "text", "text": system_param}]
            else:
                params["system"] = [CLAUDE_CODE_BILLING_BLOCK]
        else:
            params["system"] = [CLAUDE_CODE_BILLING_BLOCK]

        # metadata.user_id 注入
        meta = params.get("metadata")
        if not meta or not isinstance(meta, dict) or "user_id" not in meta:
            meta_dict = dict(meta) if isinstance(meta, dict) else {}
            u1 = uuid.uuid4().hex
            u2 = uuid.uuid4().hex
            account_hash = (u1 + u2)[:64]
            session_id = uuid.uuid4()
            meta_dict["user_id"] = f"user_{account_hash}_account__session_{session_id}"
            params["metadata"] = meta_dict

        return params

    async def send_completion(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        """非流式调用，累积 send_stream 输出拼成 OpenAI std message-like dict
        （健康检查 / 一次性回复路径用）。

        实现：内部跑 send_stream → 累积 ContentDelta + ToolCallComplete + Usage
        + RoundEnd 事件，还原成 message dict。

        Note:
            tool_calls 字段当前不支持（Anthropic 路径下 fallback 不存在；健康
            检查无需 tool_calls）。如果 req.tools 非空且模型真返了 tool_use
            block，本方法不会抓——caller 改用 send_stream 处理工具循环。

        Returns:
            {"content": str|None, "tool_calls": None, "finish_reason": str|None,
             "usage": vendor SDK 原 usage 对象}
        """
        content_parts: list[str] = []
        finish_reason: Optional[str] = None
        usage: Any = None
        final_message: Any = None

        async for ev in self.send_stream(req, ctx):
            ev_type = ev.type
            if ev_type == "content_delta":
                content_parts.append(ev.text)
            elif ev_type == "round_end":
                finish_reason = ev.finish_reason
            elif ev_type == "usage":
                usage = ev.payload
            elif ev_type == "final_assistant_content":
                final_message = ev.final_message

        # final_message 优先于 round_end stop_reason（更权威）
        if final_message is not None:
            stop_reason = getattr(final_message, "stop_reason", None)
            if stop_reason:
                finish_reason = stop_reason
            if usage is None:
                usage = getattr(final_message, "usage", None)

        return {
            "content": "".join(content_parts) if content_parts else None,
            "tool_calls": None,
            "finish_reason": finish_reason,
            "usage": usage,
            "model": getattr(final_message, "model", None) if final_message else None,
            # raw_response：完整 SDK Message 对象（供 /models/test-connection 中
            # relay_detection 形态分析用，正常 chat 路径忽略）。下划线前缀语义=内部用。
            "_raw_response": final_message,
        }

    async def send_stream(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> AsyncIterator[ParsedEvent]:
        """单次 LLM 流式调用，产 ParsedEvent 流。

        实现：
        1. **(P2-C)** 跑 outgoing plugin 链改写 req（按 (name, model, base_url)
           builder 路由）—— cache_control / thinking_kwargs 等自动注入
        2. _build_sdk_params 把改写后的 CoreRequest 翻译成 Anthropic SDK 入参
        3. async with client.messages.stream(...) 进流
        4. get_parser("anthropic", ...) 拿 parser
        5. parser(s, parse_ctx, final_message_getter=s.get_final_message) 解析事件
        6. 每个 event yield 给 caller，期间检查 ctx.cancel_event 协作式取消

        Cancel 行为（duck-typing）：
        - ctx.cancel_event.is_set() 为 True 时停止 yield 并退出（不再消费 sdk
          stream，让 async with 自然关闭流）。
        - 不强制 ctx.cancel_event 是 asyncio.Event 实例 —— 只要 hasattr is_set。

        Note:
            P1-B-2 阶段不做错误归一化（不抓 anthropic.APIError 转 LLMError）。
            穿透抛给 caller，让 backend client 或 mw chain 决定怎么处理。
        """
        # P2-C: 应用 outgoing plugin 链
        from ..outgoing_plugins import get_default_outgoing_plugin_builder
        builder = get_default_outgoing_plugin_builder()
        plugins = builder.build(self.name, req.model, self.base_url)
        for plugin in plugins:
            req = plugin.transform_request(req, ctx)

        sdk_params = self._build_sdk_params(req)
        sdk_params = self._apply_claude_code_disguise(sdk_params)

        async with self.client.messages.stream(**sdk_params) as s:
            parser, parse_ctx = get_parser(
                provider="anthropic",
                model=req.model,
                base_url=self.base_url,
                # Anthropic 路径不启用 text_leaked plugin（用 final_message
                # 作权威源），保持接口一致
                get_known_tool_names=None,
            )
            async for ev in parser(
                s,
                parse_ctx,
                final_message_getter=s.get_final_message,
            ):
                # 协作式取消（duck-typing；不强制 asyncio.Event）
                if ctx.cancel_event is not None:
                    try:
                        if ctx.cancel_event.is_set():
                            return
                    except (AttributeError, Exception):
                        pass  # cancel_event 不支持 is_set，忽略
                yield ev


__all__ = ["AnthropicApiClient"]
