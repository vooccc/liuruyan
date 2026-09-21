"""v2 · H · P1-C · OpenAICompatibleApiClient

实现 ApiClient Protocol 的 OpenAI / OpenAI 兼容协议适配器。
覆盖：OpenAI / 讯飞 OpenAI 兼容 / GLM / Kimi / DeepSeek / 豆包 / Mistral /
Groq / Together / Fireworks / SiliconFlow / OpenRouter 等所有 OpenAI 协议端点。

设计与 AnthropicApiClient 同源（`.anthropic`）：
1. CoreRequest（vendor-agnostic, OpenAI std messages）→ OpenAI SDK params
2. 调 client.chat.completions.create(stream=True) 拿 vendor 流
3. 通过 get_parser("openai", ...) 拿 parser，转换成 ParsedEvent 流
4. 流末必产 RoundEnd（parser 保证）

不在本层做的事（保留给 backend / 未来 P1-E / P2）：
- 工具循环（`_chat_with_tools`）—— backend client 职责
- context_too_long / overloaded retry —— 错误归一化层职责
- prompt cache / qwen thinking / gemini thought_signature —— P2-B OutgoingPlugin 职责

设计参考：
- _docs/handover_p1_p2_apiclient_plan.md § 三 P1-C
- _research/cherry-studio-main/src/renderer/src/aiCore/clients/OpenAIAPIClient.ts
- backend/llm_client.py:696-722（现有 _chat_stream / _chat_complete）
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Dict, Optional

try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]
    HAS_OPENAI = False

from ..api_client import CoreRequest, RequestContext
from ..events import ParsedEvent
from ..registry import get_parser
from .openai_message_converter import to_openai_messages


class OpenAICompatibleApiClient:
    """ApiClient 实现：OpenAI / OpenAI 兼容协议。

    构造期接收认证 + 端点信息（base_url 决定走 OpenAI / 讯飞 / GLM / Kimi /
    DeepSeek / 豆包 / Mistral 等），每次 send_stream 是无状态调用。

    Attributes:
        name: "openai_compat" 固定（区别于 AnthropicApiClient.name="anthropic"）。
        client: AsyncOpenAI SDK 客户端。
        base_url: 端点 URL（None=官方 OpenAI；其他 = 兼容协议）。

    实现纪律：满足 ApiClient Protocol 结构性子类型。
    """

    name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        get_known_tool_names: Optional[Callable[[], list]] = None,
        default_headers: Optional[Dict[str, str]] = None,
    ):
        """构造客户端。

        Args:
            api_key: API key（讯飞 / GLM / Kimi 等都用这个字段，按各 vendor
                规范填）。
            model: 默认 model id（如 "gpt-4o" / "deepseek-chat" /
                "moonshot-v1-32k"）。仍可被 CoreRequest.model 覆盖。
            base_url: 自定义 endpoint。None 走官方 OpenAI；其他例：
                - 讯飞 "https://maas-api.xfyun.cn/v1"
                - GLM "https://open.bigmodel.cn/api/paas/v4"
                - DeepSeek "https://api.deepseek.com/v1"
            timeout: SDK 超时（秒）。
            get_known_tool_names: 可选 callable，返回当前已注册的工具名列表。
                Phase H Slice 4 的 TextLeakedToolCallPlugin 用来识别"模型把
                tool_call 当文本输出"的情况。讯飞 / GLM5 / DSML 私有协议路径
                启用。如不传，plugin 自己 fallback 不启用文本提取。
            default_headers: 每个 HTTP 请求自动追加的 header dict。典型用途：
                - OpenRouter 要求 ``HTTP-Referer`` + ``X-Title`` 才给免费额度
                - DashScope 要求 ``x-dashscope-agentapp`` 做应用归因
                - 自建网关要求 ``X-Tenant-Id`` 做多租户路由
                空 dict / None 不注入任何 header。

        Raises:
            ImportError: 没装 openai SDK。
        """
        if not HAS_OPENAI:
            raise ImportError(
                "openai SDK 未安装。请 pip install openai。"
            )

        self.default_model = model
        if base_url:
            from backend.utils.api_host import format_api_host
            self.base_url = format_api_host(base_url, support_api_version=True)
        else:
            self.base_url = base_url
        self._get_known_tool_names = get_known_tool_names

        # 注入项目级默认 headers（Cherry-style UA + HTTP-Referer + X-Title）
        # 防聚合 router 反爬。caller 传的 default_headers 会 override 同 key 项。
        # self.default_headers 保留合并后的最终值，便于测试断言 / cache key 计算。
        from backend.utils.llm_headers import merge_default_llm_headers
        self.default_headers = merge_default_llm_headers(default_headers, transport="openai_chat")

        client_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "default_headers": self.default_headers,
        }
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        if timeout is not None:
            client_kwargs["timeout"] = timeout

        self.client = AsyncOpenAI(**client_kwargs)

    def _build_sdk_params(self, req: CoreRequest) -> Dict[str, Any]:
        """CoreRequest → OpenAI ChatCompletions SDK params。

        转换规则：
        1. messages: 走 to_openai_messages 净化（孤儿剥离 / 字段校验 /
           讯飞兼容兜底）
        2. system: 如果 req.system 显式传 + messages 里没有 system 角色，
           自动 prepend；如果 messages 里已有 system 就不动（caller 自己安排）
        3. tools: 直接透传（OpenAI std 格式跟 CoreRequest.tools 一致）
        4. model / max_tokens / stream: 标准字段
        5. extras: 任何 SDK 接受字段（temperature / response_format /
           seed / stop / tool_choice / parallel_tool_calls 等）透传
        6. **核心字段不允许被 extras 覆盖**

        Returns:
            可直接 ** 展开到 client.chat.completions.create() 的 dict。
        """
        cleaned_messages = to_openai_messages(req.messages)

        # req.system 显式传 + 历史里没有 system 时 prepend
        if req.system:
            has_system = any(m.get("role") == "system" for m in cleaned_messages)
            if not has_system:
                cleaned_messages = [{"role": "system", "content": req.system}] + cleaned_messages

        sdk_params: Dict[str, Any] = {
            "model": req.model,
            "messages": cleaned_messages,
            "stream": req.stream,
            "max_tokens": req.max_tokens,
        }
        if req.tools:
            sdk_params["tools"] = req.tools

        # extras 透传（不允许覆盖核心字段）
        _CORE_FIELDS = {"model", "messages", "stream", "max_tokens", "tools"}
        for key, value in req.extras.items():
            if key in _CORE_FIELDS:
                continue
            sdk_params[key] = value

        return sdk_params

    async def send_completion(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        """非流式调用，返 OpenAI std message-like dict（fallback / 健康检查路径用）。

        语义跟 send_stream 互补：流式失败后再用非流式重试是 fallback 模式；
        健康检查只需"能成功调用"。

        Returns:
            {
                "content": str | None,
                "tool_calls": list[{"id", "type":"function", "function":{"name", "arguments"}}] | None,
                "finish_reason": str | None,
                "usage": vendor SDK 原 usage 对象（caller 自己 normalize），
            }

        Note:
            - 跟 send_stream 一样跑 outgoing plugin 链（plugin 改 req）
            - 强制 stream=False；如 extras["stream_options"] 存在会被剥（SDK
              不接受非流式时传 stream_options）
            - 错误穿透抛（caller 按 LLMError 归一化）
        """
        # P2-D 风格：跑 outgoing plugin 链（同 send_stream 入口）
        from ..outgoing_plugins import get_default_outgoing_plugin_builder
        builder = get_default_outgoing_plugin_builder()
        plugins = builder.build(self.name, req.model, self.base_url)
        for plugin in plugins:
            req = plugin.transform_request(req, ctx)

        sdk_params = self._build_sdk_params(req)
        sdk_params["stream"] = False  # 强制非流式
        # stream=False 时 SDK 不接受 stream_options（include_usage 自动启用）
        sdk_params.pop("stream_options", None)

        response = await self.client.chat.completions.create(**sdk_params)
        choices = getattr(response, "choices", None) or []
        if not choices:
            return {
                "content": "",
                "tool_calls": None,
                "finish_reason": None,
                "usage": getattr(response, "usage", None),
                "model": getattr(response, "model", None),
                "_raw_response": response,
            }

        choice = choices[0]
        msg = getattr(choice, "message", None)
        if msg is None:
            return {
                "content": "",
                "tool_calls": None,
                "finish_reason": getattr(choice, "finish_reason", None),
                "usage": getattr(response, "usage", None),
                "model": getattr(response, "model", None),
                "_raw_response": response,
            }

        tool_calls_dict = None
        if getattr(msg, "tool_calls", None):
            tool_calls_dict = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        return {
            "content": getattr(msg, "content", None) or "",
            "tool_calls": tool_calls_dict,
            "finish_reason": getattr(choice, "finish_reason", None),
            "usage": getattr(response, "usage", None),
            "model": getattr(response, "model", None),
            # raw_response：完整 SDK ChatCompletion 对象（供 /models/test-connection
            # 中 relay_detection 形态分析用）。下划线前缀语义=内部用，正常 chat 路径忽略。
            "_raw_response": response,
        }

    async def send_stream(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> AsyncIterator[ParsedEvent]:
        """单次 LLM 流式调用，产 ParsedEvent 流。

        实现：
        1. **(P2-D)** 跑 outgoing plugin 链改写 req（按 (name, model, base_url)
           builder 路由）—— Qwen3 thinking mode 等自动注入
        2. _build_sdk_params 把改写后的 CoreRequest 翻译成 OpenAI SDK 入参（强制 stream=True）
        3. await client.chat.completions.create(**params, stream=True) 进流
        4. get_parser("openai", model, base_url, get_known_tool_names) 拿 parser
           （registry 按 base_url 自动选讯飞 / GLM5 / DSML 等私有 grammar）
        5. 异步循环 yield parser 输出，期间检查 ctx.cancel_event 协作式取消

        Note:
            - 强制 stream=True：req.stream=False 在 P1-C 阶段不支持（sync 模式
              要 P2+）。如果传 False，本实现会忽略并强制 stream
            - 错误穿透抛（caller 按 LLMError 归一化）
        """
        # P2-D: 应用 outgoing plugin 链
        from ..outgoing_plugins import get_default_outgoing_plugin_builder
        builder = get_default_outgoing_plugin_builder()
        plugins = builder.build(self.name, req.model, self.base_url)
        for plugin in plugins:
            req = plugin.transform_request(req, ctx)

        sdk_params = self._build_sdk_params(req)
        # P1-C 阶段强制流式（非流式留 P2+）
        sdk_params["stream"] = True

        sdk_stream = await self.client.chat.completions.create(**sdk_params)

        parser, parse_ctx = get_parser(
            provider="openai",
            model=req.model,
            base_url=self.base_url,
            get_known_tool_names=self._get_known_tool_names,
        )

        async for ev in parser(sdk_stream, parse_ctx):
            # 协作式取消（duck-typing；不强制 asyncio.Event）
            if ctx.cancel_event is not None:
                try:
                    if ctx.cancel_event.is_set():
                        return
                except (AttributeError, Exception):
                    pass
            yield ev


__all__ = ["OpenAICompatibleApiClient"]
